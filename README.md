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
## 核心组件与使用示例

| 组件 | 功能 | 主要参数 | 最小示例 |
|------|------|----------|----------|
| `get_transcripts.py` | 抓取视频 → 官方字幕 / Whisper / FunASR → Markdown | `url`(必填) 频道/播放列表；`--output_dir` 保存目录；`--asr funasr|whisper` 备用识别；`--auto-commit` 抓取完自动 git push；`--summarize/--correct` 调用 DeepSeek 摘要&校正 | ```
python3 get_transcripts.py "https://url" --output_dir "2.sunrich" --asr funasr --summarize --correct
``` |
| `tts_cli/long_tts.py` | 将超长 Markdown 正文分段调用 Azure Speech → WAV | `input_file`(必填) Markdown/纯文本；`output_wav`(可选) 若省略落到 `saveDir`；参数均由 `config.json` 控制，如 `voiceName/speed` 等 | ```
python3 tts_cli/long_tts.py 2.sunrich/0077.md \
                       "/mnt/d/Program Files/下载/0077.wav"
``` |
| `wrap_sunrich.sh` | Sunrich 专用流水线：① 记录旧 md ② 抓取 ③ 差集 ④ 每篇朗读 | 变量：`CHANNEL_URL` `OUTPUT_DIR` `SAVE_DIR`；内部调用前两脚本 | ```
./wrap_sunrich.sh   # 一键完成抓取+朗读
``` |


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
