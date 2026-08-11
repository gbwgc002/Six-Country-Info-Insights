import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from design_ai_weekly_push import (
    DESIGN_FEED_URL,
    DESIGN_SITE_URL,
    DesignFeed,
    DesignHeadline,
    WeekPeriod,
    _fallback_asset_urls,
    _parse_hot_items,
    _parse_news_items,
    build_alert_card,
    build_combined_card,
    design_feed_problem,
    extract_ai_category_titles,
    extract_ai_pdf_url,
    get_previous_week,
    parse_latest_section_html,
    parse_design_feed,
    save_feed_state,
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

    def test_latest_section_is_parsed_by_semantic_rendered_structure(self):
        section_html = """
        <section aria-labelledby="latest-intelligence-heading">
          <h2 id="latest-intelligence-heading">最新发布</h2>
          <div class="latest-intelligence-list">
            <article>
              <div class="latest-intelligence-meta">
                <span>官方与机构动态</span>
                <time datetime="2026-08-11T08:00:00+08:00">发布 2026.08.11</time>
              </div>
              <h3>新设计标题一</h3><b>Design Lab</b>
              <a href="https://example.com/one">阅读原始来源</a>
            </article>
            <article>
              <div class="latest-intelligence-meta">
                <span>创作者观点</span><time>发布 2026.08.10</time>
              </div>
              <h3>新设计标题二</h3><strong>Researcher</strong>
              <button>查看证据详情</button>
            </article>
          </div>
        </section>
        """
        items = parse_latest_section_html(section_html, limit=8)
        self.assertEqual(
            [item.title for item in items],
            ["新设计标题一", "新设计标题二"],
        )
        self.assertEqual(items[0].published_at, "2026-08-11")
        self.assertEqual(items[1].source, "Researcher")

    def test_latest_section_ignores_other_sections_and_malformed_cards(self):
        section_html = """
        <div class="latest-intelligence-list">
          <article><h3>缺少发布日期</h3><b>Source</b></article>
          <article>
            <div class="latest-intelligence-meta">
              <span>官方与机构动态</span><time>发布 2026-08-11</time>
            </div>
            <h3>保留标题</h3><b>Source</b>
          </article>
          <article>
            <div class="latest-intelligence-meta">
              <span>官方与机构动态</span><time>发布 2026-08-10</time>
            </div>
            <h3>保留标题！</h3><b>Duplicate</b>
          </article>
        </div>
        <div class="cited-evidence-list">
          <article><h3>别的栏目标题</h3><time>2026-08-11</time></article>
        </div>
        """
        items = parse_latest_section_html(section_html, limit=8)
        self.assertEqual([item.title for item in items], ["保留标题"])

    def test_fixed_json_feed_matches_live_contract(self):
        payload = {
            "schema_version": 3,
            "updated_at": "2026-08-11T06:57:16.454Z",
            "section": "最新发布",
            "item_count": 2,
            "items": [
                {
                    "id": "design-1",
                    "title": "设计系统进入 Agent 时代",
                    "source_name": "Design Lab",
                    "published_at": "2026-08-11T08:00:00+08:00",
                    "url": "https://example.com/design-1",
                    "stream": "官方与机构动态",
                },
                {
                    "id": "design-2",
                    "title": "用户研究工作流的新变化",
                    "source_name": "Researcher",
                    "published_at": "2026-08-10",
                    "url": "",
                    "stream": "创作者观点",
                },
            ],
        }
        feed = parse_design_feed(payload)
        self.assertEqual(
            [item.title for item in feed.headlines],
            ["设计系统进入 Agent 时代", "用户研究工作流的新变化"],
        )
        self.assertEqual(feed.headlines[0].published_at, "2026-08-11")
        self.assertEqual(feed.updated_at_iso, "2026-08-11T06:57:16.454000Z")

    def test_json_feed_rejects_the_wrong_section(self):
        with self.assertRaises(ValueError):
            parse_design_feed(
                {
                    "schema_version": 3,
                    "updated_at": "2026-08-11T06:57:16.454Z",
                    "section": "机会洞察引用的证据",
                    "items": [],
                }
            )

    def test_json_feed_rejects_empty_items_and_count_mismatch(self):
        with self.assertRaises(ValueError):
            parse_design_feed(
                {
                    "schema_version": 3,
                    "updated_at": "2026-08-11T06:57:16.454Z",
                    "section": "最新发布",
                    "item_count": 1,
                    "items": [],
                }
            )


class FeedFreshnessAndStateTests(unittest.TestCase):
    @staticmethod
    def feed(updated_at: str, title: str = "设计标题") -> DesignFeed:
        return DesignFeed(
            updated_at=datetime.fromisoformat(updated_at.replace("Z", "+00:00")),
            headlines=(
                DesignHeadline("1", title, "A", "2026-08-11", "趋势精选"),
            ),
        )

    def test_today_feed_is_ready_without_previous_state(self):
        feed = self.feed("2026-08-11T06:57:16.454Z")
        with TemporaryDirectory() as directory:
            problem = design_feed_problem(
                feed,
                Path(directory) / "state.json",
                now=datetime(2026, 8, 11, 16, 47, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
        self.assertEqual(problem, "")

    def test_previous_day_feed_is_stale(self):
        feed = self.feed("2026-08-10T08:00:00Z")
        with TemporaryDirectory() as directory:
            problem = design_feed_problem(
                feed,
                Path(directory) / "state.json",
                now=datetime(2026, 8, 11, 16, 47, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
        self.assertIn("并非今天更新", problem)

    def test_same_date_and_titles_are_blocked_as_duplicate(self):
        feed = self.feed("2026-08-11T06:57:16.454Z")
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            save_feed_state(state_path, feed)
            problem = design_feed_problem(
                feed,
                state_path,
                now=datetime(2026, 8, 11, 16, 47, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
        self.assertIn("完全相同", problem)

    def test_changed_title_is_not_duplicate(self):
        original = self.feed("2026-08-11T06:57:16.454Z")
        changed = self.feed("2026-08-11T06:57:16.454Z", "更新后的设计标题")
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            save_feed_state(state_path, original)
            problem = design_feed_problem(
                changed,
                state_path,
                now=datetime(2026, 8, 11, 16, 47, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
        self.assertEqual(problem, "")


class InsightExtractionTests(unittest.TestCase):
    def test_extracts_final_titles_by_requested_module_and_caps_each_at_two(self):
        text = """PDF版本
AI洞察PDF：查看完整周报 (https://feishu.cn/file/file_token_123)
本周最终精选
本周核心判断
1. 判断一
2. 判断二
3. 判断三
用研与消费者洞察
标题一 (https://example.com/one)
来源：A｜2026-08-08｜用研与消费者洞察
标题二 (https://example.com/two)
来源：B｜2026-08-08｜用研与消费者洞察
标题三 (https://example.com/three)
来源：C｜2026-08-08｜用研与消费者洞察
研究工具与工作流
工具标题 (https://example.com/tool)
语音、多语言与海外研究
语音标题 (https://example.com/speech)
手机与端侧 AI
端侧标题 (https://example.com/mobile)
本周必须知道的 AI 大事
不应展示的泛AI标题 (https://example.com/major)
本周给团队的三条建议
1. 建议一
"""
        self.assertEqual(
            extract_ai_pdf_url(text),
            "https://feishu.cn/file/file_token_123",
        )
        self.assertEqual(
            extract_ai_category_titles(text),
            {
                "用研与消费者洞察": ["标题一", "标题二"],
                "研究工具与工作流": ["工具标题"],
                "语音多语言与海外研究": ["语音标题"],
                "手机与端侧 AI": ["端侧标题"],
            },
        )

    def test_daily_candidates_are_grouped_when_final_selection_is_absent(self):
        text = """2026-08-08 每日收集
候选标题一 (https://example.com/one)
来源：A｜2026-08-08｜研究工具与工作流｜综合评分 8/10
候选标题二 (https://example.com/two)
来源：B｜2026-08-08｜研究工具与工作流｜综合评分 7/10
候选标题三 (https://example.com/three)
来源：C｜2026-08-08｜研究工具与工作流｜综合评分 7/10
端侧候选 (https://example.com/mobile)
来源：D｜2026-08-08｜手机与端侧 AI｜综合评分 8/10
重大AI候选 (https://example.com/major)
来源：E｜2026-08-08｜本周必须知道的 AI 大事｜综合评分 9/10
"""
        self.assertEqual(
            extract_ai_category_titles(text),
            {
                "研究工具与工作流": ["候选标题一", "候选标题二"],
                "手机与端侧 AI": ["端侧候选"],
            },
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
            {
                "用研与消费者洞察": ["洞察标题一", "洞察标题二"],
                "手机与端侧 AI": ["端侧标题"],
            },
            "https://feishu.cn/file/report",
        )
        self.assertEqual(
            card["header"]["title"]["content"],
            "AI×设计与用户研究资讯周报｜2026.08.03–08.09",
        )
        elements = card["elements"]
        self.assertIn("AI×交互与设计资讯", elements[0]["text"]["content"])
        insight_content = elements[2]["text"]["content"]
        self.assertIn("AI×用户研究与市场洞察资讯", insight_content)
        self.assertIn("用研与消费者洞察", insight_content)
        self.assertIn("洞察标题一", insight_content)
        self.assertIn("手机与端侧 AI", insight_content)
        self.assertNotIn("研究工具与工作流", insight_content)
        self.assertNotIn("核心判断", insight_content)
        actions = elements[4]["actions"]
        self.assertEqual(
            [button["text"]["content"] for button in actions],
            ["查看AI设计完整周报", "查看AI洞察完整周报"],
        )
        self.assertEqual(actions[0]["multi_url"]["url"], DESIGN_SITE_URL)

    def test_alert_card_explains_no_official_push_and_links_json(self):
        card = build_alert_card(
            "JSON并非今天更新",
            now=datetime(2026, 8, 11, 16, 47, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        self.assertEqual(card["header"]["template"], "red")
        content = card["elements"][0]["text"]["content"]
        self.assertIn("未向正式群推送", content)
        self.assertIn("JSON并非今天更新", content)
        actions = card["elements"][1]["actions"]
        self.assertEqual(actions[0]["multi_url"]["url"], DESIGN_FEED_URL)


if __name__ == "__main__":
    unittest.main()
