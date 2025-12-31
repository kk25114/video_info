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
* 代理相关（WSL 常见）：`proxy` / `bypassMicrosoftProxy`
* 合成超时相关：`rtfTimeoutThreshold` / `frameTimeoutIntervalMs`（可用来放宽 SDK 默认阈值）
* 预切分（建议开启以减少超时）：`proactiveSplitLimit`（默认 1200；设为 0 表示关闭）
* 超时自动切分：`autoSplitOnTimeout` / `timeoutSplitLimit`

配置修改后立即生效，无需重启任何服务。

## 5. 代理与直连（解决 `WS_OPEN_ERROR` / `no connection to the remote host`）
这类报错通常是 **网络不可达** 或 **代理/直连策略不匹配** 导致。

你可以先做一次“模拟连通性测试”（不需要真的合成）：
```bash
python3 long_tts_with_srt.py --probe-network
```
它会分别探测：
- 直连到 `*.tts.speech.microsoft.com:443` 的 TLS 握手是否可达
- 通过当前代理做 `CONNECT + TLS` 是否可达
并给出推荐的 `bypassMicrosoftProxy` 设置。

常见两种模式（以 WSL + Windows 代理为例）：

1) 让 Azure TTS 走代理（推荐先试这个）
- `config.json` 增加：
  - `proxy`: `"http://172.23.240.1:10806"`（按你的代理端口调整）
  - `bypassMicrosoftProxy`: `false`

2) 微软域名直连，其它走代理（有些代理对 WebSocket 不友好时可试）
- `config.json` 增加：
  - `proxy`: `"http://172.23.240.1:10806"`
  - `bypassMicrosoftProxy`: `true`

脚本会在检测到“直连 443 不通”时自动临时切换为“让微软域名走代理”，减少因直连失败导致的中断。

另外如果你看到类似：
- `Timeout while synthesizing ... Current RTF ... threshold ...`
- `frame interval ... (threshold ...)`

通常意味着 **单段文本偏长** 或 **当前网络吞吐不足**。建议：
1) 把 `proactiveSplitLimit` 调小（例如 800 / 600）
2) 把 `timeoutSplitLimit` 调小（例如 800 / 600）
3) 或适当放宽 `rtfTimeoutThreshold` / `frameTimeoutIntervalMs`
