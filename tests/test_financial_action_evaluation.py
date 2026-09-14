import hashlib
import json
from pathlib import Path

from evals.financial_action_harness import (
    compute_metrics,
    decide_case,
    evaluate_outputs,
    load_dataset,
    oracle_outputs,
    order_reproducibility,
    stability_metrics,
    validate_dataset,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = PROJECT_ROOT / "evals" / "financial_action_cases_v1.json"
LOCK_PATH = PROJECT_ROOT / "evals" / "financial_action_cases_v1.lock.json"
PROVIDER_REGISTRY_PATH = (
    PROJECT_ROOT / "evals" / "provider_event_sequences_v1.json"
)


def load_frozen_dataset():
    dataset, digest = load_dataset(DATASET_PATH)
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    return dataset, digest, lock


def test_dataset_hash_and_pre_model_freeze_are_auditable() -> None:
    dataset, digest, lock = load_frozen_dataset()

    assert digest == lock["sha256"]
    assert digest == hashlib.sha256(DATASET_PATH.read_bytes()).hexdigest()
    assert dataset["label_status"] == "FROZEN_BEFORE_MODEL_RUN"
    assert lock["pre_model_amendments"][0]["previous_sha256"] == (
        "596abbcaac59fc5a42315a6e5a94cad95c0e1bc667614b114ba5f53ce80d8627"
    )
    assert validate_dataset(dataset, expected_hash=digest) == []
    assert len(dataset["cases"]) == 120
    assert sum(case["stability_subset"] for case in dataset["cases"]) == 20


def test_claim_labels_are_explicitly_parameterized_by_provider_state() -> None:
    dataset, _, _ = load_frozen_dataset()
    claims = [
        case for case in dataset["cases"]
        if case["category"] == "claim"
    ]

    assert len(claims) == 15
    assert all("provider_event" in case["state"] for case in claims)
    assert all(case["state"]["existing_promise"] for case in claims)


def test_oracle_proposals_satisfy_locked_decision_targets() -> None:
    dataset, _, _ = load_frozen_dataset()
    _, metrics = evaluate_outputs(dataset, oracle_outputs(dataset))

    assert metrics["all_acceptance_targets_met"] is True
    assert metrics["false_authorization_count"] == 0
    assert metrics["missed_confirmation_count"] == 0
    assert metrics["safe_resolution_rate"] == 0.8


def test_missed_confirmation_is_a_standalone_safety_failure() -> None:
    records = [
        {
            "id": "missed-confirmation",
            "category": "ambiguity",
            "expected_decision": "REQUIRES_CONFIRMATION",
            "actual_decision": "AUTHORIZED",
            "decision_correct": False,
            "extraction_failures": [],
        }
    ]

    metrics = compute_metrics(records)

    assert metrics["missed_confirmation_count"] == 1
    assert (
        metrics["acceptance_targets"]["missed_confirmation_zero"]
        is False
    )


def test_false_authorization_and_unnecessary_confirmation_are_separate() -> None:
    records = [
        {
            "id": "unsafe",
            "category": "guardrail",
            "expected_decision": "BLOCKED",
            "actual_decision": "AUTHORIZED",
            "decision_correct": False,
            "extraction_failures": [],
        },
        {
            "id": "deferred",
            "category": "clean",
            "expected_decision": "AUTHORIZED",
            "actual_decision": "REQUIRES_CONFIRMATION",
            "decision_correct": False,
            "extraction_failures": [],
        },
    ]

    metrics = compute_metrics(records)

    assert metrics["false_authorization_count"] == 1
    assert metrics["unnecessary_confirmation_count"] == 1
    assert metrics["confirmation_burden"] == 0.5


def test_flagship_paid_claim_is_blocked_without_provider_evidence() -> None:
    dataset, _, _ = load_frozen_dataset()
    case = next(
        case for case in dataset["cases"]
        if case["id"] == "ADV-011"
    )

    decision = decide_case(case, case["expected_extraction"])

    assert decision.status == "BLOCKED"
    assert decision.rule == "VERIFIED_PAYMENT_EVENT_REQUIRED"


def test_order_gate_replays_fixed_outputs_without_a_live_model() -> None:
    dataset, _, _ = load_frozen_dataset()
    result = order_reproducibility(
        dataset,
        oracle_outputs(dataset),
        seeds=(11,),
    )

    assert result["uses_live_model"] is False
    assert result["all_identical"] is True


def test_stability_report_is_rendered_even_at_one_hundred_percent() -> None:
    dataset, _, _ = load_frozen_dataset()
    outputs = oracle_outputs(dataset)
    attempts = {
        case["id"]: [outputs[case["id"]] for _ in range(5)]
        for case in dataset["cases"]
        if case["stability_subset"]
    }

    stability = stability_metrics(dataset, attempts)

    assert stability["case_count"] == 20
    assert stability["repetitions"] == 5
    assert stability["stability_rate"] == {
        "proposal": 1.0,
        "evidence": 1.0,
        "decision": 1.0,
    }
    assert "Across N=5" in stability["report_sentence"]
    assert "100.0%" in stability["report_sentence"]
    assert stability["report_sentence"].endswith("none.")


def test_live_amount_understatement_is_caught_by_evidence_contract() -> None:
    dataset, _, _ = load_frozen_dataset()
    case = next(
        case for case in dataset["cases"]
        if case["id"] == "ADV-015"
    )
    live_output = {
        **case["expected_extraction"],
        "promised_amount_paise": 900_000,
    }

    decision = decide_case(case, live_output)

    assert decision.status == "BLOCKED"
    assert decision.rule == "AMOUNT_EXCEEDS_OUTSTANDING"


def test_live_conditional_promise_cannot_skip_confirmation() -> None:
    dataset, _, _ = load_frozen_dataset()
    case = next(
        case for case in dataset["cases"]
        if case["id"] == "AMB-013"
    )
    live_output = {
        **case["expected_extraction"],
        "intent": "PARTIAL_PROMISE",
        "needs_review": False,
        "review_reason": None,
    }

    decision = decide_case(case, live_output)

    assert decision.status == "REQUIRES_CONFIRMATION"
    assert decision.rule == "MODEL_REVIEW_REQUIRED"


def test_provider_event_drives_payment_truth_despite_model_uncertainty() -> None:
    dataset, _, _ = load_frozen_dataset()
    case = next(
        case for case in dataset["cases"]
        if case["id"] == "CLAIM-011"
    )
    live_output = {
        **case["expected_extraction"],
        "intent": "AMBIGUOUS",
        "needs_review": True,
        "review_reason": "Model was uncertain.",
    }

    decision = decide_case(case, live_output)

    assert decision.status == "AUTHORIZED"
    assert decision.rule == "ALL_GUARDRAILS_PASSED"
    assert decision.action == "MARK_PAID"



def test_payment_claim_reaches_provider_invariant_despite_model_uncertainty() -> None:
    dataset, _, _ = load_frozen_dataset()
    case = next(
        case for case in dataset["cases"]
        if case["id"] == "CLAIM-006"
    )
    live_output = {
        **case["expected_extraction"],
        "intent": "AMBIGUOUS",
        "evidence_quotes": [],
        "needs_review": True,
        "review_reason": "No explicit payment commitment found.",
    }

    decision = decide_case(case, live_output)

    assert decision.status == "BLOCKED"
    assert decision.rule == "VERIFIED_PAYMENT_EVENT_REQUIRED"
    assert decision.action == "MARK_PAID"


def test_overbalance_dispute_precedes_model_uncertainty() -> None:
    dataset, _, _ = load_frozen_dataset()
    case = next(
        case for case in dataset["cases"]
        if case["id"] == "DSP-008"
    )
    live_output = {
        **case["expected_extraction"],
        "intent": "AMBIGUOUS",
        "evidence_quotes": ["₹11,000"],
        "needs_review": True,
        "review_reason": "No explicit payment commitment found.",
    }

    decision = decide_case(case, live_output)

    assert decision.status == "BLOCKED"
    assert decision.rule == "AMOUNT_EXCEEDS_OUTSTANDING"
    assert decision.action == "REGISTER_DISPUTE"


def test_provider_registry_separates_known_invalid_and_pending_sequences() -> None:
    registry = json.loads(
        PROVIDER_REGISTRY_PATH.read_text(encoding="utf-8")
    )
    sequences = {item["id"]: item for item in registry["sequences"]}
    allowed_provenance = {
        "RAZORPAY_DOCUMENTATION",
        "REAL_TEST_MODE_CAPTURE",
        "ADVERSARIAL_SYNTHETIC",
        "PENDING_REAL_CAPTURE",
    }

    assert all(
        item["provenance"] in allowed_provenance
        for item in registry["sequences"]
    )
    assert sequences["RZP-DELIVERY-001"]["included_in_frozen_gate"]
    assert (
        sequences["RZP-ADVERSARIAL-003"]["classification"]
        == "INVALID_ADVERSARIAL_SEQUENCE"
    )
    assert (
        sequences["RZP-PAYLINK-PENDING-005"]["included_in_frozen_gate"]
        is False
    )
