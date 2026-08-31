from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts import ContractError, locate_exact_evidence
from ..database import get_database_session
from ..models import (
    Invoice,
    InvoiceStatus,
    PaymentPromise,
    PromiseStatus,
)
from ..schemas import (
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