"""从最新文稿生成适合小红书视频笔记的标题摘要与话题。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional, Tuple

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config.json"
DEFAULT_CONTENT_DIR = REPO_ROOT / "2.sunrich"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"


def _load_api_key() -> Optional[str]:
    """优先读取环境变量，随后兼容仓库现有 config.json。"""
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"].strip()
    if not CONFIG_PATH.exists():
        return None
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = config.get("DEEPSEEK_API_KEY")
    return str(value).strip() if value else None


def _latest_markdown(content_dir: Path = DEFAULT_CONTENT_DIR) -> Path:
    """按文件名前缀的数字选取最新文稿，避免仅靠修改时间误选旧稿。"""
    numbered: list[tuple[int, Path]] = []
    for path in content_dir.glob("*.md"):
        match = re.match(r"^(\d+)", path.stem)
        if match:
            numbered.append((int(match.group(1)), path))
    if not numbered:
        raise FileNotFoundError(f"未在 {content_dir} 找到以数字编号开头的 Markdown 文稿")
    return max(numbered, key=lambda item: item[0])[1]


def _extract_sections(markdown: str) -> tuple[str, str]:
    intro_match = re.search(r"##\s*简介\s*\n(.*?)(?:\n##|\Z)", markdown, re.S)
    topics_match = re.search(r"##\s*话题\s*\n(.*?)(?:\n##|\Z)", markdown, re.S)
    intro = intro_match.group(1).strip() if intro_match else ""
    topics_block = topics_match.group(1).strip() if topics_match else ""

    topics: list[str] = []
    for line in topics_block.splitlines():
        topic = line.strip().lstrip("- ").strip()
        if not topic:
            continue
        topics.append(topic if topic.startswith("#") else f"#{topic}")
    return intro, " ".join(topics)


def _clean_text(value: str) -> str:
    value = value.strip()
    for pattern in (
        r"^\s*[0-9]+\s*[\)）\.·、:：]\s*",
        r"^\s*[（(]?[0-9]+[)）]\s*",
        r"^\s*[-•·]\s*",
        r"^\s*(?:简介|话题|摘要)[:：]\s*",
    ):
        value = re.sub(pattern, "", value)
    return value.strip()


def _shorten_at_boundary(value: str, max_chars: int) -> str:
    value = re.sub(r"\s+", "", value).strip()
    if len(value) <= max_chars:
        return value
    boundaries = [
        match.end()
        for match in re.finditer(r"[。！？；.!?，、,]", value)
        if match.end() <= max_chars
    ]
    return value[: boundaries[-1] if boundaries else max_chars].strip()


def _limit_topics(value: str, max_count: int = 5) -> str:
    topics = re.findall(r"#\S+", value)
    return " ".join(topics[:max_count])


def generate_summary_and_topics(max_chars: int = 50) -> Tuple[str, str]:
    """调用用户配置的 DeepSeek，返回 ``(摘要, 话题串)``。"""
    latest = _latest_markdown()
    intro, topics = _extract_sections(latest.read_text(encoding="utf-8"))
    if not intro:
        raise ValueError(f"{latest.name} 中未找到 '## 简介' 段落")
    if not topics:
        raise ValueError(f"{latest.name} 中未找到 '## 话题' 段落")

    api_key = _load_api_key()
    if not api_key:
        raise RuntimeError(
            "未配置 DEEPSEEK_API_KEY。请在环境变量或仓库根目录 config.json 中配置，"
            "或在发布时传入 --title/--desc 并使用 --no-auto-desc。"
        )

    prompt = (
        "请根据以下内容返回严格两行：\n"
        f"第一行是自然、直接的中文摘要，不超过 {max_chars} 个字，不要写‘本视频’等导语；\n"
        "第二行是最多 5 个中文话题，话题以空格分隔且每个保留 #。\n\n"
        f"【简介】\n{intro}\n\n【话题】\n{topics}"
    )
    response = requests.post(
        DEEPSEEK_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": "你是一名精简、准确的中文内容编辑。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
        },
        timeout=45,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"].strip()

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    summary = _clean_text(lines[0] if lines else "")
    topics_line = next((line for line in lines if "#" in line), "")
    topics_line = re.sub(r"^话题[:：]\s*", "", topics_line)

    summary = _shorten_at_boundary(summary, max_chars)
    topics_line = _limit_topics(topics_line)
    if not summary or not topics_line:
        raise RuntimeError("DeepSeek 返回的摘要或话题不完整，已停止发布。")
    return summary, topics_line
