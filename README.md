# video_info

这是一个从 YouTube 下载视频文稿的工具。

## 功能

- 从 YouTube 频道、播放列表或单个视频链接下载所有视频的文稿。
- 将每个视频的文稿保存为独立的 Markdown 文件。
- 文件名会根据视频标题自动生成，并进行清理以兼容文件系统。
- 保存的 Markdown 文件中包含视频标题、原始链接和文稿内容。

## 依赖

- Python 3
- [yt-dlp](https://github.com/yt-dlp/yt-dlp): 一个强大的视频下载工具。
- [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api): 用于获取 YouTube 视频文稿的 Python API。
- [requests](https://pypi.org/project/requests/): 用于发起 HTTP 请求。

## 安装依赖

1.  **安装 yt-dlp**

    脚本的核心依赖 `yt-dlp` 是一个命令行工具，请根据你的操作系统参考 [官方安装指南](https://github.com/yt-dlp/yt-dlp#installation)进行安装。

2.  **安装 Python 库**

    ```bash
    pip install youtube-transcript-api requests
    ```

## 使用方法

使用 `get_transcripts.py` 脚本来下载文稿。

### 基本用法

提供一个 YouTube 链接（频道、播放列表或单个视频）作为参数。

```bash
python3 get_transcripts.py "YOUTUBE_URL"
```

文稿将默认保存在 `transcripts` 目录中。

### 示例

#### 1. 抓取一个频道的所有视频

```bash
python3 get_transcripts.py "https://www.youtube.com/@some-channel/videos"
```

#### 2. 指定输出目录

你可以使用 `--output_dir` 参数来指定保存文稿的目录。

```bash
python3 get_transcripts.py "https://www.youtube.com/@some-channel/videos" --output_dir "my_transcripts"
```

## 脚本参数

- `youtube_url`: (必需) YouTube 频道、播放列表或单个视频的 URL。
- `--output_dir`: (可选) 保存文稿文件的目录路径。默认为 `transcripts`。