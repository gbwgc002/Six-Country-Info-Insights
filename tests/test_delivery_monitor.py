from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from monitoring import (
    empty_feishu_receipt,
    new_run_receipt,
    require_confirmed_delivery,
    write_receipt_atomic,
)
from publishers.feishu_publisher import FeishuPublisher, FeishuSendError
from scripts.aggregate_monitor_receipt import aggregate


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self.payload


class UnreadableResponse(FakeResponse):
    async def json(self):
        raise ValueError("invalid json")


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, *, json, headers):
        self.request = (url, json, headers)
        return self.response


class FeishuReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_sanitized_acknowledgement(self):
        publisher = FeishuPublisher()
        publisher._get_tenant_access_token = AsyncMock(return_value="secret-token")
        session = FakeSession(
            FakeResponse(
                200,
                {
                    "code": 0,
                    "data": {
                        "message_id": "om_sensitive_message_id",
                        "create_time": "1788067494000",
                    },
                },
            )
        )
        with patch(
            "publishers.feishu_publisher.aiohttp.ClientSession",
            return_value=session,
        ):
            result = await publisher._send_message(
                "oc_sensitive_chat_id", "text", "hello"
            )

        self.assertEqual(result["status"], "acknowledged")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["provider_code"], 0)
        self.assertEqual(result["attempt_count"], 1)
        self.assertTrue(result["request_started_at"].endswith("Z"))
        self.assertTrue(result["api_ack_at"].endswith("Z"))
        self.assertTrue(result["message_ref"].startswith("sha256:"))
        serialized = json.dumps(result)
        self.assertNotIn("om_sensitive_message_id", serialized)
        self.assertNotIn("oc_sensitive_chat_id", serialized)
        self.assertNotIn("secret-token", serialized)

    async def test_provider_failure_raises_with_sanitized_receipt(self):
        publisher = FeishuPublisher()
        publisher._get_tenant_access_token = AsyncMock(return_value="secret-token")
        session = FakeSession(
            FakeResponse(
                400,
                {"code": 230001, "msg": "chat oc_sensitive does not exist"},
            )
        )
        with patch(
            "publishers.feishu_publisher.aiohttp.ClientSession",
            return_value=session,
        ):
            with self.assertRaises(FeishuSendError) as caught:
                await publisher._send_message(
                    "oc_sensitive_chat_id", "text", "hello"
                )

        receipt = caught.exception.receipt
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["http_status"], 400)
        self.assertEqual(receipt["provider_code"], 230001)
        self.assertEqual(receipt["error_code"], "provider_rejected")
        self.assertIsNone(receipt["api_ack_at"])
        self.assertNotIn("oc_sensitive", json.dumps(receipt))
        self.assertNotIn("oc_sensitive", str(caught.exception))

    async def test_unreadable_response_is_unknown_not_failed(self):
        publisher = FeishuPublisher()
        publisher._get_tenant_access_token = AsyncMock(return_value="secret-token")
        with patch(
            "publishers.feishu_publisher.aiohttp.ClientSession",
            return_value=FakeSession(UnreadableResponse(200, {})),
        ):
            with self.assertRaises(FeishuSendError) as caught:
                await publisher._send_message("oc_sensitive_chat_id", "text", "hello")
        self.assertEqual(caught.exception.receipt["status"], "unknown")
        self.assertEqual(caught.exception.receipt["error_code"], "response_unreadable")


class ReceiptFileTests(unittest.TestCase):
    def test_atomic_receipt_and_required_delivery_gate(self):
        receipt = new_run_receipt()
        receipt["feishu_send"] = empty_feishu_receipt("not_configured")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nested" / "receipt.json"
            write_receipt_atomic(receipt, path)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["feishu_send"]["status"], "not_configured")
        with self.assertRaisesRegex(RuntimeError, "not acknowledged"):
            require_confirmed_delivery(saved, required=True)
        require_confirmed_delivery(saved, required=False)
        saved["feishu_send"]["status"] = "acknowledged"
        require_confirmed_delivery(saved, required=True)


class AggregatorTests(unittest.TestCase):
    def test_aggregates_schedule_with_exact_plan_and_deduplicates(self):
        receipt = {
            "run": {"id": "123", "attempt": 1, "event": "schedule"},
            "feishu_send": {
                "request_started_at": "2026-08-30T01:04:50Z",
                "api_ack_at": "2026-08-30T01:04:54Z",
                "feishu_create_time": "1788051894000",
                "http_status": 200,
                "provider_code": 0,
                "message_ref": "sha256:1234567890abcdef",
                "attempt_count": 1,
                "status": "acknowledged",
            },
        }
        event = {
            "workflow_run": {
                "id": 123,
                "run_attempt": 1,
                "event": "schedule",
                "conclusion": "success",
                "created_at": "2026-08-29T22:40:00Z",
                "run_started_at": "2026-08-29T22:41:00Z",
                "updated_at": "2026-08-30T01:05:00Z",
                "html_url": "https://github.com/example/actions/runs/123",
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            receipt_path = root / "receipt.json"
            event_path = root / "event.json"
            output_path = root / "seven-country-runs.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            event_path.write_text(json.dumps(event), encoding="utf-8")
            aggregate(receipt_path, event_path, output_path)
            aggregate(receipt_path, event_path, output_path)
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schedule"], "06:25")
        self.assertEqual(len(payload["records"]), 1)
        record = payload["records"][0]
        self.assertEqual(record["planned_at"], "2026-08-29T22:25:00.000Z")
        self.assertEqual(record["status"], "delayed")
        self.assertEqual(record["delay_minutes"], 159.9)
        self.assertEqual(record["report_date"], "2026-08-30")
        self.assertNotIn("oc_", json.dumps(payload))
        self.assertNotIn("message_ref", json.dumps(payload))
        self.assertNotIn("provider_code", json.dumps(payload))

    def test_missing_receipt_is_unconfirmed_without_raw_logs(self):
        event = {
            "workflow_run": {
                "id": 456,
                "event": "schedule",
                "conclusion": "success",
                "created_at": "2026-08-30T22:25:01Z",
                "run_started_at": "2026-08-30T22:25:05Z",
                "updated_at": "2026-08-30T22:30:00Z",
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            event_path = root / "event.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            payload = aggregate(
                root / "missing-receipt.json",
                event_path,
                root / "seven-country-runs.json",
            )
        self.assertEqual(payload["records"][0]["status"], "unconfirmed")
        self.assertEqual(
            payload["records"][0]["errors"][0]["code"],
            "delivery_unconfirmed",
        )

    def test_acknowledged_delivery_is_not_mislabeled_when_cleanup_fails(self):
        receipt = {
            "feishu_send": {
                "request_started_at": "2026-08-30T22:30:00Z",
                "api_ack_at": "2026-08-30T22:30:01Z",
                "status": "acknowledged",
            }
        }
        event = {
            "workflow_run": {
                "id": 789,
                "event": "schedule",
                "conclusion": "failure",
                "created_at": "2026-08-30T22:26:00Z",
                "run_started_at": "2026-08-30T22:26:03Z",
                "updated_at": "2026-08-30T22:31:00Z",
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            receipt_path = root / "receipt.json"
            event_path = root / "event.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            event_path.write_text(json.dumps(event), encoding="utf-8")
            payload = aggregate(receipt_path, event_path, root / "out.json")
        record = payload["records"][0]
        self.assertEqual(record["status"], "normal")
        self.assertEqual(record["errors"][0]["code"], "workflow_failed_after_delivery")


class WorkflowTests(unittest.TestCase):
    def test_workflows_upload_and_aggregate_receipt(self):
        daily = (ROOT / ".github/workflows/daily-digest.yml").read_text()
        monitor = (ROOT / ".github/workflows/seven-country-monitor.yml").read_text()
        self.assertIn("if: always()", daily)
        self.assertIn("seven-country-monitor-receipt", daily)
        self.assertIn("github.run_attempt", daily)
        self.assertIn("REQUIRE_FEISHU_DELIVERY", daily)
        self.assertIn('workflows: ["Seven-Country Info Insights"]', monitor)
        self.assertIn("seven-country-runs.json", monitor)
        self.assertIn("contents: write", monitor)
        self.assertIn("github.event.workflow_run.event == 'schedule'", monitor)


if __name__ == "__main__":
    unittest.main()
