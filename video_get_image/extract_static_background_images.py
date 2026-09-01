#!/usr/bin/env python3
"""
从视频或 YouTube 链接中提取静止至少指定时长的背景图片素材。

适用画面：背景视频持续运动，而前景图片在固定位置静止展示。
脚本只保存局部稳定且周边仍有明显运动的区域，因此不会把整段
循环背景误判为图片素材。

示例：
    python3 video_get_image/extract_static_background_images.py input.mp4
    python3 video_get_image/extract_static_background_images.py \
        'https://www.youtube.com/watch?v=nLyNfTbbAxQ' \
        --output-dir test/背景图片提取/素材 --min-static-seconds 5

依赖：opencv-python、numpy、yt-dlp（仅 URL 输入需要）、ffmpeg（yt-dlp 合并时需要）。
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np


DEFAULT_SAMPLE_FPS = 4.0
DEFAULT_MIN_STATIC_SECONDS = 5.0
DEFAULT_DIFF_THRESHOLD = 5
DEFAULT_MIN_AREA_RATIO = 0.05
DEFAULT_MIN_BACKGROUND_MOTION = 0.03
DEFAULT_ANALYSIS_WIDTH = 960
DEFAULT_EDGE_MARGIN_RATIO = 0.04
DEFAULT_MOTION_WINDOW_SECONDS = 3.0


@dataclass
class Candidate:
    """一帧中满足静态条件的图片候选区域。"""

    box: tuple[int, int, int, int]
    frame: np.ndarray
    timestamp: float
    sharpness: float


@dataclass
class Track:
    """同一静态图片在多个抽样帧上的跟踪记录。"""

    box: tuple[int, int, int, int]
    start_time: float
    last_time: float
    best_frame: np.ndarray
    best_sharpness: float
    samples: int = 1
    last_seen_index: int = 0

    def update(self, candidate: Candidate, sample_index: int) -> None:
        self.last_time = candidate.timestamp
        self.samples += 1
        self.last_seen_index = sample_index
        self.box = candidate.box
        if candidate.sharpness > self.best_sharpness:
            self.best_frame = candidate.frame
            self.best_sharpness = candidate.sharpness


def format_timestamp(seconds: float) -> str:
    """将秒数格式化为适合文件名和元数据的时间字符串。"""
    whole_seconds = max(0, int(seconds))
    return f"{whole_seconds // 60:02d}m{whole_seconds % 60:02d}s"


def is_youtube_url(value: str) -> bool:
    return "youtube.com/" in value or "youtu.be/" in value


def get_js_runtime_args() -> list[str]:
    """为 yt-dlp 启用本机 Node.js，提升 YouTube 格式解析成功率。"""
    node_path = shutil.which("node")
    if node_path:
        return ["--js-runtimes", f"node:{node_path}", "--remote-components", "ejs:github"]
    return []


def download_youtube_video(url: str, output_dir: Path, proxy: str | None, max_height: int) -> Path:
    """下载用于分析的视频流，依次尝试 Web、Android VR、mweb 客户端。"""
    if not shutil.which("yt-dlp"):
        raise RuntimeError("未找到 yt-dlp，无法下载 YouTube 视频。")

    download_dir = output_dir / "下载视频"
    download_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(download_dir / "%(id)s.%(ext)s")
    format_selector = f"bestvideo[height<={max_height}][ext=mp4]/best[height<={max_height}]"
    shared_args = [
        "yt-dlp",
        "--no-playlist",
        "--no-continue",
        "--retries",
        "10",
        "--fragment-retries",
        "10",
        "--file-access-retries",
        "3",
        "--http-chunk-size",
        "5M",
        *get_js_runtime_args(),
    ]
    if proxy:
        shared_args.extend(["--proxy", proxy])

    attempts = ("web", "android_vr", "mweb")
    errors = []
    for client in attempts:
        command = [
            *shared_args,
            "--extractor-args",
            f"youtube:player_client={client}",
            "-f",
            format_selector,
            "-o",
            output_template,
            url,
        ]
        print(f"下载尝试：{client} 客户端")
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode == 0:
            files = sorted(
                path
                for path in download_dir.iterdir()
                if path.is_file() and path.suffix.lower() in {".mp4", ".webm", ".mkv"}
            )
            if files:
                print(f"下载完成：{files[-1]}")
                return files[-1]
        error_text = (result.stderr or result.stdout).strip().splitlines()
        errors.append(f"{client}: {error_text[-1] if error_text else '未知下载错误'}")

    raise RuntimeError("YouTube 视频下载失败：" + "；".join(errors))


def resize_for_analysis(frame: np.ndarray, analysis_width: int) -> tuple[np.ndarray, float]:
    """缩小大画面以降低检测成本，同时返回由分析图到原图的缩放比例。"""
    height, width = frame.shape[:2]
    if width <= analysis_width:
        return frame, 1.0
    scale = analysis_width / width
    resized = cv2.resize(frame, (analysis_width, round(height * scale)), interpolation=cv2.INTER_AREA)
    return resized, 1.0 / scale


def box_iou(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> float:
    """计算两个矩形框的交并比。"""
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    overlap = max(0, right - left) * max(0, bottom - top)
    if not overlap:
        return 0.0
    return overlap / (aw * ah + bw * bh - overlap)


def crop_original_frame(
    frame: np.ndarray,
    analysis_box: tuple[int, int, int, int],
    inverse_scale: float,
    padding: int = 16,
) -> np.ndarray:
    """按分析图坐标从原始分辨率帧中裁切候选图片。"""
    x, y, width, height = analysis_box
    source_height, source_width = frame.shape[:2]
    left = max(0, round(x * inverse_scale) - padding)
    top = max(0, round(y * inverse_scale) - padding)
    right = min(source_width, round((x + width) * inverse_scale) + padding)
    bottom = min(source_height, round((y + height) * inverse_scale) + padding)
    return frame[top:bottom, left:right].copy()


def trim_light_card(image: np.ndarray, expected_height: int | None = None) -> np.ndarray:
    """裁出候选区域中明显的浅色矩形图片卡片，否则保留原图。"""
    rectangular_card = trim_rectangular_card(image, expected_height)
    if rectangular_card is not None:
        return rectangular_card

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    # 地图、图表等前景素材常以白色或浅灰色画板展示；饱和度限制能
    # 排除天空等亮色循环背景。
    light_mask = cv2.inRange(hsv, (0, 0, 200), (180, 75, 255))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    light_mask = cv2.morphologyEx(light_mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _hierarchy = cv2.findContours(light_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    image_height, image_width = image.shape[:2]
    image_area = image_height * image_width
    best_box = None
    best_area = 0
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        rectangle_area = width * height
        aspect_ratio = width / height
        if rectangle_area / image_area < 0.2 or not 0.7 <= aspect_ratio <= 3.0:
            continue
        if rectangle_area > best_area:
            best_box = (x, y, width, height)
            best_area = rectangle_area

    if best_box is None:
        return image
    x, y, width, height = best_box
    return image[y : y + height, x : x + width].copy()


def trim_rectangular_card(image: np.ndarray, expected_height: int | None = None) -> np.ndarray | None:
    """通过长直线检测画面内嵌的矩形图片边界。"""
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 40, 100)
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=max(40, round(min(width, height) * 0.25)),
        minLineLength=max(50, round(min(width, height) * 0.35)),
        maxLineGap=12,
    )
    if lines is None:
        return None

    vertical_positions = []
    horizontal_lines = []
    for x1, y1, x2, y2 in lines[:, 0]:
        line_width = abs(int(x2) - int(x1))
        line_height = abs(int(y2) - int(y1))
        if line_width <= 3 and line_height >= height * 0.35:
            vertical_positions.append(round((int(x1) + int(x2)) / 2))
        if line_height <= 3 and line_width >= width * 0.35:
            left, right = sorted((int(x1), int(x2)))
            horizontal_lines.append((left, round((int(y1) + int(y2)) / 2), right))

    # 在部分素材中，左右边缘与背景对比不足，无法形成完整矩形。
    # 这时顶部横线仍很清晰，可配合已检测到的稳定区高度精确裁出卡片。
    if expected_height is not None:
        top_lines = [line for line in horizontal_lines if line[1] <= height * 0.4]
        if top_lines:
            left, top, right = max(top_lines, key=lambda line: line[2] - line[0])
            card_width = right - left
            bottom = top + expected_height
            if (
                bottom <= height
                and card_width >= width * 0.35
                and 0.7 <= card_width / expected_height <= 3.0
            ):
                return image[top:bottom, left : right + 1].copy()

    if len(vertical_positions) < 2 or len(horizontal_lines) < 2:
        return None
    left, right = min(vertical_positions), max(vertical_positions)
    top = min(line[1] for line in horizontal_lines)
    bottom = max(line[1] for line in horizontal_lines)
    card_width = right - left
    card_height = bottom - top
    if (
        card_width < width * 0.4
        or card_height < height * 0.4
        or card_width >= width * 0.99
        or card_height >= height * 0.99
        or not 0.7 <= card_width / card_height <= 3.0
    ):
        return None
    return image[top : bottom + 1, left : right + 1].copy()


def image_hash(image: np.ndarray) -> np.ndarray:
    """生成简单 dHash，用于跨片段排除重复图片。"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    return (resized[:, 1:] > resized[:, :-1]).reshape(-1)


def is_duplicate(image: np.ndarray, saved_hashes: list[np.ndarray], max_distance: int = 10) -> bool:
    """判断候选图与已保存图片是否近似相同。"""
    candidate_hash = image_hash(image)
    return any(int(np.count_nonzero(candidate_hash != saved_hash)) <= max_distance for saved_hash in saved_hashes)


def detect_candidates(
    stable_runs: np.ndarray,
    changed_mask: np.ndarray,
    frame: np.ndarray,
    original_frame: np.ndarray,
    inverse_scale: float,
    timestamp: float,
    min_samples: int,
    min_area_ratio: float,
    min_background_motion: float,
    edge_margin_ratio: float,
    trim_cards: bool,
) -> list[Candidate]:
    """从连续稳定像素中找出受动态背景包围的图片区域。"""
    stable_mask = (stable_runs >= min_samples).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    stable_mask = cv2.morphologyEx(stable_mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    stable_mask = cv2.morphologyEx(stable_mask, cv2.MORPH_OPEN, kernel, iterations=1)

    component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(stable_mask)
    frame_height, frame_width = stable_mask.shape
    frame_area = frame_height * frame_width
    horizontal_margin = round(frame_width * edge_margin_ratio)
    vertical_margin = round(frame_height * edge_margin_ratio)
    candidates = []

    for component_index in range(1, component_count):
        x, y, width, height, area = (int(value) for value in stats[component_index])
        area_ratio = area / frame_area
        if area_ratio < min_area_ratio or area_ratio > 0.88:
            continue
        if width < 48 or height < 48:
            continue
        # 循环背景通常延伸到画面边缘；素材图片通常以独立画面形式
        # 放在中部。排除贴边区域可避免将天空、城市等背景局部导出。
        if (
            x <= horizontal_margin
            or y <= vertical_margin
            or x + width >= frame_width - horizontal_margin
            or y + height >= frame_height - vertical_margin
        ):
            continue
        if area / (width * height) < 0.7:
            continue

        # 避免把单色边框、黑边等没有图片纹理的区域当作素材。
        patch_gray = cv2.cvtColor(frame[y : y + height, x : x + width], cv2.COLOR_BGR2GRAY)
        sharpness = float(cv2.Laplacian(patch_gray, cv2.CV_64F).var())
        if sharpness < 20.0:
            continue

        outside_mask = np.ones_like(changed_mask, dtype=bool)
        outside_mask[y : y + height, x : x + width] = False
        outside_pixel_count = int(np.count_nonzero(outside_mask))
        if outside_pixel_count == 0:
            continue
        background_motion = float(np.count_nonzero(changed_mask[outside_mask])) / outside_pixel_count
        if background_motion < min_background_motion:
            continue

        image = crop_original_frame(original_frame, (x, y, width, height), inverse_scale)
        if trim_cards:
            image = trim_light_card(image, round(height * inverse_scale))
        if image.size:
            candidates.append(Candidate((x, y, width, height), image, timestamp, sharpness))

    return candidates


def iter_sampled_frames(video_path: Path, sample_fps: float) -> Iterator[tuple[int, float, np.ndarray]]:
    """按指定频率读取视频帧，保留原始分辨率用于最终导出。"""
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"无法打开视频：{video_path}")

    source_fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    frame_interval = max(1, round(source_fps / sample_fps))
    frame_index = 0
    sample_index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frame_index += 1
            if frame_index % frame_interval:
                continue
            sample_index += 1
            yield sample_index, frame_index / source_fps, frame
    finally:
        capture.release()


def save_track(
    track: Track,
    output_dir: Path,
    output_index: int,
    saved_hashes: list[np.ndarray],
    metadata: list[dict],
    min_static_seconds: float,
) -> bool:
    """对完成的静态区间去重并导出最佳清晰度的素材图。"""
    duration = track.last_time - track.start_time
    if duration + 0.01 < min_static_seconds:
        return False
    if is_duplicate(track.best_frame, saved_hashes):
        print(f"跳过重复图片：{format_timestamp(track.start_time)}")
        return False

    filename = f"{output_index:03d}_{format_timestamp(track.start_time)}_{duration:.1f}s.png"
    output_path = output_dir / filename
    if not cv2.imwrite(str(output_path), track.best_frame):
        raise RuntimeError(f"无法写入图片：{output_path}")

    saved_hashes.append(image_hash(track.best_frame))
    metadata.append(
        {
            "file": filename,
            "start_seconds": round(track.start_time, 3),
            "end_seconds": round(track.last_time, 3),
            "duration_seconds": round(duration, 3),
            "samples": track.samples,
            "analysis_box": list(track.box),
        }
    )
    print(f"保存图片：{output_path.name}，静止 {duration:.1f} 秒")
    return True


def extract_static_images(
    video_path: Path,
    output_dir: Path,
    sample_fps: float = DEFAULT_SAMPLE_FPS,
    min_static_seconds: float = DEFAULT_MIN_STATIC_SECONDS,
    diff_threshold: int = DEFAULT_DIFF_THRESHOLD,
    min_area_ratio: float = DEFAULT_MIN_AREA_RATIO,
    min_background_motion: float = DEFAULT_MIN_BACKGROUND_MOTION,
    edge_margin_ratio: float = DEFAULT_EDGE_MARGIN_RATIO,
    trim_cards: bool = True,
    motion_window_seconds: float = DEFAULT_MOTION_WINDOW_SECONDS,
    max_track_gap_seconds: float = 3.0,
    analysis_width: int = DEFAULT_ANALYSIS_WIDTH,
) -> list[dict]:
    """提取静态图片并返回每张图片的时间与区域元数据。"""
    if sample_fps <= 0 or min_static_seconds <= 0:
        raise ValueError("采样频率和静止时间必须大于 0。")

    output_dir.mkdir(parents=True, exist_ok=True)
    min_samples = max(1, math.ceil(sample_fps * min_static_seconds))
    stable_runs = None
    frame_history: list[np.ndarray] = []
    motion_window_samples = max(1, round(sample_fps * motion_window_seconds))
    max_track_gap_samples = max(1, round(sample_fps * max_track_gap_seconds))
    tracks: list[Track] = []
    saved_hashes: list[np.ndarray] = []
    metadata: list[dict] = []
    sample_count = 0

    for sample_index, timestamp, original_frame in iter_sampled_frames(video_path, sample_fps):
        sample_count = sample_index
        frame, inverse_scale = resize_for_analysis(original_frame, analysis_width)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)

        frame_history.append(gray)
        if len(frame_history) <= motion_window_samples:
            if stable_runs is None:
                stable_runs = np.zeros_like(gray, dtype=np.uint16)
            continue
        if len(frame_history) > motion_window_samples + 1:
            frame_history.pop(0)

        if stable_runs is None:
            stable_runs = np.zeros_like(gray, dtype=np.uint16)
            continue

        # 与约一秒前的画面对比，避免缓慢的循环背景在相邻帧中被误判为静止。
        delta = cv2.absdiff(gray, frame_history[0])
        static_now = delta <= diff_threshold
        changed_mask = ~static_now
        stable_runs = np.where(static_now, np.minimum(stable_runs + 1, 65535), 0).astype(np.uint16)

        candidates = detect_candidates(
            stable_runs,
            changed_mask,
            frame,
            original_frame,
            inverse_scale,
            timestamp,
            min_samples,
            min_area_ratio,
            min_background_motion,
            edge_margin_ratio,
            trim_cards,
        )

        matched_tracks: set[int] = set()
        for candidate in candidates:
            match_index = next(
                (
                    index
                    for index, track in enumerate(tracks)
                    if index not in matched_tracks and box_iou(track.box, candidate.box) >= 0.55
                ),
                None,
            )
            if match_index is None:
                tracks.append(
                    Track(
                        box=candidate.box,
                        # 候选区要先累计足够多的稳定采样帧才会被检测到；
                        # 将轨迹回溯至这段已确认稳定区间的开头，避免漏算前段时长。
                        start_time=max(0.0, timestamp - min_samples / sample_fps),
                        last_time=timestamp,
                        best_frame=candidate.frame,
                        best_sharpness=candidate.sharpness,
                        last_seen_index=sample_index,
                    )
                )
                matched_tracks.add(len(tracks) - 1)
            else:
                tracks[match_index].update(candidate, sample_index)
                matched_tracks.add(match_index)

        retained_tracks = []
        for track in tracks:
            # 场景转场、字幕压住边界时，稳定像素区域会短暂断开。
            # 在小时间窗口内保留轨迹，防止同一张素材被重复导出。
            if sample_index - track.last_seen_index > max_track_gap_samples:
                if save_track(track, output_dir, len(metadata) + 1, saved_hashes, metadata, min_static_seconds):
                    pass
            else:
                retained_tracks.append(track)
        tracks = retained_tracks
    for track in tracks:
        save_track(track, output_dir, len(metadata) + 1, saved_hashes, metadata, min_static_seconds)

    metadata_path = output_dir / "提取结果.json"
    metadata_path.write_text(
        json.dumps(
            {
                "source_video": str(video_path),
                "sample_fps": sample_fps,
                "min_static_seconds": min_static_seconds,
                "sampled_frames": sample_count,
                "images": metadata,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"完成：扫描 {sample_count} 个采样帧，导出 {len(metadata)} 张图片。")
    print(f"结果清单：{metadata_path}")
    return metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="提取动态背景视频中持续静止的图片素材。")
    parser.add_argument("source", help="本地视频文件路径或 YouTube 视频链接。")
    parser.add_argument("--output-dir", type=Path, default=Path("test/背景图片提取/素材"), help="图片输出目录。")
    parser.add_argument("--sample-fps", type=float, default=DEFAULT_SAMPLE_FPS, help="检测采样频率，默认每秒 4 帧。")
    parser.add_argument("--min-static-seconds", type=float, default=DEFAULT_MIN_STATIC_SECONDS, help="图片最短静止时长，默认 5 秒。")
    parser.add_argument("--diff-threshold", type=int, default=DEFAULT_DIFF_THRESHOLD, help="跨窗口像素差阈值，默认 5。")
    parser.add_argument("--min-area-ratio", type=float, default=DEFAULT_MIN_AREA_RATIO, help="候选图片最小画面占比，默认 0.05。")
    parser.add_argument("--min-background-motion", type=float, default=DEFAULT_MIN_BACKGROUND_MOTION, help="候选区域外最小运动像素比例，默认 0.03。")
    parser.add_argument("--edge-margin-ratio", type=float, default=DEFAULT_EDGE_MARGIN_RATIO, help="候选图片距画面边缘的最小比例，默认 0.04。")
    parser.add_argument("--no-trim-light-card", action="store_true", help="保留候选区的完整画面，不自动裁出浅色图片卡片。")
    parser.add_argument("--motion-window-seconds", type=float, default=DEFAULT_MOTION_WINDOW_SECONDS, help="用于判断背景运动的帧间隔秒数，默认 3。")
    parser.add_argument("--max-track-gap-seconds", type=float, default=3.0, help="同一素材检测短暂中断的最大容忍秒数，默认 3。")
    parser.add_argument("--analysis-width", type=int, default=DEFAULT_ANALYSIS_WIDTH, help="检测时的最大画面宽度，默认 960。")
    parser.add_argument("--proxy", default=os.environ.get("YOUTUBE_PROXY"), help="下载 YouTube 时使用的代理，例如 http://127.0.0.1:7890。")
    parser.add_argument("--download-height", type=int, default=720, help="URL 下载分析视频的最大高度，默认 720。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        source = args.source.strip()
        if is_youtube_url(source):
            video_path = download_youtube_video(source, args.output_dir, args.proxy, args.download_height)
        else:
            video_path = Path(source).expanduser().resolve()
            if not video_path.is_file():
                raise FileNotFoundError(f"视频文件不存在：{video_path}")

        extract_static_images(
            video_path,
            args.output_dir,
            sample_fps=args.sample_fps,
            min_static_seconds=args.min_static_seconds,
            diff_threshold=args.diff_threshold,
            min_area_ratio=args.min_area_ratio,
            min_background_motion=args.min_background_motion,
            edge_margin_ratio=args.edge_margin_ratio,
            trim_cards=not args.no_trim_light_card,
            motion_window_seconds=args.motion_window_seconds,
            max_track_gap_seconds=args.max_track_gap_seconds,
            analysis_width=args.analysis_width,
        )
        return 0
    except Exception as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
