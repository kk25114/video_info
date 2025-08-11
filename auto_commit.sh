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

# 进入脚本所在的目录，以确保 git 命令在正确的仓库中执行
cd "$(dirname "$0")"
git add .gitignore
# 1. 检查是否有文件需要提交
if [[ -z $(git status -s) ]]; then
    echo "✅ 工作区是干净的，没有需要提交的更改。"
    exit 0
fi

# 2. 添加所有更改到暂存区
git add .
echo "Git: 已暂存所有更改。"

# 3. 创建一个提交
# 使用当前的日期和时间作为提交信息
COMMIT_MESSAGE="更新文稿 $(date +'%Y-%m-%d %H:%M:%S')"
git commit -m "$COMMIT_MESSAGE"
echo "Git: 已创建提交，信息为: $COMMIT_MESSAGE"

# 4. 推送到远程仓库
git push
echo "Git: 已成功推送到远程仓库。"

echo -e "\n🎉 所有任务完成并已成功同步到 GitHub!" 