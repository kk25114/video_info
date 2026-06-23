"""自动从 2.sunrich 最新文稿提取简介与话题，生成 45 字内描述。"""

from __future__ import annotations

import os
import json
import re
from pathlib import Path
from typing import Optional, Tuple

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config.json"
SUNRICH_DIR = REPO_ROOT / "2.sunrich"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
API_URL = f"{DEEPSEEK_BASE_URL}/chat/completions"
MODEL = "deepseek-v4-flash"
MAX_CHARS = 50
TOPIC_MAX = 5
USE_TUN_MODE = os.environ.get("VIDEO_INFO_USE_TUN", "").strip().lower() not in ("", "0", "false", "no")


def _load_api_key() -> Optional[str]:
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data.get("DEEPSEEK_API_KEY")


def _latest_markdown() -> Path:
    """选取 2.sunrich 目录下“编号最大”的 Markdown 文稿（仅按序号，不回退 mtime）。"""
    files = list(SUNRICH_DIR.glob("*.md"))
    if not files:
        raise FileNotFoundError(f"{SUNRICH_DIR} 中没有 Markdown 文稿")

    numbered = []
    for p in files:
        m = re.match(r"^(\d+)", p.stem)
        if m:
            try:
                numbered.append((int(m.group(1)), p))
            except ValueError:
                continue

    if not numbered:
        raise ValueError(f"{SUNRICH_DIR} 中没有以数字序号开头的 Markdown 文稿")

    numbered.sort(key=lambda x: x[0], reverse=True)
    return numbered[0][1]


def _extract_sections(markdown: str) -> Tuple[str, str]:
    intro = ""
    topics_block = ""

    intro_match = re.search(r"##\s*简介\n(.*?)(?:\n##|\Z)", markdown, re.S)
    if intro_match:
        intro = intro_match.group(1).strip()

    topics_match = re.search(r"##\s*话题\n(.*?)(?:\n##|\Z)", markdown, re.S)
    if topics_match:
        topics_block = topics_match.group(1).strip()

    topics_lines = []
    for line in topics_block.splitlines():
        stripped = line.strip()
        if stripped.startswith("-"):
            token = stripped.lstrip("- ")
            if token and not token.startswith("#"):
                token = "#" + token
            topics_lines.append(token)
    topics = " ".join(filter(None, topics_lines))
    return intro, topics


def _clean_text(text: str) -> str:
    cleaned = text.strip()
    # 去掉开头常见的编号/子弹符号：1) 1. 1、 （1） - • · 等
    patterns = [
        r"^\s*[0-9]+\s*[\)）\.·、:：]\s*",   # 1) / 1. / 1、/ 1: / 1：
        r"^\s*[（(]?[0-9]+[)）]\s*",           # (1) / （1）
        r"^\s*[-•·]\s*",                       # - / • / ·
        r"^\s*第[一二三四五六七八九十百千]+[、.．]\s*",  # 第一、
        r"^\s*简介[:：]\s*",                    # 简介：
        r"^\s*话题[:：]\s*",                    # 话题：
    ]
    for pat in patterns:
        cleaned = re.sub(pat, "", cleaned)
    # 去掉模型常见无效前缀（禁止“本视频/该视频/此视频/视频剖析”等发布口吻）
    meta_lead_patterns = [
        r"^\s*(?:本|该|此|这)(?:期)?视频(?:中)?(?:主要)?(?:围绕|聚焦|剖析|分析|解读|讲述|讲解|介绍|探讨|讨论)?[：:\s，,。]*",
        r"^\s*视频(?:中)?(?:主要)?(?:围绕|聚焦|剖析|分析|解读|讲述|讲解|介绍|探讨|讨论)?[：:\s，,。]*",
        r"^\s*(?:本文|这篇|本期(?:内容)?|这期(?:内容)?)(?:中)?(?:主要)?(?:围绕|聚焦|剖析|分析|解读|讲述|讲解|介绍|探讨|讨论)?[：:\s，,。]*",
    ]
    # 允许连续清洗多层前缀，如“本视频主要分析：视频剖析……”
    for _ in range(3):
        before = cleaned
        for pat in meta_lead_patterns:
            cleaned = re.sub(pat, "", cleaned)
        if cleaned == before:
            break
    return cleaned


def _shorten_without_cut(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text

    punctuation = [
        m.end()
        for m in re.finditer(r"[。！？；.!?]", text)
        if m.end() <= max_chars
    ]
    if punctuation:
        return text[: punctuation[-1]]

    punctuation = [
        m.end()
        for m in re.finditer(r"[，、,]", text)
        if m.end() <= max_chars
    ]
    if punctuation:
        return text[: punctuation[-1]]

    return text


def _limit_topics(line: str, max_count: int) -> str:
    tags = re.findall(r"#\S+", line)
    if not tags:
        tags = [t for t in line.split() if t.strip()]
    return " ".join(tags[:max_count])


def _call_deepseek(api_key: str, intro: str, topics: str, max_chars: int) -> Tuple[str, str]:
    prompt = (
        f"请阅读以下简介与话题，生成两条输出：\n"
        f"1. 中文简介摘要，不超过{max_chars}个字。直接回复，不要任何解释。"
        f"不要使用“本视频/该视频/此视频/这期视频/视频剖析/视频分析/视频解读/本文”等自指或导语词，"
        f"直接陈述核心观点。\n"
        f"2. 中文话题串，最多{TOPIC_MAX}个，话题用空格分隔，保留#号。\n\n"
        f"【简介】\n{intro}\n\n【话题】\n{topics}\n"
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "你是一名精简内容的中文编辑"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }

    session = requests.Session()
    session.trust_env = False
    session.proxies = {}

    resp = session.post(API_URL, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"].strip()

    summary = ""
    topics_line = ""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("简介"):
            summary = re.sub(r"^简介[:：]\s*", "", stripped)
        elif stripped.startswith("话题"):
            topics_line = re.sub(r"^话题[:：]\s*", "", stripped)

    if not summary:
        summary = content.splitlines()[0]
    if not topics_line:
        topics_line = " ".join(re.findall(r"#\S+", content))

    summary = _shorten_without_cut(_clean_text(summary), max_chars)
    topics_line = _limit_topics(_clean_text(topics_line), TOPIC_MAX)
    return summary, topics_line


def _fallback_summary(intro: str, topics: str, max_chars: int) -> Tuple[str, str]:
    intro_clean = re.sub(r"\s+", "", intro)
    summary = _shorten_without_cut(_clean_text(intro_clean), max_chars)

    topics_tokens = []
    for token in topics.split():
        clean = token.strip()
        if clean:
            topics_tokens.append(clean)
    # 限制最多 N 个话题
    topics_tokens = topics_tokens[:TOPIC_MAX]
    topics_str = ""
    current = []
    total_length = 0
    for token in topics_tokens:
        token_len = len(token)
        if total_length == 0:
            candidate_len = token_len
        else:
            candidate_len = total_length + 1 + token_len
        if total_length != 0 and candidate_len > max_chars:
            break
        current.append(token)
        total_length = candidate_len

    topics_str = _clean_text(" ".join(current))
    return summary, topics_str


def generate_summary_and_topics(max_chars: int = MAX_CHARS) -> Tuple[str, str]:
    """返回 (简介摘要, 话题串)。
    要求必须调用 DeepSeek 生成；若未配置或调用失败，则直接报错，不做本地兜底。
    """

    latest = _latest_markdown()
    intro, topics = _extract_sections(latest.read_text(encoding="utf-8"))
    if not intro:
        raise ValueError("未在文稿中找到 '## 简介' 段落")
    if not topics:
        raise ValueError("未在文稿中找到 '## 话题' 段落")

    api_key = _load_api_key()
    if not api_key:
        raise RuntimeError(f"未配置 DeepSeek API Key，请在 {CONFIG_PATH} 中设置 DEEPSEEK_API_KEY")

    # 仅走 AI 路径；失败即抛错
    return _call_deepseek(api_key, intro, topics, max_chars)


def generate_combined_string(max_chars: int = MAX_CHARS) -> str:
    summary, topics = generate_summary_and_topics(max_chars=max_chars)
    topics = topics.strip()
    if not topics:
        return summary
    if topics.startswith("#"):
        return f"{summary}{topics}"
    return f"{summary} {topics}"


__all__ = [
    "generate_summary_and_topics",
    "generate_combined_string",
]
