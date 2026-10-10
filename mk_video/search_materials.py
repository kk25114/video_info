#!/usr/bin/env python3
"""根据 Markdown 文稿搜索并下载图片素材。

示例：
    python3 mk_video/search_materials.py \
      --article '2.sunrich/0499_懂车帝也被解决了？.md' \
      --video-duration 686.484 \
      --clean

图片写入 ``mk_video/images``，同时生成 ``materials.json``。构建器会读取
这个清单，按 start_seconds、duration_seconds、position 和 scale 放置素材。
优先搜索微信公众号文章封面，再搜索 Openverse 和 Wikimedia Commons；可选开启 Bing。
下载前会根据标题、来源页面和文件名做相关性过滤。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from PIL import Image
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "mk_video" / "images"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
OPENVERSE_API = "https://api.openverse.org/v1/images/"
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
WECHAT_SEARCH_URL = "https://weixin.sogou.com/weixin"

# 这些词通常代表误命中，优先在下载前剔除，避免把无关页面的图片放进成片。
NEGATIVE_TERMS = (
    "strokeorder", "stroke order", "disney", "restaurant", "menu", "food",
    "horse", "school", "hospital", "medicine", "flower", "anime", "sticker",
    "clipart", "wallpaper", "portrait", "fashion", "makeup", "real estate",
    ".pdf", "report", "analysis", "technical note", "thesis", "dissertation",
    "manual", "catalog", "publication", "book", "document", "rail transit",
    "wood", "sap-stain", "stroke", "cover",
    "train", "rail", "railway", "railroad", "metro", "tram", "subway", "locomotive",
    "districtline", "londontransport", "londonunderground", "postcard",
    "testtrackepcot", "epcot",
    "spacecraft", "nasa",
    "rocket", "aircraft", "airplane", "helicopter", "motorcycle", "scooter", "bicycle",
    "modelpress", "lingerie", "sexy", "beach", "woman", "women", "girl", "girls",
    "photo shoot", "gravure", "swimsuit", "idol", "celebrity", "fashion model",
    "笔画", "餐厅", "菜单", "医院", "药品", "花卉", "动漫", "壁纸",
)
POSITIVE_TERMS = (
    "car", "auto", "automotive", "vehicle", "motor", "brake", "test", "track",
    "factory", "assembly", "plant", "production", "durability", "porsche",
    "range rover", "ford", "fiat", "volvo", "bmw", "mercedes", "tesla", "maserati",
    "ferrari", "lancia", "jeep", "willys", "sedan", "model s", "goodwood", "racing",
    "motorsport", "新能源", "汽车",
    "车辆", "刹车", "制动", "工厂", "生产", "测试", "耐久", "轿车", "越野",
    "尊界", "江淮", "懂车帝",
)


def _read_article(path: Path) -> tuple[str, str, list[str], list[str]]:
    text = path.read_text(encoding="utf-8")
    title = ""
    title_match = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    if title_match:
        title = title_match.group(1).strip()

    intro_match = re.search(r"##\s*简介\s*\n(.*?)(?:\n##|\Z)", text, re.S)
    intro = re.sub(r"\s+", " ", intro_match.group(1).strip()) if intro_match else ""

    topic_match = re.search(r"##\s*话题\s*\n(.*?)(?:\n##|\Z)", text, re.S)
    topics = []
    if topic_match:
        for line in topic_match.group(1).splitlines():
            line = line.strip().lstrip("- ")
            if line:
                topics.append(line)

    body = text
    if "---" in body:
        body = body.split("---", 1)[-1]
    paragraphs = [
        re.sub(r"\s+", " ", p.strip())
        for p in re.split(r"\n\s*\n", body)
        if len(p.strip()) >= 20 and not p.lstrip().startswith((">", "##", "#"))
    ]
    return title, intro, topics, paragraphs


def _query_terms(title: str, intro: str, topics: list[str], paragraphs: list[str]) -> list[str]:
    """提取少量可搜索的主题，避免把整篇文章发送给搜索引擎。"""
    clean_topics = [re.sub(r"^#", "", x) for x in topics]
    terms: list[str] = []
    if clean_topics:
        terms.append(" ".join(clean_topics[:3]))
        simplified = []
        raw_cleaned = []
        for topic in clean_topics:
            topic = re.sub(r"(?i)[sv]\d+", "", topic)
            raw_cleaned.append(topic)
            topic = re.sub(r"(断裂|测试|汽车)$", "", topic)
            if topic and topic not in simplified:
                simplified.append(topic)
        if simplified:
            legal_case = bool(re.search(
                r"一审|宣判|判决|判处|获刑|法院|敲诈勒索|寻衅滋事|诈骗|盗窃",
                f"{title} {intro} {' '.join(clean_topics)}",
            ))
            if legal_case:
                # 法律新闻优先组合人物/主体与罪名，避免“流量变现”等泛话题
                # 把同名人物或旧闻带进来。
                event_topics = [
                    topic for topic in simplified
                    if re.search(r"敲诈|勒索|寻衅|诈骗|盗窃|判决|获刑|宣判|起诉|逮捕|法院", topic)
                ]
                if event_topics:
                    terms.append(" ".join([simplified[0], *event_topics[:2]]))
            # 公众号索引对短主题词更友好，例如“尊界 刹车踏板 懂车帝”。
            event_topic = next(
                (topic for topic in raw_cleaned if re.search(r"(断裂|故障|失灵|起火|召回|爆炸|泄漏|事故)$", topic)),
                "",
            )
            event_match = re.search(r"(断裂|故障|失灵|起火|召回|爆炸|泄漏|事故)$", event_topic)
            if len(simplified) >= 4 and event_match:
                event_body = event_topic[: event_match.start()]
                terms.append(" ".join((simplified[3], simplified[0], event_body, event_match.group(1))))
            if len(simplified) >= 4 and not legal_case:
                terms.append(" ".join((simplified[0], simplified[1], simplified[3])))
            terms.append(" ".join(simplified[:3]))
    # 针对汽车安全类文稿提供稳定的语义扩展，通用文稿仍由标题/话题驱动。
    corpus = f"{title} {intro} {' '.join(clean_topics)}".lower()
    automotive = any(k in corpus for k in ("尊界", "刹车", "汽车", "车辆", "车企"))
    if automotive:
        terms.extend(
            [
                "automotive test track car photo",
                "car braking test",
                "car assembly line",
                "electric car factory",
                "vehicle durability test Range Rover",
                "automotive safety test vehicle",
            ]
        )
    elif title:
        terms.append(title)
    elif paragraphs:
        # 没有标题时仍保留一个可搜索的主题，避免返回空查询。
        terms.append("relevant documentary photo")

    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        term = re.sub(r"\s+", " ", term).strip()
        if term and term not in seen:
            seen.add(term)
            result.append(term)
    return result


def _openverse_images(session: requests.Session, query: str, limit: int) -> list[dict[str, str]]:
    response = session.get(
        OPENVERSE_API,
        params={"q": query, "page_size": min(max(limit, 1), 20)},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    results: list[dict[str, str]] = []
    for data in payload.get("results", []):
        image_url = str(data.get("url") or "").strip()
        thumb_url = str(data.get("thumbnail") or "").strip()
        page_url = str(data.get("foreign_landing_url") or data.get("landing_url") or "").strip()
        if not image_url and not thumb_url:
            continue
        tags = data.get("tags") or []
        keywords = " ".join(
            str(tag.get("name") or "").strip()
            for tag in tags
            if isinstance(tag, dict) and str(tag.get("name") or "").strip()
        )
        results.append(
            {
                "image_url": image_url,
                "thumbnail_url": thumb_url,
                "page_url": page_url,
                "title": str(data.get("title") or "").strip(),
                "description": str(data.get("description") or "").strip(),
                "keywords": keywords,
                "creator": str(data.get("creator") or "").strip(),
                "license": str(data.get("license") or "").strip(),
                "license_url": str(data.get("license_url") or "").strip(),
                "source": "openverse",
            }
        )
        if len(results) >= limit:
            break
    return results


def _wikimedia_images(session: requests.Session, query: str, limit: int) -> list[dict[str, str]]:
    response = session.get(
        WIKIMEDIA_API,
        params={
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,
            "gsrlimit": min(max(limit, 1), 20),
            "prop": "imageinfo",
            "iiprop": "url|mime|size|extmetadata",
            "iiurlwidth": 1280,
            "format": "json",
            "formatversion": "2",
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    results: list[dict[str, str]] = []
    for data in payload.get("query", {}).get("pages", []):
        info = (data.get("imageinfo") or [{}])[0]
        image_url = str(info.get("thumburl") or info.get("url") or "").strip()
        page_url = str(info.get("descriptionurl") or "").strip()
        title = str(data.get("title") or "").removeprefix("File:").strip()
        metadata = info.get("extmetadata") or {}
        license_name = metadata.get("LicenseShortName", {}).get("value", "")
        creator = metadata.get("Artist", {}).get("value", "")
        description = metadata.get("ImageDescription", {}).get("value", "")
        categories = metadata.get("Categories", {}).get("value", "")
        if not image_url:
            continue
        results.append(
            {
                "image_url": image_url,
                "thumbnail_url": "",
                "page_url": page_url,
                "title": title,
                "description": str(description),
                "keywords": str(categories),
                "creator": str(creator),
                "license": str(license_name),
                "license_url": page_url,
                "source": "wikimedia",
            }
        )
        if len(results) >= limit:
            break
    return results


def _bing_images(session: requests.Session, query: str, limit: int) -> list[dict[str, str]]:
    response = session.get(
        "https://www.bing.com/images/search",
        params={"q": query, "form": "HDRSC2", "first": 1},
        timeout=30,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results: list[dict[str, str]] = []
    for node in soup.select("a.iusc"):
        raw = html.unescape(node.get("m", ""))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        image_url = str(data.get("murl") or "").strip()
        thumb_url = str(data.get("turl") or "").strip()
        page_url = str(data.get("purl") or "").strip()
        if not image_url and not thumb_url:
            continue
        results.append(
            {
                "image_url": image_url,
                "thumbnail_url": thumb_url,
                "page_url": page_url,
                "title": str(data.get("t") or "").strip(),
                "description": str(data.get("desc") or "").strip(),
                "keywords": str(data.get("pt") or "").strip(),
                "source": "bing",
                "license": "",
            }
        )
        if len(results) >= limit:
            break
    return results


def _wechat_images(session: requests.Session, query: str, limit: int) -> list[dict[str, str]]:
    """搜索搜狗微信索引，使用公开结果卡片中的公众号文章封面原图。

    微信正文偶尔会要求环境验证；这里不尝试绕过，只使用搜索结果页公开
    的标题、摘要、公众号名和封面图地址。
    """
    if not re.search(r"[\u4e00-\u9fff]", query):
        return []
    response = session.get(
        WECHAT_SEARCH_URL,
        params={"type": 2, "query": query},
        headers={"Referer": "https://weixin.sogou.com/"},
        timeout=30,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results: list[dict[str, str]] = []
    cards = soup.select('li[id^="sogou_vr_"]') or soup.select(".news-list li")
    for card in cards:
        title_node = card.select_one("h3 a")
        image_node = card.select_one(".img-box img")
        if not title_node or not image_node:
            continue
        title = title_node.get_text(" ", strip=True)
        summary_node = card.select_one(".txt-info")
        publisher_node = card.select_one(".all-time-y2")
        summary = summary_node.get_text(" ", strip=True) if summary_node else ""
        publisher = publisher_node.get_text(" ", strip=True) if publisher_node else ""
        page_url = urljoin(response.url, title_node.get("href", ""))

        thumbnail_url = str(image_node.get("src") or "").strip()
        if thumbnail_url.startswith("//"):
            thumbnail_url = "https:" + thumbnail_url
        original_url = parse_qs(urlparse(thumbnail_url).query).get("url", [""])[0]
        original_host = (urlparse(original_url).hostname or "").lower()
        if original_host not in {"mmbiz.qpic.cn", "m.qpic.cn"}:
            # 只接受微信图片 CDN 原图；不把结果页中的任意外链当作微信素材。
            original_url = thumbnail_url
        if not original_url:
            continue

        results.append(
            {
                "image_url": original_url,
                "thumbnail_url": thumbnail_url if original_url != thumbnail_url else "",
                "page_url": page_url,
                "search_url": response.url,
                "title": title,
                "description": summary,
                "keywords": query,
                "creator": publisher,
                "source": "wechat",
                "license": "unknown",
            }
        )
        if len(results) >= limit:
            break
    return results


def _metadata_text(candidate: dict[str, str]) -> str:
    """组合搜索结果的可读元数据；不把查询词本身混入评分。"""
    fields = (
        candidate.get("title", ""),
        candidate.get("description", ""),
        candidate.get("keywords", ""),
        candidate.get("page_url", ""),
        candidate.get("image_url", ""),
    )
    return re.sub(r"\s+", " ", " ".join(str(value) for value in fields)).strip().lower()


def _english_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _term_in_text(text: str, term: str) -> bool:
    """英文按完整词匹配，中文按子串匹配，避免 postcard 命中 car。"""
    term = term.strip().lower()
    if not term:
        return False
    if any(ord(char) > 127 for char in term):
        return term in text
    term_tokens = re.findall(r"[a-z0-9]+", term)
    if not term_tokens:
        return False
    token_list = re.findall(r"[a-z0-9]+", text.lower())
    tokens = set(token_list)
    if len(term_tokens) == 1:
        return term_tokens[0] in tokens
    width = len(term_tokens)
    if any(token_list[i : i + width] == term_tokens for i in range(len(token_list) - width + 1)):
        return True
    # Openverse 标签有时会把多个词压成一个 token，例如 carbraketesting。
    compact_term = "".join(term_tokens)
    return any(compact_term in token for token in token_list)


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(_term_in_text(text, term) for term in terms)


def _candidate_score(candidate: dict[str, str], query: str) -> int:
    """按候选自身元数据打分，返回 0 表示拒绝。"""
    text = _metadata_text(candidate)
    if _has_any(text, NEGATIVE_TERMS):
        return 0

    query_lower = query.lower()
    vehicle = _has_any(text, (
        "car", "auto", "automotive", "vehicle", "automobile", "sedan", "rover",
        "porsche", "ford", "fiat", "tesla", "maserati", "ferrari", "汽车", "车辆",
        "尊界", "江淮",
    ))
    brake = _has_any(text, (
        "brake", "braking", "pedal", "carbraketesting", "制动", "刹车",
    ))
    test = _has_any(text, ("test", "testing", "tested", "safety", "inspection", "crash", "测试"))
    factory = _has_any(text, ("factory", "assembly", "plant", "production", "manufacturing", "工厂", "生产线"))

    # 查询本身只描述了汽车刹车时，必须在结果元数据中同时出现刹车语义和车辆语义。
    if _has_any(query_lower, ("brake", "braking", "pedal", "刹车", "制动")):
        # Openverse 的“car brake testing”结果中有一张仅包含警示牌的照片；
        # 它可作搜索命中，但不适合直接叠入视频画面。
        if _has_any(text, ("sign", "warning sign", "postcard")):
            return 0
        return 100 + int(vehicle) * 20 + int(test) * 10 if brake and vehicle else 0
    if _has_any(query_lower, ("factory", "assembly", "生产线", "工厂")):
        return 90 + int(vehicle) * 20 + int(test) * 5 if factory and vehicle else 0
    if _has_any(query_lower, ("durability", "range rover")):
        durability = _has_any(text, ("durability", "testing", "test", "develop", "range rover"))
        return 80 + int(vehicle) * 20 if durability and vehicle else 0
    if _has_any(query_lower, ("safety", "crash")):
        return 80 + int(vehicle) * 20 if test and vehicle else 0
    if _has_any(query_lower, ("test track", "proving ground")):
        track = _has_any(text, ("track", "circuit", "proving ground", "test track"))
        return 70 + int(vehicle) * 20 if track and test and vehicle else 0

    # 通用新闻查询按查询词与文章标题/摘要的重合度筛选。此前这里仍然
    # 强制要求汽车语义，导致“铁头案”等非汽车稿件即使搜到公众号文章也
    # 会全部被丢弃。
    query_tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{3,}", query_lower)
    stop_tokens = {
        "新闻", "文章", "相关", "报道", "一审", "宣判", "最新", "今天", "到底",
        "怎么回事", "是什么", "这个", "那个", "之后", "正在", "关于",
    }
    query_tokens = [token for token in query_tokens if token not in stop_tokens]
    if not query_tokens:
        return 0
    evidence_text = re.sub(
        r"\s+", " ",
        " ".join(str(candidate.get(field, "")) for field in ("title", "description", "keywords")),
    ).lower()
    hits = sum(1 for token in query_tokens if token in evidence_text)
    # Require two query concepts when the query contains multiple concepts;
    # a shared name alone (for example another account called “山西铁头”) is
    # not enough to establish that the image belongs to this news event.
    minimum_hits = 1 if len(query_tokens) == 1 else 2
    if hits < minimum_hits:
        return 0
    # 中文新闻素材优先只接受公众号结果；Openverse/Wikimedia 的通用图片
    # 不具备事件上下文，容易把无关图片混入成片。
    if candidate.get("source") != "wechat":
        return 0
    return 30 + hits * 20


def _candidate_relevance(candidate: dict[str, str], query: str) -> bool:
    return _candidate_score(candidate, query) > 0


def _candidate_sort_key(candidate: dict[str, str], query: str) -> tuple[int, int]:
    """优先标题明确讲测试和故障本身的公众号文章。"""
    score = _candidate_score(candidate, query)
    if candidate.get("source") != "wechat":
        return score, 0
    title = candidate.get("title", "").lower()
    text = f"{title} {candidate.get('description', '').lower()}"
    title_matches = sum(1 for term in query.lower().split() if term and term in title)
    event_match = int(bool(re.search(r"断裂|踩断|折断|连断|断了", title)))
    test_match = int(bool(re.search(r"测试|实测|急刹|紧急制动", title)))
    aftermath_penalty = int(bool(re.search(r"短信|轰炸|股价|跌停|下架", title)))
    # 首项确保标题命中具体故障和测试的文章排在单纯后续报道之前。
    directness = title_matches + event_match * 3 + test_match * 2 - aftermath_penalty * 2
    return directness, score + int("刹车踏板" in text or "制动踏板" in text)


def _download_image(
    session: requests.Session, candidate: dict[str, str], max_bytes: int
) -> tuple[bytes, str] | None:
    urls = [candidate.get("image_url", ""), candidate.get("thumbnail_url", "")]
    for url in urls:
        if not url:
            continue
        try:
            response = session.get(url, timeout=30, stream=True)
            response.raise_for_status()
            content_type = (response.headers.get("content-type") or "").lower()
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ValueError("image too large")
            raw = bytes(data)
            image = Image.open(io.BytesIO(raw))
            image.verify()
            if image.width < 320 or image.height < 180:
                continue
            # 过窄的扫描页/封面图通常不是可用的现场素材。
            if max(image.width, image.height) / max(1, min(image.width, image.height)) > 4.5:
                continue
            if content_type and not content_type.startswith("image/"):
                # 某些站点未返回正确 MIME，但 PIL 已经验证过，仍允许使用。
                pass
            return raw, url
        except Exception:
            continue
    return None


def _save_normalized(raw: bytes, target: Path, max_width: int, max_height: int) -> tuple[int, int]:
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="JPEG", quality=90, optimize=True)
    return image.width, image.height


def _slug(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", value).strip("-")
    return value[:48] or "material"


def _placements(materials: list[dict[str, Any]], duration: float | None) -> list[dict[str, Any]]:
    """生成不遮挡底部字幕的时间和位置。"""
    positions = ["top-right", "top-left", "center-right", "center-left", "top-right", "top-left"]
    count = len(materials)
    # 0499 这类汽车文章按语义段落布置图片，使素材与旁白大致同步。
    automotive_rules = [
        ("断裂", 12.0, "top-right"),
        ("刹车踏板", 12.0, "top-right"),
        ("紧急制动", 12.0, "top-right"),
        ("刹车踏板", 140.0, "top-left"),
        ("生产线", 330.0, "center-left"),
        ("耐久测试", 470.0, "top-right"),
        ("durability", 470.0, "top-right"),
        ("assembly line", 330.0, "center-left"),
        ("brake pedal", 140.0, "top-left"),
        ("brake", 12.0, "top-right"),
    ]
    each = min(58.0, max(24.0, ((duration or 300.0) - 40.0) / max(count, 1) - 8.0))
    if duration and duration > 0:
        fallback_starts = [
            max(8.0, (duration - each) * i / max(count - 1, 1))
            for i in range(count)
        ]
    else:
        fallback_starts = []
    result = []
    used_rules: set[str] = set()
    for index, material in enumerate(materials):
        query = str(material.get("query", "")).lower()
        matched = next(
            ((key, start, position) for key, start, position in automotive_rules if key.lower() in query and key not in used_rules),
            None,
        )
        if matched:
            rule_key, planned_start, planned_position = matched
            used_rules.add(rule_key)
        else:
            planned_start, planned_position = (fallback_starts[index] if fallback_starts else None), positions[index % len(positions)]
        item: dict[str, Any] = {
            "position": planned_position,
            "duration_seconds": round(each, 3),
            "scale": 0.72,
        }
        if duration and duration > 0:
            max_start = max(duration - each, 0.0)
            item["start_seconds"] = round(min(float(planned_start), max_start), 3)
        else:
            item["start_ratio"] = round((index + 1) / (count + 1), 5)
        result.append(item)
    if duration and duration > 0 and len(result) > 1:
        gap = each + 4.0
        max_start = max(duration - each, 0.0)
        ordered = sorted(range(len(result)), key=lambda i: float(result[i]["start_seconds"]))
        # 先向前排开相同或相近的时间点。
        previous = 0.0
        for index in ordered:
            start = min(float(result[index]["start_seconds"]), max_start)
            if index != ordered[0]:
                start = max(start, previous + gap)
            result[index]["start_seconds"] = start
            previous = start
        # 末尾空间不足时从后往前回移，保证最后一张仍能完整显示。
        next_start = max_start
        for index in reversed(ordered):
            start = min(float(result[index]["start_seconds"]), next_start)
            result[index]["start_seconds"] = round(start, 3)
            next_start = max(0.0, start - gap)
    return result


def search_and_download(
    article: Path,
    output_dir: Path,
    count_per_query: int,
    max_materials: int,
    duration: float | None,
    clean: bool,
    max_bytes: int,
    include_bing: bool,
) -> Path:
    title, intro, topics, paragraphs = _read_article(article)
    queries = _query_terms(title, intro, topics, paragraphs)
    if not queries:
        raise ValueError("无法从文稿提取搜索主题")

    output_dir.mkdir(parents=True, exist_ok=True)
    if clean:
        old_manifest_path = output_dir / "materials.json"
        if old_manifest_path.exists():
            try:
                old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                old_manifest = {}
            for item in old_manifest.get("materials", []):
                old_file = output_dir / str(item.get("file", ""))
                if old_file.parent == output_dir and old_file.is_file() and old_file.suffix.lower() in IMAGE_SUFFIXES:
                    old_file.unlink()
            old_manifest_path.unlink()

    old_manifest_path = output_dir / "materials.json"
    existing_materials: list[dict[str, Any]] = []
    old_queries: list[str] = []
    if old_manifest_path.exists() and not clean:
        try:
            old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
            if Path(str(old_manifest.get("article", ""))).resolve() == article.resolve():
                existing_materials = list(old_manifest.get("materials", []))
                old_queries = list(old_manifest.get("queries", []))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            existing_materials = []
            old_queries = []

    session = requests.Session()
    session.headers.update({
        "User-Agent": "VideoInfoMaterialFetcher/1.0 (local video production tool)",
        "Accept": "text/html,application/xhtml+xml,application/json",
    })
    seen_hashes: set[str] = {
        str(item.get("sha256")) for item in existing_materials if item.get("sha256")
    }
    seen_candidates: set[str] = {
        str(value).strip().lower()
        for item in existing_materials
        for value in (item.get("title", ""), item.get("source_url", ""), item.get("image_url", ""))
        if str(value).strip()
    }
    candidates: list[tuple[str, dict[str, str]]] = []
    result_sets: list[tuple[str, list[dict[str, str]]]] = []
    for query in queries:
        found: list[dict[str, str]] = []
        source_searches = [("微信公众号", _wechat_images)]
        # 中文新闻关键词应当只查公众号索引，避免再混入不具备事件上下文的
        # 通用图片站结果；英文视觉关键词仍可补充可授权的公共图库素材。
        if not re.search(r"[\u4e00-\u9fff]", query):
            source_searches.extend([
                ("Openverse", _openverse_images),
                ("Wikimedia", _wikimedia_images),
            ])
            if include_bing:
                source_searches.append(("Bing", _bing_images))
        for source_name, search in source_searches:
            try:
                result_limit = max(count_per_query, 10) if source_name == "微信公众号" else count_per_query
                source_results = search(session, query, result_limit)
                found.extend(item for item in source_results if _candidate_relevance(item, query))
            except Exception as exc:
                print(f"⚠️ {source_name} 搜索失败: {query}: {exc}", file=sys.stderr)
        found.sort(key=lambda item: _candidate_sort_key(item, query), reverse=True)
        result_sets.append((query, found))

    # 按查询词轮询，优先保证不同语义场景各拿到一张图，而不是被第一个
    # 返回大量结果的通用查询占满。
    for offset in range(count_per_query):
        for query, found in result_sets:
            if offset < len(found):
                candidates.append((query, found[offset]))

    new_materials: list[dict[str, Any]] = []
    for query, candidate in candidates:
        if len(new_materials) >= max_materials:
            break
        candidate_key = next(
            (
                value.strip().lower()
                for value in (candidate.get("title", ""), candidate.get("page_url", ""), candidate.get("image_url", ""))
                if value.strip()
            ),
            "",
        )
        if candidate_key and candidate_key in seen_candidates:
            continue
        if candidate_key:
            seen_candidates.add(candidate_key)
        downloaded = _download_image(session, candidate, max_bytes)
        if not downloaded:
            continue
        raw, fetched_url = downloaded
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        index = len(existing_materials) + len(new_materials) + 1
        prefix = "wechat" if candidate.get("source") == "wechat" else "material"
        file_name = f"{prefix}-{index:02d}-{_slug(query)}-{digest[:8]}.jpg"
        file_path = output_dir / file_name
        width, height = _save_normalized(raw, file_path, 960, 600)
        new_materials.append(
            {
                "file": file_name,
                "query": query,
                "source_url": candidate.get("page_url", ""),
                "source_search_url": candidate.get("search_url", ""),
                "image_url": fetched_url,
                "title": candidate.get("title", ""),
                "description": candidate.get("description", ""),
                "keywords": candidate.get("keywords", ""),
                "creator": candidate.get("creator", ""),
                "source": candidate.get("source", ""),
                "license": candidate.get("license", ""),
                "license_url": candidate.get("license_url", ""),
                "sha256": digest,
                "width": width,
                "height": height,
            }
        )

    if not new_materials and not existing_materials:
        raise RuntimeError("没有下载到可用图片素材")

    materials = existing_materials + new_materials
    placements = _placements(materials, duration)
    for material, placement in zip(materials, placements):
        material.update(placement)

    manifest = {
        "version": 1,
        "article": str(article),
        "title": title,
        "queries": list(dict.fromkeys([*old_queries, *queries])),
        "materials": materials,
    }
    manifest_path = output_dir / "materials.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 本次下载 {len(new_materials)} 张素材（清单共 {len(materials)} 张）到: {output_dir}")
    for item in materials:
        timing = item.get("start_seconds", item.get("start_ratio"))
        print(f"  {item['file']}  source={item.get('source', '')}  start={timing}  duration={item['duration_seconds']}s  position={item['position']}")
    print(f"🧾 来源清单: {manifest_path}")
    return manifest_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="根据文稿搜索并下载视频叠图素材")
    parser.add_argument("--article", required=True, help="Markdown 文稿路径")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT), help="素材输出目录")
    parser.add_argument("--video-duration", type=float, help="成片时长（秒）；不提供则使用相对时间")
    parser.add_argument("--count-per-query", type=int, default=6, help="每个搜索词最多读取结果数")
    parser.add_argument("--max-materials", type=int, default=5, help="最多下载图片数")
    parser.add_argument("--max-bytes", type=int, default=12 * 1024 * 1024, help="单张图片最大字节数")
    parser.add_argument("--clean", action="store_true", help="清理输出目录中的旧图片和 materials.json")
    parser.add_argument("--include-bing", action="store_true", help="允许补充版权不明的 Bing 图片结果")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        search_and_download(
            article=Path(args.article).expanduser().resolve(),
            output_dir=Path(args.output_dir).expanduser().resolve(),
            count_per_query=max(1, args.count_per_query),
            max_materials=max(1, args.max_materials),
            duration=args.video_duration,
            clean=args.clean,
            max_bytes=max(256 * 1024, args.max_bytes),
            include_bing=args.include_bing,
        )
    except Exception as exc:
        print(f"❌ 素材搜索失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
