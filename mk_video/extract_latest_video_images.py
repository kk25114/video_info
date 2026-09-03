#!/usr/bin/env python3
"""从 wrap_sunrich.sh 最近处理的视频中提取静止图片素材。

手动执行：
    python3 mk_video/extract_latest_video_images.py

脚本读取 ``2.sunrich/processed_videos.log`` 的最后一个视频 ID，下载该
视频的高画质版本，再使用 video_get_image 的静态图片检测器提取图片。
最终图片写入 ``mk_video/images``，文件名为：

    <图片出现时间>-<源视频时长的80%>.png

例如，图片在源视频 5 秒处、源视频时长为 10 分钟时，文件名为
``005-0800.png``。13 分钟视频的 80% 是 624 秒，即 ``1024``（10 分
24 秒）。文件名中的第二段用于兼容 mk_video/build.py 的比例换算。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "mk_video" / "images"
DEFAULT_PROCESSED_LOG = PROJECT_ROOT / "2.sunrich" / "processed_videos.log"
DEFAULT_WORK_DIR = Path("/tmp/video_info_latest_video_images")
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def load_extractor_module():
    """加载已有提取器，避免手动脚本复制一套下载和识别逻辑。"""
    module_path = PROJECT_ROOT / "video_get_image" / "extract_static_background_images.py"
    spec = importlib.util.spec_from_file_location("static_background_image_extractor", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载图片提取器：{module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EXTRACTOR = load_extractor_module()


def video_id_to_url(video_id: str) -> str:
    """将 YouTube 视频 ID 转为标准链接。"""
    if not VIDEO_ID_PATTERN.fullmatch(video_id):
        raise ValueError(f"无效的 YouTube 视频 ID：{video_id}")
    return f"https://www.youtube.com/watch?v={video_id}"


def extract_video_id(value: str) -> str | None:
    """从 YouTube 链接或纯视频 ID 中提取 11 位视频 ID。"""
    value = value.strip()
    if VIDEO_ID_PATTERN.fullmatch(value):
        return value
    match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})(?:[&#]|$)", value)
    if match:
        return match.group(1)
    match = re.search(r"https?://(?:www\.)?youtu\.be/([A-Za-z0-9_-]{11})(?:[?&#/]|$)", value)
    if match:
        return match.group(1)
    return None


def get_latest_video_url(processed_log: Path) -> str:
    """从成功记录中读取最后一个有效视频 ID。"""
    if not processed_log.is_file():
        raise FileNotFoundError(
            f"找不到成功记录：{processed_log}\n"
            "请先执行 wrap_sunrich.sh，让它获取至少一个视频。"
        )

    for raw_line in reversed(processed_log.read_text(encoding="utf-8").splitlines()):
        value = raw_line.strip()
        if not value:
            continue
        video_id = extract_video_id(value)
        if video_id:
            return video_id_to_url(video_id)

    raise ValueError(f"成功记录中没有有效的 YouTube 视频 ID：{processed_log}")


def probe_duration(video_path: Path) -> float:
    """读取视频时长（秒）。"""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"无法读取视频时长：{video_path}\n{result.stderr.strip()}")
    try:
        duration = float(result.stdout.strip())
    except ValueError as error:
        raise RuntimeError(f"ffprobe 返回了无效的视频时长：{result.stdout!r}") from error
    if duration <= 0:
        raise RuntimeError(f"视频时长无效：{duration}")
    return duration


def format_timestamp_code(seconds: float, *, total: bool = False) -> str:
    """将秒数转为 build.py 使用的 MMSS 数字格式。

    图片时间保留最短形式：5 秒为 ``005``、2 分 11 秒为 ``211``；
    总时长始终补足四位：8 分钟为 ``0800``。
    """
    whole_seconds = max(0, int(seconds))
    minutes, remainder = divmod(whole_seconds, 60)
    if minutes > 99:
        raise ValueError("当前文件名格式最多支持 99 分 59 秒的视频。")
    if total:
        return f"{minutes:02d}{remainder:02d}"
    return f"{minutes}{remainder:02d}"


def eighty_percent_seconds(duration: float) -> int:
    """计算视频时长的 80%，以完整秒向下取整便于写入文件名。"""
    return max(1, int(duration * 0.8))


def build_image_filename(start_seconds: float, source_duration: float) -> str:
    """生成单张图片的兼容文件名。"""
    start_code = format_timestamp_code(start_seconds)
    duration_code = format_timestamp_code(eighty_percent_seconds(source_duration), total=True)
    return f"{start_code}-{duration_code}.png"


def clean_output_images(output_dir: Path) -> int:
    """只清理目标目录中的图片，避免把目录里的其他文件误删。"""
    removed = 0
    for path in output_dir.iterdir():
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            path.unlink()
            removed += 1
    return removed


def unique_output_path(output_dir: Path, filename: str, ordinal: int) -> Path:
    """处理同一秒检测到多张图片时的文件名冲突。"""
    path = output_dir / filename
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    candidate = output_dir / f"{stem}_{ordinal:03d}{suffix}"
    counter = ordinal
    while candidate.exists():
        counter += 1
        candidate = output_dir / f"{stem}_{counter:03d}{suffix}"
    return candidate


def extract_latest_video_images(
    *,
    url: str | None = None,
    processed_log: Path = DEFAULT_PROCESSED_LOG,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    work_dir: Path = DEFAULT_WORK_DIR,
    proxy: str | None = None,
    download_height: int = 1080,
    sample_fps: float = 2.0,
    min_static_seconds: float = 5.0,
    analysis_width: int = 960,
    keep_existing: bool = False,
) -> list[dict]:
    """下载最新视频并把识别到的图片输出到目标目录。"""
    source_url = url.strip() if url else get_latest_video_url(processed_log)
    video_id = extract_video_id(source_url)
    if not video_id:
        raise ValueError(f"无法从链接中解析 YouTube 视频 ID：{source_url}")
    source_url = video_id_to_url(video_id)

    output_dir.mkdir(parents=True, exist_ok=True)
    work_video_dir = work_dir / video_id
    work_output_dir = work_video_dir / "提取结果"
    work_output_dir.mkdir(parents=True, exist_ok=True)

    print(f"最新视频：{source_url}")
    print(f"工作目录：{work_video_dir}")
    video_path = EXTRACTOR.download_youtube_video(
        source_url,
        work_video_dir,
        proxy,
        download_height,
    )
    source_duration = probe_duration(video_path)
    source_resolution = EXTRACTOR.probe_video_resolution(video_path)
    resolution_text = (
        f"{source_resolution[0]}x{source_resolution[1]}" if source_resolution else "未知"
    )
    print(f"源视频：{resolution_text}，时长 {source_duration / 60:.2f} 分钟")
    print(
        f"文件名中的 80% 时长：{eighty_percent_seconds(source_duration)} 秒 "
        f"（{format_timestamp_code(eighty_percent_seconds(source_duration), total=True)}）"
    )

    # 旧的检测结果只属于同一个工作目录，不参与本次输出。
    for path in work_output_dir.iterdir():
        if path.is_file() and (path.suffix.lower() in IMAGE_SUFFIXES or path.name == "提取结果.json"):
            path.unlink()

    metadata = EXTRACTOR.extract_static_images(
        video_path,
        work_output_dir,
        sample_fps=sample_fps,
        min_static_seconds=min_static_seconds,
        analysis_width=analysis_width,
        source_resolution=source_resolution,
    )

    if not metadata:
        print("未检测到符合条件的静止图片，目标目录未做清理。")
        return []

    if not keep_existing:
        removed = clean_output_images(output_dir)
        if removed:
            print(f"已清理目标目录中的 {removed} 张旧图片。")

    exported: list[dict] = []
    for ordinal, item in enumerate(metadata, start=1):
        source_image = work_output_dir / item["file"]
        if not source_image.is_file():
            print(f"跳过不存在的检测结果：{source_image}")
            continue

        filename = build_image_filename(item.get("start_seconds", 0), source_duration)
        target_path = unique_output_path(output_dir, filename, ordinal)
        shutil.copy2(source_image, target_path)
        exported_item = {
            "file": target_path.name,
            "source_file": item["file"],
            "start_seconds": item.get("start_seconds"),
            "end_seconds": item.get("end_seconds"),
            "duration_seconds": item.get("duration_seconds"),
            "image_resolution": item.get("image_resolution"),
            "source_resolution": list(source_resolution) if source_resolution else None,
        }
        exported.append(exported_item)
        print(f"保存图片：{target_path.name}（{target_path.stat().st_size} bytes）")

    manifest = {
        "source_video": source_url,
        "source_video_file": str(video_path),
        "source_duration_seconds": round(source_duration, 3),
        "eighty_percent_seconds": eighty_percent_seconds(source_duration),
        "images": exported,
    }
    manifest_path = output_dir.parent / "latest_video_images.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"完成：共输出 {len(exported)} 张图片到 {output_dir}")
    print(f"清单：{manifest_path}")
    return exported


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="提取 wrap_sunrich.sh 最新视频中的静止图片素材。")
    parser.add_argument("--url", help="可选：手动指定 YouTube 链接；默认读取 processed_videos.log 最后一条记录。")
    parser.add_argument("--processed-log", type=Path, default=DEFAULT_PROCESSED_LOG, help="成功视频记录文件。")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="图片输出目录。")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR, help="下载和临时识别结果目录。")
    parser.add_argument("--proxy", default=os.environ.get("YOUTUBE_PROXY"), help="YouTube 下载代理。")
    parser.add_argument("--download-height", type=int, default=1080, help="最高下载高度，默认 1080。")
    parser.add_argument("--sample-fps", type=float, default=2.0, help="静止检测采样频率，默认每秒 2 帧。")
    parser.add_argument("--min-static-seconds", type=float, default=5.0, help="图片至少静止的秒数，默认 5 秒。")
    parser.add_argument("--analysis-width", type=int, default=960, help="检测用画面宽度，默认 960；导出仍使用原始帧。")
    parser.add_argument("--keep-existing", action="store_true", help="不清理 images 目录中的旧图片。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        extract_latest_video_images(
            url=args.url,
            processed_log=args.processed_log,
            output_dir=args.output_dir,
            work_dir=args.work_dir,
            proxy=args.proxy,
            download_height=args.download_height,
            sample_fps=args.sample_fps,
            min_static_seconds=args.min_static_seconds,
            analysis_width=args.analysis_width,
            keep_existing=args.keep_existing,
        )
        return 0
    except Exception as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
