"""Country-specific Feishu PDF archive routing.

The shared root remains configured by ``FeishuArchiveManager``. This module only
resolves the five country child folders and requires ownership transfer after
each upload, without changing the existing aggregate or AI report routes.
"""

from __future__ import annotations

import os
import re

from .feishu_archive import FeishuArchiveError, FeishuArchiveManager


COUNTRY_REPORT_FOLDER_ALIASES = {
    "india": ("印度", "India"),
    "indonesia": ("印尼", "印度尼西亚", "Indonesia"),
    "nigeria": ("尼日利亚", "Nigeria"),
    "pakistan": ("巴基斯坦", "Pakistan"),
    "bangladesh": ("孟加拉", "孟加拉国", "Bangladesh"),
}
COUNTRY_REPORT_FOLDER_SUFFIXES = (
    "",
    "洞察",
    "洞察报告",
    "周报",
    "报告",
    "Insights",
    "InsightsReport",
    "WeeklyInsights",
    "Report",
)


class CountryReportArchiveManager(FeishuArchiveManager):
    """Route country PDFs while preserving the existing archive manager."""

    def __init__(self, publisher, **kwargs) -> None:
        super().__init__(publisher, **kwargs)
        self._country_folder_tokens: dict[str, str] = {}

    @staticmethod
    def _normalize_folder_name(name: str) -> str:
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", name).casefold()

    def _country_folder_names(self, country: str) -> set[str]:
        if country not in COUNTRY_REPORT_FOLDER_ALIASES:
            raise FeishuArchiveError(f"Unsupported country report folder: {country}")

        override = os.environ.get(
            f"FEISHU_COUNTRY_FOLDER_NAME_{country.upper()}",
            "",
        ).strip()
        if override:
            return {self._normalize_folder_name(override)}

        return {
            self._normalize_folder_name(f"{alias}{suffix}")
            for alias in COUNTRY_REPORT_FOLDER_ALIASES[country]
            for suffix in COUNTRY_REPORT_FOLDER_SUFFIXES
        }

    async def country_report_folder_token(self, country: str) -> str:
        """Resolve one country folder directly under the shared archive root."""
        country = country.strip().lower()
        if country in self._country_folder_tokens:
            return self._country_folder_tokens[country]
        if not self.root_folder_token:
            raise FeishuArchiveError("Feishu archive root folder token is missing.")

        expected_names = self._country_folder_names(country)
        children = await self.list_folder(self.root_folder_token)
        folders = [item for item in children if item.get("type") == "folder"]
        matches = [
            item
            for item in folders
            if self._normalize_folder_name(self._item_name(item)) in expected_names
        ]
        if len(matches) != 1:
            available = ", ".join(
                sorted(self._item_name(item) for item in folders)
            ) or "(none)"
            raise FeishuArchiveError(
                f"Expected exactly one archive folder for {country}; "
                f"found {len(matches)}. Available child folders: {available}"
            )

        token = self._item_token(matches[0])
        if not token:
            raise FeishuArchiveError(
                f"Country folder {self._item_name(matches[0])!r} has no token."
            )
        self._country_folder_tokens[country] = token
        print(
            "   ✅ Country archive folder resolved: "
            f"{country} -> {self._item_name(matches[0])}"
        )
        return token

    async def upload_country_pdf(
        self,
        pdf_path: str,
        title: str,
        chat_id: str | None,
        country: str,
    ) -> str | None:
        """Upload a country PDF into its folder and require ownership transfer."""
        folder_token = await self.country_report_folder_token(country)
        self.publisher.folder_token = folder_token
        result = await self.publisher.upload_file(pdf_path, f"{title}.pdf")
        if not result:
            return None

        file_token = result["file_token"]
        await self.publisher.set_file_permission(file_token, chat_id)
        self.publisher._record_document(file_token, title)
        await self.transfer_owner(file_token, "file", strict=True)
        return result["url"]
