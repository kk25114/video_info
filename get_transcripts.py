import os
import argparse
import re
import requests
import subprocess
import shutil
from youtube_transcript_api import YouTubeTranscriptApi

def check_dependencies():
    """检查脚本所需的外部命令行工具是否存在。"""
    if not shutil.which('yt-dlp'):
        print("错误: 核心依赖 'yt-dlp' 未找到。")
        print("请确保 yt-dlp 已安装并位于您系统的 PATH 环境变量中。")
        print("安装指南: https://github.com/yt-dlp/yt-dlp")
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

        try:
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['zh-Hans', 'zh-CN', 'zh', 'en'])
            
            transcript_text = '\n\n'.join([item['text'] for item in transcript_list])
            
            filename = f"{str(index + 1).zfill(num_digits)}_{sanitized_title}.md"
            transcript_file_path = os.path.join(args.output_dir, filename)
            
            display_title = video_title if video_title else f"ID: {video_id}"
            markdown_content = f"# {display_title}\n\n"
            markdown_content += f"**原始链接:** <{link}>\n\n"
            markdown_content += "---\n\n"
            markdown_content += transcript_text
            
            with open(transcript_file_path, 'w', encoding='utf-8') as tf:
                tf.write(markdown_content)
            
            print(f"已将 '{display_title}' 的文稿保存至: {transcript_file_path}")

        except Exception as e:
            print(f"无法下载视频 {link} 的文稿。错误: {e}")

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
    
    args = parser.parse_args()
    main(args)