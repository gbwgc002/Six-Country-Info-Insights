import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import yaml

from collectors.base import NewsItem
from country_candidate_store import items_for_period, merge_and_save
from country_report import (
    COUNTRY_REPORTS,
    _country_items,
    get_report_period,
)
from email_sender import EmailSender
from processors.deduper import TARGET_COUNTRIES
from reporting import (
    CATEGORY_BILINGUAL_NAMES,
    build_source_appendix,
)


ROOT = Path(__file__).resolve().parents[1]


class SevenCountryCoverageTests(unittest.TestCase):
    def test_bangladesh_is_a_target_and_country_report_market(self):
        self.assertIn("bangladesh", TARGET_COUNTRIES)
        self.assertEqual(
            COUNTRY_REPORTS,
            ("india", "indonesia", "nigeria", "pakistan", "bangladesh"),
        )

    def test_country_pool_includes_matching_global_items_only(self):
        india = NewsItem(
            title="India expands mobile payment access",
            url="https://example.com/india",
            source="Global",
            category="commerce_economy",
        )
        pakistan = NewsItem(
            title="Pakistan expands mobile payment access",
            url="https://example.com/pakistan",
            source="Global",
            category="commerce_economy",
        )
        selected = _country_items([india, pakistan], "india")
        self.assertEqual(selected, [india])

    def test_report_period_supports_rolling_preview_and_previous_natural_week(self):
        current = datetime(2026, 8, 25, 8, tzinfo=ZoneInfo("Asia/Shanghai"))
        rolling = get_report_period(current)
        previous = get_report_period(current, previous_week=True)
        self.assertEqual((rolling.start.isoformat(), rolling.end.isoformat()), ("2026-08-19", "2026-08-25"))
        self.assertEqual((previous.start.isoformat(), previous.end.isoformat()), ("2026-08-17", "2026-08-23"))

    def test_daily_candidate_store_deduplicates_and_selects_week(self):
        with TemporaryDirectory() as directory:
            store_path = Path(directory) / "candidates.json"
            collected = datetime(2026, 8, 25, 0, tzinfo=timezone.utc)
            item = NewsItem(
                title="India mobile wallets expand",
                url="https://example.com/weekly",
                source="Local",
                category="digital_ecosystem",
                country="india",
                published=datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
            )
            merge_and_save([item, item], path=store_path, collected_at=collected)
            selected = items_for_period(
                start=collected.date() - timedelta(days=1),
                end=collected.date(),
                path=store_path,
                timezone_info=timezone.utc,
            )
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0].url, item.url)


class BilingualReportTests(unittest.TestCase):
    def test_country_pdf_html_contains_matching_chinese_and_english(self):
        item = NewsItem(
            title="🇮🇳 印度智能手机价格出现变化",
            title_en="🇮🇳 Smartphone prices shift in India",
            summary="价格变化可能影响中端用户的换机决策。",
            summary_en="The price change may affect replacement decisions among mid-range users.",
            url="https://example.com/report",
            source="Example",
            category="mobile_market",
            country="india",
        )
        html = EmailSender().render_email(
            categories={"mobile_market": [item]},
            category_names=CATEGORY_BILINGUAL_NAMES,
            report_title="印度用研洞察 / India User Research Insights",
            bilingual=True,
        )
        self.assertIn(item.title, html)
        self.assertIn(item.title_en, html)
        self.assertIn(item.summary, html)
        self.assertIn(item.summary_en, html)
        self.assertIn("1 条洞察 / insights", html)

    def test_country_appendix_contains_country_and_shared_sources_only(self):
        config = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())
        appendix = build_source_appendix(config, "india")
        sources = [source for column in appendix["columns"] for source in column]
        country_codes = {source["country_code"] for source in sources}
        self.assertLessEqual(country_codes, {"india", "multi"})
        self.assertIn("india", country_codes)
        self.assertIn("multi", country_codes)
        self.assertNotIn("七国", " ".join(appendix["filter_rules"]))
        self.assertIn("本国", " ".join(appendix["filter_rules"]))


class CountryPreviewWorkflowTests(unittest.TestCase):
    def test_preview_is_manual_only_and_uses_named_test_group_secret(self):
        workflow = (
            ROOT / ".github" / "workflows" / "country-insight-preview.yml"
        ).read_text()
        self.assertIn("workflow_dispatch", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn(
            "secrets.FEISHU_GROUP_ZHANDIANGUANLIYONGYANNEIBU",
            workflow,
        )
        self.assertIn("python country_report.py", workflow)

    def test_country_runner_uses_archive_upload_with_ownership_transfer(self):
        runner = (ROOT / "country_report.py").read_text()
        self.assertIn("CountryReportArchiveManager(publisher)", runner)
        self.assertIn("archive.upload_country_pdf", runner)

    def test_country_runner_uses_weekly_titles_and_window(self):
        runner = (ROOT / "country_report.py").read_text()
        self.assertIn("洞察周报", runner)
        self.assertIn("Weekly Insights", runner)
        self.assertIn("report_days=7", runner)
        self.assertNotIn("洞察日报", runner)

    def test_daily_collection_and_weekly_publish_are_isolated(self):
        daily = (
            ROOT / ".github" / "workflows" / "country-insights-daily-collect.yml"
        ).read_text()
        weekly = (
            ROOT / ".github" / "workflows" / "country-insights-weekly.yml"
        ).read_text()
        aggregate = (ROOT / ".github" / "workflows" / "daily-digest.yml").read_text()
        self.assertIn("cron: '30 23 * * *'", daily)
        self.assertIn("--mode collect", daily)
        self.assertNotIn("FEISHU_APP_ID", daily)
        self.assertIn("cron: '0 23 * * 0'", weekly)
        self.assertIn("--previous-week", weekly)
        self.assertIn("FEISHU_GROUP_INDIA_ID", weekly)
        self.assertIn("cron: '0 23 * * *'", aggregate)


if __name__ == "__main__":
    unittest.main()
