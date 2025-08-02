#!/usr/bin/env bash
# get_txt.bash: 手动获取各频道视频的示例脚本
#
# 功能：为每个频道提供一键获取视频文稿的示例命令
# 特点：包含所有频道的完整示例，可复制粘贴使用
#
# 使用方法：
#   方法1：直接执行查看所有示例
#     ./get_txt.bash
#
#   方法2：复制对应命令单独执行
#     # 复制下面任意一行执行
#
# 支持的频道及用途：
#   1.大问题    - 哲学思辨类内容
#   2.sunrich   - 时事评论类内容  
#   3.越哥说电影 - 电影解说类内容
#   4.吟游诗人基德 - 科技科普类内容
#   5.科学声音  - 科学教育类内容
#   6.天才简史  - 人物传记类内容
#   9.小播讲哲学 - 哲学普及类内容
#   12.小林说   - 金融经济类内容

echo "=== YouTube视频文稿获取示例 ==="
echo ""
echo "使用方法：复制下面任意命令执行"
echo ""

# 示例：指定输出目录并获取
cat << 'EOF'
# 获取1.大问题频道（哲学思辨）
python3 get_transcripts.py "https://www.youtube.com/@question-dialectic/videos" --output_dir "1.大问题" --auto-commit --summarize --correct

# 获取2.sunrich频道（时事评论）
python3 get_transcripts.py "https://www.youtube.com/@sunriches/videos" --output_dir "2.sunrich" --auto-commit --summarize --correct

# 获取3.越哥说电影频道（电影解说）
python3 get_transcripts.py "https://www.youtube.com/@yuegemovie" --output_dir "3.越哥说电影" --auto-commit --summarize --correct

# 获取4.吟游诗人基德频道（科技科普）
python3 get_transcripts.py "https://www.youtube.com/@gleekid/videos" --output_dir "4.吟游诗人基德" --auto-commit --summarize --correct

# 获取5.科学声音频道（科学教育）
python3 get_transcripts.py "https://www.youtube.com/@voice-of-science/videos" --output_dir "5.科学声音" --auto-commit --summarize --correct

# 获取6.天才简史频道（人物传记）
python3 get_transcripts.py "https://www.youtube.com/@TianCaiJianShi/videos" --output_dir "6.天才简史" --auto-commit --summarize --correct

# 获取9.小播讲哲学频道（哲学普及）
python3 get_transcripts.py "https://www.youtube.com/@xiaoboreading/videos" --output_dir "9.小播讲哲学" --auto-commit --summarize --correct

# 获取12.小林说频道（金融经济）
python3 get_transcripts.py "https://www.youtube.com/@xiao_lin_shuo/videos" --output_dir "12.小林说" --auto-commit --summarize --correct
EOF

echo ""
echo "=== 使用说明 ==="
echo "参数说明："
echo "  --output_dir    指定输出目录"
echo "  --auto-commit   自动提交到Git"
echo "  --summarize     自动总结内容"
echo "  --correct       启用字幕校正"
echo ""
echo "执行步骤："
echo "1. 选择需要的频道命令"
echo "2. 复制粘贴到终端执行"
echo "3. 等待处理完成"