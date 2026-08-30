import json
import re
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import yaml

from ai_insights import (
    filter_recent_candidates,
    get_week_period,
    render_highlights_html,
    render_daily_section,
)
from collectors.base import NewsItem
from collectors.rss_collector import RSSCollector
from email_sender import EmailSender
from processors.ai_insights_summarizer import (
    ScoredInsight,
    WeeklyDigest,
    WeeklyDigestItem,
)
from processors.summarizer import GeminiSummarizer
from publishers.feishu_publisher import FeishuPublisher

ROOT = Path(__file__).resolve().parents[1]


class WeekPeriodTests(unittest.TestCase):
    def test_current_and_previous_natural_week(self):
        now = datetime(2026, 7, 27, 7, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        current = get_week_period(now)
        previous = get_week_period(now, previous=True)

        self.assertEqual(str(current.start), "2026-07-27")
        self.assertEqual(str(current.end), "2026-08-02")
        self.assertEqual(current.title, "AI洞察资讯周报｜2026.07.27–08.02")
        self.assertEqual(str(previous.start), "2026-07-20")
        self.assertEqual(str(previous.end), "2026-07-26")


class CandidateFilteringTests(unittest.TestCase):
    def test_recent_filter_deduplicates_and_excludes_old_items(self):
        now = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
        recent = NewsItem(
            title="Recent",
            url="https://example.com/recent",
            source="Source",
            category="major_ai",
            published=now - timedelta(hours=4),
        )
        duplicate = NewsItem(
            title="Recent duplicate",
            url="https://example.com/recent",
            source="Other",
            category="major_ai",
            published=now - timedelta(hours=2),
        )
        old = NewsItem(
            title="Old",
            url="https://example.com/old",
            source="Source",
            category="major_ai",
            published=now - timedelta(days=4),
        )

        result = filter_recent_candidates(
            [recent, duplicate, old],
            lookback_hours=48,
            now=now,
        )
        self.assertEqual([item.url for item in result], [recent.url])

    def test_daily_markdown_keeps_evidence_link(self):
        item = NewsItem(
            title="Original",
            url="https://example.com/source",
            source="Research Lab",
            category="human_ai",
            published=datetime(2026, 7, 26, tzinfo=timezone.utc),
        )
        insight = ScoredInsight(
            item=item,
            title_cn="AI 交互研究",
            category="human_ai",
            relevance_score=9,
            actionability_score=8,
            impact_score=7,
            credibility_score=9,
            summary_cn="研究发布。",
            why_it_matters="可用于评估 Agent。",
            action_hint="加入可控性指标。",
        )
        markdown = render_daily_section(
            [insight],
            local_date=date(2026, 7, 27),
        )
        self.assertIn("https://example.com/source", markdown)
        self.assertIn("为什么值得关注", markdown)
        self.assertIn("建议动作", markdown)


class SourceIsolationTests(unittest.TestCase):
    def test_ai_sources_are_in_separate_configuration(self):
        with (ROOT / "config" / "sources.yaml").open(encoding="utf-8") as file:
            six_country = yaml.safe_load(file)
        with (ROOT / "config" / "ai_insights_sources.yaml").open(
            encoding="utf-8"
        ) as file:
            ai_insights = yaml.safe_load(file)

        self.assertIn("moscow_times", six_country["rss_sources"])
        self.assertNotIn("nngroup", six_country["rss_sources"])
        self.assertIn("nngroup", ai_insights["rss_sources"])
        self.assertEqual(
            GeminiSummarizer.DEFAULT_MODEL,
            "gemini-3.6-flash",
        )

    def test_required_keyword_group_is_anded_with_primary_group(self):
        collector = RSSCollector(
            "phone_ai",
            {
                "name": "Phone AI",
                "url": "https://example.com/feed",
                "keywords": ["OPPO", "vivo"],
                "require_keywords": ["AI", "agent"],
            },
        )
        text = "OPPO launches a new AI agent"
        self.assertTrue(
            collector.filter_by_keywords(text, collector.keywords)
            and collector.filter_by_required_keywords(
                text,
                collector.require_keywords,
            )
        )
        text_without_ai = "OPPO launches a new color"
        self.assertFalse(
            collector.filter_by_keywords(text_without_ai, collector.keywords)
            and collector.filter_by_required_keywords(
                text_without_ai,
                collector.require_keywords,
            )
        )


class FeishuTests(unittest.TestCase):
    def test_block_text_preserves_link_target(self):
        block = {
            "block_type": 2,
            "text": {
                "elements": [
                    {
                        "text_run": {
                            "content": "原始来源",
                            "text_element_style": {
                                "link": {"url": "https://example.com/article"}
                            },
                        }
                    }
                ]
            },
        }
        text = FeishuPublisher._extract_block_text(block)
        self.assertEqual(
            text,
            "原始来源 (https://example.com/article)",
        )


class FeishuSendFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_feishu_business_error_fails_the_workflow(self):
        class FakeResponse:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def json(self):
                return {"code": 999, "msg": "simulated failure"}

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def post(self, *args, **kwargs):
                return FakeResponse()

        publisher = FeishuPublisher()

        async def fake_token():
            return "test-token"

        publisher._get_tenant_access_token = fake_token
        with patch(
            "publishers.feishu_publisher.aiohttp.ClientSession",
            return_value=FakeSession(),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated failure"):
                await publisher._send_message(
                    "oc_" + "testgroup123",
                    "interactive",
                    "{}",
                )


class WeeklyReportTests(unittest.TestCase):
    def _digest(self):
        return WeeklyDigest(
            core_judgments=["AI 用研进入工作流执行阶段"],
            items=[
                WeeklyDigestItem(
                    title="AI 研究平台更新",
                    url="https://example.com/research",
                    source="Research Lab",
                    published="2026-07-26",
                    category="user_research",
                    what_happened="平台新增研究执行能力。",
                    why_it_matters="可减少重复工作。",
                    action_hint="先在低风险环节试点。",
                )
            ],
            team_advice=["关键执行操作保留人工确认。"],
        )

    def test_structured_digest_drives_markdown_and_pdf_data(self):
        digest = self._digest()
        markdown = digest.to_markdown()
        categories = digest.to_report_categories()
        card_categories = digest.card_category_titles()

        self.assertIn("## 本周最终精选", markdown)
        self.assertIn("https://example.com/research", markdown)
        self.assertIn("user_research", categories)
        self.assertEqual(categories["user_research"][0].source, "Research Lab")
        self.assertEqual(
            card_categories,
            {"用研与消费者洞察": ["AI 研究平台更新"]},
        )

    def test_weekly_card_uses_new_title_core_judgments_and_five_modules(self):
        card = json.loads(
            FeishuPublisher()._build_ai_insights_card(
                highlights="- 判断一\n- 判断二",
                categories={
                    "用研与消费者洞察": ["标题一", "标题二", "标题三"],
                    "研究工具与工作流": ["工具标题"],
                    "人机交互与研究方法": [],
                    "语音多语言与海外研究": ["语音标题"],
                    "手机与端侧 AI": ["端侧标题"],
                },
                doc_url="https://feishu.cn/file/report",
            )
        )
        self.assertEqual(
            card["header"]["title"]["content"],
            "AI×用户研究与市场洞察资讯",
        )
        contents = "\n".join(
            element.get("text", {}).get("content", "")
            for element in card["elements"]
        )
        self.assertIn("**核心判断**", contents)
        self.assertIn("用研与消费者洞察", contents)
        self.assertIn("标题一", contents)
        self.assertIn("标题二", contents)
        self.assertNotIn("标题三", contents)
        self.assertNotIn("人机交互与研究方法", contents)
        self.assertIn("语音多语言与海外研究", contents)
        self.assertIn("手机与端侧 AI", contents)

    def test_ai_report_reuses_six_country_template_with_ai_labels(self):
        digest = self._digest()
        renderer = EmailSender()
        report_html = renderer.render_email(
            categories=digest.to_report_categories(),
            category_names={"user_research": "用研与消费者洞察"},
            highlights=render_highlights_html(digest.core_judgments),
            date_label="2026年07月20日 至 2026年07月26日",
            report_title="AI洞察资讯周报",
            report_subtitle="AI Insights",
            highlights_title="本周核心判断",
            toc_title="本期目录",
            recommendations=digest.team_advice,
            recommendations_title="本周给团队的三条建议",
        )
        self.assertIn("AI洞察资讯周报", report_html)
        self.assertIn("本周核心判断", report_html)
        self.assertIn("本周给团队的三条建议", report_html)
        self.assertIn("AI 研究平台更新", report_html)

    def test_seven_country_template_defaults_are_current(self):
        report_html = EmailSender().render_email(
            categories={},
            category_names={},
        )
        self.assertIn("<h1>🔍 七国用研洞察</h1>", report_html)
        self.assertIn("⚡ 今日要点", (ROOT / "templates" / "email.html").read_text())
        self.assertIn(
            "🇷🇺 EE1 · 🇮🇳 India · 🇮🇩 Indonesia",
            report_html,
        )
        self.assertIn("🇧🇩 Bangladesh", report_html)


class WorkflowTests(unittest.TestCase):
    def test_schedules_and_commands(self):
        daily = (ROOT / ".github" / "workflows" / "ai-insights-daily.yml").read_text(
            encoding="utf-8"
        )
        weekly = (ROOT / ".github" / "workflows" / "ai-insights-weekly.yml").read_text(
            encoding="utf-8"
        )
        six_country = (ROOT / ".github" / "workflows" / "daily-digest.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('cron: "30 23 * * *"', daily)
        self.assertIn("python ai_insights.py collect", daily)
        self.assertIn('cron: "47 8 * * 1"', weekly)
        self.assertIn("python ai_insights.py publish", weekly)
        self.assertIn("fonts-noto-cjk", weekly)
        self.assertIn("cron: '25 22 * * *'", six_country)
        self.assertIn("python main.py", six_country)
        self.assertIn("GEMINI_MODEL: gemini-3.6-flash", six_country)

    def test_workflows_use_named_group_secrets_without_plaintext_chat_ids(self):
        workflows = {
            path.name: path.read_text(encoding="utf-8")
            for path in (ROOT / ".github" / "workflows").glob("*.yml")
        }
        software_group_secret = "secrets.FEISHU_GROUP_RUANJIANYONGYAN_ID"
        self.assertIn(software_group_secret, workflows["daily-digest.yml"])
        self.assertIn(software_group_secret, workflows["ai-insights-daily.yml"])
        self.assertIn(software_group_secret, workflows["ai-insights-weekly.yml"])

        combined = workflows["ai-design-combined-weekly.yml"]
        self.assertIn(
            "secrets.FEISHU_GROUP_SWYONGHUTIYANBU_ID",
            combined,
        )
        self.assertIn(
            "secrets.FEISHU_GROUP_AI2DZUOYECESHIQUN_ID",
            combined,
        )

        all_workflows = "\n".join(workflows.values())
        self.assertNotIn("secrets.FEISHU_BOT_CHAT_ID", all_workflows)
        self.assertIsNone(
            re.search(r"oc_[A-Za-z0-9]{10,}", all_workflows)
        )


if __name__ == "__main__":
    unittest.main()
