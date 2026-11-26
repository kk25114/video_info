#!/usr/bin/env python3
"""根据 Markdown 文稿搜索并下载图片素材。

示例：
    python3 mk_video/search_materials.py \
      --article '2.sunrich/0499_懂车帝也被解决了？.md' \
      --video-duration 686.484 \
      --clean

图片写入 ``mk_video/images``，同时生成 ``materials.json``。构建器会读取
这个清单，按 start_seconds、duration_seconds、position 和 scale 放置素材。
通过搜狗微信搜索一个主题；按相关性只打开一篇公众号文章。
正文由默认浏览器打开，在正文区域定位图片，通过右键“图片另存为”保存图片。
正文触发验证时保留页面，不自动混用封面或通用图库素材。
下载前会根据公众号文章标题和摘要做相关性过滤。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from PIL import Image
from bs4 import BeautifulSoup

if __package__:
    from .windows_browser import WindowsBrowser
else:
    from windows_browser import WindowsBrowser


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "mk_video" / "images"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
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
            line = line.strip()
            if not line:
                continue
            # 文档的“话题”标题后面可能没有下一个 Markdown 标题；只读取
            # 紧随其后的标签列表，不能把正文段落也误当成主题标签。
            match = re.match(r"^-\s*(#\S.*)$", line)
            if not match:
                break
            topics.append(match.group(1).strip())

    body = text
    if "---" in body:
        body = body.split("---", 1)[-1]
    paragraphs = [
        re.sub(r"\s+", " ", p.strip())
        for p in re.split(r"\n\s*\n", body)
        if len(p.strip()) >= 20 and not p.lstrip().startswith((">", "##", "#"))
    ]
    return title, intro, topics, paragraphs


def _sanitize_unicode(value: Any) -> Any:
    """替换网页响应中的孤立 surrogate，避免 JSON 清单无法写入 UTF-8。"""
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_sanitize_unicode(item) for item in value]
    if isinstance(value, dict):
        return {_sanitize_unicode(key): _sanitize_unicode(item) for key, item in value.items()}
    return value


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
    # 保持文档的新闻主题，不再自动扩展为汽车工厂等通用图库关键词。
    if title:
        terms.append(title)
    elif paragraphs:
        terms.append(paragraphs[0][:30])

    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        term = re.sub(r"\s+", " ", term).strip()
        if term and term not in seen:
            seen.add(term)
            result.append(term)
    return result


def _wechat_images(session: requests.Session, query: str, limit: int) -> list[dict[str, str]]:
    """只解析搜狗结果列表；选中的文章由浏览器打开。"""
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
        if not title_node:
            continue
        title = title_node.get_text(" ", strip=True)
        summary_node = card.select_one(".txt-info")
        publisher_node = card.select_one(".all-time-y2")
        summary = summary_node.get_text(" ", strip=True) if summary_node else ""
        publisher = publisher_node.get_text(" ", strip=True) if publisher_node else ""
        page_url = urljoin(response.url, title_node.get("href", ""))

        thumbnail_url = str(image_node.get("src") or "").strip() if image_node else ""
        if thumbnail_url.startswith("//"):
            thumbnail_url = "https:" + thumbnail_url
        original_url = parse_qs(urlparse(thumbnail_url).query).get("url", [""])[0]
        original_host = (urlparse(original_url).hostname or "").lower()
        if original_host not in {"mmbiz.qpic.cn", "m.qpic.cn"}:
            # 只接受微信图片 CDN 原图；不把结果页中的任意外链当作微信素材。
            original_url = thumbnail_url
        results.append(
            {
                "image_url": original_url,
                "thumbnail_url": thumbnail_url if original_url != thumbnail_url else "",
                "page_url": page_url,
                "article_url": "",
                "article_status": "resolve_in_windows_browser",
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


def _resolve_wechat_article(session: requests.Session, url: str) -> tuple[str, str]:
    """解析搜狗结果到公众号文章地址，不处理或绕过环境验证。"""
    if not url:
        return "", "missing_result_url"
    try:
        response = session.get(url, allow_redirects=True, timeout=30)
    except requests.RequestException as exc:
        return "", f"resolve_failed:{type(exc).__name__}"
    final_url = response.url
    if "mp.weixin.qq.com/" in final_url:
        return final_url, "ok"
    if "weixin.sogou.com/antispider" in final_url:
        return "", "sogou_antispider"

    body = response.content.decode(response.encoding or "utf-8", errors="replace")
    parts = re.findall(r"url\s*\+=\s*'([^']*)'", body)
    if parts:
        # 不对整条 URL 调用 html.unescape，避免把 ``&timestamp`` 误识别成
        # HTML 实体并破坏公众号链接。
        target = "".join(parts).replace("\\/", "/")
        if target.startswith("https://mp.weixin.qq.com/"):
            return target, "ok"
    return "", "unresolved_redirect"


class _WechatBodyBrowser:
    """在 Windows 默认浏览器中右键保存公众号正文图片。"""

    def __init__(self, *, browser: WindowsBrowser | None = None):
        self._windows = browser
        self._images: dict[str, int] = {}
        self._candidate: dict[str, Any] = {}
        self._save_failed = False

    def start(self) -> "_WechatBodyBrowser":
        if self._windows is None:
            self._windows = WindowsBrowser()
        self._windows.start()
        return self

    def extract(self, candidate: dict[str, str], max_images: int) -> tuple[list[str], str]:
        """定位正文图片，返回本次页面内的图片标识和访问状态。"""
        self._images.clear()
        self._candidate = candidate
        self._save_failed = False
        if self._windows is None:
            return [], "browser_not_started"
        page_url = str(candidate.get("page_url") or candidate.get("article_url") or "").strip()
        if not page_url:
            return [], "article_url_missing"
        try:
            page = self._windows.open_article(page_url, str(candidate.get("title", "")))
        except Exception as exc:
            candidate["article_fetch_error"] = str(exc)
            return [], f"browser_error:{type(exc).__name__}"
        if page.get("status") != "ok":
            return [], str(page.get("status") or "article_body_not_found")
        article_url = str(page.get("address") or page_url)
        if article_url.startswith("mp.weixin.qq.com/"):
            article_url = "https://" + article_url
        candidate["article_url"] = article_url
        if page.get("title"):
            candidate["title"] = str(page["title"])
        for image in page.get("images", [])[:max(1, max_images)]:
            index = int(image["index"])
            self._images[f"windows-image:{index}"] = index
        return list(self._images), "ok" if self._images else "body_images_not_found"

    @staticmethod
    def _windows_path(value: str) -> Path:
        match = re.match(r"^([A-Za-z]):[\\/](.*)$", value)
        if not match:
            return Path(value)
        parts = [part for part in match.group(2).replace("\\", "/").split("/") if part]
        return Path("/mnt", match.group(1).lower(), *parts)

    def download(self, image_ref: str, referer: str, max_bytes: int) -> bytes | None:
        """执行图片另存为，验证图片后清理本次创建的临时文件。"""
        if self._windows is None or image_ref not in self._images or self._save_failed:
            return None
        save_directory: Path | None = None
        try:
            saved = self._windows.save_image(self._images[image_ref])
            image_path = self._windows_path(str(saved["file_path"]))
            directory = image_path.parent
            if directory.parent.name.lower() != "temp" or not re.fullmatch(
                r"VideoInfoWechatImages-[A-Za-z0-9]+", directory.name
            ):
                raise RuntimeError("浏览器图片未保存到本次临时目录")
            save_directory = directory
            if image_path.stat().st_size > max_bytes:
                return None
            raw = image_path.read_bytes()
            with Image.open(io.BytesIO(raw)) as image:
                image.verify()
                if image.width < 320 or image.height < 180:
                    return None
                if max(image.size) / max(1, min(image.size)) > 4.5:
                    return None
            return raw
        except Exception as exc:
            self._save_failed = True
            self._candidate["article_fetch_status"] = "image_save_failed"
            self._candidate["image_save_error"] = str(exc)
            print(f"⚠️ 正文图片保存失败：{exc}", file=sys.stderr, flush=True)
            return None
        finally:
            if save_directory is not None:
                shutil.rmtree(save_directory, ignore_errors=True)

    def close(self) -> None:
        self._images.clear()
        if self._windows is not None:
            self._windows.close()



def _metadata_text(candidate: dict[str, str]) -> str:
    """组合搜索结果的可读元数据；不把查询词本身混入评分。"""
    fields = (
        candidate.get("title", ""),
        candidate.get("description", ""),
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
    # 元数据有时会把多个词压成一个 token，例如 carbraketesting。
    compact_term = "".join(term_tokens)
    return any(compact_term in token for token in token_list)


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(_term_in_text(text, term) for term in terms)


def _candidate_score(candidate: dict[str, str], query: str) -> int:
    """按候选自身元数据打分，返回 0 表示拒绝。"""
    if candidate.get("source") != "wechat":
        return 0
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
        # 仅包含警示牌的搜索命中不适合直接叠入视频画面。
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
        " ".join(str(candidate.get(field, "")) for field in ("title", "description")),
    ).lower()
    hits = sum(1 for token in query_tokens if token in evidence_text)
    # Require two query concepts when the query contains multiple concepts;
    # a shared name alone (for example another account called “山西铁头”) is
    # not enough to establish that the image belongs to this news event.
    minimum_hits = 1 if len(query_tokens) == 1 else 2
    if hits < minimum_hits:
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
    """生成不遮挡底部字幕的时间和位置；每张素材默认显示 10 秒。

    默认统一居中显示：素材按 0.72 缩放后水平、垂直居中，位于画面中部，
    与底部字幕（MarginV=22）保持距离。
    """
    positions = ["center"]
    count = len(materials)
    # 0499 这类汽车文章按语义段落布置图片，使素材与旁白大致同步。
    automotive_rules = [
        ("断裂", 12.0, "center"),
        ("刹车踏板", 12.0, "center"),
        ("紧急制动", 12.0, "center"),
        ("刹车踏板", 140.0, "center"),
        ("生产线", 330.0, "center"),
        ("耐久测试", 470.0, "center"),
        ("durability", 470.0, "center"),
        ("assembly line", 330.0, "center"),
        ("brake pedal", 140.0, "center"),
        ("brake", 12.0, "center"),
    ]
    each = 10.0
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
            planned_start = fallback_starts[index] if fallback_starts else None
            planned_position = positions[index % len(positions)]
        item: dict[str, Any] = {
            "position": planned_position,
            "duration_seconds": round(each, 3),
            # 与原文件名规则一致：等比缩放到画面框内（force_original_aspect_ratio=decrease），
            # 配合 position=center，素材居中显示且不与底部字幕重叠。
            "scale": 0.0,
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


def _mmss(seconds: float) -> str:
    """把秒数编码成原来的文件名时间码，例如 10s -> ``010``、610s -> ``1010``。"""
    minutes, secs = divmod(int(round(max(0.0, seconds))), 60)
    return f"{minutes}{secs:02d}"


def _apply_timecode_names(materials: list[dict[str, Any]], output_dir: Path, duration: float) -> None:
    """把素材重命名为原有的“起始时间-总时长”格式，例如 ``010-1010.jpg``。

    该格式与 build.py 的文件名规则兼容：起点/总长的比值决定出现时间，
    并且默认居中显示。
    """
    if not duration or duration <= 0:
        return
    total_code = _mmss(duration)
    used: set[str] = set()
    for material in materials:
        old_path = output_dir / str(material.get("file", ""))
        if not old_path.is_file():
            continue
        start = float(material.get("start_seconds", 0.0) or 0.0)
        new_name = f"{_mmss(start)}-{total_code}.jpg"
        suffix = 1
        while new_name in used or (output_dir / new_name).exists() and (output_dir / new_name) != old_path:
            suffix += 1
            new_name = f"{_mmss(start)}-{total_code}-{suffix}.jpg"
        used.add(new_name)
        new_path = output_dir / new_name
        if new_path != old_path:
            old_path.replace(new_path)
        material["file"] = new_name


def search_and_download(
    article: Path,
    output_dir: Path,
    count_per_query: int,
    max_materials: int,
    duration: float | None,
    clean: bool,
    max_bytes: int,
    use_wechat_body: bool,
) -> Path:
    title, intro, topics, paragraphs = _read_article(article)
    queries = _query_terms(title, intro, topics, paragraphs)
    if not queries:
        raise ValueError("无法从文稿提取搜索主题")
    queries = queries[:1]

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
    search_records: list[dict[str, Any]] = []
    windows_browser: WindowsBrowser | None = None
    windows_browser_init_error = ""
    for query in queries:
        found: list[dict[str, str]] = []
        record: dict[str, Any] = {"query": query, "results": []}
        search_records.append(record)
        try:
            source_results = _sanitize_unicode(
                _wechat_images(session, query, max(count_per_query, 10))
            )
            found = [item for item in source_results if _candidate_relevance(item, query)]
            record["results"] = source_results
            record["result_count"] = len(source_results)
            record["relevant_count"] = len(found)
            print(f"🔎 搜狗微信：{query} → {len(source_results)} 篇，相关 {len(found)} 篇", flush=True)
        except Exception as exc:
            record["error"] = str(exc)
            print(f"⚠️ 微信公众号搜索失败: {query}: {exc}", file=sys.stderr)
        if not found:
            try:
                if windows_browser is None:
                    if windows_browser_init_error:
                        raise RuntimeError(windows_browser_init_error)
                    windows_browser = WindowsBrowser().start()
                browser_results = _sanitize_unicode(
                    windows_browser.search_wechat(query, max(count_per_query, 10))
                )
                browser_found = [item for item in browser_results if _candidate_relevance(item, query)]
                record["windows_browser_results"] = browser_results
                record["windows_browser_relevant_count"] = len(browser_found)
                if browser_found:
                    record["selected_search_source"] = "windows_default_browser"
                    found = browser_found
                print(
                    f"🌐 默认浏览器微信搜索：{query} → {len(browser_results)} 篇，相关 {len(browser_found)} 篇",
                    flush=True,
                )
            except Exception as exc:
                if windows_browser is None:
                    windows_browser_init_error = str(exc)
                record["windows_browser_error"] = str(exc)
                print(f"⚠️ 默认浏览器搜索失败: {query}: {exc}", file=sys.stderr)
        found.sort(key=lambda item: _candidate_sort_key(item, query), reverse=True)
        result_sets.append((query, found))

    # 只打开排序最靠前的一篇相关公众号文章；从其正文中提取多张图片。
    for offset in range(count_per_query):
        for query, found in result_sets:
            if offset < len(found):
                candidates.append((query, found[offset]))
    candidates = candidates[:1]

    new_materials: list[dict[str, Any]] = []
    body_browser: _WechatBodyBrowser | None = None
    body_browser_failed = False
    body_access_blocked = False
    body_status_logged: set[str] = set()
    try:
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

            targets: list[tuple[str, bool]] = []
            if use_wechat_body and candidate.get("source") == "wechat" and not body_access_blocked:
                if body_browser is None and not body_browser_failed:
                    try:
                        body_browser = _WechatBodyBrowser(browser=windows_browser).start()
                    except Exception as exc:
                        body_browser_failed = True
                        candidate["article_fetch_status"] = "windows_browser_unavailable"
                        print(f"⚠️ Windows 浏览器不可用，本次不使用封面或图库替代：{exc}", file=sys.stderr)
                if body_browser is not None:
                    print(f"📄 读取公众号正文：{candidate.get('title', '')}", flush=True)
                    body_urls, body_status = body_browser.extract(
                        candidate,
                        max_images=max(20, max_materials * 4),
                    )
                    candidate["article_fetch_status"] = body_status
                    if body_status not in ("ok", "body_images_not_found") and body_status not in body_status_logged:
                        body_status_logged.add(body_status)
                        print(f"⚠️ 公众号正文抓取状态：{body_status}；不以封面或图库充当正文素材", flush=True)
                    if body_status == "verification_required":
                        # 不重复请求同一环境的验证页，也不尝试绕过验证。
                        body_access_blocked = True
                    targets.extend((url, True) for url in body_urls)

            # 只有显式使用 --no-wechat-body 时才允许下载搜索结果封面。
            if not use_wechat_body and candidate.get("image_url"):
                targets.append((str(candidate.get("image_url")), False))
                if candidate.get("thumbnail_url"):
                    targets.append((str(candidate.get("thumbnail_url")), False))
            for target_url, is_body_image in targets:
                if len(new_materials) >= max_materials:
                    break
                image_candidate = dict(candidate)
                image_candidate["image_url"] = target_url
                image_candidate["thumbnail_url"] = ""
                downloaded: tuple[bytes, str] | None = None
                if is_body_image and body_browser is not None:
                    body_raw = body_browser.download(
                        target_url,
                        str(candidate.get("article_url") or ""),
                        max_bytes,
                    )
                    if body_raw is not None:
                        downloaded = body_raw, target_url
                is_saved_image = target_url.startswith("windows-image:")
                if downloaded is None and not is_saved_image:
                    downloaded = _download_image(session, image_candidate, max_bytes)
                if not downloaded:
                    continue
                raw, fetched_url = downloaded
                digest = hashlib.sha256(raw).hexdigest()
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)
                index = len(existing_materials) + len(new_materials) + 1
                prefix = "wechat-body" if is_body_image else ("wechat" if candidate.get("source") == "wechat" else "material")
                file_name = f"{prefix}-{index:02d}-{_slug(query)}-{digest[:8]}.jpg"
                file_path = output_dir / file_name
                width, height = _save_normalized(raw, file_path, 960, 600)
                new_materials.append(
                    {
                        "file": file_name,
                        "query": query,
                        "source_url": candidate.get("article_url") if is_body_image else candidate.get("page_url", ""),
                        "source_search_url": candidate.get("search_url", ""),
                        "article_url": candidate.get("article_url", ""),
                        "image_url": "" if is_saved_image else fetched_url,
                        "source_image_index": int(target_url.split(":", 1)[1]) if is_saved_image else None,
                        "capture_method": "windows_image_save_as" if is_saved_image else "http",
                        "cover_image_url": candidate.get("image_url", "") if is_body_image else "",
                        "title": candidate.get("title", ""),
                        "description": candidate.get("description", ""),
                        "keywords": candidate.get("keywords", ""),
                        "creator": candidate.get("creator", ""),
                        "source": "wechat_body" if is_body_image else candidate.get("source", ""),
                        "article_fetch_status": candidate.get("article_fetch_status", "not_attempted"),
                        "license": candidate.get("license", ""),
                        "license_url": candidate.get("license_url", ""),
                        "sha256": digest,
                        "width": width,
                        "height": height,
                    }
                )
                candidate["downloaded_images"] = int(candidate.get("downloaded_images", 0)) + 1
    finally:
        if body_browser is not None:
            body_browser.close()
        report_path = output_dir / "search_results.json"
        report_path.write_text(
            json.dumps({"article": str(article), "searches": search_records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if not new_materials and not existing_materials:
        raise RuntimeError(f"没有下载到可用公众号图片素材；搜索和正文状态见 {report_path}")

    materials = existing_materials + new_materials
    placements = _placements(materials, duration)
    for material, placement in zip(materials, placements):
        material.update(placement)

    # 沿用原有素材命名习惯：起始时间-总时长（mmss-mmss），例如 010-1010.jpg。
    if duration and duration > 0:
        _apply_timecode_names(materials, output_dir, duration)

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
    parser.add_argument(
        "--no-wechat-body",
        action="store_true",
        help="显式仅下载公众号搜索结果封面，不打开 Windows 浏览器读取正文",
    )
    parser.add_argument(
        "--wechat-headful",
        action="store_true",
        help="兼容旧参数；默认始终使用可见 Windows 浏览器读取公众号正文",
    )
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
            use_wechat_body=not args.no_wechat_body,
        )
    except Exception as exc:
        print(f"❌ 素材搜索失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
