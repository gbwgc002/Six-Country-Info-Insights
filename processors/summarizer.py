"""
Gemini-based summarizer for news items with translation support.
Uses Google GenAI SDK (Vertex AI) with service account authentication.
"""

import json
import os
import re
import asyncio
from pathlib import Path
from typing import Optional

from google.oauth2 import service_account
from google import genai
from google.genai import types

from collectors.base import NewsItem

# 默认 Service Account 文件路径（项目根目录下）
_DEFAULT_SA_FILE = str(Path(__file__).resolve().parent.parent / "transsion-sw-cd-6610d5d50199.json")


def is_english(text: str) -> bool:
    """检查文本是否主要是英文（或非中文）。"""
    if not text:
        return False

    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')

    if chinese_chars >= 1:
        if len(text) > 30 and (chinese_chars / len(text)) < 0.05:
            return True
        return False

    return True


def _clean_json_response(text: str) -> str:
    """清理 Gemini 返回的 JSON 文本（去除 markdown code blocks 等）。"""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


class GeminiSummarizer:
    """Use Gemini (Vertex AI) to summarize, translate and highlight key news."""

    def __init__(
        self,
        service_account_file: Optional[str] = None,
        model: str = "gemini-2.0-flash",
        project: str = "transsion-sw-cd",
        location: str = "global",
    ):
        sa_file = service_account_file or os.environ.get("GOOGLE_SA_FILE", _DEFAULT_SA_FILE)
        if not Path(sa_file).exists():
            raise FileNotFoundError(
                f"Service account file not found: {sa_file}\n"
                "Set GOOGLE_SA_FILE env var or place the JSON file in project root."
            )

        credentials = service_account.Credentials.from_service_account_file(
            sa_file,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        self.client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
            credentials=credentials,
        )
        self.model_name = model
        self.semaphore = asyncio.Semaphore(5)

    # ──────────────────────────────────────────────
    #  底层调用
    # ──────────────────────────────────────────────

    async def _call(self, prompt: str, *, json_mode: bool = False) -> str:
        """统一的 Gemini 调用入口，返回纯文本。"""
        config = types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=4096,
        )
        if json_mode:
            config.response_mime_type = "application/json"

        async with self.semaphore:
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config,
            )

        if not response or not response.candidates:
            raise RuntimeError("Gemini 未返回有效响应")

        candidate = response.candidates[0]
        if candidate.content and candidate.content.parts:
            text = candidate.content.parts[0].text
            return text.strip() if text else ""

        return ""

    # ──────────────────────────────────────────────
    #  翻译
    # ──────────────────────────────────────────────

    async def translate_to_chinese(self, text: str) -> str:
        """将英文文本翻译成中文。"""
        if not text or len(text) < 2:
            return text or ""

        prompt = f"""Translate the following text into Simplified Chinese (简体中文).

Original Text:
"{text}"

Task Instructions:
1. Translate the text into natural-sounding Simplified Chinese.
2. Keep brand names and technical terms in English (e.g., OpenAI, GPT-5, LLM, Claude, Google).
3. Do not include quotes, explanations, or original text. Return only the translated Chinese string.
"""
        try:
            result = await self._call(prompt)
            # 去掉可能的外层引号
            if (result.startswith('"') and result.endswith('"')) or \
               (result.startswith("'") and result.endswith("'")):
                result = result[1:-1].strip()
            return result
        except Exception as e:
            print(f"Translation error: {e}")
            return text

    # ──────────────────────────────────────────────
    #  核心：标题改写 + 摘要 + 相关性过滤
    # ──────────────────────────────────────────────

    async def summarize_and_translate(self, item: NewsItem) -> tuple[str, str, bool]:
        """生成摘要并翻译标题和内容。返回 (标题, 摘要, 是否已翻译)。"""
        title = item.title
        summary = item.summary or ""
        is_translated = False

        # 优先使用完整内容进行总结，取较长的那个
        raw_content = item.content if item.content and len(item.content) > len(item.summary or "") else (item.summary or "")

        # 内容质量门槛：不足80字则直接丢弃，不送给 AI
        if len(raw_content.strip()) < 80:
            print(f"   🗑️ 内容过短，丢弃: {item.title[:40]}")
            return item.title, "IRRELEVANT", False

        # 限制输入长度，避免token溢出
        if len(raw_content) > 10000:
            raw_content = raw_content[:10000] + "..."

        prompt = f"""You are a professional Chinese tech news editor. Analyze the following news item.

Title: {item.title}
Source: {item.source}
Content: {raw_content.strip()}

Task Instructions:
1. Relevance Check: Is this news primarily about Artificial Intelligence (AI), LLMs, Machine Learning, or Generative AI?
   - Return false for: General Tech without AI angle, Crypto, Blockchain, Politics, pure Science, product launches unrelated to AI.

2. Title Rewrite: Write an informative Chinese headline that captures the KEY POINT of this news.
   - MUST be in Simplified Chinese (简体中文) with Chinese characters.
   - Keep brand names and technical terms in English (e.g., OpenAI, GPT-5, LLM, Claude, Google).
   - Be SPECIFIC about WHO did WHAT: "OpenAI发布GPT-5，多模态能力全面超越前代" NOT just "GPT-5发布".
   - Target length: 20-35 characters.
   - Do NOT translate word-for-word. Write a proper informative Chinese news headline.

3. Summary: Write a high-quality summary entirely in Simplified Chinese (简体中文).
   - Length: 60-100 words covering: what happened, key details, and why it matters.
   - Do NOT simply rephrase or copy the provided content — write an original synthesis.
   - Avoid vague openers like "本文介绍了" or "这篇文章讨论了". Lead with the core news fact.
   - Full Chinese sentences only — English product names/terms (e.g. GPT-5, API) are OK inline.
   - Tone: Professional, factual, third-person news brief.

You MUST return ONLY a valid JSON object:
{{
    "is_relevant": true or false,
    "title": "Rewritten Chinese headline",
    "summary": "Chinese summary"
}}
"""

        try:
            text_response = _clean_json_response(await self._call(prompt, json_mode=True))

            try:
                data = json.loads(text_response)

                # Check relevance
                if not data.get("is_relevant", True):
                    return item.title, "IRRELEVANT", False

                json_title = data.get("title", "").strip()
                title = json_title if json_title else item.title

                summary = data.get("summary", "").strip()
                is_translated = is_english(item.title)

                title = re.sub(r'^AI[:：]\s*(YES|NO|Related).*?[:：]\s*', '', title, flags=re.IGNORECASE).strip()

                # 1. Fallback for empty or too-short summary
                if not summary or len(summary.strip()) < 5:
                    if title:
                        summary = f"{title}（点击查看详情）"
                    else:
                        summary = "暂无详细摘要，请点击标题查看原文。"

                # 2. Force translation if still English (Double Insurance)
                if is_english(summary) and len(summary) > 10:
                    try:
                        summary = await self.translate_to_chinese(summary)
                    except Exception:
                        pass

                # 3. Check TITLE for English and force translate
                if is_english(title) and len(title) >= 3:
                    try:
                        translated_title = await self.translate_to_chinese(title)
                        if translated_title and not is_english(translated_title):
                            title = translated_title
                        else:
                            print(f"   ⚠️ Title translation still English, keeping: {title[:30]}...")
                    except Exception as e:
                        print(f"   Title translation failed: {e}")

                return title, summary, is_translated

            except json.JSONDecodeError:
                print(f"JSON Parse Error for '{item.title}': {text_response[:50]}...")
                return item.title, "Summary generation failed (JSON Error)", False

        except Exception as e:
            print(f"Translate & summarize error for '{item.title[:20]}...': {e}")
            if is_english(item.title):
                try:
                    translated = await self.translate_to_chinese(item.title)
                    if translated and not is_english(translated):
                        title = translated
                        is_translated = True
                except Exception:
                    pass
            if item.summary and is_english(item.summary):
                try:
                    summary = await self.translate_to_chinese(item.summary)
                except Exception:
                    summary = item.summary
            else:
                summary = item.summary or ""

        if summary and len(summary) > 300:
            summary = summary[:297] + "..."

        return title, summary, is_translated

    # ──────────────────────────────────────────────
    #  单条摘要
    # ──────────────────────────────────────────────

    async def summarize_item(self, item: NewsItem) -> str:
        """Generate a concise summary for a single news item (Chinese content)."""
        content_to_summarize = item.content if item.content and len(item.content) > len(item.summary or "") else (item.summary or "无")

        if len(content_to_summarize) > 10000:
            content_to_summarize = content_to_summarize[:10000] + "..."

        prompt = f"""You are a professional tech news editor. Summarize the following news item.

Title: {item.title}
Source: {item.source}
Content: {content_to_summarize}

Task Instructions:
1. Relevance Check: Determine if this news is primarily about Artificial Intelligence (AI).
   - Return false for: General Tech, Crypto, Politics, Science.
2. Summarization: Write a concise summary in Simplified Chinese (简体中文).
   - Length: 50-100 words.
   - Tone: Professional news brief.

You MUST return ONLY a valid JSON object matching this schema exactly:
{{
    "is_relevant": true or false,
    "summary": "Chinese summary here"
}}
"""

        try:
            text_response = _clean_json_response(await self._call(prompt, json_mode=True))

            try:
                data = json.loads(text_response)
                if not data.get("is_relevant", True):
                    return "IRRELEVANT"
                return data.get("summary", "").strip()
            except json.JSONDecodeError:
                result = text_response.replace('```', '').strip()
                if "IRRELEVANT" in result.upper():
                    return "IRRELEVANT"
                return result

        except Exception as e:
            print(f"Summarize error: {e}")
            return item.summary or ""

    # ──────────────────────────────────────────────
    #  今日要点
    # ──────────────────────────────────────────────

    async def generate_daily_highlights(
        self,
        items_by_category: dict[str, list[NewsItem]],
        category_names: dict[str, str]
    ) -> str:
        """Generate overall daily highlights summary with HTML formatting."""

        content_parts = []
        for category, items in items_by_category.items():
            cat_name = category_names.get(category, category)
            content_parts.append(f"\n## {cat_name}")
            for item in items[:5]:
                content_parts.append(f"- {item.title} ({item.source})")

        all_content = "\n".join(content_parts)

        prompt = f"""You are an AI industry analyst. Based on the following news list, select the top 3 most important news items for today.

News List:
{all_content}

Task Instructions:
1. Selection: Select exactly 3 most impactful AI news items (major releases, funding, breakthroughs).
2. Summarization: Write a concise summary for each selected item in Simplified Chinese (简体中文).

You MUST return ONLY a valid JSON object matching this schema exactly:
{{
    "highlights": [
        "First highlight in complete Chinese sentence.",
        "Second highlight in complete Chinese sentence.",
        "Third highlight in complete Chinese sentence."
    ]
}}
"""

        try:
            text_response = _clean_json_response(await self._call(prompt, json_mode=True))

            try:
                data = json.loads(text_response)
                highlights_list = data.get("highlights", [])

                html_parts = []
                for i, highlight in enumerate(highlights_list, 1):
                    clean_highlight = re.sub(r'^(AI[:：]\s*(YES|NO|Related)|Title:|Summary:).*?[:：]\s*', '', highlight, flags=re.IGNORECASE).strip()
                    if clean_highlight:
                        html_parts.append(
                            f'<div class="highlight-item">'
                            f'<span class="highlight-number">{i}</span>'
                            f'<span class="highlight-text">{clean_highlight}</span>'
                            f'</div>'
                        )

                if html_parts:
                    return '\n'.join(html_parts)

            except json.JSONDecodeError:
                print(f"JSON Parse Error for highlights: {text_response[:50]}...")
                return self._format_highlights_html(text_response)

            return "今日AI动态收集完成，请查看下方详情。"

        except Exception as e:
            print(f"Highlights error: {e}")
            return "今日AI动态收集完成，请查看下方详情。"

    def _format_highlights_html(self, text: str) -> str:
        """将要点文本转换为HTML格式。"""
        html_parts = []

        pattern_num = r'(\d+)[.、．]\s*'
        parts_num = re.split(pattern_num, text)

        if len(parts_num) > 1:
            i = 1
            while i < len(parts_num):
                if parts_num[i].isdigit():
                    number = parts_num[i]
                    content = parts_num[i + 1].strip() if i + 1 < len(parts_num) else ""
                    if content:
                        html_parts.append(
                            f'<div class="highlight-item">'
                            f'<span class="highlight-number">{number}</span>'
                            f'<span class="highlight-text">{content}</span>'
                            f'</div>'
                        )
                    i += 2
                else:
                    i += 1
        else:
            lines = text.split('\n')
            counter = 1
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                clean_line = re.sub(r'^[-*•]\s*', '', line)
                if clean_line:
                    html_parts.append(
                        f'<div class="highlight-item">'
                        f'<span class="highlight-number">{counter}</span>'
                        f'<span class="highlight-text">{clean_line}</span>'
                        f'</div>'
                    )
                    counter += 1

        if html_parts:
            return '\n'.join(html_parts)
        else:
            return f'<div class="highlight-item"><span class="highlight-text">{text}</span></div>'

    # ──────────────────────────────────────────────
    #  批量处理
    # ──────────────────────────────────────────────

    async def process_items_with_translation(
        self,
        items: list[NewsItem],
        max_items: int = 30
    ) -> list[NewsItem]:
        """处理新闻项：翻译英文内容并生成摘要 (Parallel)."""
        tasks = []
        for item in items[:max_items]:
            tasks.append(self.summarize_and_translate(item))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed_items = []
        for i, result in enumerate(results):
            if isinstance(result, tuple):
                title, summary, is_translated = result
                item = items[i]
                item.title = title
                item.summary = summary
                item.is_translated = is_translated
                processed_items.append(item)
            elif isinstance(result, Exception):
                print(f"Error processing item {items[i].title}: {result}")
                processed_items.append(items[i])

        return processed_items

    async def process_and_filter_items(
        self,
        items: list[NewsItem],
        max_items: int = 30,
    ) -> tuple[list[NewsItem], int]:
        """
        Process items with translation and filter out irrelevant content.
        Returns (valid_items, translated_count).
        """
        print(f"🌐 Translating {len(items)} items...")

        tasks = []
        for item in items:
            tasks.append(self.summarize_and_translate(item))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid_items = []
        translated_count = 0

        for i, result in enumerate(results):
            item = items[i]

            if isinstance(result, Exception):
                print(f"   Translation error for '{item.title[:30]}...': {result}")
                valid_items.append(item)
                continue

            title, summary, is_translated = result

            if summary and "IRRELEVANT" in summary:
                print(f"   🚫 Skipping irrelevant item: {item.title}")
                continue

            if not title or not title.strip() or not summary or len(summary.strip()) < 5:
                print(f"   🚫 Skipping item with missing title/summary: {item.title[:30]}")
                continue

            item.title = title
            item.summary = summary
            item.is_translated = is_translated
            if is_translated:
                translated_count += 1

            valid_items.append(item)

        print(f"   Translated {translated_count} items (Filtered {len(items) - len(valid_items)} irrelevant)\n")
        return valid_items, translated_count

    # ──────────────────────────────────────────────
    #  语义去重
    # ──────────────────────────────────────────────

    async def semantic_deduplicate(
        self,
        categories: dict[str, list['NewsItem']],
    ) -> dict[str, list['NewsItem']]:
        """使用 Gemini 识别跨来源的相同主题新闻，保留最全面的一条。"""

        all_items: list[tuple[str, 'NewsItem']] = []
        for cat, items in categories.items():
            for item in items:
                all_items.append((cat, item))

        if len(all_items) <= 1:
            return categories

        titles_text = "\n".join(
            f"{i}: {item.title} [{item.source}]"
            for i, (_, item) in enumerate(all_items)
        )

        prompt = f"""You are a professional tech news editor. Group the following news headlines into identical topics/events.

News Headlines:
{titles_text}

Task Instructions:
1. Grouping: Identify groups of news headlines that are reporting on the exact same specific event.
2. Similarity Threshold: Only group headlines together if they are clearly about the exact same release, event, or specific announcement. If they are just generally similar topics (e.g. two different models released by different companies), do not group them.
3. If no identical events exist, return an empty array.

You MUST return ONLY a valid JSON object matching this schema exactly:
{{
    "groups": [[0, 3, 7], [2, 5]]
}}
Each sub-array should contain the index numbers of news items reporting on the same event.
"""

        try:
            text_response = _clean_json_response(await self._call(prompt, json_mode=True))

            data = json.loads(text_response)
            groups = data.get("groups", [])

            if not groups:
                return categories

            indices_to_remove: set[int] = set()
            for group in groups:
                if len(group) < 2:
                    continue
                best_idx = max(
                    group,
                    key=lambda idx: len((all_items[idx][1].content or "") + (all_items[idx][1].summary or ""))
                    if 0 <= idx < len(all_items) else 0,
                )
                for idx in group:
                    if idx != best_idx and 0 <= idx < len(all_items):
                        removed = all_items[idx][1]
                        kept = all_items[best_idx][1]
                        print(f"   🔗 去重: 移除「{removed.title[:30]}」({removed.source})，保留「{kept.title[:30]}」({kept.source})")
                        indices_to_remove.add(idx)

            new_categories: dict[str, list['NewsItem']] = {cat: [] for cat in categories}
            for i, (cat, item) in enumerate(all_items):
                if i not in indices_to_remove:
                    new_categories[cat].append(item)

            new_categories = {cat: items for cat, items in new_categories.items() if items}

            removed_count = len(indices_to_remove)
            if removed_count:
                print(f"   ✅ 语义去重完成：移除 {removed_count} 条重复新闻")

            return new_categories

        except Exception as e:
            print(f"   ⚠️ 语义去重失败（保留全部）: {e}")
            return categories

    async def batch_summarize(
        self,
        items: list[NewsItem],
        max_items: int = 20
    ) -> list[NewsItem]:
        """Batch summarize multiple items (for efficiency)."""
        return await self.process_items_with_translation(items, max_items)
