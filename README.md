# ReputationLedger

A GenLayer Intelligent Contract primitive for **consensus-backed reputation**: a subject registers public evidence URLs (portfolio, contributions, GitHub, publications, ...) against an on-chain rubric, and the network — not a single "AI judge" — produces a reputation score and tier. The reputation is stored as a **ledger**: a history of independently-agreed assessments whose final value is a *median* (robust to a single outlier or gaming attempt), mapped to a deterministic tier a composing contract can gate on.

## Deployed contract (Studio / studionet)

- **Contract address:** `0xa7F2cbbbC2e1e22eCca2D893B973258BB5E893e2`
- **Explorer:** https://explorer-studio.genlayer.com/address/0xa7F2cbbbC2e1e22eCca2D893B973258BB5E893e2
- **Registrar:** `0x0Aba17df9848a5Ebc14C2922917FED5C36BB4F78`
- **Scheme:** "Builder Trust Scheme" — dimensions `[contribution_quality, consistency, verifiability]`, weights `[2, 1, 1]`, cooldown 7 days.
- Verified on-chain with `tests/integration/test_deployed_reputation_ledger.py` (8/8 passed, including a real `assess_subject` round that reached consensus). Built from the current integer-only source.

## Why this is more than "AI decides X"

| Concern | How it's handled |
|---|---|
| Rubric + weights are on-chain and immutable | The deployer sets a `scheme_name`, a natural-language `rubric`, an ordered list of `dimensions` and matching `weights` at deploy time. The AI's only job is to score each fixed dimension 0-100 against that rubric; the **aggregate score and tier are deterministic arithmetic** on the agreed dimension scores (`_weighted_score`, `_tier_for_score`). |
| Evidence is public and validator-checked | A subject registers a bounded list of evidence URLs. Every validator **independently re-fetches each URL** and re-scores the dimensions; the leader's scores are accepted only when each dimension agrees within `SCORE_TOLERANCE`, the weighted aggregate agrees within it, the derived *tier* matches exactly, the red-flag verdict matches, and the reachable-evidence count matches. Unreachable evidence can't be hidden. |
| Stored score is a median, not the raw output | `assess_subject` appends the agreed weighted score to `subject.score_history` (last `MAX_HISTORY` = 5) and stores the **median** as `current_score`. One rigged assessment moves a 5-entry median by little, so a subject cannot farm a reputation with a single transaction and a griefer cannot zero it with a single hit. |
| Red flags cap trust | If evaluators flag the evidence as inconsistent/fabricated, the tier is capped at `bronze` — flagged evidence can't earn a trusting tier until the evidence is fixed and reassessed (after the cooldown). |
| Registration is self/registrar-only | A third party cannot register a bad profile against a victim's address: only the subject themselves or the deployer (registrar) can register or update evidence. |
| Reassessment is cooldown-gated | A subject can be reassessed at most once per `cooldown_seconds` (default 7 days), so a subject can't spam the ledger in its own favor. |
| Deterministic, unit-testable helpers | `_weighted_score`, `_tier_for_score`, `_tier_rank`, `_median`, `_within_tolerance`, `_strip_code_fence`, `_parse_json_object`, `_to_int`, `_consensus_ok`, `_validate_urls` are plain, I/O-free Python tested in `tests/direct/test_helpers.py` with no VM. |

> **Integer-only arithmetic.** GenVM calldata has **no float type** — a return value containing a float fails to encode and kills the assessment (`TypeError: not calldata encodable 0.0: float`). This contract keeps every score an integer end-to-end: the LLM is asked for 0-100 integer scores, `_to_int` coerces string/float-looking results by slicing at the decimal point (it never calls `float()`), and `_weighted_score` / `_median` / `_within_tolerance` are integer-math only. Deploy this source; an earlier Studio build that returned float `scores` bypassed this and its `assess_subject` could never succeed.

## State design

```
Scheme (set once at deploy)
  scheme_name:      str
  rubric:           str            # natural-language evaluation rubric
  dimensions:       DynArray[str]  # fixed, ordered dimension names
  weights:          DynArray[u256] # fixed, matching positive weights
  cooldown_seconds: u256
  registrar:        Address        # deployer; may register/update any subject

Subject
  subject:           Address
  status:            str           # "pending" | "assessed"
  evidence_urls:     DynArray[str] # 1..MAX_EVIDENCE_URLS public URLs
  score_history:     DynArray[u256] # agreed weighted scores, newest last
  current_score:     u256          # median of score_history
  tier:              str           # none | bronze | silver | gold | platinum
  red_flagged:       bool
  assessment_count:  u256
  last_assessed_at:  u256

Assessment (latest per subject)
  subject_id:         str
  dimension_scores:   DynArray[u256]
  weighted_score:     u256
  tier:               str
  reachable_evidence: u256
  red_flagged:        bool
  reasoning:          str
  assessed_at:        u256

ReputationLedger
  subjects: TreeMap[str, Subject]            # keyed "subject-<n>"
  subject_ids_by_address: TreeMap[str, str]  # address -> subject_id
  assessments: TreeMap[str, Assessment]      # keyed by subject_id
  subject_count: u256
```

## Tier mapping (deterministic)

| Score | Tier |
|---|---|
| 80 – 100 | platinum |
| 65 – 79 | gold |
| 50 – 64 | silver |
| 35 – 49 | bronze |
| < 35 | none |

If the evidence is `red_flagged`, the tier is capped at `bronze`.

## Lifecycle

```
deploy(scheme_name, rubric, dimensions, weights, cooldown_seconds)
      │
register_subject(subject, evidence_urls)   (subject or registrar)
      │
      ├── update_evidence(subject_id, urls)  (only while "pending")
      │
assess_subject(subject_id)   (anyone; cooldown per subject)
      │
      ├── consensus block: fetch each evidence URL, score each
      │     dimension 0-100 (integer) against the rubric
      │     (validators independently re-fetch + re-score)
      │
      ├── validator checks: per-dimension tolerance, aggregate
      │     tolerance, exact tier match, exact red-flag match,
      │     exact reachable-evidence count
      │
      └── deterministic post-processing: weighted_score -> tier;
            append to score_history; stored current_score = median;
            red-flagged tiers capped at bronze
```

## Public interface

Constructor:
- `__init__(scheme_name, rubric, dimensions, weights, cooldown_seconds=604800)` — sets the immutable scheme; the deployer becomes the `registrar`.

Write methods:
- `register_subject(subject_address, evidence_urls) -> str` — subject-self or registrar only; returns `subject_id`.
- `update_evidence(subject_id, evidence_urls) -> None` — subject/registrar only, while `"pending"`.
- `assess_subject(subject_id) -> dict` — anyone may trigger; runs the consensus round. Returns `{weighted_score, tier, scores, red_flagged, reachable_evidence, assessment_count, current_score, stored_tier}`.

View methods:
- `get_scheme() -> dict`, `get_subject(subject_id) -> dict`, `get_subject_by_address(address) -> str`, `get_assessment(subject_id) -> dict`, `get_subject_count() -> u256`.
- `get_qualified(subject_id, min_tier) -> bool` — the reusable gate: does the subject's stored tier meet `min_tier`? Other contracts call this to gate access/payouts/voting.

## The consensus block (the interesting part)

`assess_subject` closes over the subject's `evidence_urls`, the scheme's `rubric`/`dimensions`/`weights`, and the tolerance, then runs:

```python
result = gl.vm.run_nondet_unsafe(
    lambda: _consensus_leader(evidence_urls, scheme_name, rubric, dimensions, weights),
    lambda leaders_res: _consensus_validator(
        leaders_res, evidence_urls, scheme_name, rubric, dimensions, weights,
        SCORE_TOLERANCE,
    ),
)
```

- **Leader** (`_consensus_leader`): for each evidence URL, `gl.nondet.web.get(url)` then `gl.nondet.exec_prompt(...)` scoring each dimension 0-100 and flagging inconsistent evidence. Unreachable sources are reported as `reachable` count mismatches rather than silently dropped.
- **Validator** (`_consensus_validator`): re-runs the full extraction independently, then requires the deterministic shape check, per-dimension tolerance, aggregate tolerance, an **exact tier match** (a leader result that straddles a tier boundary is rejected even when numerically close), an exact red-flag match, and an exact reachable count. `reasoning` text is deliberately NOT compared — two good evaluators word it differently.

This is genuine multi-source consensus with a real equivalence check — the AI never writes the ledger directly; it only proposes dimension scores that validators must independently corroborate, and the ledger itself is deterministic arithmetic on the agreed numbers.

## Reuse pattern

A marketplace, DAO, or grants contract holds this contract's address and calls `get_qualified(subject_id, min_tier)` to gate access/payouts/voting on a subject's consensus-backed reputation tier, and `get_subject(subject_id)` to read the numeric score and evidence. Registration is self-serve or registrar-backed; a composing contract can act as the deployer's address to vouch for users.

## Trust model / limitations

- The rubric and dimensions are set once at deploy; a vague rubric produces noisy scores. Rubrics should be explicit about what earns a top score.
- The contract verifies agreement *between* validators about the evidence, not the truth of the evidence itself. An attacker who controls all evidence URLs can influence scores; keep the evidence set public and auditable.
- Reputation is an on-chain number, not identity. A subject can register under multiple addresses (self-sybil). The tier is a *primitive* other contracts combine with their own sybil resistance.

## Testing

- `tests/direct/test_helpers.py` — pure-Python unit tests of the deterministic helpers, loaded with a tiny `genlayer` stub (no Studio, no network): `pytest tests/direct/test_helpers.py`
- `tests/direct/test_reputation_ledger.py` — direct-mode tests (no server) for registration/access control, evidence updates, the consensus block (agree / disagree / tier-boundary rejection / reachable-count mismatch / red-flag disagreement), median smoothing, the red-flag tier cap, the cooldown gate, and `get_qualified`: `pytest tests/direct/test_reputation_ledger.py`
- `tests/integration/test_reputation_ledger_integration.py` — full lifecycle against a real GenLayer network (Studio): register -> assess with real web + LLM -> read stored tier -> gate with `get_qualified`: `pytest tests/integration`. Integration tests skip cleanly when no validator network is reachable.
- `tests/integration/test_deployed_reputation_ledger.py` — write-method tests bound to an already-deployed contract (set `DEPLOYED_ADDRESS` or the `DEPLOYED_REPUTATION_ADDRESS` env var): probe, self-register, duplicate/third-party guards, `update_evidence`, `assess_subject`, `get_qualified`. Idempotent against a contract that already has state. Point it at a Studio deployment of the current integer-only source (a float-returning build can't pass the assessment tests).

Test-runner notes (Windows):
- The gltest plugin resolves every env var referenced by `gltest.config.yaml` at `pytest_configure`, so `PRIVATE_KEY_1`, `PRIVATE_KEY_2` and `TESTNET_PRIVATE_KEY` must be set (even for direct tests that never touch the network). Dummy 64-hex values work for direct runs.
- Direct mode downloads the pinned `py-genlayer` runner into `~/.cache/gltest-direct`. On Windows set `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` to a certifi bundle if certificate verification fails.

## Dependency pin

The contract pins `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` in its `Depends` header. Update this hash if you're targeting a different SDK version.
