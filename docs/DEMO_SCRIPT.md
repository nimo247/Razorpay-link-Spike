# RecoveryOS Demo Script

Target length: **2 minutes 45 seconds**. Keep the product full-screen for most
of the recording; use full-screen facecam only for the opening and close.

## 0:00–0:15 — Hook (facecam)

“A customer does not just fail a payment. They say: ‘I can pay ₹40,000 Friday;
the other ₹8,000 is disputed.’ RecoveryOS understands that commitment without
ever allowing the LLM to become the source of financial truth.”

## 0:15–0:55 — Interpret the commitment (product)

Open the clean ₹48,000 Northstar Retail invoice. Paste:

> I can pay 40k this Friday. The other 8k is disputed.

Show the extracted ₹40,000 promise, resolved date, ₹8,000 dispute, and verbatim
evidence. Say:

“The model proposes an interpretation. Deterministic validation grounds the
evidence, resolves the supported date expression, and checks the balance. A
human confirms the proposal before a commitment or payment link is created.”

## 0:55–1:25 — Establish payment truth (product + Razorpay)

Create the exact-amount Test Mode link and complete the ₹40,000 payment. Return
to the dashboard and show the verified webhook changing the promise to paid,
with ₹8,000 remaining as disputed.

“The customer message did not mark anything paid. A signed Razorpay event did,
and replay protection ensures the event is applied once.”

## 1:25–1:50 — Prove the structural boundary (terminal)

Run the direct invariant test:

```bash
python -m pytest -q \
  tests/test_financial_action_firewall.py::test_mark_paid_is_blocked_without_persisted_provider_event
```

“This bypasses the LLM entirely and calls the authorization layer with a
`MARK_PAID` proposal but no verified provider event. It is blocked regardless
of the supplied arguments. That is an architectural guarantee, not a prompt
instruction.”

## 1:50–2:20 — Architecture and evidence (diagram)

Show the README architecture diagram and verified-results table.

“The model interprets language. The Financial Action Firewall returns
authorized, requires confirmation, or blocked. Deterministic executors perform
approved actions, while the provider-event ledger remains the authority for
payment state.”

Mention only the defensible results:

- 73 passing tests.
- 22 of 22 frozen workflow-safety scenarios passed.
- 120 frozen action cases agree with the oracle contract; this is not model
  accuracy.
- The live 120-case Groq result is not yet measured.

## 2:20–2:35 — Simulator, with the limitation first

“In a synthetic, uncalibrated eight-persona simulation—not a real-world uplift
claim—the promise-aware policy recovered 33.98% versus 30.91%, used 3.319 fewer
contacts per episode, and blocked 213 unsupported paid claims. It also escalated
42% of episodes and achieved fewer full recoveries, so the trade-off remains
visible.”

## 2:35–2:45 — Close (facecam)

“RecoveryOS is not an AI that controls money. It is financial infrastructure
that lets AI understand customers while keeping authority deterministic,
auditable, and tied to verified payment events.”

## Recording checklist

- Hide the taskbar, unrelated tabs, extensions, credentials, and notification
  overlays.
- Use a small picture-in-picture facecam during selected product moments.
- Do not scroll through source files; show only the targeted invariant test.
- Keep the simulation-only label visible beside simulator numbers.
- Do not call the oracle preflight “model accuracy.”
- Replace “live result not yet measured” only after committing an immutable
  Groq run artifact.
