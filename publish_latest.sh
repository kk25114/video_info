#!/usr/bin/env bash
set -euo pipefail

python3 /home/github/video_info/mk_video/build.py


VIDEO_DIR="/mnt/d/Program Files/下载"
LATEST_VIDEO=$(find "$VIDEO_DIR" -type f -name '*.mp4' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)

if [[ -z "${LATEST_VIDEO:-}" ]]; then
  echo "未找到最新的 mp4 视频文件" >&2
  exit 1
fi

echo "使用最新视频: $LATEST_VIDEO"

python3 /home/github/video_info/douyin_playwright/scripts/publish_video.py \
  --video "$LATEST_VIDEO" \
  --auto-desc \
  --state-path /home/github/video_info/douyin_playwright/storage_state.json \
  --wait-upload-text "上传成功" \
  --publish-selector 'button.button-dhlUZE.primary-cECiOJ.fixed-J9O8Yw' \
  --success-text "发布成功" \
  --slow-mo 200 \
  --screenshot /home/github/video_info/douyin_playwright/test/latest.png
