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
import multiprocessing as mp
import queue
import traceback
import azure.cognitiveservices.speech as speechsdk
import socket
import ssl
import base64
from urllib.parse import urlparse, unquote
from typing import List, Optional, Tuple

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
    "chunkLimit": 4500,
    "rtfTimeoutThreshold": 3.0,
    "frameTimeoutIntervalMs": 6000,
    "enableCompressedAudioTransmission": True,
    "proactiveSplitLimit": 1200,
    "autoSplitOnTimeout": True,
    "timeoutSplitLimit": 1200,
    "segmentTimeoutSeconds": 240,
    "proxy": "",
    "bypassMicrosoftProxy": False
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
RTF_TIMEOUT_THRESHOLD = float(cfg["rtfTimeoutThreshold"])
FRAME_TIMEOUT_INTERVAL_MS = int(cfg["frameTimeoutIntervalMs"])
ENABLE_COMPRESSED_AUDIO_TRANSMISSION = bool(cfg["enableCompressedAudioTransmission"])
PROACTIVE_SPLIT_LIMIT = int(cfg["proactiveSplitLimit"])
AUTO_SPLIT_ON_TIMEOUT = bool(cfg["autoSplitOnTimeout"])
TIMEOUT_SPLIT_LIMIT = int(cfg["timeoutSplitLimit"])
SEGMENT_TIMEOUT_SECONDS = int(cfg["segmentTimeoutSeconds"])
PROXY           = cfg["proxy"]
BYPASS_MS_PROXY = cfg["bypassMicrosoftProxy"]
USE_TUN_MODE = os.environ.get("VIDEO_INFO_USE_TUN", "").strip().lower() not in ("", "0", "false", "no")

if USE_TUN_MODE:
    # TUN 模式下由系统/客户端接管转发，这里不再显式注入代理或切换到代理。
    PROXY = ""
    BYPASS_MS_PROXY = True

os.makedirs(SAVE_DIR, exist_ok=True)

MS_NO_PROXY_HOSTS = [
    ".microsoft.com",
    ".azure.com",
    ".speech.microsoft.com",
    ".tts.speech.microsoft.com",
]
NO_PROXY_BASE = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
EFFECTIVE_BYPASS_MS_PROXY = BYPASS_MS_PROXY


def _set_no_proxy(value: str):
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value


def _build_no_proxy(base: str, hosts: list) -> str:
    items = [h.strip() for h in base.split(",") if h.strip()]
    for h in hosts:
        if h not in items:
            items.append(h)
    return ",".join(items)


def _apply_ms_bypass(enable: bool):
    """
    enable=True: 让 *.microsoft.com / *.speech.microsoft.com 等域名绕过代理（直连）。
    enable=False: 恢复到启动时的 NO_PROXY 基线（允许这些域名走代理）。
    """
    if enable:
        _set_no_proxy(_build_no_proxy(NO_PROXY_BASE, MS_NO_PROXY_HOSTS))
    else:
        _set_no_proxy(NO_PROXY_BASE)


def _has_proxy_env() -> bool:
    return any(
        os.environ.get(k)
        for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    )


def _tls_probe(host: str, port: int = 443, timeout_s: float = 3.5) -> Tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as raw:
            raw.settimeout(timeout_s)
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(raw, server_hostname=host) as s:
                s.settimeout(timeout_s)
                s.do_handshake()
        return True, "tls ok"
    except OSError as e:
        return False, f"tls failed: {e.__class__.__name__}: {e}"
    except ssl.SSLError as e:
        return False, f"tls failed: SSLError: {e}"


def _tts_host(region: str) -> str:
    return f"{region}.tts.speech.microsoft.com"


def merge_no_proxy(hosts):
    """为 NO_PROXY / no_proxy 追加域名，确保 Azure 请求走直连"""
    raw = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    exist = [h for h in raw.split(",") if h]
    for h in hosts:
        if h not in exist:
            exist.append(h)
    if exist:
        joined = ",".join(exist)
        os.environ["NO_PROXY"] = joined
        os.environ["no_proxy"] = joined


def setup_proxy():
    keys = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ftp_proxy", "FTP_PROXY")
    if USE_TUN_MODE:
        for key in keys:
            os.environ.pop(key, None)
        return
    if PROXY:
        for key in keys:
            os.environ.setdefault(key, PROXY)


setup_proxy()

PROXY_ENV_KEYS = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "all_proxy",
    "ftp_proxy",
    "FTP_PROXY",
)
PROXY_ENV_BASE = {k: os.environ.get(k) for k in PROXY_ENV_KEYS}


def _normalize_proxy_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        return f"http://{raw}"
    return raw


def _effective_proxy_url() -> str:
    if USE_TUN_MODE:
        return ""
    if PROXY:
        return PROXY
    return (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or ""
    )


def _redact_proxy_url(proxy_url: str) -> str:
    proxy_url = _normalize_proxy_url(proxy_url)
    if not proxy_url:
        return ""
    u = urlparse(proxy_url)
    if not (u.username or u.password):
        return proxy_url
    host = u.hostname or ""
    port = f":{u.port}" if u.port else ""
    return f"{u.scheme}://***:***@{host}{port}"


def _apply_speechsdk_proxy(cfg, enable: bool):
    if USE_TUN_MODE or not enable:
        return
    proxy_url = _normalize_proxy_url(_effective_proxy_url())
    if not proxy_url:
        return
    u = urlparse(proxy_url)
    host = u.hostname
    if not host:
        return
    port = u.port or (443 if u.scheme == "https" else 80)
    username = unquote(u.username) if u.username else ""
    password = unquote(u.password) if u.password else ""
    try:
        cfg.set_proxy(host, port, username, password)
    except Exception:
        pass


def _apply_proxy_env(use_proxy: bool):
    if USE_TUN_MODE:
        for key in PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        return
    if use_proxy:
        for key, value in PROXY_ENV_BASE.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return
    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)


def _proxy_connect_then_tls_probe(
    proxy_url: str,
    target_host: str,
    target_port: int = 443,
    timeout_s: float = 4.0,
) -> Tuple[Optional[bool], str]:
    """
    返回 (ok/failed/unknown, detail)：
    - ok=True：通过代理 CONNECT + TLS 握手成功
    - ok=False：探测失败（代理不可用/被拒绝/握手失败）
    - ok=None：代理 scheme 不支持探测（例如 socks5），仅表示“有代理但未验证”
    """
    proxy_url = _normalize_proxy_url(proxy_url)
    if not proxy_url:
        return False, "no proxy configured"

    u = urlparse(proxy_url)
    scheme = (u.scheme or "").lower()
    if scheme not in ("http", "https", "socks5", "socks5h", "socks4", "socks4a"):
        return None, f"unknown proxy scheme: {scheme or 'missing'}"
    if scheme.startswith("socks"):
        return None, f"proxy scheme {scheme} not probed"

    proxy_host = u.hostname
    if not proxy_host:
        return False, "proxy host missing"
    proxy_port = u.port or (443 if scheme == "https" else 80)

    auth_header = ""
    if u.username or u.password:
        user = unquote(u.username or "")
        pwd = unquote(u.password or "")
        token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
        auth_header = f"Proxy-Authorization: Basic {token}\r\n"

    try:
        raw = socket.create_connection((proxy_host, proxy_port), timeout=timeout_s)
        try:
            raw.settimeout(timeout_s)
            if scheme == "https":
                ctxp = ssl.create_default_context()
                raw = ctxp.wrap_socket(raw, server_hostname=proxy_host)
                raw.settimeout(timeout_s)

            req = (
                f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                f"Host: {target_host}:{target_port}\r\n"
                f"{auth_header}"
                "Proxy-Connection: keep-alive\r\n"
                "Connection: keep-alive\r\n"
                "\r\n"
            )
            raw.sendall(req.encode("ascii", errors="strict"))
            resp = raw.recv(4096).decode("iso-8859-1", errors="replace")
            status_line = resp.split("\r\n", 1)[0].strip()
            if not status_line.startswith("HTTP/"):
                return False, f"bad proxy response: {status_line or 'empty'}"
            if " 200 " not in status_line:
                return False, f"proxy connect failed: {status_line}"

            ctx = ssl.create_default_context()
            with ctx.wrap_socket(raw, server_hostname=target_host) as s:
                s.settimeout(timeout_s)
                s.do_handshake()
            return True, f"connect ok: {status_line}"
        finally:
            try:
                raw.close()
            except Exception:
                pass
    except OSError as e:
        return False, f"proxy probe failed: {e.__class__.__name__}: {e}"
    except ssl.SSLError as e:
        return False, f"proxy probe failed: SSLError: {e}"


def _init_network_mode() -> Tuple[bool, List[bool]]:
    """
    根据直连/代理的连通性探测，决定本次运行的 EFFECTIVE_BYPASS_MS_PROXY，
    并返回“允许尝试的 bypass 模式列表”（用于失败时切换重试）。
    """
    host = _tts_host(SERVICE_REGION)
    if USE_TUN_MODE:
        direct_ok, direct_detail = _tls_probe(host)
        if not direct_ok:
            print(
                f"⚠️  TUN 模式已启用，脚本不会使用代理；但直连 {host}:443 当前不可用（{direct_detail}）。"
                "后续若报错，请检查 TUN 路由/系统网络，而不是代理配置。"
            )
        _apply_ms_bypass(True)
        return True, [True]

    proxy_url = _effective_proxy_url()
    has_proxy = bool(proxy_url) or _has_proxy_env()

    preferred = bool(BYPASS_MS_PROXY)  # True=微软域名直连；False=微软域名走代理
    if not has_proxy:
        _apply_ms_bypass(preferred)
        return preferred, [preferred]

    direct_ok, direct_detail = _tls_probe(host)
    proxy_ok: Optional[bool] = None
    proxy_detail = "no proxy"
    if has_proxy:
        proxy_ok, proxy_detail = _proxy_connect_then_tls_probe(proxy_url, host)

    chosen = preferred

    # 仅当“另一种模式更可能可用”时才覆盖配置偏好
    if has_proxy:
        if preferred and not direct_ok and (proxy_ok is True or proxy_ok is None):
            chosen = False
            print(
                f"⚠️  直连 {host}:443 不可用（{direct_detail}），本次运行临时关闭 bypassMicrosoftProxy，改走代理。"
                f"如需固定：在 tts_cli/config.json 设置 bypassMicrosoftProxy=false。"
            )
        elif (not preferred) and proxy_ok is False and direct_ok:
            chosen = True
            print(
                f"⚠️  代理不可用（{proxy_detail}），本次运行临时开启 bypassMicrosoftProxy，改为直连微软域名。"
                f"如需固定：在 tts_cli/config.json 设置 bypassMicrosoftProxy=true。"
            )

    # 构造“可切换重试”的候选列表：只包含未被探测明确判死刑的模式
    candidates: List[bool] = [chosen]
    if has_proxy:
        alt = not chosen
        if alt:  # alt=True => 直连
            if direct_ok:
                candidates.append(alt)
        else:  # alt=False => 走代理
            if proxy_ok is not False:
                candidates.append(alt)

    # 去重且保序
    seen = set()
    candidates = [m for m in candidates if (m not in seen and not seen.add(m))]

    # 应用 chosen
    _apply_ms_bypass(chosen)

    # 如果两边都不行，额外提示一下（不阻断运行，交给 SDK 报错）
    if not direct_ok and (proxy_ok is False or not has_proxy):
        proxy_hint = _redact_proxy_url(proxy_url) if proxy_url else "（未配置代理 URL，仅检测到环境变量）"
        print(
            f"⚠️  网络探测显示：直连不可用（{direct_detail}），代理也不可用（{proxy_hint} / {proxy_detail}）。"
            "后续若报 WS_OPEN_ERROR，优先检查 WSL 代理地址/端口是否可达。"
        )

    return chosen, candidates


EFFECTIVE_BYPASS_MS_PROXY, BYPASS_MODE_CANDIDATES = _init_network_mode()

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
    """按中文标点/换行切分，确保每段 <= limit 字符；无标点时会硬切。"""
    if limit <= 0:
        return [text] if text else []

    pieces: List[str] = []
    buf = ""

    def flush_buffer():
        nonlocal buf
        if buf:
            pieces.append(buf)
            buf = ""

    def hard_split(seg: str):
        """把单个超长 seg 硬切到 <= limit，并尽量在标点处断开。"""
        remaining = seg
        while remaining:
            if len(remaining) <= limit:
                yield remaining
                return
            window = remaining[:limit]
            cut = max(window.rfind("。"), window.rfind("！"), window.rfind("？"), window.rfind("；"), window.rfind("…"), window.rfind("，"), window.rfind("\n"))
            if cut <= 0:
                cut = limit
            else:
                cut = cut + 1
            yield remaining[:cut]
            remaining = remaining[cut:]

    for seg in re.split(r"(?<=[。？！；…\n])", text):
        if not seg:
            continue
        for part in hard_split(seg):
            if len(buf) + len(part) > limit:
                flush_buffer()
            buf += part

    flush_buffer()
    return [p for p in pieces if p]


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


def _is_text_synthesis_timeout(msg: str) -> bool:
    return (
        "Timeout while synthesizing" in msg
        or "RTF" in msg
        or "frame interval" in msg
    )


def _is_startup_audio_timeout(msg: str) -> bool:
    return "timeout waiting for the first audio chunk" in msg.lower()


def _is_connection_error(msg: str) -> bool:
    low = msg.lower()
    return (
        "WS_OPEN_ERROR" in msg
        or "UNDERLYING_IO_OPEN_FAILED" in msg
        or "no connection to the remote host" in low
        or "connection failed" in low
        or _is_startup_audio_timeout(msg)
    )


def _serialize_word_boundary(e) -> tuple:
    duration_ticks = int(round(e.duration.total_seconds() * 10_000_000))
    return (int(e.audio_offset), duration_ticks, e.text)


def _synthesize_worker(ssml: str, outfile: str, result_queue):
    boundaries = []
    try:
        duration = synthesize(ssml, outfile, boundaries)
        duration_ticks = int(round(duration.total_seconds() * 10_000_000))
        result_queue.put({
            "ok": True,
            "duration_ticks": duration_ticks,
            "boundaries": [_serialize_word_boundary(e) for e in boundaries],
        })
    except Exception as e:
        result_queue.put({
            "ok": False,
            "message": str(e),
            "traceback": traceback.format_exc(),
        })


def synthesize_with_timeout(ssml: str, outfile: str, word_boundaries: list) -> datetime.timedelta:
    """
    Azure Speech SDK 的 speak_ssml_async(...).get() 偶尔不会按 SDK 配置超时返回。
    用子进程包住单段合成，超时后终止子进程并重试，避免 cron 长时间挂起。
    """
    if SEGMENT_TIMEOUT_SECONDS <= 0:
        return synthesize(ssml, outfile, word_boundaries)

    attempt = 0
    while True:
        if attempt > 0 and os.path.exists(outfile):
            os.remove(outfile)

        result_queue = mp.Queue(maxsize=1)
        proc = mp.Process(target=_synthesize_worker, args=(ssml, outfile, result_queue))
        proc.start()
        proc.join(SEGMENT_TIMEOUT_SECONDS)

        if proc.is_alive():
            proc.terminate()
            proc.join(10)
            if proc.is_alive():
                proc.kill()
                proc.join()
            attempt += 1
            if os.path.exists(outfile):
                os.remove(outfile)
            if attempt > RETRY_COUNT:
                raise RuntimeError(f"Segment timeout after {SEGMENT_TIMEOUT_SECONDS}s")
            print(f"[看门狗重试] 单段合成超过 {SEGMENT_TIMEOUT_SECONDS}s，已终止并重试 {attempt}/{RETRY_COUNT}，{RETRY_INTERVAL}s 后重试")
            time.sleep(RETRY_INTERVAL)
            continue

        try:
            result = result_queue.get_nowait()
        except queue.Empty:
            raise RuntimeError(f"Segment worker exited without result, exitcode={proc.exitcode}")

        if result.get("ok"):
            word_boundaries.clear()
            word_boundaries.extend(result["boundaries"])
            return datetime.timedelta(seconds=result["duration_ticks"] / 10_000_000)

        raise RuntimeError(result.get("message") or result.get("traceback") or "Segment worker failed")


def synthesize(ssml: str, outfile: str, word_boundaries: list) -> datetime.timedelta:
    """
    调用 Azure Speech SDK 合成 ssml 保存为 outfile，
    并捕获词语时间戳。
    返回合成音频的时长。
    """
    def make_cfg(use_proxy: bool):
        cfg = speechsdk.SpeechConfig(subscription=SPEECH_KEY, region=SERVICE_REGION)
        cfg.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm
        )
        cfg.set_property(
            speechsdk.PropertyId.SpeechSynthesis_RtfTimeoutThreshold,
            str(RTF_TIMEOUT_THRESHOLD),
        )
        cfg.set_property(
            speechsdk.PropertyId.SpeechSynthesis_FrameTimeoutInterval,
            str(FRAME_TIMEOUT_INTERVAL_MS),
        )
        if ENABLE_COMPRESSED_AUDIO_TRANSMISSION:
            cfg.set_property(
                speechsdk.PropertyId.SpeechServiceConnection_SynthEnableCompressedAudioTransmission,
                "true",
            )
        _apply_speechsdk_proxy(cfg, use_proxy)
        return cfg

    bypass_modes = BYPASS_MODE_CANDIDATES
    bypass_idx = 0

    attempt = 0
    while True:
        try:
            word_boundaries.clear()
            if attempt > 0 and os.path.exists(outfile):
                os.remove(outfile)

            # 根据当前模式调整 NO_PROXY（决定微软域名是直连还是走代理）
            _apply_ms_bypass(bypass_modes[bypass_idx])

            use_proxy = not bypass_modes[bypass_idx]
            _apply_proxy_env(use_proxy)
            cfg = make_cfg(use_proxy)
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=cfg,
                audio_config=speechsdk.audio.AudioConfig(filename=outfile),
            )
            synthesizer.synthesis_word_boundary.connect(lambda e: word_boundaries.append(e))

            result = synthesizer.speak_ssml_async(ssml).get()
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                return result.audio_duration
            elif result.reason == speechsdk.ResultReason.Canceled:
                details = speechsdk.SpeechSynthesisCancellationDetails(result)
                raise RuntimeError(f"Canceled: {details.reason} | {details.error_details}")
            else:
                raise RuntimeError(result.reason)
        except Exception as e:
            msg = str(e)
            is_connection_error = _is_connection_error(msg)
            if is_connection_error and bypass_idx + 1 < len(bypass_modes):
                bypass_idx += 1
                mode = "直连微软域名" if bypass_modes[bypass_idx] else "让微软域名走代理"
                print(f"[网络切换] 检测到连接失败，切换为：{mode}，{RETRY_INTERVAL}s 后重试")
                time.sleep(RETRY_INTERVAL)
                continue

            if AUTO_SPLIT_ON_TIMEOUT and _is_text_synthesis_timeout(msg):
                raise

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
    if len(sys.argv) >= 2 and sys.argv[1] == "--probe-network":
        host = _tts_host(SERVICE_REGION)
        proxy_url = _effective_proxy_url()
        direct_ok, direct_detail = _tls_probe(host)
        proxy_ok, proxy_detail = _proxy_connect_then_tls_probe(proxy_url, host)

        print(f"[探测] 目标: {host}:443")
        print(f"  直连(TLS): {'OK' if direct_ok else 'FAIL'}  ({direct_detail})")
        if proxy_url or _has_proxy_env():
            shown = _redact_proxy_url(proxy_url) if proxy_url else "（未配置 proxy，但检测到环境变量）"
            ok_str = "OK" if proxy_ok is True else ("UNKNOWN" if proxy_ok is None else "FAIL")
            print(f"  代理(CONNECT+TLS via {shown}): {ok_str}  ({proxy_detail})")
        else:
            print("  代理: 未配置")

        if direct_ok and (proxy_ok is False or not (proxy_url or _has_proxy_env())):
            recommended = "bypassMicrosoftProxy=true（直连）"
        elif proxy_ok is True and not direct_ok:
            recommended = "bypassMicrosoftProxy=false（走代理）"
        elif direct_ok and proxy_ok is True:
            recommended = f"两者都通，按配置偏好（当前 bypassMicrosoftProxy={str(BYPASS_MS_PROXY).lower()}）"
        else:
            recommended = "两者都不通/不确定：优先修代理或网络"

        print(f"[建议] {recommended}")
        sys.exit(0)

    if len(sys.argv) < 2:
        print("用法: python3 long_tts_with_srt.py <input.txt> [output.wav]")
        print("      python3 long_tts_with_srt.py --probe-network")
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

    split_limit = CHUNK_LIMIT
    if PROACTIVE_SPLIT_LIMIT and PROACTIVE_SPLIT_LIMIT > 0:
        split_limit = min(split_limit, PROACTIVE_SPLIT_LIMIT)
    segments = split_text(text, limit=split_limit)
    print(f"共 {len(segments)} 段，开始合成…（splitLimit={split_limit}）")

    chunk_files = []
    all_word_boundaries = []
    total_duration_ticks = 0  # int, 100ns ticks

    i = 0
    while i < len(segments):
        seg = segments[i]
        chunk_idx = len(chunk_files) + 1
        out = os.path.join(SAVE_DIR, f"chunk_{chunk_idx:03}.wav")
        print(f"  [{i+1}/{len(segments)}] 合成中 …")

        chunk_boundaries = []
        try:
            chunk_duration = synthesize_with_timeout(build_ssml(seg), out, chunk_boundaries)
        except Exception as e:
            msg = str(e)
            if AUTO_SPLIT_ON_TIMEOUT and _is_text_synthesis_timeout(msg):
                # 若当前段已经不超过 TIMEOUT_SPLIT_LIMIT，仍可能因网络/阈值导致超时；
                # 此时用更小的动态 limit 再拆一轮，避免反复卡死在同一个段长度上。
                new_limit = TIMEOUT_SPLIT_LIMIT
                if TIMEOUT_SPLIT_LIMIT <= 0:
                    new_limit = 0
                elif len(seg) <= TIMEOUT_SPLIT_LIMIT:
                    new_limit = max(200, int(len(seg) * 0.6))
                smaller = split_text(seg, limit=new_limit)
                if len(smaller) > 1:
                    print(f"[自动切分] 当前段疑似超时，拆成 {len(smaller)} 段后重试（limit={new_limit}）")
                    segments = segments[:i] + smaller + segments[i+1:]
                    continue
            raise

        # 为每个词语事件添加时间偏移并保存
        for e in chunk_boundaries:
            if isinstance(e, tuple):
                audio_offset, duration_ticks, text = e
            else:
                audio_offset = int(e.audio_offset)
                duration_ticks = int(round(e.duration.total_seconds() * 10_000_000))
                text = e.text
            adjusted_event = (
                int(audio_offset) + total_duration_ticks,
                duration_ticks,
                text,
            )
            all_word_boundaries.append(adjusted_event)

        chunk_files.append(out)
        total_duration_ticks += int(round(chunk_duration.total_seconds() * 10_000_000))
        i += 1

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
