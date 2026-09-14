from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts import (
    ContractError,
    commitment_language_requires_confirmation,
    extract_rupee_amounts_from_evidence,
    locate_exact_evidence,
)
from ..models import (
    Invoice,
    InvoiceStatus,
    PaymentPromise,
    PromiseStatus,
    WebhookEvent,
)
from .audit import add_audit_event


class FinancialAction(str, Enum):
    CREATE_PAYMENT_LINK = "CREATE_PAYMENT_LINK"
    CREATE_COMMITMENT = "CREATE_COMMITMENT"
    REGISTER_DISPUTE = "REGISTER_DISPUTE"
    CREATE_INSTALLMENT_PLAN = "CREATE_INSTALLMENT_PLAN"
    MARK_PAID = "MARK_PAID"


class FirewallDecisionStatus(str, Enum):
    AUTHORIZED = "AUTHORIZED"
    REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"
    BLOCKED = "BLOCKED"


class FirewallRule(str, Enum):
    ALL_GUARDRAILS_PASSED = "ALL_GUARDRAILS_PASSED"
    HUMAN_CONFIRMATION_REQUIRED = "HUMAN_CONFIRMATION_REQUIRED"
    INVOICE_NOT_FOUND = "INVOICE_NOT_FOUND"
    INVOICE_ALREADY_PAID = "INVOICE_ALREADY_PAID"
    PROMISE_NOT_FOUND = "PROMISE_NOT_FOUND"
    EXISTING_COMMITMENT = "EXISTING_COMMITMENT"
    PROMISE_INVALID_STATE = "PROMISE_INVALID_STATE"
    INVALID_AMOUNT = "INVALID_AMOUNT"
    AMOUNT_EXCEEDS_OUTSTANDING = "AMOUNT_EXCEEDS_OUTSTANDING"
    AMOUNT_EVIDENCE_MISMATCH = "AMOUNT_EVIDENCE_MISMATCH"
    EVIDENCE_NOT_GROUNDED = "EVIDENCE_NOT_GROUNDED"
    MODEL_REVIEW_REQUIRED = "MODEL_REVIEW_REQUIRED"
    INVALID_PROMISED_DATE = "INVALID_PROMISED_DATE"
    PAYMENT_LINK_MISMATCH = "PAYMENT_LINK_MISMATCH"
    PAYMENT_LINK_AMOUNT_MISMATCH = "PAYMENT_LINK_AMOUNT_MISMATCH"
    VERIFIED_PAYMENT_EVENT_REQUIRED = (
        "VERIFIED_PAYMENT_EVENT_REQUIRED"
    )
    PROVIDER_EVENT_TYPE_INVALID = "PROVIDER_EVENT_TYPE_INVALID"
    PROVIDER_EVENT_STATUS_INVALID = "PROVIDER_EVENT_STATUS_INVALID"
    PROVIDER_EVENT_LINK_MISMATCH = "PROVIDER_EVENT_LINK_MISMATCH"
    PROVIDER_EVENT_AMOUNT_MISMATCH = "PROVIDER_EVENT_AMOUNT_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    INSTALLMENT_POLICY_VIOLATION = "INSTALLMENT_POLICY_VIOLATION"


@dataclass(frozen=True)
class ActionProposal:
    action: FinancialAction
    invoice_id: str
    promise_id: str | None = None
    amount_paise: int | None = None
    disputed_amount_paise: int = 0
    currency: str = "INR"
    customer_message: str | None = None
    evidence_quotes: tuple[str, ...] = field(default_factory=tuple)
    promised_date: date | None = None
    payment_link_id: str | None = None
    provider_event_id: str | None = None
    installments_paise: tuple[int, ...] = field(default_factory=tuple)
    human_confirmed: bool = False
    actor: str = "SYSTEM"


@dataclass(frozen=True)
class FirewallDecision:
    decision_id: str
    action: FinancialAction
    status: FirewallDecisionStatus
    rule: FirewallRule
    reason: str
    evaluated_rules: tuple[str, ...]

    @property
    def authorized(self) -> bool:
        return self.status == FirewallDecisionStatus.AUTHORIZED


class FinancialActionFirewall:
    """Authorize financial actions against trusted database state.

    The caller supplies a proposal, never a trusted payment-state assertion.
    For MARK_PAID, this service independently loads the provider event from
    the persisted ledger and validates its entity, amount, currency and state.
    """

    def __init__(
        self,
        session: Session,
        *,
        today_provider: Callable[[], date] = date.today,
    ) -> None:
        self.session = session
        self.today_provider = today_provider

    def authorize(self, proposal: ActionProposal) -> FirewallDecision:
        evaluated = ["INVOICE_EXISTS"]
        invoice = self.session.scalar(
            select(Invoice)
            .where(Invoice.id == proposal.invoice_id)
            .with_for_update()
        )

        if invoice is None:
            return self._decision(
                proposal,
                None,
                None,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.INVOICE_NOT_FOUND,
                "Invoice not found",
                evaluated,
            )

        payment_promise = self._load_promise(proposal, invoice)

        if proposal.promise_id and payment_promise is None:
            return self._decision(
                proposal,
                invoice,
                None,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.PROMISE_NOT_FOUND,
                "Payment promise not found for this invoice",
                [*evaluated, "PROMISE_BELONGS_TO_INVOICE"],
            )

        if proposal.action == FinancialAction.CREATE_COMMITMENT:
            return self._authorize_commitment(
                proposal,
                invoice,
                payment_promise,
                evaluated,
            )

        if proposal.action == FinancialAction.REGISTER_DISPUTE:
            return self._authorize_dispute(
                proposal,
                invoice,
                payment_promise,
                evaluated,
            )

        if proposal.action == FinancialAction.CREATE_PAYMENT_LINK:
            return self._authorize_payment_link(
                proposal,
                invoice,
                payment_promise,
                evaluated,
            )

        if proposal.action == FinancialAction.CREATE_INSTALLMENT_PLAN:
            return self._authorize_installment_plan(
                proposal,
                invoice,
                payment_promise,
                evaluated,
            )

        return self._authorize_mark_paid(
            proposal,
            invoice,
            payment_promise,
            evaluated,
        )

    def _load_promise(
        self,
        proposal: ActionProposal,
        invoice: Invoice,
    ) -> PaymentPromise | None:
        if proposal.promise_id is None:
            return None

        return self.session.scalar(
            select(PaymentPromise)
            .where(
                PaymentPromise.id == proposal.promise_id,
                PaymentPromise.invoice_id == invoice.id,
            )
            .with_for_update()
        )

    def _authorize_commitment(
        self,
        proposal: ActionProposal,
        invoice: Invoice,
        payment_promise: PaymentPromise | None,
        evaluated: list[str],
    ) -> FirewallDecision:
        if invoice.status == InvoiceStatus.PAID:
            return self._blocked_paid_invoice(
                proposal, invoice, payment_promise, evaluated
            )

        existing_promise_id = self.session.scalar(
            select(PaymentPromise.id)
            .where(PaymentPromise.invoice_id == invoice.id)
            .limit(1)
        )
        if existing_promise_id is not None:
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.EXISTING_COMMITMENT,
                "Invoice already has a payment commitment",
                [*evaluated, "INVOICE_HAS_NO_EXISTING_COMMITMENT"],
            )

        amount = proposal.amount_paise
        if type(amount) is not int or amount <= 0:
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.INVALID_AMOUNT,
                "Commitment amount must be a positive integer",
                [*evaluated, "POSITIVE_COMMITMENT_AMOUNT"],
            )

        if (
            type(proposal.disputed_amount_paise) is not int
            or proposal.disputed_amount_paise < 0
        ):
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.INVALID_AMOUNT,
                "Disputed amount must be a non-negative integer",
                [*evaluated, "NON_NEGATIVE_DISPUTED_AMOUNT"],
            )

        total = amount + proposal.disputed_amount_paise
        if total > invoice.outstanding_amount_paise:
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.AMOUNT_EXCEEDS_OUTSTANDING,
                "Commitment and dispute exceed the outstanding balance",
                [*evaluated, "AMOUNT_WITHIN_OUTSTANDING"],
            )

        evidence_error = self._evidence_error(proposal)
        if evidence_error:
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.EVIDENCE_NOT_GROUNDED,
                evidence_error,
                [*evaluated, "EVIDENCE_IS_VERBATIM"],
            )

        evidence_amounts = extract_rupee_amounts_from_evidence(
            proposal.evidence_quotes
        )
        required_amounts = (amount,) + (
            (proposal.disputed_amount_paise,)
            if proposal.disputed_amount_paise > 0
            else ()
        )
        if any(value not in evidence_amounts for value in required_amounts):
            if any(
                value > invoice.outstanding_amount_paise
                for value in evidence_amounts
            ):
                return self._decision(
                    proposal,
                    invoice,
                    payment_promise,
                    FirewallDecisionStatus.BLOCKED,
                    FirewallRule.AMOUNT_EXCEEDS_OUTSTANDING,
                    "Grounded evidence contains an amount above the balance",
                    [*evaluated, "AMOUNT_WITHIN_OUTSTANDING"],
                )
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.AMOUNT_EVIDENCE_MISMATCH,
                "Proposed amounts do not match grounded evidence",
                [*evaluated, "AMOUNTS_MATCH_GROUNDED_EVIDENCE"],
            )

        if (
            proposal.promised_date is None
            or proposal.promised_date < self.today_provider()
        ):
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.INVALID_PROMISED_DATE,
                "Promised date cannot be in the past",
                [*evaluated, "PROMISED_DATE_IS_CURRENT_OR_FUTURE"],
            )

        if commitment_language_requires_confirmation(
            proposal.customer_message or ""
        ):
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.REQUIRES_CONFIRMATION,
                FirewallRule.MODEL_REVIEW_REQUIRED,
                "Conditional or hedged commitment requires disambiguation",
                [*evaluated, "COMMITMENT_LANGUAGE_IS_UNAMBIGUOUS"],
            )

        return self._confirmation_or_authorization(
            proposal,
            invoice,
            payment_promise,
            [
                *evaluated,
                "POSITIVE_COMMITMENT_AMOUNT",
                "AMOUNT_WITHIN_OUTSTANDING",
                "EVIDENCE_IS_VERBATIM",
                "AMOUNTS_MATCH_GROUNDED_EVIDENCE",
                "PROMISED_DATE_IS_CURRENT_OR_FUTURE",
                "COMMITMENT_LANGUAGE_IS_UNAMBIGUOUS",
            ],
        )

    def _authorize_dispute(
        self,
        proposal: ActionProposal,
        invoice: Invoice,
        payment_promise: PaymentPromise | None,
        evaluated: list[str],
    ) -> FirewallDecision:
        if invoice.status == InvoiceStatus.PAID:
            return self._blocked_paid_invoice(
                proposal, invoice, payment_promise, evaluated
            )

        amount = proposal.amount_paise
        if type(amount) is not int or amount <= 0:
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.INVALID_AMOUNT,
                "Disputed amount must be a positive integer",
                [*evaluated, "POSITIVE_DISPUTED_AMOUNT"],
            )

        if amount > invoice.outstanding_amount_paise:
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.AMOUNT_EXCEEDS_OUTSTANDING,
                "Dispute exceeds the outstanding balance",
                [*evaluated, "AMOUNT_WITHIN_OUTSTANDING"],
            )

        evidence_error = self._evidence_error(proposal)
        if evidence_error:
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.EVIDENCE_NOT_GROUNDED,
                evidence_error,
                [*evaluated, "EVIDENCE_IS_VERBATIM"],
            )

        evidence_amounts = extract_rupee_amounts_from_evidence(
            proposal.evidence_quotes
        )
        if amount not in evidence_amounts:
            if any(
                value > invoice.outstanding_amount_paise
                for value in evidence_amounts
            ):
                return self._decision(
                    proposal,
                    invoice,
                    payment_promise,
                    FirewallDecisionStatus.BLOCKED,
                    FirewallRule.AMOUNT_EXCEEDS_OUTSTANDING,
                    "Grounded evidence contains an amount above the balance",
                    [*evaluated, "AMOUNT_WITHIN_OUTSTANDING"],
                )
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.AMOUNT_EVIDENCE_MISMATCH,
                "Proposed dispute amount does not match grounded evidence",
                [*evaluated, "AMOUNTS_MATCH_GROUNDED_EVIDENCE"],
            )

        return self._confirmation_or_authorization(
            proposal,
            invoice,
            payment_promise,
            [
                *evaluated,
                "POSITIVE_DISPUTED_AMOUNT",
                "AMOUNT_WITHIN_OUTSTANDING",
                "EVIDENCE_IS_VERBATIM",
                "AMOUNTS_MATCH_GROUNDED_EVIDENCE",
            ],
        )

    def _authorize_payment_link(
        self,
        proposal: ActionProposal,
        invoice: Invoice,
        payment_promise: PaymentPromise | None,
        evaluated: list[str],
    ) -> FirewallDecision:
        if invoice.status == InvoiceStatus.PAID:
            return self._blocked_paid_invoice(
                proposal, invoice, payment_promise, evaluated
            )

        if (
            payment_promise is None
            or payment_promise.status != PromiseStatus.VALIDATED
        ):
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.PROMISE_INVALID_STATE,
                "Only validated promises can create Payment Links",
                [*evaluated, "PROMISE_IS_VALIDATED"],
            )

        if (
            proposal.amount_paise
            != payment_promise.promised_amount_paise
            or payment_promise.promised_amount_paise
            > invoice.outstanding_amount_paise
        ):
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.PAYMENT_LINK_AMOUNT_MISMATCH,
                "Payment Link amount does not match a valid commitment",
                [*evaluated, "PAYMENT_LINK_AMOUNT_IS_VALID"],
            )

        if proposal.currency != "INR":
            return self._currency_mismatch(
                proposal, invoice, payment_promise, evaluated
            )

        return self._decision(
            proposal,
            invoice,
            payment_promise,
            FirewallDecisionStatus.AUTHORIZED,
            FirewallRule.ALL_GUARDRAILS_PASSED,
            "Validated commitment may create an exact Payment Link",
            [
                *evaluated,
                "PROMISE_IS_VALIDATED",
                "PAYMENT_LINK_AMOUNT_IS_VALID",
                "CURRENCY_MATCHES",
            ],
        )

    def _authorize_installment_plan(
        self,
        proposal: ActionProposal,
        invoice: Invoice,
        payment_promise: PaymentPromise | None,
        evaluated: list[str],
    ) -> FirewallDecision:
        installments = proposal.installments_paise
        valid_installments = (
            2 <= len(installments) <= 3
            and all(type(value) is int and value > 0 for value in installments)
            and type(proposal.amount_paise) is int
            and sum(installments) == proposal.amount_paise
            and sum(installments) <= invoice.outstanding_amount_paise
        )

        if invoice.status == InvoiceStatus.PAID:
            return self._blocked_paid_invoice(
                proposal, invoice, payment_promise, evaluated
            )

        if not valid_installments:
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.INSTALLMENT_POLICY_VIOLATION,
                "Installments must contain two or three positive amounts "
                "that sum to an amount within the outstanding balance",
                [*evaluated, "INSTALLMENT_STRUCTURE_IS_VALID"],
            )

        return self._confirmation_or_authorization(
            proposal,
            invoice,
            payment_promise,
            [*evaluated, "INSTALLMENT_STRUCTURE_IS_VALID"],
        )

    def _authorize_mark_paid(
        self,
        proposal: ActionProposal,
        invoice: Invoice,
        payment_promise: PaymentPromise | None,
        evaluated: list[str],
    ) -> FirewallDecision:
        if (
            payment_promise is None
            or payment_promise.status != PromiseStatus.LINK_CREATED
        ):
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.PROMISE_INVALID_STATE,
                "Only a linked, unpaid promise can be marked paid",
                [*evaluated, "PROMISE_IS_LINKED_AND_UNPAID"],
            )

        if (
            not proposal.payment_link_id
            or proposal.payment_link_id
            != payment_promise.payment_link_id
        ):
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.PAYMENT_LINK_MISMATCH,
                "Payment Link does not match the promise",
                [*evaluated, "PAYMENT_LINK_MATCHES_PROMISE"],
            )

        if not proposal.provider_event_id:
            return self._verified_event_required(
                proposal, invoice, payment_promise, evaluated
            )

        provider_event = self.session.get(
            WebhookEvent,
            proposal.provider_event_id,
        )

        if provider_event is None:
            return self._verified_event_required(
                proposal, invoice, payment_promise, evaluated
            )

        if provider_event.event_type not in {
            "payment_link.paid",
            "payment_link.reconciliation.paid",
        }:
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.PROVIDER_EVENT_TYPE_INVALID,
                "Provider event does not prove a completed payment",
                [*evaluated, "PROVIDER_EVENT_TYPE_IS_PAID"],
            )

        entity = (
            provider_event.payload.get("payload", {})
            .get("payment_link", {})
            .get("entity", {})
        )

        if entity.get("id") != payment_promise.payment_link_id:
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.PROVIDER_EVENT_LINK_MISMATCH,
                "Provider event belongs to another Payment Link",
                [*evaluated, "PROVIDER_EVENT_LINK_MATCHES"],
            )

        if entity.get("status") != "paid":
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.PROVIDER_EVENT_STATUS_INVALID,
                "Provider event does not contain paid status",
                [*evaluated, "PROVIDER_EVENT_STATUS_IS_PAID"],
            )

        amount = proposal.amount_paise
        if (
            type(amount) is not int
            or amount != payment_promise.promised_amount_paise
            or entity.get("amount_paid") != amount
            or amount > invoice.outstanding_amount_paise
        ):
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.BLOCKED,
                FirewallRule.PROVIDER_EVENT_AMOUNT_MISMATCH,
                "Provider payment amount does not match the commitment",
                [*evaluated, "PROVIDER_EVENT_AMOUNT_MATCHES"],
            )

        if (
            entity.get("currency") != proposal.currency
            or proposal.currency != "INR"
        ):
            return self._currency_mismatch(
                proposal, invoice, payment_promise, evaluated
            )

        return self._decision(
            proposal,
            invoice,
            payment_promise,
            FirewallDecisionStatus.AUTHORIZED,
            FirewallRule.ALL_GUARDRAILS_PASSED,
            "Persisted provider evidence authorizes the payment transition",
            [
                *evaluated,
                "PROMISE_IS_LINKED_AND_UNPAID",
                "PAYMENT_LINK_MATCHES_PROMISE",
                "PROVIDER_EVENT_EXISTS",
                "PROVIDER_EVENT_TYPE_IS_PAID",
                "PROVIDER_EVENT_LINK_MATCHES",
                "PROVIDER_EVENT_STATUS_IS_PAID",
                "PROVIDER_EVENT_AMOUNT_MATCHES",
                "CURRENCY_MATCHES",
            ],
        )

    def _confirmation_or_authorization(
        self,
        proposal: ActionProposal,
        invoice: Invoice,
        payment_promise: PaymentPromise | None,
        evaluated: list[str],
    ) -> FirewallDecision:
        if not proposal.human_confirmed:
            return self._decision(
                proposal,
                invoice,
                payment_promise,
                FirewallDecisionStatus.REQUIRES_CONFIRMATION,
                FirewallRule.HUMAN_CONFIRMATION_REQUIRED,
                "A merchant must confirm this proposed action",
                [*evaluated, "HUMAN_CONFIRMATION_PRESENT"],
            )

        return self._decision(
            proposal,
            invoice,
            payment_promise,
            FirewallDecisionStatus.AUTHORIZED,
            FirewallRule.ALL_GUARDRAILS_PASSED,
            "Action satisfies deterministic guardrails and was confirmed",
            [*evaluated, "HUMAN_CONFIRMATION_PRESENT"],
        )

    def _evidence_error(self, proposal: ActionProposal) -> str | None:
        if proposal.customer_message is None:
            return "Customer message is required for evidence validation"

        try:
            locate_exact_evidence(
                proposal.customer_message,
                proposal.evidence_quotes,
            )
        except ContractError as error:
            return str(error)

        return None

    def _blocked_paid_invoice(
        self,
        proposal: ActionProposal,
        invoice: Invoice,
        payment_promise: PaymentPromise | None,
        evaluated: list[str],
    ) -> FirewallDecision:
        return self._decision(
            proposal,
            invoice,
            payment_promise,
            FirewallDecisionStatus.BLOCKED,
            FirewallRule.INVOICE_ALREADY_PAID,
            "Invoice is already paid",
            [*evaluated, "INVOICE_IS_NOT_PAID"],
        )

    def _currency_mismatch(
        self,
        proposal: ActionProposal,
        invoice: Invoice,
        payment_promise: PaymentPromise | None,
        evaluated: list[str],
    ) -> FirewallDecision:
        return self._decision(
            proposal,
            invoice,
            payment_promise,
            FirewallDecisionStatus.BLOCKED,
            FirewallRule.CURRENCY_MISMATCH,
            "Currency must match the INR Payment Link",
            [*evaluated, "CURRENCY_MATCHES"],
        )

    def _verified_event_required(
        self,
        proposal: ActionProposal,
        invoice: Invoice,
        payment_promise: PaymentPromise | None,
        evaluated: list[str],
    ) -> FirewallDecision:
        return self._decision(
            proposal,
            invoice,
            payment_promise,
            FirewallDecisionStatus.BLOCKED,
            FirewallRule.VERIFIED_PAYMENT_EVENT_REQUIRED,
            "A matching verified provider event is required",
            [*evaluated, "PROVIDER_EVENT_EXISTS"],
        )

    def _decision(
        self,
        proposal: ActionProposal,
        invoice: Invoice | None,
        payment_promise: PaymentPromise | None,
        status: FirewallDecisionStatus,
        rule: FirewallRule,
        reason: str,
        evaluated_rules: list[str],
    ) -> FirewallDecision:
        decision = FirewallDecision(
            decision_id=str(uuid4()),
            action=proposal.action,
            status=status,
            rule=rule,
            reason=reason,
            evaluated_rules=tuple(evaluated_rules),
        )

        if invoice is not None:
            add_audit_event(
                self.session,
                invoice_id=invoice.id,
                promise_id=(
                    payment_promise.id
                    if payment_promise is not None
                    else proposal.promise_id
                ),
                event_type="FINANCIAL_ACTION_DECIDED",
                event_data={
                    "decision_id": decision.decision_id,
                    "action": proposal.action.value,
                    "decision": status.value,
                    "rule": rule.value,
                    "reason": reason,
                    "evaluated_rules": list(evaluated_rules),
                    "actor": proposal.actor,
                    "human_confirmation": (
                        "CONFIRMED"
                        if proposal.human_confirmed
                        else None
                    ),
                    "proposal": {
                        "amount_paise": proposal.amount_paise,
                        "disputed_amount_paise": (
                            proposal.disputed_amount_paise
                        ),
                        "currency": proposal.currency,
                        "payment_link_id": proposal.payment_link_id,
                        "provider_event_id": proposal.provider_event_id,
                        "installments_paise": list(
                            proposal.installments_paise
                        ),
                        "evidence_quotes": list(
                            proposal.evidence_quotes
                        ),
                    },
                    "resulting_financial_event": None,
                },
            )

        return decision
