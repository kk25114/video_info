#!/usr/bin/env bash
# run_transcribe.sh: 定时任务-转换脚本使用 统一封装视频转录脚本
# 用法: run_transcribe.sh CHANNEL_URL OUTPUT_DIR
# 依赖: get_transcripts.py 位于同目录

# ---- 代理配置 ----
export HTTP_PROXY="http://172.23.240.1:10806"
export HTTPS_PROXY="http://172.23.240.1:10806"
export NO_PROXY="localhost,127.0.0.1,::1"

# ---- 进入项目目录 ----
cd /home/github/video_info || exit 1

CHANNEL_URL="$1"
OUTPUT_DIR="$2"

if [[ -z "$CHANNEL_URL" || -z "$OUTPUT_DIR" ]]; then
  echo "用法: $0 CHANNEL_URL OUTPUT_DIR" >&2
  exit 1
fi

/usr/bin/python3 get_transcripts.py "$CHANNEL_URL" \
  --output_dir "$OUTPUT_DIR" \
  --auto-commit --summarize --correct \
  >> /home/github/video_info/cron_get_transcripts.log 2>&1
