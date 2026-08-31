from sqlalchemy import create_engine, inspect

from app.database import Base
from app import models  # noqa: F401


def test_all_expected_tables_are_registered() -> None:
    test_engine = create_engine("sqlite+pysqlite:///:memory:")

    Base.metadata.create_all(bind=test_engine)

    table_names = set(inspect(test_engine).get_table_names())

    assert table_names == {
        "invoices",
        "payment_promises",
        "audit_events",
        "webhook_events",
    }