from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ai_insights import WeekPeriod, _create_week_document
from publishers.feishu_archive import (
    AI_INSIGHTS,
    SIX_COUNTRY,
    ArchiveCandidate,
    FeishuArchiveError,
    FeishuArchiveManager,
)
from publishers.country_report_archive import CountryReportArchiveManager


ROOT = Path(__file__).resolve().parents[1]


class FakePublisher:
    BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(self):
        self.folder_token = "legacy-folder"
        self.permission_calls = []
        self.records = []

    async def _get_tenant_access_token(self):
        return "tenant-token"

    async def upload_file(self, file_path, file_name):
        self.upload = (file_path, file_name, self.folder_token)
        return {
            "file_token": "pdf-token",
            "url": "https://feishu.cn/file/pdf-token",
        }

    async def set_file_permission(self, file_token, chat_id):
        self.permission_calls.append((file_token, chat_id))
        return True

    def _record_document(self, token, title):
        self.records.append((token, title))


class StubArchive(CountryReportArchiveManager):
    def __init__(self, publisher, children=None, owner_id="old-owner"):
        super().__init__(
            publisher,
            root_folder_token="root-token",
            admin_open_id="ou_admin",
        )
        self.children = children or []
        self.owner_id = owner_id
        self.requests = []

    async def list_folder(self, folder_token, **kwargs):
        if folder_token == "root-token":
            return list(self.children)
        return []

    async def get_meta(self, file_token, resource_type):
        return {"owner_id": self.owner_id}

    async def _request_json(self, method, path, *, params=None, payload=None):
        self.requests.append((method, path, params, payload))
        return {}


def folder(name, token):
    return {"type": "folder", "name": name, "token": token}


class ArchiveRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_exact_two_child_folders(self):
        archive = StubArchive(
            FakePublisher(),
            [
                folder("六国洞察报告", "six-token"),
                folder("AI洞察报告", "ai-token"),
            ],
        )
        self.assertEqual(
            await archive.resolve_report_folders(),
            {SIX_COUNTRY: "six-token", AI_INSIGHTS: "ai-token"},
        )

    async def test_duplicate_or_missing_folder_fails_closed(self):
        archive = StubArchive(
            FakePublisher(),
            [
                folder("六国洞察报告", "six-a"),
                folder("六国洞察报告", "six-b"),
                folder("AI洞察报告", "ai-token"),
            ],
        )
        with self.assertRaises(FeishuArchiveError):
            await archive.resolve_report_folders()

    async def test_resolves_country_folder_from_chinese_or_english_alias(self):
        archive = StubArchive(
            FakePublisher(),
            [
                folder("印度洞察报告", "india-token"),
                folder("Indonesia Weekly Insights", "indonesia-token"),
            ],
        )
        self.assertEqual(
            await archive.country_report_folder_token("india"),
            "india-token",
        )
        self.assertEqual(
            await archive.country_report_folder_token("indonesia"),
            "indonesia-token",
        )

    async def test_missing_country_folder_fails_closed(self):
        archive = StubArchive(
            FakePublisher(),
            [folder("肯尼亚", "kenya-token")],
        )
        with self.assertRaisesRegex(FeishuArchiveError, "Available child folders"):
            await archive.country_report_folder_token("india")

    async def test_transfer_owner_uses_stay_put_and_retains_bot_access(self):
        archive = StubArchive(FakePublisher())
        self.assertTrue(await archive.transfer_owner("doc-token", "docx", strict=True))
        method, path, params, payload = archive.requests[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(
            path,
            "/drive/v1/permissions/doc-token/members/transfer_owner",
        )
        self.assertEqual(params["stay_put"], "true")
        self.assertEqual(params["remove_old_owner"], "false")
        self.assertEqual(params["old_owner_perm"], "full_access")
        self.assertEqual(payload["member_type"], "openid")
        self.assertEqual(payload["member_id"], "ou_admin")

    async def test_upload_pdf_routes_before_upload_and_is_non_blocking(self):
        publisher = FakePublisher()
        archive = StubArchive(
            publisher,
            [
                folder("六国洞察报告", "six-token"),
                folder("AI洞察报告", "ai-token"),
            ],
            owner_id="ou_admin",
        )
        with tempfile.NamedTemporaryFile(suffix=".pdf") as pdf:
            url = await archive.upload_pdf(
                pdf.name,
                "六国用研洞察 - 2026-08-10",
                "oc_chat",
                SIX_COUNTRY,
            )
        self.assertEqual(url, "https://feishu.cn/file/pdf-token")
        self.assertEqual(publisher.upload[2], "six-token")
        self.assertEqual(publisher.permission_calls, [("pdf-token", "oc_chat")])
        self.assertEqual(
            publisher.records,
            [("pdf-token", "六国用研洞察 - 2026-08-10")],
        )

    async def test_country_pdf_routes_and_requires_owner_transfer(self):
        publisher = FakePublisher()
        archive = StubArchive(
            publisher,
            [folder("印度", "india-token")],
            owner_id="old-owner",
        )
        with tempfile.NamedTemporaryFile(suffix=".pdf") as pdf:
            url = await archive.upload_country_pdf(
                pdf.name,
                "印度洞察周报 / India Weekly Insights - 2026-08-25",
                "oc_chat",
                "india",
            )
        self.assertEqual(url, "https://feishu.cn/file/pdf-token")
        self.assertEqual(publisher.upload[2], "india-token")
        self.assertEqual(publisher.permission_calls, [("pdf-token", "oc_chat")])
        self.assertTrue(
            any("transfer_owner" in request[1] for request in archive.requests)
        )

    def test_report_classification_is_narrow(self):
        self.assertEqual(
            FeishuArchiveManager.classify_report(
                "AI_Insights_2026-08-03_2026-08-09.pdf"
            ),
            AI_INSIGHTS,
        )
        self.assertEqual(
            FeishuArchiveManager.classify_report("🔍 六国用研洞察 - 2026-08-10.pdf"),
            SIX_COUNTRY,
        )
        self.assertIsNone(
            FeishuArchiveManager.classify_report("unrelated research report.pdf")
        )


class MigrationIsolationTests(unittest.TestCase):
    def test_existing_workflow_schedules_and_commands_are_unchanged(self):
        daily = (ROOT / ".github/workflows/ai-insights-daily.yml").read_text()
        weekly = (ROOT / ".github/workflows/ai-insights-weekly.yml").read_text()
        six = (ROOT / ".github/workflows/daily-digest.yml").read_text()
        migration = (
            ROOT / ".github/workflows/feishu-archive-migration.yml"
        ).read_text()

        self.assertIn('cron: "30 23 * * *"', daily)
        self.assertIn("python ai_insights.py collect", daily)
        self.assertIn('cron: "47 8 * * 1"', weekly)
        self.assertIn("python ai_insights.py publish", weekly)
        self.assertIn("cron: '0 23 * * *'", six)
        self.assertIn("python main.py", six)
        self.assertIn("workflow_dispatch:", migration)
        self.assertNotIn("schedule:", migration)
        self.assertNotIn("FEISHU_BOT_CHAT_ID", migration)

    def test_daily_and_weekly_fallbacks_preserve_existing_publish_paths(self):
        ai_code = (ROOT / "ai_insights.py").read_text()
        six_code = (ROOT / "main.py").read_text()
        self.assertIn("falling back to the app root", ai_code)
        self.assertIn("pdf_url = await publisher.upload_pdf", ai_code)
        self.assertIn("doc_url = await publisher.upload_pdf", six_code)


class ExistingFlowFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_folder_failure_does_not_transfer_fallback_document(self):
        class ExistingPublisher:
            folder_token = ""

            async def create_document(self, title):
                return "fallback-doc"

            async def set_document_public_permission(self, document_id, chat_id):
                return True

            def _markdown_to_blocks(self, content):
                return [{"content": content}]

            async def write_content(self, document_id, blocks):
                return None

        class FailingArchive:
            is_enabled = True
            transfer_called = False

            async def configure_publisher_folder(self, report_kind):
                raise FeishuArchiveError("folder unavailable")

            async def archive_created_document(self, document_id, strict=False):
                self.transfer_called = True

        archive = FailingArchive()
        result = await _create_week_document(
            ExistingPublisher(),
            WeekPeriod(date(2026, 8, 10), date(2026, 8, 16)),
            "oc_chat",
            archive,
        )
        self.assertEqual(result["document_id"], "fallback-doc")
        self.assertFalse(archive.transfer_called)
