# tts_cli 使用说明

该目录包含长文本语音合成脚本 `long_tts_with_srt.py` 及配置文件 `config.json`（同时输出 `.wav` + `.srt`）。

## 1. 快速开始
```bash
cd tts_cli
# 第一次运行会生成 config.json 并提示填写 speechKey
python3 long_tts_with_srt.py dummy.txt
# 编辑 config.json 填写 Azure KEY 后即可使用
```

## 2. 手动转换任意 Markdown
示例：将 `../2.sunrich/0075_释永信的问题，没那么简单.md` 朗读为同名 wav。

```bash
# 进入 tts_cli 目录
cd /home/github/video_info/tts_cli

MD="../2.sunrich/0075_释永信的问题，没那么简单.md"
BASENAME=$(basename "$MD" .md)
OUT="/mnt/d/Program Files/下载/${BASENAME}.wav"

python3 long_tts_with_srt.py "$MD" "$OUT"
```

脚本会：
1. 自动裁剪正文（跳过标题/注意提示）。
2. 按中文标点分段调用 Azure TTS。
3. 拼接生成与 Markdown 同名的 wav 到 `saveDir`（或你指定的 OUT 路径）。

## 3. 仅指定输入，不给输出
如果第二个参数省略，脚本会把音频保存到 `config.json` 的 `saveDir`，文件名为时间戳：
```bash
python3 long_tts_with_srt.py ../2.sunrich/0075_释永信的问题，没那么简单.md
# => /mnt/d/Program Files/下载/20250730_203015.wav
```

## 4. 调整参数
修改 `config.json` 中：
* `voiceName` / `voiceStyle` / `speed` / `pitch`
* `saveDir` 输出目录
* `retryCount` / `retryInterval` 网络不稳时可增加重试
* 合成超时相关：`rtfTimeoutThreshold` / `frameTimeoutIntervalMs`（可用来放宽 SDK 默认阈值）
* 预切分（建议开启以减少超时）：`proactiveSplitLimit`（默认 1200；设为 0 表示关闭）
* 超时自动切分：`autoSplitOnTimeout` / `timeoutSplitLimit`

配置修改后立即生效，无需重启任何服务。
