#!/usr/bin/env python3
# coding: utf-8
"""
从成品视频中**自动识别叠加图片**并保存到本地。

核心思路：
1. 以较低 FPS 抽帧，降低运算量；
2. 连续帧做逐像素差分，通过累计“低变化计数”定位**长期静止**区域；
3. 对静止像素做连通域分析，得到可能的图片区域（bounding box）；
4. 当某个区域在视频中连续保持 ≥ min_persist 秒即认为是真正叠图；
5. 叠图消失或检测到足够帧后，将叠图裁剪为 PNG 并保存。

实验阈值已在代码里给出，必要时请自行微调：
    • sample_fps：   每秒抽多少帧做检测
    • diff_thresh：  单像素灰度差阈值（0~255）
    • area_thresh：  叠图最小面积占整帧比例
    • min_persist：  叠图最短持续时间（秒）

依赖：
    pip install opencv-python numpy tqdm

用法示例：
    python3 auto_detect_overlays.py input.mp4 -o shots

输出：
    shots/overlay_001_123.4s.png  # 第1个叠图，出现时刻123.4秒
"""
import cv2, numpy as np, argparse, os, math, hashlib, subprocess, shlex, json
from pathlib import Path
from tqdm import tqdm

DEFAULT_SAMPLE_FPS   = 3
DIFF_THRESH          = 8
# 叠图面积下限（相对整帧）与上限，避免选到太小噪点或整帧大块
MIN_AREA_RATIO       = 0.04
MAX_AREA_RATIO       = 0.40
# 允许的长宽比范围（横向 0.3 起，竖向 3.0 封顶）
MIN_ASPECT_RATIO     = 0.3
MAX_ASPECT_RATIO     = 3.0
# 纹理锐度阈值（Laplacian 方差）
MIN_EDGE_VAR         = 50
AREA_THRESH_RATIO    = MIN_AREA_RATIO
MIN_PERSIST_SEC      = 5.0
MAX_PERSIST_SEC      = 12.0
SAVE_MARGIN          = 4
BOTTOM_IGNORE_RATIO  = 0.18

class OverlayTracker:
    def __init__(self, frame, bbox, timestamp, total_mmss):
        self.total_mmss = total_mmss
        x, y, w, h = bbox
        self.bbox = bbox
        self.start_ts = timestamp
        self.last_ts  = timestamp
        self.frames   = [frame[y:y+h, x:x+w]]
        self.saved    = False
        # 存储原始图片用于相似度比较
        self.original_image = frame[y:y+h, x:x+w].copy()
    def update(self, frame, timestamp):
        x, y, w, h = self.bbox
        self.last_ts = timestamp
        self.frames.append(frame[y:y+h, x:x+w])
    def should_finalize(self, current_ts, min_persist):
        duration = self.last_ts - self.start_ts
        # 必须持续 5-12 秒，且 0.5 秒内无更新
        return (min_persist <= duration <= MAX_PERSIST_SEC) and (current_ts - self.last_ts) >= 0.5
    def save(self, out_dir, idx):
        if self.saved:
            return
        mid_idx = len(self.frames)//2
        img = self.frames[mid_idx]
        def sec_to_mmss(sec:float):
            m=int(sec)//60; s=int(sec)%60; return f"{m:02d}{s:02d}"
        appear = sec_to_mmss(self.start_ts)
        filename = f"{appear}-{self.total_mmss}.png"
        out_path = out_dir / filename
        cv2.imwrite(str(out_path), img)
        self.saved = True
        print(f"✅ 保存叠图 → {out_path}")

def calculate_image_similarity(img1, img2, threshold=0.85):
    """计算两张图片的相似度，处理字幕遮挡情况"""
    if img1.shape != img2.shape:
        # 调整到相同尺寸
        h, w = min(img1.shape[0], img2.shape[0]), min(img1.shape[1], img2.shape[1])
        img1 = cv2.resize(img1, (w, h))
        img2 = cv2.resize(img2, (w, h))
    
    # 方法1: 结构相似性 (SSIM)
    try:
        from skimage.metrics import structural_similarity as ssim
        gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY) if len(img1.shape) == 3 else img1
        gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY) if len(img2.shape) == 3 else img2
        similarity = ssim(gray1, gray2)
        if similarity > threshold:
            return True
    except ImportError:
        pass
    
    # 方法2: 直方图比较 (备选方案)
    hist1 = cv2.calcHist([img1], [0, 1, 2], None, [50, 50, 50], [0, 256, 0, 256, 0, 256])
    hist2 = cv2.calcHist([img2], [0, 1, 2], None, [50, 50, 50], [0, 256, 0, 256, 0, 256])
    correlation = cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL)
    
    # 方法3: 特征点匹配 (最鲁棒)
    try:
        gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY) if len(img1.shape) == 3 else img1
        gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY) if len(img2.shape) == 3 else img2
        
        # 使用ORB特征检测
        orb = cv2.ORB_create(nfeatures=100)
        kp1, des1 = orb.detectAndCompute(gray1, None)
        kp2, des2 = orb.detectAndCompute(gray2, None)
        
        if des1 is not None and des2 is not None and len(des1) > 10 and len(des2) > 10:
            # 特征匹配
            bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
            matches = bf.match(des1, des2)
            matches = sorted(matches, key=lambda x: x.distance)
            
            # 好的匹配点比例
            good_matches = [m for m in matches if m.distance < 50]
            match_ratio = len(good_matches) / min(len(des1), len(des2))
            
            if match_ratio > 0.3:  # 30%的特征点匹配
                return True
    except Exception:
        pass
    
    # 如果特征匹配失败，使用直方图相关性
    return correlation > 0.8

def iou(boxA, boxB):
    xA = max(boxA[0], boxB[0]); yA = max(boxA[1], boxB[1])
    xB = min(boxA[0]+boxA[2], boxB[0]+boxB[2]); yB = min(boxA[1]+boxA[3], boxB[1]+boxB[3])
    inter = max(0, xB - xA) * max(0, yB - yA)
    if inter == 0:
        return 0.0
    union = boxA[2]*boxA[3] + boxB[2]*boxB[3] - inter
    return inter / union

def build_background_model(video_path: str, sample_count: int = 50):
    """构建背景模型，用于分离叠加图片"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise IOError(f"无法打开视频构建背景: {video_path}")
    
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames == 0:
        cap.release()
        return None
    
    print(f"🎬 构建背景模型，采样 {sample_count} 帧...")
    frames = []
    step = max(1, total_frames // sample_count)
    
    for i in range(0, total_frames, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, frame = cap.read()
        if ret:
            frames.append(frame.astype(np.float32))
        if len(frames) >= sample_count:
            break
    
    cap.release()
    
    if not frames:
        return None
    
    # 返回中位数背景（排除叠图影响）
    background = np.median(frames, axis=0).astype(np.uint8)
    print(f"✅ 背景模型构建完成，基于 {len(frames)} 帧")
    return background

def build_background_model(video_path: str, sample_count: int = 50):
    """构建背景模型 - 用于分离背景视频和叠加图片"""
    cap = cv2.VideoCapture(video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    if total_frames == 0:
        cap.release()
        return None
    
    frames = []
    step = max(1, total_frames // sample_count)
    
    print(f"🎬 构建背景模型: 从 {total_frames} 帧中采样 {sample_count} 帧")
    
    for i in range(0, total_frames, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, frame = cap.read()
        if ret:
            frames.append(frame.astype(np.float32))
        if len(frames) >= sample_count:
            break
    
    cap.release()
    
    if not frames:
        return None
    
    # 使用中位数作为背景（更好地排除叠图影响）
    background = np.median(frames, axis=0).astype(np.uint8)
    print(f"✅ 背景模型构建完成")
    
    return background

def detect_overlays(video_path: str, out_dir: Path, sample_fps: int = DEFAULT_SAMPLE_FPS, diff_thresh: int = DIFF_THRESH, area_ratio_thresh: float = AREA_THRESH_RATIO, min_persist_sec: float = MIN_PERSIST_SEC, bottom_ignore_ratio: float = BOTTOM_IGNORE_RATIO):
    # 先构建背景模型
    background = build_background_model(video_path)
    if background is None:
        raise IOError(f"无法构建背景模型: {video_path}")
    
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise IOError(f"无法打开视频: {video_path}")
    orig_fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    total_seconds = total_frames / orig_fps if orig_fps>0 else 0
    total_mmss = f"{int(total_seconds)//60:02d}{int(total_seconds)%60:02d}"
    frame_interval = int(max(1, orig_fps // sample_fps))
    width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_area = width * height
    trackers = []
    saved_count = 0
    frame_id = 0
    saved_images = []  # 存储已保存的图片用于相似度比较
    prev_gray = None
    pbar_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) / frame_interval) + 1
    pbar = tqdm(total=pbar_total, desc="扫描中", unit="帧")
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frame_id += 1
        if frame_id % frame_interval != 0:
            continue
        ts = frame_id / orig_fps
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        
        # 背景减除：找出与背景不同的区域（叠加图片）
        bg_gray = cv2.cvtColor(background, cv2.COLOR_BGR2GRAY)
        diff_from_bg = cv2.absdiff(gray, bg_gray)
        _, bg_mask = cv2.threshold(diff_from_bg, 25, 255, cv2.THRESH_BINARY)
        
        # 原有的帧间差分（用于检测静止）
        if prev_gray is None:
            prev_gray = gray.copy(); continue
        diff = cv2.absdiff(gray, prev_gray)
        _, static_mask = cv2.threshold(diff, diff_thresh, 255, cv2.THRESH_BINARY_INV)
        
        # 结合两个掩码：既与背景不同，又相对静止的区域
        mask_static = cv2.bitwise_and(bg_mask, static_mask)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        clean = cv2.morphologyEx(mask_static, cv2.MORPH_OPEN, kernel, iterations=2)
        contours, _ = cv2.findContours(clean, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidate_boxes = []
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            area = w * h
            if area < frame_area * MIN_AREA_RATIO or area > frame_area * MAX_AREA_RATIO:
                continue
            aspect = w / h if h else 0
            if aspect < MIN_ASPECT_RATIO or aspect > MAX_ASPECT_RATIO:
                continue
            # 纹理锐度（排除纯色/模糊背景）
            patch = gray[y:y+h, x:x+w]
            edge_var = cv2.Laplacian(patch, cv2.CV_64F).var()
            if edge_var < MIN_EDGE_VAR:
                continue
            ignore_y = int(height * (1 - bottom_ignore_ratio))
            if y >= ignore_y:
                continue
            if y + h > ignore_y:
                h = ignore_y - y
                if h <= 0:
                    continue
            x = max(0, x - SAVE_MARGIN)
            y = max(0, y - SAVE_MARGIN)
            w = min(width - x,  w + 2*SAVE_MARGIN)
            h = min(height - y, h + 2*SAVE_MARGIN)
            candidate_boxes.append((x, y, w, h))
        # --- 非极大抑制：去掉重叠框 ---
        candidate_boxes.sort(key=lambda b: b[2]*b[3], reverse=True)
        filtered_boxes = []
        for box in candidate_boxes:
            if all(iou(box, b) < 0.3 for b in filtered_boxes):
                filtered_boxes.append(box)
        for box in filtered_boxes:
            matched = False
            for trk in trackers:
                # 极严格的位置匹配：坐标偏移 < 5px（图片位置完全固定）
                box_x, box_y, box_w, box_h = box
                trk_x, trk_y, trk_w, trk_h = trk.bbox
                
                # 检查左上角和右下角的位置偏移
                if (abs(box_x - trk_x) < 5 and abs(box_y - trk_y) < 5 and 
                    abs((box_x + box_w) - (trk_x + trk_w)) < 5 and 
                    abs((box_y + box_h) - (trk_y + trk_h)) < 5):
                    trk.update(frame, ts); matched = True; break
            if not matched:
                trackers.append(OverlayTracker(frame, box, ts, total_mmss))
        for trk in list(trackers):
            if trk.should_finalize(ts, min_persist_sec):
                # 检查是否已保存过相似的图片
                is_duplicate = False
                for saved_img in saved_images:
                    if calculate_image_similarity(trk.original_image, saved_img):
                        is_duplicate = True
                        print(f"⚠️ 跳过相似图片 (字幕遮挡变体)")
                        break
                
                if not is_duplicate:
                    saved_count += 1
                    trk.save(out_dir, saved_count)
                    saved_images.append(trk.original_image.copy())
                    print(f"📸 保存新图片 #{saved_count}")
                
                trackers.remove(trk)
        prev_gray = gray.copy(); pbar.update(1)
    for trk in trackers:
        duration = trk.last_ts - trk.start_ts
        if min_persist_sec <= duration <= MAX_PERSIST_SEC:
            # 最后也要检查相似度去重
            is_duplicate = False
            for saved_img in saved_images:
                if calculate_image_similarity(trk.original_image, saved_img):
                    is_duplicate = True
                    break
            
            if not is_duplicate:
                saved_count += 1
                trk.save(out_dir, saved_count)
                saved_images.append(trk.original_image.copy())
    pbar.close(); cap.release(); print(f"🎉 完成，共识别并保存 {saved_count} 张叠图。")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="自动下载(可选) + 转码 + 识别叠加图片")
    parser.add_argument("source", help="本地视频文件路径或在线视频 URL")
    parser.add_argument("-o", "--output", default="extracted_images", help="输出目录")
    parser.add_argument("--sample_fps", type=int, default=DEFAULT_SAMPLE_FPS, help="检测时每秒抽多少帧 (默认3)")
    parser.add_argument("--diff", type=int, default=DIFF_THRESH, help="像素灰度差阈值 (默认8)")
    parser.add_argument("--area", type=float, default=AREA_THRESH_RATIO, help="叠图最小面积比例 (默认0.02)")
    parser.add_argument("--persist", type=float, default=MIN_PERSIST_SEC, help="叠图最短持续时间 (秒, 默认2.0)")
    parser.add_argument("--ignore_bottom", type=float, default=BOTTOM_IGNORE_RATIO, help="从底部忽略的高度比例 (0~1, 默认0.18)")
    parser.add_argument("--browser", default="chrome", help="下载 YouTube 时自动提取的浏览器名 (默认: chrome)")
    args = parser.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    # -------- 清理旧的图片 --------
    for p in out_dir.glob("*.png"):
        if p.name != "download.png":  # 保留非截图文件
            try:
                p.unlink()
            except Exception:
                pass

    src = args.source

    # -------- Step 1: 如为 URL 则先下载 --------
    if src.startswith("http://") or src.startswith("https://"):
        import re, urllib.parse
        vid_match = re.search(r"[?&]v=([\w-]{11})", src)
        vid = vid_match.group(1) if vid_match else "video"
        download_path = out_dir / f"{vid}.webm"
        if download_path.exists():
            print(f"📂 已存在 {download_path}，跳过下载")
        else:
            print(f"🌐 下载视频 → {download_path}")
            cmd = [
            "yt-dlp",
            "--cookies-from-browser", args.browser,
            "-f", "bestvideo+bestaudio",
            "--merge-output-format", "webm",
            "-o", str(download_path),
            src
        ]
            subprocess.run(cmd, check=True)
        src = str(download_path)

    # -------- Step 2: 转码为 H.264 MP4 --------
    converted_path = out_dir / (Path(src).stem + "_h264.mp4")

    def video_codec(path:str) -> str:
        try:
            probe = subprocess.check_output([
                "ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=codec_name","-of","json",path
            ], stderr=subprocess.DEVNULL)
            info = json.loads(probe)
            return info["streams"][0]["codec_name"]
        except Exception:
            return "unknown"

    need_convert = True
    if converted_path.exists():
        if video_codec(str(converted_path)) == "h264":
            need_convert = False
    if need_convert:
        print(f"🎬 转码为 H.264 MP4 → {converted_path}")
        cmd = [
            "ffmpeg", "-y", "-i", src,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "copy",
            str(converted_path)
        ]
        subprocess.run(cmd, check=True)
    else:
        print(f"📂 已存在 H.264 文件 {converted_path}，跳过转码")

    # -------- Step 3: 识别叠图 --------
    detect_overlays(str(converted_path), out_dir,
                    sample_fps=args.sample_fps,
                    diff_thresh=args.diff,
                    area_ratio_thresh=args.area,
                    min_persist_sec=args.persist,
                    bottom_ignore_ratio=args.ignore_bottom)

if __name__ == "__main__":
    main()