# Video Info Pipeline 🗂️→📝→🔊

本仓库用于 **语音识别 → 转 Markdown → 生成语音文件*


---
## 目录结构
```
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
## 核心组件
| 组件 | 功能 |
|------|------|
| `get_transcripts.py` | 抓取视频 → 官方字幕 / Whisper / FunASR → Markdown |
| `tts_cli/long_tts.py` | 将超长 Markdown 正文分段调用 Azure Speech → WAV |
| `wrap_sunrich.sh` | 将两者串联：<br>① 记录旧 md → ② 抓取 → ③ 找新增 md → ④ 同名 WAV |

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
     "voiceName": "zh-CN-YunyangNeural",
     ...
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
# 终端输出：
# ⚙️  合成 2.sunrich/0077_…md -> …/下载/0077_….wav
# ✅ wrap_sunrich.sh 完成，本次新增 1 篇
```

---
## 定时任务 crontab
```
HTTP_PROXY=… (如需要)
HTTPS_PROXY=…

# 每日 07:00 抓取 + 转语音@Sunrich
0 7 * * * /home/github/video_info/wrap_sunrich.sh \
        >> /home/github/video_info/cron_get_transcripts.log 2>&1
```

---
## 流程细节
### get_transcripts.py 如何判重？
1. 读取 `processed_videos.log` / `failed_videos.log`。
2. 若 video_id 已出现则跳过。
3. 若上传日期 ≤ `last_processed_date.log` 立即停止遍历。

### long_tts.py 如何提取正文？
1. 定位 **最后一条 `---` 分隔线**。
2. 跳过空行与 `> **注意** …` 引用。
3. 剩余文字按中文标点自动分段后送 Azure TTS。

---
## 常见问题
* **WAV 与 Markdown 同名吗？** 是。`wrap_sunrich.sh` 取 `basename .md` 拼成 `basename.wav`。
* **其它频道想复用？** 复制 `wrap_sunrich.sh`，改 `CHANNEL_URL / OUTPUT_DIR` 即可。
* **想生成 MP3？** 在 `long_tts.py` 把 `SpeechSynthesisOutputFormat` 改成 MP3，并调整 `ffmpeg` 输出格式。

---
Enjoy the automated pipeline! 🎉