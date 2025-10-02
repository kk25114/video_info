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
import cv2, numpy as np, os, math, hashlib, subprocess, shlex, json, re, requests

def analyze_text_with_deepseek(text: str):
    """使用 DeepSeek API 分析文本，返回摘要和关键词。"""
    print("🤖 调用 DeepSeek API 进行内容分析...")
    config_path = '/home/github/video_info/config.json'
    api_key = None
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                api_key = json.load(f).get("DEEPSEEK_API_KEY")
        except Exception as e:
            print(f"⚠️ 读取配置文件 {config_path} 时出错: {e}")

    if not api_key:
        print("❌ 错误: 未在 config.json 中找到 DEEPSEEK_API_KEY，无法进行 AI 分析。")
        return None

    api_url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    prompt = (
        "你是一名资深新闻编辑。请阅读以下视频文稿，并以严格的 JSON 格式完成两项任务：\n"
        "1. `summary`: 生成一段不超过150字的摘要，精准概括文稿讨论的核心新闻事件。\n"
        "2. `keywords`: 提取3个最适合用于搜索相关新闻的关键词（字符串数组）。\n\n"
        "确保你的回复只有纯粹的 JSON 对象，不包含任何额外的解释或标记。\n\n"
        "--- 文稿开始 ---\n"
        f"{text}\n"
        "--- 文稿结束 ---"
    )

    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"}
    }

    try:
        response = requests.post(api_url, headers=headers, json=data, timeout=180)
        response.raise_for_status()
        content_str = response.json()['choices'][0]['message']['content']
        analysis_data = json.loads(content_str)
        
        if 'summary' in analysis_data and 'keywords' in analysis_data:
            print("✅ AI 内容分析完成。")
            return analysis_data
        else:
            print("❌ 错误: AI 返回的 JSON 格式不符合预期。")
            return None
    except requests.exceptions.RequestException as e:
        print(f"❌ 错误: 调用 DeepSeek API 时出错: {e}")
        return None
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        print(f"❌ 错误: 解析 AI 响应时出错: {e}")
        return None

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





def detect_overlays(video_path: str, out_dir: Path, sample_fps: int = DEFAULT_SAMPLE_FPS, diff_thresh: int = DIFF_THRESH, area_ratio_thresh: float = AREA_THRESH_RATIO, min_persist_sec: float = MIN_PERSIST_SEC, bottom_ignore_ratio: float = BOTTOM_IGNORE_RATIO):
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
        
        # 原有的帧间差分（用于检测静止）
        if prev_gray is None:
            prev_gray = gray.copy(); continue
        diff = cv2.absdiff(gray, prev_gray)
        _, static_mask = cv2.threshold(diff, diff_thresh, 255, cv2.THRESH_BINARY_INV)
        
        # 使用帧间差分的结果作为唯一的掩码
        mask_static = static_mask
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

def parse_srt_file(file_path: str) -> str:
    """读取 SRT 文件并提取所有文本内容。"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # 使用正则表达式移除序号、时间戳和空行，只留下文本
        text_only = re.sub(r'\d+\n\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}\n', '', content)
        return text_only.strip()
    except FileNotFoundError:
        print(f"❌ 错误: 字幕文件未找到 -> {file_path}")
        return ""
    except Exception as e:
        print(f"❌ 读取或解析 SRT 文件时出错: {e}")
        return ""

def find_latest_srt_file():
    """自动查找 mk_video 目录下最新的 .srt 字幕文件。"""
    mk_video_dir = "/home/github/video_info/mk_video/"
    if not os.path.exists(mk_video_dir):
        print(f"❌ 错误: 目录不存在 -> {mk_video_dir}")
        return None

    srt_files = []
    for filename in os.listdir(mk_video_dir):
        if filename.endswith('.srt'):
            file_path = os.path.join(mk_video_dir, filename)
            mtime = os.path.getmtime(file_path)
            srt_files.append((mtime, file_path, filename))

    if not srt_files:
        print(f"❌ 错误: 在 {mk_video_dir} 中未找到 .srt 字幕文件")
        return None

    # 按修改时间排序，取最新的文件
    srt_files.sort(reverse=True)
    latest_file = srt_files[0][1]
    print(f"📁 找到 {len(srt_files)} 个字幕文件，使用最新的: {srt_files[0][2]}")
    return latest_file

import cv2, numpy as np, argparse, os, math, hashlib, subprocess, shlex, json, re, requests\n\ndef parse_srt_file(file_path: str) -> str:\n    \"\"\"读取 SRT 文件并提取所有文本内容。\"\"\"\n    try:\n        with open(file_path, \'r\', encoding=\'utf-8\') as f:\n            content = f.read()\n        # 使用正则表达式移除序号、时间戳和空行，只留下文本\n        text_only = re.sub(r\'\\d+\\n\\d{2}:\\d{2}:\\d{2},\\d{3} --> \\d{2}:\\d{2}:\\d{2},\\d{3}\\n\', \'\', content)\n        return text_only.strip()\n    except FileNotFoundError:\n        print(f\"❌ 错误: 字幕文件未找到 -> {file_path}\")\n        return \"\"\n    except Exception as e:\n        print(f\"❌ 读取或解析 SRT 文件时出错: {e}\")\n        return \"\"\n\ndef analyze_text_with_deepseek(text: str):\n    \"\"\"使用 DeepSeek API 分析文本，返回摘要和关键词。\"\"\"\n    print(\"🤖 调用 DeepSeek API 进行内容分析...\")\n    config_path = \'/home/github/video_info/config.json\'\n    api_key = None\n    if os.path.exists(config_path):\n        try:\n            with open(config_path, \'r\', encoding=\'utf-8\') as f:\n                api_key = json.load(f).get(\"DEEPSEEK_API_KEY\")\n        except Exception as e:\n            print(f\"⚠️ 读取配置文件 {config_path} 时出错: {e}\")\n\n    if not api_key:\n        print(\"❌ 错误: 未在 config.json 中找到 DEEPSEEK_API_KEY，无法进行 AI 分析。\")\n        return None\n\n    api_url = \"https://api.deepseek.com/chat/completions\"\n    headers = {\n        \"Authorization\": f\"Bearer {api_key}\",\n        \"Content-Type\": \"application/json\",\n    }\n\n    prompt = (\n        \"你是一名资深新闻编辑。请阅读以下视频文稿，并以严格的 JSON 格式完成两项任务：\\n\"\n        \"1. `summary`: 生成一段不超过150字的摘要，精准概括文稿讨论的核心新闻事件。\\n\"\n        \"2. `keywords`: 提取3个最适合用于搜索相关新闻的关键词（字符串数组）。\\n\\n\"\n        \"确保你的回复只有纯粹的 JSON 对象，不包含任何额外的解释或标记。\\n\\n\"\n        \"--- 文稿开始 ---\\n\"\n        f\"{text}\\n\"\n        \"--- 文稿结束 ---\"\n    )\n\n    data = {\n        \"model\": \"deepseek-chat\",\n        \"messages\": [{\"role\": \"user\", \"content\": prompt}],\n        \"response_format\": {\"type\": \"json_object\"}\n    }\n\n    try:\n        response = requests.post(api_url, headers=headers, json=data, timeout=180)\n        response.raise_for_status()\n        content_str = response.json()[\'choices\'][0][\'message\'][\'content\']\n        analysis_data = json.loads(content_str)\n        \n        if \'summary\' in analysis_data and \'keywords\' in analysis_data:\n            print(\"✅ AI 内容分析完成。\")\n            return analysis_data\n        else:\n            print(\"❌ 错误: AI 返回的 JSON 格式不符合预期。\")\n            return None\n    except requests.exceptions.RequestException as e:\n        print(f\"❌ 错误: 调用 DeepSeek API 时出错: {e}\")\n        return None\n    except (KeyError, IndexError, json.JSONDecodeError) as e:\n        print(f\"❌ 错误: 解析 AI 响应时出错: {e}\")\n        return None\n\ndef search_recent_articles(keywords: list) -> list:\n    \"\"\"使用关键词搜索最近一周的热门文章，返回文章URL列表。\"\"\"\n    print(f\"🌐 正在搜索最近一周的热门文章，关键词: {keywords}...\")\n    query = \" \".join(keywords) + \" 最新一周\"\n    # 使用 default_api.google_web_search 工具进行搜索\n    search_results = default_api.google_web_search(query=query)\n    \n    articles = []\n    if search_results and \'output\' in search_results:\n        # 假设搜索结果是文本，需要解析出URL\n        # 这是一个简化的解析，实际可能需要更复杂的正则或HTML解析\n        # 目前我们只提取看起来像URL的字符串\n        urls = re.findall(r\'(https?://[^\s]+)\', search_results[\'output\'])\n        # 过滤掉一些明显不是文章链接的URL，例如图片链接、PDF等\n        articles = [url for url in urls if not any(ext in url for ext in [\'.png\', \'.jpg\', \'.pdf\', \'.gif\'])]\n        print(f\"✅ 找到 {len(articles)} 篇文章链接。\")\n    else:\n        print(\"⚠️ 未找到相关文章。\")\n    return articles\n\ndef main():
    # 自动查找最新的字幕文件
    srt_file = find_latest_srt_file()
    if not srt_file:
        return

    print(f"🚀 开始处理字幕文件: {srt_file}")

    srt_text = parse_srt_file(srt_file)
    if not srt_text:
        return

    print("\n---")
    print("📝 提取的字幕文本内容:")
    print(srt_text[:500] + "..." if len(srt_text) > 500 else srt_text)
    print("---")

    # 2. AI 总结字幕内容并提取关键词
    analysis_result = analyze_text_with_deepseek(srt_text)
    if not analysis_result:
        return

    print("\n---")
    print("🤖 AI 分析结果:")
    print(f"  - 摘要: {analysis_result.get('summary')}")
    print(f"  - 关键词: {analysis_result.get('keywords')}")
    print("---")

if __name__ == "__main__":
    main()