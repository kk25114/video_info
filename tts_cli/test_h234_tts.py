#!/usr/bin/env python3
# coding: utf-8
"""测试 H234 Azure-compatible Speech TTS 接口。

用法：
  H234_API_KEY='你的 API Key' python3 tts_cli/test_h234_tts.py
  H234_API_KEY='你的 API Key' python3 tts_cli/test_h234_tts.py path/to/article.md

脚本不把 API Key 写入文件或输出；默认只测试最新的 0503 文稿。
"""

from __future__ import annotations

import argparse
import html
import os
import sys
from pathlib import Path

import requests


DEFAULT_ARTICLE = (
    Path(__file__).resolve().parents[1]
    / "2.sunrich"
    / "0503_中国为什么消费这么差？最新诺奖早就给了答案.md"
)
DEFAULT_URL = "https://api.h234.cn/cognitiveservices/v1"
DEFAULT_OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
DEFAULT_VOICE = "zh-CN-YunyangNeural"


def extract_body(path: Path) -> str:
    """沿用项目 TTS 脚本的正文提取规则。"""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    body_start = 0
    for index, line in enumerate(lines):
        if line.strip() == "---":
            body_start = index + 1
    while body_start < len(lines):
        line = lines[body_start].strip()
        if not line or line.startswith(">"):
            body_start += 1
        else:
            break
    return "".join(lines[body_start:]).strip()


def build_ssml(text: str, voice: str) -> str:
    escaped = html.escape(text, quote=False)
    return (
        '<speak version="1.0" xmlns:mstts="http://www.w3.org/2001/mstts" '
        'xml:lang="zh-CN">'
        f'<voice name="{html.escape(voice, quote=True)}">'
        f"<prosody rate=\"1.0\">{escaped}</prosody>"
        "</voice></speak>"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="测试 H234 Speech TTS API")
    parser.add_argument("article", nargs="?", type=Path, default=DEFAULT_ARTICLE)
    parser.add_argument("--output", type=Path, help="成功时保存返回音频的路径")
    parser.add_argument("--url", default=os.environ.get("H234_TTS_URL", DEFAULT_URL))
    parser.add_argument("--voice", default=os.environ.get("H234_TTS_VOICE", DEFAULT_VOICE))
    args = parser.parse_args()

    api_key = os.environ.get("H234_API_KEY", "").strip()
    if not api_key:
        print("错误：请通过 H234_API_KEY 环境变量提供 API Key。", file=sys.stderr)
        return 2
    if not args.article.is_file():
        print(f"错误：文稿不存在：{args.article}", file=sys.stderr)
        return 2

    text = extract_body(args.article)
    if not text:
        print("错误：文稿正文为空。", file=sys.stderr)
        return 2

    ssml = build_ssml(text, args.voice)
    headers = {
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": DEFAULT_OUTPUT_FORMAT,
        "Ocp-Apim-Subscription-Key": api_key,
        "User-Agent": "video-info-h234-tts-test/1.0",
    }

    print(f"接口：{args.url}")
    print(f"文稿：{args.article.name}")
    print(f"正文字符数：{len(text)}；SSML 字节数：{len(ssml.encode('utf-8'))}")
    try:
        response = requests.post(
            args.url,
            headers=headers,
            data=ssml.encode("utf-8"),
            timeout=(15, 180),
        )
    except requests.RequestException as exc:
        print(f"请求失败：{exc}", file=sys.stderr)
        return 1

    content_type = response.headers.get("Content-Type", "")
    print(f"HTTP 状态：{response.status_code}")
    print(f"响应类型：{content_type or '(未返回)'}")
    print(f"响应字节数：{len(response.content)}")
    if response.status_code != 200:
        detail = response.text.strip().replace("\n", " ")
        if len(detail) > 500:
            detail = detail[:500] + "..."
        print(f"接口返回：{detail or '(空响应)'}")
        return 1

    if args.output:
        output = args.output
    else:
        output = Path("/tmp") / "h234_0503_test.mp3"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(response.content)
    print(f"测试音频：{output}")
    print("结论：API Key 可用，接口成功返回音频。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
