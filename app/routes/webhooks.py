import hashlib
import json
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_database_session
from ..models import (
    Invoice,
    InvoiceStatus,
    PaymentPromise,
    PromiseStatus,
    WebhookEvent,
)
from ..razorpay_client import verify_webhook_signature
from ..services.audit import add_audit_event


router = APIRouter(tags=["Webhooks"])


def move_to_human_review(
    session: Session,
    *,
    invoice: Invoice,
    payment_promise: PaymentPromise,
    event_type: str,
    event_data: dict,
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


@router.post("/webhooks/razorpay")
async def process_razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(),
    x_razorpay_event_id: str = Header(),
    session: Session = Depends(get_database_session),
) -> dict[str, object]:
    webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")

    if not webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook secret is not configured",
        )

    raw_body = await request.body()

    if not verify_webhook_signature(
        raw_body,
        x_razorpay_signature,
        webhook_secret,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature",
        )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from error

    event_type = payload.get("event")

    supported_events = {
        "payment_link.paid",
        "payment_link.partially_paid",
    }

    if event_type not in supported_events:
        return {
            "accepted": True,
            "ignored": True,
            "event_type": event_type,
        }

    entity = (
        payload.get("payload", {})
        .get("payment_link", {})
        .get("entity", {})
    )

    payment_link_id = entity.get("id")
    amount_paid = entity.get("amount_paid")

    if not payment_link_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook does not contain a Payment Link ID",
        )

    webhook_event = WebhookEvent(
        event_id=x_razorpay_event_id,
        event_type=event_type,
        payload_sha256=hashlib.sha256(raw_body).hexdigest(),
        payload=payload,
    )

    try:
        session.add(webhook_event)
        session.flush()

    except IntegrityError:
        session.rollback()

        return {
            "accepted": True,
            "duplicate": True,
            "event_id": x_razorpay_event_id,
        }

    payment_promise = session.scalar(
        select(PaymentPromise)
        .where(
            PaymentPromise.payment_link_id == payment_link_id
        )
        .with_for_update()
    )

    if payment_promise is None:
        session.rollback()

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook Payment Link is not registered",
        )

    invoice = session.scalar(
        select(Invoice)
        .where(Invoice.id == payment_promise.invoice_id)
        .with_for_update()
    )

    if invoice is None:
        session.rollback()

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Associated invoice was not found",
        )

    try:
        if event_type == "payment_link.partially_paid":
            move_to_human_review(
                session,
                invoice=invoice,
                payment_promise=payment_promise,
                event_type="UNEXPECTED_PARTIAL_PAYMENT",
                event_data={
                    "razorpay_event_id": x_razorpay_event_id,
                    "payment_link_id": payment_link_id,
                    "amount_paid": amount_paid,
                    "reason": (
                        "Exact-amount promise link received "
                        "a partial-payment event"
                    ),
                },
            )

            session.commit()

            return {
                "accepted": True,
                "human_review": True,
                "reason": "Unexpected partial payment",
            }

        if payment_promise.status == PromiseStatus.PAID:
            session.commit()

            return {
                "accepted": True,
                "already_applied": True,
                "event_id": x_razorpay_event_id,
            }

        if payment_promise.status != PromiseStatus.LINK_CREATED:
            move_to_human_review(
                session,
                invoice=invoice,
                payment_promise=payment_promise,
                event_type="INVALID_PROMISE_STATE",
                event_data={
                    "razorpay_event_id": x_razorpay_event_id,
                    "current_status": payment_promise.status.value,
                },
            )

            session.commit()

            return {
                "accepted": True,
                "human_review": True,
                "reason": "Invalid promise state",
            }

        expected_amount = (
            payment_promise.promised_amount_paise
        )

        if (
            not isinstance(amount_paid, int)
            or amount_paid != expected_amount
        ):
            move_to_human_review(
                session,
                invoice=invoice,
                payment_promise=payment_promise,
                event_type="PAYMENT_AMOUNT_MISMATCH",
                event_data={
                    "razorpay_event_id": x_razorpay_event_id,
                    "expected_amount_paise": expected_amount,
                    "received_amount_paise": amount_paid,
                },
            )

            session.commit()

            return {
                "accepted": True,
                "human_review": True,
                "reason": "Payment amount mismatch",
            }

        if amount_paid > invoice.outstanding_amount_paise:
            move_to_human_review(
                session,
                invoice=invoice,
                payment_promise=payment_promise,
                event_type="PAYMENT_EXCEEDS_OUTSTANDING",
                event_data={
                    "razorpay_event_id": x_razorpay_event_id,
                    "amount_paid_paise": amount_paid,
                    "outstanding_amount_paise": (
                        invoice.outstanding_amount_paise
                    ),
                },
            )

            session.commit()

            return {
                "accepted": True,
                "human_review": True,
                "reason": "Payment exceeds outstanding balance",
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

        add_audit_event(
            session,
            invoice_id=invoice.id,
            promise_id=payment_promise.id,
            event_type="PAYMENT_RECEIVED",
            event_data={
                "razorpay_event_id": x_razorpay_event_id,
                "payment_link_id": payment_link_id,
                "amount_paid_paise": amount_paid,
                "invoice_paid_amount_paise": (
                    invoice.paid_amount_paise
                ),
                "invoice_outstanding_amount_paise": (
                    invoice.outstanding_amount_paise
                ),
                "invoice_status": invoice.status.value,
            },
        )

        session.commit()

        return {
            "accepted": True,
            "duplicate": False,
            "event_id": x_razorpay_event_id,
            "promise_status": payment_promise.status.value,
            "invoice_status": invoice.status.value,
            "amount_paid_paise": amount_paid,
            "outstanding_amount_paise": (
                invoice.outstanding_amount_paise
            ),
        }

    except Exception:
        session.rollback()
        raise