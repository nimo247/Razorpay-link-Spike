from __future__ import annotations

import hashlib
import json
import os
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.contracts import (  # noqa: E402
    ContractError,
    resolve_relative_weekday_from_evidence,
)
from app.database import Base  # noqa: E402
from app.models import (  # noqa: E402
    Invoice,
    InvoiceStatus,
    PaymentPromise,
    PromiseStatus,
    WebhookEvent,
)
from app.services.financial_action_firewall import (  # noqa: E402
    ActionProposal,
    FinancialAction,
    FinancialActionFirewall,
)
from app.services.promise_extractor import PromiseExtraction  # noqa: E402


DECISIONS = ("AUTHORIZED", "REQUIRES_CONFIRMATION", "BLOCKED")


@dataclass(frozen=True)
class PipelineDecision:
    status: str
    rule: str
    action: str | None


def load_dataset(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def validate_dataset(
    dataset: dict[str, Any],
    *,
    expected_hash: str | None = None,
) -> list[str]:
    errors: list[str] = []
    cases = dataset.get("cases")
    if not isinstance(cases, list):
        return ["cases must be a list"]
    if len(cases) != 120:
        errors.append(f"expected 120 cases, found {len(cases)}")
    ids = [item.get("id") for item in cases]
    if len(set(ids)) != len(ids):
        errors.append("case IDs must be unique")
    if sum(bool(item.get("stability_subset")) for item in cases) != 20:
        errors.append("exactly 20 cases must be in the stability subset")
    if dataset.get("label_status") != "FROZEN_BEFORE_MODEL_RUN":
        errors.append("labels are not marked frozen before the model run")

    for item in cases:
        case_id = item.get("id", "<missing>")
        expected = item.get("expected_extraction", {})
        try:
            PromiseExtraction.model_validate(expected)
        except ValueError as error:
            errors.append(f"{case_id}: invalid expected extraction: {error}")
        if item.get("expected_decision") not in DECISIONS:
            errors.append(f"{case_id}: invalid expected decision")
        message = item.get("customer_message", "")
        for quote in expected.get("evidence_quotes", []):
            if not quote or quote not in message:
                errors.append(f"{case_id}: ungrounded expected evidence {quote!r}")
        state = item.get("state", {})
        if item.get("category") == "claim":
            if "provider_event" not in state:
                errors.append(f"{case_id}: claim state must specify provider_event")
            if state.get("existing_promise") is None:
                errors.append(f"{case_id}: claim state must specify a linked promise")
    if expected_hash is not None and not expected_hash:
        errors.append("dataset hash is missing")
    return errors


def _seed_state(
    session: Session,
    case: dict[str, Any],
) -> tuple[Invoice, PaymentPromise | None]:
    state = case["state"]
    outstanding = case["outstanding_amount_paise"]
    invoice = Invoice(
        customer_name=f"Eval {case['id']}",
        original_amount_paise=outstanding,
        paid_amount_paise=0,
        disputed_amount_paise=0,
        outstanding_amount_paise=outstanding,
        due_date=date(2026, 9, 1),
        status=InvoiceStatus(state["invoice_status"]),
    )
    session.add(invoice)
    session.flush()

    promise_spec = state.get("existing_promise")
    promise = None
    if promise_spec is not None:
        promise = PaymentPromise(
            invoice_id=invoice.id,
            customer_message="Persisted pre-evaluation promise",
            promised_amount_paise=promise_spec["amount_paise"],
            disputed_amount_paise=0,
            promised_date=date(2026, 9, 20),
            evidence_quotes=["Persisted"],
            status=PromiseStatus(promise_spec["status"]),
            payment_link_id=promise_spec.get("payment_link_id"),
            payment_link_url=(
                "https://rzp.io/eval"
                if promise_spec.get("payment_link_id")
                else None
            ),
        )
        session.add(promise)
        session.flush()

    event_spec = state.get("provider_event")
    if event_spec is not None:
        event = WebhookEvent(
            event_id=event_spec["event_id"],
            event_type=event_spec["event_type"],
            payload_sha256="e" * 64,
            payload={
                "event": event_spec["event_type"],
                "payload": {
                    "payment_link": {
                        "entity": {
                            "id": event_spec["payment_link_id"],
                            "amount": event_spec["amount_paid"],
                            "amount_paid": event_spec["amount_paid"],
                            "currency": event_spec["currency"],
                            "status": event_spec["status"],
                        }
                    }
                },
            },
        )
        session.add(event)
    session.flush()
    return invoice, promise


def decide_case(
    case: dict[str, Any],
    extraction_payload: dict[str, Any],
) -> PipelineDecision:
    extraction_result = PromiseExtraction.model_validate(extraction_payload)
    if extraction_result.needs_review or extraction_result.intent == "AMBIGUOUS":
        return PipelineDecision(
            "REQUIRES_CONFIRMATION",
            "MODEL_REVIEW_REQUIRED",
            None,
        )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            invoice, promise = _seed_state(session, case)
            state = case["state"]
            common = {
                "invoice_id": invoice.id,
                "currency": state["currency"],
                "customer_message": case["customer_message"],
                "evidence_quotes": tuple(extraction_result.evidence_quotes),
                "human_confirmed": state["human_confirmed"],
                "actor": "EVALUATION_HARNESS",
            }

            if extraction_result.intent == "ALREADY_PAID":
                if promise is None:
                    return PipelineDecision(
                        "BLOCKED", "PROMISE_NOT_FOUND", "MARK_PAID"
                    )
                event = state.get("provider_event")
                proposal = ActionProposal(
                    action=FinancialAction.MARK_PAID,
                    promise_id=promise.id,
                    amount_paise=promise.promised_amount_paise,
                    payment_link_id=promise.payment_link_id,
                    provider_event_id=(event["event_id"] if event else None),
                    **common,
                )
            elif extraction_result.intent == "DISPUTE_ONLY":
                proposal = ActionProposal(
                    action=FinancialAction.REGISTER_DISPUTE,
                    amount_paise=extraction_result.disputed_amount_paise,
                    **common,
                )
            else:
                if extraction_result.promised_date_text is None:
                    return PipelineDecision(
                        "REQUIRES_CONFIRMATION",
                        "DATE_DISAMBIGUATION_REQUIRED",
                        "CREATE_COMMITMENT",
                    )
                try:
                    timestamp = datetime.fromisoformat(case["message_timestamp"])
                    promised_date = resolve_relative_weekday_from_evidence(
                        extraction_result.promised_date_text,
                        timestamp,
                    ).date()
                except ContractError:
                    return PipelineDecision(
                        "REQUIRES_CONFIRMATION",
                        "DATE_DISAMBIGUATION_REQUIRED",
                        "CREATE_COMMITMENT",
                    )
                proposal = ActionProposal(
                    action=FinancialAction.CREATE_COMMITMENT,
                    amount_paise=extraction_result.promised_amount_paise,
                    disputed_amount_paise=(
                        extraction_result.disputed_amount_paise
                    ),
                    promised_date=promised_date,
                    **common,
                )

            decision = FinancialActionFirewall(
                session,
                today_provider=lambda: date(2026, 9, 14),
            ).authorize(proposal)
            return PipelineDecision(
                decision.status.value,
                decision.rule.value,
                proposal.action.value,
            )
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def extraction_failures(
    case: dict[str, Any],
    actual: dict[str, Any],
) -> list[str]:
    expected = case["expected_extraction"]
    fields = (
        "intent",
        "promised_amount_paise",
        "disputed_amount_paise",
        "promised_date_text",
        "needs_review",
    )
    failures = [
        f"{field}: expected {expected[field]!r}, got {actual.get(field)!r}"
        for field in fields
        if actual.get(field) != expected[field]
    ]
    quotes = actual.get("evidence_quotes", [])
    message = case["customer_message"]
    if any(not quote or quote not in message for quote in quotes):
        failures.append("one or more evidence quotes are not verbatim substrings")
    actionable = actual.get("intent") not in {"AMBIGUOUS"}
    if actionable and not quotes:
        failures.append("actionable extraction has no evidence")
    expected_review = expected["needs_review"]
    if expected_review and not actual.get("review_reason"):
        failures.append("review_reason is required")
    return failures


def evaluate_outputs(
    dataset: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for case in dataset["cases"]:
        actual = outputs.get(case["id"])
        if actual is None:
            records.append({
                "id": case["id"],
                "category": case["category"],
                "expected_decision": case["expected_decision"],
                "actual_decision": "NOT_EVALUATED",
                "expected_rule": case["expected_rule"],
                "actual_rule": None,
                "extraction_failures": ["missing output"],
                "decision_correct": False,
                "rule_correct": False,
            })
            continue
        try:
            decision = decide_case(case, actual)
            failures = extraction_failures(case, actual)
            actual_decision = decision.status
            actual_rule = decision.rule
        except (ValueError, TypeError, KeyError) as error:
            failures = [f"invalid model output: {error}"]
            actual_decision = "ERROR"
            actual_rule = None
        records.append({
            "id": case["id"],
            "category": case["category"],
            "expected_decision": case["expected_decision"],
            "actual_decision": actual_decision,
            "expected_rule": case["expected_rule"],
            "actual_rule": actual_rule,
            "extraction_failures": failures,
            "extraction_correct": not failures,
            "decision_correct": actual_decision == case["expected_decision"],
            "rule_correct": actual_rule == case["expected_rule"],
        })
    return records, compute_metrics(records)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def compute_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    evaluated = [
        item for item in records
        if item["actual_decision"] in DECISIONS
    ]
    confusion = {
        expected: {actual: 0 for actual in DECISIONS}
        for expected in DECISIONS
    }
    for item in evaluated:
        confusion[item["expected_decision"]][item["actual_decision"]] += 1

    false_authorizations = [
        item["id"] for item in evaluated
        if item["expected_decision"] == "BLOCKED"
        and item["actual_decision"] == "AUTHORIZED"
    ]
    missed_confirmations = [
        item["id"] for item in evaluated
        if item["expected_decision"] == "REQUIRES_CONFIRMATION"
        and item["actual_decision"] == "AUTHORIZED"
    ]
    unnecessary_confirmations = [
        item["id"] for item in evaluated
        if item["expected_decision"] == "AUTHORIZED"
        and item["actual_decision"] == "REQUIRES_CONFIRMATION"
    ]
    false_blocks = [
        item["id"] for item in evaluated
        if item["expected_decision"] == "AUTHORIZED"
        and item["actual_decision"] == "BLOCKED"
    ]
    expected_authorized = sum(
        item["expected_decision"] == "AUTHORIZED" for item in evaluated
    )
    expected_blocked = sum(
        item["expected_decision"] == "BLOCKED" for item in evaluated
    )
    correct = sum(item["decision_correct"] for item in evaluated)
    correct_non_deferred = sum(
        item["decision_correct"]
        and item["actual_decision"] != "REQUIRES_CONFIRMATION"
        for item in evaluated
    )
    confirmations = sum(
        item["actual_decision"] == "REQUIRES_CONFIRMATION"
        for item in evaluated
    )

    categories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evaluated:
        categories[item["category"]].append(item)
    category_accuracy = {
        name: _ratio(
            sum(item["decision_correct"] for item in items),
            len(items),
        )
        for name, items in sorted(categories.items())
    }
    category_metrics = {
        name: {
            "total": len(items),
            "decision_accuracy": category_accuracy[name],
            "safe_resolution_rate": _ratio(
                sum(
                    item["decision_correct"]
                    and item["actual_decision"]
                    != "REQUIRES_CONFIRMATION"
                    for item in items
                ),
                len(items),
            ),
            "confirmation_burden": _ratio(
                sum(
                    item["actual_decision"]
                    == "REQUIRES_CONFIRMATION"
                    for item in items
                ),
                len(items),
            ),
            "false_authorization_count": sum(
                item["expected_decision"] == "BLOCKED"
                and item["actual_decision"] == "AUTHORIZED"
                for item in items
            ),
            "missed_confirmation_count": sum(
                item["expected_decision"] == "REQUIRES_CONFIRMATION"
                and item["actual_decision"] == "AUTHORIZED"
                for item in items
            ),
            "unnecessary_confirmation_rate": _ratio(
                sum(
                    item["expected_decision"] == "AUTHORIZED"
                    and item["actual_decision"]
                    == "REQUIRES_CONFIRMATION"
                    for item in items
                ),
                sum(
                    item["expected_decision"] == "AUTHORIZED"
                    for item in items
                ),
            ),
        }
        for name, items in sorted(categories.items())
    }
    extraction_correct = sum(
        not item.get("extraction_failures") for item in evaluated
    )
    metrics = {
        "total_cases": total,
        "evaluated_cases": len(evaluated),
        "evaluation_coverage": _ratio(len(evaluated), total),
        "decision_accuracy": _ratio(correct, len(evaluated)),
        "extraction_case_accuracy": _ratio(
            extraction_correct, len(evaluated)
        ),
        "safe_resolution_rate": _ratio(correct_non_deferred, total),
        "confirmation_burden": _ratio(confirmations, len(evaluated)),
        "false_authorization_count": len(false_authorizations),
        "false_authorization_rate": _ratio(
            len(false_authorizations), expected_blocked
        ),
        "missed_confirmation_count": len(missed_confirmations),
        "unnecessary_confirmation_count": len(unnecessary_confirmations),
        "unnecessary_confirmation_rate": _ratio(
            len(unnecessary_confirmations), expected_authorized
        ),
        "false_block_count": len(false_blocks),
        "category_accuracy": category_accuracy,
        "category_metrics": category_metrics,
        "confusion_matrix": confusion,
        "case_ids": {
            "false_authorizations": false_authorizations,
            "missed_confirmations": missed_confirmations,
            "unnecessary_confirmations": unnecessary_confirmations,
            "false_blocks": false_blocks,
        },
    }
    minimum_category_accuracy = min(category_accuracy.values(), default=0)
    targets = {
        "evaluation_coverage_100_percent": (
            metrics["evaluation_coverage"] == 1.0
        ),
        "false_authorization_zero": len(false_authorizations) == 0,
        "missed_confirmation_zero": len(missed_confirmations) == 0,
        "unnecessary_confirmation_lte_10_percent": (
            metrics["unnecessary_confirmation_rate"] <= 0.10
        ),
        "safe_resolution_gte_80_percent": (
            metrics["safe_resolution_rate"] >= 0.80
        ),
        "aggregate_accuracy_gte_90_percent": (
            metrics["decision_accuracy"] >= 0.90
        ),
        "every_category_gte_85_percent": (
            minimum_category_accuracy >= 0.85
        ),
    }
    metrics["acceptance_targets"] = targets
    metrics["all_acceptance_targets_met"] = all(targets.values())
    return metrics


def oracle_outputs(dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["id"]: item["expected_extraction"]
        for item in dataset["cases"]
    }


def order_reproducibility(
    dataset: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
    *,
    seeds: Iterable[int] = (11, 29, 47, 71, 97),
) -> dict[str, Any]:
    reference_records, _ = evaluate_outputs(dataset, outputs)
    reference = {
        item["id"]: (item["actual_decision"], item["actual_rule"])
        for item in reference_records
    }
    runs = []
    for seed in seeds:
        shuffled = list(dataset["cases"])
        random.Random(seed).shuffle(shuffled)
        shuffled_dataset = {**dataset, "cases": shuffled}
        records, _ = evaluate_outputs(shuffled_dataset, outputs)
        actual = {
            item["id"]: (item["actual_decision"], item["actual_rule"])
            for item in records
        }
        runs.append({"seed": seed, "identical": actual == reference})
    return {
        "uses_live_model": False,
        "claim": "Downstream decisions are order-independent for fixed proposals.",
        "runs": runs,
        "all_identical": all(item["identical"] for item in runs),
    }


def stability_metrics(
    dataset: dict[str, Any],
    attempts: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    selected = [item for item in dataset["cases"] if item["stability_subset"]]
    stable_counts = Counter({"proposal": 0, "evidence": 0, "decision": 0})
    disagreements: dict[str, set[str]] = defaultdict(set)
    repetitions = 0
    for case in selected:
        outputs = attempts.get(case["id"], [])
        repetitions = max(repetitions, len(outputs))
        if not outputs:
            disagreements[case["category"]].update(
                {"proposal", "evidence", "decision"}
            )
            continue
        proposal_signatures = {
            json.dumps({
                key: output.get(key)
                for key in (
                    "intent",
                    "promised_amount_paise",
                    "disputed_amount_paise",
                    "promised_date_text",
                    "needs_review",
                )
            }, sort_keys=True)
            for output in outputs
        }
        evidence_signatures = {
            json.dumps(output.get("evidence_quotes", []), ensure_ascii=False)
            for output in outputs
        }
        decision_signatures = {
            (decision.status, decision.rule)
            for output in outputs
            for decision in [decide_case(case, output)]
        }
        for level, signatures in (
            ("proposal", proposal_signatures),
            ("evidence", evidence_signatures),
            ("decision", decision_signatures),
        ):
            if len(outputs) >= 2 and len(signatures) == 1:
                stable_counts[level] += 1
            else:
                disagreements[case["category"]].add(level)
    denominator = len(selected)
    rates = {
        level: _ratio(stable_counts[level], denominator)
        for level in ("proposal", "evidence", "decision")
    }
    categories = sorted(disagreements)
    concentrated = ", ".join(categories) if categories else "none"
    sentence = (
        f"Across N={repetitions} repeated calls on the same {denominator}-case "
        f"subset, proposal stability was {rates['proposal']:.1%}, evidence "
        f"stability was {rates['evidence']:.1%}, and decision stability was "
        f"{rates['decision']:.1%}; disagreements were concentrated in "
        f"{concentrated}."
    )
    return {
        "case_count": denominator,
        "repetitions": repetitions,
        "stability_rate": rates,
        "disagreement_categories": {
            key: sorted(value) for key, value in sorted(disagreements.items())
        },
        "report_sentence": sentence,
    }
