import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from collectors.base import NewsItem
from country_report import WEEKLY_COUNTRY_REPORTS, _country_items
from email_sender import EmailSender
from processors.deduper import TARGET_COUNTRIES, filter_by_date
from reporting import (
    CATEGORY_BILINGUAL_NAMES,
    build_source_appendix,
)


ROOT = Path(__file__).resolve().parents[1]


class SevenCountryCoverageTests(unittest.TestCase):
    def test_bangladesh_is_a_target_and_weekly_market(self):
        self.assertIn("bangladesh", TARGET_COUNTRIES)
        self.assertEqual(
            WEEKLY_COUNTRY_REPORTS,
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

    def test_weekly_window_applies_to_every_category(self):
        item = NewsItem(
            title="Five-day-old youth mobile trend",
            url="https://example.com/weekly",
            source="Local",
            category="pop_culture",
            country="india",
            published=datetime.now(timezone.utc) - timedelta(days=5),
        )
        self.assertEqual(filter_by_date([item], days=7), [item])


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


if __name__ == "__main__":
    unittest.main()
