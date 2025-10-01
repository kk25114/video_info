#!/usr/bin/env bash
set -euo pipefail

python3 /home/github/video_info/mk_video/build.py


VIDEO_DIR="/mnt/d/Program Files/下载"
# 优先按“文件名前缀的连续数字最大”选择最新作品；若无编号则回退按修改时间
LATEST_VIDEO=$(python3 - <<'PY'
import os, re, sys, glob
VIDEO_DIR = os.environ.get('VIDEO_DIR', '/mnt/d/Program Files/下载')
try:
    names = os.listdir(VIDEO_DIR)
except FileNotFoundError:
    names = []

cands = []
for name in names:
    if not name.lower().endswith('.mp4'):
        continue
    m = re.match(r'^(\d+)', name)
    if m:
        try:
            cands.append((int(m.group(1)), name))
        except ValueError:
            pass

if cands:
    cands.sort(key=lambda x: x[0], reverse=True)
    print(os.path.join(VIDEO_DIR, cands[0][1]))
else:
    files = glob.glob(os.path.join(VIDEO_DIR, '*.mp4'))
    if not files:
        sys.exit(1)
    latest = max(files, key=os.path.getmtime)
    print(latest)
PY
)

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
