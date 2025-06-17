import os
import argparse
import re
import requests
import subprocess
import shutil
from youtube_transcript_api import YouTubeTranscriptApi

# 全局变量，用于懒加载 ASR 模型
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
    global asr_model
    print(f"--> 备用方案: 正在尝试使用 {args.asr} 从音频转录...")

    audio_filename = f"{base_filename}.mp3"
    audio_path = os.path.join(output_dir, audio_filename)
    
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
            if asr_model is None:
                print(f"    2/3: 首次加载 FunASR 模型 (paraformer-zh)...")
                from funasr import AutoModel
                asr_model = AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc-c",)
            
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

def main(args):
    """主执行函数。"""
    check_dependencies()
    
    video_links = get_video_links_from_url(args.youtube_url)
    if not video_links:
        print("未获取到任何视频链接，程序退出。")
        return

    # 确保输出目录存在
    os.makedirs(args.output_dir, exist_ok=True)
    print(f"文稿将保存在: {args.output_dir}")

    # 加载已处理的视频ID
    processed_log_path = os.path.join(args.output_dir, 'processed_videos.log')
    processed_ids = set()
    if os.path.exists(processed_log_path):
        with open(processed_log_path, 'r', encoding='utf-8') as f:
            processed_ids = set(line.strip() for line in f)
        print(f"已加载 {len(processed_ids)} 条已处理视频的记录。")

    # 高效筛选新视频
    # 假设 yt-dlp 返回的列表是按最新到最旧排序的
    print("\n正在从视频列表中查找新内容...")
    new_video_links = []
    for link in video_links:
        video_id = get_video_id(link)
        if not video_id:
            print(f"无法解析视频ID，已跳过: {link}")
            continue
        
        # 一旦遇到已经处理过的视频，就停止查找
        # 因为列表是按时间倒序的，这之后都是旧视频
        if video_id in processed_ids:
            print("检测到已处理过的视频，扫描停止。")
            break
        
        new_video_links.append(link)
    
    if not new_video_links:
        print("\n没有需要处理的新视频。程序退出。")
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

        video_title = get_video_title(link)
        
        if video_title:
            sanitized_title = sanitize_filename(video_title)
        else:
            print(f"--> 警告: 无法获取视频标题。将使用视频 ID '{video_id}' 作为备用文件名。")
            sanitized_title = video_id

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
            # 使用一个临时的、唯一的名称来下载音频，避免冲突
            base_filename_for_audio = f"temp_audio_{video_id}"
            transcript_text = transcribe_audio_fallback(link, args.output_dir, base_filename_for_audio, args)
            if transcript_text:
                is_from_asr = True

        if transcript_text:
            filename = f"{str(next_file_index).zfill(4)}_{sanitized_title}.md"
            transcript_file_path = os.path.join(args.output_dir, filename)
            
            display_title = video_title if video_title else f"ID: {video_id}"
            markdown_content = f"# {display_title}\n\n"
            markdown_content += f"**原始链接:** <{link}>\n\n"
            
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
            print(f"处理失败，未能获取视频文稿: {link}")

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

    args = parser.parse_args()
    main(args)