"""
Gemini-based summarizer for six-country user research insights.
Uses Google GenAI SDK (Vertex AI) with service account authentication.

Translates, summarises, filters and highlights news from
Russia, India, Indonesia, Nigeria, Kenya, Pakistan.
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

# Default service account file path (project root)
_DEFAULT_SA_FILE = ""


def is_english(text: str) -> bool:
    """Check if text is primarily English (non-Chinese)."""
    if not text:
        return False
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    if chinese_chars >= 1:
        if len(text) > 30 and (chinese_chars / len(text)) < 0.05:
            return True
        return False
    return True


def _clean_json_response(text: str) -> str:
    """Clean Gemini JSON response (strip markdown code blocks)."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


# Target countries for filtering
TARGET_COUNTRIES = [
    "Russia", "India", "Indonesia", "Nigeria", "Kenya", "Pakistan",
    "Russian", "Indian", "Indonesian", "Nigerian", "Kenyan", "Pakistani",
    "Moscow", "Delhi", "Mumbai", "Jakarta", "Lagos", "Abuja", "Nairobi",
    "Karachi", "Islamabad", "Lahore", "Kolkata", "Chennai", "Bangalore",
    "Hyderabad", "Surabaya", "Bandung", "Kano", "Mombasa", "Peshawar",
    "Africa", "African", "South Asia", "Southeast Asia",
]


class GeminiSummarizer:
    """Use Gemini (Vertex AI) to summarize, translate and highlight insights."""

    def __init__(
        self,
        service_account_file: Optional[str] = None,
        model: str = "gemini-2.0-flash",
        project: str = "",  # Set your Google Cloud project ID
        location: str = "global",
    ):
        # Resolve project from env or default
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", project)
        if not project:
            raise ValueError(
                "Google Cloud project ID not set. "
                "Set GOOGLE_CLOUD_PROJECT env var or pass project= parameter."
            )

        sa_file = service_account_file or os.environ.get("GOOGLE_SA_FILE", _DEFAULT_SA_FILE)

        # Support injecting JSON content via env (for CI/CD)
        sa_json_content = os.environ.get("GOOGLE_SA_JSON")
        if sa_json_content and (not sa_file or not Path(sa_file).exists()):
            import tempfile
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
            tmp.write(sa_json_content)
            tmp.close()
            sa_file = tmp.name

        if not sa_file or not Path(sa_file).exists():
            raise FileNotFoundError(
                f"Service account file not found: {sa_file}\n"
                "Set GOOGLE_SA_FILE or GOOGLE_SA_JSON env var, "
                "or place the JSON file in project root."
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
    #  Low-level call
    # ──────────────────────────────────────────────

    async def _call(self, prompt: str, *, json_mode: bool = False) -> str:
        """Unified Gemini call, returns plain text."""
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
            raise RuntimeError("Gemini returned no valid response")

        candidate = response.candidates[0]
        if candidate.content and candidate.content.parts:
            text = candidate.content.parts[0].text
            return text.strip() if text else ""

        return ""

    # ──────────────────────────────────────────────
    #  Translation
    # ──────────────────────────────────────────────

    async def translate_to_chinese(self, text: str) -> str:
        """Translate text to Simplified Chinese."""
        if not text or len(text) < 2:
            return text or ""

        prompt = f"""Translate the following text into Simplified Chinese (简体中文).

Original Text:
"{text}"

Task Instructions:
1. Translate into natural-sounding Simplified Chinese.
2. Keep proper nouns, brand names and technical terms in their original language (e.g., M-Pesa, Flutterwave, Jumia, TikTok, Google Play, UPI).
3. Keep country and city names in Chinese (e.g., 尼日利亚, 肯尼亚, 印度, 印尼, 巴基斯坦, 俄罗斯).
4. Return ONLY the translated Chinese string — no quotes, no explanations.
"""
        try:
            result = await self._call(prompt)
            if (result.startswith('"') and result.endswith('"')) or \
               (result.startswith("'") and result.endswith("'")):
                result = result[1:-1].strip()
            return result
        except Exception as e:
            print(f"Translation error: {e}")
            return text

    # ──────────────────────────────────────────────
    #  Core: Classify + Summarize + Translate + Content filter
    # ──────────────────────────────────────────────

    async def summarize_and_translate(self, item: NewsItem) -> tuple[str, str, bool]:
        """Generate summary and translate. Returns (title, summary, is_translated)."""
        title = item.title
        summary = item.summary or ""
        is_translated = False

        raw_content = item.content if item.content and len(item.content) > len(item.summary or "") else (item.summary or "")

        # Content quality threshold: discard items shorter than 80 chars
        if len(raw_content.strip()) < 80:
            print(f"   🗑️ Content too short, discarding: {item.title[:40]}")
            return item.title, "IRRELEVANT", False

        if len(raw_content) > 10000:
            raw_content = raw_content[:10000] + "..."

        prompt = f"""You are a professional Chinese analyst specialising in user-research insights for emerging markets.
Your job is to evaluate news from six target countries: Russia, India, Indonesia, Nigeria, Kenya, Pakistan.

Title: {item.title}
Source: {item.source}
Content: {raw_content.strip()}

═══ TASK ═══

1. **CONTENT SAFETY CHECK** (mandatory first step):
   Return is_relevant=false immediately if the content contains:
   - Sexually explicit or pornographic material
   - Graphic violence or gore
   - Extreme political propaganda or hate speech
   - Terrorism promotion
   - Content that is purely about domestic politics with no relevance to market/consumer/tech insights

2. **RELEVANCE CHECK** — Is this news useful for "desktop research insights" (洞察桌面研究)?
   Return is_relevant=true if the news relates to ANY of these dimensions for the six target countries:
   - 🏛️ Macro & Infrastructure: government policies (tariffs, app bans, regulations), 5G/telecom rollout, power grid, natural disasters, internet connectivity
   - 💰 Commerce & Economy: inflation, consumer spending, e-commerce, mobile money/fintech, retail trends, payment methods, price changes
   - 🚀 Digital Ecosystem: startup funding, app trends, Google Play dynamics, local tech companies, super apps, digital wallets
   - 🎭 Pop Culture & Sentiment: trending topics, Gen Z culture, festivals/holidays, music/film, social media trends, memes, public debates
   - 📱 Mobile Market: smartphone launches, market share, brand dynamics (Transsion/Tecno/Infinix/itel, Samsung, Xiaomi, OPPO, vivo, realme)
   Return is_relevant=false if none of the above apply.

3. **TITLE REWRITE** — Write an informative Chinese headline:
   - MUST be in Simplified Chinese (简体中文)
   - Prefix with country flag emoji: 🇷🇺🇮🇳🇮🇩🇳🇬🇰🇪🇵🇰 (or 🌍 for multi-country)
   - Be SPECIFIC: WHO did WHAT in WHERE
   - Keep brand names / proper nouns in original language
   - Target: 20-40 characters

4. **SUMMARY** — Write a concise summary in Simplified Chinese:
   - 60-120 words, covering: what happened, key details, and **why it matters for user research / product insights**
   - Lead with the core fact — no vague openers
   - Professional, factual tone

Return ONLY a valid JSON object:
{{
    "is_relevant": true or false,
    "title": "Chinese headline with country flag",
    "summary": "Chinese summary"
}}
"""

        try:
            text_response = _clean_json_response(await self._call(prompt, json_mode=True))

            try:
                data = json.loads(text_response)

                if not data.get("is_relevant", True):
                    return item.title, "IRRELEVANT", False

                json_title = data.get("title", "").strip()
                title = json_title if json_title else item.title

                summary = data.get("summary", "").strip()
                is_translated = is_english(item.title)

                title = re.sub(r'^AI[:：]\s*(YES|NO|Related).*?[:：]\s*', '', title, flags=re.IGNORECASE).strip()

                if not summary or len(summary.strip()) < 5:
                    if title:
                        summary = f"{title}（点击查看详情）"
                    else:
                        summary = "暂无详细摘要，请点击标题查看原文。"

                # Force translate if still English
                if is_english(summary) and len(summary) > 10:
                    try:
                        summary = await self.translate_to_chinese(summary)
                    except Exception:
                        pass

                if is_english(title) and len(title) >= 3:
                    try:
                        translated_title = await self.translate_to_chinese(title)
                        if translated_title and not is_english(translated_title):
                            title = translated_title
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
    #  Daily highlights
    # ──────────────────────────────────────────────

    async def generate_daily_highlights(
        self,
        items_by_category: dict[str, list[NewsItem]],
        category_names: dict[str, str]
    ) -> str:
        """Generate daily highlights with HTML formatting."""

        content_parts = []
        for category, items in items_by_category.items():
            cat_name = category_names.get(category, category)
            content_parts.append(f"\n## {cat_name}")
            for item in items[:5]:
                content_parts.append(f"- {item.title} ({item.source})")

        all_content = "\n".join(content_parts)

        prompt = f"""You are a senior user-research analyst covering six emerging markets: Russia, India, Indonesia, Nigeria, Kenya, Pakistan.

Based on the following news list, select the top 3 most important insights for product teams and user researchers today.

News List:
{all_content}

Task Instructions:
1. Select exactly 3 items that are most actionable for product/UX teams building for these markets.
2. Prioritise: infrastructure changes that affect device usage, consumer behaviour shifts, breakout apps or services, and cultural moments that reveal user needs.
3. Write each highlight as a complete sentence in Simplified Chinese (简体中文).
4. Each highlight should explain WHY it matters for user research, not just WHAT happened.

Return ONLY a valid JSON object:
{{
    "highlights": [
        "First insight in Chinese — what happened and why it matters.",
        "Second insight in Chinese.",
        "Third insight in Chinese."
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

            return "今日六国洞察收集完成，请查看下方详情。"

        except Exception as e:
            print(f"Highlights error: {e}")
            return "今日六国洞察收集完成，请查看下方详情。"

    def _format_highlights_html(self, text: str) -> str:
        """Convert highlight text to HTML format."""
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
    #  Batch processing
    # ──────────────────────────────────────────────

    async def process_and_filter_items(
        self,
        items: list[NewsItem],
        max_items: int = 30,
    ) -> tuple[list[NewsItem], int]:
        """Process items with translation, filter irrelevant content.
        Returns (valid_items, translated_count)."""
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
    #  Semantic deduplication
    # ──────────────────────────────────────────────

    async def semantic_deduplicate(
        self,
        categories: dict[str, list['NewsItem']],
    ) -> dict[str, list['NewsItem']]:
        """Use Gemini to identify cross-source duplicate stories."""

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

        prompt = f"""You are a professional news editor. Group the following headlines into identical topics/events.

News Headlines:
{titles_text}

Task Instructions:
1. Identify groups of headlines reporting on the EXACT SAME specific event.
2. Only group if they are clearly about the same release, event, or announcement.
3. If no identical events exist, return an empty array.

Return ONLY a valid JSON object:
{{
    "groups": [[0, 3, 7], [2, 5]]
}}
Each sub-array contains index numbers of news items about the same event.
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
                        print(f"   🔗 Dedup: removed「{removed.title[:30]}」({removed.source}), kept「{kept.title[:30]}」({kept.source})")
                        indices_to_remove.add(idx)

            new_categories: dict[str, list['NewsItem']] = {cat: [] for cat in categories}
            for i, (cat, item) in enumerate(all_items):
                if i not in indices_to_remove:
                    new_categories[cat].append(item)

            new_categories = {cat: items for cat, items in new_categories.items() if items}

            removed_count = len(indices_to_remove)
            if removed_count:
                print(f"   ✅ Semantic dedup done: removed {removed_count} duplicates")

            return new_categories

        except Exception as e:
            print(f"   ⚠️ Semantic dedup failed (keeping all): {e}")
            return categories

    async def batch_summarize(
        self,
        items: list[NewsItem],
        max_items: int = 20
    ) -> list[NewsItem]:
        """Batch summarize multiple items."""
        valid, _ = await self.process_and_filter_items(items, max_items)
        return valid
