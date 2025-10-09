#!/usr/bin/env bash
set -euo pipefail

python3 /home/github/video_info/mk_video/build.py


VIDEO_DIR="/mnt/d/Program Files/下载"
# 非递归、按修改时间倒序列出后取第一个（更快）；路径含空格也安全
LATEST_VIDEO=$(ls -t -- "$VIDEO_DIR"/*.mp4 2>/dev/null | head -n1)

if [[ -z "${LATEST_VIDEO:-}" ]]; then
  echo "未找到最新的 mp4 视频文件" >&2
  exit 1
fi

echo "使用最新视频: $LATEST_VIDEO"

python3 /home/github/video_info/douyin_playwright/scripts/publish_video.py \
  --video "$LATEST_VIDEO" \
  --auto-desc \
  --caption-mode desc \
  --state-path /home/github/video_info/douyin_playwright/storage_state.json \
  --wait-upload-text "上传成功" \
  --publish-selector 'button.button-dhlUZE.primary-cECiOJ.fixed-J9O8Yw' \
  --success-text "发布成功" \
  --slow-mo 200 \
  --screenshot /home/github/video_info/douyin_playwright/test/latest.png
