#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中文文本自动分段脚本 - 智能文本格式化工具

核心功能：
1. 智能识别markdown文件结构，保护头部元信息
2. 基于中文语义进行智能分段，每段约100个汉字
3. 自动跳过已处理文件，避免重复操作
4. 支持批量目录处理和单个文件处理

技术特点：
- 使用正则表达式精确识别中文标点符号
- 保留原文格式和标记信息
- 智能检测文件是否已分段
- 错误处理和日志记录

使用场景：
- 将AI语音识别生成的长文本转换为易读格式
- 批量处理YouTube视频文稿
- 优化中文markdown文档的可读性

快速开始：
    # 一键处理所有频道目录
    python3 auto_segment_chinese_text.py
    
    # 处理特定目录
    python3 auto_segment_chinese_text.py --dir "2.sunrich"
    
    # 处理单个文件
    python3 auto_segment_chinese_text.py --file "2.sunrich/0001_高考结束后，更艰难时刻.md"

高级用法：
    # 在Python脚本中使用
    from auto_segment_chinese_text import process_file, process_directory
    
    # 处理单个文件
    process_file("path/to/file.md")
    
    # 处理整个目录
    process_directory("path/to/directory")
    
    # 自定义段落长度
    from auto_segment_chinese_text import split_text_into_paragraphs
    paragraphs = split_text_into_paragraphs(long_text, max_chars=80)

支持的目录结构：
├── 1.大问题/          # 哲学思辨类内容
├── 2.sunrich/         # 时事评论类内容
├── 3.越哥说电影/       # 电影解说类内容
├── 4.吟游诗人基德/     # 科技科普类内容
├── 5.科学声音/        # 科学教育类内容
├── 6.天才简史/        # 人物传记类内容
└── 9.小播讲哲学/       # 哲学普及类内容

处理规则：
- 跳过隐藏文件（以.开头的文件）
- 保留markdown头部信息
- 每段长度控制在80-120个汉字
- 按中文标点符号断句（。！？；）
- 已分段的文件自动跳过

示例输出：
    已处理: 0001_高考结束后，更艰难时刻.md
    目录 2.sunrich 处理完成！共处理了 5 个文件
    所有目录处理完成！总共处理了 23 个文件
"""

import os
import re
import glob
import argparse
import sys


def split_text_into_paragraphs(text, max_chars=100):
    """
    将文本按句子分段，每段约100汉字
    """
    # 移除多余的空白行
    text = re.sub(r'\n{3,}', '\n\n', text.strip())
    
    # 按句子分割（中文句号、问号、感叹号、分号）
    sentences = re.split(r'([。！？；])', text)
    
    paragraphs = []
    current_paragraph = ""
    
    # 重新组合句子和标点
    for i in range(0, len(sentences) - 1, 2):
        if i + 1 < len(sentences):
            sentence = sentences[i] + sentences[i + 1]
        else:
            sentence = sentences[i]
        
        # 如果当前段落加上新句子超过限制，则开始新段落
        if len(current_paragraph + sentence) > max_chars and current_paragraph.strip():
            paragraphs.append(current_paragraph.strip())
            current_paragraph = sentence.strip()
        else:
            current_paragraph += sentence.strip()
    
    # 添加最后一段
    if current_paragraph.strip():
        paragraphs.append(current_paragraph.strip())
    
    return paragraphs


def process_file(file_path):
    """
    处理单个文件的分段
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 查找分割点
        split_marker = "> **注意**: 本文稿由 `funasr` 语音识别生成，可能存在错误。"
        if split_marker in content:
            parts = content.split(split_marker)
            header = parts[0] + split_marker
            body = split_marker.join(parts[1:]) if len(parts) > 1 else ""
        else:
            # 如果没有找到标记，整个内容作为正文
            return False
        
        # 检查是否已经分段（看是否有以\n\n开头的段落）
        body_stripped = body.strip()
        if '\n\n' in body_stripped and len(body_stripped.split('\n\n')) > 3:
            print(f"文件 {os.path.basename(file_path)} 已有分段，跳过处理")
            return False
        
        # 处理正文分段
        if body.strip():
            paragraphs = split_text_into_paragraphs(body.strip())
            segmented_body = '\n\n'.join(paragraphs)
            
            # 重新组合内容
            new_content = header + '\n\n' + segmented_body
            
            # 写回文件
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            
            print(f"已处理: {os.path.basename(file_path)}")
            return True
        
    except Exception as e:
        print(f"处理文件 {os.path.basename(file_path)} 时出错: {e}")
        return False


def process_directory(directory_path):
    """
    处理指定目录下的所有markdown文件
    """
    md_files = glob.glob(os.path.join(directory_path, "**/*.md"), recursive=True)
    
    processed_count = 0
    for file_path in md_files:
        if not os.path.basename(file_path).startswith('.'):  # 跳过隐藏文件
            if process_file(file_path):
                processed_count += 1
    
    print(f"\n目录 {directory_path} 处理完成！共处理了 {processed_count} 个文件")
    return processed_count


def process_all_directories():
    """
    处理所有需要分段的目录
    """
    directories = [
        "1.大问题",
        "2.sunrich", 
        "3.越哥说电影",
        "4.吟游诗人基德",
        "5.科学声音",
        "6.天才简史",
        "9.小播讲哲学"
    ]
    
    total_processed = 0
    for directory in directories:
        dir_path = os.path.join("/home/github/video_info", directory)
        if os.path.exists(dir_path):
            count = process_directory(dir_path)
            total_processed += count
    
    print(f"\n所有目录处理完成！总共处理了 {total_processed} 个文件")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='中文文本自动分段工具')
    parser.add_argument('--dir', type=str, help='处理指定目录下的所有markdown文件')
    parser.add_argument('--file', type=str, help='处理单个markdown文件')
    
    args = parser.parse_args()
    
    if args.dir:
        # 处理指定目录
        dir_path = os.path.abspath(args.dir)
        if os.path.exists(dir_path):
            process_directory(dir_path)
        else:
            print(f"错误：目录 {dir_path} 不存在")
            sys.exit(1)
    elif args.file:
        # 处理单个文件
        file_path = os.path.abspath(args.file)
        if os.path.exists(file_path):
            if process_file(file_path):
                print(f"文件处理完成：{file_path}")
            else:
                print(f"文件无需处理或处理失败：{file_path}")
        else:
            print(f"错误：文件 {file_path} 不存在")
            sys.exit(1)
    else:
        # 处理所有默认目录
        process_all_directories()