#!/usr/bin/env bash
# wrap_sunrich.sh: Sunrich频道专用定时任务包装脚本
#
# 功能：自动处理Sunrich频道新视频，包含文稿生成+语音合成
# 特点：智能检测新增文件、自动清理、批量语音合成
#
# 使用方法：
#   ./wrap_sunrich.sh
#
# 处理流程：
#   1. 记录现有markdown文件
#   2. 抓取最新视频并生成文稿
#   3. 检测新增文件
#   4. 清理旧音频文件
#   5. 批量合成语音
#
# 输出目录：
#   文稿：2.sunrich/
#   音频：mk_video/
#
# 使用示例：
#   # 添加到crontab，每小时检查一次
#   0 * * * * /home/github/video_info/wrap_sunrich.sh
#
#   # 手动执行
#   ./wrap_sunrich.sh


set -e

# ---- 代理配置 ----
export HTTP_PROXY="http://172.23.240.1:10806"
export HTTPS_PROXY="http://172.23.240.1:10806"
export NO_PROXY="localhost,127.0.0.1,::1"

cd /home/github/video_info

CHANNEL_URL="https://www.youtube.com/@sunriches/videos"
OUTPUT_DIR="2.sunrich"
SAVE_DIR="mk_video"

# ---------- 0. 记录现有 Markdown ----------
find "$OUTPUT_DIR" -maxdepth 1 -name '*.md' | sort > /tmp/md_before.txt

# ---------- 1. 抓取最新视频并生成文稿 ----------
/usr/bin/python3 get_transcripts.py "$CHANNEL_URL" \
  --output_dir "$OUTPUT_DIR" --auto-commit --summarize --correct

# ---------- 2. 计算新增 Markdown ----------
find "$OUTPUT_DIR" -maxdepth 1 -name '*.md' | sort > /tmp/md_after.txt
NEW_MD=$(comm -13 /tmp/md_before.txt /tmp/md_after.txt)

# ---------- 3. 清空目标目录的旧音频和字幕 ----------
echo "🧹 清理 $SAVE_DIR 目录中的旧音频和字幕文件..."
rm -f "$SAVE_DIR"/*.wav "$SAVE_DIR"/*.srt
echo "   清理完成"

# ---------- 4. 对新增文稿逐个合成语音 ----------
for md in $NEW_MD; do
  [[ -z "$md" ]] && continue  # 防止 NEW_MD 为空时报错
  base=$(basename "$md" .md)
  wav="$SAVE_DIR/${base}.wav"
  echo "⚙️  合成 $md -> $wav"
  /usr/bin/python3 tts_cli/long_tts_with_srt.py "$md" "$wav"
  echo "   完成 $wav"
done

echo "✅ wrap_sunrich.sh 完成，本次新增 $(echo "$NEW_MD" | wc -w) 篇"
