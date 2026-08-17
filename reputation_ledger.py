# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
ReputationLedger
================

A reusable Intelligent Contract primitive for *consensus-backed reputation*:
a subject registers public evidence URLs (portfolio, contributions, GitHub,
publications, ...) against an on-chain rubric, and the network -- not a single
"AI judge" -- produces a reputation score and tier. The reputation is stored
as a **ledger**: a history of independently-agreed assessments whose final
value is a *median* (robust to a single outlier or gaming attempt), mapped to
a deterministic tier a composing contract can gate on.

Why this is more than "AI decides X"
-------------------------------------
1. Rubric + weights are on-chain and configurable. The deployer sets a
   `scheme_name`, a natural-language `rubric`, an ordered list of `dimensions`
   and a matching list of integer `weights`. Nobody can change them after
   deploy. The AI's only job is to score each fixed dimension 0-100 against
   that rubric; the *aggregate score and tier are deterministic arithmetic*
   on the agreed dimension scores.
2. Evidence is public and validator-checked. A subject registers a bounded
   list of evidence URLs. Every validator **independently re-fetches each
   URL** and re-scores the dimensions; the leader's scores are only accepted
   when every dimension agrees within a tolerance, the weighted aggregate
   agrees within a tolerance, the derived *tier* matches exactly, and the
   reachable-evidence count matches exactly. Unreachable evidence can't be
   hidden -- validators must see the same set of sources.
3. The stored score is a median of the last `MAX_HISTORY` agreed scores, not
   the raw leader output. One unusually high or low assessment can move the
   median only slightly, so a subject cannot farm a reputation with a single
   rigged transaction and a griefer cannot zero it with a single hit.
4. Red flags cap trust. If the evaluators flag the evidence as inconsistent
   (claims not matching sources, suspected fabrication), the subject's tier is
   capped at `bronze` -- flagged evidence can't earn a trusting tier until the
   evidence is fixed and reassessed.
5. Deterministic, unit-testable helpers. `_weighted_score`, `_tier_for_score`,
   `_tier_rank`, `_median`, `_within_tolerance`, `_strip_code_fence`,
   `_parse_json_object`, `_consensus_ok` are plain, I/O-free Python tested in
   `tests/direct/test_helpers.py` with no VM.

Reuse pattern
-------------
A marketplace, DAO, or grants contract holds this contract's address and calls
`get_qualified(subject_id, min_tier)` to gate access / payouts / voting on a
subject's consensus-backed reputation tier, and `get_subject(subject_id)` to
read the numeric score and evidence. Registration is self-serve (a subject
registers their own evidence) or registrar-backed (the deployer can register a
subject on their behalf); a composing contract can act as the deployer's
address to vouch for users.

Trust model / limitations
-------------------------
- The rubric and dimensions are set once at deploy; a scheme whose rubric is
  vague produces noisy scores. Rubrics should be explicit about what earns a
  top score.
- The contract verifies agreement *between* validators about the evidence, not
  the truth of the evidence itself. An attacker who controls all evidence URLs
  can influence scores; keep the evidence set public and auditable.
- Reputation is an on-chain number, not identity. A subject can register under
  multiple addresses (self-sybil). The tier is a *primitive* other contracts
  combine with their own sybil resistance.
"""

from genlayer import *
from dataclasses import dataclass
import json

# --- Tunable constants ---------------------------------------------------

# Max evidence URLs a subject may register.
MAX_EVIDENCE_URLS = 6

# Evidence page excerpt length fed to the model (bound the prompt size).
MAX_EVIDENCE_CHARS = 4000

# Every dimension score must be within this many points of the leader's, and
# the weighted aggregate within this many points too.
SCORE_TOLERANCE = 10

# Number of recent agreed scores kept in the ledger history. The stored
# reputation is the median of these.
MAX_HISTORY = 5

# A subject can be reassessed at most once per this window.
COOLDOWN_SECONDS = 604800  # 7 days

# An assessment needs at least this many evidence URLs to be reachable,
# otherwise the result is unusable and the assessment fails.
MIN_REACHABLE_EVIDENCE = 1

TIERS = ("none", "bronze", "silver", "gold", "platinum")


def _tier_for_score(score: int) -> str:
    """Deterministic mapping from a 0-100 score to a tier.

    80+  platinum
    65+  gold
    50+  silver
    35+  bronze
    else none
    """
    if score >= 80:
        return "platinum"
    if score >= 65:
        return "gold"
    if score >= 50:
        return "silver"
    if score >= 35:
        return "bronze"
    return "none"


def _tier_rank(tier: str) -> int:
    return TIERS.index(tier)


@allow_storage
@dataclass
class Subject:
    subject: Address
    status: str                # "pending" | "assessed"
    evidence_urls: DynArray[str]
    score_history: DynArray[u256]   # agreed weighted scores, newest last
    current_score: u256        # median of score_history
    tier: str                  # deterministic tier of current_score
    red_flagged: bool
    assessment_count: u256
    last_assessed_at: u256

    def as_dict(self) -> dict:
        return {
            "subject": str(self.subject),
            "status": self.status,
            "evidence_urls": [u for u in self.evidence_urls],
            "score_history": [int(s) for s in self.score_history],
            "current_score": int(self.current_score),
            "tier": self.tier,
            "red_flagged": self.red_flagged,
            "assessment_count": int(self.assessment_count),
            "last_assessed_at": int(self.last_assessed_at),
        }


@allow_storage
@dataclass
class Assessment:
    subject_id: str
    dimension_scores: DynArray[u256]
    weighted_score: u256
    tier: str
    reachable_evidence: u256
    red_flagged: bool
    reasoning: str
    assessed_at: u256

    def as_dict(self) -> dict:
        return {
            "subject_id": self.subject_id,
            "dimension_scores": [int(s) for s in self.dimension_scores],
            "weighted_score": int(self.weighted_score),
            "tier": self.tier,
            "reachable_evidence": int(self.reachable_evidence),
            "red_flagged": self.red_flagged,
            "reasoning": self.reasoning,
            "assessed_at": int(self.assessed_at),
        }


def _current_timestamp() -> u256:
    """Deterministic per-transaction Unix timestamp (seconds). GenLayer
    pins the stdlib clock to the transaction datetime, so every validator
    computing it sees the same value."""
    import datetime as _dt
    return u256(int(_dt.datetime.now(_dt.timezone.utc).timestamp()))


def _coerce_address(value) -> Address:
    """Normalize an address argument (Address, hex/base64 str, or raw int)
    so write methods don't break depending on how the caller encoded it."""
    if isinstance(value, Address):
        return value
    if isinstance(value, str):
        return Address(value)
    if isinstance(value, int):
        return Address(value.to_bytes(20, "big"))
    return Address(bytes(value))


def _validate_urls(urls: list[str], max_urls: int) -> list[str]:
    """Deterministic URL validation shared by registration and evidence
    updates. No I/O. Rejects non-http(s) URLs and over-length lists."""
    if len(urls) > max_urls:
        raise Exception(f"at most {max_urls} evidence URLs allowed")
    validated: list[str] = []
    for url in urls:
        url = url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise Exception(f"invalid evidence URL: {url!r}")
        if url not in validated:
            validated.append(url)
    if not validated:
        raise Exception("at least one evidence URL is required")
    return validated


def _weighted_score(scores: list[int], weights: list[int]) -> int:
    """Weighted average of dimension scores (integer math only -- GenVM
    calldata has no float type, so every score is kept as an integer).

    ``(sum(s_i * w_i) + W/2) // W`` rounds to the nearest integer.
    Raises ValueError when weights are empty or non-positive."""
    if not weights or any(w <= 0 for w in weights):
        raise ValueError("weights must be a non-empty list of positive ints")
    if len(scores) != len(weights):
        raise ValueError("scores and weights must have the same length")
    total = sum(s * w for s, w in zip(scores, weights))
    wsum = sum(weights)
    return (total + wsum // 2) // wsum


def _median(values: list[int]) -> int:
    """Median of a non-empty list (integer result). Robust to a single
    outlier."""
    if not values:
        raise ValueError("median of empty sequence")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def _within_tolerance(a: int, b: int, tolerance: int) -> bool:
    return abs(a - b) <= tolerance


def _strip_code_fence(raw: str) -> str:
    """Strip a markdown code fence from LLM JSON output if present."""
    s = raw.strip()
    if s.startswith("```"):
        first_newline = s.find("\n")
        s = s[first_newline + 1:] if first_newline != -1 else s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return s


def _parse_json_object(raw) -> dict | None:
    """Parse an agreed consensus payload, tolerating a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(_strip_code_fence(raw))
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None
    return None


def _to_int(value) -> int | None:
    """Coerce an LLM-produced score into an integer 0-100, or None.

    Integer math only: GenVM calldata has no float type, so scores are
    kept as integers end-to-end. Values like ``"80.0"`` are accepted by
    slicing at the decimal point (``float()`` is never invoked)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        num = value
    elif isinstance(value, str):
        text = value.strip()
        if "." in text:
            text = text.split(".")[0]
        if not text:
            return None
        try:
            num = int(text)
        except ValueError:
            return None
    elif isinstance(value, float):
        text = repr(value)
        if "." in text:
            text = text.split(".")[0]
        try:
            num = int(text)
        except ValueError:
            return None
    else:
        return None
    return num if 0 <= num <= 100 else None


def _consensus_ok(data, num_dims: int) -> bool:
    """Deterministic shape check on the agreed consensus payload. Runs on
    the returned leader result, so a malformed or under-evidenced result
    can never be stored even if a validator accepted it."""
    payload = _parse_json_object(data)
    if payload is None:
        return False
    scores = payload.get("scores")
    if not isinstance(scores, list) or len(scores) != num_dims:
        return False
    if any(_to_int(s) is None for s in scores):
        return False
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        return False
    if not isinstance(payload.get("red_flagged"), bool):
        return False
    reachable = payload.get("reachable")
    if not isinstance(reachable, int) or reachable < MIN_REACHABLE_EVIDENCE:
        return False
    return True


# --- Consensus block (leader + validator) ----------------------------------

def _consensus_leader(
    evidence_urls: list[str],
    scheme_name: str,
    rubric: str,
    dimensions: list[str],
    weights: list[int],
) -> dict:
    """Runs independently on every validator. Fetches each evidence URL,
    and asks the model to score each dimension 0-100 against the rubric.
    Unreachable sources are reported as such (they count toward a mismatch
    in the validator, not silently dropped).

    Returns ``{"scores": [...], "reasoning": str, "red_flagged": bool,
    "reachable": int}``."""
    blocks: list[str] = []
    reachable = 0
    for i, url in enumerate(evidence_urls):
        try:
            response = gl.nondet.web.get(url)
            text = response.body.decode("utf-8")[:MAX_EVIDENCE_CHARS]
            reachable += 1
        except Exception:
            text = "(evidence source unreachable)"
        blocks.append(f"Evidence {i + 1}: {url}\n{text}")

    dim_lines = "\n".join(
        f"- {dim} (weight {weight})"
        for dim, weight in zip(dimensions, weights)
    )
    prompt = f"""You are a neutral reputation assessor for the scheme "{scheme_name}".
Score the subject on each dimension from 0 to 100. 100 is exceptional, 50 is
average, 0 is no evidence of the quality at all. Be strict and fair; base every
score on the evidence below, never on assumptions about the person.

RUBRIC:
{rubric}

DIMENSIONS (score each one):
{dim_lines}

EVIDENCE:
{"\n\n".join(blocks)}

Score only the dimensions listed. Then set "red_flagged" to true if the evidence
is inconsistent, contradictory, or appears fabricated (claims that the sources
do not support); otherwise false. Set "reachable" to the number of evidence
sources that were reachable and readable.

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"scores": [<0-100 per dimension, in order>], "reasoning": "<2-3 sentences>", "red_flagged": <true|false>, "reachable": <int>}}"""

    parsed = gl.nondet.exec_prompt(prompt, response_format="json")
    payload = _parse_json_object(parsed) or {}
    scores = payload.get("scores", [])
    if not isinstance(scores, list):
        scores = []
    return {
        "scores": [(_to_int(s) or 0) for s in scores],
        "reasoning": str(payload.get("reasoning", "")),
        "red_flagged": bool(payload.get("red_flagged", False)),
        "reachable": int(payload.get("reachable", 0)),
    }


def _consensus_validator(
    leaders_res,
    evidence_urls: list[str],
    scheme_name: str,
    rubric: str,
    dimensions: list[str],
    weights: list[int],
    tolerance: int = SCORE_TOLERANCE,
) -> bool:
    """The equivalence check. Runs on every validator, which independently
    re-fetches every evidence URL and re-scores the dimensions. The leader's
    result is accepted only if:

    - the leader result passes the deterministic shape check;
    - every dimension score agrees within `tolerance`;
    - the weighted aggregates agree within `tolerance`;
    - the deterministic tier derived from the aggregate matches exactly
      (a leader result that straddles a tier boundary is rejected);
    - the reachable-evidence count matches exactly.

    The `reasoning` text is deliberately NOT compared: two good evaluators
    will word it differently."""
    if not isinstance(leaders_res, gl.vm.Return):
        return False
    leader_data = leaders_res.calldata
    if not isinstance(leader_data, dict):
        return False
    if not _consensus_ok(leader_data, len(dimensions)):
        return False

    my_data = _consensus_leader(evidence_urls, scheme_name, rubric, dimensions, weights)
    if not _consensus_ok(my_data, len(dimensions)):
        return False

    if int(leader_data.get("reachable", -1)) != int(my_data.get("reachable", -1)):
        return False
    if bool(leader_data.get("red_flagged")) != bool(my_data.get("red_flagged")):
        return False

    leader_scores = [(_to_int(s) or 0) for s in leader_data["scores"]]
    my_scores = [(_to_int(s) or 0) for s in my_data["scores"]]
    if len(leader_scores) != len(my_scores):
        return False

    for ls, ms in zip(leader_scores, my_scores):
        if not _within_tolerance(ls, ms, tolerance):
            return False

    leader_weighted = _weighted_score(leader_scores, weights)
    my_weighted = _weighted_score(my_scores, weights)
    if not _within_tolerance(leader_weighted, my_weighted, tolerance):
        return False
    if _tier_for_score(leader_weighted) != _tier_for_score(my_weighted):
        return False

    return True


# --- The contract ----------------------------------------------------------

class ReputationLedger(gl.Contract):
    scheme_name: str
    rubric: str
    dimensions: DynArray[str]
    weights: DynArray[u256]
    cooldown_seconds: u256
    registrar: Address
    subjects: TreeMap[str, Subject]
    subject_ids_by_address: TreeMap[str, str]
    assessments: TreeMap[str, Assessment]
    subject_count: u256

    def __init__(
        self,
        scheme_name: str,
        rubric: str,
        dimensions: list[str],
        weights: list[int],
        cooldown_seconds: int = COOLDOWN_SECONDS,
    ):
        self.scheme_name = scheme_name.strip()
        self.rubric = rubric.strip()
        if not self.scheme_name:
            raise gl.vm.UserError("scheme_name must not be empty")
        if not self.rubric:
            raise gl.vm.UserError("rubric must not be empty")

        dims = [d.strip() for d in dimensions]
        if not all(dims):
            raise gl.vm.UserError("dimensions must not be empty")
        if len(dims) != len(weights):
            raise gl.vm.UserError("dimensions and weights must have the same length")
        if any(w <= 0 for w in weights):
            raise gl.vm.UserError("weights must be positive")

        for d in dims:
            self.dimensions.append(d)
        for w in weights:
            self.weights.append(u256(int(w)))
        self.cooldown_seconds = u256(int(cooldown_seconds))
        self.registrar = gl.message.sender_address
        self.subject_count = u256(0)

    # -- Registration / evidence -----------------------------------------

    @gl.public.write
    def register_subject(self, subject_address, evidence_urls: list[str]) -> str:
        """Register a subject's evidence. The caller must be the subject
        themselves, or the deployer (registrar) vouching for them, so no
        third party can register a bad profile against a victim. Returns
        the new subject_id."""
        subject = _coerce_address(subject_address)
        sender = gl.message.sender_address
        if subject != sender and sender != self.registrar:
            raise gl.vm.UserError("only the subject or the registrar can register")

        addr_key = str(subject)
        if addr_key in self.subject_ids_by_address:
            raise gl.vm.UserError("subject already registered")

        urls = _validate_urls(evidence_urls, MAX_EVIDENCE_URLS)

        subject_id = f"subject-{self.subject_count}"
        self.subject_count = self.subject_count + u256(1)

        self.subjects[subject_id] = Subject(
            subject=subject,
            status="pending",
            evidence_urls=urls,
            score_history=[],
            current_score=u256(0),
            tier="none",
            red_flagged=False,
            assessment_count=u256(0),
            last_assessed_at=u256(0),
        )
        self.subject_ids_by_address[addr_key] = subject_id
        return subject_id

    @gl.public.write
    def update_evidence(self, subject_id: str, evidence_urls: list[str]) -> None:
        """Replace a subject's evidence. Only the subject themselves (or the
        registrar) may do this, and only while the subject is still pending.
        Once assessed, evidence can be changed only by re-registering a new
        subject entry."""
        subject_id = str(subject_id)
        subject = self.subjects.get(subject_id)
        if subject is None:
            raise gl.vm.UserError("unknown subject_id")
        if subject.status != "pending":
            raise gl.vm.UserError("evidence can only be updated while pending")

        sender = gl.message.sender_address
        if subject.subject != sender and sender != self.registrar:
            raise gl.vm.UserError("only the subject or the registrar can update evidence")

        urls = _validate_urls(evidence_urls, MAX_EVIDENCE_URLS)
        subject.evidence_urls = urls
        self.subjects[subject_id] = subject

    # -- Assessment -------------------------------------------------------

    @gl.public.write
    def assess_subject(self, subject_id: str) -> dict:
        """Run one consensus assessment of a subject. Anyone may trigger it,
        but a subject can be reassessed at most once per cooldown window, and
        the stored score is a median of recent agreed scores.

        Returns the assessment result dict: ``{weighted_score, tier, scores,
        red_flagged, reachable_evidence, assessment_count, current_score}``."""
        subject_id = str(subject_id)
        subject = self.subjects.get(subject_id)
        if subject is None:
            raise gl.vm.UserError("unknown subject_id")

        now = _current_timestamp()
        if subject.status == "assessed":
            if now < subject.last_assessed_at + self.cooldown_seconds:
                raise gl.vm.UserError("assessment cooldown has not elapsed")

        scheme_name = self.scheme_name
        rubric = self.rubric
        dimensions = [d for d in self.dimensions]
        weights = [int(w) for w in self.weights]
        evidence_urls = [u for u in subject.evidence_urls]

        # The non-deterministic block never touches storage -- it only fetches
        # evidence and proposes dimension scores. Named nested functions (not
        # lambdas) keep GenVM lint's call-graph analysis from reaching the
        # deterministic side effects below.
        def leader_fn():
            return _consensus_leader(
                evidence_urls, scheme_name, rubric, dimensions, weights,
            )

        def validator_fn(leaders_res):
            return _consensus_validator(
                leaders_res,
                evidence_urls, scheme_name, rubric, dimensions, weights,
                SCORE_TOLERANCE,
            )

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        if not _consensus_ok(result, len(dimensions)):
            raise gl.vm.UserError("consensus result was unusable")

        scores = [(_to_int(s) or 0) for s in result["scores"]]
        weighted = _weighted_score(scores, weights)
        tier = _tier_for_score(weighted)
        red_flagged = bool(result["red_flagged"])
        reachable = int(result["reachable"])

        # Red-flagged evidence can't earn a trusting tier.
        if red_flagged and _tier_rank(tier) > _tier_rank("bronze"):
            tier = "bronze"

        # Append to the ledger history and recompute the stored score as the
        # median of the recent window, so a single assessment can't move it
        # much in either direction.
        history = [int(s) for s in subject.score_history]
        history.append(weighted)
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        current = _median(history)
        stored_tier = _tier_for_score(current)
        if red_flagged and _tier_rank(stored_tier) > _tier_rank("bronze"):
            stored_tier = "bronze"

        subject.status = "assessed"
        subject.score_history = [u256(h) for h in history]
        subject.current_score = u256(current)
        subject.tier = stored_tier
        subject.red_flagged = red_flagged
        subject.assessment_count = subject.assessment_count + u256(1)
        subject.last_assessed_at = now
        self.subjects[subject_id] = subject

        self.assessments[subject_id] = Assessment(
            subject_id=subject_id,
            dimension_scores=[u256(s) for s in scores],
            weighted_score=u256(weighted),
            tier=tier,
            reachable_evidence=u256(reachable),
            red_flagged=red_flagged,
            reasoning=str(result["reasoning"]),
            assessed_at=now,
        )

        return {
            "weighted_score": weighted,
            "tier": tier,
            "scores": scores,
            "red_flagged": red_flagged,
            "reachable_evidence": reachable,
            "assessment_count": int(subject.assessment_count),
            "current_score": current,
            "stored_tier": stored_tier,
        }

    # -- Read methods -----------------------------------------------------

    @gl.public.view
    def get_scheme(self) -> dict:
        return {
            "scheme_name": self.scheme_name,
            "rubric": self.rubric,
            "dimensions": [d for d in self.dimensions],
            "weights": [int(w) for w in self.weights],
            "cooldown_seconds": int(self.cooldown_seconds),
            "registrar": str(self.registrar),
        }

    @gl.public.view
    def get_subject(self, subject_id: str) -> dict:
        subject_id = str(subject_id)
        subject = self.subjects.get(subject_id)
        if subject is None:
            raise gl.vm.UserError("unknown subject_id")
        return subject.as_dict()

    @gl.public.view
    def get_subject_by_address(self, subject_address) -> str:
        addr_key = str(_coerce_address(subject_address))
        return self.subject_ids_by_address.get(addr_key, "")

    @gl.public.view
    def get_assessment(self, subject_id: str) -> dict:
        subject_id = str(subject_id)
        assessment = self.assessments.get(subject_id)
        if assessment is None:
            raise gl.vm.UserError("no assessment for this subject")
        return assessment.as_dict()

    @gl.public.view
    def get_subject_count(self) -> u256:
        return self.subject_count

    @gl.public.view
    def get_qualified(self, subject_id: str, min_tier: str) -> bool:
        """Reusable primitive for composing contracts: does the subject's
        stored reputation tier meet `min_tier`? Returns False for unknown
        subjects and for subjects that have never been assessed."""
        min_tier = str(min_tier).strip().lower()
        if min_tier not in TIERS:
            raise gl.vm.UserError(f"min_tier must be one of {TIERS}")

        subject_id = str(subject_id)
        subject = self.subjects.get(subject_id)
        if subject is None or subject.status != "assessed":
            return False
        return _tier_rank(subject.tier) >= _tier_rank(min_tier)
