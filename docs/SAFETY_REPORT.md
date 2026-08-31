# Workflow Safety Report

Generated: `2026-08-31T20:22:23.426554+00:00`

## Summary

- Frozen safety scenarios: **16**
- Passed: **16**
- Failed or missing: **0**
- Safety-control pass rate: **100.0%**

## Evaluated controls

| Category | Control | Expected behavior | Result |
|---|---|---|---|
| Evidence | Reject fabricated evidence | Ungrounded evidence cannot create a promise | PASSED |
| Financial guardrail | Bound commitments by balance | Promise plus dispute cannot exceed outstanding balance | PASSED |
| Idempotency | Prevent duplicate Payment Links | Repeated requests reuse the existing link | PASSED |
| Webhook | Apply successful payment once | Webhook replay cannot double-count payment | PASSED |
| Financial guardrail | Reject mismatched payment amount | Mismatch routes invoice and promise to human review | PASSED |
| Financial guardrail | Reject unexpected partial payment | Partial event on exact-amount link triggers review | PASSED |
| Reconciliation | Repair missed webhook safely | Paid Razorpay link is reconciled exactly once | PASSED |
| Reconciliation | Verify external payment status | Unpaid external links cannot alter local balances | PASSED |
| Reconciliation | Validate reconciled amount | Reconciliation mismatch triggers human review | PASSED |
| Reconciliation | Preserve state during provider failure | Razorpay failure does not modify financial state | PASSED |
| Deadline worker | Mark overdue promises idempotently | Expired unpaid promise becomes broken exactly once | PASSED |
| Deadline worker | Respect the full promise date | Promise due today remains active | PASSED |
| Deadline worker | Protect completed payments | Paid promise can never be marked broken | PASSED |
| Deadline worker | Maintain correct invoice state | Broken undisputed promise returns invoice to overdue | PASSED |
| Webhook security | Verify the exact raw payload | Payload modification invalidates its signature | PASSED |
| Webhook security | Reject replayed event IDs | Duplicate webhook event ID is recorded once | PASSED |

## Interpretation

These scenarios test deterministic safety controls, state transitions, idempotency, webhook validation, reconciliation, and deadline handling.

This report does **not** claim real-world payment-recovery uplift. It demonstrates that the implemented recovery workflow behaves safely under the frozen scenarios.

## Limitations

- Razorpay is exercised in Test Mode, not with real money.
- Automated workflow tests use SQLite; PostgreSQL row locking is validated by implementation and manual integration testing, not concurrent load testing.
- Groq extraction quality is evaluated separately using the frozen adversarial extraction dataset.
- The current evaluation set is intentionally small and should be described as a safety smoke suite.
