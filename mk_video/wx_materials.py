#!/usr/bin/env python3
# coding: utf-8
"""按文稿关键词检索微信公众号文章，抓取正文配图作为视频叠图素材。

流程：文稿 -> 关键词 -> 百度移动版 site:mp.weixin.qq.com 检索 -> 打开微信文章
      -> 提取正文图片 -> 过滤/去重 -> 保存到素材目录并写 materials.json

用法：
  python3 mk_video/wx_materials.py --article '2.sunrich/0547_xxx.md' --video-duration 798.8
  python3 mk_video/wx_materials.py --article ... --links 'https://mp.weixin.qq.com/s?...'
"""
import argparse, hashlib, html, io, json, re, sys, time
import urllib.parse, urllib.request
from pathlib import Path

UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")
FRAME_W = 1280
BANK = Path(__file__).with_name("wx_url_bank.json")


def fetch(url, ua=UA_MOBILE, timeout=30, referer=None):
    headers = {"User-Agent": ua, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout).read()


def article_queries(md_text):
    topics = [t.strip().lstrip("#") for t in re.findall(r"^-\s*#(\S+)", md_text, re.M)]
    queries = [t for t in topics if 2 <= len(t) <= 12]
    m = re.search(r"^##\s*简介\s*\n+(.+)", md_text, re.M)
    if m:
        for part in re.split(r"[，。；、,.;]", m.group(1)):
            part = part.strip()
            if 4 <= len(part) <= 12 and part not in queries:
                queries.append(part)
            if len(queries) >= 8:
                break
    seen, out = set(), []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out[:8]


def baidu_search(query, tries=3):
    """返回 (urls, status)。status 取值：ok / captcha / empty / error。"""
    word = urllib.parse.quote("site:mp.weixin.qq.com " + query)
    url = f"https://m.baidu.com/s?word={word}"
    status = "error"
    for i in range(tries):
        try:
            raw = fetch(url).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            status = f"error:{type(exc).__name__}"
            time.sleep(4)
            continue
        if "百度安全验证" in raw or "wappass.baidu.com" in raw:
            status = "captcha"
            time.sleep(8 + i * 6)
            continue
        urls = []
        for u in re.findall(r'https?://mp\.weixin\.qq\.com/s\?[^"\'\\ <>]+', raw):
            u = html.unescape(u)
            if u not in urls:
                urls.append(u)
        if urls:
            return urls, "ok"
        status = "empty"
        break
    return [], status


def load_bank():
    if BANK.exists():
        try:
            return json.loads(BANK.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_bank(bank):
    try:
        BANK.write_text(json.dumps(bank, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        print(f"⚠️ URL 库写入失败: {exc}")


def article_images(url):
    """抓取微信文章，返回 (title, [图片URL])"""
    raw = fetch(url).decode("utf-8", "replace")
    m = re.search(r"var msg_title = '(.*?)'\.html", raw) or re.search(r'<h1[^>]*>(.*?)</h1>', raw, re.S)
    title = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else ""
    urls = []
    for u in re.findall(r'data-src="([^"]+)"', raw) + re.findall(r'<img[^>]+src="([^"]+)"', raw):
        u = html.unescape(html.unescape(u))
        if "mmbiz.qpic.cn" in u and u not in urls:
            urls.append(u)
    return title, urls


def _has_qr(im):
    try:
        import cv2, numpy as np
    except Exception:
        return False
    try:
        arr = np.array(im.convert("RGB"))[:, :, ::-1].copy()
        det = cv2.QRCodeDetector()
        ok, _ = det.detect(arr)
        return bool(ok)
    except Exception:
        return False


def valid_image(data):
    """过滤掉海报、二维码、纯色底图等不适合做叠图的素材。"""
    from PIL import Image, ImageStat
    try:
        im = Image.open(io.BytesIO(data))
        w, h = im.size
    except Exception:
        return None
    if w < 600 or h < 340:
        return None
    ratio = w / h
    if not (1.1 <= ratio <= 2.3):
        return None
    if len(data) < 30000 or len(data) > 12 * 1024 * 1024:
        return None
    gray = im.convert("L")
    stat = ImageStat.Stat(gray)
    if stat.stddev[0] < 32:          # 大块纯色/空白底
        return None
    mean = stat.mean[0]
    if mean > 232 or mean < 26:      # 近乎全白/全黑
        return None
    if _has_qr(im):
        return None
    return w, h, im.format


def main():
    ap = argparse.ArgumentParser(description="按文稿关键词从微信公众号抓取配图素材")
    ap.add_argument("--article", required=True)
    ap.add_argument("--output-dir", default="mk_video/images")
    ap.add_argument("--video-duration", type=float, default=600.0)
    ap.add_argument("--max-materials", type=int, default=5)
    ap.add_argument("--max-articles", type=int, default=8)
    ap.add_argument("--links", default="", help="直接指定微信文章链接，逗号分隔")
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()

    article = Path(args.article)
    md = article.read_text(encoding="utf-8")
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    if args.clean:
        for p in outdir.iterdir():
            if p.is_file() and (p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}):
                p.unlink()

    report = {"article": str(article), "queries": [], "articles": [], "downloaded": []}

    links = [u.strip() for u in args.links.split(",") if u.strip()]
    bank = load_bank()
    if not links:
        queries = article_queries(md)
        report["queries"] = queries
        for q in queries:
            if len(links) >= args.max_articles:
                break
            urls, status = baidu_search(q)
            print(f"🔎 检索「{q}」→ {status}，{len(urls)} 篇")
            report["articles"].append({"query": q, "status": status, "found": len(urls)})
            bank.setdefault(q, [])
            for u in urls:
                if u not in bank[q]:
                    bank[q].append(u)
                if u not in links:
                    links.append(u)
            save_bank(bank)
        if not links:
            for q, urls in bank.items():
                for u in urls:
                    if u not in links:
                        links.append(u)
            if links:
                print(f"ℹ️ 检索受限，改用本地已收录的 {len(links)} 个文章链接")
    links = links[: args.max_articles]

    materials, hashes = [], set()
    for idx, url in enumerate(links):
        if len(materials) >= args.max_materials:
            break
        try:
            title, imgs = article_images(url)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 正文抓取失败 {url[:60]}: {type(exc).__name__}")
            report["articles"].append({"url": url, "error": type(exc).__name__})
            continue
        print(f"📄 {title[:40] or url[:40]} → {len(imgs)} 张候选图")
        picked = 0
        for iu in imgs:
            if len(materials) >= args.max_materials:
                break
            try:
                data = fetch(iu, referer="https://mp.weixin.qq.com/")
            except Exception:
                continue
            info = valid_image(data)
            if not info:
                continue
            digest = hashlib.md5(data).hexdigest()
            if digest in hashes:
                continue
            hashes.add(digest)
            ext = "png" if "png" in (info[2] or "").lower() else "jpg"
            name = f"wx_{len(materials) + 1:02d}_{digest[:8]}.{ext}"
            (outdir / name).write_bytes(data)
            w, h, _ = info
            materials.append({
                "file": name,
                "source_article": url,
                "source_title": title,
                "width": w,
                "height": h,
                "bytes": len(data),
            })
            picked += 1
            print(f"   ✅ {name} {w}x{h} {len(data)//1024}KB")
        report["articles"].append({"url": url, "title": title, "images_found": len(imgs), "picked": picked})

    if not materials:
        print("❌ 未能获取任何可用图片")
        (outdir / "search_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    dur = args.video_duration
    start0, gap = 10.0, max(12.0, (dur - 20.0) / max(1, len(materials)))
    items = []
    for i, m in enumerate(materials):
        scale = round(min(0.9, max(0.35, (FRAME_W * 0.66) / m["width"])), 3)
        items.append({
            "file": m["file"],
            "start_seconds": round(start0 + i * gap, 2),
            "duration_seconds": min(12.0, round(gap - 1, 2)),
            "scale": scale,
            "position": "center",
            "source_article": m["source_article"],
            "source_title": m["source_title"],
        })
    manifest = {"article": str(article), "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "video_duration": dur, "materials": items}
    (outdir / "materials.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    report["downloaded"] = materials
    (outdir / "search_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 完成：{len(items)} 张素材 → {outdir}/materials.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
