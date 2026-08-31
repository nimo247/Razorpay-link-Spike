from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import InvoiceStatus


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