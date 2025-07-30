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
5. 输出文件保存在 SAVE_DIR，命名为 YYYYMMDD_HHMMSS.wav。

依赖安装：
  pip install azure-cognitiveservices-speech
  sudo apt install ffmpeg   # 若系统未安装

使用：
  python3 long_tts.py article.txt

作者：自动生成 by ChatGPT
"""

import os
import sys
import re
import time
import datetime
import subprocess
import json
import azure.cognitiveservices.speech as speechsdk

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


def synthesize(ssml: str, outfile: str):
    """调用 Azure Speech SDK 合成 ssml 保存为 outfile"""
    cfg = speechsdk.SpeechConfig(subscription=SPEECH_KEY, region=SERVICE_REGION)
    cfg.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm
    )
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=cfg,
        audio_config=speechsdk.audio.AudioConfig(filename=outfile)
    )

    attempt = 0
    while True:
        try:
            result = synthesizer.speak_ssml_async(ssml).get()
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                return
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
        print("用法: python3 long_tts.py <input.txt> [output.wav]")
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
    for idx, seg in enumerate(segments, 1):
        out = os.path.join(SAVE_DIR, f"chunk_{idx:03}.wav")
        print(f"  [{idx}/{len(segments)}] 合成中 …")
        synthesize(build_ssml(seg), out)
        chunk_files.append(out)

    if custom_output:
        final_file = custom_output if os.path.isabs(custom_output) else os.path.join(SAVE_DIR, custom_output)
    else:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        final_file = os.path.join(SAVE_DIR, f"{timestamp}.wav")
    concat_wav(chunk_files, final_file)

    # 清理临时分段
    for f in chunk_files:
        os.remove(f)

    print(f"\n✅ 已输出: {final_file}")


if __name__ == "__main__":
    main()
