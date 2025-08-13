#!/usr/bin/env bash
set -euo pipefail

# video_info 打包脚本
# 产物：dist/video_info-${VERSION}.tar.gz, dist/video_info-${VERSION}.zip, dist/SHA256SUMS

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT_DIR"

# 1) 解析版本号：优先取 $VERSION，其次取 tag（GITHUB_REF 或 git describe），最后用日期
VERSION=${VERSION:-}
if [[ -z "${VERSION}" ]]; then
    if [[ -n "${GITHUB_REF:-}" && "${GITHUB_REF}" =~ refs/tags/ ]]; then
        VERSION=${GITHUB_REF#refs/tags/}
    else
        if git describe --tags --always >/dev/null 2>&1; then
            VERSION=$(git describe --tags --always)
        else
            VERSION=$(date +%Y%m%d%H%M%S)
        fi
    fi
fi

echo "[release] version = ${VERSION}"

# 2) 目录准备
rm -rf dist build
mkdir -p dist build
STAGE_DIR="build/video_info-${VERSION}"
mkdir -p "$STAGE_DIR"

# 3) 生成/准备示例配置，避免把真实密钥打包
if [[ ! -f config.example.json ]]; then
cat > config.example.json <<'JSON'
{
  "DEEPSEEK_API_KEY": "sk-xxxxxxxxxxxxxxxxxxxx",
  "azure_speech_key": "your-azure-key",
  "azure_service_region": "eastasia",
  "default_voice": "zh-CN-YunyangNeural",
  "output_dir": "transcripts",
  "video_quality": "720p",
  "auto_commit": true
}
JSON
fi

# 4) 拷贝核心文件/目录（显式白名单），排除数据与日志
copy_if_exists() {
    local path="$1"
    if [[ -e "$path" ]]; then
        rsync -a --exclude "__pycache__" --exclude "*.pyc" --exclude "*.log" "$path" "$STAGE_DIR/"
    fi
}

copy_if_exists README.md
copy_if_exists LICENSE
copy_if_exists requirements.txt
copy_if_exists get_transcripts.py
copy_if_exists auto_segment_chinese_text.py
copy_if_exists auto_commit.sh
copy_if_exists wrap_sunrich.sh
copy_if_exists mk_video
copy_if_exists tts_cli

# 用示例配置替代真实配置
cp -f config.example.json "$STAGE_DIR/config.json"

# 覆盖 tts_cli 配置，避免把真实语音密钥打进包
if [[ -f "$STAGE_DIR/tts_cli/config.json" ]]; then
    rm -f "$STAGE_DIR/tts_cli/config.json"
fi
if [[ -f "tts_cli/config.example.json" ]]; then
    cp -f "tts_cli/config.example.json" "$STAGE_DIR/tts_cli/config.json"
else
    cat > "$STAGE_DIR/tts_cli/config.json" <<'JSON'
{
  "speechKey": "your-azure-key",
  "serviceRegion": "eastasia",
  "voiceName": "zh-CN-YunyangNeural",
  "voiceStyle": "general",
  "role": "",
  "speed": 1.0,
  "pitch": "0%",
  "saveDir": "mk_video",
  "retryCount": 0,
  "retryInterval": 5,
  "chunkLimit": 4500
}
JSON
fi

# 5) 附加清单：写入快速安装说明
cat > "$STAGE_DIR/INSTALL.md" <<'MD'
# 安装与快速开始

1. 安装依赖
```bash
python3 -m pip install -r requirements.txt
sudo apt update && sudo apt install -y ffmpeg git
```

2. 配置密钥
```bash
cp config.json config.local.json
# 编辑 config.local.json，填入你的密钥（例如 DEEPSEEK_API_KEY）
```

3. 运行示例
```bash
python3 get_transcripts.py "https://www.youtube.com/@sunriches/videos" \
  --output_dir "2.sunrich" --auto-commit --summarize --correct
```
MD

# 6) 归档打包
ARCHIVE_NAME="video_info-${VERSION}"
tar -C build -czf "dist/${ARCHIVE_NAME}.tar.gz" "${ARCHIVE_NAME}"
# 如系统无 zip，则用 Python 生成 zip 以减少外部依赖
if command -v zip >/dev/null 2>&1; then
    (
        cd build
        zip -qr "../dist/${ARCHIVE_NAME}.zip" "${ARCHIVE_NAME}"
    )
else
    python3 - <<PY
import os, sys, zipfile
root = os.path.join('build', '${ARCHIVE_NAME}')
zip_path = os.path.join('dist', '${ARCHIVE_NAME}.zip')
with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
    for base, _, files in os.walk(root):
        for f in files:
            p = os.path.join(base, f)
            zf.write(p, arcname=os.path.relpath(p, 'build'))
print(zip_path)
PY
fi

# 7) 生成校验文件
(
    cd dist
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "${ARCHIVE_NAME}.tar.gz" "${ARCHIVE_NAME}.zip" > SHA256SUMS
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "${ARCHIVE_NAME}.tar.gz" "${ARCHIVE_NAME}.zip" > SHA256SUMS
    else
        python3 - <<PY
import hashlib, sys
paths = ['${ARCHIVE_NAME}.tar.gz','${ARCHIVE_NAME}.zip']
with open('SHA256SUMS','w') as w:
    for p in paths:
        h = hashlib.sha256()
        with open(p,'rb') as f:
            for chunk in iter(lambda: f.read(1<<20), b''):
                h.update(chunk)
        w.write(f"{h.hexdigest()}  {p}\n")
PY
    fi
)

echo "[release] artifacts:"
ls -lh dist


