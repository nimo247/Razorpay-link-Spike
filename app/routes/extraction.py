from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)
from sqlalchemy.orm import Session

from ..contracts import (
    ContractError,
    locate_exact_evidence,
    resolve_relative_weekday_from_evidence,
)
from ..database import get_database_session
from ..models import Invoice
from ..schemas import (
    CustomerReplyRequest,
    EvidenceSpanResponse,
    ExtractionPreviewResponse,
)
from ..services.promise_extractor import (
    ExtractionError,
    extract_promise_with_groq,
)


router = APIRouter(tags=["AI Extraction"])


@router.post(
    "/invoices/{invoice_id}/extract-promise",
    response_model=ExtractionPreviewResponse,
)
def extract_payment_promise(
    invoice_id: str,
    body: CustomerReplyRequest,
    session: Session = Depends(get_database_session),
) -> ExtractionPreviewResponse:
    invoice = session.get(Invoice, invoice_id)

    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invoice not found",
        )

    try:
        extraction = extract_promise_with_groq(
            customer_message=body.customer_message,
            outstanding_amount_paise=(
                invoice.outstanding_amount_paise
            ),
        )

    except ExtractionError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
        ) from error

    errors: list[str] = []
    evidence_spans: list[EvidenceSpanResponse] = []
    resolved_date = None

    try:
        located = locate_exact_evidence(
            body.customer_message,
            extraction.evidence_quotes,
        )

        evidence_spans = [
            EvidenceSpanResponse(
                quote=span.quote,
                start=span.start,
                end=span.end,
            )
            for span in located
        ]

    except ContractError as error:
        errors.append(str(error))

    if extraction.needs_review:
        errors.append(
            extraction.review_reason
            or "Model requested human review"
        )

    if extraction.promised_amount_paise is None:
        errors.append("Promised amount is missing")

    elif extraction.promised_amount_paise <= 0:
        errors.append("Promised amount must be positive")

    committed_amount = (
        (extraction.promised_amount_paise or 0)
        + extraction.disputed_amount_paise
    )

    if committed_amount > invoice.outstanding_amount_paise:
        errors.append(
            "Promised and disputed amounts exceed "
            "the outstanding balance"
        )

    if extraction.disputed_amount_paise < 0:
        errors.append("Disputed amount cannot be negative")

    if extraction.promised_date_text is None:
        errors.append("Promised date is missing")

    else:
        if (
            extraction.promised_date_text
            not in body.customer_message
        ):
            errors.append(
                "Promised date is not an exact input substring"
            )

        else:
            try:
                resolved_datetime = (
                    resolve_relative_weekday_from_evidence(
                        extraction.promised_date_text,
                        body.message_timestamp,
                    )
                )

                resolved_date = resolved_datetime.date()

            except ContractError as error:
                errors.append(str(error))

    return ExtractionPreviewResponse(
        intent=extraction.intent,
        promised_amount_paise=(
            extraction.promised_amount_paise
        ),
        disputed_amount_paise=(
            extraction.disputed_amount_paise
        ),
        promised_date_text=(
            extraction.promised_date_text
        ),
        evidence_quotes=extraction.evidence_quotes,
        needs_review=extraction.needs_review,
        review_reason=extraction.review_reason,
        ready_for_validation=len(errors) == 0,
        resolved_promised_date=resolved_date,
        validation_errors=errors,
        evidence_spans=evidence_spans,
    )