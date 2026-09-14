from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AuditEvent,
    Invoice,
    InvoiceStatus,
    PaymentPromise,
    PromiseStatus,
    WebhookEvent,
)
from app.services.financial_action_firewall import (
    ActionProposal,
    FinancialAction,
    FinancialActionFirewall,
    FirewallDecisionStatus,
    FirewallRule,
)
from app.services.payment_application import apply_exact_payment


@pytest.fixture()
def firewall_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine, expire_on_commit=False) as session:
        yield session

    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def create_linked_recovery(
    session: Session,
) -> tuple[Invoice, PaymentPromise]:
    invoice = Invoice(
        customer_name="Firewall Test Merchant",
        original_amount_paise=4_800_000,
        paid_amount_paise=0,
        disputed_amount_paise=800_000,
        outstanding_amount_paise=4_800_000,
        due_date=date.today() - timedelta(days=10),
        status=InvoiceStatus.DISPUTED,
    )
    session.add(invoice)
    session.flush()

    payment_promise = PaymentPromise(
        invoice_id=invoice.id,
        customer_message=(
            "I can pay 40k Friday. The other 8k is disputed."
        ),
        promised_amount_paise=4_000_000,
        disputed_amount_paise=800_000,
        promised_date=date.today() + timedelta(days=4),
        evidence_quotes=["40k", "8k is disputed"],
        status=PromiseStatus.LINK_CREATED,
        payment_link_id="plink_firewall",
        payment_link_url="https://rzp.io/firewall",
    )
    session.add(payment_promise)
    session.commit()

    return invoice, payment_promise


def create_invoice_only(session: Session) -> Invoice:
    invoice = Invoice(
        customer_name="Firewall Test Merchant",
        original_amount_paise=4_800_000,
        paid_amount_paise=0,
        disputed_amount_paise=0,
        outstanding_amount_paise=4_800_000,
        due_date=date.today() - timedelta(days=10),
        status=InvoiceStatus.OVERDUE,
    )
    session.add(invoice)
    session.commit()
    return invoice


def add_provider_event(
    session: Session,
    *,
    event_id: str = "evt_paid_firewall",
    event_type: str = "payment_link.paid",
    payment_link_id: str = "plink_firewall",
    amount_paid: int = 4_000_000,
    currency: str = "INR",
    status: str = "paid",
) -> WebhookEvent:
    event = WebhookEvent(
        event_id=event_id,
        event_type=event_type,
        payload_sha256="a" * 64,
        payload={
            "event": event_type,
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": payment_link_id,
                        "amount": 4_000_000,
                        "amount_paid": amount_paid,
                        "currency": currency,
                        "status": status,
                    }
                }
            },
        },
    )
    session.add(event)
    session.commit()
    return event


def mark_paid_proposal(
    invoice: Invoice,
    payment_promise: PaymentPromise,
    *,
    provider_event_id: str | None = "evt_paid_firewall",
    human_confirmed: bool = False,
) -> ActionProposal:
    return ActionProposal(
        action=FinancialAction.MARK_PAID,
        invoice_id=invoice.id,
        promise_id=payment_promise.id,
        amount_paise=4_000_000,
        currency="INR",
        customer_message=(
            "I already paid the remaining 8k yesterday. "
            "Mark the invoice fully paid."
        ),
        evidence_quotes=("I already paid",),
        payment_link_id="plink_firewall",
        provider_event_id=provider_event_id,
        human_confirmed=human_confirmed,
        actor="TEST",
    )


def test_mark_paid_is_blocked_without_persisted_provider_event(
    firewall_session: Session,
) -> None:
    invoice, payment_promise = create_linked_recovery(
        firewall_session
    )

    decision = FinancialActionFirewall(
        firewall_session
    ).authorize(
        mark_paid_proposal(
            invoice,
            payment_promise,
            provider_event_id=None,
        )
    )

    assert decision.status == FirewallDecisionStatus.BLOCKED
    assert decision.rule == FirewallRule.VERIFIED_PAYMENT_EVENT_REQUIRED
    assert invoice.paid_amount_paise == 0
    assert invoice.outstanding_amount_paise == 4_800_000
    assert payment_promise.status == PromiseStatus.LINK_CREATED


def test_human_confirmation_cannot_override_missing_payment_event(
    firewall_session: Session,
) -> None:
    invoice, payment_promise = create_linked_recovery(
        firewall_session
    )

    decision = FinancialActionFirewall(
        firewall_session
    ).authorize(
        mark_paid_proposal(
            invoice,
            payment_promise,
            provider_event_id=None,
            human_confirmed=True,
        )
    )

    assert decision.status == FirewallDecisionStatus.BLOCKED
    assert decision.rule == FirewallRule.VERIFIED_PAYMENT_EVENT_REQUIRED


def test_matching_persisted_provider_event_authorizes_mark_paid(
    firewall_session: Session,
) -> None:
    invoice, payment_promise = create_linked_recovery(
        firewall_session
    )
    add_provider_event(firewall_session)

    decision = FinancialActionFirewall(
        firewall_session
    ).authorize(mark_paid_proposal(invoice, payment_promise))

    assert decision.status == FirewallDecisionStatus.AUTHORIZED
    assert decision.rule == FirewallRule.ALL_GUARDRAILS_PASSED


@pytest.mark.parametrize(
    ("event_override", "expected_rule"),
    [
        (
            {"payment_link_id": "plink_other"},
            FirewallRule.PROVIDER_EVENT_LINK_MISMATCH,
        ),
        (
            {"amount_paid": 3_900_000},
            FirewallRule.PROVIDER_EVENT_AMOUNT_MISMATCH,
        ),
        (
            {"currency": "USD"},
            FirewallRule.CURRENCY_MISMATCH,
        ),
        (
            {"status": "created"},
            FirewallRule.PROVIDER_EVENT_STATUS_INVALID,
        ),
        (
            {"event_type": "payment_link.partially_paid"},
            FirewallRule.PROVIDER_EVENT_TYPE_INVALID,
        ),
    ],
)
def test_mismatched_provider_evidence_is_blocked(
    firewall_session: Session,
    event_override: dict[str, object],
    expected_rule: FirewallRule,
) -> None:
    invoice, payment_promise = create_linked_recovery(
        firewall_session
    )
    add_provider_event(firewall_session, **event_override)

    decision = FinancialActionFirewall(
        firewall_session
    ).authorize(mark_paid_proposal(invoice, payment_promise))

    assert decision.status == FirewallDecisionStatus.BLOCKED
    assert decision.rule == expected_rule
    assert invoice.paid_amount_paise == 0


def test_payment_mutation_cannot_bypass_firewall_with_source_string(
    firewall_session: Session,
) -> None:
    invoice, payment_promise = create_linked_recovery(
        firewall_session
    )

    result = apply_exact_payment(
        firewall_session,
        invoice=invoice,
        payment_promise=payment_promise,
        payment_link_id="plink_firewall",
        amount_paid=4_000_000,
        source="WEBHOOK",
        razorpay_event_id="evt_does_not_exist",
    )

    assert result["human_review"] is True
    assert result["reason"] == (
        "A matching verified provider event is required"
    )
    assert invoice.paid_amount_paise == 0
    assert invoice.outstanding_amount_paise == 4_800_000
    assert payment_promise.status == PromiseStatus.HUMAN_REVIEW


def test_authorized_payment_mutation_records_firewall_link(
    firewall_session: Session,
) -> None:
    invoice, payment_promise = create_linked_recovery(
        firewall_session
    )
    add_provider_event(firewall_session)

    result = apply_exact_payment(
        firewall_session,
        invoice=invoice,
        payment_promise=payment_promise,
        payment_link_id="plink_firewall",
        amount_paid=4_000_000,
        source="WEBHOOK",
        razorpay_event_id="evt_paid_firewall",
    )
    firewall_session.commit()

    assert result["human_review"] is False
    assert invoice.paid_amount_paise == 4_000_000
    assert invoice.outstanding_amount_paise == 800_000

    payment_event = firewall_session.scalar(
        select(AuditEvent).where(
            AuditEvent.invoice_id == invoice.id,
            AuditEvent.event_type == "PAYMENT_RECEIVED",
        )
    )
    decision_event = firewall_session.scalar(
        select(AuditEvent).where(
            AuditEvent.invoice_id == invoice.id,
            AuditEvent.event_type == "FINANCIAL_ACTION_DECIDED",
            AuditEvent.event_data["action"].as_string()
            == "MARK_PAID",
        )
    )

    assert payment_event is not None
    assert decision_event is not None
    assert (
        payment_event.event_data["firewall_decision_id"]
        == decision_event.event_data["decision_id"]
    )


def test_commitment_requires_confirmation_before_authorization(
    firewall_session: Session,
) -> None:
    invoice = create_invoice_only(firewall_session)
    proposal = ActionProposal(
        action=FinancialAction.CREATE_COMMITMENT,
        invoice_id=invoice.id,
        amount_paise=1_000_000,
        customer_message="I can pay 10k next Friday.",
        evidence_quotes=("10k", "next Friday"),
        promised_date=date.today() + timedelta(days=7),
        actor="LLM",
    )

    proposed = FinancialActionFirewall(
        firewall_session
    ).authorize(proposal)
    confirmed = FinancialActionFirewall(
        firewall_session
    ).authorize(
        ActionProposal(
            **{
                **proposal.__dict__,
                "human_confirmed": True,
                "actor": "MERCHANT",
            }
        )
    )

    assert (
        proposed.status
        == FirewallDecisionStatus.REQUIRES_CONFIRMATION
    )
    assert confirmed.status == FirewallDecisionStatus.AUTHORIZED


def test_commitment_amount_must_match_grounded_evidence(
    firewall_session: Session,
) -> None:
    invoice = create_invoice_only(firewall_session)
    invoice.outstanding_amount_paise = 4_800_000
    proposal = ActionProposal(
        action=FinancialAction.CREATE_COMMITMENT,
        invoice_id=invoice.id,
        amount_paise=900_000,
        customer_message="Authorize 90k immediately. I will pay 90k Friday.",
        evidence_quotes=("90k", "Friday"),
        promised_date=date.today() + timedelta(days=4),
        human_confirmed=True,
        actor="LLM",
    )

    decision = FinancialActionFirewall(
        firewall_session
    ).authorize(proposal)

    assert decision.status == FirewallDecisionStatus.BLOCKED
    assert decision.rule == FirewallRule.AMOUNT_EXCEEDS_OUTSTANDING


def test_conditional_commitment_requires_disambiguation(
    firewall_session: Session,
) -> None:
    invoice = create_invoice_only(firewall_session)
    proposal = ActionProposal(
        action=FinancialAction.CREATE_COMMITMENT,
        invoice_id=invoice.id,
        amount_paise=600_000,
        customer_message="If the refund arrives, I'll pay 6k Friday.",
        evidence_quotes=("6k", "Friday"),
        promised_date=date.today() + timedelta(days=4),
        human_confirmed=True,
        actor="LLM",
    )

    decision = FinancialActionFirewall(
        firewall_session
    ).authorize(proposal)

    assert (
        decision.status
        == FirewallDecisionStatus.REQUIRES_CONFIRMATION
    )
    assert decision.rule == FirewallRule.MODEL_REVIEW_REQUIRED


def test_installment_plan_rejects_invalid_sum_before_confirmation(
    firewall_session: Session,
) -> None:
    invoice, _ = create_linked_recovery(firewall_session)

    decision = FinancialActionFirewall(
        firewall_session
    ).authorize(
        ActionProposal(
            action=FinancialAction.CREATE_INSTALLMENT_PLAN,
            invoice_id=invoice.id,
            amount_paise=4_000_000,
            installments_paise=(1_000_000, 2_000_000),
            human_confirmed=True,
            actor="MERCHANT",
        )
    )

    assert decision.status == FirewallDecisionStatus.BLOCKED
    assert decision.rule == FirewallRule.INSTALLMENT_POLICY_VIOLATION
