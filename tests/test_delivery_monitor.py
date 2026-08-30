from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from monitoring import (
    empty_feishu_receipt,
    new_run_receipt,
    record_delivery,
    require_all_required_primary,
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
    async def test_ai_card_returns_shared_send_receipt(self):
        publisher = FeishuPublisher()
        expected = {
            **empty_feishu_receipt("acknowledged"),
            "api_ack_at": "2026-08-30T01:00:00Z",
        }
        publisher._send_message = AsyncMock(return_value=expected)
        result = await publisher.send_ai_insights_card(
            "oc_not_serialized",
            "判断",
            {"用研": ["标题"]},
            "https://example.com/report.pdf",
        )
        self.assertEqual(result, expected)

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
        self.assertEqual(saved["schema_version"], 2)
        self.assertEqual(saved["destination_tier"], "custom")
        self.assertEqual(saved["feishu_send"]["status"], "blocked")
        with self.assertRaisesRegex(RuntimeError, "not acknowledged"):
            require_confirmed_delivery(saved, required=True)
        require_confirmed_delivery(saved, required=False)
        record_delivery(
            saved,
            "seven-country-daily",
            {**empty_feishu_receipt("acknowledged"), "api_ack_at": "2026-08-30T01:00:00Z"},
        )
        require_confirmed_delivery(saved, required=True)

    def test_multi_target_partial_failure_fails_closed_and_redacts(self):
        receipt = new_run_receipt(
            "country-weekly",
            ["country-weekly-india", "country-weekly-indonesia"],
        )
        record_delivery(
            receipt,
            "country-weekly-india",
            {
                **empty_feishu_receipt("acknowledged"),
                "api_ack_at": "2026-08-30T01:00:00Z",
                "raw_message": "oc_secret provider says token=secret",
            },
        )
        record_delivery(
            receipt,
            "country-weekly-indonesia",
            {
                **empty_feishu_receipt("failed"),
                "provider_code": 230001,
                "error_code": "provider_rejected",
                "provider_message": "oc_secret",
            },
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "receipt.json"
            write_receipt_atomic(receipt, path)
            serialized = path.read_text(encoding="utf-8")
        self.assertNotIn("oc_secret", serialized)
        self.assertNotIn("provider_message", serialized)
        with self.assertRaisesRegex(RuntimeError, "country-weekly-indonesia=failed"):
            require_all_required_primary(receipt, required=True)

    def test_unknown_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown monitoring target"):
            new_run_receipt("country-weekly", ["oc_secret"])

    def test_acknowledged_status_without_timestamp_fails_closed(self):
        receipt = new_run_receipt()
        record_delivery(
            receipt,
            "seven-country-daily",
            empty_feishu_receipt("acknowledged"),
        )
        with self.assertRaisesRegex(RuntimeError, "seven-country-daily=acknowledged"):
            require_all_required_primary(receipt, required=True)


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
            "receipt_missing",
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

    def test_post_delivery_safe_error_is_preserved(self):
        receipt = new_run_receipt(
            "ux-combined-weekly", ["ux-combined-weekly"]
        )
        record_delivery(
            receipt,
            "ux-combined-weekly",
            {
                **empty_feishu_receipt("acknowledged"),
                "api_ack_at": "2026-08-31T09:00:00Z",
            },
        )
        record_delivery(
            receipt,
            "ux-combined-weekly",
            error_code="state_save_failed",
        )
        event = {
            "workflow_run": {
                "id": 790,
                "name": "AI Design + Insights Weekly Push",
                "event": "workflow_run",
                "conclusion": "failure",
                "created_at": "2026-08-31T08:59:00Z",
                "run_started_at": "2026-08-31T08:59:01Z",
                "updated_at": "2026-08-31T09:01:00Z",
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
        self.assertEqual(record["errors"][0]["code"], "state_save_failed")

    def test_missing_country_receipt_creates_five_expected_records(self):
        event = {
            "workflow_run": {
                "id": 900,
                "name": "Five-Country Weekly Insights",
                "event": "schedule",
                "conclusion": "failure",
                "created_at": "2026-08-30T22:30:00Z",
                "run_started_at": "2026-08-30T22:30:02Z",
                "updated_at": "2026-08-30T22:35:00Z",
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            event_path = root / "event.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            payload = aggregate(root / "missing.json", event_path, root / "out.json")
        self.assertEqual(len(payload["records"]), 5)
        self.assertEqual(
            {record["target_key"] for record in payload["records"]},
            {
                "country-weekly-india", "country-weekly-indonesia",
                "country-weekly-nigeria", "country-weekly-pakistan",
                "country-weekly-bangladesh",
            },
        )
        self.assertTrue(all(record["status"] == "unconfirmed" for record in payload["records"]))
        self.assertTrue(
            all(record["errors"][0]["code"] == "receipt_missing" for record in payload["records"])
        )

    def test_country_partial_receipt_marks_later_targets_not_attempted(self):
        receipt = new_run_receipt(
            "country-weekly",
            [
                "country-weekly-india",
                "country-weekly-indonesia",
                "country-weekly-nigeria",
                "country-weekly-pakistan",
                "country-weekly-bangladesh",
            ],
            destination_tier="production",
        )
        record_delivery(
            receipt,
            "country-weekly-india",
            {
                **empty_feishu_receipt("acknowledged"),
                "api_ack_at": "2026-08-30T22:30:00Z",
            },
        )
        record_delivery(
            receipt,
            "country-weekly-indonesia",
            status="failed",
            error_code="provider_rejected",
        )
        event = {
            "workflow_run": {
                "id": 903,
                "name": "Five-Country Weekly Insights",
                "event": "schedule",
                "conclusion": "failure",
                "created_at": "2026-08-30T22:25:00Z",
                "run_started_at": "2026-08-30T22:25:02Z",
                "updated_at": "2026-08-30T22:35:00Z",
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            receipt_path = root / "receipt.json"
            event_path = root / "event.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            event_path.write_text(json.dumps(event), encoding="utf-8")
            payload = aggregate(receipt_path, event_path, root / "out.json")
        statuses = {record["target_key"]: record["status"] for record in payload["records"]}
        self.assertEqual(statuses["country-weekly-india"], "normal")
        self.assertEqual(statuses["country-weekly-indonesia"], "failed")
        self.assertTrue(
            all(
                statuses[key] == "not_attempted"
                for key in (
                    "country-weekly-nigeria",
                    "country-weekly-pakistan",
                    "country-weekly-bangladesh",
                )
            )
        )

    def test_manual_test_is_excluded_but_manual_production_is_recorded(self):
        event = {
            "workflow_run": {
                "id": 902,
                "name": "Five-Country Weekly Insights",
                "event": "workflow_dispatch",
                "conclusion": "success",
                "created_at": "2026-08-31T01:00:00Z",
                "run_started_at": "2026-08-31T01:00:01Z",
                "updated_at": "2026-08-31T01:05:00Z",
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            event_path = root / "event.json"
            receipt_path = root / "receipt.json"
            output_path = root / "out.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")

            test_receipt = new_run_receipt(
                "country-weekly",
                ["country-weekly-india"],
                destination_tier="test",
            )
            receipt_path.write_text(json.dumps(test_receipt), encoding="utf-8")
            payload = aggregate(receipt_path, event_path, output_path)
            self.assertEqual(payload["records"], [])

            production_receipt = new_run_receipt(
                "country-weekly",
                ["country-weekly-india"],
                destination_tier="production",
            )
            record_delivery(
                production_receipt,
                "country-weekly-india",
                {
                    **empty_feishu_receipt("acknowledged"),
                    "api_ack_at": "2026-08-31T01:04:00Z",
                },
            )
            receipt_path.write_text(json.dumps(production_receipt), encoding="utf-8")
            payload = aggregate(receipt_path, event_path, output_path)
            self.assertEqual(len(payload["records"]), 1)
            self.assertEqual(payload["records"][0]["trigger_scope"], "manual-production")

    def test_ux_alert_is_not_counted_as_formal_delivery(self):
        receipt = new_run_receipt(
            "ux-combined-weekly",
            ["ux-combined-weekly"],
            planned_at="2026-08-31T08:50:00Z",
        )
        record_delivery(
            receipt, "ux-combined-weekly", status="blocked", error_code="feed_invalid"
        )
        record_delivery(
            receipt,
            "ux-combined-alert",
            {**empty_feishu_receipt("acknowledged"), "api_ack_at": "2026-08-31T08:51:00Z"},
            required=False,
        )
        event = {
            "workflow_run": {
                "id": 901,
                "name": "AI Design + Insights Weekly Push",
                "event": "workflow_run",
                "conclusion": "failure",
                "created_at": "2026-08-31T08:50:01Z",
                "run_started_at": "2026-08-31T08:50:02Z",
                "updated_at": "2026-08-31T08:51:01Z",
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            receipt_path = root / "receipt.json"
            event_path = root / "event.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            event_path.write_text(json.dumps(event), encoding="utf-8")
            payload = aggregate(receipt_path, event_path, root / "out.json")
        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(payload["records"][0]["target_key"], "ux-combined-weekly")
        self.assertEqual(payload["records"][0]["status"], "blocked")
        self.assertEqual(payload["records"][0]["planned_at"], "2026-08-31T08:50:00.000Z")


class WorkflowTests(unittest.TestCase):
    def test_workflows_upload_and_aggregate_receipt(self):
        production_files = [
            "daily-digest.yml",
            "ai-insights-weekly.yml",
            "ai-design-combined-weekly.yml",
            "country-insights-weekly.yml",
        ]
        workflows = {
            name: (ROOT / ".github/workflows" / name).read_text()
            for name in production_files
        }
        monitor = (ROOT / ".github/workflows/seven-country-monitor.yml").read_text()
        for content in workflows.values():
            self.assertIn("if: always()", content)
            self.assertIn("push-monitor-receipt-${{ github.run_attempt }}", content)
            self.assertIn("REQUIRE_FEISHU_DELIVERY", content)
            self.assertIn("MONITOR_RECEIPT_PATH", content)
            self.assertIn("MONITOR_DESTINATION_TIER", content)
        for workflow_name in (
            "Seven-Country Info Insights",
            "AI Insights Weekly Publish",
            "AI Design + Insights Weekly Push",
            "Five-Country Weekly Insights",
        ):
            self.assertIn(workflow_name, monitor)
        self.assertIn("push-monitor-runs.json", monitor)
        self.assertIn("contents: write", monitor)
        self.assertIn("github.event.workflow_run.event == 'schedule'", monitor)
        self.assertIn("github.event.workflow_run.event == 'workflow_run'", monitor)
        self.assertIn("github.event.workflow_run.event == 'workflow_dispatch'", monitor)


if __name__ == "__main__":
    unittest.main()
