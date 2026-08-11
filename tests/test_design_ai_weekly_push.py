import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from design_ai_weekly_push import (
    DESIGN_SITE_URL,
    DesignHeadline,
    WeekPeriod,
    _fallback_asset_urls,
    _parse_hot_items,
    _parse_news_items,
    build_combined_card,
    extract_ai_candidate_titles,
    extract_ai_highlights,
    extract_ai_pdf_url,
    get_previous_week,
    select_latest,
)


class WeekTests(unittest.TestCase):
    def test_previous_natural_week(self):
        period = get_previous_week(
            datetime(2026, 8, 10, 17, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        self.assertEqual(str(period.start), "2026-08-03")
        self.assertEqual(str(period.end), "2026-08-09")
        self.assertEqual(
            period.ai_report_title,
            "AI洞察资讯周报｜2026.08.03–08.09",
        )


class SiteParserTests(unittest.TestCase):
    def test_fallback_assets_are_discovered_from_hashed_imports(self):
        main_js = (
            'import("./data-build-a.js");'
            'import("./hotData-build-b.js");'
        )
        data_url, hot_url = _fallback_asset_urls(main_js)
        self.assertEqual(
            data_url,
            f"{DESIGN_SITE_URL}assets/data-build-a.js",
        )
        self.assertEqual(
            hot_url,
            f"{DESIGN_SITE_URL}assets/hotData-build-b.js",
        )

    def test_static_news_translation_and_source_group_parsing(self):
        main_js = (
            'KW=["creator"],HW=["official"],'
            '"url:https://example.com/a":{title_zh:"中文标题"}'
        )
        data_js = (
            'const e={items:[{id:1,title:"English",'
            'content_url:"https://example.com/a",'
            'published_at:"2026-08-10T01:00:00+00:00",'
            'source_slug:"creator",source_name:"Author"}]};'
        )
        items = _parse_news_items(data_js, main_js)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "中文标题")
        self.assertEqual(items[0].stream, "创作者观点")

    def test_hot_item_requires_link_or_multi_source_evidence(self):
        hot_js = (
            'const e={items:['
            '{id:"keep",title_zh:"保留",published_at:"2026-08-10",'
            'source:"Source",content_url:"https://example.com/a"},'
            '{id:"drop",title_zh:"丢弃",published_at:"2026-08-11",'
            'source:"Source",content_url:"",evidence_items:[]}'
            '],workbench_topics:[],research_items:[],selected_items:[]};'
        )
        items = _parse_hot_items(hot_js)
        self.assertEqual([item.title for item in items], ["保留"])

    def test_latest_selection_sorts_and_deduplicates(self):
        items = [
            DesignHeadline("1", "同一标题", "A", "2026-08-10T01:00:00Z", "x"),
            DesignHeadline("2", "同一标题！", "B", "2026-08-10T02:00:00Z", "x"),
            DesignHeadline("3", "不同标题", "C", "2026-08-09T01:00:00Z", "x"),
        ]
        selected = select_latest(items, limit=8)
        self.assertEqual([item.id for item in selected], ["2", "3"])


class InsightExtractionTests(unittest.TestCase):
    def test_extracts_pdf_and_first_three_core_judgments(self):
        text = """PDF版本
AI洞察PDF：查看完整周报 (https://feishu.cn/file/file_token_123)
本周最终精选
本周核心判断
1. 判断一
2. 判断二
3. 判断三
用研与消费者洞察
"""
        self.assertEqual(
            extract_ai_pdf_url(text),
            "https://feishu.cn/file/file_token_123",
        )
        self.assertEqual(
            extract_ai_highlights(text),
            ["判断一", "判断二", "判断三"],
        )

    def test_candidate_titles_are_a_safe_prepublication_fallback(self):
        text = """2026-08-08 每日收集
候选标题一 (https://example.com/one)
来源：A
候选标题二 (https://example.com/two)
候选标题一 (https://example.com/one)
"""
        self.assertEqual(
            extract_ai_candidate_titles(text),
            ["候选标题一", "候选标题二"],
        )


class CardTests(unittest.TestCase):
    def test_design_section_precedes_insights_and_has_two_buttons(self):
        period = WeekPeriod(
            start=datetime(2026, 8, 3).date(),
            end=datetime(2026, 8, 9).date(),
        )
        card = build_combined_card(
            period,
            [DesignHeadline("1", "设计标题", "A", "2026-08-10", "设计")],
            ["洞察判断"],
            "https://feishu.cn/file/report",
        )
        elements = card["elements"]
        self.assertIn("AI设计资讯", elements[0]["text"]["content"])
        self.assertIn("AI洞察资讯", elements[2]["text"]["content"])
        actions = elements[4]["actions"]
        self.assertEqual(
            [button["text"]["content"] for button in actions],
            ["查看AI设计完整周报", "查看AI洞察完整周报"],
        )
        self.assertEqual(actions[0]["multi_url"]["url"], DESIGN_SITE_URL)


if __name__ == "__main__":
    unittest.main()
