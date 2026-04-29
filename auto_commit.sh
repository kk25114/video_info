#!/bin/bash
#
# 自动提交脚本 - GitHub同步工具
#
# 功能：
# - 自动检测git仓库变更
# - 智能提交所有更改
# - 自动生成时间戳提交信息
# - 一键推送到远程仓库
#
# 使用方法：
#     # 手动执行
#     ./auto_commit.sh
#     
#     # 在get_transcripts.py中自动调用
#     python3 get_transcripts.py URL --output_dir DIR --auto-commit
#     
#     # 添加到crontab定时执行
#     0 */6 * * * /home/github/video_info/auto_commit.sh
# 示例输出：
#     -----------------------------------------
#     🚀 开始自动同步到 GitHub...
#     -----------------------------------------
#     Git: 已暂存所有更改。
#     Git: 已创建提交，信息为: 更新文稿 2025-08-02 14:30:25
#     Git: 已成功推送到远程仓库。


echo "-----------------------------------------"
echo "🚀 开始自动同步到 GitHub..."
echo "-----------------------------------------"

DEEPSEEK_BASE_URL="https://api.deepseek.com"
DEEPSEEK_MODEL="deepseek-v4-pro"

# 进入脚本所在的目录，以确保 git 命令在正确的仓库中执行
cd "$(dirname "$0")"

# 预处理：读取 .gitignore，停止追踪已被忽略但仍在版本库中的文件
if [[ -f .gitignore ]]; then
    IGNORED_TRACKED=$(git ls-files -ci --exclude-from=.gitignore || true)
    if [[ -n "$IGNORED_TRACKED" ]]; then
        echo "🔎 检测到已被 .gitignore 忽略但仍受版本控制的文件，准备从暂存区移除追踪："
        echo "$IGNORED_TRACKED" | sed 's/^/ - /'
        echo "$IGNORED_TRACKED" | xargs -r git rm -r --cached --
    fi
fi

# 1. 检查是否有文件需要提交
if [[ -z $(git status -s) ]]; then
    echo "✅ 工作区是干净的，没有需要提交的更改。"
    exit 0
fi

# 2. 添加所有更改到暂存区
git add -A
echo "Git: 已暂存所有更改。"

# 3. 创建一个提交
# 生成提交信息：优先使用 DeepSeek 智能摘要，失败则回退到时间戳信息
STATS=$(git diff --cached --shortstat)
FILE_LIST=$(git diff --cached --name-status | sed 's/^/ - /' | head -n 100)

AI_COMMIT_MESSAGE=$(env GIT_STATS="$STATS" GIT_FILE_LIST="$FILE_LIST" DEEPSEEK_BASE_URL="$DEEPSEEK_BASE_URL" DEEPSEEK_MODEL="$DEEPSEEK_MODEL" python3 - <<'PY'
import os, json, urllib.request, sys

# 读取 API Key（与 get_transcripts.py 相同的配置方式）
api_key = None
try:
    with open("config.json", "r", encoding="utf-8") as f:
        api_key = json.load(f).get("DEEPSEEK_API_KEY")
except Exception:
    api_key = None

if not api_key:
    print("")
    sys.exit(0)

stats = os.environ.get("GIT_STATS", "")
files = os.environ.get("GIT_FILE_LIST", "")
base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

prompt = (
    "你是中文Git提交信息生成器。请根据以下已暂存变更生成一条不超过60字的中文单行提交信息："
    "准确概括本次修改的主要意图与范围，使用动宾短语，避免口水词，只输出提交信息本身。\n"
    f"变更统计: {stats}\n"
    f"文件列表:\n{files}"
)

data = {
    "model": model,
    "messages": [{"role": "user", "content": prompt}],
    "temperature": 0.2
}

req = urllib.request.Request(
    f"{base_url}/chat/completions",
    data=json.dumps(data).encode("utf-8"),
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    method="POST"
)

try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        msg = (result.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        print(msg)
except Exception:
    print("")
PY
)

if [ -n "$AI_COMMIT_MESSAGE" ]; then
    COMMIT_MESSAGE=$(echo "$AI_COMMIT_MESSAGE" | head -n 1 | tr -d '\r')
else
    COMMIT_MESSAGE="更新文稿 $(date +'%Y-%m-%d %H:%M:%S')"
fi

git commit -m "$COMMIT_MESSAGE"
echo "Git: 已创建提交，信息为: $COMMIT_MESSAGE"

# 4. 推送到远程仓库
git push
echo "Git: 已成功推送到远程仓库。"

echo -e "\n🎉 所有任务完成并已成功同步到 GitHub!" 
