from typing import Any

from sqlalchemy.orm import Session

from ..models import (
    Invoice,
    InvoiceStatus,
    PaymentPromise,
    PromiseStatus,
)
from .audit import add_audit_event


SUPPORTED_PAYMENT_SOURCES = {
    "WEBHOOK",
    "RAZORPAY_RECONCILIATION",
}


def move_to_human_review(
    session: Session,
    *,
    invoice: Invoice,
    payment_promise: PaymentPromise,
    event_type: str,
    event_data: dict[str, Any],
) -> None:
    invoice.status = InvoiceStatus.HUMAN_REVIEW
    payment_promise.status = PromiseStatus.HUMAN_REVIEW

    add_audit_event(
        session,
        invoice_id=invoice.id,
        promise_id=payment_promise.id,
        event_type=event_type,
        event_data=event_data,
    )


def apply_exact_payment(
    session: Session,
    *,
    invoice: Invoice,
    payment_promise: PaymentPromise,
    payment_link_id: str,
    amount_paid: object,
    source: str,
    razorpay_event_id: str | None = None,
) -> dict[str, object]:
    """
    Apply an exact payment without committing the transaction.

    The caller must lock the promise and invoice rows before calling
    this function and must commit or roll back the transaction.
    """

    if source not in SUPPORTED_PAYMENT_SOURCES:
        raise ValueError("Unsupported payment application source")

    event_context: dict[str, Any] = {
        "source": source,
        "payment_link_id": payment_link_id,
    }

    if razorpay_event_id is not None:
        event_context["razorpay_event_id"] = (
            razorpay_event_id
        )

    if payment_promise.status == PromiseStatus.PAID:
        return {
            "already_applied": True,
            "human_review": False,
            "reason": None,
            "promise_status": payment_promise.status.value,
            "invoice_status": invoice.status.value,
            "amount_paid_paise": amount_paid,
            "outstanding_amount_paise": (
                invoice.outstanding_amount_paise
            ),
        }

    if payment_promise.status != PromiseStatus.LINK_CREATED:
        move_to_human_review(
            session,
            invoice=invoice,
            payment_promise=payment_promise,
            event_type="INVALID_PROMISE_STATE",
            event_data={
                **event_context,
                "current_status": (
                    payment_promise.status.value
                ),
            },
        )

        return {
            "already_applied": False,
            "human_review": True,
            "reason": "Invalid promise state",
            "promise_status": payment_promise.status.value,
            "invoice_status": invoice.status.value,
            "amount_paid_paise": amount_paid,
            "outstanding_amount_paise": (
                invoice.outstanding_amount_paise
            ),
        }

    expected_amount = payment_promise.promised_amount_paise

    # `bool` is a subclass of `int`, so use type() here.
    if (
        type(amount_paid) is not int
        or amount_paid != expected_amount
    ):
        move_to_human_review(
            session,
            invoice=invoice,
            payment_promise=payment_promise,
            event_type="PAYMENT_AMOUNT_MISMATCH",
            event_data={
                **event_context,
                "expected_amount_paise": expected_amount,
                "received_amount_paise": amount_paid,
            },
        )

        return {
            "already_applied": False,
            "human_review": True,
            "reason": "Payment amount mismatch",
            "promise_status": payment_promise.status.value,
            "invoice_status": invoice.status.value,
            "amount_paid_paise": amount_paid,
            "outstanding_amount_paise": (
                invoice.outstanding_amount_paise
            ),
        }

    if amount_paid > invoice.outstanding_amount_paise:
        move_to_human_review(
            session,
            invoice=invoice,
            payment_promise=payment_promise,
            event_type="PAYMENT_EXCEEDS_OUTSTANDING",
            event_data={
                **event_context,
                "amount_paid_paise": amount_paid,
                "outstanding_amount_paise": (
                    invoice.outstanding_amount_paise
                ),
            },
        )

        return {
            "already_applied": False,
            "human_review": True,
            "reason": "Payment exceeds outstanding balance",
            "promise_status": payment_promise.status.value,
            "invoice_status": invoice.status.value,
            "amount_paid_paise": amount_paid,
            "outstanding_amount_paise": (
                invoice.outstanding_amount_paise
            ),
        }

    payment_promise.status = PromiseStatus.PAID

    invoice.paid_amount_paise += amount_paid
    invoice.outstanding_amount_paise = (
        invoice.original_amount_paise
        - invoice.paid_amount_paise
    )

    if invoice.outstanding_amount_paise == 0:
        invoice.status = InvoiceStatus.PAID
    elif invoice.disputed_amount_paise > 0:
        invoice.status = InvoiceStatus.DISPUTED
    else:
        invoice.status = InvoiceStatus.PARTIALLY_PAID

    audit_event_type = (
        "PAYMENT_RECEIVED"
        if source == "WEBHOOK"
        else "PAYMENT_RECONCILED"
    )

    event_data = {
        **event_context,
        "amount_paid_paise": amount_paid,
        "invoice_paid_amount_paise": (
            invoice.paid_amount_paise
        ),
        "invoice_outstanding_amount_paise": (
            invoice.outstanding_amount_paise
        ),
        "invoice_status": invoice.status.value,
    }

    if source == "RAZORPAY_RECONCILIATION":
        event_data["reconciliation_reason"] = (
            "MISSED_OR_DELAYED_WEBHOOK"
        )

    add_audit_event(
        session,
        invoice_id=invoice.id,
        promise_id=payment_promise.id,
        event_type=audit_event_type,
        event_data=event_data,
    )

    return {
        "already_applied": False,
        "human_review": False,
        "reason": None,
        "promise_status": payment_promise.status.value,
        "invoice_status": invoice.status.value,
        "amount_paid_paise": amount_paid,
        "outstanding_amount_paise": (
            invoice.outstanding_amount_paise
        ),
    }