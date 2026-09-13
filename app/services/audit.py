from typing import Any

from sqlalchemy.orm import Session

from ..models import AuditEvent


def add_audit_event(
    session: Session,
    *,
    invoice_id: str,
    event_type: str,
    event_data: dict[str, Any],
    promise_id: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        invoice_id=invoice_id,
        promise_id=promise_id,
        event_type=event_type,
        event_data=event_data,
    )

    session.add(event)

    return event