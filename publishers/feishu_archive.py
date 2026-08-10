"""Feishu report archive routing and ownership helpers.

This module is intentionally separate from the existing publisher so archive
changes cannot alter collection, summarisation, scheduling, or message logic.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import aiohttp


SIX_COUNTRY = "six_country"
AI_INSIGHTS = "ai_insights"

DEFAULT_ROOT_FOLDER_TOKEN = "Cwkdf77bjldi8Xd3liac3NHDnPb"
DEFAULT_REPORT_FOLDERS = {
    SIX_COUNTRY: "六国洞察报告",
    AI_INSIGHTS: "AI洞察报告",
}
SUPPORTED_RESOURCE_TYPES = {
    "doc",
    "docx",
    "sheet",
    "bitable",
    "mindnote",
    "file",
    "slides",
    "wiki",
}


class FeishuArchiveError(RuntimeError):
    """Raised when a report archive operation cannot be completed safely."""


@dataclass(frozen=True)
class ArchiveCandidate:
    token: str
    name: str
    resource_type: str
    report_kind: str
    current_folder_token: str | None
    target_folder_token: str
    owner_id: str | None = None


class FeishuArchiveManager:
    """Route reports into shared folders and transfer ownership to a user."""

    def __init__(
        self,
        publisher,
        root_folder_token: str | None = None,
        admin_open_id: str | None = None,
    ) -> None:
        self.publisher = publisher
        self.root_folder_token = (
            root_folder_token
            if root_folder_token is not None
            else os.environ.get(
                "FEISHU_ARCHIVE_ROOT_FOLDER_TOKEN",
                DEFAULT_ROOT_FOLDER_TOKEN,
            )
        ).strip()
        self.admin_open_id = (
            admin_open_id
            if admin_open_id is not None
            else os.environ.get("FEISHU_ADMIN_OPEN_ID", "")
        ).strip()
        self.report_folder_names = {
            SIX_COUNTRY: os.environ.get(
                "FEISHU_SIX_COUNTRY_FOLDER_NAME",
                DEFAULT_REPORT_FOLDERS[SIX_COUNTRY],
            ).strip(),
            AI_INSIGHTS: os.environ.get(
                "FEISHU_AI_INSIGHTS_FOLDER_NAME",
                DEFAULT_REPORT_FOLDERS[AI_INSIGHTS],
            ).strip(),
        }
        self._folder_tokens: dict[str, str] | None = None

    @property
    def is_enabled(self) -> bool:
        return bool(self.root_folder_token)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        payload: dict | None = None,
    ) -> dict:
        tenant_token = await self.publisher._get_tenant_access_token()
        headers = {"Authorization": f"Bearer {tenant_token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"

        url = f"{self.publisher.BASE_URL}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                url,
                params=params,
                json=payload,
                headers=headers,
            ) as response:
                try:
                    data = await response.json()
                except Exception as exc:
                    body = await response.text()
                    raise FeishuArchiveError(
                        f"Feishu returned non-JSON data for {path}: "
                        f"HTTP {response.status}, {body[:300]}"
                    ) from exc

        if response.status != 200 or data.get("code") != 0:
            raise FeishuArchiveError(
                f"Feishu request failed for {path}: "
                f"HTTP {response.status}, code={data.get('code')}, "
                f"msg={data.get('msg', '')}"
            )
        return data.get("data", {})

    async def list_folder(
        self,
        folder_token: str | None,
        *,
        recursive: bool = False,
        _seen: set[str] | None = None,
    ) -> list[dict]:
        """List every item in one folder, following all pagination tokens."""
        page_token = None
        items: list[dict] = []
        seen = _seen if _seen is not None else set()

        while True:
            params = {
                "page_size": 200,
                "order_by": "EditedTime",
                "direction": "DESC",
                "user_id_type": "open_id",
            }
            if folder_token:
                params["folder_token"] = folder_token
            if page_token:
                params["page_token"] = page_token

            data = await self._request_json(
                "GET",
                "/drive/v1/files",
                params=params,
            )
            for raw in data.get("files", []):
                item = dict(raw)
                item["_parent_folder_token"] = folder_token
                items.append(item)

            if not data.get("has_more"):
                break
            page_token = data.get("next_page_token") or data.get("page_token")
            if not page_token:
                raise FeishuArchiveError(
                    "Feishu reported has_more=true without a page token."
                )

        if recursive:
            for item in list(items):
                if item.get("type") != "folder":
                    continue
                child_token = self._item_token(item)
                if not child_token or child_token in seen:
                    continue
                seen.add(child_token)
                items.extend(
                    await self.list_folder(
                        child_token,
                        recursive=True,
                        _seen=seen,
                    )
                )
        return items

    @staticmethod
    def _item_token(item: dict) -> str | None:
        return (
            item.get("token")
            or item.get("file_token")
            or item.get("document_id")
        )

    @staticmethod
    def _item_name(item: dict) -> str:
        return (
            item.get("name")
            or item.get("title")
            or item.get("file_name")
            or ""
        )

    async def resolve_report_folders(self) -> dict[str, str]:
        """Resolve the two exact child folder names under the shared root."""
        if self._folder_tokens is not None:
            return dict(self._folder_tokens)
        if not self.root_folder_token:
            raise FeishuArchiveError("Feishu archive root folder token is missing.")

        children = await self.list_folder(self.root_folder_token)
        resolved: dict[str, str] = {}
        for report_kind, folder_name in self.report_folder_names.items():
            matches = [
                item
                for item in children
                if item.get("type") == "folder"
                and self._item_name(item) == folder_name
            ]
            if len(matches) != 1:
                raise FeishuArchiveError(
                    f"Expected exactly one child folder named {folder_name!r}; "
                    f"found {len(matches)}."
                )
            token = self._item_token(matches[0])
            if not token:
                raise FeishuArchiveError(
                    f"Child folder {folder_name!r} has no folder token."
                )
            resolved[report_kind] = token

        self._folder_tokens = resolved
        return dict(resolved)

    async def report_folder_token(self, report_kind: str) -> str:
        folders = await self.resolve_report_folders()
        if report_kind not in folders:
            raise FeishuArchiveError(f"Unknown report kind: {report_kind}")
        return folders[report_kind]

    async def configure_publisher_folder(self, report_kind: str) -> str:
        """Point the existing publisher at one report folder."""
        folder_token = await self.report_folder_token(report_kind)
        self.publisher.folder_token = folder_token
        return folder_token

    async def find_report_by_title(
        self,
        report_kind: str,
        title: str,
    ) -> dict | None:
        folder_token = await self.report_folder_token(report_kind)
        for item in await self.list_folder(folder_token):
            if self._item_name(item) != title:
                continue
            token = self._item_token(item)
            if token:
                return {
                    "document_id": token,
                    "title": title,
                    "url": item.get("url") or f"https://feishu.cn/docx/{token}",
                    "raw": item,
                }
        return None

    async def get_meta(self, file_token: str, resource_type: str) -> dict:
        data = await self._request_json(
            "POST",
            "/drive/v1/metas/batch_query",
            params={"user_id_type": "open_id"},
            payload={
                "request_docs": [
                    {
                        "doc_token": file_token,
                        "doc_type": resource_type,
                    }
                ],
                "with_url": True,
            },
        )
        metas = data.get("metas", [])
        return metas[0] if metas else {}

    async def transfer_owner(
        self,
        file_token: str,
        resource_type: str,
        *,
        strict: bool = False,
    ) -> bool:
        """Transfer ownership to the configured user and retain bot full access."""
        if not self.admin_open_id:
            message = "FEISHU_ADMIN_OPEN_ID is missing; ownership was not transferred."
            if strict:
                raise FeishuArchiveError(message)
            print(f"   ⚠️ {message}")
            return False
        if resource_type not in SUPPORTED_RESOURCE_TYPES:
            message = f"Unsupported Feishu resource type: {resource_type}"
            if strict:
                raise FeishuArchiveError(message)
            print(f"   ⚠️ {message}")
            return False

        try:
            meta = await self.get_meta(file_token, resource_type)
            if meta.get("owner_id") == self.admin_open_id:
                return True
            await self._request_json(
                "POST",
                f"/drive/v1/permissions/{file_token}/members/transfer_owner",
                params={
                    "type": resource_type,
                    "need_notification": "false",
                    "remove_old_owner": "false",
                    "stay_put": "true",
                    "old_owner_perm": "full_access",
                },
                payload={
                    "member_type": "openid",
                    "member_id": self.admin_open_id,
                },
            )
            print("   ✅ Ownership transferred; bot retained full_access")
            return True
        except FeishuArchiveError:
            if strict:
                raise
            print(
                "   ⚠️ Ownership transfer failed; the report remains in the "
                "shared folder and publishing continues."
            )
            return False

    async def move_file(
        self,
        file_token: str,
        resource_type: str,
        folder_token: str,
    ) -> None:
        await self._request_json(
            "POST",
            f"/drive/v1/files/{file_token}/move",
            payload={
                "type": resource_type,
                "folder_token": folder_token,
            },
        )

    async def archive_created_document(
        self,
        document_id: str,
        *,
        strict: bool = False,
    ) -> bool:
        return await self.transfer_owner(
            document_id,
            "docx",
            strict=strict,
        )

    async def upload_pdf(
        self,
        pdf_path: str,
        title: str,
        chat_id: str | None,
        report_kind: str,
    ) -> str | None:
        """Upload one PDF directly into its report folder, then transfer owner."""
        await self.configure_publisher_folder(report_kind)
        result = await self.publisher.upload_file(pdf_path, f"{title}.pdf")
        if not result:
            return None

        file_token = result["file_token"]
        await self.publisher.set_file_permission(file_token, chat_id)
        self.publisher._record_document(file_token, title)
        await self.transfer_owner(file_token, "file", strict=False)
        return result["url"]

    @staticmethod
    def classify_report(name: str) -> str | None:
        normalized = re.sub(r"[\s_\-–—]+", "", name).lower()
        if "ai洞察" in normalized or "aiinsights" in normalized:
            return AI_INSIGHTS
        if "六国" in normalized or "sixcountry" in normalized:
            return SIX_COUNTRY
        return None

    async def migration_candidates(self) -> list[ArchiveCandidate]:
        """Find matching app-root and target-folder reports, without mutating them."""
        folders = await self.resolve_report_folders()
        sources: list[tuple[str | None, list[dict]]] = [
            (None, await self.list_folder(None, recursive=True)),
        ]
        for folder_token in folders.values():
            sources.append((folder_token, await self.list_folder(folder_token)))

        candidates: list[ArchiveCandidate] = []
        seen_tokens: set[str] = set()
        for source_folder, items in sources:
            for item in items:
                resource_type = item.get("type", "")
                if resource_type not in SUPPORTED_RESOURCE_TYPES:
                    continue
                name = self._item_name(item)
                report_kind = self.classify_report(name)
                token = self._item_token(item)
                if not report_kind or not token or token in seen_tokens:
                    continue
                seen_tokens.add(token)
                current_folder = item.get("_parent_folder_token") or source_folder
                meta = await self.get_meta(token, resource_type)
                candidates.append(
                    ArchiveCandidate(
                        token=token,
                        name=name,
                        resource_type=resource_type,
                        report_kind=report_kind,
                        current_folder_token=current_folder,
                        target_folder_token=folders[report_kind],
                        owner_id=meta.get("owner_id"),
                    )
                )
        candidates.sort(key=lambda item: (item.report_kind, item.name, item.token))
        return candidates

    async def migrate_candidate(self, candidate: ArchiveCandidate) -> str:
        """Move one report if needed, then transfer it to the configured owner."""
        moved = False
        transferred = False
        if candidate.current_folder_token != candidate.target_folder_token:
            await self.move_file(
                candidate.token,
                candidate.resource_type,
                candidate.target_folder_token,
            )
            moved = True
        if candidate.owner_id != self.admin_open_id:
            await self.transfer_owner(
                candidate.token,
                candidate.resource_type,
                strict=True,
            )
            transferred = True
        if moved and transferred:
            return "moved_and_transferred"
        if moved:
            return "moved"
        if transferred:
            return "transferred"
        return "already_complete"
