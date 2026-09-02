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
# 白底图表、文章截图等卡片通常至少占画面的约 12%。这个分支用于
# 处理视频编码导致前景图片存在轻微像素波动的情况。
DEFAULT_CARD_MIN_AREA_RATIO = 0.12
DEFAULT_CARD_LIGHT_RATIO = 0.55
DEFAULT_CARD_CENTER_TOLERANCE = 0.16


@dataclass
class Candidate:
    """一帧中满足静态条件的图片候选区域。"""

    box: tuple[int, int, int, int]
    frame: np.ndarray
    timestamp: float
    sharpness: float
    # 候选首次出现时，已由多少秒的前后帧对比确认其稳定；用于还原
    # 实际开始时间，而不是从首次被检测到的采样帧开始计时。
    lead_seconds: float = 0.0
    signature: np.ndarray | None = None
    detector: str = "general"


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
    last_signature: np.ndarray | None = None
    detector: str = "general"
    # 轨迹可能先检测到图片的一小块，再检测到完整图片。记录当前最佳
    # 候选面积，导出时优先使用完整框，而不是偶然更清晰的局部框。
    best_area: int = 0

    def __post_init__(self) -> None:
        if self.best_area <= 0:
            self.best_area = self.box[2] * self.box[3]

    def update(self, candidate: Candidate, sample_index: int) -> None:
        self.last_time = candidate.timestamp
        self.samples += 1
        self.last_seen_index = sample_index
        candidate_area = candidate.box[2] * candidate.box[3]
        # 边界抖动时保留原框；只有新候选明显更大，或面积接近但更清晰，
        # 才替换轨迹的 canonical 框和导出帧。
        if (
            candidate_area > self.best_area * 1.02
            or (
                candidate_area >= self.best_area * 0.96
                and candidate.sharpness > self.best_sharpness
            )
        ):
            self.box = candidate.box
            self.best_frame = candidate.frame
            self.best_sharpness = candidate.sharpness
            self.best_area = candidate_area
        if candidate.signature is not None and candidate_area >= self.best_area * 0.90:
            self.last_signature = candidate.signature


@dataclass
class PictureRefinementContext:
    """同一帧中供多个粗候选复用的边界分析数据。"""

    stable: np.ndarray
    integral: np.ndarray
    horizontal_gradient: np.ndarray
    vertical_gradient: np.ndarray
    horizontal_gradient_integral: np.ndarray
    vertical_gradient_integral: np.ndarray
    horizontal_edges: np.ndarray
    vertical_edges: np.ndarray


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


def hash_distance(first: np.ndarray | None, second: np.ndarray | None) -> int | None:
    """计算两个候选图片指纹的差异；任一指纹缺失时不限制匹配。"""
    if first is None or second is None:
        return None
    return int(np.count_nonzero(first != second))


def _block_difference(first_gray: np.ndarray, second_gray: np.ndarray, block_size: int) -> np.ndarray:
    """将逐像素差异压缩为小网格，容忍编码造成的零散噪点。"""
    height, width = first_gray.shape[:2]
    grid_width = max(1, math.ceil(width / block_size))
    grid_height = max(1, math.ceil(height / block_size))
    delta = cv2.absdiff(first_gray, second_gray)
    return cv2.resize(delta, (grid_width, grid_height), interpolation=cv2.INTER_AREA)


def _stable_block_rectangles(
    stable_blocks: np.ndarray,
    block_size: int,
    frame_width: int,
    frame_height: int,
) -> list[tuple[int, int, int, int]]:
    """从稳定网格中找出近似完整的矩形区域。"""
    mask = (stable_blocks.astype(np.uint8) * 255)
    # 关闭少量编码噪声造成的空洞，同时不把相距较远的区域连接起来。
    kernel = np.ones((3, 3), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
    component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask)
    rectangles = []
    for component_index in range(1, component_count):
        grid_x, grid_y, grid_width, grid_height, area = (
            int(value) for value in stats[component_index]
        )
        if grid_width < 3 or grid_height < 3:
            continue
        occupancy = area / (grid_width * grid_height)
        if occupancy < 0.68:
            continue
        x = grid_x * block_size
        y = grid_y * block_size
        width = min(frame_width - x, grid_width * block_size)
        height = min(frame_height - y, grid_height * block_size)
        if width > 0 and height > 0:
            rectangles.append((x, y, width, height))
    return rectangles


def _card_border_box(
    frame: np.ndarray,
    fallback_box: tuple[int, int, int, int],
) -> tuple[tuple[int, int, int, int], int]:
    """用长直线微调卡片边界，并返回检测到的边数。"""
    frame_height, frame_width = frame.shape[:2]
    x, y, width, height = fallback_box
    right = x + width
    bottom = y + height
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 40, 100)
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=max(20, round(min(frame_width, frame_height) * 0.18)),
        minLineLength=max(24, round(min(width, height) * 0.28)),
        maxLineGap=12,
    )
    if lines is None:
        return fallback_box, 0

    horizontal = []
    vertical = []
    for x1, y1, x2, y2 in lines[:, 0]:
        x1, y1, x2, y2 = map(int, (x1, y1, x2, y2))
        line_width = abs(x2 - x1)
        line_height = abs(y2 - y1)
        if line_height <= 3 and line_width >= width * 0.38:
            line_left, line_right = sorted((x1, x2))
            overlap = max(0, min(right, line_right) - max(x, line_left))
            if overlap / max(1, width) >= 0.42:
                horizontal.append((round((y1 + y2) / 2), line_width))
        if line_width <= 3 and line_height >= height * 0.38:
            line_top, line_bottom = sorted((y1, y2))
            overlap = max(0, min(bottom, line_bottom) - max(y, line_top))
            if overlap / max(1, height) >= 0.42:
                vertical.append((round((x1 + x2) / 2), line_height))

    # 宽松距离用于判断是否真的存在矩形边线，严格距离用于避免把卡片内部
    # 的文字横线误当成边界。未能严格确认的边保留稳定区的原始边界。
    broad_x = max(20, round(width * 0.18))
    broad_y = max(20, round(height * 0.18))
    strict_x = max(8, round(width * 0.08))
    strict_y = max(8, round(height * 0.08))

    def nearest(position: int, values: list[tuple[int, int]], tolerance: int) -> int | None:
        choices = [item for item in values if abs(item[0] - position) <= tolerance]
        if not choices:
            return None
        return min(choices, key=lambda item: (abs(item[0] - position), -item[1]))[0]

    border_score = sum(
        (
            nearest(x, vertical, broad_x) is not None,
            nearest(right, vertical, broad_x) is not None,
            nearest(y, horizontal, broad_y) is not None,
            nearest(bottom, horizontal, broad_y) is not None,
        )
    )
    refined_left = nearest(x, vertical, strict_x)
    refined_right = nearest(right, vertical, strict_x)
    refined_top = nearest(y, horizontal, strict_y)
    refined_bottom = nearest(bottom, horizontal, strict_y)
    if refined_left is not None:
        x = refined_left
    if refined_right is not None:
        right = refined_right
    if refined_top is not None:
        y = refined_top
    if refined_bottom is not None:
        bottom = refined_bottom

    x = max(0, min(x, frame_width - 1))
    y = max(0, min(y, frame_height - 1))
    right = max(x + 1, min(right, frame_width))
    bottom = max(y + 1, min(bottom, frame_height))
    return (x, y, right - x, bottom - y), border_score


def _profile_peak_positions(
    profile: np.ndarray,
    start: int,
    end: int,
    maximum: int = 24,
    minimum_distance: int = 3,
) -> list[int]:
    """从一维信号中取分散的强峰位置，供矩形边界组合使用。"""
    start = max(0, start)
    end = min(len(profile), end)
    if end <= start:
        return []

    positions = []
    for position in np.argsort(profile[start:end])[::-1] + start:
        position = int(position)
        if all(abs(position - existing) >= minimum_distance for existing in positions):
            positions.append(position)
            if len(positions) >= maximum:
                break
    return positions


def _mean_integral_area(
    integral: np.ndarray,
    x: int,
    y: int,
    width: int,
    height: int,
) -> float:
    """通过积分图计算矩形均值，越界区域会自动收缩。"""
    image_height, image_width = integral.shape[0] - 1, integral.shape[1] - 1
    left = max(0, min(x, image_width))
    top = max(0, min(y, image_height))
    right = max(left, min(x + width, image_width))
    bottom = max(top, min(y + height, image_height))
    area = (right - left) * (bottom - top)
    if area == 0:
        return 0.0
    total = integral[bottom, right] - integral[top, right] - integral[bottom, left] + integral[top, left]
    return float(total) / area


def build_picture_refinement_context(
    frame: np.ndarray,
    stable_mask: np.ndarray,
) -> PictureRefinementContext | None:
    """预计算一帧的稳定性和视觉边缘，供多个粗候选共享。"""
    frame_height, frame_width = frame.shape[:2]
    if stable_mask.shape != (frame_height, frame_width):
        return None

    # 调用方有时传入 0/255 掩码，有时传入布尔掩码。统一为 0/1，避免
    # 积分图的稳定性阈值因像素取值范围不同而失真。
    stable = (stable_mask > 0).astype(np.uint8)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.int16)
    horizontal_gradient = np.abs(np.diff(gray, axis=1)).astype(np.float32)
    vertical_gradient = np.abs(np.diff(gray, axis=0)).astype(np.float32)
    return PictureRefinementContext(
        stable=stable,
        integral=cv2.integral(stable, sdepth=cv2.CV_64F),
        horizontal_gradient=horizontal_gradient,
        vertical_gradient=vertical_gradient,
        horizontal_gradient_integral=cv2.integral(
            horizontal_gradient, sdepth=cv2.CV_64F
        ),
        vertical_gradient_integral=cv2.integral(vertical_gradient, sdepth=cv2.CV_64F),
        horizontal_edges=horizontal_gradient.mean(axis=0),
        vertical_edges=vertical_gradient.mean(axis=1),
    )


def _rank_boundary_positions(
    positions: set[int],
    edge_profile: np.ndarray,
    stability_profile: np.ndarray,
    target: int,
    lower: int,
    upper: int,
    maximum: int = 9,
) -> list[int]:
    """按边缘强度及与粗候选框的距离筛选候选边界位置。"""
    lower = max(0, lower)
    upper = min(len(edge_profile), upper)
    if upper <= lower:
        return []

    edge_scale = max(1.0, float(np.max(edge_profile[lower:upper])))
    stability_scale = max(1e-6, float(np.max(stability_profile[lower:upper])))
    radius = max(1, upper - lower)
    ranked = []
    for position in positions:
        if not lower <= position < upper:
            continue
        edge_value = edge_profile[min(len(edge_profile) - 1, max(0, position))] / edge_scale
        stability_value = stability_profile[min(len(stability_profile) - 1, max(0, position))] / stability_scale
        distance_bonus = 1.0 - min(1.0, abs(position - target) / radius)
        ranked.append((edge_value + stability_value * 0.9 + distance_bonus * 0.18, position))

    ranked.sort(reverse=True)
    result = []
    for _score, position in ranked:
        if all(abs(position - existing) >= 2 for existing in result):
            result.append(position)
            if len(result) >= maximum:
                break
    return sorted(result)


def refine_static_picture_box(
    frame: np.ndarray,
    stable_mask: np.ndarray,
    fallback_box: tuple[int, int, int, int],
    context: PictureRefinementContext | None = None,
) -> tuple[int, int, int, int] | None:
    """将粗略稳定区域收紧为完整图片的四条外边界。

    字幕、图片内部文字和背景局部都可能在数秒内保持不动，不能直接将
    稳定连通区导出。真实图片通常同时满足：内部稳定、四边连续稳定、
    四周比内部更易发生变化。此处将这些时间特征与画面边缘共同评分，
    未能确认完整矩形时返回 ``None``，而不是输出混有字幕或背景的图片。
    """
    frame_height, frame_width = frame.shape[:2]
    x, y, width, height = fallback_box
    if width < 24 or height < 24:
        return None

    context = context or build_picture_refinement_context(frame, stable_mask)
    if context is None or context.stable.shape != (frame_height, frame_width):
        return None
    stable = context.stable
    integral = context.integral

    # 粗候选可能只覆盖图片的一部分，也可能把一行字幕带进去，所以在
    # 四周留出足够的搜索范围。
    # 粗候选常常只落在图片的一列文字、一个图表或半边地图上。把搜索
    # 范围扩到粗框两侧，才能恢复完整外边界；最终仍会以四边稳定性和
    # 外围运动做严格校验，所以不会因此把字幕或循环背景当作图片。
    search_left = max(0, round(x - width * 0.90))
    search_right = min(frame_width, round(x + width * 1.90))
    search_top = max(0, round(y - height * 0.70))
    search_bottom = min(frame_height, round(y + height * 1.70))
    if search_right - search_left < 24 or search_bottom - search_top < 24:
        return None

    horizontal_edges = context.horizontal_edges
    vertical_edges = context.vertical_edges
    column_stability = np.mean(stable[search_top:search_bottom, :], axis=0)
    row_stability = np.mean(stable[:, search_left:search_right], axis=1)
    horizontal_stability_edges = np.abs(np.diff(column_stability))
    vertical_stability_edges = np.abs(np.diff(row_stability))

    x_positions: set[int] = {x, x + width}
    y_positions: set[int] = {y, y + height}
    for profile, start, end, positions in (
        (horizontal_edges, search_left, search_right - 1, x_positions),
        (horizontal_stability_edges, search_left, search_right - 1, x_positions),
        (vertical_edges, search_top, search_bottom - 1, y_positions),
        (vertical_stability_edges, search_top, search_bottom - 1, y_positions),
    ):
        for position in _profile_peak_positions(profile, start, end):
            # 差分位置位于两个像素之间；两个相邻坐标都保留，后续由时间
            # 稳定性决定真正位于图片内侧的那一个。
            positions.update((position, position + 1))

    left_positions = _rank_boundary_positions(
        x_positions,
        horizontal_edges,
        horizontal_stability_edges,
        x,
        round(x - width * 0.50),
        round(x + width * 0.34),
    )
    right_positions = _rank_boundary_positions(
        x_positions,
        horizontal_edges,
        horizontal_stability_edges,
        x + width,
        round(x + width * 0.66),
        round(x + width * 1.50),
    )
    top_positions = _rank_boundary_positions(
        y_positions,
        vertical_edges,
        vertical_stability_edges,
        y,
        round(y - height * 0.50),
        round(y + height * 0.34),
    )
    bottom_positions = _rank_boundary_positions(
        y_positions,
        vertical_edges,
        vertical_stability_edges,
        y + height,
        round(y + height * 0.66),
        round(y + height * 1.50),
    )
    if not left_positions or not right_positions or not top_positions or not bottom_positions:
        return None

    minimum_width = max(40, round(width * 0.48))
    maximum_width = min(frame_width, round(width * 2.05))
    minimum_height = max(36, round(height * 0.48))
    maximum_height = min(frame_height, round(height * 2.05))
    horizontal_pairs = [
        (left, right)
        for left in left_positions
        for right in right_positions
        if minimum_width <= right - left <= maximum_width
    ]
    vertical_pairs = [
        (top, bottom)
        for top in top_positions
        for bottom in bottom_positions
        if minimum_height <= bottom - top <= maximum_height
    ]

    verified_boxes: list[
        tuple[float, float, float, float, float, int, tuple[int, int, int, int]]
    ] = []
    for left, right in horizontal_pairs:
        candidate_width = right - left
        for top, bottom in vertical_pairs:
            candidate_height = bottom - top
            aspect_ratio = candidate_width / candidate_height
            if not 0.55 <= aspect_ratio <= 4.2:
                continue

            # 取矩形内侧的窄带评估四条边。若一条边落在背景或字幕上，
            # 这一项会显著降低，即使图片内部本身是稳定的也不会通过。
            edge_band = max(3, min(10, round(min(candidate_width, candidate_height) * 0.045)))
            inside_stability = _mean_integral_area(
                integral, left, top, candidate_width, candidate_height
            )
            edge_stabilities = (
                _mean_integral_area(
                    integral, left + edge_band, top + edge_band, candidate_width - edge_band * 2, edge_band
                ),
                _mean_integral_area(
                    integral, left + edge_band, bottom - edge_band * 2, candidate_width - edge_band * 2, edge_band
                ),
                _mean_integral_area(
                    integral, left + edge_band, top + edge_band, edge_band, candidate_height - edge_band * 2
                ),
                _mean_integral_area(
                    integral, right - edge_band * 2, top + edge_band, edge_band, candidate_height - edge_band * 2
                ),
            )
            minimum_edge_stability = min(edge_stabilities)
            average_edge_stability = sum(edge_stabilities) / len(edge_stabilities)

            # 稳定性只能说明内容没有动，无法区分图片的外框和内部表格线。
            # 再测量四条候选边附近的灰度跳变，字幕边缘、正文分隔线和
            # 背景局部通常无法同时形成四条连续的边界。
            visual_band = max(2, min(5, round(min(candidate_width, candidate_height) * 0.02)))
            visual_margin = max(4, min(16, round(min(candidate_width, candidate_height) * 0.07)))
            left_gradient = _mean_integral_area(
                context.horizontal_gradient_integral,
                left - visual_band,
                top + visual_margin,
                visual_band * 2,
                candidate_height - visual_margin * 2,
            )
            right_gradient = _mean_integral_area(
                context.horizontal_gradient_integral,
                right - visual_band - 1,
                top + visual_margin,
                visual_band * 2,
                candidate_height - visual_margin * 2,
            )
            top_gradient = _mean_integral_area(
                context.vertical_gradient_integral,
                left + visual_margin,
                top - visual_band,
                candidate_width - visual_margin * 2,
                visual_band * 2,
            )
            bottom_gradient = _mean_integral_area(
                context.vertical_gradient_integral,
                left + visual_margin,
                bottom - visual_band - 1,
                candidate_width - visual_margin * 2,
                visual_band * 2,
            )
            minimum_visual_gradient = min(
                left_gradient, right_gradient, top_gradient, bottom_gradient
            )
            visual_gradients = (left_gradient, right_gradient, top_gradient, bottom_gradient)
            boundary_gradients = (
                horizontal_edges[min(len(horizontal_edges) - 1, max(0, left))],
                horizontal_edges[min(len(horizontal_edges) - 1, max(0, right - 1))],
                vertical_edges[min(len(vertical_edges) - 1, max(0, top))],
                vertical_edges[min(len(vertical_edges) - 1, max(0, bottom - 1))],
            )
            weak_visual_edges = [
                index for index, value in enumerate(visual_gradients) if value < 9.0
            ]
            # 浅色卡片的某一边可能与循环背景同色，窄带梯度会偏低；如果
            # 长边轮廓很清楚、卡片内部和四边都高度稳定，只恢复这一条边。
            # 普通候选仍要求四边全部通过 9.0 的视觉梯度门槛。
            recoverable_weak_edge = (
                len(weak_visual_edges) == 1
                and minimum_visual_gradient >= 5.0
                and boundary_gradients[weak_visual_edges[0]] >= 18.0
                and inside_stability >= 0.985
                and minimum_edge_stability >= 0.95
                and average_edge_stability >= 0.97
            )

            ring_band = max(4, min(16, round(min(candidate_width, candidate_height) * 0.075)))
            outer_left = max(0, left - ring_band)
            outer_top = max(0, top - ring_band)
            outer_right = min(frame_width, right + ring_band)
            outer_bottom = min(frame_height, bottom + ring_band)
            outer_area = (outer_right - outer_left) * (outer_bottom - outer_top)
            inner_sum = inside_stability * candidate_width * candidate_height
            outer_sum = _mean_integral_area(
                integral, outer_left, outer_top, outer_right - outer_left, outer_bottom - outer_top
            ) * outer_area
            ring_area = outer_area - candidate_width * candidate_height
            ring_stability = (outer_sum - inner_sum) / max(1, ring_area)
            ring_motion = 1.0 - max(0.0, min(1.0, ring_stability))

            # 这些阈值特意偏向完整图片：内部及四边必须足够稳定，而外围
            # 至少需要存在少量运动。这样不会把静态字幕与背景拼成一张图。
            if (
                inside_stability < 0.82
                or minimum_edge_stability < 0.66
                or average_edge_stability < 0.78
                or ring_motion < 0.08
                # 真实图片边缘在压缩视频中可能比字幕边缘弱；稳定度和外圈
                # 运动检测仍是主约束，这里只降低视觉梯度的辅助门槛。
                or (minimum_visual_gradient < 9.0 and not recoverable_weak_edge)
            ):
                continue

            contrast = inside_stability - ring_stability
            verified_boxes.append(
                (
                    inside_stability,
                    minimum_edge_stability,
                    average_edge_stability,
                    minimum_visual_gradient,
                    ring_motion + contrast * 0.25,
                    candidate_width * candidate_height,
                    (left, top, candidate_width, candidate_height),
                )
            )

    if not verified_boxes:
        return None

    # 完整图片内部会在数秒内几乎不变；把阈值设为 97.5% 能排除混有
    # 背景、字幕的外扩框。随后在合格框里选面积最大者，保留完整素材
    # 而不是正文、地图或图表中的局部区域。
    complete_boxes = [
        item
        for item in verified_boxes
        if item[0] >= 0.975
        and item[1] >= 0.86
        and item[2] >= 0.90
        and (
            item[3] >= 9.0
            or (
                item[3] >= 5.0
                and item[0] >= 0.985
                and item[1] >= 0.95
                and item[2] >= 0.97
            )
        )
    ]
    if not complete_boxes:
        return None

    (
        _inside,
        _minimum_edge,
        _average_edge,
        _minimum_visual_gradient,
        _surrounding_motion,
        _area,
        best_box,
    ) = max(
        complete_boxes,
        key=lambda item: (item[5], item[3], item[1], item[0], item[4]),
    )

    # 边缘像素最容易混入背景或抗锯齿后的字幕线。安全地向内收一到三个
    # 分析像素，宁可丢弃极窄的边框，也不导出图片外侧的背景。
    left, top, candidate_width, candidate_height = best_box
    inset = max(3, min(5, round(min(candidate_width, candidate_height) * 0.014)))
    if candidate_width <= inset * 2 + 24 or candidate_height <= inset * 2 + 24:
        return None
    return (
        left + inset,
        top + inset,
        candidate_width - inset * 2,
        candidate_height - inset * 2,
    )


def _cluster_hough_lines(
    lines: list[tuple[int, int, int, int]],
    tolerance: int = 4,
) -> list[tuple[int, int, int, int]]:
    """合并位置接近的 Hough 线段，保留其完整可见范围。"""
    if not lines:
        return []

    groups: list[list[tuple[int, int, int, int]]] = []
    for line in sorted(lines, key=lambda item: item[0]):
        if not groups or line[0] - groups[-1][-1][0] > tolerance:
            groups.append([line])
        else:
            groups[-1].append(line)

    clustered = []
    for group in groups:
        # 同一边界可能被相邻的背景线段连接成一组；取最长线段的位置，
        # 比按长度平均更不容易把真正的卡片边缘拉向背景。
        strongest = max(group, key=lambda item: item[3])
        position = strongest[0]
        clustered.append(
            (
                position,
                min(item[1] for item in group),
                max(item[2] for item in group),
                max(item[3] for item in group),
            )
        )
    return clustered


def _profile_edge_positions(
    light_profile: np.ndarray,
    minimum_change: float,
    window: int = 5,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """找出浅色卡片在垂直方向上显著进入和退出的位置。"""
    rises = []
    falls = []
    for position in range(window, len(light_profile) - window):
        before = float(np.mean(light_profile[position - window : position]))
        after = float(np.mean(light_profile[position : position + window]))
        rise = after - before
        fall = before - after
        if rise >= minimum_change:
            rises.append((position, rise))
        if fall >= minimum_change:
            falls.append((position, fall))
    return rises, falls


def _box_coverage(
    inner: tuple[int, int, int, int],
    outer: tuple[int, int, int, int],
) -> float:
    """计算 inner 被 outer 覆盖的比例，用于抑制完整卡片内的碎片候选。"""
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    overlap_width = max(0, min(ix + iw, ox + ow) - max(ix, ox))
    overlap_height = max(0, min(iy + ih, oy + oh) - max(iy, oy))
    return overlap_width * overlap_height / max(1, iw * ih)


def trim_unstable_bottom_edge(
    box: tuple[int, int, int, int],
    stable_mask: np.ndarray,
    min_stability: float = 0.60,
    frame: np.ndarray | None = None,
) -> tuple[int, int, int, int]:
    """截掉候选底部的字幕、黑边和循环背景。

    图片主体在时间上应保持稳定；字幕往往只出现在候选框的底部，并在相邻
    抽样帧之间变化。只有检测到“上方长期稳定、下方持续不稳定”的明显落差时
    才裁切。部分视频的字幕或播放器控件会短暂保持不变，因此同时检查候选
    底部是否出现明显的暗色低纹理带，避免把图片下方的黑边写入素材。
    """
    x, y, width, height = (int(value) for value in box)
    if width <= 0 or height <= 0:
        return box
    mask = np.asarray(stable_mask)
    if mask.ndim != 2:
        return box
    frame_height, frame_width = mask.shape
    left = max(0, x)
    top = max(0, y)
    right = min(frame_width, x + width)
    bottom = min(frame_height, y + height)
    if right <= left or bottom <= top:
        return box
    rows = np.mean(mask[top:bottom, left:right] > 0, axis=1)
    if rows.size < 24:
        return box

    # 字幕通常占候选底部的一小段。窗口随候选高度变化，但限制上限，
    # 使短图和高清视频都能识别连续落差。
    window = max(3, min(12, round(rows.size * 0.025)))
    search_start = max(window * 2, round(rows.size * 0.45))
    minimum_height = max(24, round(rows.size * 0.55))

    # 视觉边界作为第二道保护：黑边、播放器字幕栏和静音图标常会让
    # 稳定掩码看起来“静止”，但它们通常表现为候选底部突然变暗，并在
    # 多行上保持较低亮度。只在落差足够明显且暗带持续时裁切。
    if frame is not None:
        frame_array = np.asarray(frame)
        if frame_array.ndim == 3 and frame_array.shape[:2] == mask.shape:
            frame_gray = cv2.cvtColor(frame_array, cv2.COLOR_BGR2GRAY)
            luminance = np.mean(frame_gray[top:bottom, left:right], axis=1)
            dark_ratio = np.mean(frame_gray[top:bottom, left:right] < 45, axis=1)
            visual_window = max(3, min(10, round(rows.size * 0.02)))
            visual_tail = max(8, round(rows.size * 0.04))
            for position in range(
                search_start,
                max(search_start, rows.size - visual_tail + 1),
            ):
                before_start = max(0, position - visual_window * 2)
                before_luma = float(np.mean(luminance[before_start:position]))
                after_end = min(rows.size, position + visual_window)
                after_luma = float(np.mean(luminance[position:after_end]))
                tail_end = min(rows.size, position + visual_tail)
                tail_luma = float(np.mean(luminance[position:tail_end]))
                tail_dark = float(np.mean(dark_ratio[position:tail_end]))
                if (
                    position >= minimum_height
                    and before_luma >= 70.0
                    and before_luma - after_luma >= 30.0
                    and tail_luma <= 65.0
                    and tail_dark >= 0.40
                ):
                    return (left, top, right - left, position)

    for position in range(search_start, rows.size - window + 1):
        before_start = max(0, position - window * 2)
        before = float(np.mean(rows[before_start:position]))
        after = float(np.mean(rows[position : position + window]))
        if before < 0.72 or after >= min_stability or before - after < 0.20:
            continue
        if position < minimum_height:
            continue
        # 额外确认落差之后仍有一段不稳定区域，避免把单个编码噪声行当作
        # 字幕边界。达到候选底部时不要求完整的第二个窗口。
        tail_end = min(rows.size, position + window * 2)
        if float(np.mean(rows[position:tail_end])) >= min_stability:
            continue
        return (left, top, right - left, position)
    return box


def _profile_runs(
    profile: np.ndarray,
    threshold: float,
    minimum_length: int,
) -> list[tuple[int, int]]:
    """返回一维稳定性曲线中连续超过阈值的区间。"""
    mask = np.asarray(profile >= threshold, dtype=np.uint8)
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for position, value in enumerate(np.r_[mask, 0]):
        if value and start is None:
            start = position
        elif not value and start is not None:
            if position - start >= minimum_length:
                runs.append((start, position))
            start = None
    return runs


def _first_strong_edge(
    profile: np.ndarray,
    start: int,
    end: int,
    minimum_strength: float,
) -> int | None:
    """从指定范围中取最早的局部强边缘，避免选到图片内部文字线。"""
    start = max(1, start)
    end = min(len(profile), end)
    if end <= start:
        return None
    window = max(1, min(3, round((end - start) * 0.025)))
    for position in range(start, end):
        left = max(start, position - window)
        right = min(end, position + window + 1)
        value = float(profile[position])
        if value < minimum_strength:
            continue
        if value >= float(np.max(profile[left:right])) - 1e-6:
            return position
    return None


def detect_static_portrait_candidate(
    frame: np.ndarray,
    stable_mask: np.ndarray,
    original_frame: np.ndarray,
    inverse_scale: float,
    timestamp: float,
    motion_window_seconds: float,
    context: PictureRefinementContext | None = None,
) -> Candidate | None:
    """检测动态背景上叠加的竖版静态图片，并排除底部字幕区域。

    竖版图片通常没有浅色画板，不能依赖横向卡片的亮度突变。这里利用
    图片内部跨时间高度稳定、左右列形成高稳定平台、底部离开图片后稳定度
    突然下降这三个特征，再用四条视觉边缘做确认。
    """
    frame_height, frame_width = frame.shape[:2]
    if frame_height < 100 or frame_width < 100:
        return None
    context = context or build_picture_refinement_context(frame, stable_mask)
    if context is None:
        return None

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.int16)
    # 底部通常有动态字幕，列稳定性只在画面上方计算，避免字幕把左右
    # 边界抹平；同时保留足够高度以覆盖大多数竖版素材。
    profile_bottom = max(24, round(frame_height * 0.86))
    column_profile = np.mean(stable_mask[:profile_bottom], axis=0)
    column_profile = cv2.GaussianBlur(
        column_profile.astype(np.float32).reshape(1, -1), (0, 0), 1.5
    ).reshape(-1)

    candidate_runs: list[tuple[float, int, int]] = []
    minimum_width = max(36, round(frame_height * 0.22))
    maximum_width = min(round(frame_width * 0.62), round(frame_height * 1.25))
    for threshold in (0.94, 0.90, 0.86, 0.82):
        for left, right in _profile_runs(column_profile, threshold, minimum_length=minimum_width):
            width = right - left
            if not minimum_width <= width <= maximum_width:
                continue
            # 贴近画面边缘的稳定平台更可能是循环背景；竖版素材一般在
            # 中部展示，保留少量偏移但排除明显贴边区域。
            if left <= round(frame_width * 0.02) or right >= round(frame_width * 0.98):
                continue
            candidate_runs.append(
                (float(column_profile[left:right].mean()), left, right)
            )
        if candidate_runs:
            # 高阈值已经找到完整平台时，不让较低阈值产生的外扩区覆盖它。
            break
    if not candidate_runs:
        return None

    horizontal_gradient = context.horizontal_gradient
    vertical_gradient = context.vertical_gradient
    verified: list[tuple[float, tuple[int, int, int, int]]] = []
    for _stability_score, left, right in candidate_runs:
        width = right - left
        row_profile = np.mean(stable_mask[:, left:right], axis=1)
        reference_start = max(0, round(frame_height * 0.06))
        reference_end = min(frame_height, round(frame_height * 0.72))
        reference_level = float(np.median(row_profile[reference_start:reference_end]))
        if reference_level < 0.72:
            continue
        drop_threshold = max(0.55, reference_level - 0.25)

        # 找到图片底部离开后稳定度的持续下降。字幕覆盖图片时，这个
        # 位置会提前，确保导出的素材不含字幕。
        drop_window = max(3, round(frame_height * 0.012))
        bottom = None
        for position in range(
            max(round(frame_height * 0.30), round(frame_height * 0.45)),
            min(round(frame_height * 0.93), frame_height - drop_window),
        ):
            if float(np.mean(row_profile[position : position + drop_window])) >= drop_threshold:
                continue
            before_start = max(0, position - drop_window * 2)
            if float(np.mean(row_profile[before_start:position])) < drop_threshold * 0.88:
                continue
            bottom = position
            break
        if bottom is None:
            continue

        # 图片顶部的第一条强边缘通常比内部文字线更早出现；只在底部
        # 之前的一段范围寻找，避免字幕或图中大标题成为“顶部”。
        vertical_band_gradient = np.mean(
            np.abs(np.diff(gray[:, left:right], axis=0)), axis=1
        )
        top_search_end = max(
            round(frame_height * 0.18),
            bottom - round(width * 0.68),
        )
        baseline_end = max(8, min(top_search_end, round(frame_height * 0.22)))
        baseline = float(np.median(vertical_band_gradient[:baseline_end]))
        percentile = float(np.percentile(vertical_band_gradient[:top_search_end], 75))
        edge_threshold = max(7.0, baseline * 2.2, percentile * 1.35)
        top = _first_strong_edge(
            vertical_band_gradient,
            max(2, round(frame_height * 0.01)),
            top_search_end,
            edge_threshold,
        )
        if top is None:
            continue

        height = bottom - top
        aspect_ratio = width / max(1, height)
        if (
            height < max(48, round(width * 0.82))
            or height > frame_height * 0.92
            or not 0.28 <= aspect_ratio <= 1.25
        ):
            continue

        edge_band = max(2, min(8, round(min(width, height) * 0.025)))
        inner_left = left + edge_band
        inner_right = right - edge_band
        inner_top = top + edge_band
        inner_bottom = bottom - edge_band
        if inner_right <= inner_left or inner_bottom <= inner_top:
            continue
        inside_stability = float(
            np.mean(stable_mask[inner_top:inner_bottom, inner_left:inner_right])
        )
        if inside_stability < 0.78:
            continue

        # 四边都要有明显的视觉变化。内侧边缘可以较弱，但不能完全
        # 没有边界，否则容易把稳定的城市/建筑局部当成图片。
        # 稳定平台的边界可能比真实图片边缘偏移 1~2 个分析像素；在
        # 边缘附近取峰值可容忍这种偏移，同时仍要求整条边在图片高度内
        # 有明显对比，避免把内部文字线当成图片边缘。
        left_band = horizontal_gradient[
            inner_top:inner_bottom,
            max(0, left - 3) : min(horizontal_gradient.shape[1], left + 4),
        ]
        right_band = horizontal_gradient[
            inner_top:inner_bottom,
            max(0, right - 3) : min(horizontal_gradient.shape[1], right + 4),
        ]
        left_gradient = float(np.max(np.mean(left_band, axis=0))) if left_band.size else 0.0
        right_gradient = float(np.max(np.mean(right_band, axis=0))) if right_band.size else 0.0
        top_start = max(0, top - 3)
        top_end = min(len(vertical_band_gradient), top + 4)
        bottom_start = max(0, bottom - 3)
        bottom_end = min(len(vertical_band_gradient), bottom + 4)
        top_gradient = float(np.max(vertical_band_gradient[top_start:top_end]))
        bottom_gradient = float(np.max(vertical_band_gradient[bottom_start:bottom_end]))
        if min(left_gradient, right_gradient, top_gradient, bottom_gradient) < 5.0:
            continue

        ring_band = max(4, min(12, round(min(width, height) * 0.055)))
        outer_left = max(0, left - ring_band)
        outer_top = max(0, top - ring_band)
        outer_right = min(frame_width, right + ring_band)
        outer_bottom = min(frame_height, bottom + ring_band)
        ring = np.zeros_like(stable_mask, dtype=bool)
        ring[outer_top:outer_bottom, outer_left:outer_right] = True
        ring[top:bottom, left:right] = False
        ring_motion = float(np.mean(~stable_mask[ring])) if np.any(ring) else 0.0
        if ring_motion < 0.03:
            continue

        inset = max(3, min(5, round(min(width, height) * 0.014)))
        if width <= inset * 2 + 24 or height <= inset * 2 + 24:
            continue
        box = (left + inset, top + inset, width - inset * 2, height - inset * 2)
        box = trim_unstable_bottom_edge(box, stable_mask, frame=frame)
        if box[2] <= 24 or box[3] <= 24:
            return None
        x, y, box_width, box_height = box
        if box_width * box_height / max(1, frame_width * frame_height) < DEFAULT_MIN_AREA_RATIO:
            # 过小的固定台标、按钮和角标虽然可能满足局部稳定条件，
            # 但不属于需要提取的图片素材。
            continue
        patch_gray = cv2.cvtColor(
            frame[y : y + box_height, x : x + box_width], cv2.COLOR_BGR2GRAY
        )
        sharpness = float(cv2.Laplacian(patch_gray, cv2.CV_64F).var())
        if sharpness < 10.0:
            continue
        verified.append((inside_stability * ring_motion, box))

    if not verified:
        return None
    _score, box = max(
        verified,
        key=lambda item: (item[1][2] * item[1][3], item[0]),
    )
    image = crop_original_frame(original_frame, box, inverse_scale, padding=0)
    if image.size == 0:
        return None
    x, y, width, height = box
    patch_gray = cv2.cvtColor(frame[y : y + height, x : x + width], cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(patch_gray, cv2.CV_64F).var())
    return Candidate(
        box,
        image,
        timestamp,
        sharpness,
        lead_seconds=motion_window_seconds,
        signature=image_hash(image),
        detector="card",
    )


def detect_static_card_candidates(
    previous_gray: np.ndarray,
    frame: np.ndarray,
    original_frame: np.ndarray,
    inverse_scale: float,
    timestamp: float,
    diff_threshold: int,
    motion_window_seconds: float,
) -> list[Candidate]:
    """检测动态背景中有明确边界、且持续静止的完整浅色图片卡片。

    不能只依赖稳定像素连通区：云层、楼宇等背景也可能局部稳定，并会把
    图片边缘粘连成碎片。这里先由左右竖线定位横向范围，再用浅色画板
    的上下亮度突变确定完整边界，最后用跨时间稳定性排除动态背景。
    """
    frame_height, frame_width = frame.shape[:2]
    raw_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(raw_gray, (3, 3), 0)
    delta = cv2.absdiff(previous_gray, gray)
    stable_mask = delta <= diff_threshold
    frame_area = frame_width * frame_height
    refinement_context = build_picture_refinement_context(frame, stable_mask)

    portrait_candidate = detect_static_portrait_candidate(
        frame,
        stable_mask,
        original_frame,
        inverse_scale,
        timestamp,
        motion_window_seconds,
        context=refinement_context,
    )
    if portrait_candidate is not None:
        return [portrait_candidate]

    # 先用覆盖画面主体的宽松框反推真实图片边缘。这样即使初始稳定区域
    # 只落在文章的一列文字、图表的一部分，或图片没有明显竖线，也能从
    # 四边的稳定性和视觉变化恢复完整素材。最终结果会再向内收，避免把
    # 字幕、阴影和循环背景的边缘写入文件。
    centered_box = (
        round(frame_width * 0.06),
        round(frame_height * 0.05),
        round(frame_width * 0.88),
        round(frame_height * 0.78),
    )
    strict_box = refine_static_picture_box(
        frame, stable_mask, centered_box, context=refinement_context
    )
    if strict_box is not None:
        strict_box = trim_unstable_bottom_edge(strict_box, stable_mask, frame=frame)
        x, y, width, height = strict_box
        area_ratio = width * height / frame_area
        outside_mask = np.ones_like(stable_mask, dtype=bool)
        outside_mask[y : y + height, x : x + width] = False
        outside_motion = float(np.mean(~stable_mask[outside_mask]))
        patch_gray = raw_gray[y : y + height, x : x + width]
        sharpness = float(cv2.Laplacian(patch_gray, cv2.CV_64F).var())
        if (
            DEFAULT_CARD_MIN_AREA_RATIO <= area_ratio <= 0.75
            and outside_motion >= DEFAULT_MIN_BACKGROUND_MOTION
            and sharpness >= 10.0
        ):
            image = crop_original_frame(original_frame, strict_box, inverse_scale, padding=0)
            if image.size:
                return [
                    Candidate(
                        strict_box,
                        image,
                        timestamp,
                        sharpness,
                        lead_seconds=motion_window_seconds,
                        signature=image_hash(image),
                        detector="card",
                    )
                ]

    edges = cv2.Canny(raw_gray, 20, 70)
    line_minimum = max(45, round(min(frame_width, frame_height) * 0.12))
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=max(22, round(min(frame_width, frame_height) * 0.06)),
        minLineLength=line_minimum,
        maxLineGap=18,
    )
    if lines is None:
        return []

    vertical_lines = []
    for x1, y1, x2, y2 in lines[:, 0]:
        x1, y1, x2, y2 = map(int, (x1, y1, x2, y2))
        line_width = abs(x2 - x1)
        line_height = abs(y2 - y1)
        if line_width <= 5 and line_height >= line_minimum:
            vertical_lines.append(
                (
                    round((x1 + x2) / 2),
                    min(y1, y2),
                    max(y1, y2),
                    line_height,
                )
            )

    vertical_lines = _cluster_hough_lines(vertical_lines)
    if len(vertical_lines) < 2:
        return []

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    light_mask = (hsv[:, :, 1] <= 130) & (hsv[:, :, 2] >= 135)
    scored_boxes: list[tuple[float, tuple[int, int, int, int]]] = []

    for left_index, left_line in enumerate(vertical_lines[:-1]):
        for right_line in vertical_lines[left_index + 1 :]:
            left, right = left_line[0], right_line[0]
            width = right - left
            center = (left + right) / 2
            if (
                width < frame_width * 0.22
                or width > frame_width * 0.85
                or abs(center - frame_width / 2) > frame_width * DEFAULT_CARD_CENTER_TOLERANCE
            ):
                continue

            light_profile = np.mean(light_mask[:, left:right], axis=1)
            top_edges, bottom_edges = _profile_edge_positions(light_profile, minimum_change=0.16)
            if not top_edges or not bottom_edges:
                continue

            # 仅保留最强的一小组上下边界。卡片内文字也会带来行亮度变化，
            # 但强度通常弱于卡片进入/退出背景时的整体突变。
            strongest_tops = sorted(top_edges, key=lambda item: item[1], reverse=True)[:8]
            strongest_bottoms = sorted(bottom_edges, key=lambda item: item[1], reverse=True)[:10]
            row_stability = np.mean(stable_mask[:, left:right], axis=1)

            for top, top_strength in strongest_tops:
                for bottom, bottom_strength in strongest_bottoms:
                    height = bottom - top
                    minimum_height = max(round(frame_height * 0.30), round(width * 0.28))
                    if (
                        height < minimum_height
                        or height > frame_height * 0.88
                        or bottom > frame_height - 2
                    ):
                        continue
                    area_ratio = width * height / frame_area
                    if not DEFAULT_CARD_MIN_AREA_RATIO <= area_ratio <= 0.75:
                        continue

                    left_coverage = max(
                        0, min(bottom, left_line[2]) - max(top, left_line[1])
                    ) / height
                    right_coverage = max(
                        0, min(bottom, right_line[2]) - max(top, right_line[1])
                    ) / height
                    if min(left_coverage, right_coverage) < 0.28:
                        continue

                    patch_stability = float(np.mean(stable_mask[top:bottom, left:right]))
                    if patch_stability < 0.65:
                        continue
                    patch_light_ratio = float(np.mean(light_mask[top:bottom, left:right]))
                    if patch_light_ratio < 0.35:
                        continue

                    outside_mask = np.ones_like(stable_mask, dtype=bool)
                    outside_mask[top:bottom, left:right] = False
                    outside_motion = float(np.mean(~stable_mask[outside_mask]))
                    if outside_motion < DEFAULT_MIN_BACKGROUND_MOTION:
                        continue

                    boundary_strength = min(top_strength, bottom_strength)
                    # 卡片边缘附近应有连续的静态内容，避免把背景里的亮云层
                    # 与字幕组合成伪卡片。
                    edge_stability = float(
                        np.mean(row_stability[max(0, top + 3) : min(frame_height, bottom - 3)])
                    )
                    if edge_stability < 0.72:
                        continue

                    score = (
                        patch_stability * 3.0
                        + patch_light_ratio * 0.45
                        + outside_motion * 0.15
                        + boundary_strength * 0.65
                        + min(left_coverage, right_coverage) * 0.20
                    )
                    scored_boxes.append((score, (left, top, width, height)))

    candidates = []
    # 只尝试得分最高的一小组粗候选。严格边界校验成本较高，而同一帧
    # 的低分候选大多只是正文、字幕或背景碎片。
    rough_boxes: list[tuple[int, int, int, int]] = []
    for _score, rough_box in sorted(scored_boxes, reverse=True):
        # 同一张图片会因文字、表格线和压缩噪声生成许多近乎重叠的粗框。
        # 只保留边界明显不同的框，既避免重复计算，也不会漏掉同帧的多张图。
        if any(
            box_iou(rough_box, existing) >= 0.62
            or _box_coverage(rough_box, existing) >= 0.82
            or _box_coverage(existing, rough_box) >= 0.82
            for existing in rough_boxes
        ):
            continue
        rough_boxes.append(rough_box)
        if len(rough_boxes) >= 3:
            break

    for rough_box in rough_boxes:
        box = refine_static_picture_box(
            frame, stable_mask, rough_box, context=refinement_context
        )
        if box is None:
            continue
        if any(
            box_iou(box, candidate.box) >= 0.62
            or _box_coverage(box, candidate.box) >= 0.70
            for candidate in candidates
        ):
            continue
        # 若后续候选覆盖了先前的局部候选，保留面积更大的完整卡片。
        candidates = [
            candidate
            for candidate in candidates
            if _box_coverage(candidate.box, box) < 0.70
        ]
        x, y, width, height = box
        patch_gray = raw_gray[y : y + height, x : x + width]
        sharpness = float(cv2.Laplacian(patch_gray, cv2.CV_64F).var())
        if sharpness < 10.0:
            continue
        image = crop_original_frame(original_frame, box, inverse_scale, padding=0)
        if image.size:
            candidates.append(
                Candidate(
                    box,
                    image,
                    timestamp,
                    sharpness,
                    lead_seconds=motion_window_seconds,
                    signature=image_hash(image),
                    detector="card",
                )
            )
        if len(candidates) >= 3:
            break

    return candidates


def merge_candidates(primary: list[Candidate], fallback: list[Candidate]) -> list[Candidate]:
    """合并通过严格边界校验的候选，并去掉同一图片的重复框。"""
    # 卡片分支已确认图片的完整四边，并会主动向内收；只要它找到结果，
    # 就不再让稳定连通区分支用局部文字、字幕或背景碎片覆盖该结果。
    if primary:
        fallback = []
    merged = []
    for candidate in sorted(
        [*primary, *fallback],
        key=lambda item: (item.box[2] * item.box[3], item.sharpness),
        reverse=True,
    ):
        if any(box_iou(candidate.box, existing.box) >= 0.68 for existing in merged):
            continue
        merged.append(candidate)
    return merged


def candidate_matches_track(track: Track, candidate: Candidate) -> bool:
    """判断候选是否仍属于同一张图片，兼顾边界轻微漂移和编码噪声。"""
    if track.detector != candidate.detector:
        return False
    overlap = box_iou(track.box, candidate.box)
    # 稳定区有时只覆盖图片的一列、半边或一个局部文字块。只要它被
    # canonical 完整框几乎完全包住，就仍属于同一轨迹；反向覆盖同样
    # 支持轨迹先从局部框开始、之后才检测到完整框的情况。
    track_coverage = _box_coverage(track.box, candidate.box)
    candidate_coverage = _box_coverage(candidate.box, track.box)
    containment = max(track_coverage, candidate_coverage)
    track_area = max(1, track.box[2] * track.box[3])
    candidate_area = max(1, candidate.box[2] * candidate.box[3])
    relative_area = min(track_area, candidate_area) / max(track_area, candidate_area)
    # 完整卡片的正常边界抖动很小；提高阈值可以避免偶发的局部稳定区
    # 混入同一轨迹，并导致最终图片被字幕或背景污染。
    required_overlap = 0.60 if track.detector == "card" else 0.42
    # 只有明显的局部框（面积不超过完整框约 55%）才允许用较宽松的
    # 0.90 覆盖阈值。这样能合并右侧文字列等碎片，又不会把两张相邻
    # 地图在转场时因位置重叠而串成同一轨迹。
    strong_partial_containment = containment >= 0.90 and relative_area <= 0.55
    if overlap < required_overlap and not strong_partial_containment:
        return False
    distance = hash_distance(track.last_signature, candidate.signature)
    # 强几何重合可放宽 dHash，编码噪声和不同裁切会让局部图的 dHash
    # 变化很大；但只有 0.94 以上的覆盖才可无视明显的内容差异。
    return (
        distance is None
        or distance <= 28
        or overlap >= 0.75
        or strong_partial_containment
    )


def tracks_can_merge(first: Track, second: Track) -> bool:
    """判断两个已结束轨迹是否是同一张图的重叠检测结果。"""
    if first.detector != second.detector:
        return False
    # 只合并时间上确实重叠的轨迹。不同素材连续切换时，即使卡片位置
    # 相同，也不会因为边界相似而被串成一张图。
    if min(first.last_time, second.last_time) <= max(first.start_time, second.start_time):
        return False
    overlap = box_iou(first.box, second.box)
    coverage = max(
        _box_coverage(first.box, second.box),
        _box_coverage(second.box, first.box),
    )
    first_area = max(1, first.box[2] * first.box[3])
    second_area = max(1, second.box[2] * second.box[3])
    relative_area = min(first_area, second_area) / max(first_area, second_area)
    # 同一张图在转场或编码波动期间可能只剩约四分之三的可见区域；
    # 只有“小框明显小于大框”时放宽覆盖阈值，避免把相邻完整图片合并。
    strong_partial_containment = coverage >= 0.70 and relative_area <= 0.55
    return overlap >= 0.45 or coverage >= 0.78 or strong_partial_containment


def merge_tracks(tracks: list[Track]) -> list[Track]:
    """合并扫描过程中因局部框、转场而产生的重叠轨迹。"""
    merged: list[Track] = []
    for track in sorted(tracks, key=lambda item: (item.start_time, item.last_time)):
        candidates = [
            (index, existing)
            for index, existing in enumerate(merged)
            if tracks_can_merge(existing, track)
        ]
        if not candidates:
            merged.append(track)
            continue

        # 同一时段可能有多个局部框，优先吸收到时间覆盖最长的轨迹。
        target_index, target = max(
            candidates,
            key=lambda item: (
                min(item[1].last_time, track.last_time)
                - max(item[1].start_time, track.start_time),
                item[1].best_area,
            ),
        )
        target.start_time = min(target.start_time, track.start_time)
        target.last_time = max(target.last_time, track.last_time)
        target.samples += track.samples
        target.last_seen_index = max(target.last_seen_index, track.last_seen_index)
        if (
            track.best_area > target.best_area
            or (
                track.best_area == target.best_area
                and track.best_sharpness > target.best_sharpness
            )
        ):
            target.box = track.box
            target.best_frame = track.best_frame
            target.best_sharpness = track.best_sharpness
            target.best_area = track.best_area
            target.last_signature = track.last_signature
        elif target.last_signature is None:
            target.last_signature = track.last_signature

        # 合并后新轨迹的边界可能还会与更早的轨迹相交；下一轮按排序顺序
        # 继续检查即可，不需要重新扫描视频。
        merged[target_index] = target
    return merged


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

        # 默认模式只接受有完整四边证据的主体区域。无法确认边界时宁可
        # 跳过，避免把字幕或图片外缘的循环背景写入素材文件。
        if trim_cards:
            refined_box = refine_static_picture_box(frame, stable_mask > 0, (x, y, width, height))
            if refined_box is None:
                continue
            refined_box = trim_unstable_bottom_edge(refined_box, stable_mask, frame=frame)
            x, y, width, height = refined_box
            patch_gray = cv2.cvtColor(
                frame[y : y + height, x : x + width], cv2.COLOR_BGR2GRAY
            )
            sharpness = float(cv2.Laplacian(patch_gray, cv2.CV_64F).var())
            image = crop_original_frame(original_frame, refined_box, inverse_scale, padding=0)
        else:
            image = crop_original_frame(original_frame, (x, y, width, height), inverse_scale)
        if image.size:
            candidates.append(
                Candidate(
                    (x, y, width, height),
                    image,
                    timestamp,
                    sharpness,
                    signature=image_hash(image),
                    detector="card" if trim_cards else "general",
                )
            )

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


def parse_timestamp_seconds(value: str) -> float:
    """解析秒数、MM:SS 或 HH:MM:SS 格式的时间点。"""
    text = value.strip()
    if not text:
        raise ValueError("时间点不能为空。")
    parts = text.split(":")
    try:
        if len(parts) == 1:
            seconds = float(parts[0])
        elif len(parts) == 2:
            minutes, seconds_part = (float(part) for part in parts)
            seconds = minutes * 60 + seconds_part
        elif len(parts) == 3:
            hours, minutes, seconds_part = (float(part) for part in parts)
            seconds = hours * 3600 + minutes * 60 + seconds_part
        else:
            raise ValueError
    except ValueError as error:
        raise ValueError(f"无法解析时间点：{value}") from error
    if seconds < 0:
        raise ValueError(f"时间点不能为负数：{value}")
    return seconds


def parse_timestamp_list(value: str) -> list[float]:
    """解析以英文逗号分隔的多个时间点。"""
    timestamps = [parse_timestamp_seconds(item) for item in value.split(",") if item.strip()]
    if not timestamps:
        raise ValueError("至少需要提供一个时间点。")
    return timestamps


def _read_frame_at_time(capture: cv2.VideoCapture, seconds: float) -> np.ndarray:
    """定位并读取指定时间附近的一帧原始视频画面。"""
    capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, seconds) * 1000)
    ok, frame = capture.read()
    if not ok:
        raise RuntimeError(f"无法读取视频第 {seconds:.2f} 秒的画面。")
    return frame


def _collect_card_tracks_near_timestamp(
    video_path: Path,
    timestamp: float,
    sample_fps: float,
    search_seconds: float,
    diff_threshold: int,
    motion_window_seconds: float,
    analysis_width: int,
) -> list[Track]:
    """在指定时间点后追踪完整静态卡片，供人工复核场景使用。"""
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"无法打开视频：{video_path}")

    tracks: list[Track] = []
    time_step = 1 / sample_fps
    sample_total = max(1, math.floor(search_seconds * sample_fps) + 1)
    max_gap_seconds = max(1.5, time_step * 3)
    try:
        for sample_index in range(1, sample_total + 1):
            current_time = timestamp + (sample_index - 1) * time_step
            original_frame = _read_frame_at_time(capture, current_time)
            previous_frame = _read_frame_at_time(
                capture, max(0.0, current_time - motion_window_seconds)
            )
            frame, inverse_scale = resize_for_analysis(original_frame, analysis_width)
            previous_analysis, _unused_inverse_scale = resize_for_analysis(previous_frame, analysis_width)
            previous_gray = cv2.cvtColor(previous_analysis, cv2.COLOR_BGR2GRAY)
            previous_gray = cv2.GaussianBlur(previous_gray, (3, 3), 0)
            candidates = detect_static_card_candidates(
                previous_gray,
                frame,
                original_frame,
                inverse_scale,
                current_time,
                diff_threshold,
                motion_window_seconds,
            )

            matched_tracks: set[int] = set()
            for candidate in candidates:
                match_index = next(
                    (
                        index
                        for index, track in enumerate(tracks)
                        if (
                            index not in matched_tracks
                            and current_time - track.last_time <= max_gap_seconds
                            and candidate_matches_track(track, candidate)
                        )
                    ),
                    None,
                )
                if match_index is None:
                    tracks.append(
                        Track(
                            box=candidate.box,
                            # 此候选已与三秒前画面比对确认静止，起点可安全
                            # 回溯到该验证窗口的开头。
                            start_time=max(
                                timestamp, current_time - candidate.lead_seconds
                            ),
                            last_time=current_time,
                            best_frame=candidate.frame,
                            best_sharpness=candidate.sharpness,
                            last_seen_index=sample_index,
                            last_signature=candidate.signature,
                            detector=candidate.detector,
                        )
                    )
                    matched_tracks.add(len(tracks) - 1)
                else:
                    tracks[match_index].update(candidate, sample_index)
                    matched_tracks.add(match_index)
    finally:
        capture.release()
    # 时间点复核同样可能先看到图片的一小块，再看到完整图片；先合并
    # 重叠轨迹，选择时才能拿到完整素材，而不是局部候选。
    return merge_tracks(tracks)


def extract_static_images_at_timestamps(
    video_path: Path,
    output_dir: Path,
    timestamps: list[float],
    sample_fps: float = DEFAULT_SAMPLE_FPS,
    min_static_seconds: float = DEFAULT_MIN_STATIC_SECONDS,
    diff_threshold: int = DEFAULT_DIFF_THRESHOLD,
    motion_window_seconds: float = DEFAULT_MOTION_WINDOW_SECONDS,
    analysis_width: int = DEFAULT_ANALYSIS_WIDTH,
    search_seconds: float = 12.0,
) -> list[dict]:
    """按指定时间点导出经连续静止时长验证的完整图片卡片。"""
    if sample_fps <= 0 or min_static_seconds <= 0 or search_seconds <= 0:
        raise ValueError("采样频率、静止时间和搜索时长必须大于 0。")

    output_dir.mkdir(parents=True, exist_ok=True)
    metadata: list[dict] = []
    time_step = 1 / sample_fps

    for output_index, timestamp in enumerate(timestamps, start=1):
        # 时间点可能落在卡片的展示中段，例如 0:54 的文章图已在数秒前
        # 出现。向前补扫一个最短静止时长，才能真正验证它持续展示至少 5 秒。
        search_start = max(0.0, timestamp - min_static_seconds)
        tracks = _collect_card_tracks_near_timestamp(
            video_path,
            search_start,
            sample_fps,
            search_seconds + (timestamp - search_start),
            diff_threshold,
            motion_window_seconds,
            analysis_width,
        )
        eligible_tracks = [
            track
            for track in tracks
            if (
                track.last_time - track.start_time + time_step + 0.01 >= min_static_seconds
            )
        ]
        if not eligible_tracks:
            raise RuntimeError(
                f"在 {format_timestamp(timestamp)} 后 {search_seconds:.1f} 秒内，"
                f"未找到持续至少 {min_static_seconds:.1f} 秒的完整静态图片。"
            )

        # 优先选择覆盖请求时间点的卡片。若时间点刚好落在转场空档，
        # 则优先选择其后出现的完整卡片，避免误取刚刚消失的上一张。
        covering_tracks = [
            track
            for track in eligible_tracks
            if track.start_time <= timestamp <= track.last_time
        ]
        if covering_tracks:
            selected = min(
                covering_tracks,
                key=lambda track: (-track.samples, -(track.box[2] * track.box[3])),
            )
        else:
            future_tracks = [track for track in eligible_tracks if track.start_time > timestamp]
            selection_pool = future_tracks or eligible_tracks
            selected = min(
                selection_pool,
                key=lambda track: (
                    abs(track.start_time - timestamp),
                    -track.samples,
                    -(track.box[2] * track.box[3]),
                ),
            )
        filename = f"{output_index:03d}_{format_timestamp(timestamp)}.png"
        output_path = output_dir / filename
        # 不采用轨迹中“最清晰”的旧帧：字幕会让拉普拉斯锐度虚高。覆盖请求
        # 时刻的轨迹，必须回到请求时刻重新检测边缘；这样不会因为展示尾部的
        # 转场或字幕变化而把图片裁成半张，或把画面背景带进来。
        source_time = (
            timestamp
            if selected.start_time <= timestamp <= selected.last_time
            else selected.last_time
        )
        capture = cv2.VideoCapture(str(video_path))
        try:
            source_frame = _read_frame_at_time(capture, source_time)
            comparison_frame = _read_frame_at_time(
                capture, max(0.0, source_time - motion_window_seconds)
            )
        finally:
            capture.release()
        analysis_frame, inverse_scale = resize_for_analysis(source_frame, analysis_width)
        comparison_analysis, _ = resize_for_analysis(comparison_frame, analysis_width)
        comparison_gray = cv2.GaussianBlur(
            cv2.cvtColor(comparison_analysis, cv2.COLOR_BGR2GRAY), (3, 3), 0
        )
        refreshed_candidates = detect_static_card_candidates(
            comparison_gray,
            analysis_frame,
            source_frame,
            inverse_scale,
            source_time,
            diff_threshold,
            motion_window_seconds,
        )
        matching_candidates = [
            candidate
            for candidate in refreshed_candidates
            if box_iou(candidate.box, selected.box) >= 0.45
        ]
        selected_box = selected.box
        if matching_candidates:
            refreshed_box = max(
                matching_candidates,
                key=lambda candidate: candidate.box[2] * candidate.box[3],
            ).box
            selected_area = selected.box[2] * selected.box[3]
            refreshed_area = refreshed_box[2] * refreshed_box[3]
            # 画面中的局部候选可能随背景循环交替出现。轨迹已经在多帧
            # 中确认过完整框，单帧刷新不能把它降级成面积明显更小的局部框。
            if refreshed_area >= selected_area * 0.90:
                selected_box = refreshed_box
        image = crop_original_frame(source_frame, selected_box, inverse_scale, padding=0)
        if image.size == 0:
            raise RuntimeError(f"无法从确认帧裁切图片：{format_timestamp(timestamp)}")
        if not cv2.imwrite(str(output_path), image):
            raise RuntimeError(f"无法写入图片：{output_path}")

        confirmed_duration = selected.last_time - selected.start_time + time_step
        metadata.append(
            {
                "file": filename,
                "requested_seconds": round(timestamp, 3),
                "first_confirmed_seconds": round(selected.start_time, 3),
                "last_confirmed_seconds": round(selected.last_time, 3),
                "confirmed_duration_seconds": round(confirmed_duration, 3),
                "samples": selected.samples,
                "analysis_box": list(selected_box),
                "source_frame_seconds": round(source_time, 3),
            }
        )
        print(
            f"保存复核图片：{output_path.name}，"
            f"确认静止 {confirmed_duration:.1f} 秒"
        )

    metadata_path = output_dir / "提取结果.json"
    metadata_path.write_text(
        json.dumps(
            {
                "source_video": str(video_path),
                "mode": "timestamp_review",
                "sample_fps": sample_fps,
                "min_static_seconds": min_static_seconds,
                "search_seconds": search_seconds,
                "images": metadata,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"完成：按时间点复核并导出 {len(metadata)} 张图片。")
    print(f"结果清单：{metadata_path}")
    return metadata


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
    completed_tracks: list[Track] = []
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

        stable_candidates = detect_candidates(
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
        card_candidates = detect_static_card_candidates(
            frame_history[0],
            frame,
            original_frame,
            inverse_scale,
            timestamp,
            diff_threshold,
            motion_window_seconds,
        )
        candidates = merge_candidates(card_candidates, stable_candidates)

        matched_tracks: set[int] = set()
        for candidate in candidates:
            match_index = next(
                (
                    index
                    for index, track in enumerate(tracks)
                    if index not in matched_tracks and candidate_matches_track(track, candidate)
                ),
                None,
            )
            if match_index is None:
                tracks.append(
                    Track(
                        box=candidate.box,
                        # 候选区要先累计足够多的稳定采样帧才会被检测到；
                        # 将轨迹回溯至这段已确认稳定区间的开头，避免漏算前段时长。
                        start_time=max(
                            0.0,
                            timestamp
                            - (candidate.lead_seconds or min_samples / sample_fps),
                        ),
                        last_time=timestamp,
                        best_frame=candidate.frame,
                        best_sharpness=candidate.sharpness,
                        last_seen_index=sample_index,
                        last_signature=candidate.signature,
                        detector=candidate.detector,
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
                completed_tracks.append(track)
            else:
                retained_tracks.append(track)
        tracks = retained_tracks
    completed_tracks.extend(tracks)

    # 先合并重叠轨迹，再按时间顺序导出。这样局部检测框不会各自生成
    # 文件，也不会因轨迹结束时机不同而打乱素材顺序。
    merged_tracks = merge_tracks(completed_tracks)
    saved_hashes: list[np.ndarray] = []
    metadata: list[dict] = []
    for track in sorted(merged_tracks, key=lambda item: item.start_time):
        save_track(
            track,
            output_dir,
            len(metadata) + 1,
            saved_hashes,
            metadata,
            min_static_seconds,
        )

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
    parser.add_argument(
        "--timestamps",
        help="按指定时间点复核导出图片，多个时间点以英文逗号分隔，例如 00:54,00:57,03:30。",
    )
    parser.add_argument(
        "--timestamp-search-seconds",
        type=float,
        default=12.0,
        help="每个指定时间点后继续复核的秒数，默认 12。",
    )
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

        if args.timestamps:
            extract_static_images_at_timestamps(
                video_path,
                args.output_dir,
                parse_timestamp_list(args.timestamps),
                sample_fps=args.sample_fps,
                min_static_seconds=args.min_static_seconds,
                diff_threshold=args.diff_threshold,
                motion_window_seconds=args.motion_window_seconds,
                analysis_width=args.analysis_width,
                search_seconds=args.timestamp_search_seconds,
            )
        else:
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
