import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from collectors.base import NewsItem
from email_sender import EmailSender
from processors.deduper import (
    balanced_limit,
    filter_by_date,
    finalize_categories,
)
from publishers.feishu_publisher import FeishuPublisher
from reporting import build_source_appendix, sanitize_public_text


ROOT = Path(__file__).resolve().parents[1]


def make_item(
    title: str,
    country: str,
    *,
    category: str = "mobile_market",
    relevance_score: float = 3.0,
) -> NewsItem:
    return NewsItem(
        title=title,
        url=f"https://example.com/{country}/{title}",
        source="Test",
        category=category,
        country=country,
        relevance_score=relevance_score,
        published=datetime.now(timezone.utc),
    )


class CountryBalanceTests(unittest.TestCase):
    def test_available_country_gets_a_slot_before_category_cap(self):
        india = [
            make_item(f"india-{index}", "india", relevance_score=5.0)
            for index in range(8)
        ]
        kenya = make_item("kenya-signal", "kenya", relevance_score=2.0)

        selected = balanced_limit(india + [kenya], limit=3)

        self.assertIn("kenya", {item.country for item in selected})
        self.assertEqual(len(selected), 3)

    def test_ai_category_is_used_for_final_regrouping(self):
        item = make_item("reclassified", "pakistan", category="mobile_market")
        result = finalize_categories(
            {"country_news": [item]},
            max_per_category=15,
            category_order=["country_news", "mobile_market"],
        )

        self.assertNotIn("country_news", result)
        self.assertEqual(result["mobile_market"], [item])


class FreshnessTests(unittest.TestCase):
    def test_periodic_official_source_uses_its_own_window(self):
        now = datetime.now(timezone.utc)
        official = NewsItem(
            title="Official weekly release",
            url="https://example.com/official",
            source="Official",
            category="mobile_market",
            country="russia",
            published=now - timedelta(days=4),
            freshness_days=7,
        )
        ordinary = NewsItem(
            title="Ordinary old article",
            url="https://example.com/ordinary",
            source="Ordinary",
            category="mobile_market",
            country="russia",
            published=now - timedelta(days=4),
        )

        result = filter_by_date([official, ordinary], days=1)

        self.assertEqual(result, [official])


class SourceConfigTests(unittest.TestCase):
    def test_enabled_feeds_have_unique_urls(self):
        with (ROOT / "config" / "sources.yaml").open(encoding="utf-8") as file:
            config = yaml.safe_load(file)

        enabled = [
            source
            for source in config["rss_sources"].values()
            if source.get("enabled", True)
        ]
        urls = [source["url"] for source in enabled]
        self.assertEqual(len(urls), len(set(urls)))

    def test_broken_feeds_are_disabled_and_official_feeds_enabled(self):
        with (ROOT / "config" / "sources.yaml").open(encoding="utf-8") as file:
            sources = yaml.safe_load(file)["rss_sources"]

        for source_id in (
            "channels_tv",
            "krasia",
            "medianama",
            "techcabal",
            "techpoint_africa",
            "techeconomy_ng",
            "techweez_ke",
            "indian_express_tech",
            "phoneradar",
        ):
            self.assertFalse(sources[source_id]["enabled"])

        for source_id in (
            "bank_of_russia",
            "reserve_bank_india",
            "trai_official",
            "komdigi_official",
            "ncc_official",
            "ca_kenya_official",
            "pta_official",
            "counterpoint_market",
            "omdia_market",
        ):
            self.assertTrue(sources[source_id]["enabled"])


class PublicOutputTests(unittest.TestCase):
    def test_public_country_label_is_always_sanitized(self):
        self.assertEqual(sanitize_public_text("俄罗斯市场与俄罗斯用户"), "EE1市场与EE1用户")

        item = make_item("俄罗斯手机市场变化", "russia")
        item.summary = "俄罗斯用户更加关注续航。"
        report_html = EmailSender().render_email(
            categories={"mobile_market": [item]},
            category_names={"mobile_market": "手机市场"},
            highlights="俄罗斯今日要点",
        )
        self.assertNotIn("俄罗斯", report_html)
        self.assertIn("EE1手机市场变化", report_html)
        self.assertIn("EE1用户更加关注续航", report_html)

        card = FeishuPublisher()._build_card_content(
            "俄罗斯洞察",
            "俄罗斯用户行为变化",
            {},
            {},
        )
        card_payload = json.loads(card)
        visible_text = json.dumps(card_payload, ensure_ascii=False)
        self.assertNotIn("俄罗斯", visible_text)
        self.assertIn("EE1洞察", visible_text)
        self.assertEqual(FeishuPublisher._safe_lark_md_line("俄罗斯用户"), "EE1用户")

    def test_appendix_lists_every_enabled_source_with_weight(self):
        with (ROOT / "config" / "sources.yaml").open(encoding="utf-8") as file:
            config = yaml.safe_load(file)

        appendix = build_source_appendix(config)
        sources = [source for column in appendix["columns"] for source in column]
        enabled_count = sum(
            source.get("enabled", True)
            for source in config["rss_sources"].values()
        )

        self.assertEqual(appendix["enabled_count"], enabled_count)
        self.assertEqual(len(sources), enabled_count)
        self.assertTrue(all(source["priority"] for source in sources))
        self.assertEqual(
            {source["country"] for source in sources if source["country_code"] == "russia"},
            {"EE1"},
        )

        report_html = EmailSender().render_email(
            categories={},
            category_names={},
            source_appendix=appendix,
        )
        self.assertIn("信息源、权重与过滤逻辑", report_html)
        self.assertIn("The Moscow Times", report_html)
        self.assertIn("W1.4", report_html)
        self.assertNotIn("俄罗斯", report_html)


if __name__ == "__main__":
    unittest.main()
