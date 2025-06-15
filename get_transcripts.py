import os
import argparse
import re
import requests
import subprocess
import shutil
from youtube_transcript_api import YouTubeTranscriptApi

# 全局变量，用于懒加载 Whisper 模型
whisper_model = None

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

def get_video_links_from_url(youtube_url):
    """使用 yt-dlp 从给定的 YouTube 频道/播放列表/视频链接获取所有视频的 URL。"""
    print(f"正在从目标链接获取所有视频 URL: {youtube_url}")
    try:
        # 执行 yt-dlp 命令并捕获输出
        result = subprocess.run(
            ['yt-dlp', '--flat-playlist', '--get-url', youtube_url],
            capture_output=True,
            text=True,
            check=True
        )
        links = result.stdout.strip().splitlines()
        if not links:
            print("警告: 未能从提供的链接中找到任何视频。请检查链接是否有效。")
        else:
            print(f"成功找到 {len(links)} 个视频链接。")
        return links
    except subprocess.CalledProcessError as e:
        print(f"执行 yt-dlp 时出错。请确保链接有效，且 yt-dlp 是最新版本。\n错误详情: {e.stderr}")
        return []
    except Exception as e:
        print(f"获取视频链接时发生未知错误: {e}")
        return []

def sanitize_filename(title):
    """将字符串清理为有效的文件名。"""
    sanitized = re.sub(r'[\\/*?:"<>|]', "", title)
    sanitized = sanitized.replace(' ', '_')
    return sanitized[:100]

def get_video_title(video_url):
    """使用 YouTube oEmbed API 获取视频标题。"""
    oembed_url = f"https://www.youtube.com/oembed?url={video_url}&format=json"
    try:
        response = requests.get(oembed_url)
        if response.status_code == 200:
            return response.json()['title']
    except requests.exceptions.RequestException as e:
        print(f"--> 获取标题时网络错误: {e}")
    return None

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

def transcribe_audio_fallback(video_url, output_dir, base_filename, args):
    """使用指定的ASR引擎从音频转录文稿作为备用方案。"""
    print(f"--> 备用方案: 正在尝试使用 {args.asr} 从音频转录...")

    audio_filename = f"{base_filename}.mp3"
    audio_path = os.path.join(output_dir, audio_filename)
    
    # 定义 ASR 模型
    asr_model = None

    try:
        # 1. 下载音频
        print(f"    1/3: 正在下载音频: {video_url}")
        download_command = [
            'yt-dlp', '-x', '--audio-format', 'mp3', 
            '--audio-quality', '128K',
            '--output', audio_path, 
            video_url
        ]
        subprocess.run(download_command, check=True, capture_output=True, text=True)

        # 2. 根据选择加载模型并转录
        transcript_text = ""
        
        if args.asr == 'funasr':
            print(f"    2/3: 首次加载 FunASR 模型 (paraformer-zh)...")
            from funasr import AutoModel
            asr_model = AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc-c",)
            print("        正在进行语音识别，这可能需要一些时间...")
            result = asr_model.generate(input=audio_path)
            if result and result[0].get("text"):
                transcript_text = result[0]["text"]
                print("    -> FunASR 转录完成，已自动添加标点。")

        elif args.asr == 'whisper':
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
            return formatted_text
        except Exception as e:
            print(f"    -> 警告: 文本后处理失败: {e}。将返回原始转录文本。")
            return transcript_text

    except Exception as e:
        print(f"--> [{args.asr} 备用方案失败] 发生未知错误: {e}")
        return None
    finally:
        # 4. 清理临时音频文件
        if os.path.exists(audio_path):
            os.remove(audio_path)

def main(args):
    """主执行函数。"""
    check_dependencies()
    
    video_links = get_video_links_from_url(args.youtube_url)
    if not video_links:
        print("未获取到任何视频链接，程序退出。")
        return

    if os.path.exists(args.output_dir):
        print(f"发现已存在的目录 '{args.output_dir}'，正在删除...")
        shutil.rmtree(args.output_dir)
    
    os.makedirs(args.output_dir)

    total_videos = len(video_links)
    num_digits = len(str(total_videos))

    for index, link in enumerate(video_links):
        video_id = get_video_id(link)
        if not video_id:
            print(f"无法从链接中解析视频 ID: {link}")
            continue

        print(f"\n正在处理视频 ({index + 1}/{total_videos}): {link}")

        video_title = get_video_title(link)
        
        if video_title:
            sanitized_title = sanitize_filename(video_title)
        else:
            print(f"--> 警告: 无法获取视频标题。将使用视频 ID '{video_id}' 作为备用文件名。")
            sanitized_title = video_id

        transcript_text = None
        is_from_whisper = False
        
        try:
            # 优先尝试获取官方字幕
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['zh-Hans', 'zh-CN', 'zh', 'en'])
            transcript_text = '\n\n'.join([item['text'] for item in transcript_list])
            print(f"成功获取官方文稿。")

        except Exception as e:
            print(f"无法获取官方文稿: {str(e).strip()}")
            # 官方文稿获取失败，启动 ASR 备用方案
            base_filename_for_audio = f"{str(index + 1).zfill(num_digits)}_{sanitized_title}"
            transcript_text = transcribe_audio_fallback(link, args.output_dir, base_filename_for_audio, args)
            if transcript_text:
                is_from_whisper = True

        if transcript_text:
            filename = f"{str(index + 1).zfill(num_digits)}_{sanitized_title}.md"
            transcript_file_path = os.path.join(args.output_dir, filename)
            
            display_title = video_title if video_title else f"ID: {video_id}"
            markdown_content = f"# {display_title}\n\n"
            markdown_content += f"**原始链接:** <{link}>\n\n"
            
            if is_from_whisper:
                markdown_content += f"> **注意**: 本文稿由 `{args.asr}` 语音识别生成，可能存在错误。\n\n"
            
            markdown_content += "---\n\n"
            markdown_content += transcript_text
            
            with open(transcript_file_path, 'w', encoding='utf-8') as tf:
                tf.write(markdown_content)
            
            print(f"已将 '{display_title}' 的文稿保存至: {transcript_file_path}")
        else:
            print(f"处理视频 {link} 失败，所有方法均未能获取文稿。")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='一键从 YouTube 频道/播放列表链接下载所有视频的文稿，并存为 Markdown 文件。',
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        'youtube_url', 
        type=str, 
        help='YouTube 频道、播放列表或单个视频的 URL。\n例如:\n"https://www.youtube.com/@username/videos"\n"https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxx"'
    )
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
    
    args = parser.parse_args()
    main(args)