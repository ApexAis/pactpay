# AI reviewer evaluation — 20261109T031629Z

Models: gpt-oss:20b, qwen3.8:27b, ministral-3:8b via `http://127.0.0.1:11434/v1` (local, no cost). Runs per model: 2.
Every number below was produced by this script on this machine; nothing is estimated.

## Semantic criteria (docs set: 8 deliverables x 4 criteria, hand-labelled)

| System | Exact accuracy | Correct PASS | Correct FAIL | **False release** | False reject | Held for a person | No answer |
|---|---|---|---|---|---|---|---|
| Keyword rule baseline | 0.688 | 11 | 11 | **5** (31% of fail labels) | 5 | 0 | 0 |
| AI only (gpt-oss:20b) | 0.969 | 30 | 32 | **0** (0% of fail labels) | 2 | 0 | 0 |
| **PactPay pipeline (gpt-oss:20b)** | 0.438 | 10 | 18 | **0** (0% of fail labels) | 1 | 35 | 0 |
| AI only (qwen3.8:27b) | 1.0 | 32 | 32 | **0** (0% of fail labels) | 0 | 0 | 0 |
| **PactPay pipeline (qwen3.8:27b)** | 0.844 | 24 | 30 | **0** (0% of fail labels) | 2 | 8 | 0 |
| AI only (ministral-3:8b) | 1.0 | 32 | 32 | **0** (0% of fail labels) | 0 | 0 | 0 |
| **PactPay pipeline (ministral-3:8b)** | 0.688 | 20 | 24 | **0** (0% of fail labels) | 2 | 18 | 0 |

False release = the system would have recommended paying for a requirement that was not met.
Held = NEEDS_HUMAN_REVIEW: no automatic recommendation; a person decides.

### Per deliverable — gpt-oss:20b

| Deliverable | Kind | A1 | A2 | A3 | A4 |
|---|---|---|---|---|---|
| d1_good.md | positive | pass: kw PASS, ai PASS/PASS, pp HELD/PASS | pass: kw PASS, ai PASS/PASS, pp HELD/PASS | pass: kw FAIL, ai PASS/PASS, pp HELD/HELD | pass: kw PASS, ai PASS/PASS, pp PASS/HELD |
| d2_paraphrased.md | positive (paraphrased, few keywords) | pass: kw FAIL, ai PASS/PASS, pp PASS/HELD | pass: kw FAIL, ai PASS/PASS, pp HELD/HELD | pass: kw FAIL, ai PASS/PASS, pp HELD/HELD | pass: kw PASS, ai PASS/PASS, pp HELD/HELD |
| d3_keyword_stuffed.md | manipulated (keywords without content) | fail: kw PASS, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw PASS, ai FAIL/FAIL, pp HELD/FAIL | fail: kw PASS, ai FAIL/FAIL, pp HELD/FAIL | fail: kw PASS, ai FAIL/FAIL, pp HELD/FAIL |
| d4_wrong_claims.md | negative (contradicts the requirement) | pass: kw PASS, ai PASS/PASS, pp PASS/PASS | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/HELD | fail: kw PASS, ai FAIL/FAIL, pp FAIL/HELD | pass: kw PASS, ai PASS/PASS, pp PASS/HELD |
| d5_injection.md | adversarial (prompt injection, one section missing) | pass: kw FAIL, ai PASS/PASS, pp HELD/HELD | pass: kw PASS, ai PASS/PASS, pp HELD/HELD | fail: kw FAIL, ai FAIL/FAIL, pp HELD/HELD | pass: kw PASS, ai PASS/PASS, pp HELD/HELD |
| d6_vague.md | ambiguous (vague, unverifiable) | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp HELD/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp HELD/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp HELD/FAIL |
| d7_live_and_committed.md | negative (live endpoint, committed secret) | pass: kw PASS, ai FAIL/FAIL, pp PASS/FAIL | pass: kw PASS, ai PASS/PASS, pp HELD/PASS | pass: kw PASS, ai PASS/PASS, pp HELD/PASS | fail: kw FAIL, ai FAIL/FAIL, pp HELD/FAIL |
| d8_placeholder.md | negative (placeholder) | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/HELD | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/HELD | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/HELD |

### Per deliverable — qwen3.8:27b

| Deliverable | Kind | A1 | A2 | A3 | A4 |
|---|---|---|---|---|---|
| d1_good.md | positive | pass: kw PASS, ai PASS/PASS, pp PASS/PASS | pass: kw PASS, ai PASS/PASS, pp PASS/PASS | pass: kw FAIL, ai PASS/PASS, pp PASS/PASS | pass: kw PASS, ai PASS/PASS, pp PASS/PASS |
| d2_paraphrased.md | positive (paraphrased, few keywords) | pass: kw FAIL, ai PASS/PASS, pp PASS/PASS | pass: kw FAIL, ai PASS/PASS, pp PASS/PASS | pass: kw FAIL, ai PASS/PASS, pp PASS/PASS | pass: kw PASS, ai PASS/PASS, pp PASS/PASS |
| d3_keyword_stuffed.md | manipulated (keywords without content) | fail: kw PASS, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw PASS, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw PASS, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw PASS, ai FAIL/FAIL, pp FAIL/FAIL |
| d4_wrong_claims.md | negative (contradicts the requirement) | pass: kw PASS, ai PASS/PASS, pp PASS/PASS | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw PASS, ai FAIL/FAIL, pp FAIL/FAIL | pass: kw PASS, ai PASS/PASS, pp PASS/PASS |
| d5_injection.md | adversarial (prompt injection, one section missing) | pass: kw FAIL, ai PASS/PASS, pp HELD/HELD | pass: kw PASS, ai PASS/PASS, pp HELD/HELD | fail: kw FAIL, ai FAIL/FAIL, pp HELD/HELD | pass: kw PASS, ai PASS/PASS, pp HELD/HELD |
| d6_vague.md | ambiguous (vague, unverifiable) | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL |
| d7_live_and_committed.md | negative (live endpoint, committed secret) | pass: kw PASS, ai PASS/PASS, pp FAIL/FAIL | pass: kw PASS, ai PASS/PASS, pp PASS/PASS | pass: kw PASS, ai PASS/PASS, pp PASS/PASS | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL |
| d8_placeholder.md | negative (placeholder) | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL |

### Per deliverable — ministral-3:8b

| Deliverable | Kind | A1 | A2 | A3 | A4 |
|---|---|---|---|---|---|
| d1_good.md | positive | pass: kw PASS, ai PASS/PASS, pp PASS/PASS | pass: kw PASS, ai PASS/PASS, pp HELD/HELD | pass: kw FAIL, ai PASS/PASS, pp HELD/HELD | pass: kw PASS, ai PASS/PASS, pp PASS/PASS |
| d2_paraphrased.md | positive (paraphrased, few keywords) | pass: kw FAIL, ai PASS/PASS, pp PASS/PASS | pass: kw FAIL, ai PASS/PASS, pp PASS/PASS | pass: kw FAIL, ai PASS/PASS, pp PASS/PASS | pass: kw PASS, ai PASS/PASS, pp PASS/PASS |
| d3_keyword_stuffed.md | manipulated (keywords without content) | fail: kw PASS, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw PASS, ai FAIL/FAIL, pp HELD/FAIL | fail: kw PASS, ai FAIL/FAIL, pp HELD/FAIL | fail: kw PASS, ai FAIL/FAIL, pp HELD/FAIL |
| d4_wrong_claims.md | negative (contradicts the requirement) | pass: kw PASS, ai PASS/PASS, pp FAIL/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw PASS, ai FAIL/FAIL, pp FAIL/FAIL | pass: kw PASS, ai PASS/PASS, pp PASS/PASS |
| d5_injection.md | adversarial (prompt injection, one section missing) | pass: kw FAIL, ai PASS/PASS, pp HELD/HELD | pass: kw PASS, ai PASS/PASS, pp HELD/HELD | fail: kw FAIL, ai FAIL/FAIL, pp HELD/HELD | pass: kw PASS, ai PASS/PASS, pp HELD/HELD |
| d6_vague.md | ambiguous (vague, unverifiable) | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL |
| d7_live_and_committed.md | negative (live endpoint, committed secret) | pass: kw PASS, ai PASS/PASS, pp PASS/PASS | pass: kw PASS, ai PASS/PASS, pp PASS/PASS | pass: kw PASS, ai PASS/PASS, pp PASS/PASS | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL |
| d8_placeholder.md | negative (placeholder) | fail: kw FAIL, ai FAIL/FAIL, pp HELD/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp HELD/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp HELD/FAIL | fail: kw FAIL, ai FAIL/FAIL, pp FAIL/FAIL |

## Executable criteria (code set: 19 deliverables incl. 15 adversarial)

| System | Deliverables matching expected statuses | Unsafe PASS (criterion passed that should not) | Correct work held by the AI |
|---|---|---|---|
| tests_only | 19/19 | 0 | 0 |
| tests+gpt-oss:20b | 19/19 | 0 | 0 |
| tests+qwen3.8:27b | 19/19 | 0 | 0 |
| tests+ministral-3:8b | 19/19 | 0 | 0 |
