# long_tts  —  Azure 长文本语音合成脚本

一个 **单文件命令行** 工具，依赖 Azure Speech SDK，可将超长 UTF-8 文本合成为 WAV 音频。

---
## 1. 安装依赖
```bash
pip install azure-cognitiveservices-speech
sudo apt install -y ffmpeg   # 用于拼接分段音频
```

---
## 2. 配置（config.json）
首次运行脚本会在当前目录自动创建 `config.json` 并退出：
```bash
python3 long_tts.py dummy.txt  # 生成配置模板
```
打开文件填写或修改即可：
```json
{
  "speechKey": "<YOUR_AZURE_KEY>",
  "serviceRegion": "westus",
  "voiceName": "zh-CN-YunyangNeural",
  "voiceStyle": "customerservice",
  "role": "",
  "speed": 1.16,
  "pitch": "0%",
  "saveDir": "./tts_output",
  "retryCount": 0,
  "retryInterval": 5,
  "chunkLimit": 4500
}
```
常用字段说明：
| 字段 | 作用 | 示例 |
|------|------|------|
| speechKey | **必填**，Azure 语音服务密钥 | `52J6...` |
| serviceRegion | 区域 | `westus` |
| voiceName | 音色 | `zh-CN-XiaoxiaoNeural` |
| voiceStyle | 说话风格（部分音色支持） | `customerservice` |
| role | 角色（部分音色支持） | `Boy` |
| speed | 语速 (1.0 = 原速) | `0.9` |
| pitch | 音高 | `"+5%"` |
| saveDir | 输出目录 | `./tts_output` |
| retryCount / retryInterval | 失败重试 | `3 / 5` |
| chunkLimit | 单段最大字符数 | `4500` |

---
## 3. 使用
```bash
cd /home/github/video_info/tts_cli
python3 long_tts.py article.txt
```
完成后将在 `saveDir` 中生成 `YYYYMMDD_HHMMSS.wav`。

---
## 4. 主要特性
* 自动分段（默认每段 ≤ 4500 字符），超长文本一键合成。
* 支持失败自动重试、临时分段自动清理。
* 所有逻辑纯 Python，易于集成到定时任务、CI、后台服务。