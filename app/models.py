from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Enum as SQLAlchemyEnum,
    ForeignKey,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def generate_id() -> str:
    return str(uuid4())


class InvoiceStatus(str, Enum):
    OVERDUE = "OVERDUE"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    DISPUTED = "DISPUTED"
    PAID = "PAID"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class PromiseStatus(str, Enum):
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    LINK_CREATED = "LINK_CREATED"
    PAID = "PAID"
    BROKEN = "BROKEN"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=generate_id,
    )

    customer_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    original_amount_paise: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    paid_amount_paise: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    disputed_amount_paise: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    outstanding_amount_paise: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    due_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )

    status: Mapped[InvoiceStatus] = mapped_column(
        SQLAlchemyEnum(
            InvoiceStatus,
            name="invoice_status",
        ),
        nullable=False,
        default=InvoiceStatus.OVERDUE,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    promises: Mapped[list["PaymentPromise"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
    )

    audit_events: Mapped[list["AuditEvent"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
    )


class PaymentPromise(Base):
    __tablename__ = "payment_promises"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=generate_id,
    )

    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    customer_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    promised_amount_paise: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    disputed_amount_paise: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    promised_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )

    evidence_quotes: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
    )

    status: Mapped[PromiseStatus] = mapped_column(
        SQLAlchemyEnum(
            PromiseStatus,
            name="promise_status",
        ),
        nullable=False,
        default=PromiseStatus.PROPOSED,
    )

    payment_link_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        unique=True,
    )

    payment_link_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    invoice: Mapped["Invoice"] = relationship(
        back_populates="promises",
    )

    audit_events: Mapped[list["AuditEvent"]] = relationship(
        back_populates="payment_promise",
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=generate_id,
    )

    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    promise_id: Mapped[str | None] = mapped_column(
        ForeignKey("payment_promises.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    event_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    event_data: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    invoice: Mapped["Invoice"] = relationship(
        back_populates="audit_events",
    )

    payment_promise: Mapped["PaymentPromise | None"] = relationship(
        back_populates="audit_events",
    )


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(
        String(100),
        primary_key=True,
    )

    event_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    payload_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )

    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )