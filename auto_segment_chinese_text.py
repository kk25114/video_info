#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中文文本自动分段脚本
自动将长文本按中文句子边界分割成易读的短段落

功能说明：
1. 智能识别markdown文件中的正文内容（跳过头部信息）
2. 按中文标点符号自动断句，每段约100个汉字
3. 自动跳过已分段的文件，避免重复处理
4. 支持批量处理整个目录

使用方法：
1. 一键处理所有目录：python3 auto_segment_chinese_text.py
2. 处理单个目录：python3 -c "from auto_segment_chinese_text import process_directory; process_directory('目录路径')"
3. 处理单个文件：python3 -c "from auto_segment_chinese_text import process_file; process_file('文件路径.md')"
4. 处理当前打开的文件：python3 -c "from auto_segment_chinese_text import process_file; process_file('$(pwd)/当前文件名.md')"

自动处理以下目录的所有markdown文件：
- 1.大问题/
- 2.sunrich/
- 3.越哥说电影/
- 4.吟游诗人基德/
- 5.科学声音/
- 6.天才简史/
- 9.小播讲哲学/
"""

import os
import re
import glob


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
    process_all_directories()