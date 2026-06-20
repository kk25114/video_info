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
#   0 * * * * /home/github/video_info/wrap_sunrich.sh >> /home/github/video_info/cron_get_transcripts.log 2>&1
#
#   # 手动执行
#   ./wrap_sunrich.sh

set -euo pipefail

export HOME="/home/yank"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export LANG="C.UTF-8"
export LC_ALL="C.UTF-8"

LOG_FILE="/home/github/video_info/cron_get_transcripts.log"
echo "=== wrap_sunrich.sh 开始 - $(date '+%Y-%m-%d %H:%M:%S') ==="

# ---- 代理配置 ----
export HTTP_PROXY="http://172.23.240.1:10806"
export HTTPS_PROXY="http://172.23.240.1:10806"
export NO_PROXY="localhost,127.0.0.1,::1"

cd /home/github/video_info

# ---- 注入 YT_PO_TOKEN（可选）----
# 优先使用已有环境变量；若无，则从 config.json 读取并导出
if [ -z "${YT_PO_TOKEN:-}" ] && [ -f config.json ]; then
  TOK=$(python3 - <<'PY'
import json
try:
    with open('config.json','r',encoding='utf-8') as f:
        d=json.load(f)
    t=d.get('YT_PO_TOKEN')
    if t:
        print(t)
except Exception:
    pass
PY
  )
  if [ -n "$TOK" ]; then
    export YT_PO_TOKEN="$TOK"
    echo "🔑 已从 config.json 注入 YT_PO_TOKEN"
  else
    echo "ℹ️ 未在 config.json 中发现 YT_PO_TOKEN（可选）。"
  fi
fi

CHANNEL_URL="https://www.youtube.com/@sunriches/videos"
OUTPUT_DIR="2.sunrich"
SAVE_DIR="mk_video"

mkdir -p "$SAVE_DIR"

TMP_BEFORE=$(mktemp)
TMP_AFTER=$(mktemp)
cleanup() {
  rm -f "$TMP_BEFORE" "$TMP_AFTER"
}
trap cleanup EXIT

# ---------- 0. 记录现有 Markdown ----------
find "$OUTPUT_DIR" -maxdepth 1 -name '*.md' | LC_ALL=C sort > "$TMP_BEFORE"

# ---------- 1. 抓取最新视频并生成文稿 ----------
/usr/bin/python3 get_transcripts.py "$CHANNEL_URL" \
  --output_dir "$OUTPUT_DIR" --auto-commit --summarize --correct --candidate-size 7

# ---------- 2. 计算新增 Markdown ----------
find "$OUTPUT_DIR" -maxdepth 1 -name '*.md' | LC_ALL=C sort > "$TMP_AFTER"
mapfile -t NEW_MD_ARRAY < <(/usr/bin/python3 - "$TMP_BEFORE" "$TMP_AFTER" <<'PY'
import sys
from pathlib import Path

before = set(Path(sys.argv[1]).read_text(encoding="utf-8").splitlines())
after = Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
for path in after:
    if path and path not in before:
        print(path)
PY
)

if [ "${#NEW_MD_ARRAY[@]}" -eq 0 ]; then
  echo "ℹ️ 本次没有新增文稿，无需合成。"
  echo "=== wrap_sunrich.sh 执行完成 - $(date '+%Y-%m-%d %H:%M:%S') ==="
  exit 0
fi

# ---------- 3. 清空目标目录的旧音频和字幕 ----------
echo "🧹 清理 $SAVE_DIR 目录中的旧音频和字幕文件..."
rm -f "$SAVE_DIR"/*.wav "$SAVE_DIR"/*.srt 2>/dev/null || true
echo "   清理完成"
echo "🧹 旧音频和字幕文件清理完成"

# ---------- 4. 对新增文稿逐个合成语音 ----------
for md in "${NEW_MD_ARRAY[@]}"; do
  [[ -z "$md" ]] && continue
  base=$(basename "$md" .md)
  wav="$SAVE_DIR/${base}.wav"
  echo "⚙️  合成 $md -> $wav"
  if ! /usr/bin/python3 tts_cli/long_tts_with_srt.py "$md" "$wav"; then
    echo "❌ 语音合成失败: $md"
    exit 1
  fi
  echo "   完成 $wav"
  echo "✅ 语音合成完成: $wav"
done

echo "✅ wrap_sunrich.sh 完成，本次处理 ${#NEW_MD_ARRAY[@]} 篇"
echo "=== wrap_sunrich.sh 执行完成 - $(date '+%Y-%m-%d %H:%M:%S') ==="
echo "📈 本次执行总结: 处理 ${#NEW_MD_ARRAY[@]} 篇文稿"
