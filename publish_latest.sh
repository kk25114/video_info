#!/usr/bin/env bash
set -euo pipefail

# 构建新成片；若音频/字幕已被上一轮构建消费掉（例如重复执行本脚本），
# 则跳过构建，直接发布下载目录中已有的最新成片。
if compgen -G "/home/github/video_info/mk_video/*.wav" > /dev/null; then
  python3 /home/github/video_info/mk_video/build.py
else
  echo "未发现待构建的 .wav 音频，跳过构建，直接发布最新成片"
fi


VIDEO_DIR="/mnt/d/Program Files/下载"
# 非递归、按修改时间倒序列出后取第一个（更快）；路径含空格也安全
LATEST_VIDEO=$(ls -t -- "$VIDEO_DIR"/*.mp4 2>/dev/null | head -n1)

if [[ -z "${LATEST_VIDEO:-}" ]]; then
  echo "未找到最新的 mp4 视频文件" >&2
  exit 1
fi

echo "使用最新视频: $LATEST_VIDEO"

# 自动发布默认使用虚拟 X Server，避免依赖或打扰可见桌面会话。
# 需要人工操作浏览器验证时，可设置 DOUYIN_VISIBLE_BROWSER=1 显式使用当前桌面。
BROWSER_LAUNCH=()
if [[ "${DOUYIN_VISIBLE_BROWSER:-0}" == "1" ]]; then
  if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    echo "可见浏览器模式需要 DISPLAY 或 WAYLAND_DISPLAY 已配置" >&2
    exit 1
  fi
  echo "使用可见桌面启动 Chrome（人工验证模式）"
elif command -v xvfb-run >/dev/null 2>&1; then
  BROWSER_LAUNCH=(xvfb-run -a)
  echo "常规发布使用 Xvfb 启动 Chrome"
elif [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  echo "找不到 xvfb-run，且没有可用的桌面显示器" >&2
  exit 1
else
  echo "找不到 xvfb-run，回退到当前桌面启动 Chrome"
fi

# 强制直连：仅对本次发布命令生效，不影响系统代理环境
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY -u ftp_proxy -u FTP_PROXY -u NO_PROXY -u no_proxy \
  "${BROWSER_LAUNCH[@]}" python3 /home/github/video_info/douyin_playwright/scripts/publish_video.py \
  --video "$LATEST_VIDEO" \
  --auto-desc \
  --caption-mode desc \
  --state-path /home/github/video_info/douyin_playwright/storage_state.json \
  --save-state \
  --wait-upload-text "上传成功" \
  --publish-selector 'button.button-dhlUZE.primary-cECiOJ.fixed-J9O8Yw' \
  --success-text "发布成功" \
  --slow-mo 200 \
  --screenshot /home/github/video_info/douyin_playwright/test/latest.png

# 发布脚本只有检测到抖音“发布成功”提示才返回成功；失败时 set -e 会在此之前退出，
# 因此素材只会在确认发布成功后清理，失败重试仍可保留本次图片。
IMAGE_DIR="/home/github/video_info/mk_video/images"
if [[ -d "$IMAGE_DIR" ]]; then
  find "$IMAGE_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
  echo "🧹 抖音发布成功，已清空素材目录：$IMAGE_DIR"
fi
