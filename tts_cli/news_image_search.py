#!/usr/bin/env python3
# coding: utf-8
"""
news_image_search.py  —  根据markdown文档内容搜索新闻图片
支持从markdown文档中提取关键信息，使用DeepSeek生成搜索关键词，然后搜索相关新闻图片并保存到当前目录。

特性：
1. 解析markdown文档，提取标题和关键内容
2. 使用DeepSeek API生成图片搜索关键词
3. 使用Unsplash API搜索相关新闻图片
4. 支持自定义图片尺寸和数量
5. 自动保存图片到当前目录
6. 支持重试机制和错误处理

依赖安装：
  pip install openai pillow requests

使用：
  python3 news_image_search.py article.md

作者：基于long_tts_with_srt.py模式改编
"""

import os
import sys
import re
import time
import json
import requests
from typing import List, Dict, Optional
from PIL import Image
import io

# ---- 代理兜底设置 ----
PROXY = "http://172.23.240.1:10806"
for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.setdefault(key, PROXY)
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")

# ========= 1. 读取配置 =========
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "image_config.json")

DEFAULT_CONFIG = {
    "deepseekApiKey": "",
    "model": "deepseek-chat",
    "unsplashAccessKey": "",  # 可选，如果不提供将使用免费搜索
    "imageSize": "1024x1024",
    "imageCount": 1,
    "quality": "standard",
    "retryCount": 3,
    "retryInterval": 5,
    "saveDir": "./news_images"
}

if not os.path.isfile(CONFIG_PATH):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    print(f"已创建默认配置文件 {CONFIG_PATH} ，请填入 deepseekApiKey 后重新运行。")
    sys.exit(0)

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    cfg = {**DEFAULT_CONFIG, **json.load(f)}

DEEPSEEK_API_KEY = cfg["deepseekApiKey"]
MODEL = cfg["model"]
UNSPLASH_ACCESS_KEY = cfg.get("unsplashAccessKey", "")
IMAGE_SIZE = cfg["imageSize"]
IMAGE_COUNT = cfg["imageCount"]
QUALITY = cfg["quality"]
RETRY_COUNT = cfg["retryCount"]
RETRY_INTERVAL = cfg["retryInterval"]
SAVE_DIR = os.path.abspath(cfg["saveDir"])

os.makedirs(SAVE_DIR, exist_ok=True)

# ========= 2. 工具函数 =========

def extract_markdown_content(md_path: str) -> Dict[str, str]:
    """从markdown文件中提取标题和内容"""
    with open(md_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # 提取标题（第一个#开头的行）
    title = ""
    content_lines = []
    
    for line in lines:
        if line.startswith('#') and not title:
            title = line.strip('# ').strip()
        # 跳过元数据区域（---之间的内容）
        elif line.strip() == '---':
            continue
        elif not line.startswith('>') and line.strip():
            content_lines.append(line.strip())
    
    content = ' '.join(content_lines)
    
    return {
        "title": title,
        "content": content[:1000]  # 限制内容长度
    }

def generate_search_keywords(title: str, content: str) -> str:
    """使用DeepSeek生成图片搜索关键词"""
    from openai import OpenAI
    
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1"
    )
    
    prompt = f"""基于以下新闻文章内容，生成3-5个英文图片搜索关键词，用于搜索相关的新闻图片。

标题: {title}
内容: {content}

请返回逗号分隔的关键词列表，关键词应该：
1. 与新闻主题高度相关
2. 适合新闻图片搜索
3. 包含主要的人物、地点、事件或概念
4. 使用英文关键词

示例格式: artificial intelligence, medical technology, hospital, doctor, innovation"""
    
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=100
        )
        
        keywords = response.choices[0].message.content.strip()
        return keywords
    except Exception as e:
        print(f"DeepSeek API调用失败: {e}")
        # 备用方案：使用标题和内容的关键词
        fallback_keywords = f"{title}, news, technology"
        return fallback_keywords

def search_and_download_image(keywords: str, image_path: str) -> bool:
    """搜索并下载图片"""
    # 使用Unsplash API进行图片搜索
    if UNSPLASH_ACCESS_KEY:
        return search_with_unsplash(keywords, image_path)
    else:
        return search_with_pixabay(keywords, image_path)

def search_with_unsplash(keywords: str, image_path: str) -> bool:
    """使用Unsplash API搜索图片"""
    headers = {
        "Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"
    }
    
    url = f"https://api.unsplash.com/search/photos"
    params = {
        "query": keywords,
        "per_page": 1,
        "orientation": "landscape"
    }
    
    attempt = 0
    while attempt <= RETRY_COUNT:
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            if response.status_code == 200:
                data = response.json()
                if data['results']:
                    image_url = data['results'][0]['urls']['regular']
                    return download_image(image_url, image_path)
                else:
                    print(f"未找到相关图片: {keywords}")
                    return False
            else:
                print(f"Unsplash API错误: {response.status_code}")
                return False
                
        except Exception as e:
            attempt += 1
            if attempt > RETRY_COUNT:
                print(f"图片搜索失败: {e}")
                return False
            print(f"[重试] 第 {attempt}/{RETRY_COUNT} 次失败：{e}，{RETRY_INTERVAL}s 后重试")
            time.sleep(RETRY_INTERVAL)
    
    return False

def search_with_pixabay(keywords: str, image_path: str) -> bool:
    """使用多种方法搜索图片（免费）"""
    # 使用第一个关键词
    search_term = keywords.split(',')[0].strip()
    
    # 尝试不同的图片搜索方法
    methods = [
        ("Unsplash", lambda: search_unsplash_source(search_term, image_path)),
        ("Lorem Picsum", lambda: search_picsum_source(search_term, image_path)),
        ("Via.placeholder", lambda: search_placeholder_source(search_term, image_path))
    ]
    
    for method_name, method in methods:
        print(f"尝试使用 {method_name} 搜索...")
        try:
            if method():
                return True
        except Exception as e:
            print(f"{method_name} 搜索失败: {e}")
            continue
    
    return False

def search_unsplash_source(search_term: str, image_path: str) -> bool:
    """使用Unsplash源搜索图片"""
    url = f"https://source.unsplash.com/featured/?{search_term}"
    response = requests.get(url, timeout=30)
    if response.status_code == 200:
        with open(image_path, 'wb') as f:
            f.write(response.content)
        return True
    return False

def search_picsum_source(search_term: str, image_path: str) -> bool:
    """使用Lorem Picsum搜索图片"""
    url = f"https://picsum.photos/seed/{search_term}/1024/768.jpg"
    response = requests.get(url, timeout=30)
    if response.status_code == 200:
        with open(image_path, 'wb') as f:
            f.write(response.content)
        return True
    return False

def search_placeholder_source(search_term: str, image_path: str) -> bool:
    """使用占位符图片服务"""
    url = f"https://via.placeholder.com/1024x768/363544/FFFFFF?text={search_term}"
    response = requests.get(url, timeout=30)
    if response.status_code == 200:
        with open(image_path, 'wb') as f:
            f.write(response.content)
        return True
    return False

def download_image(image_url: str, image_path: str) -> bool:
    """下载图片到本地"""
    try:
        response = requests.get(image_url, timeout=30)
        if response.status_code == 200:
            with open(image_path, 'wb') as f:
                f.write(response.content)
            return True
        else:
            print(f"图片下载失败: {response.status_code}")
            return False
    except Exception as e:
        print(f"图片下载失败: {e}")
        return False

def sanitize_filename(filename: str) -> str:
    """清理文件名，移除特殊字符"""
    # 移除或替换特殊字符
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    filename = re.sub(r'[\s]+', '_', filename)
    return filename[:50]  # 限制长度

# ========= 3. 主入口 =========

def main():
    if len(sys.argv) < 2:
        print("用法: python3 news_image_search.py <input.md>")
        sys.exit(1)

    md_path = sys.argv[1]
    if not os.path.isfile(md_path):
        print(f"文件不存在: {md_path}")
        sys.exit(1)

    print(f"正在解析markdown文件: {md_path}")
    
    # 提取内容
    md_data = extract_markdown_content(md_path)
    title = md_data["title"]
    content = md_data["content"]
    
    if not title and not content:
        print("无法从markdown文件中提取有效内容")
        sys.exit(1)
    
    print(f"标题: {title}")
    print(f"内容长度: {len(content)} 字符")
    
    # 生成搜索关键词
    print("正在使用DeepSeek生成搜索关键词...")
    keywords = generate_search_keywords(title, content)
    print(f"生成的搜索关键词: {keywords}")
    
    # 搜索并下载图片
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    safe_title = sanitize_filename(title) if title else "news_image"
    image_filename = f"{safe_title}_{timestamp}.jpg"
    image_path = os.path.join(SAVE_DIR, image_filename)
    
    print(f"正在搜索图片，保存路径: {image_path}")
    
    if search_and_download_image(keywords, image_path):
        print(f"✅ 图片搜索成功: {image_path}")
        
        # 显示图片信息
        try:
            with Image.open(image_path) as img:
                print(f"图片尺寸: {img.size}")
                print(f"图片格式: {img.format}")
        except Exception as e:
            print(f"无法读取图片信息: {e}")
    else:
        print("❌ 图片搜索失败")
        sys.exit(1)

if __name__ == "__main__":
    main()