#!/usr/bin/env python3
"""Send one weekly Feishu card containing AI design and AI insight news."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv


load_dotenv()

DESIGN_SITE_URL = (
    "https://ai-intel-news-hub.woodsy-crown-9414.chatgpt.site/"
)
DESIGN_SITE_HOST = urlparse(DESIGN_SITE_URL).hostname
DESIGN_FEED_URL = urljoin(DESIGN_SITE_URL, "api/rankings/latest.json")
DEFAULT_ITEM_LIMIT = 8
MAX_ASSET_BYTES = 8_000_000
USER_AGENT = "Six-Country-Info-Insights/1.0"
DEFAULT_ALERT_CHAT_ID = "oc_d4411bf497444ec7d818d18b3738773d"
DEFAULT_STATE_PATH = ".cache/ai-design-feed/last-success.json"
AI_INSIGHT_CATEGORY_LIMIT = 2
AI_INSIGHT_CATEGORIES = (
    ("用研与消费者洞察", ("用研与消费者洞察",)),
    ("研究工具与工作流", ("研究工具与工作流",)),
    ("人机交互与研究方法", ("人机交互与研究方法",)),
    (
        "语音多语言与海外研究",
        ("语音、多语言与海外研究", "语音多语言与海外研究"),
    ),
    ("手机与端侧 AI", ("手机与端侧 AI", "手机与端侧AI")),
)


@dataclass(frozen=True)
class WeekPeriod:
    start: date
    end: date

    @property
    def label(self) -> str:
        return f"{self.start:%Y.%m.%d}–{self.end:%m.%d}"

    @property
    def ai_report_title(self) -> str:
        return f"AI洞察资讯周报｜{self.label}"

    @property
    def card_title(self) -> str:
        return f"AI×设计与用户研究资讯周报｜{self.label}"


@dataclass(frozen=True)
class DesignHeadline:
    id: str
    title: str
    source: str
    published_at: str
    stream: str


@dataclass(frozen=True)
class DesignFeed:
    updated_at: datetime
    headlines: tuple[DesignHeadline, ...]

    @property
    def updated_at_iso(self) -> str:
        return self.updated_at.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )

    @property
    def signature(self) -> str:
        payload = {
            "updated_at": self.updated_at_iso,
            "titles": [item.title for item in self.headlines],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def get_previous_week(
    now: datetime | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> WeekPeriod:
    zone = ZoneInfo(timezone_name)
    current = now or datetime.now(tz=zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    local_date = current.astimezone(zone).date()
    monday = local_date - timedelta(days=local_date.weekday() + 7)
    return WeekPeriod(monday, monday + timedelta(days=6))


def _validated_site_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != DESIGN_SITE_HOST
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"Unexpected design-site asset URL: {url}")
    return url


async def _fetch_text(
    session: aiohttp.ClientSession,
    url: str,
) -> str:
    target = _validated_site_url(url)
    async with session.get(target, allow_redirects=True) as response:
        if response.status != 200:
            raise RuntimeError(
                f"Design site returned HTTP {response.status} for {target}"
            )
        final_url = _validated_site_url(str(response.url))
        body = await response.read()
        if len(body) > MAX_ASSET_BYTES:
            raise RuntimeError(f"Design-site asset is too large: {final_url}")
        return body.decode(response.charset or "utf-8", errors="replace")


def _main_asset_url(home_html: str) -> str:
    soup = BeautifulSoup(home_html, "html.parser")
    candidates = [
        tag.get("src", "")
        for tag in soup.find_all("script", src=True)
        if tag.get("type") == "module"
    ]
    for source in candidates:
        if source.endswith(".js"):
            return _validated_site_url(urljoin(DESIGN_SITE_URL, source))
    raise RuntimeError("The design site exposes no module JavaScript asset")


def _fallback_asset_urls(main_js: str) -> tuple[str, str]:
    matches = re.findall(
        r'import\(["\'](\./(?:data|hotData)-[^"\']+\.js)["\']\)',
        main_js,
    )
    data_path = next(
        (path for path in matches if path.rsplit("/", 1)[-1].startswith("data-")),
        None,
    )
    hot_path = next(
        (
            path
            for path in matches
            if path.rsplit("/", 1)[-1].startswith("hotData-")
        ),
        None,
    )
    if not data_path or not hot_path:
        raise RuntimeError("Unable to discover the design site's fallback data")
    assets_base = urljoin(DESIGN_SITE_URL, "assets/")
    return (
        _validated_site_url(urljoin(assets_base, data_path.removeprefix("./"))),
        _validated_site_url(urljoin(assets_base, hot_path.removeprefix("./"))),
    )


def _skip_js_string(source: str, start: int) -> int:
    quote = source[start]
    index = start + 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    raise ValueError("Unterminated JavaScript string")


def _extract_balanced(
    source: str,
    start: int,
    opener: str,
    closer: str,
) -> str:
    if start >= len(source) or source[start] != opener:
        raise ValueError(f"Expected {opener!r} at offset {start}")
    depth = 0
    index = start
    while index < len(source):
        char = source[index]
        if char in ('"', "'", "`"):
            index = _skip_js_string(source, index)
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
        index += 1
    raise ValueError(f"Unbalanced JavaScript {opener}{closer} literal")


def _find_array_literal(source: str, key: str) -> str:
    match = re.search(rf"(?<![\w$]){re.escape(key)}\s*:\s*\[", source)
    if not match:
        return "[]"
    start = source.find("[", match.start())
    return _extract_balanced(source, start, "[", "]")


def _iter_object_literals(array_literal: str) -> Iterable[str]:
    index = 1
    square_depth = 1
    while index < len(array_literal) - 1:
        char = array_literal[index]
        if char in ('"', "'", "`"):
            index = _skip_js_string(array_literal, index)
            continue
        if char == "[":
            square_depth += 1
        elif char == "]":
            square_depth -= 1
        elif char == "{" and square_depth == 1:
            literal = _extract_balanced(array_literal, index, "{", "}")
            yield literal
            index += len(literal)
            continue
        index += 1


def _top_level_parts(source: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depths = {"{": 0, "[": 0, "(": 0}
    closing = {"}": "{", "]": "[", ")": "("}
    index = 0
    while index < len(source):
        char = source[index]
        if char in ('"', "'", "`"):
            index = _skip_js_string(source, index)
            continue
        if char in depths:
            depths[char] += 1
        elif char in closing:
            depths[closing[char]] -= 1
        elif char == delimiter and not any(depths.values()):
            parts.append(source[start:index])
            start = index + 1
        index += 1
    parts.append(source[start:])
    return parts


def _decode_js_string(raw: str) -> str:
    value = raw.strip()
    if len(value) < 2 or value[0] not in ('"', "'", "`"):
        return ""
    quote = value[0]
    if value[-1] != quote:
        return ""
    body = value[1:-1]
    output: list[str] = []
    index = 0
    escapes = {
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "0": "\0",
    }
    while index < len(body):
        char = body[index]
        if char != "\\":
            output.append(char)
            index += 1
            continue
        index += 1
        if index >= len(body):
            output.append("\\")
            break
        escaped = body[index]
        if escaped in escapes:
            output.append(escapes[escaped])
            index += 1
        elif escaped == "x" and re.match(r"^[0-9a-fA-F]{2}", body[index + 1 :]):
            output.append(chr(int(body[index + 1 : index + 3], 16)))
            index += 3
        elif escaped == "u" and body[index + 1 : index + 2] == "{":
            end = body.find("}", index + 2)
            if end == -1:
                output.append("u")
                index += 1
            else:
                output.append(chr(int(body[index + 2 : end], 16)))
                index = end + 1
        elif escaped == "u" and re.match(r"^[0-9a-fA-F]{4}", body[index + 1 :]):
            output.append(chr(int(body[index + 1 : index + 5], 16)))
            index += 5
        elif escaped in ("\n", "\r"):
            index += 1
        else:
            output.append(escaped)
            index += 1
    return "".join(output)


def _object_fields(object_literal: str) -> dict[str, str]:
    body = object_literal.strip()[1:-1]
    fields: dict[str, str] = {}
    for part in _top_level_parts(body, ","):
        colon_parts = _top_level_parts(part, ":")
        if len(colon_parts) < 2:
            continue
        raw_key = colon_parts[0].strip()
        raw_value = part[part.find(":") + 1 :].strip()
        key = (
            _decode_js_string(raw_key)
            if raw_key[:1] in ('"', "'", "`")
            else raw_key
        )
        if re.fullmatch(r"[A-Za-z_$][\w$]*", key or ""):
            fields[key] = raw_value
    return fields


def _string_field(fields: dict[str, str], key: str) -> str:
    return _decode_js_string(fields.get(key, ""))


def _first_array_string(raw: str) -> str:
    match = re.search(r'["\'`]', raw or "")
    if not match:
        return ""
    end = _skip_js_string(raw, match.start())
    return _decode_js_string(raw[match.start() : end])


def _parse_string_array(raw: str) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(raw):
        if raw[index] in ('"', "'", "`"):
            end = _skip_js_string(raw, index)
            values.append(_decode_js_string(raw[index:end]))
            index = end
        else:
            index += 1
    return values


def _source_groups(main_js: str) -> tuple[set[str], set[str]]:
    match = re.search(r"KW=(\[[^\]]*\]),HW=(\[[^\]]*\])", main_js)
    if not match:
        raise RuntimeError("Unable to identify design-site source groups")
    return set(_parse_string_array(match.group(1))), set(
        _parse_string_array(match.group(2))
    )


def _translated_title(main_js: str, content_url: str) -> str:
    if not content_url:
        return ""
    key = f'"url:{content_url}":'
    search_from = 0
    translated = ""
    while True:
        index = main_js.find(key, search_from)
        if index == -1:
            return translated
        object_start = main_js.find("{", index + len(key))
        if object_start == -1:
            return translated
        try:
            fields = _object_fields(
                _extract_balanced(main_js, object_start, "{", "}")
            )
        except ValueError:
            return translated
        translated = _string_field(fields, "title_zh") or translated
        search_from = object_start + 1


def _timestamp(fields: dict[str, str]) -> str:
    for key in (
        "publishedAt",
        "published_at",
        "updatedAt",
        "updated_at",
        "latest_at",
        "reviewedAt",
        "captured_at",
        "organizedAt",
    ):
        value = _string_field(fields, key)
        if value:
            return value
    return ""


def _parse_news_items(data_js: str, main_js: str) -> list[DesignHeadline]:
    creator_sources, official_sources = _source_groups(main_js)
    headlines: list[DesignHeadline] = []
    for literal in _iter_object_literals(_find_array_literal(data_js, "items")):
        fields = _object_fields(literal)
        source_slug = _string_field(fields, "source_slug")
        if source_slug in creator_sources:
            stream = "创作者观点"
        elif source_slug in official_sources:
            stream = "官方与机构动态"
        else:
            continue
        content_url = _string_field(fields, "content_url")
        published_at = _timestamp(fields)
        title = (
            _translated_title(main_js, content_url)
            or _string_field(fields, "title_zh")
            or _string_field(fields, "title")
        )
        if not title or not published_at or not content_url.startswith("https://"):
            continue
        headlines.append(
            DesignHeadline(
                id=f"{stream}:{_string_field(fields, 'id') or content_url}",
                title=title,
                source=_string_field(fields, "source_name") or "公开信息源",
                published_at=published_at,
                stream=stream,
            )
        )
    return headlines


def _hot_item_available(fields: dict[str, str]) -> bool:
    content_url = _string_field(fields, "content_url")
    if content_url.startswith("https://"):
        return True
    evidence = fields.get("evidence_items", "")
    if not evidence.startswith("["):
        return False
    return sum(1 for _ in _iter_object_literals(evidence)) >= 2


def _parse_hot_items(hot_js: str) -> list[DesignHeadline]:
    arrays = (
        ("items", "实时热点"),
        ("workbench_topics", "趋势研判"),
        ("research_items", "MIT 周度研究"),
        ("selected_items", "趋势精选"),
    )
    headlines: list[DesignHeadline] = []
    for key, stream in arrays:
        for literal in _iter_object_literals(_find_array_literal(hot_js, key)):
            fields = _object_fields(literal)
            title = _string_field(fields, "title_zh") or _string_field(
                fields, "title"
            )
            published_at = _timestamp(fields)
            if not title or not published_at or not _hot_item_available(fields):
                continue
            source = (
                _first_array_string(fields.get("source_names", ""))
                or _string_field(fields, "source_name")
                or _string_field(fields, "source")
                or "公开信息源"
            )
            item_id = _string_field(fields, "id") or f"{title}:{published_at}"
            headlines.append(
                DesignHeadline(
                    id=f"{stream}:{item_id}",
                    title=title,
                    source=source,
                    published_at=published_at,
                    stream=stream,
                )
            )
    return headlines


def _normalized_title(title: str) -> str:
    return re.sub(r"[^\w]+", "", title.casefold(), flags=re.UNICODE)


def _title_similarity(left: str, right: str) -> float:
    def bigrams(value: str) -> set[str]:
        return (
            {value}
            if len(value) < 2
            else {value[index : index + 2] for index in range(len(value) - 1)}
        )

    left_set = bigrams(left)
    right_set = bigrams(right)
    if not left_set or not right_set:
        return 0.0
    return 2 * len(left_set & right_set) / (len(left_set) + len(right_set))


def _sortable_timestamp(value: str) -> float:
    candidate = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).timestamp()
    except ValueError:
        return 0.0


def select_latest(
    headlines: Iterable[DesignHeadline],
    limit: int = DEFAULT_ITEM_LIMIT,
) -> list[DesignHeadline]:
    selected: list[DesignHeadline] = []
    normalized: list[str] = []
    ordered = sorted(
        headlines,
        key=lambda item: (_sortable_timestamp(item.published_at), item.id),
        reverse=True,
    )
    for item in ordered:
        title_key = _normalized_title(item.title)
        if not title_key:
            continue
        if any(
            title_key == existing
            or _title_similarity(existing, title_key) >= 0.72
            for existing in normalized
        ):
            continue
        selected.append(item)
        normalized.append(title_key)
        if len(selected) >= limit:
            break
    return selected


def parse_latest_section_html(
    section_html: str,
    limit: int = DEFAULT_ITEM_LIMIT,
) -> list[DesignHeadline]:
    """Parse the cards visibly rendered under the site's `最新发布` heading.

    The site is a client-rendered application whose bundle names, minified
    variables, and data packaging can change on every deployment.  The
    section's semantic heading and rendered card fields are the stable public
    contract, so runtime scraping deliberately depends only on those.
    """
    soup = BeautifulSoup(section_html or "", "html.parser")
    articles = soup.select(".latest-intelligence-list article")
    if not articles:
        articles = soup.select("article")

    headlines: list[DesignHeadline] = []
    normalized_titles: list[str] = []
    for index, article in enumerate(articles, start=1):
        title_node = article.find("h3")
        meta_node = article.select_one(".latest-intelligence-meta")
        stream_node = meta_node.find("span") if meta_node else None
        time_node = meta_node.find("time") if meta_node else article.find("time")
        source_node = article.find("b") or article.find("strong")
        link_node = article.find("a", href=True)

        title = title_node.get_text(" ", strip=True) if title_node else ""
        stream = (
            stream_node.get_text(" ", strip=True)
            if stream_node
            else "AI设计资讯"
        )
        source = (
            source_node.get_text(" ", strip=True)
            if source_node
            else "公开信息源"
        )
        published_at = ""
        if time_node:
            published_at = (
                time_node.get("datetime", "")
                or time_node.get_text(" ", strip=True)
            )
        date_match = re.search(
            r"\d{4}[.-]\d{2}[.-]\d{2}",
            published_at,
        )
        if date_match:
            published_at = date_match.group(0).replace(".", "-")

        if not title or not published_at:
            continue
        title_key = _normalized_title(title)
        if not title_key or title_key in normalized_titles:
            continue

        external_url = link_node.get("href", "") if link_node else ""
        item_id = external_url or f"rendered:{index}:{title_key}"
        headlines.append(
            DesignHeadline(
                id=item_id,
                title=title,
                source=source,
                published_at=published_at,
                stream=stream,
            )
        )
        normalized_titles.append(title_key)
        if len(headlines) >= limit:
            break
    return headlines


def parse_design_feed(
    payload: object,
    limit: int = DEFAULT_ITEM_LIMIT,
) -> DesignFeed:
    """Validate the fixed machine-readable `最新发布` JSON feed."""
    if not isinstance(payload, dict):
        raise ValueError("Design feed must be a JSON object")
    if payload.get("section") != "最新发布":
        raise ValueError("Design feed section must be 最新发布")
    if payload.get("schema_version") != 3:
        raise ValueError("Unsupported design feed schema version")
    updated_at = _parse_iso_datetime(str(payload.get("updated_at", "")))
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("Design feed items must be an array")
    declared_count = payload.get("item_count")
    if declared_count is not None and declared_count != len(raw_items):
        raise ValueError("Design feed item_count does not match items")

    headlines: list[DesignHeadline] = []
    seen_titles: set[str] = set()
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise ValueError(f"Design feed item {index} must be an object")
        title = str(raw_item.get("title", "")).strip()
        source = str(raw_item.get("source_name", "")).strip() or "公开信息源"
        published_at = str(raw_item.get("published_at", "")).strip()
        stream = str(raw_item.get("stream", "")).strip() or "AI设计资讯"
        external_url = str(raw_item.get("url", "")).strip()
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", published_at)
        if not title or not date_match:
            raise ValueError(
                f"Design feed item {index} requires title and ISO published_at"
            )
        if external_url:
            parsed_url = urlparse(external_url)
            if (
                parsed_url.scheme != "https"
                or parsed_url.username
                or parsed_url.password
            ):
                raise ValueError(f"Design feed item {index} has an invalid URL")

        title_key = _normalized_title(title)
        if not title_key or title_key in seen_titles:
            continue
        headlines.append(
            DesignHeadline(
                id=str(raw_item.get("id", "")).strip()
                or external_url
                or f"feed:{index}:{title_key}",
                title=title,
                source=source,
                published_at=date_match.group(0),
                stream=stream,
            )
        )
        seen_titles.add(title_key)
        if len(headlines) >= limit:
            break
    if not headlines:
        raise ValueError("Design feed contains no usable titles")
    return DesignFeed(updated_at=updated_at, headlines=tuple(headlines))


def _parse_iso_datetime(value: str) -> datetime:
    candidate = (value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("Design feed updated_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("Design feed updated_at must include a timezone")
    return parsed


def design_feed_problem(
    feed: DesignFeed,
    state_path: str | Path,
    now: datetime | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> str:
    """Return an alert reason when the feed is stale or already pushed."""
    zone = ZoneInfo(timezone_name)
    current = now or datetime.now(tz=zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    current = current.astimezone(zone)
    updated = feed.updated_at.astimezone(zone)
    if updated > current + timedelta(minutes=10):
        return f"JSON更新时间异常，晚于当前时间：{updated:%Y-%m-%d %H:%M}"
    if updated.date() != current.date():
        return (
            "JSON并非今天更新："
            f"更新时间为{updated:%Y-%m-%d %H:%M}，"
            f"当前日期为{current:%Y-%m-%d}"
        )

    previous = load_feed_state(state_path)
    if previous.get("signature") == feed.signature:
        return (
            "JSON日期和标题内容与上次成功推送完全相同："
            f"{updated:%Y-%m-%d %H:%M}"
        )
    return ""


def load_feed_state(state_path: str | Path) -> dict:
    path = Path(state_path)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_feed_state(state_path: str | Path, feed: DesignFeed) -> None:
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "signature": feed.signature,
        "updated_at": feed.updated_at_iso,
        "titles": [item.title for item in feed.headlines],
        "saved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


async def fetch_design_feed(
    limit: int = DEFAULT_ITEM_LIMIT,
) -> DesignFeed:
    """Read the fixed JSON feed; failures must alert instead of scraping HTML."""
    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
        trust_env=True,
    ) as session:
        async with session.get(DESIGN_FEED_URL, allow_redirects=True) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"Design feed returned HTTP {response.status}"
                )
            content_type = response.headers.get("Content-Type", "")
            body = await response.read()
            if len(body) > MAX_ASSET_BYTES:
                raise RuntimeError("Design feed is too large")
            if "json" not in content_type.casefold():
                raise RuntimeError("Design feed response is not JSON")
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Design feed is not valid UTF-8 JSON") from exc
    return parse_design_feed(payload, limit)


async def fetch_design_headlines(
    limit: int = DEFAULT_ITEM_LIMIT,
) -> DesignFeed:
    return await fetch_design_feed(limit)


def _clean_document_line(raw_line: str) -> str:
    return re.sub(r"^#{1,6}\s*", "", (raw_line or "").strip()).strip()


def _linked_title(line: str) -> str:
    cleaned = _clean_document_line(line)
    for pattern in (
        r"^\[(.+?)\]\(https?://[^\s)]+\)$",
        r"^(.+?)\s+\(https?://[^\s)]+\)$",
    ):
        match = re.match(pattern, cleaned)
        if match:
            return match.group(1).strip()
    return ""


def _category_aliases() -> dict[str, str]:
    return {
        alias: display_name
        for display_name, aliases in AI_INSIGHT_CATEGORIES
        for alias in aliases
    }


def _append_category_title(
    categories: dict[str, list[str]],
    category: str,
    title: str,
    limit: int,
) -> None:
    items = categories[category]
    normalized = _normalized_title(title)
    if (
        not normalized
        or len(items) >= limit
        or any(_normalized_title(existing) == normalized for existing in items)
    ):
        return
    items.append(title)


def _nonempty_categories(
    categories: dict[str, list[str]],
) -> dict[str, list[str]]:
    return {
        display_name: categories[display_name]
        for display_name, _ in AI_INSIGHT_CATEGORIES
        if categories[display_name]
    }


def _extract_final_ai_category_titles(
    lines: list[str],
    limit: int,
) -> dict[str, list[str]]:
    categories = {display_name: [] for display_name, _ in AI_INSIGHT_CATEGORIES}
    aliases = _category_aliases()
    try:
        start = lines.index("本周最终精选") + 1
    except ValueError:
        return {}

    current_category = ""
    for line in lines[start:]:
        if line == "本周给团队的三条建议" or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}\s+每日收集",
            line,
        ):
            break
        if line in aliases:
            current_category = aliases[line]
            continue
        if line in {"本周核心判断", "本周必须知道的 AI 大事"}:
            current_category = ""
            continue
        title = _linked_title(line)
        if current_category and title:
            _append_category_title(
                categories,
                current_category,
                title,
                limit,
            )
    return _nonempty_categories(categories)


def _extract_candidate_ai_category_titles(
    lines: list[str],
    limit: int,
) -> dict[str, list[str]]:
    """Group daily candidates when the final weekly selection is not ready."""
    categories = {display_name: [] for display_name, _ in AI_INSIGHT_CATEGORIES}
    aliases = _category_aliases()
    pending_title = ""

    for line in lines:
        title = _linked_title(line)
        if title:
            pending_title = title
            continue
        if not pending_title:
            continue
        matched_category = next(
            (
                display_name
                for alias, display_name in aliases.items()
                if alias in line
            ),
            "",
        )
        if matched_category:
            _append_category_title(
                categories,
                matched_category,
                pending_title,
                limit,
            )
            pending_title = ""
        elif line in aliases or line.startswith("本周"):
            pending_title = ""
    return _nonempty_categories(categories)


def extract_ai_category_titles(
    document_text: str,
    limit: int = AI_INSIGHT_CATEGORY_LIMIT,
) -> dict[str, list[str]]:
    """Extract up to two titles for each requested AI Insights module."""
    lines = [
        _clean_document_line(line)
        for line in (document_text or "").splitlines()
        if _clean_document_line(line)
    ]
    final_categories = _extract_final_ai_category_titles(lines, limit)
    if final_categories:
        return final_categories
    return _extract_candidate_ai_category_titles(lines, limit)


def extract_ai_pdf_url(document_text: str) -> str:
    match = re.search(
        r"AI洞察PDF.{0,160}?(https://[^\s)]+)",
        document_text or "",
        flags=re.DOTALL,
    )
    if not match:
        return ""
    url = match.group(1).rstrip(".,，。；;")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return ""
    return url


def _safe_card_text(value: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", value or "").strip()[:limit]
    return re.sub(r"([\\*_~\[\]()#>])", r"\\\1", compact)


def _button(label: str, url: str, button_type: str) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": button_type,
        "multi_url": {
            "url": url,
            "pc_url": url,
            "ios_url": url,
            "android_url": url,
        },
    }


def build_combined_card(
    period: WeekPeriod,
    design_headlines: list[DesignHeadline],
    ai_categories: dict[str, list[str]],
    ai_report_url: str,
) -> dict:
    if not design_headlines:
        raise ValueError("AI design headlines are required")
    if not ai_categories:
        raise ValueError("Categorized AI insight titles are required")
    if not ai_report_url.startswith("https://"):
        raise ValueError("A valid AI insight report URL is required")

    design_lines = "\n".join(
        f"{index}. {_safe_card_text(item.title)}"
        for index, item in enumerate(design_headlines, start=1)
    )
    insight_sections = []
    for category, _ in AI_INSIGHT_CATEGORIES:
        titles = ai_categories.get(category, [])[:AI_INSIGHT_CATEGORY_LIMIT]
        if not titles:
            continue
        title_lines = "\n".join(
            f"{index}. {_safe_card_text(title)}"
            for index, title in enumerate(titles, start=1)
        )
        insight_sections.append(
            f"**{_safe_card_text(category)}**\n{title_lines}"
        )
    insight_lines = "\n\n".join(insight_sections)
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": period.card_title},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**🎨 AI×交互与设计资讯**\n\n{design_lines}",
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**🔍 AI×用户研究与市场洞察资讯**\n\n{insight_lines}",
                },
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    _button("查看AI设计完整周报", DESIGN_SITE_URL, "primary"),
                    _button("查看AI洞察完整周报", ai_report_url, "default"),
                ],
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "AI Design · User Research & Consumer Insights",
                    }
                ],
            },
        ],
    }


def build_alert_card(reason: str, now: datetime | None = None) -> dict:
    current = now or datetime.now(tz=ZoneInfo("Asia/Shanghai"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    current = current.astimezone(ZoneInfo("Asia/Shanghai"))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "red",
            "title": {
                "tag": "plain_text",
                "content": "AI×设计与用户研究周报｜异常提醒",
            },
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "**本次未向正式群推送组合周报。**\n\n"
                        f"**原因：** {_safe_card_text(reason, 500)}\n\n"
                        f"**检查时间：** {current:%Y-%m-%d %H:%M}（北京时间）"
                    ),
                },
            },
            {
                "tag": "action",
                "actions": [
                    _button("查看设计资讯JSON", DESIGN_FEED_URL, "primary"),
                    _button("查看设计资讯网站", DESIGN_SITE_URL, "default"),
                ],
            },
        ],
    }


async def _find_ai_document(publisher, period: WeekPeriod) -> dict | None:
    from publishers.feishu_archive import (
        AI_INSIGHTS,
        FeishuArchiveError,
        FeishuArchiveManager,
    )

    archive = FeishuArchiveManager(publisher)
    try:
        document = await archive.find_report_by_title(
            AI_INSIGHTS,
            period.ai_report_title,
        )
        if document:
            return document
    except FeishuArchiveError as exc:
        print(f"Archive lookup failed; checking the app root: {exc}")
    return await publisher.find_document_by_title(period.ai_report_title)


async def _send_card(publisher, chat_id: str, card: dict) -> None:
    token = await publisher._get_tenant_access_token()
    url = f"{publisher.BASE_URL}/im/v1/messages?receive_id_type=chat_id"
    payload = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as response:
            data = await response.json()
    if response.status != 200 or data.get("code") != 0:
        raise RuntimeError(
            "Feishu combined-card send failed: "
            f"HTTP {response.status}, code={data.get('code')}, "
            f"msg={data.get('msg', '')}"
        )
    print(f"Feishu card sent to {chat_id}")


async def publish_combined(
    chat_id: str,
    alert_chat_id: str = DEFAULT_ALERT_CHAT_ID,
    state_path: str | Path = DEFAULT_STATE_PATH,
    dry_run: bool = False,
    upstream_conclusion: str = "",
) -> int:
    if not re.fullmatch(r"oc_[A-Za-z0-9]+", chat_id or ""):
        print("AI_DESIGN_FEISHU_CHAT_ID is missing or invalid.")
        return 1
    if not re.fullmatch(r"oc_[A-Za-z0-9]+", alert_chat_id or ""):
        print("AI_DESIGN_ALERT_CHAT_ID is missing or invalid.")
        return 1

    period = get_previous_week()
    if dry_run:
        feed = await fetch_design_headlines()
        problem = design_feed_problem(feed, state_path)
        print(f"\n{period.card_title}")
        print(f"Feed updated at: {feed.updated_at_iso}")
        print(f"Feed status: {problem or 'ready'}")
        for index, item in enumerate(feed.headlines, start=1):
            print(f"{index}. {item.title}")
        return 0 if not problem else 1

    from publishers.feishu_publisher import FeishuPublisher

    publisher = FeishuPublisher()
    if not publisher.is_configured():
        print("Feishu credentials are missing.")
        return 1

    async def alert(reason: str) -> int:
        print(f"Combined weekly push blocked: {reason}")
        try:
            await _send_card(publisher, alert_chat_id, build_alert_card(reason))
        except Exception as exc:  # alerting must never hide the root cause
            print(f"Feishu alert send failed: {exc}")
        return 1

    if upstream_conclusion and upstream_conclusion != "success":
        return await alert(
            f"上游AI洞察周报任务状态为 {upstream_conclusion}，组合周报未执行"
        )

    try:
        feed = await fetch_design_headlines()
    except Exception as exc:
        return await alert(f"设计资讯JSON读取或解析失败：{exc}")

    print(
        f"Fetched {len(feed.headlines)} design headlines; "
        f"feed updated at {feed.updated_at_iso}."
    )
    problem = design_feed_problem(feed, state_path)
    if problem:
        return await alert(problem)

    try:
        document = await _find_ai_document(publisher, period)
        if not document:
            raise RuntimeError(
                f"未找到AI洞察周报文档：{period.ai_report_title}"
            )
        document_text = await publisher.read_document_text(document["document_id"])
        ai_categories = extract_ai_category_titles(document_text)
        ai_pdf_url = extract_ai_pdf_url(document_text)
        ai_report_url = ai_pdf_url or document["url"]
        if not ai_categories:
            raise RuntimeError("AI洞察周报中没有可用的分类资讯标题")

        token_match = re.search(r"/file/([A-Za-z0-9_-]+)", ai_pdf_url)
        if token_match:
            await publisher.set_file_permission(token_match.group(1), chat_id)
        else:
            await publisher.set_document_public_permission(
                document["document_id"],
                chat_id,
            )
            print(
                "AI insight final PDF is not ready; the combined card links to "
                "the existing weekly document."
            )

        card = build_combined_card(
            period,
            list(feed.headlines),
            ai_categories,
            ai_report_url,
        )
        await _send_card(publisher, chat_id, card)
    except Exception as exc:
        return await alert(f"组合周报生成或飞书推送失败：{exc}")

    try:
        save_feed_state(state_path, feed)
    except OSError as exc:
        return await alert(f"周报已推送，但去重状态保存失败：{exc}")
    print(f"Saved successful design-feed state to {state_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chat-id",
        default=os.environ.get("AI_DESIGN_FEISHU_CHAT_ID", "").strip(),
        help="target Feishu chat ID; defaults to AI_DESIGN_FEISHU_CHAT_ID",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scrape and print design headlines without reading or sending Feishu data",
    )
    parser.add_argument(
        "--check-design-feed",
        action="store_true",
        help="exit successfully only when the fixed design JSON feed has items",
    )
    parser.add_argument(
        "--alert-chat-id",
        default=os.environ.get(
            "AI_DESIGN_ALERT_CHAT_ID",
            DEFAULT_ALERT_CHAT_ID,
        ).strip(),
        help="Feishu test group used for data and push alerts",
    )
    parser.add_argument(
        "--state-path",
        default=os.environ.get("AI_DESIGN_STATE_PATH", DEFAULT_STATE_PATH).strip(),
        help="cached state file used to prevent duplicate pushes",
    )
    parser.add_argument(
        "--upstream-conclusion",
        default=os.environ.get("AI_INSIGHTS_UPSTREAM_CONCLUSION", "").strip(),
        help="conclusion of the upstream AI Insights workflow_run event",
    )
    return parser.parse_args()


async def check_design_feed() -> int:
    try:
        feed = await fetch_design_feed()
    except (RuntimeError, ValueError) as exc:
        print(f"Design JSON feed is not ready: {exc}")
        return 1
    problem = design_feed_problem(feed, Path("/tmp/nonexistent-design-feed-state"))
    if problem:
        print(f"Design JSON feed is not ready: {problem}")
        return 1
    print(
        f"Design JSON feed is ready with {len(feed.headlines)} headlines; "
        f"updated_at={feed.updated_at_iso}."
    )
    return 0


def main() -> None:
    args = parse_args()
    if args.check_design_feed:
        raise SystemExit(asyncio.run(check_design_feed()))
    raise SystemExit(
        asyncio.run(
            publish_combined(
                args.chat_id,
                alert_chat_id=args.alert_chat_id,
                state_path=args.state_path,
                dry_run=args.dry_run,
                upstream_conclusion=args.upstream_conclusion,
            )
        )
    )


if __name__ == "__main__":
    main()
