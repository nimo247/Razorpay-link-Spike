# Live Validation Record

Last validated: 1 September 2026

This document records external integration evidence that cannot be established by unit tests alone.

All payment activity described here occurred in Razorpay Test Mode. No real funds were processed.

## Groq extraction evaluation

The frozen extraction smoke set was rerun immediately before deployment preparation.

| Field | Result |
|---|---|
| Model | `openai/gpt-oss-20b` |
| Frozen cases | 10 |
| Evaluated cases | 10 |
| Passed | 10 |
| Failed | 0 |
| Infrastructure errors | 0 |
| Case accuracy | 100% |
| Field accuracy | 100% |
| Evidence grounding | 100% |
| Generated at | `2026-09-01T12:00:23.127620+00:00` |

The cases cover:

- Full payment promise
- Partial payment promise
- Promise with a dispute
- Dispute-only response
- Already-paid claim
- Hinglish/code-switching
- Prompt injection
- Multiple dates
- Amount exceeding balance
- No explicit payment commitment

### Interpretation

This is a small adversarial smoke set, not evidence of broad production accuracy.

The model is not trusted to enforce financial safety. For example, the amount-exceeds-balance case can be extracted correctly while deterministic backend validation prevents it from becoming an actionable promise.

The committed result is available at:

```text
evals/results/promise_extraction_latest.json