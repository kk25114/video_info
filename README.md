# Video Info Pipeline 🗂️→📝→🔊→🎬

本仓库是一个完整的**视频内容自动化处理流水线**，支持从**视频抓取→字幕提取→AI摘要→语音合成→视频制作**的全流程自动化。

---

## 🚀 功能特性

### 1. 视频内容获取
- ✅ 支持YouTube、B站等主流平台
- ✅ 自动提取官方字幕/备用AI语音识别
- ✅ 智能去重，避免重复处理
- ✅ 支持批量播放列表处理

### 2. AI内容增强
- ✅ DeepSeek AI自动摘要
- ✅ 内容语法校正
- ✅ 智能分段优化

### 3. 语音合成
- ✅ TTS高质量语音合成
- ✅ 支持超长文本分段处理
- ✅ 多种中文语音可选
- ✅ 自动语速语调调节

### 4. 视频制作
- ✅ 自动生成背景视频+配音+字幕
- ✅ 智能封面生成
- ✅ 720p高清输出
- ✅ 支持图片叠加层

---

## 📁 完整目录结构

```
video_info/
├── 1.大问题/                    # 哲学思辨类内容
├── 2.sunrich/                  # 财经时事分析
├── 3.越哥说电影/               # 电影解说
├── 4.吟游诗人基德/             # 科技科普
├── 5.科学声音/                 # 科学教育
├── mk_video/                   # 🎬 视频制作工具
│   ├── build.py               # 核心构建脚本
│   ├── videos/                # 背景视频素材
│   └── images/                # 叠加图片素材
├── tts_cli/                   # 🔊 语音合成工具
│   ├── long_tts.py            # 长文本TTS主程序
│   ├── config.json            # TTS配置
│   └── README.md              # TTS使用说明
├── get_transcripts.py         # 📥 视频内容获取
├── wrap_sunrich.sh           # 🔄 完整流水线脚本
├── requirements.txt          # Python依赖
├── config.json              # 全局配置
├── auto_commit.sh          # 自动Git提交
└── README.md               # 📖 项目文档
```

---

## 🛠️ 核心组件详解

### 1. 内容获取 - get_transcripts.py
**功能**: 视频→字幕→Markdown

| 参数 | 说明 | 示例 |
|------|------|------|
| `url` | 视频/播放列表URL | `https://www.youtube.com/watch?v=xxx` |
| `--output_dir` | 输出目录 | `2.sunrich` |
| `--asr` | ASR引擎 | `whisper` / `funasr` |
| `--summarize` | AI摘要 | ✓ |
| `--correct` | AI校正 | ✓ |

```bash
# 示例：获取单个视频并AI增强
python3 get_transcripts.py "https://youtu.be/xxx" \
  --output_dir "2.sunrich" \
  --asr whisper \
  --summarize \
  --correct
```

### 2. 语音合成 - tts_cli/long_tts.py
**功能**: Markdown→高质量语音

```bash
# 首次运行生成配置
cd tts_cli
python3 long_tts.py dummy.txt  # 自动生成config.json

# 编辑配置
nano config.json
{
  "speechKey": "你的Azure密钥",
  "serviceRegion": "eastasia",
  "voiceName": "zh-CN-YunyangNeural",
  "saveDir": "/mnt/d/Program Files/下载"
}

# 合成语音
python3 long_tts.py ../2.sunrich/最新文章.md
```

### 3. 视频制作 - mk_video/build.py
**功能**: 背景视频+配音+字幕→成品视频


```bash
# 使用方法
1. 准备文件：
   - 音频：xxx.wav（tts输出）
   - 字幕：xxx.srt（自动生成）
   - 背景：videos/*.mp4

2. 运行构建
cd mk_video
python3 build.py

3. 输出文件：
   - xxx.mp4（成品视频）
   - xxx.png（封面图）
```

### 4. 一键流水线 - wrap_sunrich.sh
**功能**: 全自动更新→朗读→视频制作

```bash
# 一键执行完整流程
./wrap_sunrich.sh

# 输出示例：
# 🔍 发现新视频 3 个
# 📥 已获取字幕 3 篇
# 🔊 已合成语音 3 条
# 🎬 已制作视频 3 个
# ✅ 全部完成！
```

---

## ⚙️ 安装配置

### 系统要求
- Python 3.9+
- FFmpeg
- Git
- 8GB+ RAM（推荐）

### 快速安装
```bash
# 克隆项目
git clone https://github.com/yourname/video_info.git
cd video_info

# 安装依赖
python3 -m pip install -r requirements.txt
python3 -m pip install azure-cognitiveservices-speech

# 系统依赖
sudo apt update && sudo apt install -y ffmpeg git

# 初始化配置
python3 get_transcripts.py --help
```

### 配置文件
```json
// config.json
{
  "azure_speech_key": "your-key-here",
  "azure_service_region": "eastasia",
  "default_voice": "zh-CN-YunyangNeural",
  "output_dir": "/mnt/d/Program Files/下载",
  "video_quality": "720p",
  "auto_commit": true
}
```


## 📊 处理状态追踪

每个频道目录包含状态文件：
```
频道目录/
├── processed_videos.log      # 已处理视频ID
├── failed_videos.log         # 失败视频ID
├── last_processed_date.log   # 最后处理时间
└── processing.log            # 详细处理日志
```

---

## 🎯 使用场景

### 个人创作者
- 快速制作知识分享视频
- 批量处理播客内容
- 自动生成学习笔记

### 企业应用
- 内部培训内容制作
- 会议纪要视频化
- 产品说明自动化

### 教育领域
- 课程视频批量制作
- 学习资料语音化
- 多语言内容生成



## 🔧 故障排除

### 常见问题解决
```bash
# 检查FFmpeg
ffmpeg -version

# 检查Python环境
python3 -c "import whisper; print('OK')"

# 检查网络连接
curl -I https://www.youtube.com

# 查看详细日志
tail -f cron_get_transcripts.log
```

### 性能优化
- 使用GPU加速：安装CUDA版本的PyTorch
- 并发处理：修改脚本参数支持多线程
- 缓存优化：合理设置缓存目录

---


## 📄 许可证

MIT License - 详见 [LICENSE](LICENSE) 文件

---


如有问题，请提 [Issue](https://github.com/yourname/video_info/issues) 或联系维护者。