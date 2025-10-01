#!/usr/bin/env bash
# run_transcribe.sh: 定时任务-转换脚本使用 统一封装视频转录脚本
#
# 功能：从YouTube频道获取视频并转录为markdown文本
# 特点：包含代理配置、日志记录、错误处理
#
# 使用方法：
#   ./run_transcribe.sh CHANNEL_URL OUTPUT_DIR
#
# 参数说明：
#   CHANNEL_URL - 频道完整URL
#   OUTPUT_DIR  - 输出目录名称（如：1.大问题）
#
# 使用示例：
#   # 转录哲学频道
#   ./run_transcribe.sh "https://www.youtube.com/@question-dialectic/videos" "1.大问题"
#
#   # 转录财经频道
#   ./run_transcribe.sh "https://www.youtube.com/@sunriches/videos" "2.sunrich"
#
#   # 转录电影解说频道
#   ./run_transcribe.sh "https://www.youtube.com/@yuegemovie" "3.越哥说电影"
#
# 日志文件：cron_get_transcripts.log

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
  echo "示例: $0 'https://www.youtube.com/@question-dialectic/videos' '1.大问题'" >&2
  exit 1
fi

/usr/bin/python3 get_transcripts.py "$CHANNEL_URL" \
  --output_dir "$OUTPUT_DIR" \
  --auto-commit --summarize --correct --candidate-size 7 \
  >> /home/github/video_info/cron_get_transcripts.log 2>&1
