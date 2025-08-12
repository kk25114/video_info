#!/usr/bin/env python3
# coding: utf-8
"""
背景视频 + 配音 + SRT 字幕 + 覆盖图层 => MP4，并生成封面 cover.png
本版：封面/片头均为"居中两行"，无外部红框；上下行字号不同，上大下小
覆盖图层支持：PNG, JPG, JPEG, GIF, BMP, WebP
用法：python build.py
前置：已安装 ffmpeg/ffprobe 并在 PATH 中
"""

import subprocess, shlex, time, re, threading, datetime
from pathlib import Path

# ---------- 基础配置 ----------
BG_VIDEO_OVERRIDE = None  # 指定绝对或相对路径覆盖背景（测试用），否则自动轮换
AUDIO = None  # 运行时自动检测脚本目录下的 .wav 文件
TITLE_DURATION = 5  # 片头两行文字显示时长（秒）
OUTPUT_DIR = Path("/mnt/d/Program Files/下载")
OUTPUT_MP4 = None  # 运行时生成
COVER_PNG  = None  # 运行时生成
FPS        = 30
SIZE       = (1280, 720)                                    # 输出分辨率（固定 720p）

# 额外叠图（可选）
# (文件, start, end, scale_ratio, x, y)
IMAGES = [
    # ("shots/01.png", 5, 12, 0.25, "(W-w)/2", "(H-h)/2"),
]

# ---------- 风格与布局（本版均居中，无外部红框） ----------
# 两行字号（相对高度的比例）
FS1_RATIO = 0.112            # 第一行字号 ≈ H*0.112（上行更大）
FS2_RATIO = 0.080            # 第二行字号 ≈ H*0.080（下行略小）
GAP_RATIO = 0.099            # 两行间距（像素 = H*GAP_RATIO）

# 轻微上下微调像素（整体上移为负，下移为正；通常保持 0 即可）
CENTER_NUDGE_PX = 0

# 文字描边与底色（示例：浅红底、黄字、黑描边）
TEXT_COLOR     = "yellow"
STROKE_COLOR   = "black"
STROKE_W       = 6          # 黑描边厚度（像素）
BOX_COLOR      = "0xE63946@1.0"  # 深红底（@1.0 不透明）
BOX_PAD        = 34          # 红底内边距（boxborderw）

# 封面外部红框（本版不要）
COVER_DRAW_FRAME = False     # 一律 False
FRAME_COLOR   = "0xE23D3D@1.0"
FRAME_THICK   = 6
FRAME_X_RATIO = 0.60
FRAME_Y_RATIO = 0.18
FRAME_W_RATIO = 0.36
FRAME_H_RATIO = 0.46

# 从背景视频第几秒截帧作为封面底图
COVER_SEEK_SEC = 2.0

# 字体候选（按顺序找一个可用的）
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Heavy.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "simhei.ttf",
]

# 每行最大字符（防止过长出框；不想截断可调大或置为很大）
MAX_CHARS_PER_LINE = 12
# --------------------------------

def run(cmd: str):
    print("→", cmd)
    subprocess.run(cmd, shell=True, check=True)

def probe_duration(path: str) -> float:
    out = subprocess.check_output(
        f"ffprobe -v error -show_entries format=duration -of csv=p=0 {shlex.quote(path)}",
        shell=True
    )
    return float(out)

def pick_font() -> str:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return ""  # 找不到就交给 ffmpeg 默认字体（可能不支持中文）

def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")

def split_title(raw: str):
    # 按中文/英文逗号或句号拆两行；过长则对半切；不足两行则只返回一行
    if '，' in raw:    parts = raw.split('，', 1)
    elif ',' in raw:  parts = raw.split(',', 1)
    elif '。' in raw:  parts = raw.split('。', 1)
    elif len(raw) > 8:
        mid = len(raw)//2; parts = [raw[:mid], raw[mid:]]
    else:
        parts = [raw]
    return [p.strip()[:MAX_CHARS_PER_LINE] for p in parts if p.strip()]

def pick_bg_video() -> Path:
    """按天顺序循环选择 videos 目录的背景视频。
    原理：在 .bg_index 文件中存储 "YYYY-MM-DD,idx"。
    当天再次运行脚本时复用同一视频；到新的一天自动换下一支。"""
    script_dir = Path(__file__).parent
    vid_dir = script_dir / "videos"
    vids = sorted(vid_dir.glob("*.mp4"))
    if not vids:
        raise FileNotFoundError(f"未在 {vid_dir} 找到任何 .mp4 背景视频")

    idx_file = vid_dir / ".bg_index"
    today = datetime.date.today().isoformat()
    last_idx = -1
    last_date = None
    if idx_file.exists():
        try:
            content = idx_file.read_text().strip()
            last_date, idx_str = content.split(",")
            last_idx = int(idx_str)
        except Exception:
            last_idx = -1
            last_date = None

    # 如果还是今天，就复用上一索引；否则顺序+1
    if last_date == today and 0 <= last_idx < len(vids):
        next_idx = last_idx
    else:
        next_idx = (last_idx + 1) % len(vids)
        try:
            idx_file.write_text(f"{today},{next_idx}")
        except Exception as e:
            print(f"⚠️ 无法写入 {idx_file}: {e}")

    return vids[next_idx]


def load_overlay_images(total_duration: float):
    """
    扫描 images 目录下的图片文件，根据文件名推算插入时间。
    支持的格式：PNG, JPG, JPEG, GIF, BMP, WebP

    支持两种命名形式：
    1. "3:20/10:25-描述.png" → 代表在一段总长 10:25 的素材里，3:20 处插入。
        算法：ratio = 3:20 / 10:25，然后 start = ratio * total_duration。
    2. "3:20-20:00.png"（旧）或 "3:20-描述.png"（若只给比例）。
        旧算法：视为 3/20 比例。

    每张图默认持续 70 秒，但如果下一张图片开始时间早于当前图片的默认结束时间，
    则当前图片的结束时间设为下一张图片的开始时间，避免重叠显示。
    返回值: [(path, start, end, scale_ratio, x, y), ...]
    """
    script_dir = Path(__file__).parent
    img_dir = script_dir / "images"
    if not img_dir.exists():
        return []

    def timestr_to_sec(ts: str) -> float:
        """将 mm:ss 或 ss 格式转成秒"""
        if ':' in ts:
            m, s = ts.split(':', 1)
            return int(m) * 60 + int(s)
        return float(ts)

    res = []
    # 支持多种图片格式：PNG, JPG, JPEG, GIF, BMP, WebP
    image_extensions = ["*.png", "*.jpg", "*.jpeg", "*.gif", "*.bmp", "*.webp"]
    image_files = []
    for ext in image_extensions:
        image_files.extend(img_dir.glob(ext))
    
    for p in sorted(image_files):
        stem = p.stem
        # 1) 尝试匹配 320-1020 这种纯数字 mmss-mmss 格式
        m_num = re.match(r"^(\d{3,4})-(\d{3,4})", stem)
        if m_num:
            def mmss_to_sec(s: str) -> int:
                m, s2 = divmod(int(s), 100)
                return m * 60 + s2
            t_sec  = mmss_to_sec(m_num.group(1))
            tot_sec = mmss_to_sec(m_num.group(2))
            if tot_sec > 0:
                ratio = t_sec / tot_sec
                start = ratio * total_duration
                scale_expr = f"min(1\\,{SIZE[0]}/iw\\,{SIZE[1]}/ih)"
                res.append((str(p), start, start + 70, scale_expr, "(W-w)/2", "(H-h)/2"))
            continue

        # 2) 尝试匹配 a:b/c:d 含冒号格式
        m = re.match(r"^(\d{1,2}:\d{2})[\/_-](\d{1,2}:\d{2})", stem)
        if m:
            t_str, total_str = m.group(1), m.group(2)
            t = timestr_to_sec(t_str)
            total = timestr_to_sec(total_str)
            if total > 0:
                ratio = t / total
                start = ratio * total_duration
                scale_expr = f"min(1\\,{SIZE[0]}/iw\\,{SIZE[1]}/ih)"
                res.append((str(p), start, start + 70, scale_expr, "(W-w)/2", "(H-h)/2"))
            continue
        # 尝试旧的 num:den 比例
        if ':' in stem:
            ratio_part = stem.split('-', 1)[0]
            if ':' in ratio_part:
                num_str, den_str = ratio_part.split(':', 1)
                try:
                    num = int(num_str)
                    den = int(den_str)
                    if den != 0:
                        start = total_duration * num / den
                        scale_expr = f"min(1\\,{SIZE[0]}/iw\\,{SIZE[1]}/ih)"
                        res.append((str(p), start, start + 70, scale_expr, "(W-w)/2", "(H-h)/2"))
                except ValueError:
                    pass
    
    # 按开始时间排序
    res.sort(key=lambda x: x[1])  # 按start时间排序
    
    # 调整结束时间：如果下一张图片开始时间早于当前图片的默认结束时间，
    # 则当前图片的结束时间设为下一张图片的开始时间
    for i in range(len(res) - 1):
        current_end = res[i][2]  # 当前图片的结束时间
        next_start = res[i+1][1]  # 下一张图片的开始时间
        if next_start < current_end:
            # 将当前图片的结束时间设为下一张图片的开始时间
            res[i] = (res[i][0], res[i][1], next_start, res[i][3], res[i][4], res[i][5])
    
    return res

def build_center_two_lines(tag_in: str, line1: str, line2: str,
                           enable: str, tag_out: str) -> str:
    """
    居中两行：用"像素常量"把两行视作整体来居中（即使上下字号不同也对齐）。
    """
    font = pick_font()
    W, H = SIZE
    fs1  = int(H * FS1_RATIO)
    fs2  = int(H * FS2_RATIO)
    gap  = int(H * GAP_RATIO)

    # 计算整体居中的 y 坐标（像素）
    # group_height = 第一行高度 + 间距 + 第二行高度
    y1_px = (H - (fs1 + gap + fs2)) // 2 + CENTER_NUDGE_PX
    y2_px = y1_px + fs1 + gap

    l1 = esc(line1)
    l2 = esc(line2)

    ft1 = (
        f"[{tag_in}]drawtext=fontfile='{font}':text='{l1}':"
        f"fontcolor={TEXT_COLOR}:fontsize={fs1}:"
        f"bordercolor={STROKE_COLOR}:borderw={STROKE_W}:"
        f"box=1:boxcolor={BOX_COLOR}:boxborderw={BOX_PAD}:"
        f"x=(w-text_w)/2:y={y1_px}:enable='{enable}'[t1];"
    )
    ft2 = (
        f"[t1]drawtext=fontfile='{font}':text='{l2}':"
        f"fontcolor={TEXT_COLOR}:fontsize={fs2}:"
        f"bordercolor={STROKE_COLOR}:borderw={STROKE_W}:"
        f"box=1:boxcolor={BOX_COLOR}:boxborderw={BOX_PAD}:"
        f"x=(w-text_w)/2:y={y2_px}:enable='{enable}'{tag_out};"
    )
    return ft1 + ft2

def make_cover(bg_path: Path, out_png: str, line1: str, line2: str):
    """
    封面图：两行居中、无外部红框；上下字号不同。
    这里 y 坐标直接用像素常量确保两行整体居中。
    """
    font = pick_font()
    W, H = SIZE
    fs1  = int(H * FS1_RATIO)
    fs2  = int(H * FS2_RATIO)
    gap  = int(H * GAP_RATIO)
    y1_px = (H - (fs1 + gap + fs2)) // 2 + CENTER_NUDGE_PX
    y2_px = y1_px + fs1 + gap

    vf_parts = []

    # 不要外部红框（如需调试，手工设 True 并会用像素常量）
    if COVER_DRAW_FRAME:
        fx = int(W * FRAME_X_RATIO)
        fy = int(H * FRAME_Y_RATIO)
        fw = int(W * FRAME_W_RATIO)
        fh = int(H * FRAME_H_RATIO)
        vf_parts.append(
            f"drawbox=x={fx}:y={fy}:w={fw}:h={fh}:color={FRAME_COLOR}:t={FRAME_THICK}"
        )

    if line1:
        vf_parts.append(
            f"drawtext=fontfile='{font}':text='{esc(line1)}':"
            f"fontcolor={TEXT_COLOR}:fontsize={fs1}:"
            f"bordercolor={STROKE_COLOR}:borderw={STROKE_W}:"
            f"box=1:boxcolor={BOX_COLOR}:boxborderw={BOX_PAD}:"
            f"x=(w-text_w)/2:y={y1_px}"
        )
    if line2:
        vf_parts.append(
            f"drawtext=fontfile='{font}':text='{esc(line2)}':"
            f"fontcolor={TEXT_COLOR}:fontsize={fs2}:"
            f"bordercolor={STROKE_COLOR}:borderw={STROKE_W}:"
            f"box=1:boxcolor={BOX_COLOR}:boxborderw={BOX_PAD}:"
            f"x=(w-text_w)/2:y={y2_px}"
        )

    cmd = (
        f"ffmpeg -y -ss {COVER_SEEK_SEC} -i {shlex.quote(str(bg_path))} "
        f"-frames:v 1 -vf \"{','.join(vf_parts)}\" {shlex.quote(out_png)}"
    )
    run(cmd)

def batch_convert_to_720p():
    """首次运行时，批量将 videos 目录下非 720p 视频转换为 720p"""
    script_dir = Path(__file__).parent
    vid_dir = script_dir / "videos"
    if not vid_dir.exists():
        return
    
    # 找出所有非 .720.mp4 的视频文件
    non_720p_videos = []
    def is_720p_file(p: Path) -> bool:
        return p.name.endswith(".720.mp4") or "(720p)" in p.stem.lower()

    for vid in vid_dir.glob("*.mp4"):
        if not is_720p_file(vid):
            non_720p_videos.append(vid)
    
    if not non_720p_videos:
        print("📹 所有视频已是 720p 格式")
        return
    
    print(f"📹 发现 {len(non_720p_videos)} 个非 720p 视频，开始批量转换...")
    for i, vid in enumerate(non_720p_videos, 1):
        output_720p = vid.with_suffix(".720.mp4")
        if output_720p.exists():
            print(f"  [{i}/{len(non_720p_videos)}] 跳过 {vid.name} (已有 720p 版本)")
            continue
        
        print(f"  [{i}/{len(non_720p_videos)}] 转换 {vid.name} → 720p...")
        try:
            run(
                f"ffmpeg -y -i {shlex.quote(str(vid))} "
                f"-vf scale={SIZE[0]}:{SIZE[1]} "
                f"-c:v h264_nvenc -preset p5 -b:v 5M -an {shlex.quote(str(output_720p))}"
            )
            # 转换成功后删除原文件
            vid.unlink()
            print(f"  ✅ {vid.name} 转换完成并删除原文件")
        except Exception as e:
            print(f"  ❌ {vid.name} 转换失败: {e}")


def main():
    start = time.time()
    # 确保输出目录存在
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # 首次运行批量转换所有视频为 720p
    batch_convert_to_720p()

    # 720p 背景缓存
    if BG_VIDEO_OVERRIDE:
        bg_src = Path(BG_VIDEO_OVERRIDE)
        if not bg_src.exists():
            raise FileNotFoundError(f"指定的背景视频不存在: {bg_src}")
    else:
        bg_src = pick_bg_video()
    bg720 = bg_src.with_suffix('.720.mp4')
    if not bg720.exists():
        run(
            f"ffmpeg -y -i {shlex.quote(str(bg_src))} "
            f"-vf scale={SIZE[0]}:{SIZE[1]} "
            f"-c:v h264_nvenc -preset p5 -b:v 5M -an {shlex.quote(str(bg720))}"
        )
        # 转码完成后删除原高分辨率视频，节省空间且避免计作新视频
        try:
            bg_src.unlink()
        except Exception as e:
            print(f"⚠️ 无法删除原视频 {bg_src}: {e}")
    bg = bg720

    # 音频与字幕 (自动检测)
    script_dir = Path(__file__).parent
    wav_files = sorted(script_dir.glob("*.wav"))
    if not wav_files:
        raise FileNotFoundError("脚本目录下未找到 .wav 音频文件")
    audio_path = wav_files[0]
    global AUDIO, OUTPUT_MP4, COVER_PNG
    AUDIO = audio_path.name
    OUTPUT_MP4 = str(OUTPUT_DIR / audio_path.with_suffix('.mp4').name)
    COVER_PNG  = str(OUTPUT_DIR / audio_path.with_suffix('.png').name)

    dur = probe_duration(str(audio_path))
    images = load_overlay_images(dur) or IMAGES
    srt = audio_path.with_suffix('.srt')
    if not srt.exists():
        raise FileNotFoundError(f"找不到对应的字幕文件: {srt}")
    print(f"📝 使用音频: {audio_path}")
    print(f"📝 使用字幕: {srt}")

    # 标题文本智能分行
    title_raw = Path(AUDIO).stem.split('_', 1)[1] if '_' in Path(AUDIO).stem else Path(AUDIO).stem
    parts = split_title(title_raw)
    if len(parts) == 1:
        parts.append("")  # 保证两行
    line1, line2 = parts[0], parts[1]

    # 输入
    cmd = ["ffmpeg -y", "-stream_loop -1", f"-i {shlex.quote(str(bg))}", f"-i {shlex.quote(str(audio_path))}"]
    for img, *_ in images:
        cmd += ["-loop", "1", "-i", shlex.quote(img)]

    # 滤镜：背景裁到音频时长
    filters = [f"[0:v]trim=duration={dur},setpts=PTS-STARTPTS[bg0];"]

    # 片头两行（同封面居中算法；启用时间为前 TITLE_DURATION 秒）
    filters.append(
        build_center_two_lines(
            tag_in="bg0",
            line1=line1,
            line2=line2,
            enable=f"lte(t,{TITLE_DURATION})",
            tag_out="[v1]"
        )
    )

    # 额外叠图
    last = "[v1]"
    for idx, (img, start_t, end_t, r, x, y) in enumerate(images, start=2):
        tag = f"img{idx}"
        filters.append(
            f"[{idx}:v]scale={SIZE[0]}:{SIZE[1]}:force_original_aspect_ratio=decrease[{tag}];"
            f"{last}[{tag}]overlay=enable='between(t,{start_t},{end_t})':x={x}:y={y}[v{idx}];"
        )
        last = f"[v{idx}]"

    # 字幕
    filters.append(
        f"{last}subtitles={shlex.quote(str(srt))}:force_style="
        f"'Fontsize=23,Bold=1,PrimaryColour=&H0000FFFF&,OutlineColour=&H00000000&,"
        f"BorderStyle=1,Outline=1,Shadow=0,Alignment=2,MarginV=22'[vout]"
    )
    if filters[-1].endswith(';'):
        filters[-1] = filters[-1][:-1]

    # 输出（如无 NVENC 可把 h264_nvenc 改成 libx264）
    cmd += [
        "-filter_complex", '"'+''.join(filters)+'"',
        "-map", "[vout]", "-map", "1:a",
        "-c:v h264_nvenc -preset p4 -rc vbr -b:v 2.5M -maxrate 2.5M -bufsize 5M",
        "-c:a aac -b:a 192k -pix_fmt yuv420p",
        "-shortest",  # 音频结束时停止输出，避免无限循环
        shlex.quote(OUTPUT_MP4)
    ]
    run(' '.join(cmd))

    # 生成封面（两行整体居中，无外部红框）
    make_cover(bg, COVER_PNG, line1, line2)

    # 生成完毕后立即清空 images 素材（保留音频和字幕）
    img_dir = script_dir / "images"
    if img_dir.exists():
        for p in img_dir.glob("*"):
            if p.is_file():
                p.unlink()

    print(f"✅ 完成 → {OUTPUT_MP4}    ⏱ 用时 {time.time()-start:.1f}s")
    print(f"📸 封面 → {COVER_PNG}（两行居中，无外部红框；上大下小）")

if __name__ == '__main__':
    main()