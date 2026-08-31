# Razorpay Payment Link and Webhook Spike

## Current status

The local contracts and FastAPI wiring are validated. The external spike is
**not yet validated**: it requires Razorpay test keys, a configured webhook
secret, a public HTTPS URL, and an actual partial test payment. Do not treat the
partial-payment architecture as confirmed until the live pass/fail gate below
has been completed.

This is deliberately an integration spike, not the buildathon application. It
tests the assumptions that would otherwise be expensive to discover later:

1. Standard Payment Links can be created in Razorpay test mode.
2. An exact-amount link can be paid and observed through a signed webhook.
3. A partial-enabled link can emit `payment_link.partially_paid`.
4. Replayed webhook events do not produce a second state transition.
5. LLM evidence must be copied verbatim from the customer input.
6. Relative weekday phrases follow one rule in both code and evaluation labels.

## Written contracts

### Evidence

The extraction prompt in `app/contracts.py` requires character-for-character,
contiguous source quotes. The backend performs no fuzzy matching. A semantically
correct paraphrase such as `₹40,000` is not valid audit evidence when the source
says `40k`; the prompt must instead make the model return `40k`.

Test this contract on the first ten extraction examples before labeling the
remaining dataset. A systematic quote-format mismatch is a prompt-contract bug,
not model underperformance.

### Relative weekdays

- `Friday` and `this Friday` mean the first Friday strictly after the message
  timestamp.
- `next Friday` means seven days after that first future Friday.
- A message sent on Friday therefore resolves `this Friday` to the following
  Friday.
- Timestamps must be timezone-aware. The intended merchant timezone is
  `Asia/Kolkata`.

Unsupported or ambiguous date phrases go to human review.

The model may return a larger exact phrase such as `by Friday`, `Friday please`,
or `pay on next Friday`. The backend isolates exactly one supported weekday
expression inside that verbatim evidence. Zero or multiple weekdays go to
review. This boundary is covered by the contract tests.

## Run locally

Create a virtual environment and install the requirements:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Load the three Razorpay test values from `.env` into your shell, then run:

```bash
uvicorn app.main:app --reload --port 8000
```

Expose `http://localhost:8000/webhooks/razorpay` through an HTTPS tunnel and add
that HTTPS URL as a Razorpay **test-mode** webhook. Subscribe to:

- `payment_link.paid`
- `payment_link.partially_paid`

Use a webhook secret different from the API key secret.

## Exact-amount spike

```bash
curl -X POST http://localhost:8000/payment-links \
  -H 'Content-Type: application/json' \
  -d '{
    "amount_paise": 4000000,
    "reference_id": "spike-exact-001",
    "description": "Promise against INV-1042",
    "accept_partial": false
  }'
```

Open the returned `short_url`, complete a test payment, and verify that the
webhook response reports `payment_link.paid`.

## Partial-payment spike

```bash
curl -X POST http://localhost:8000/payment-links \
  -H 'Content-Type: application/json' \
  -d '{
    "amount_paise": 4800000,
    "reference_id": "spike-partial-001",
    "description": "Partial promise against INV-1042",
    "accept_partial": true,
    "first_min_partial_amount": 4000000
  }'
```

Pay only the allowed partial amount and verify that Razorpay sends
`payment_link.partially_paid`. Preserve the raw request body for signature
verification; parsing and re-serializing JSON before HMAC verification changes
the bytes and invalidates the signature.

## Automated contract tests

Install development dependencies and run the unit and FastAPI route tests. They
do not call Razorpay:

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
```

## Pass/fail gate

Do not build the full application until these are recorded:

| Check | Required result |
|---|---|
| Exact link creation | API returns an ID and `short_url` |
| Exact link payment | Signed `payment_link.paid` received |
| Partial link creation | API accepts `accept_partial` payload |
| Partial payment | Signed `payment_link.partially_paid` received |
| Replay | Reusing `X-Razorpay-Event-Id` returns `duplicate: true` |
| Evidence | First 10 examples return exact source spans |
| Date rule | Code and labels match documented Friday cases |

If the partial-payment event fails, keep the product architecture and create an
exact-amount link for each accepted commitment. The original invoice ledger can
still represent a partial settlement without depending on Razorpay's partial
link state.

`X-Razorpay-Event-Id` is mandatory in this spike. An earlier body-hash fallback
was intentionally removed because two distinct byte-identical payloads could be
misclassified as one event. Confirm that the live webhook includes this header.
