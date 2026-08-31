from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


RAZORPAY_PAYMENT_LINKS_URL = "https://api.razorpay.com/v1/payment_links"


class RazorpayError(RuntimeError):
    pass


def build_payment_link_payload(
    *,
    amount_paise: int,
    reference_id: str,
    description: str,
    accept_partial: bool = False,
    first_min_partial_amount: int | None = None,
) -> dict[str, Any]:
    if amount_paise <= 0:
        raise ValueError("amount_paise must be positive")
    if accept_partial:
        if first_min_partial_amount is None:
            raise ValueError("first_min_partial_amount is required for partial links")
        if not 0 < first_min_partial_amount < amount_paise:
            raise ValueError("first_min_partial_amount must be between zero and amount")
    elif first_min_partial_amount is not None:
        raise ValueError("first_min_partial_amount requires accept_partial=true")

    payload: dict[str, Any] = {
        "amount": amount_paise,
        "currency": "INR",
        "accept_partial": accept_partial,
        "reference_id": reference_id,
        "description": description,
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
    }
    if first_min_partial_amount is not None:
        payload["first_min_partial_amount"] = first_min_partial_amount
    return payload


def create_payment_link(
    *,
    key_id: str,
    key_secret: str,
    amount_paise: int,
    reference_id: str,
    description: str,
    accept_partial: bool = False,
    first_min_partial_amount: int | None = None,
) -> dict[str, Any]:
    payload = build_payment_link_payload(
        amount_paise=amount_paise,
        reference_id=reference_id,
        description=description,
        accept_partial=accept_partial,
        first_min_partial_amount=first_min_partial_amount,
    )

    credentials = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    request = Request(
        RAZORPAY_PAYMENT_LINKS_URL,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except HTTPError as error:
        body = error.read().decode(errors="replace")
        raise RazorpayError(f"Razorpay returned HTTP {error.code}: {body}") from error


def expected_webhook_signature(raw_body: bytes, webhook_secret: str) -> str:
    return hmac.new(webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    raw_body: bytes, supplied_signature: str, webhook_secret: str
) -> bool:
    expected = expected_webhook_signature(raw_body, webhook_secret)
    return hmac.compare_digest(expected, supplied_signature)


class WebhookLedger:
    """Tiny persistent replay ledger for the integration spike.

    Production processing should update the promise and insert the event inside
    one database transaction. The spike records receipt and proves that repeated
    X-Razorpay-Event-Id values are ignored.
    """

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    received_at TEXT NOT NULL
                )
                """
            )

    def record_once(self, event_id: str, event_type: str, raw_body: bytes) -> bool:
        """Return True for a new event and False for a replay."""
        digest = hashlib.sha256(raw_body).hexdigest()
        try:
            with sqlite3.connect(self.database_path) as connection:
                connection.execute(
                    """
                    INSERT INTO webhook_events
                        (event_id, event_type, payload_sha256, received_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event_type,
                        digest,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False
