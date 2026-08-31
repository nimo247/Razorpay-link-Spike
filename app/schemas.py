from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import InvoiceStatus

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