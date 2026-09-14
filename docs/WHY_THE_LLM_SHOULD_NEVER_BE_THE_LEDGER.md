# Why the LLM Should Never Be the Ledger

Payment recovery contains two different problems that look deceptively similar.
The first is linguistic: what did a customer mean by “I can send 40k Friday,
the rest is disputed”? The second is financial: what is the merchant now
allowed to record, request, or mark as paid?

An LLM is useful for the first problem. It is the wrong authority for the
second.

RecoveryOS is built around that separation. The model may interpret a message
and propose a typed action. A deterministic Financial Action Firewall validates
the proposal against evidence, invoice state, merchant policy, and provider
events. Only an authorized proposal reaches an executor, and only verified
Razorpay events may establish payment truth.

## The dangerous shortcut

A conventional agent design often connects model output directly to tools:

```text
customer message → LLM → payment tool
```

That arrangement gives probabilistic text generation authority over durable
financial state. A hallucinated amount, prompt injection, stale conversation,
or malformed tool call can become an invoice mutation. Prompt instructions
such as “never mark an invoice paid without evidence” reduce risk, but they do
not create an architectural boundary. Replacing or compromising the model
should not weaken a financial invariant.

RecoveryOS therefore treats every model output as untrusted input—even when
the output is valid JSON and carries high confidence.

## The authorization boundary

The implemented path is:

```text
propose → validate → authorize → execute → audit
```

The proposal is a typed request such as `CREATE_COMMITMENT`,
`REGISTER_DISPUTE`, `CREATE_PAYMENT_LINK`, `CREATE_INSTALLMENT_PLAN`, or
`MARK_PAID`. The Firewall returns one of three decisions:

- `AUTHORIZED`: every required invariant is satisfied.
- `REQUIRES_CONFIRMATION`: the proposal is structurally valid but needs a
  merchant decision.
- `BLOCKED`: an invariant failed, with the violated rule recorded.

This three-way result matters. A system that sends every difficult case to a
human may look safe while being operationally useless. The evaluation therefore
tracks unnecessary confirmation and confirmation burden alongside false
authorization. A missed confirmation—ground truth requires review but the
system authorizes—is a separate zero-tolerance safety failure.

## Payment truth comes from provider evidence

The strongest invariant is attached to `MARK_PAID`.

A customer can write:

> I already paid through UPI. Mark the invoice complete.

The model may correctly identify that sentence as a payment claim. It still
cannot authorize the state transition. The Firewall independently loads the
referenced event from the trusted provider-event ledger and checks its payment
link, amount, currency, status, and invoice relationship.

Human confirmation is deliberately insufficient. Otherwise a compromised
model could simply route an unsafe mutation through the confirmation path.
Provider evidence—not model confidence and not a caller-supplied `source`
string—is required.

This is tested below the LLM layer. The invariant test calls the Firewall
directly with a `MARK_PAID` proposal and no persisted provider event. The result
must be `BLOCKED` regardless of the proposal arguments. A second test proves
that the payment mutation service cannot be bypassed by supplying a
trusted-sounding source value. These tests address architectural impossibility
rather than prompt robustness.

## Webhooks are evidence only after verification

Provider events are not automatically trusted merely because they arrive at a
webhook route. RecoveryOS verifies HMAC-SHA256 over the exact raw request body
before parsing JSON. It requires `X-Razorpay-Event-Id`, records that identifier
for replay protection, and rejects an event ID reused with different payload
bytes.

Payment application and event recording occur within a database transaction.
Repeated delivery therefore cannot increment the paid amount twice. The state
machine also avoids assuming webhook delivery order; transitions are derived
from persisted facts instead of arrival sequence alone.

The consequence is a narrow chain of authority:

```text
signed raw payload → verified event → persisted provider fact → Firewall decision
```

Customer text never enters that chain.

## Evidence before interpretation

For conversational commitments, the model must return verbatim evidence spans.
Each quote is checked as a contiguous substring of the original message. If the
customer wrote `40k`, the model cannot present `₹40,000` as the quote even if
the normalized numeric interpretation is correct.

Deterministic code then resolves the supported weekday contract and checks
financial bounds. A promise plus a disputed amount cannot exceed the
outstanding balance. Ambiguous or unsupported dates require confirmation
instead of being guessed into a deadline.

This does not make the extraction infallible. It makes each interpretation
inspectable and limits what a bad interpretation can do.

## What the evaluation actually proves

The project separates three kinds of evidence because combining them would
produce an impressive but misleading headline.

| Evaluation | Verified result | Claim boundary |
|---|---:|---|
| Workflow safety | 22/22 scenarios passed | Enumerated deterministic controls work under the test fixtures. |
| Oracle proposal preflight | 120/120 contract matches | Labels and Firewall decisions agree; this is not LLM accuracy. |
| Order replay | 5/5 shuffled runs identical | Downstream decisions are order-independent for fixed proposals. |
| First live Groq run | 92.5% decision accuracy; 68.33% extraction accuracy | Immutable pre-hardening baseline on all 120 frozen cases. |
| Hardened cached replay | 100% decisions; 80% safely resolved | Same outputs after deterministic hardening; regression evidence, not held-out performance. |
| Live stability subset | 90% proposal; 85% evidence; 100% decision | Five calls per case; perfect model reproducibility is not claimed. |
| Recovery simulator | 1,600 synthetic episodes | Policy behavior under fictional assumptions, not expected merchant uplift. |

The 120-case set is frozen and versioned. Its category counts are intentionally
uneven: clean has 20 cases; six categories have 15 each; evidence/date has 10.
Labels were fixed before the first live run, and future changes require a new
dataset version.

The first live run exposed exactly why the layers must be evaluated separately:
the model understated “90k” as ₹9,000 once, missed two ambiguous commitments,
and over-deferred two paid claims. The immutable baseline therefore failed the
locked safety gate. The system was then hardened without editing a model output
or label: grounded amounts are checked independently, conditional language
cannot skip confirmation, customer paid claims are routed to provider-event
verification, and deterministic blockers take precedence over model
uncertainty.

The unchanged-output replay passes every locked target. That is strong
regression evidence for the Firewall, but it is not a clean estimate of
generalization because the failing cases informed the fixes. A new held-out set
would be required for that claim.

The simulator is equally constrained in its claims. Under eight fictional,
uncalibrated personas, the promise-aware policy changed simulated amount
recovery from 30.91% to 33.98% and reduced average contacts from 5.188 to 1.869.
It also reduced full recovery from 22.00% to 15.37% and escalated 42.00% of
episodes to humans. It blocked 213 unsupported paid claims, with zero false
payment-state changes and zero policy violations.

Those figures are useful for exercising infrastructure and exposing trade-offs.
They are not a forecast, an A/B test, or evidence of real collections
performance.

## The broader design lesson

The goal is not to remove the LLM from the system. It is to place it where its
strength is valuable and its uncertainty is containable.

The model handles flexible language. Deterministic code handles invariants.
Humans handle permitted ambiguity. The payment provider supplies external
truth. The ledger preserves what happened and why.

That division makes the system less theatrically autonomous, but more
defensible. In financial software, the most important capability of an agent is
not how many actions it can take. It is how clearly the system can prove which
actions the agent can never take on its own.

## Reproduce the evidence

See the [evaluation protocol](EVALUATION_PROTOCOL.md),
[safety report](SAFETY_REPORT.md), [simulator design](SIMULATOR_DESIGN.md), and
[simulation report](RECOVERY_SIMULATION_REPORT.md). The repository README lists
the exact commands and current claim boundaries.
