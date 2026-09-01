import argparse
import json
import os
from datetime import date, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE_URL = os.getenv(
    "DEMO_API_BASE_URL",
    "http://127.0.0.1:8000",
).rstrip("/")

DEMO_CUSTOMER = "Northstar Retail"
DEMO_AMOUNT_PAISE = 4_800_000

DEMO_MESSAGE = (
    "I can pay 40k this Friday. "
    "The other 8k is disputed."
)


def api_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None

    if payload is not None:
        body = json.dumps(payload).encode()

    request = Request(
        f"{API_BASE_URL}{path}",
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read())

    except HTTPError as error:
        error_body = error.read().decode(errors="replace")

        raise RuntimeError(
            f"API returned HTTP {error.code}: {error_body}"
        ) from error

    except URLError as error:
        raise RuntimeError(
            f"Could not connect to {API_BASE_URL}: "
            f"{error.reason}"
        ) from error


def invoice_is_clean(invoice: dict[str, Any]) -> bool:
    if (
        invoice["customer_name"] != DEMO_CUSTOMER
        or invoice["status"] != "OVERDUE"
        or invoice["paid_amount_paise"] != 0
        or invoice["disputed_amount_paise"] != 0
        or invoice["outstanding_amount_paise"]
        != invoice["original_amount_paise"]
    ):
        return False

    workspace = api_request(
        "GET",
        f"/invoices/{invoice['id']}/workspace",
    )

    return workspace["promise"] is None


def find_reusable_invoice() -> dict[str, Any] | None:
    invoices = api_request("GET", "/invoices")

    for invoice in invoices:
        if invoice_is_clean(invoice):
            return invoice

    return None


def create_demo_invoice() -> dict[str, Any]:
    return api_request(
        "POST",
        "/invoices",
        {
            "customer_name": DEMO_CUSTOMER,
            "original_amount_paise": DEMO_AMOUNT_PAISE,
            "due_date": (
                date.today() - timedelta(days=30)
            ).isoformat(),
        },
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare a clean invoice for the recovery demo."
        )
    )

    parser.add_argument(
        "--fresh",
        action="store_true",
        help=(
            "Always create a new invoice instead of reusing "
            "an existing clean invoice."
        ),
    )

    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()

    if arguments.fresh:
        invoice = create_demo_invoice()
        action = "Created fresh"
    else:
        invoice = find_reusable_invoice()

        if invoice is None:
            invoice = create_demo_invoice()
            action = "Created"
        else:
            action = "Reused"

    if not invoice_is_clean(invoice):
        raise RuntimeError(
            "The selected demo invoice is not in a clean state"
        )

    print(f"{action} clean demo invoice")
    print(f"Invoice ID: {invoice['id']}")
    print(f"Customer: {invoice['customer_name']}")
    print("Status: OVERDUE")
    print("Amount: INR 48,000")
    print("Existing promise: none")
    print(f"Customer reply: {DEMO_MESSAGE}")


if __name__ == "__main__":
    main()