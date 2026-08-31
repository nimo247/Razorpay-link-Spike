import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.razorpay_client import expected_webhook_signature


class FastAPIWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        app = create_app(Path(self.temporary_directory.name) / "events.db")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_health_route(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_payment_link_requires_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post(
                "/payment-links",
                json={
                    "amount_paise": 4_000_000,
                    "reference_id": "spike-exact-001",
                    "description": "Promise against INV-1042",
                },
            )
        self.assertEqual(response.status_code, 503)

    @patch("app.main.create_payment_link")
    def test_payment_link_route_maps_valid_response(self, create_link) -> None:
        create_link.return_value = {
            "id": "plink_test",
            "short_url": "https://rzp.io/i/test",
            "status": "created",
            "amount": 4_000_000,
            "accept_partial": False,
        }
        with patch.dict(
            os.environ,
            {"RAZORPAY_KEY_ID": "rzp_test_x", "RAZORPAY_KEY_SECRET": "secret"},
            clear=True,
        ):
            response = self.client.post(
                "/payment-links",
                json={
                    "amount_paise": 4_000_000,
                    "reference_id": "spike-exact-001",
                    "description": "Promise against INV-1042",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "plink_test")

    def test_signed_partial_webhook_extracts_nested_payment_link(self) -> None:
        payload = {
            "event": "payment_link.partially_paid",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": "plink_partial",
                        "amount": 4_800_000,
                        "amount_paid": 4_000_000,
                        "status": "partially_paid",
                    }
                }
            },
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        signature = expected_webhook_signature(raw, "webhook-secret")
        headers = {
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": "evt_partial_1",
        }
        with patch.dict(
            os.environ, {"RAZORPAY_WEBHOOK_SECRET": "webhook-secret"}, clear=True
        ):
            response = self.client.post(
                "/webhooks/razorpay", content=raw, headers=headers
            )
            replay = self.client.post(
                "/webhooks/razorpay", content=raw, headers=headers
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payment_link_id"], "plink_partial")
        self.assertEqual(response.json()["amount_paid"], 4_000_000)
        self.assertFalse(response.json()["duplicate"])
        self.assertTrue(replay.json()["duplicate"])

    def test_invalid_signature_is_rejected_before_processing(self) -> None:
        raw = b'{"event":"payment_link.paid"}'
        with patch.dict(
            os.environ, {"RAZORPAY_WEBHOOK_SECRET": "webhook-secret"}, clear=True
        ):
            response = self.client.post(
                "/webhooks/razorpay",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": "wrong",
                    "X-Razorpay-Event-Id": "evt_bad_sig",
                },
            )
        self.assertEqual(response.status_code, 400)

    def test_event_id_header_is_required(self) -> None:
        raw = b'{"event":"payment_link.paid"}'
        signature = expected_webhook_signature(raw, "webhook-secret")
        with patch.dict(
            os.environ, {"RAZORPAY_WEBHOOK_SECRET": "webhook-secret"}, clear=True
        ):
            response = self.client.post(
                "/webhooks/razorpay",
                content=raw,
                headers={"X-Razorpay-Signature": signature},
            )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()

