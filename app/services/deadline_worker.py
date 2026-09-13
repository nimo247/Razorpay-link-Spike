import os
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Invoice,
    InvoiceStatus,
    PaymentPromise,
    PromiseStatus,
)
from .audit import add_audit_event


def current_business_date() -> date:
    timezone_name = os.getenv(
        "BUSINESS_TIMEZONE",
        "Asia/Kolkata",
    )

    try:
        business_timezone = ZoneInfo(timezone_name)

    except ZoneInfoNotFoundError as error:
        raise RuntimeError(
            f"Invalid BUSINESS_TIMEZONE: {timezone_name}"
        ) from error

    return datetime.now(business_timezone).date()


def mark_broken_promises(
    session: Session,
    *,
    as_of: date | None = None,
) -> list[str]:
    """
    Mark overdue unpaid promises as broken.

    This function does not commit. The caller owns the transaction.
    Repeated execution is idempotent because BROKEN promises are
    excluded from subsequent queries.
    """

    effective_date = as_of or current_business_date()

    overdue_promises = session.scalars(
        select(PaymentPromise)
        .where(
            PaymentPromise.status.in_(
                [
                    PromiseStatus.VALIDATED,
                    PromiseStatus.LINK_CREATED,
                ]
            ),
            PaymentPromise.promised_date
            < effective_date,
        )
        .order_by(
            PaymentPromise.promised_date,
            PaymentPromise.id,
        )
        .with_for_update(skip_locked=True)
    ).all()

    broken_promise_ids: list[str] = []

    for payment_promise in overdue_promises:
        invoice = session.scalar(
            select(Invoice)
            .where(
                Invoice.id == payment_promise.invoice_id
            )
            .with_for_update()
        )

        if invoice is None:
            raise RuntimeError(
                "Payment promise has no associated invoice"
            )

        if invoice.outstanding_amount_paise <= 0:
            raise RuntimeError(
                "Unpaid promise belongs to a settled invoice"
            )

        previous_status = payment_promise.status
        payment_promise.status = PromiseStatus.BROKEN

        if invoice.disputed_amount_paise > 0:
            invoice.status = InvoiceStatus.DISPUTED
        else:
            invoice.status = InvoiceStatus.OVERDUE

        add_audit_event(
            session,
            invoice_id=invoice.id,
            promise_id=payment_promise.id,
            event_type="PROMISE_BROKEN",
            event_data={
                "previous_promise_status": (
                    previous_status.value
                ),
                "promised_date": (
                    payment_promise.promised_date.isoformat()
                ),
                "evaluated_as_of": (
                    effective_date.isoformat()
                ),
                "outstanding_amount_paise": (
                    invoice.outstanding_amount_paise
                ),
                "payment_link_id": (
                    payment_promise.payment_link_id
                ),
                "reason": (
                    "Promise date passed without "
                    "confirmed payment"
                ),
            },
        )

        broken_promise_ids.append(payment_promise.id)

    return broken_promise_ids