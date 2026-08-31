from __future__ import annotations
from .routes.invoices import router as invoices_router
import json
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from .routes.promises import router as promises_router

from .razorpay_client import (
    RazorpayError,
    WebhookLedger,
    create_payment_link,
    verify_webhook_signature,
)


class PaymentLinkRequest(BaseModel):
    amount_paise: int = Field(gt=0)
    reference_id: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=255)
    accept_partial: bool = False
    first_min_partial_amount: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_partial_configuration(self) -> "PaymentLinkRequest":
        if self.accept_partial:
            if self.first_min_partial_amount is None:
                raise ValueError("first_min_partial_amount is required")
            if self.first_min_partial_amount >= self.amount_paise:
                raise ValueError("first_min_partial_amount must be below amount_paise")
        elif self.first_min_partial_amount is not None:
            raise ValueError("first_min_partial_amount requires accept_partial=true")
        return self


class PaymentLinkResponse(BaseModel):
    id: str
    short_url: str
    status: str
    amount: int
    accept_partial: bool


def create_app(database_path: str | Path | None = None) -> FastAPI:
    application = FastAPI(title="Promise-to-Pay Recovery Orchestrator", version="0.2.0")
    application.include_router(invoices_router)
    application.include_router(promises_router)
    event_ledger = WebhookLedger(
        database_path or Path(os.getenv("SPIKE_DATABASE_PATH", "spike.db"))
    )

    @application.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @application.post("/payment-links", response_model=PaymentLinkResponse)
    def payment_links(body: PaymentLinkRequest) -> PaymentLinkResponse:
        key_id = os.getenv("RAZORPAY_KEY_ID")
        key_secret = os.getenv("RAZORPAY_KEY_SECRET")
        if not key_id or not key_secret:
            raise HTTPException(
                status_code=503, detail="Razorpay test credentials not configured"
            )
        try:
            result = create_payment_link(
                key_id=key_id,
                key_secret=key_secret,
                amount_paise=body.amount_paise,
                reference_id=body.reference_id,
                description=body.description,
                accept_partial=body.accept_partial,
                first_min_partial_amount=body.first_min_partial_amount,
            )
        except RazorpayError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return PaymentLinkResponse.model_validate(result)

    @application.post("/webhooks/razorpay")
    async def razorpay_webhook(
        request: Request,
        x_razorpay_signature: str = Header(),
        x_razorpay_event_id: str = Header(),
    ) -> dict[str, object]:
        webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")
        if not webhook_secret:
            raise HTTPException(status_code=503, detail="Webhook secret not configured")

        raw_body = await request.body()
        if not verify_webhook_signature(
            raw_body, x_razorpay_signature, webhook_secret
        ):
            raise HTTPException(status_code=400, detail="Invalid webhook signature")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=400, detail="Invalid JSON") from error

        event_type = payload.get("event")
        allowed_events: set[str] = {
            "payment_link.paid",
            "payment_link.partially_paid",
        }
        if event_type not in allowed_events:
            return {"accepted": True, "ignored": True, "event_type": event_type}

        # The documented event ID is mandatory for money-state events. This
        # avoids conflating byte-identical but genuinely distinct events.
        inserted = event_ledger.record_once(
            x_razorpay_event_id, event_type, raw_body
        )
        if not inserted:
            return {
                "accepted": True,
                "duplicate": True,
                "event_id": x_razorpay_event_id,
            }

        entity = (
            payload.get("payload", {}).get("payment_link", {}).get("entity", {})
        )
        return {
            "accepted": True,
            "duplicate": False,
            "event_id": x_razorpay_event_id,
            "event_type": event_type,
            "payment_link_id": entity.get("id"),
            "amount_paid": entity.get("amount_paid"),
            "amount": entity.get("amount"),
            "status": entity.get("status"),
        }

    return application


app = create_app()
