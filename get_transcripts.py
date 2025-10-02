#!/usr/bin/env python3
"""
视频文稿获取脚本 - 完整解决方案

功能：
- 从频道、播放列表或单个视频获取文稿
- 支持官方字幕和AI语音识别双重方案
- 智能错误处理和重试机制
- 自动摘要生成和错别字校正
- Git自动提交集成

使用方法：
    # 基本使用 - 获取频道所有新视频
    python3 get_transcripts.py "https://www.youtube.com/@question-dialectic/videos" --output_dir "1.大问题"
    
    # 完整功能 - 包含摘要和校正
    python3 get_transcripts.py "https://www.youtube.com/@sunriches/videos" \
        --output_dir "2.sunrich" --auto-commit --summarize --correct
    
    # 使用Whisper模型
    python3 get_transcripts.py "https://www.youtube.com/@yuegemovie" \
        --output_dir "3.越哥说电影" --asr whisper --whisper_model medium

依赖环境：
    pip install youtube-transcript-api yt-dlp requests opencc funasr whisper
    apt install ffmpeg  # Ubuntu/Debian

配置文件：
    创建config.json文件，添加DeepSeek API密钥：
    {"DEEPSEEK_API_KEY": "sk-xxxxxxxxxxxxxxxxxxxx"}
"""

import os
import argparse
import re
import json
import requests
import subprocess
import shutil
from youtube_transcript_api import YouTubeTranscriptApi

# ---- 代理兜底设置 ----
# 支持网络代理配置，确保在中国大陆环境也能正常访问YouTube
PROXY = "http://172.23.240.1:10806"
for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.setdefault(key, PROXY)
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")

# 全局变量，用于懒加载 ASR 模型，避免重复加载
asr_model = None

def check_dependencies():
    """检查脚本所需的外部命令行工具是否存在。"""
    if not shutil.which('yt-dlp'):
        print("错误: 核心依赖 'yt-dlp' 未找到。")
        print("请确保 yt-dlp 已安装并位于您系统的 PATH 环境变量中。")
        print("安装指南: https://github.com/yt-dlp/yt-dlp")
        exit(1)
    if not shutil.which('ffmpeg'):
        print("错误: 依赖 'ffmpeg' 未找到。")
        print("ffmpeg 是从视频中提取音频所必需的。")
        print("请根据您的操作系统进行安装。例如在 Ubuntu/Debian 上: sudo apt update && sudo apt install ffmpeg")
        exit(1)

def get_video_links_from_url(youtube_url, output_dir=None, candidate_size: int = 20):
    """使用 yt-dlp 从给定的 YouTube 频道/播放列表/视频链接获取视频 URL 列表。

    约定：
    - 如果输出目录中已存在 `processed_videos.log`（表示非首次运行），
      则进入“快速模式”，仅抓取最新 candidate_size 条候选视频；新增视频判定仅按“是否已处理过的ID”。
    - 如果不存在该文件（首次运行），抓取该来源的所有视频。

    说明：`--dateafter` 在 `--flat-playlist` 下无效，因此本函数只做“候选集合”抓取，
    真正的“按上传日期过滤”放在主流程中逐条读取元数据后完成。
    """
    use_fast_mode = False
    if output_dir:
        processed_log_path_hint = os.path.join(output_dir, 'processed_videos.log')
        if os.path.exists(processed_log_path_hint):
            use_fast_mode = True
            print(f"📄 检测到 processed_videos.log，使用快速模式（候选取最新{candidate_size}条，按ID判定新增）")
    
    if not use_fast_mode:
        print(f"正在从目标链接获取所有视频 URL: {youtube_url}（首次运行，耗时~47秒）")
    
    try:
        # 构造 yt-dlp 命令，增强反爬虫防护
        command = [
            'yt-dlp',
            '--cookies', 'cookies.txt',
            '--extractor-args', 'youtube:player-client=mweb',
            '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            '--flat-playlist'
        ]
        
        # 快速模式：只获取最新 candidate_size 个视频（YouTube按时间倒序，最新在前）
        if use_fast_mode:
            command.extend(['--playlist-end', str(candidate_size)])
        
        command.extend(['--get-url', youtube_url])
        result = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=180
        )
        links = result.stdout.strip().splitlines()
        if not links:
            print("警告: 未能从提供的链接中找到任何视频。请检查链接是否有效。")
        else:
            if use_fast_mode:
                print(f"✅ 成功获取 {len(links)} 个视频（快速模式），随后将通过ID集合判定新增")
            else:
                print(f"✅ 成功获取 {len(links)} 个视频链接")
        return links
    except subprocess.TimeoutExpired:
        print("错误: yt-dlp 获取视频列表超时 (180秒)。")
        return []
    except subprocess.CalledProcessError as e:
        print(f"执行 yt-dlp 时出错。请确保链接有效，且 yt-dlp 是最新版本。\n错误详情: {e.stderr.strip()}")
        return []
    except Exception as e:
        print(f"获取视频链接时发生未知错误: {e}")
        return []

def sanitize_filename(title):
    """将字符串清理为有效的文件名。"""
    sanitized = re.sub(r'[\\/*?:"<>|]', "", title)
    sanitized = sanitized.replace(' ', '_')
    return sanitized[:100]



def get_video_id(url):
    """从 URL 中提取 YouTube 视频 ID。"""
    if 'v=' in url:
        return url.split('v=')[1].split('&')[0]
    return None

def format_transcript_text(text, asr_provider='whisper'):
    """清理转录文本。"""
    if not text:
        return ""
    
    import re
    
    # 对 Whisper 的输出：移除所有标点和空格，按行合并
    if asr_provider == 'whisper':
        lines = text.split('\n')
        cleaned_lines = []
        for line in lines:
            line_no_punct = re.sub(r'[^\u4e00-\u9fff\w\s]', '', line)
            line_no_space = re.sub(r'\s+', '', line_no_punct)
            if line_no_space:
                cleaned_lines.append(line_no_space)
        return '\n'.join(cleaned_lines)
        
    # 对 FunASR 的输出：仅清理首尾空白，保留其原生标点和格式
    elif asr_provider == 'funasr':
        return text.strip()
        
    return text

def summarize_with_deepseek(transcript_text):
    """使用 DeepSeek API 生成文本摘要。"""
    print("--> 正在尝试使用 DeepSeek API 生成摘要...")
    
    config_path = 'config.json'
    api_key = None
    
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            try:
                config = json.load(f)
                api_key = config.get("DEEPSEEK_API_KEY")
            except json.JSONDecodeError:
                print(f"    -> 警告: '{config_path}' 文件格式错误，不是有效的 JSON。")
    
    if not api_key:
        print(f"    -> 警告: 未在 '{config_path}' 文件中找到 'DEEPSEEK_API_KEY'。已跳过摘要生成。")
        print(f"    -> 请确保 '{config_path}' 文件存在且包含您的密钥，格式如下:")
        print('    -> {"DEEPSEEK_API_KEY": "sk-xxxxxxxxxxxxxxxxxxxx"}')
        return None

    api_url = "https://api.deepseek.com/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    prompt = (
        "你是一个专业的中文内容编辑，擅长从视频文稿中提炼核心信息。\n"
        "请根据以下视频文稿，生成一个简洁、流畅、分为三个段落的摘要。\n"
        "摘要应准确地反映视频的主要内容、关键论点和结论，避免加入自己的观点或猜测。\n"
        "--- 文稿开始 ---\n"
        f"{transcript_text}\n"
        "--- 文稿结束 ---\n"
        "请输出三个段落的摘要："
    )

    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        print(f"    1/2: 正在向 DeepSeek API (deepseek-chat) 发送请求...")
        response = requests.post(api_url, headers=headers, json=data, timeout=180)
        response.raise_for_status()
        print("    2/2: 已收到 API 响应。")
        
        result = response.json()
        summary = result['choices'][0]['message']['content'].strip()
        return summary

    except requests.exceptions.RequestException as e:
        print(f"    -> 错误: 调用 DeepSeek API 时出错: {e}")
        if hasattr(e, 'response') and e.response:
            print(f"    -> 响应内容: {e.response.text}")
        return None
    except (KeyError, IndexError) as e:
        print(f"    -> 错误: 解析 API 响应失败: {e}")
        if 'response' in locals():
            print(f"    -> 完整响应: {response.text}")
        return None

def transcribe_audio_fallback(video_url, output_dir, base_filename, args):
    """使用指定的ASR引擎从音频转录文稿作为备用方案。"""
    global asr_model
    print(f"--> 备用方案: 正在尝试使用 {args.asr} 从音频转录...")

    audio_filename = f"{base_filename}.mp3"
    audio_path = os.path.join(output_dir, audio_filename)
    
    try:
        # 1. 下载音频
        print(f"    1/3: 正在下载音频: {video_url}")
        download_command = [
            'yt-dlp',
            '--cookies', 'cookies.txt',
            '--extractor-args', 'youtube:player-client=mweb',
            '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            '-x', '--audio-format', 'mp3', 
            '--audio-quality', '128K',
            '--output', audio_path, 
            video_url
        ]
        subprocess.run(download_command, check=True, capture_output=True, text=True)

        # 2. 根据选择加载模型并转录
        transcript_text = ""
        
        if args.asr == 'funasr':
            if asr_model is None:
                print(f"    2/3: 首次加载 FunASR 模型 (paraformer-zh)...")
                from funasr import AutoModel
                asr_model = AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc-c", disable_update=True)
            
            print("        正在进行语音识别，这可能需要一些时间...")
            result = asr_model.generate(input=audio_path)
            
            if result and result[0].get("sentence_info"):
                sentences = [s['text'] for s in result[0]['sentence_info']]
                transcript_text = '\n'.join(sentences)
                print(f"    -> FunASR 转录完成，并已按句子分段 (共 {len(sentences)} 句)。")
            elif result and result[0].get("text"): # Fallback if sentence_info is not available
                transcript_text = result[0]['text']
                print("    -> FunASR 转录完成 (单段文本)，已自动添加标点。")

        elif args.asr == 'whisper':
            if asr_model is None:
                print(f"    2/3: 首次加载 Whisper 模型 ({args.whisper_model})...")
                import whisper
                asr_model = whisper.load_model(args.whisper_model)

            print("        正在进行语音识别，这可能需要一些时间...")
            result = asr_model.transcribe(audio_path, language="zh", fp16=False)
            if 'segments' in result and result['segments']:
                transcript_lines = [segment['text'] for segment in result['segments']]
                transcript_text = '\n'.join(transcript_lines)
                print(f"    -> 已采用 Whisper 原生分段，共 {len(transcript_lines)} 个片段。")
            else:
                transcript_text = result['text']
        
        if not transcript_text:
            print("    -> 警告：ASR未能生成任何文本。")
            return None

        # 3. 强制转换为简体中文和格式化
        print("    3/3: 后处理转录文本...")
        try:
            from opencc import OpenCC
            cc = OpenCC('t2s')
            simplified_text = cc.convert(transcript_text)
            
            # 根据 ASR 提供商选择不同的格式化策略
            formatted_text = format_transcript_text(simplified_text, args.asr)
            print(f"    -> 已完成文本格式化 ({args.asr} 模式)。")
            # 返回处理成功的文本
            return formatted_text
        except Exception as e:
            print(f"    -> 警告: 文本后处理失败: {e}。将返回原始转录文本。")
            return transcript_text

    except subprocess.CalledProcessError as e:
        # 智能判断 yt-dlp 的错误类型
        error_output = e.stderr.lower()
        permanent_error_keywords = [
            'video unavailable', 'private video', 'members-only',
            'this video is private', 'this video is unavailable',
            'has been removed', 'login required', 'copyright',
            'video has been deleted', 'user has closed their account'
        ]
        is_permanent = any(keyword in error_output for keyword in permanent_error_keywords)

        if is_permanent:
            print(f"--> [yt-dlp下载失败] 检测到永久性错误，将记录ID。错误: {e.stderr.strip()}")
            return "permanent_failure" # 返回一个特殊信号
        else:
            print(f"--> [yt-dlp下载失败] 检测到临时性错误，将可重试。错误: {e.stderr.strip()}")
            return None # 返回 None 代表临时失败

    except Exception as e:
        print(f"--> [{args.asr} 备用方案失败] 发生未知错误，将可重试: {e}")
        return None # 其他所有错误都视为临时性
    finally:
        # 4. 清理临时音频文件
        if os.path.exists(audio_path):
            os.remove(audio_path)

def get_next_file_index(output_dir):
    """
    扫描输出目录，根据 'XXXX_title.md' 命名方案查找现有的最大文件序号。
    返回下一个要使用的索引。
    """
    max_index = 0
    if not os.path.isdir(output_dir):
        return 1  # 如果目录不存在，从 1 开始

    for filename in os.listdir(output_dir):
        match = re.match(r'^(\d+)_.*\.md$', filename)
        if match:
            current_index = int(match.group(1))
            if current_index > max_index:
                max_index = current_index
    return max_index + 1

def get_video_title(video_url):
    """使用 yt-dlp 获取视频的原始标题。"""
    print("--> 正在获取视频原始标题...")
    try:
        command = [
            'yt-dlp',
            '--cookies', 'cookies.txt',
            '--extractor-args', 'youtube:player-client=mweb',
            '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            '--print', 'title', '--no-playlist', video_url
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=30
        )
        return result.stdout.strip()
    except Exception as e:
        print(f"--> 获取原始标题时出错: {e}")
        return None

def get_video_upload_date(video_url):
    """使用 yt-dlp 获取视频的上传日期。"""
    try:
        command = [
            'yt-dlp',
            '--cookies', 'cookies.txt',
            '--extractor-args', 'youtube:player-client=mweb',
            '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            '--print', '%(upload_date)s', '--no-playlist', video_url
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=30
        )
        return result.stdout.strip()
    except Exception as e:
        return None

def get_deepseek_api_key():
    """从 config.json 或环境变量中安全地加载 DeepSeek API Key。"""
    config_path = 'config.json'
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                api_key = config.get("DEEPSEEK_API_KEY")
                if api_key:
                    return api_key
        except (json.JSONDecodeError, IOError):
            print(f"警告: 读取或解析 '{config_path}' 文件时出错。")
    
    # 如果文件中没有，可以尝试从环境变量读取
    api_key_env = os.environ.get("DEEPSEEK_API_KEY")
    if api_key_env:
        return api_key_env
        
    return None

def generate_ai_summary(transcript_text):
    """使用 DeepSeek API 根据文稿生成简介和话题。"""
    print("--> 正在使用 DeepSeek API 生成简介和话题...")
    
    api_key = get_deepseek_api_key()
    if not api_key:
        print(f"    -> 警告: 未找到 DeepSeek API Key。已跳过此步骤。")
        return None

    api_url = "https://api.deepseek.com/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    prompt = (
        "你是一个专业的中文内容分析师。请根据以下视频文稿，分析其核心内容，并以严格的 JSON 格式返回一个包含以下两个键的对​​象：\n"
        "1. `description`: 一段约100-150字的简介，对读者清晰地介绍视频的主要内容、关键论点和结论 (字符串)。请直接以内容的核心话题作为开头，避免使用“本视频介绍了”、“该视频探讨了...”等引导语，让这段简介本身就像是内容的自然开场白。\n"
        "2. `tags`: 一个包含5个最相关的关键词的数组 (字符串数组)。\n\n"
        "确保你的回复只有纯粹的 JSON 对象，不包含任何额外的解释或标记。\n\n"
        "--- 文稿开始 ---\n"
        f"{transcript_text}\n"
        "--- 文稿结束 ---"
    )

    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"}
    }

    try:
        print("    1/2: 正在向 DeepSeek API 发送请求...")
        response = requests.post(api_url, headers=headers, json=data, timeout=180)
        response.raise_for_status()
        print("    2/2: 已收到 API 响应。")
        
        result = response.json()
        content_str = result['choices'][0]['message']['content']
        summary_data = json.loads(content_str)
        
        if 'description' in summary_data and 'tags' in summary_data:
            return summary_data
        else:
            print("    -> 错误: API 返回的 JSON 格式不符合预期。")
            return None

    except requests.exceptions.RequestException as e:
        print(f"    -> 错误: 调用 DeepSeek API 时出错: {e}")
        if hasattr(e, 'response') and e.response:
            print(f"    -> 响应内容: {e.response.text}")
        return None
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        print(f"    -> 错误: 解析 API 响应失败: {e}")
        if 'response' in locals():
            print(f"    -> 完整响应: {response.text}")
        return None

def correct_transcript_with_deepseek(transcript_text):
    """调用 DeepSeek API，对文稿进行错别字校正，并保持原有段落结构。"""
    print("--> 正在使用 DeepSeek API 进行错别字校正...")

    api_key = get_deepseek_api_key()

    if not api_key:
        print("    -> 未找到 DEEPSEEK_API_KEY，跳过校正。")
        return transcript_text

    api_url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    prompt = (
        "你是一位专业的中文校对专家。请仔细检查并纠正下方文稿中的所有错别字、常见用词错误和明显的标点错误。日期,数字类型用数字标识出来。"
        "增加合理的段落，不要添加或删除内容，只做必要的文字校正。"
        "直接返回修订后的完整文稿，不要输出任何解释说明。\n\n"
        "--- 原文开始 ---\n"
        f"{transcript_text}\n"
        "--- 原文结束 ---"
    )

    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        resp = requests.post(api_url, headers=headers, json=data, timeout=180)
        resp.raise_for_status()
        corrected = resp.json()['choices'][0]['message']['content'].strip()
        return corrected if corrected else transcript_text
    except Exception as e:
        print(f"    -> 校正失败: {e}，将使用原文。")
        return transcript_text

def main(args):
    """主执行函数。"""
    check_dependencies()
    
    # 传递 output_dir 和候选窗口大小
    video_links = get_video_links_from_url(
        args.youtube_url,
        output_dir=args.output_dir,
        candidate_size=args.candidate_size
    )
    if not video_links:
        print("未获取到任何视频链接，程序退出。")
        return

    # 确保输出目录存在
    os.makedirs(args.output_dir, exist_ok=True)
    
    # 加载已处理和已失败的视频ID
    processed_log_path = os.path.join(args.output_dir, 'processed_videos.log')
    processed_ids = set()
    if os.path.exists(processed_log_path):
        with open(processed_log_path, 'r', encoding='utf-8') as f:
            processed_ids = set(line.strip() for line in f if line.strip())

    failed_log_path = os.path.join(args.output_dir, 'failed_videos.log')
    failed_ids = set()
    if os.path.exists(failed_log_path):
        with open(failed_log_path, 'r', encoding='utf-8') as f:
            failed_ids = set(line.strip() for line in f if line.strip())

    # 合并成一个总的跳过列表
    skip_ids = processed_ids.union(failed_ids)
    print(f"已加载 {len(processed_ids)} 条成功记录和 {len(failed_ids)} 条失败记录。共跳过 {len(skip_ids)} 个视频。")

    # 仅按“ID是否已处理”判定新增（更快，满足你每日执行的节奏）
    print("\n正在从视频列表中查找新内容（仅按ID判定）...")
    new_video_links = []
    for link in video_links:
        vid = get_video_id(link)
        if not vid or vid in skip_ids:
            continue
        new_video_links.append(link)

    if not new_video_links:
        print("\n没有发现需要处理的新视频。程序退出。")
        return

    print(f"\n共发现 {len(new_video_links)} 个新视频。")

    # 反转列表，确保从最旧的视频开始处理，使得最新的视频获得最大的序号
    new_video_links.reverse()
    print("已将视频列表反转，将从最旧的视频开始处理。")
    
    # 确定新文件的起始编号
    next_file_index = get_next_file_index(args.output_dir)
    print(f"将从序号 {next_file_index:04d} 开始为新文件命名。")

    total_new_videos = len(new_video_links)
    for current_progress, link in enumerate(new_video_links):
        video_id = get_video_id(link)
        # 视频ID在此处一定存在且是新的，因为前面已经筛选过

        print(f"\n正在处理第 {current_progress + 1}/{total_new_videos} 个新视频: {link}")
        
        # 1. 使用 yt-dlp 获取原始标题
        display_title = get_video_title(link)
        if display_title:
            sanitized_title = sanitize_filename(display_title)
        else:
            print(f"--> 警告: 无法获取视频原始标题。将使用视频 ID '{video_id}' 作为备用。")
            display_title = f"视频ID: {video_id}"
            sanitized_title = video_id

        # 2. 获取文稿
        transcript_text = None
        is_from_asr = False
        
        try:
            # 优先尝试获取官方字幕
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['zh-Hans', 'zh-CN', 'zh', 'en'])
            transcript_text = '\n\n'.join([item['text'] for item in transcript_list])
            print(f"成功获取官方文稿。")

        except Exception as e:
            print(f"无法获取官方文稿: {str(e).strip()}")
            # 官方文稿获取失败，启动 ASR 备用方案
            base_filename_for_audio = f"temp_audio_{video_id}"
            transcript_text = transcribe_audio_fallback(link, args.output_dir, base_filename_for_audio, args) # transcript_text can now be string, None, or "permanent_failure"
            if transcript_text and transcript_text != "permanent_failure":
                is_from_asr = True

        # 3. 如果成功获取文稿，则继续处理
        if transcript_text and transcript_text != "permanent_failure":
            
            # 可选错别字校正
            if args.correct:
                transcript_text = correct_transcript_with_deepseek(transcript_text)

            # 5. 构建基础 Markdown 内容
            filename = f"{str(next_file_index).zfill(4)}_{sanitized_title}.md"
            transcript_file_path = os.path.join(args.output_dir, filename)
            
            markdown_content = f"# {display_title}\n\n"
            markdown_content += f"**原始链接:** <{link}>\n\n"

            # 6. 如果需要，使用 AI 生成简介和话题，并插入
            if args.summarize:
                ai_summary = generate_ai_summary(transcript_text)
                if ai_summary:
                    summary_section = "---\n\n"
                    if ai_summary.get("description"):
                        summary_section += f"## 简介\n\n{ai_summary['description']}\n\n"
                    if ai_summary.get("tags"):
                        summary_section += f"## 话题\n\n"
                        for tag in ai_summary['tags']:
                            hashtag = tag.replace(' ', '')
                            summary_section += f"- #{hashtag}\n"
                        summary_section += "\n"
                    summary_section += "---\n\n"
                    
                    # 将AI生成的部分插入到链接和文稿之间
                    markdown_content += summary_section

            # 7. 添加 ASR 注意事项和文稿正文
            if is_from_asr:
                markdown_content += f"> **注意**: 本文稿由 `{args.asr}` 语音识别生成，可能存在错误。\n\n"

            markdown_content += transcript_text
            
            with open(transcript_file_path, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
            print(f"成功保存文稿: {filename}")

            # 记录已处理的ID并递增文件序号
            with open(processed_log_path, 'a', encoding='utf-8') as f:
                f.write(f"{video_id}\n")
            next_file_index += 1
        else:
            # 永久性失败或临时性失败
            if transcript_text == "permanent_failure":
                print(f"处理失败，检测到永久性错误: {link}")
                # 将失败的ID记录下来，下次不再尝试
                with open(failed_log_path, 'a', encoding='utf-8') as f:
                    f.write(f"{video_id}\n")
                print(f"--> 已将失败的视频ID '{video_id}' 记录到失败日志中。")
            else: # transcript_text is None
                print(f"处理失败，检测到临时性错误，将可重试: {link}")

    # 本方案不再记录日期文件，新增判定完全基于 ID 集合。

    # 如果指定了 --auto-commit，则在最后调用外部脚本执行 Git 操作
    if args.auto_commit:
        print("\n-----------------------------------------")
        print("🚀 检测到 --auto-commit，准备调用提交脚本...")
        
        commit_script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'auto_commit.sh')
        
        if not os.path.exists(commit_script_path):
            print(f"错误: 提交脚本 'auto_commit.sh' 未在目录中找到。")
            return

        try:
            # 执行外部的提交脚本
            result = subprocess.run([commit_script_path], check=True, capture_output=True, text=True)
            # 打印提交脚本的输出
            print(result.stdout)
        except FileNotFoundError:
            print(f"\n错误: 无法执行 '{commit_script_path}'。请确保它有执行权限 (chmod +x auto_commit.sh)。")
        except subprocess.CalledProcessError as e:
            print(f"\n错误: Git 同步脚本执行失败。")
            print(f"  返回码: {e.returncode}")
            print(f"  --- 脚本标准输出 ---\n{e.stdout.strip()}")
            print(f"  --- 脚本错误输出 ---\n{e.stderr.strip()}")
        except Exception as e:
            print(f"\n一个未知错误导致 Git 同步失败: {e}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="下载 YouTube 视频的文稿。")
    parser.add_argument("youtube_url", help="YouTube 视频、播放列表或频道的 URL。")
    parser.add_argument(
        '--output_dir', 
        type=str, 
        default='transcripts', 
        help='保存文稿文件的目录路径 (默认为: transcripts)。'
    )
    parser.add_argument(
        '--asr',
        type=str,
        default='funasr',
        choices=['funasr', 'whisper'],
        help='选择使用的语音识别引擎 (默认为: funasr)。'
    )
    parser.add_argument(
        '--whisper_model',
        type=str,
        default='medium',
        choices=['tiny', 'base', 'small', 'medium', 'large-v3'],
        help='当选择 whisper 引擎时，指定使用的模型大小 (默认为: medium)。'
    )
    
    # 新增的参数
    parser.add_argument('--auto-commit', action='store_true', help='在脚本成功执行后，调用 auto_commit.sh 脚本进行提交。')
    parser.add_argument('--summarize', action='store_true', help='使用 DeepSeek API 基于文稿内容生成简介和话题。')
    parser.add_argument('--correct', action='store_true', help='在生成摘要之前，对文稿进行错别字校正。')
    parser.add_argument('--candidate-size', type=int, default=20, help='快速模式下抓取的最新候选视频数量（默认20）。')

    args = parser.parse_args()
    main(args)
