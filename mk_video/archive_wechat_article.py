#!/usr/bin/env python3
"""归档搜狗微信搜索到的公众号文章正文、正文图片和封面图。

脚本只访问公开搜索结果和公开页面；如果搜狗或公众号返回验证页，
会保存状态说明并停止，不尝试绕过验证。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image


WECHAT_SEARCH_URL = "https://weixin.sogou.com/weixin"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "mk_video" / "wechat_archive"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def slug(value: str, limit: int = 60) -> str:
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", value).strip("-")
    return value[:limit] or "wechat-article"


def sogou_target(session: requests.Session, url: str) -> tuple[str | None, str]:
    """跟随搜狗文章跳转；公开 302 或页面脚本中的微信地址均可解析。"""
    response = session.get(url, allow_redirects=True, timeout=30)
    final_url = response.url
    if "mp.weixin.qq.com/" in final_url:
        return final_url, "ok"
    if "weixin.sogou.com/antispider" in final_url:
        return None, "sogou_antispider"

    body = response.content.decode(response.encoding or "utf-8", errors="replace")
    parts = re.findall(r"url\s*\+=\s*'([^']*)'", body)
    if parts:
        # 这里不能对整条 URL 调用 html.unescape：它会把 URL 参数
        # ``&timestamp`` 的前缀误识别成 HTML 实体 ``&times``，破坏链接。
        target = "".join(parts).replace("\\/", "/")
        if target.startswith("https://mp.weixin.qq.com/"):
            return target, "ok"
    return None, "unresolved_redirect"


def extract_search_cards(session: requests.Session, query: str, limit: int) -> list[dict[str, str]]:
    response = session.get(
        WECHAT_SEARCH_URL,
        params={"type": 2, "query": query},
        headers={"Referer": "https://weixin.sogou.com/"},
        timeout=30,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select('li[id^="sogou_vr_"]') or soup.select(".news-list li")
    results: list[dict[str, str]] = []
    for card in cards:
        title_node = card.select_one("h3 a")
        if not title_node:
            continue
        summary_node = card.select_one(".txt-info")
        publisher_node = card.select_one(".all-time-y2")
        image_node = card.select_one(".img-box img")
        thumb = ""
        cover_url = ""
        if image_node:
            thumb = str(image_node.get("src") or "").strip()
            if thumb.startswith("//"):
                thumb = "https:" + thumb
            cover_url = parse_qs(urlparse(thumb).query).get("url", [""])[0]
            if not cover_url:
                cover_url = thumb
        result_url = urljoin(response.url, title_node.get("href", ""))
        target_url, redirect_status = sogou_target(session, result_url)
        results.append({
            "query": query,
            "title": title_node.get_text(" ", strip=True),
            "publisher": publisher_node.get_text(" ", strip=True) if publisher_node else "",
            "summary": summary_node.get_text(" ", strip=True) if summary_node else "",
            "search_url": response.url,
            "sogou_result_url": result_url,
            "article_url": target_url or "",
            "redirect_status": redirect_status,
            "thumbnail_url": thumb,
            "cover_url": cover_url,
        })
        if len(results) >= limit:
            break
    return results


def download_image(session: requests.Session, url: str, target: Path) -> dict[str, object] | None:
    if not url:
        return None
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()
        image = Image.open(__import__("io").BytesIO(response.content))
        image.verify()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
        with Image.open(target) as check:
            width, height = check.size
            image_format = check.format or "unknown"
        return {
            "file": str(target.name),
            "url": url,
            "sha256": hashlib.sha256(response.content).hexdigest(),
            "width": width,
            "height": height,
            "format": image_format,
        }
    except Exception as exc:
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"}


def archive_article(session: requests.Session, item: dict[str, str], archive_dir: Path) -> dict[str, object]:
    article_url = item.get("article_url", "")
    item_result: dict[str, object] = {
        "query": item["query"],
        "title": item["title"],
        "publisher": item["publisher"],
        "summary": item["summary"],
        "search_url": item["search_url"],
        "sogou_result_url": item["sogou_result_url"],
        "article_url": article_url,
        "redirect_status": item["redirect_status"],
        "images": [],
    }
    stem = slug(item.get("publisher", "") + "-" + item["title"])
    cover = download_image(session, item.get("cover_url", ""), archive_dir / f"{stem}-cover.jpg")
    if cover:
        item_result["cover"] = cover

    if not article_url:
        item_result["status"] = item["redirect_status"]
        item_result["message"] = "搜狗文章链接跳转失败或触发验证；未尝试绕过。"
        return item_result

    try:
        response = session.get(article_url, timeout=30)
        response.raise_for_status()
    except Exception as exc:
        item_result["status"] = "article_request_failed"
        item_result["message"] = f"{type(exc).__name__}: {exc}"
        return item_result

    soup = BeautifulSoup(response.content, "html.parser")
    body = soup.select_one("#js_content")
    if not body:
        page_text = soup.get_text(" ", strip=True)
        if any(term in page_text for term in ("环境异常", "完成验证", "去验证")) or "captcha" in response.url.lower():
            item_result["status"] = "wechat_verification_required"
            item_result["message"] = "公众号页面要求环境验证；未下载正文，也未尝试绕过。"
        else:
            item_result["status"] = "article_body_not_found"
            item_result["message"] = "页面未返回可识别的公众号正文节点。"
        item_result["final_url"] = response.url
        return item_result

    title_node = soup.select_one("#activity-name") or soup.title
    article_title = title_node.get_text(" ", strip=True) if title_node else item["title"]
    paragraphs: list[str] = []
    for node in body.find_all(["p", "section", "h1", "h2", "h3", "blockquote"]):
        text = node.get_text(" ", strip=True)
        if text and (not paragraphs or paragraphs[-1] != text):
            paragraphs.append(text)
    if not paragraphs:
        paragraphs = [body.get_text("\n", strip=True)]

    image_records = []
    for index, image_node in enumerate(body.select("img"), start=1):
        src = str(
            image_node.get("data-src") or image_node.get("data-original")
            or image_node.get("src") or ""
        ).strip()
        src = urljoin(article_url, src)
        if not src or src.startswith("data:"):
            continue
        parsed = urlparse(src)
        if parsed.hostname and not (parsed.hostname.endswith("qpic.cn") or parsed.hostname.endswith("qlogo.cn")):
            # 文章图只取微信图片 CDN 地址；不抓取正文嵌入的第三方外链。
            continue
        ext = Path(parsed.path).suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            ext = ".jpg"
        record = download_image(session, src, archive_dir / f"{stem}-image-{index:02d}{ext}")
        if record:
            image_records.append(record)

    markdown_lines = [f"# {article_title}", "", f"- 公众号：{item['publisher']}", f"- 原文：{article_url}", f"- 搜索词：{item['query']}", "", "## 正文", ""]
    markdown_lines.extend(paragraphs)
    markdown_lines.extend(["", "## 正文图片", ""])
    markdown_lines.extend(f"![正文图片 {i}]({record['file']})" for i, record in enumerate(image_records, start=1))
    article_path = archive_dir / f"{stem}.md"
    article_path.write_text("\n\n".join(markdown_lines).rstrip() + "\n", encoding="utf-8")
    item_result.update({
        "status": "archived",
        "final_url": response.url,
        "article_file": article_path.name,
        "body_chars": sum(len(paragraph) for paragraph in paragraphs),
        "body_images_count": len(image_records),
        "images": image_records,
    })
    return item_result


def main() -> int:
    parser = argparse.ArgumentParser(description="离线保存公开可访问的公众号文章和图片")
    parser.add_argument("--query", required=True, help="搜狗微信关键词")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT), help="归档目录")
    parser.add_argument("--limit", type=int, default=10, help="读取公众号搜索结果数")
    parser.add_argument("--max-articles", type=int, default=5, help="最多尝试归档文章数")
    args = parser.parse_args()

    out = Path(args.output_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })

    try:
        results = extract_search_cards(session, args.query, max(args.limit, 1))
    except Exception as exc:
        print(f"公众号搜索失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    (out / "search_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    archived = []
    for item in results[: max(1, args.max_articles)]:
        archived.append(archive_article(session, item, out))
    manifest = {
        "query": args.query,
        "search_source": WECHAT_SEARCH_URL,
        "results_count": len(results),
        "articles": archived,
    }
    manifest_path = out / "archive_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"搜索结果：{len(results)} 条；尝试归档：{len(archived)} 篇")
    for item in archived:
        print(f"[{item['status']}] {item['publisher']} | {item['title']}")
        if item.get("article_file"):
            print(f"  正文：{item['article_file']}；正文图片：{item['body_images_count']}")
        elif item.get("message"):
            print(f"  {item['message']}")
        if item.get("cover"):
            cover = item['cover']
            if isinstance(cover, dict) and cover.get("file"):
                print(f"  封面：{cover['file']}")
    print(f"清单：{manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
