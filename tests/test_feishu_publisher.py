from __future__ import annotations

import unittest
from unittest.mock import patch

from publishers.feishu_publisher import FeishuPublisher


class FakeResponse:
    def __init__(self, body: str, status: int = 200):
        self.body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self):
        return self.body


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class FeishuDocumentCreationTests(unittest.IsolatedAsyncioTestCase):
    async def _publisher(self, folder_token: str = ""):
        publisher = FeishuPublisher()
        publisher.folder_token = folder_token

        async def fake_token():
            return "tenant-token"

        publisher._get_tenant_access_token = fake_token
        return publisher

    async def test_creates_folder_document_through_docx_api(self):
        publisher = await self._publisher("ai-insights-folder")
        session = FakeSession(
            FakeResponse(
                '{"code":0,"msg":"success","data":{"document":'
                '{"document_id":"doc-token","title":"Weekly"}}}'
            )
        )

        with patch(
            "publishers.feishu_publisher.aiohttp.ClientSession",
            return_value=session,
        ):
            document_id = await publisher.create_document("Weekly")

        self.assertEqual(document_id, "doc-token")
        url, request = session.calls[0]
        self.assertEqual(
            url,
            "https://open.feishu.cn/open-apis/docx/v1/documents",
        )
        self.assertEqual(
            request["json"],
            {"title": "Weekly", "folder_token": "ai-insights-folder"},
        )

    async def test_omits_folder_token_for_root_document(self):
        publisher = await self._publisher()
        session = FakeSession(
            FakeResponse(
                '{"code":0,"data":{"document":'
                '{"document_id":"root-doc"}}}'
            )
        )

        with patch(
            "publishers.feishu_publisher.aiohttp.ClientSession",
            return_value=session,
        ):
            await publisher.create_document("Root document")

        self.assertEqual(session.calls[0][1]["json"], {"title": "Root document"})

    async def test_non_json_error_reports_http_status(self):
        publisher = await self._publisher("ai-insights-folder")
        session = FakeSession(FakeResponse("404 page not found", status=404))

        with patch(
            "publishers.feishu_publisher.aiohttp.ClientSession",
            return_value=session,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "returned non-JSON data \\(HTTP 404\\)",
            ):
                await publisher.create_document("Weekly")

    async def test_business_error_is_not_treated_as_success(self):
        publisher = await self._publisher("ai-insights-folder")
        session = FakeSession(
            FakeResponse('{"code":1770001,"msg":"invalid param"}', status=400)
        )

        with patch(
            "publishers.feishu_publisher.aiohttp.ClientSession",
            return_value=session,
        ):
            with self.assertRaisesRegex(RuntimeError, "code=1770001"):
                await publisher.create_document("Weekly")


if __name__ == "__main__":
    unittest.main()
