from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_database_session
from ..models import Invoice, InvoiceStatus
from ..schemas import InvoiceCreate, InvoiceResponse
from ..services.audit import add_audit_event


router = APIRouter(
    prefix="/invoices",
    tags=["Invoices"],
)


@router.post(
    "",
    response_model=InvoiceResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_invoice(
    body: InvoiceCreate,
    session: Session = Depends(get_database_session),
) -> Invoice:
    if body.due_date >= date.today():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only overdue invoices can enter the recovery workflow",
        )

    invoice = Invoice(
        customer_name=body.customer_name,
        original_amount_paise=body.original_amount_paise,
        paid_amount_paise=0,
        disputed_amount_paise=0,
        outstanding_amount_paise=body.original_amount_paise,
        due_date=body.due_date,
        status=InvoiceStatus.OVERDUE,
    )

    try:
        session.add(invoice)
        session.flush()

        add_audit_event(
            session,
            invoice_id=invoice.id,
            event_type="INVOICE_CREATED",
            event_data={
                "customer_name": invoice.customer_name,
                "original_amount_paise": invoice.original_amount_paise,
                "due_date": invoice.due_date.isoformat(),
                "status": invoice.status.value,
            },
        )

        session.commit()
        session.refresh(invoice)

        return invoice

    except Exception:
        session.rollback()
        raise


@router.get(
    "/{invoice_id}",
    response_model=InvoiceResponse,
)
def get_invoice(
    invoice_id: str,
    session: Session = Depends(get_database_session),
) -> Invoice:
    invoice = session.get(Invoice, invoice_id)

    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invoice not found",
        )

    return invoice