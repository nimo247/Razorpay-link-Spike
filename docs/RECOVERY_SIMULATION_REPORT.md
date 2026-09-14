# Recovery Simulator V1 Report

> **SIMULATION ONLY — NOT A REAL-WORLD RECOVERY CLAIM**

Generated: `2026-09-14T11:16:00.043471+00:00`
Deterministic result: `16052a7483291e3cd9430e7697bfe4842daad0505bf53336250421f277f6da93`

## Scope

- Synthetic personas: **8** (frozen at exactly eight)
- Episodes per persona and policy: **100**
- Total simulated episodes: **1600**
- Fixed seed: **247**
- Horizon: **21 simulated days**
- Historical calibration: **none**

All behavior probabilities are fictional test assumptions. The results
demonstrate state-machine and policy behavior under those assumptions;
they do not estimate merchant revenue, collections performance, or
Razorpay recovery uplift.

## Simulated comparison

| Metric | Generic exact-link reminders | Promise-aware Firewall |
|---|---:|---:|
| Simulated amount recovery | 30.9% | 34.0% |
| Full recovery | 22.0% | 15.4% |
| Any recovery | 22.0% | 37.4% |
| Average contacts | 5.188 | 1.869 |
| Human escalation | 0.0% | 42.0% |
| Confirmations per episode | 0.000 | 0.934 |
| Blocked unverified paid claims | 0 | 213 |
| False payment-state changes | 0 | 0 |

The delta is intentionally named **simulated policy delta**, not uplift:

- Amount-recovery-rate delta: **+3.1%**
- Average-contact delta: **-3.319**

## Per-persona amount recovery

| Persona | Generic reminders | Promise-aware Firewall |
|---|---:|---:|
| `cooperative_full_payer` | 100.0% | 84.0% |
| `partial_payer` | 0.0% | 22.8% |
| `chronic_late_payer` | 48.0% | 17.0% |
| `dispute_heavy_customer` | 0.0% | 3.0% |
| `adversarial_paid_claimant` | 28.0% | 22.0% |
| `vague_communicator` | 0.0% | 3.0% |
| `hinglish_customer` | 0.0% | 33.1% |
| `cashflow_constrained_customer` | 0.0% | 14.0% |

## Safety result

- False payment-state changes: **0**
- Policy violations: **0**
- Unverified paid claims blocked by the promise-aware policy: **213**

A simulated payment changes the invoice balance only after the harness
persists a provider event and calls the production `apply_exact_payment`
path, which invokes the Financial Action Firewall. Customer claims alone
cannot mutate the balance.

## Deliberate exclusions

- No promise-reliability model
- No multi-turn negotiation
- No contextual bandit
- No omnichannel delivery
- No real customer or repayment data
- No claim of causal or production recovery improvement
