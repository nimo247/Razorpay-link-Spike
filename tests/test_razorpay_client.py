import json
from pathlib import Path
import tempfile
import unittest

from app.razorpay_client import (
    WebhookLedger,
    build_payment_link_payload,
    expected_webhook_signature,
    verify_webhook_signature,
)


class WebhookTests(unittest.TestCase):
    def test_signature_uses_raw_body(self) -> None:
        raw = json.dumps({"event": "payment_link.paid"}, separators=(",", ":")).encode()
        signature = expected_webhook_signature(raw, "test-secret")
        self.assertTrue(verify_webhook_signature(raw, signature, "test-secret"))
        self.assertFalse(verify_webhook_signature(raw + b" ", signature, "test-secret"))

    def test_duplicate_event_id_is_recorded_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = WebhookLedger(Path(directory) / "events.db")
            raw = b'{"event":"payment_link.partially_paid"}'
            self.assertTrue(ledger.record_once("evt_123", "payment_link.partially_paid", raw))
            self.assertFalse(ledger.record_once("evt_123", "payment_link.partially_paid", raw))


class PaymentLinkPayloadTests(unittest.TestCase):
    def test_exact_amount_payload_does_not_enable_partial_payment(self) -> None:
        payload = build_payment_link_payload(
            amount_paise=4_000_000,
            reference_id="spike-exact-001",
            description="Promise against INV-1042",
        )
        self.assertEqual(payload["amount"], 4_000_000)
        self.assertFalse(payload["accept_partial"])
        self.assertNotIn("first_min_partial_amount", payload)

    def test_partial_payload_contains_minimum_first_amount(self) -> None:
        payload = build_payment_link_payload(
            amount_paise=4_800_000,
            reference_id="spike-partial-001",
            description="Partial promise against INV-1042",
            accept_partial=True,
            first_min_partial_amount=4_000_000,
        )
        self.assertTrue(payload["accept_partial"])
        self.assertEqual(payload["first_min_partial_amount"], 4_000_000)

    def test_partial_minimum_must_be_below_total(self) -> None:
        with self.assertRaises(ValueError):
            build_payment_link_payload(
                amount_paise=4_800_000,
                reference_id="spike-partial-001",
                description="Partial promise against INV-1042",
                accept_partial=True,
                first_min_partial_amount=4_800_000,
            )


if __name__ == "__main__":
    unittest.main()
