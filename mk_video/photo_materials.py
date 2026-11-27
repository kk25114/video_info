#!/usr/bin/env python3
# coding: utf-8
"""按文稿关键词抓取新闻配图，写入视频叠图素材（materials.json）。

图源：今日头条图片搜索结果（国内可达、图片为新闻现场图）。
公众号正文图渠道见 wx_materials.py；本脚本作为可用的兜底图源。

用法：
  python3 mk_video/photo_materials.py --article '2.sunrich/0547_xxx.md' --video-duration 798.8
"""
import argparse, hashlib, io, json, re, sys, time
import urllib.parse, urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
FRAME_W = 1280


def fetch(url, timeout=30, referer=None):
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout).read()


def article_queries(md_text, limit=6):
    topics = [t.strip().lstrip("#") for t in re.findall(r"^-\s*#(\S+)", md_text, re.M)]
    queries = [t for t in topics if 2 <= len(t) <= 10]
    title = re.search(r"^#\s+(.+)", md_text, re.M)
    if title:
        core = re.split(r"[，。！？、,.!?]", title.group(1))[0].strip()
        if 4 <= len(core) <= 14:
            queries.append(core)
    seen, out = set(), []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out[:limit]


def so360_images(query, limit=40):
    """360 图片搜索（image.so.com/j），返回候选 [{img,title,link,w,h}]。"""
    url = f"https://image.so.com/j?q={urllib.parse.quote(query)}&pn=0&rn={limit}"
    try:
        d = json.loads(fetch(url).decode("utf-8", "replace"))
    except Exception:
        return []
    out = []
    for it in d.get("list", []):
        img = (it.get("img") or "").strip()
        if not img.startswith("http"):
            continue
        out.append({
            "img": img,
            "title": (it.get("title") or "")[:80],
            "link": it.get("link") or "",
            "site": (it.get("site") or "").lower(),
            "w": it.get("width") or 0,
            "h": it.get("height") or 0,
        })
    news = ("sohu.com", "163.com", "qq.com", "ifeng.com", "people.com", "xinhuanet.com",
            "thepaper.cn", "cctv.com", "chinanews.com", "sina.com", "youth.cn", "cnr.cn",
            "eol.cn", "chinadaily.com.cn", "huanqiu.com", "stcn.com", "yicai.com")
    bad = ("zhihu.com", "weibo.com", "bilibili.com", "douban.com", "xiaohongshu.com", "baijiahao")
    def rank(x):
        s = x["site"]
        tier = 0 if any(n in s for n in news) else (2 if any(b in s for b in bad) else 1)
        return (tier, -int(x["w"] or 0))
    out.sort(key=rank)
    return out


def valid_image(data):
    from PIL import Image, ImageStat
    try:
        im = Image.open(io.BytesIO(data))
        w, h = im.size
    except Exception:
        return None
    if w < 640 or h < 360:
        return None
    if not (1.15 <= w / h <= 2.35):
        return None
    if len(data) < 40000 or len(data) > 12 * 1024 * 1024:
        return None
    stat = ImageStat.Stat(im.convert("L"))
    if stat.stddev[0] < 34 or stat.mean[0] > 232 or stat.mean[0] < 26:
        return None
    try:
        import cv2, numpy as np
        arr = np.array(im.convert("RGB"))[:, :, ::-1].copy()
        if cv2.QRCodeDetector().detect(arr)[0]:
            return None
    except Exception:
        pass
    return w, h, im.format


def main():
    ap = argparse.ArgumentParser(description="按文稿关键词抓取新闻配图素材")
    ap.add_argument("--article", required=True)
    ap.add_argument("--output-dir", default="mk_video/images")
    ap.add_argument("--video-duration", type=float, default=600.0)
    ap.add_argument("--max-materials", type=int, default=5)
    ap.add_argument("--queries", default="", help="自定义检索词，逗号分隔")
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()

    md = Path(args.article).read_text(encoding="utf-8")
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    if args.clean:
        for p in outdir.iterdir():
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                p.unlink()

    queries = [q.strip() for q in args.queries.split(",") if q.strip()] or article_queries(md)
    print("🔎 检索词:", " / ".join(queries))

    materials, hashes = [], set()
    for i, q in enumerate(queries):
        if len(materials) >= args.max_materials:
            break
        if i:
            time.sleep(4)          # 360 接口有频率限制，逐词之间留间隔
        cands = so360_images(q)
        if not cands:
            time.sleep(6)
            cands = so360_images(q)
        print(f"  「{q}」候选 {len(cands)} 张")
        for cand in cands:
            if len(materials) >= args.max_materials:
                break
            iu = cand["img"]
            try:
                data = fetch(iu, referer="https://image.so.com/")
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
            name = f"pic_{len(materials) + 1:02d}_{digest[:8]}.{ext}"
            (outdir / name).write_bytes(data)
            materials.append({"file": name, "query": q, "source_url": iu,
                              "source_page": cand.get("link", ""), "title": cand.get("title", ""),
                              "width": info[0], "height": info[1], "bytes": len(data)})
            print(f"   ✅ {name} {info[0]}x{info[1]} {len(data)//1024}KB")

    if not materials:
        print("❌ 未获取到可用图片")
        return 1

    dur = args.video_duration
    start0 = 12.0
    gap = max(14.0, (dur - 24.0) / max(1, len(materials)))
    items = []
    for i, m in enumerate(materials):
        scale = round(min(0.9, max(0.42, (FRAME_W * 0.62) / m["width"])), 3)
        items.append({
            "file": m["file"],
            "start_seconds": round(start0 + i * gap, 2),
            "duration_seconds": min(12.0, round(gap - 2, 2)),
            "scale": scale,
            "position": "center",
            "source_query": m["query"],
            "source_url": m["source_url"],
        })
    manifest = {"article": args.article, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "video_duration": dur, "source": "toutiao", "materials": items}
    (outdir / "materials.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 完成：{len(items)} 张素材 → {outdir}/materials.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
