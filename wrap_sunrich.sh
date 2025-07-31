#!/usr/bin/env bash
# 定时任务包装脚本：抓取 Sunrich 新视频文稿，并把新增 .md 转成同名 WAV 和 SRT
# Author: ChatGPT 自动生成
#直接执行该脚本生成音频和字幕

set -e
cd /home/github/video_info

CHANNEL_URL="https://www.youtube.com/@sunriches/videos"
OUTPUT_DIR="2.sunrich"
SAVE_DIR="/home/github/video_info/mk_video"

# ---------- 0. 记录现有 Markdown ----------
find "$OUTPUT_DIR" -maxdepth 1 -name '*.md' | sort > /tmp/md_before.txt

# ---------- 1. 抓取最新视频并生成文稿 ----------
/usr/bin/python3 get_transcripts.py "$CHANNEL_URL" \
  --output_dir "$OUTPUT_DIR" --auto-commit --summarize --correct

# ---------- 2. 计算新增 Markdown ----------
find "$OUTPUT_DIR" -maxdepth 1 -name '*.md' | sort > /tmp/md_after.txt
NEW_MD=$(comm -13 /tmp/md_before.txt /tmp/md_after.txt)

# ---------- 3. 对新增文稿逐个合成语音和字幕 ----------
for md in $NEW_MD; do
  [[ -z "$md" ]] && continue  # 防止 NEW_MD 为空时报错
  base=$(basename "$md" .md)
  wav="$SAVE_DIR/${base}.wav"
  srt="$SAVE_DIR/${base}.srt"
  echo "⚙️  合成 $md -> $wav + $srt"
  /usr/bin/python3 tts_cli/long_tts_with_srt.py "$md"
  echo "   完成 $wav + $srt"
done

echo "✅ wrap_sunrich.sh 完成，本次新增 $(echo "$NEW_MD" | wc -w) 篇"
