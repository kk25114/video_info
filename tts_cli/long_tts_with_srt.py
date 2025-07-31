#!/usr/bin/env python3
# coding: utf-8
"""
long_tts.py  —  结合 Azure Speech SDK 的批量长文本合成工具
适配 WSL / Linux，直接命令行运行。

特性：
1. 按中文标点自动分段（单段<=4500 字符，Azure 上限 5000）。
2. 支持自定义音色、风格、语速、音高。
3. 支持失败自动重试（retryCount / retryInterval）。
4. 每段生成临时 WAV，最终无损拼接成一个 WAV 文件。
5. 同时生成与 WAV 文件同名的 SRT 字幕文件。
6. 输出文件保存在 SAVE_DIR，命名为 YYYYMMDD_HHMMSS.wav/srt。

依赖安装：
  pip install azure-cognitiveservices-speech
  sudo apt install ffmpeg   # 若系统未安装

使用：
  python3 long_tts_with_srt.py article.txt

作者：自动生成 by ChatGPT, modified by Gemini
"""

import os
import sys
import re
import time
import datetime
import subprocess
import json
import azure.cognitiveservices.speech as speechsdk
from typing import List



# ========= 1. 读取配置 =========
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_CONFIG = {
    "speechKey": "",
    "serviceRegion": "westus",
    "voiceName": "zh-CN-XiaoxiaoNeural",
    "voiceStyle": "Default",
    "role": "",
    "speed": 1.0,
    "pitch": "0%",
    "saveDir": "./tts_output",
    "retryCount": 0,
    "retryInterval": 5,
    "chunkLimit": 4500
}

if not os.path.isfile(CONFIG_PATH):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    print(f"已创建默认配置文件 {CONFIG_PATH} ，请填入 speechKey 后重新运行。")
    sys.exit(0)

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    cfg = {**DEFAULT_CONFIG, **json.load(f)}

SPEECH_KEY      = cfg["speechKey"]
SERVICE_REGION  = cfg["serviceRegion"]
VOICE_NAME      = cfg["voiceName"]
VOICE_STYLE     = cfg["voiceStyle"]
ROLE            = cfg["role"]
SPEED           = cfg["speed"]
PITCH           = cfg["pitch"]
SAVE_DIR        = os.path.abspath(cfg["saveDir"])
RETRY_COUNT     = cfg["retryCount"]
RETRY_INTERVAL  = cfg["retryInterval"]
CHUNK_LIMIT     = cfg["chunkLimit"]

os.makedirs(SAVE_DIR, exist_ok=True)

# ========= 2. 工具函数 =========

def format_time(ticks: int) -> str:
    """将 100ns-ticks 转换为 SRT 时间戳格式 `HH:MM:SS,ms`"""
    seconds = ticks / 10_000_000
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    ms = round((seconds - int(seconds)) * 1000)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"

def generate_srt(word_boundaries: List[tuple], srt_path: str):
    """根据词语时间戳列表生成 SRT 文件"""
    if not word_boundaries:
        return
    
    SUB_LINE_MAX_LEN = 25  # 每行字幕最大长度（中文字符）
    PUNCTUATION = '。？！；…、，'  # 标点符号
    SENTENCE_END = '。？！'  # 句子结束标点符号（优先断行）

    srt_content = []
    sub_index = 1
    current_line_text = ''
    line_start_time = 0
    last_punctuation_pos = -1  # 记录最后一个标点符号的位置
    last_punctuation_word_idx = -1  # 记录最后一个标点符号对应的词索引
    
    for i, (audio_offset, duration, text) in enumerate(word_boundaries):
        if not current_line_text:  # 新行开始
            line_start_time = audio_offset
            last_punctuation_pos = -1
            last_punctuation_word_idx = -1

        # 添加当前词语
        current_line_text += text
        
        # 检查当前词是否包含标点符号
        has_sentence_end = False
        if any(p in text for p in PUNCTUATION):
            # 找到标点符号在当前词中的位置
            for j, char in enumerate(text):
                if char in PUNCTUATION:
                    # 标点符号的绝对位置是：之前文本长度 + 当前词中标点符号的位置
                    last_punctuation_pos = len(current_line_text) - len(text) + j
                    last_punctuation_word_idx = i
                    # 检查是否是句子结束标点
                    if char in SENTENCE_END:
                        has_sentence_end = True
                    break

        is_last_word = (i == len(word_boundaries) - 1)
        
        # 如果遇到句子结束标点，优先断行（除非当前行太短）
        if has_sentence_end and len(current_line_text) >= 10:
            # 从句子结束标点前面切割
            cut_text = current_line_text[:last_punctuation_pos]
            remaining_text = current_line_text[last_punctuation_pos:]
            
            if cut_text:
                start_time_str = format_time(line_start_time)
                # 使用标点符号前一个词的结束时间
                if last_punctuation_word_idx > 0:
                    prev_audio_offset, prev_duration, _ = word_boundaries[last_punctuation_word_idx - 1]
                    end_time_str = format_time(prev_audio_offset + prev_duration)
                else:
                    # 如果标点符号在第一个词中，使用该词的开始时间
                    punct_audio_offset, _, _ = word_boundaries[last_punctuation_word_idx]
                    end_time_str = format_time(punct_audio_offset)
                
                srt_content.append(str(sub_index))
                srt_content.append(f"{start_time_str} --> {end_time_str}")
                srt_content.append(cut_text)
                srt_content.append('')
                
                sub_index += 1
            
            # 开始新行，剩余文本作为新行的开始
            # 去除开头的标点符号
            current_line_text = remaining_text.lstrip(PUNCTUATION)
            # 新行开始时间是包含标点符号的词的开始时间
            punct_audio_offset, _, _ = word_boundaries[last_punctuation_word_idx]
            line_start_time = punct_audio_offset
            
            last_punctuation_pos = -1
            last_punctuation_word_idx = -1
        
        # 如果超过长度限制
        if len(current_line_text) > SUB_LINE_MAX_LEN:
            # 如果有标点符号，从最后一个标点符号处切割
            if last_punctuation_pos >= 0:
                # 从标点符号前面切割（不包含标点符号）
                cut_text = current_line_text[:last_punctuation_pos]
                remaining_text = current_line_text[last_punctuation_pos:]
                
                if cut_text:
                    start_time_str = format_time(line_start_time)
                    # 使用标点符号前一个词的结束时间
                    if last_punctuation_word_idx > 0:
                        prev_audio_offset, prev_duration, _ = word_boundaries[last_punctuation_word_idx - 1]
                        end_time_str = format_time(prev_audio_offset + prev_duration)
                    else:
                        # 如果标点符号在第一个词中，使用该词的开始时间
                        punct_audio_offset, _, _ = word_boundaries[last_punctuation_word_idx]
                        end_time_str = format_time(punct_audio_offset)
                    
                    srt_content.append(str(sub_index))
                    srt_content.append(f"{start_time_str} --> {end_time_str}")
                    srt_content.append(cut_text)
                    srt_content.append('')
                    
                    sub_index += 1
                
                # 开始新行，剩余文本作为新行的开始
                # 去除开头的标点符号
                current_line_text = remaining_text.lstrip(PUNCTUATION)
                # 新行开始时间是包含标点符号的词的开始时间
                punct_audio_offset, _, _ = word_boundaries[last_punctuation_word_idx]
                line_start_time = punct_audio_offset
                
                last_punctuation_pos = -1
                last_punctuation_word_idx = -1
            else:
                # 没有标点符号，强制从当前位置切割
                # 回退一个词
                if i > 0:
                    prev_audio_offset, prev_duration, prev_text = word_boundaries[i-1]
                    cut_text = current_line_text[:-len(text)]
                    
                    # 去除首尾标点符号
                    clean_cut_text = cut_text.strip(PUNCTUATION)
                    if clean_cut_text:
                        start_time_str = format_time(line_start_time)
                        end_time_str = format_time(prev_audio_offset + prev_duration)
                        
                        srt_content.append(str(sub_index))
                        srt_content.append(f"{start_time_str} --> {end_time_str}")
                        srt_content.append(clean_cut_text)
                        srt_content.append('')
                        
                        sub_index += 1
                    
                    # 开始新行，去除开头标点符号
                    current_line_text = text.lstrip(PUNCTUATION)
                    line_start_time = audio_offset

        # 如果是最后一个词，输出当前行
        if is_last_word and current_line_text:
            # 去除首尾的标点符号
            clean_text = current_line_text.strip(PUNCTUATION)
            
            if clean_text:
                start_time_str = format_time(line_start_time)
                end_time_str = format_time(audio_offset + duration)
                
                srt_content.append(str(sub_index))
                srt_content.append(f"{start_time_str} --> {end_time_str}")
                srt_content.append(clean_text)
                srt_content.append('')

    with open(srt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(srt_content))
    print(f"字幕已生成: {srt_path}")


def split_text(text: str, limit: int = CHUNK_LIMIT):
    """按中文标点/换行切分，确保每段 <= limit 字符"""
    pieces, buf = [], ''
    for seg in re.split(r'(?<=[。？！；…\n])', text):
        if len(buf) + len(seg) > limit:
            pieces.append(buf)
            buf = seg
        else:
            buf += seg
    if buf:
        pieces.append(buf)
    return pieces


def build_ssml(txt: str) -> str:
    """根据当前配置生成 Azure TTS SSML 字符串"""
    role_part = f' role="{ROLE}"' if ROLE else ''
    header = (
        f'<speak version="1.0" xmlns:mstts="http://www.w3.org/2001/mstts" xml:lang="zh-CN">\n'
        f'  <voice name="{VOICE_NAME}"{role_part}>\n'
        f'    <mstts:express-as style="{VOICE_STYLE}">'  # style
    )
    prosody = f'<prosody rate="{SPEED}" pitch="{PITCH}">{txt}</prosody>'
    tail = '</mstts:express-as></voice></speak>'
    return header + prosody + tail


def synthesize(ssml: str, outfile: str, word_boundaries: list) -> datetime.timedelta:
    """
    调用 Azure Speech SDK 合成 ssml 保存为 outfile，
    并捕获词语时间戳。
    返回合成音频的时长。
    """
    cfg = speechsdk.SpeechConfig(subscription=SPEECH_KEY, region=SERVICE_REGION)
    cfg.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm
    )
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=cfg,
        audio_config=speechsdk.audio.AudioConfig(filename=outfile)
    )

    # 连接 synthesis_word_boundary 事件
    synthesizer.synthesis_word_boundary.connect(lambda e: word_boundaries.append(e))
    
    audio_duration = datetime.timedelta(0)
    attempt = 0
    while True:
        try:
            result = synthesizer.speak_ssml_async(ssml).get()
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                audio_duration = result.audio_duration
                return audio_duration
            elif result.reason == speechsdk.ResultReason.Canceled:
                details = speechsdk.SpeechSynthesisCancellationDetails(result)
                raise RuntimeError(f"Canceled: {details.reason} | {details.error_details}")
            else:
                raise RuntimeError(result.reason)
        except Exception as e:
            attempt += 1
            if attempt > RETRY_COUNT:
                raise
            print(f"[重试] 第 {attempt}/{RETRY_COUNT} 次失败：{e}，{RETRY_INTERVAL}s 后重试")
            time.sleep(RETRY_INTERVAL)


def concat_wav(parts: list, output: str):
    """使用 ffmpeg concat 无损拼接 wav 文件"""
    concat_txt = os.path.join(SAVE_DIR, "_concat.txt")
    with open(concat_txt, 'w', encoding='utf-8') as f:
        for p in parts:
            f.write(f"file '{p}'\n")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_txt,
        "-c", "copy", output
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.remove(concat_txt)

# ========= 3. 主入口 =========

def main():
    if len(sys.argv) < 2:
        print("用法: python3 long_tts_with_srt.py <input.txt> [output.wav]")
        sys.exit(1)

    txt_path = sys.argv[1]
    custom_output = sys.argv[2] if len(sys.argv) >=3 else None
    if not os.path.isfile(txt_path):
        print(f"文件不存在: {txt_path}")
        sys.exit(1)

    def extract_body(lines):
        """提取正文：取最后一个 '---' 之后，跳过空行与 '> ' 引用"""
        body_start = 0
        for idx, ln in enumerate(lines):
            if ln.strip() == '---':
                body_start = idx + 1
        # 跳过空行和引用
        while body_start < len(lines):
            raw = lines[body_start].strip()
            if raw == '' or raw.startswith('>'):
                body_start += 1
            else:
                break
        return ''.join(lines[body_start:])

    with open(txt_path, 'r', encoding='utf-8') as fh:
        lines = fh.readlines()
    text = extract_body(lines).strip()
    if not text:
        print("文本内容为空")
        sys.exit(1)

    segments = split_text(text)
    print(f"共 {len(segments)} 段，开始合成…")

    chunk_files = []
    all_word_boundaries = []
    total_duration_ticks = 0

    for idx, seg in enumerate(segments, 1):
        out = os.path.join(SAVE_DIR, f"chunk_{idx:03}.wav")
        print(f"  [{idx}/{len(segments)}] 合成中 …")
        
        chunk_boundaries = []
        chunk_duration = synthesize(build_ssml(seg), out, chunk_boundaries)
        
        # 为每个词语事件添加时间偏移并保存
        for e in chunk_boundaries:
            # 创建一个包含偏移时间的元组，统一使用 ticks 单位
            adjusted_event = (
                e.audio_offset + total_duration_ticks,  # 调整后的开始时间（ticks）
                e.duration.total_seconds() * 10_000_000,  # 持续时长转换为 ticks
                e.text  # 文本内容
            )
            all_word_boundaries.append(adjusted_event)

        chunk_files.append(out)
        total_duration_ticks += chunk_duration.total_seconds() * 10_000_000

    # 确定输出文件名和路径
    if custom_output:
        if os.path.isabs(custom_output):
            final_file = custom_output
        else:
            # 如果是相对路径，直接使用（因为 wrap_sunrich.sh 已经包含了完整路径）
            final_file = custom_output
            # 确保输出目录存在
            os.makedirs(os.path.dirname(final_file), exist_ok=True)
    else:
        # 使用与输入md文件同名的文件名
        base_name = os.path.splitext(os.path.basename(txt_path))[0]
        final_file = os.path.join(SAVE_DIR, f"{base_name}.wav")
    
    concat_wav(chunk_files, final_file)

    # 生成 SRT 字幕
    srt_file = os.path.splitext(final_file)[0] + ".srt"
    generate_srt(all_word_boundaries, srt_file)

    # 清理临时分段
    for f in chunk_files:
        os.remove(f)

    print(f"\n✅ 已输出: {final_file}")


if __name__ == "__main__":
    main()
