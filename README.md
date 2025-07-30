# Video Info Pipeline 🗂️→📝→🔊

本仓库用于 **语音识别 → 转 Markdown → 生成语音文件**，并自动同步 GitHub 与 Windows 下载目录。

---
## 目录结构
```txt
video_info/
├─ 2.sunrich/                  # Sunrich 频道 Markdown 输出
│  ├─ 0077_*.md
│  ├─ processed_videos.log     # 成功 ID
│  ├─ failed_videos.log        # 连续失败 ID
│  └─ last_processed_date.log  # 上次完整运行时间
│
├─ tts_cli/                    # 长文本 TTS 工具
│  ├─ long_tts.py              # 主脚本
│  ├─ config.json              # 语音配置（首次自动生成）
│  └─ README.md
│
├─ wrap_sunrich.sh             # Sunrich 专用抓取+朗读流水线
└─ cron_get_transcripts.log    # 定时任务日志
```

---
## 核心组件与使用示例

| 组件 | 功能 | 主要参数 | 最小示例 |
|------|------|----------|----------|
| `get_transcripts.py` | 抓取视频 → 官方字幕 / Whisper / FunASR → Markdown | `url`(必填)：频道/播放列表<br>`--output_dir`：保存目录<br>`--asr funasr\|whisper`：备用识别<br>`--auto-commit`：自动 git push<br>`--summarize / --correct`：DeepSeek 摘要&校正 | `python3 get_transcripts.py "https://url" --output_dir "2.sunrich" --asr funasr --summarize --correct` |
| `tts_cli/long_tts.py` | 将超长 Markdown 正文分段调用 Azure Speech → WAV | `input_file`(必填)：Markdown/纯文本<br>`output_wav`(可选)：省略则落到 `saveDir`<br>其余参数在 `config.json` 控制 | `python3 tts_cli/long_tts.py 2.sunrich/0077.md "/mnt/d/Program Files/下载/0077.wav"` |
| `wrap_sunrich.sh` | Sunrich 专用流水线（差集 + 朗读） | 修改脚本顶部 `CHANNEL_URL / OUTPUT_DIR / SAVE_DIR` | `./wrap_sunrich.sh` |

---
## 安装依赖
```bash
# Python 3.9+
python3 -m pip install -r requirements.txt        # yt-dlp / requests / whisper 等
python3 -m pip install azure-cognitiveservices-speech
sudo apt install -y ffmpeg                        # TTS 拼接
```

---
## tts_cli 使用
1. 首次运行生成 `config.json`：
   ```bash
   cd tts_cli
   python3 long_tts.py dummy.txt   # 会提示填写 speechKey
   ```
2. 编辑 `config.json`：
   ```json
   {
     "speechKey": "YOUR_AZURE_KEY",
     "serviceRegion": "westus",
     "saveDir": "/mnt/d/Program Files/下载",
     "voiceName": "zh-CN-YunyangNeural"
   }
   ```
3. 单独朗读一篇 Markdown：
   ```bash
   python3 long_tts.py ../2.sunrich/0077_房价暴跌背后的逻辑.md \
                       "/mnt/d/Program Files/下载/0077_房价暴跌背后的逻辑.wav"
   ```

---
## wrap_sunrich.sh 手动执行示例
```bash
./wrap_sunrich.sh
# 输出示例：
# ⚙️  合成 2.sunrich/0077_*.md -> …/下载/0077_*.wav
# ✅ wrap_sunrich.sh 完成，本次新增 1 篇
```

---
## 定时任务 crontab
```crontab
HTTP_PROXY=…
HTTPS_PROXY=…

# 每日 07:00 抓取 + 转语音 @Sunrich
0 7 * * * /home/github/video_info/wrap_sunrich.sh >> /home/github/video_info/cron_get_transcripts.log 2>&1
```

---
## 流程细节
### get_transcripts.py 判重逻辑
1. 已成功 / 失败 ID 写入 `processed_videos.log`、`failed_videos.log`。
2. 再遇到相同 ID 直接跳过。
3. 若上传日期 ≤ `last_processed_date.log`，停止遍历余下列表。

### long_tts.py 正文抽取 & 分段
1. 找到最后 `---` 分隔线。<br>2. 跳过空行与 `> **注意** …` 引用。<br>3. 按中文标点自动分段后调用 Azure TTS。

---
## FAQ
* **WAV 与 Markdown 会同名吗？** 会，`wrap_sunrich.sh` 用 `basename .md → .wav`。
* **想批量朗读其它频道？** 复制一份 `wrap_xxx.sh`，改 `CHANNEL_URL/OUTPUT_DIR`。
* **想生成 MP3？** 改 `long_tts.py` 中 `SpeechSynthesisOutputFormat`，并调整 ffmpeg 参数。

---
Enjoy the automated pipeline! 🎉
