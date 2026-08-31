from datetime import date
import os
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..razorpay_client import RazorpayError, create_payment_link
from ..schemas import PaymentLinkCreationResponse

from ..contracts import ContractError, locate_exact_evidence
from ..database import get_database_session
from ..models import (
    Invoice,
    InvoiceStatus,
    PaymentPromise,
    PromiseStatus,
)
from ..schemas import (
    PaymentLinkCreationResponse,
    PaymentPromiseCreate,
    PaymentPromiseResponse,
)
from ..services.audit import add_audit_event


router = APIRouter(tags=["Promises"])


@router.post(
    "/invoices/{invoice_id}/promises",
    response_model=PaymentPromiseResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_payment_promise(
    invoice_id: str,
    body: PaymentPromiseCreate,
    session: Session = Depends(get_database_session),
) -> PaymentPromise:
    try:
        evidence_spans = locate_exact_evidence(
            body.customer_message,
            body.evidence_quotes,
        )
    except ContractError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error

    if body.promised_date < date.today():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="promised_date cannot be in the past",
        )

    invoice = session.scalar(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .with_for_update()
    )

    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invoice not found",
        )

    if invoice.status == InvoiceStatus.PAID:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot create a promise for a paid invoice",
        )

    committed_amount = (
        body.promised_amount_paise
        + body.disputed_amount_paise
    )

    if committed_amount > invoice.outstanding_amount_paise:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Promised and disputed amounts exceed "
                "the outstanding invoice balance"
            ),
        )

    existing_promise_id = session.scalar(
        select(PaymentPromise.id)
        .where(PaymentPromise.invoice_id == invoice_id)
        .limit(1)
    )

    if existing_promise_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This invoice already has a payment promise",
        )

    payment_promise = PaymentPromise(
        invoice_id=invoice.id,
        customer_message=body.customer_message,
        promised_amount_paise=body.promised_amount_paise,
        disputed_amount_paise=body.disputed_amount_paise,
        promised_date=body.promised_date,
        evidence_quotes=body.evidence_quotes,
        status=PromiseStatus.PROPOSED,
    )

    try:
        session.add(payment_promise)
        session.flush()

        add_audit_event(
            session,
            invoice_id=invoice.id,
            promise_id=payment_promise.id,
            event_type="PROMISE_PROPOSED",
            event_data={
                "promised_amount_paise": (
                    payment_promise.promised_amount_paise
                ),
                "disputed_amount_paise": (
                    payment_promise.disputed_amount_paise
                ),
                "promised_date": (
                    payment_promise.promised_date.isoformat()
                ),
                "evidence_spans": [
                    {
                        "quote": span.quote,
                        "start": span.start,
                        "end": span.end,
                    }
                    for span in evidence_spans
                ],
            },
        )

        payment_promise.status = PromiseStatus.VALIDATED
        invoice.disputed_amount_paise = (
            body.disputed_amount_paise
        )

        if body.disputed_amount_paise > 0:
            invoice.status = InvoiceStatus.DISPUTED

        add_audit_event(
            session,
            invoice_id=invoice.id,
            promise_id=payment_promise.id,
            event_type="PROMISE_VALIDATED",
            event_data={
                "outstanding_amount_paise": (
                    invoice.outstanding_amount_paise
                ),
                "committed_amount_paise": committed_amount,
                "financial_guardrails_passed": True,
                "evidence_grounded": True,
            },
        )

        session.commit()
        session.refresh(payment_promise)

        return payment_promise

    except Exception:
        session.rollback()
        raise

@router.post(
    "/promises/{promise_id}/payment-link",
    response_model=PaymentLinkCreationResponse,
)
def generate_payment_link(
    promise_id: str,
    session: Session = Depends(get_database_session),
) -> PaymentLinkCreationResponse:
    payment_promise = session.scalar(
        select(PaymentPromise)
        .where(PaymentPromise.id == promise_id)
        .with_for_update()
    )

    if payment_promise is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment promise not found",
        )

    invoice = session.scalar(
        select(Invoice)
        .where(Invoice.id == payment_promise.invoice_id)
        .with_for_update()
    )

    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invoice not found",
        )

    # Repeated requests must not create repeated links.
    if (
        payment_promise.payment_link_id
        and payment_promise.payment_link_url
    ):
        return PaymentLinkCreationResponse(
            promise_id=payment_promise.id,
            invoice_id=invoice.id,
            promise_status=payment_promise.status,
            payment_link_id=payment_promise.payment_link_id,
            payment_link_url=payment_promise.payment_link_url,
            amount_paise=payment_promise.promised_amount_paise,
            reused=True,
        )

    if payment_promise.status != PromiseStatus.VALIDATED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only validated promises can create Payment Links",
        )

    if invoice.status == InvoiceStatus.PAID:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Invoice is already paid",
        )

    if (
        payment_promise.promised_amount_paise
        > invoice.outstanding_amount_paise
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Promise exceeds the current outstanding balance",
        )

    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")

    if not key_id or not key_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Razorpay test credentials are not configured",
        )

    reference_id = f"ptp-{payment_promise.id[:32]}"

    try:
        result = create_payment_link(
            key_id=key_id,
            key_secret=key_secret,
            amount_paise=payment_promise.promised_amount_paise,
            reference_id=reference_id,
            description=f"Payment promise for invoice {invoice.id}",
            accept_partial=False,
        )

    except RazorpayError as error:
        session.rollback()

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
        ) from error

    payment_link_id = result.get("id")
    payment_link_url = result.get("short_url")
    returned_amount = result.get("amount")

    if not payment_link_id or not payment_link_url:
        session.rollback()

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Razorpay returned an incomplete Payment Link",
        )

    if returned_amount != payment_promise.promised_amount_paise:
        session.rollback()

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Razorpay returned an unexpected payment amount",
        )

    try:
        payment_promise.payment_link_id = payment_link_id
        payment_promise.payment_link_url = payment_link_url
        payment_promise.status = PromiseStatus.LINK_CREATED

        add_audit_event(
            session,
            invoice_id=invoice.id,
            promise_id=payment_promise.id,
            event_type="PAYMENT_LINK_CREATED",
            event_data={
                "payment_link_id": payment_link_id,
                "amount_paise": (
                    payment_promise.promised_amount_paise
                ),
                "mode": "EXACT_AMOUNT",
                "accept_partial": False,
                "reference_id": reference_id,
            },
        )

        session.commit()
        session.refresh(payment_promise)

    except Exception:
        session.rollback()
        raise

    return PaymentLinkCreationResponse(
        promise_id=payment_promise.id,
        invoice_id=invoice.id,
        promise_status=payment_promise.status,
        payment_link_id=payment_promise.payment_link_id,
        payment_link_url=payment_promise.payment_link_url,
        amount_paise=payment_promise.promised_amount_paise,
        reused=False,
    )