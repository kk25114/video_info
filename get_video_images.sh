#!/usr/bin/env bash
# 手动提取 wrap_sunrich.sh 最近处理的视频中的静止图片。
#
# 用法：
#   ./get_video_images.sh
#   ./get_video_images.sh --url 'https://www.youtube.com/watch?v=XXXXXXXXXXX'

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

exec /usr/bin/python3 "$SCRIPT_DIR/mk_video/extract_latest_video_images.py" "$@"
