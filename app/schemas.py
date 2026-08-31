from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import InvoiceStatus, PromiseStatus


class InvoiceCreate(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200)
    original_amount_paise: int = Field(gt=0)
    due_date: date

    @field_validator("customer_name")
    @classmethod
    def clean_customer_name(cls, value: str) -> str:
        cleaned = value.strip()

        if not cleaned:
            raise ValueError("customer_name cannot be empty")

        return cleaned


class InvoiceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    customer_name: str
    original_amount_paise: int
    paid_amount_paise: int
    disputed_amount_paise: int
    outstanding_amount_paise: int
    due_date: date
    status: InvoiceStatus
    created_at: datetime
    updated_at: datetime


class PaymentPromiseCreate(BaseModel):
    customer_message: str = Field(min_length=1, max_length=2000)
    promised_amount_paise: int = Field(gt=0)
    disputed_amount_paise: int = Field(default=0, ge=0)
    promised_date: date
    evidence_quotes: list[str] = Field(min_length=1)

    @field_validator("customer_message")
    @classmethod
    def clean_customer_message(cls, value: str) -> str:
        cleaned = value.strip()

        if not cleaned:
            raise ValueError("customer_message cannot be empty")

        return cleaned

    @field_validator("evidence_quotes")
    @classmethod
    def validate_evidence_quotes(
        cls,
        quotes: list[str],
    ) -> list[str]:
        if any(not quote for quote in quotes):
            raise ValueError("Evidence quotes cannot be empty")

        return quotes


class PaymentPromiseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    invoice_id: str
    customer_message: str
    promised_amount_paise: int
    disputed_amount_paise: int
    promised_date: date
    evidence_quotes: list[str]
    status: PromiseStatus
    payment_link_id: str | None
    payment_link_url: str | None
    created_at: datetime
    updated_at: datetime


class PaymentLinkCreationResponse(BaseModel):
    promise_id: str
    invoice_id: str
    promise_status: PromiseStatus
    payment_link_id: str
    payment_link_url: str
    amount_paise: int
    reused: bool

class CustomerReplyRequest(BaseModel):
    customer_message: str = Field(
        min_length=1,
        max_length=2000,
    )

    message_timestamp: datetime

    @field_validator("message_timestamp")
    @classmethod
    def require_timezone(
        cls,
        value: datetime,
    ) -> datetime:
        if value.tzinfo is None:
            raise ValueError(
                "message_timestamp must include a timezone"
            )

        return value


class EvidenceSpanResponse(BaseModel):
    quote: str
    start: int
    end: int


class ExtractionPreviewResponse(BaseModel):
    intent: str
    promised_amount_paise: int | None
    disputed_amount_paise: int
    promised_date_text: str | None
    evidence_quotes: list[str]
    needs_review: bool
    review_reason: str | None

    ready_for_validation: bool
    resolved_promised_date: date | None
    validation_errors: list[str]
    evidence_spans: list[EvidenceSpanResponse]


class PaymentReconciliationResponse(BaseModel):
    promise_id: str
    invoice_id: str
    payment_link_id: str
    external_status: str

    reconciled: bool
    already_applied: bool
    human_review: bool
    reason: str | None

    promise_status: PromiseStatus
    invoice_status: InvoiceStatus
    amount_paid_paise: int | None
    outstanding_amount_paise: int

class BrokenPromiseSweepResponse(BaseModel):
    as_of: date
    broken_count: int
    broken_promise_ids: list[str]