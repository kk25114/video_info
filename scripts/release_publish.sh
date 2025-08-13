#!/usr/bin/env bash
set -euo pipefail

# 一键发布脚本
# 支持两种发布模式：
# - actions: 推 tag 触发 GitHub Actions 自动打包与发布（默认）
# - gh: 使用 gh CLI 本地打包并直接创建/更新 Release

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT_DIR"

usage() {
	cat <<'USAGE'
用法:
  scripts/release_publish.sh --tag v1.0.0 [--title "v1.0.0"] [--notes RELEASE.md] [--mode actions|gh] [--build true|false]

参数:
  --tag		版本标签(必填)，如 v1.0.0
  --title	Release 标题(可选，默认同 tag)
  --notes	Release 说明(可选)，可以是纯文本或一个 Markdown 文件路径
  --mode	发布模式: actions|gh (默认 actions)
  --build	是否本地打包: true|false (默认 true)

示例:
  # 1) 推 tag 触发 Actions 自动发布（推荐）
  scripts/release_publish.sh --tag v0.2.0

  # 2) 推 tag 并自定义标题
  scripts/release_publish.sh --tag v0.2.0 --title "v0.2.0"

  # 3) 只推送 tag，不在本地打包（完全交给 Actions 构建）
  scripts/release_publish.sh --tag v0.2.0 --build false

  # 4) 本地打包并用 gh CLI 直接发布（需 gh 已登录）
  scripts/release_publish.sh --tag v0.2.0 --mode gh --notes CHANGELOG.md

  # 5) gh 模式下用内联文本作为发布说明
  scripts/release_publish.sh --tag v0.2.0 --mode gh --notes "修复:xx; 新增:yy"

说明:
  - actions 模式会创建/推送 tag 并触发 .github/workflows/release.yml
  - gh 模式会检测同名 release 是否存在：存在则覆盖上传产物并更新标题/说明
  - 若未指定 --title，则默认使用 --tag 作为标题
USAGE
}

TAG=""
TITLE=""
NOTES=""
MODE="actions"
BUILD="true"

while [[ $# -gt 0 ]]; do
	case "$1" in
		--tag)
			TAG="$2"; shift 2 ;;
		--title)
			TITLE="$2"; shift 2 ;;
		--notes)
			NOTES="$2"; shift 2 ;;
		--mode)
			MODE="$2"; shift 2 ;;
		--build)
			BUILD="$2"; shift 2 ;;
		-h|--help)
			usage; exit 0 ;;
		*)
			echo "未知参数: $1"; usage; exit 1 ;;
	esac
done

if [[ -z "$TAG" ]]; then
	echo "错误: 必须指定 --tag"; usage; exit 1
fi

TITLE=${TITLE:-$TAG}

echo "[release] tag=$TAG mode=$MODE build=$BUILD"

if [[ "$BUILD" == "true" ]]; then
	echo "[release] 开始本地打包..."
	chmod +x scripts/release_package.sh
	VERSION="$TAG" scripts/release_package.sh
fi

if [[ "$MODE" == "actions" ]]; then
	# 推送标签，触发 GitHub Actions 工作流
	if git show-ref --tags --quiet --verify -- "refs/tags/$TAG"; then
		echo "[release] 本地已存在 tag $TAG"
	else
		echo "[release] 创建本地 tag $TAG"
		git tag "$TAG"
	fi

	echo "[release] 推送 tag 到远端以触发 Actions..."
	git push origin "$TAG"
	echo "[release] 已推送。请到 GitHub Actions 查看构建与发布进度。"
	exit 0
fi

# 走 gh CLI 本地直传模式
if ! command -v gh >/dev/null 2>&1; then
	echo "错误: 未检测到 gh CLI，请安装后重试，或使用 --mode actions" >&2
	exit 1
fi

# 构建 notes 参数
NOTES_ARG=( )
if [[ -n "$NOTES" ]]; then
	if [[ -f "$NOTES" ]]; then
		NOTES_ARG=( --notes-file "$NOTES" )
	else
		NOTES_ARG=( --notes "$NOTES" )
	fi
else
	NOTES_ARG=( --notes "自动发布：详见包内 INSTALL.md" )
fi

TARBALL=( dist/video_info-${TAG}.tar.gz )
ZIPBALL=( dist/video_info-${TAG}.zip )
CHECKSUM=( dist/SHA256SUMS )

# 兼容未按 TAG 命名的打包产物（例如 tag 含前缀 v）
if [[ ! -f "${TARBALL[0]}" || ! -f "${ZIPBALL[0]}" ]]; then
	TARBALL=( dist/video_info-*.tar.gz )
	ZIPBALL=( dist/video_info-*.zip )
fi

echo "[release] 使用 gh CLI 发布 release..."
set +e
gh release view "$TAG" >/dev/null 2>&1
exists=$?
set -e

if [[ $exists -eq 0 ]]; then
	echo "[release] 发现已存在的 release，执行更新上传..."
	gh release upload "$TAG" "${TARBALL[@]}" "${ZIPBALL[@]}" "${CHECKSUM[@]}" --clobber
	gh release edit "$TAG" --title "$TITLE" "${NOTES_ARG[@]}"
else
	echo "[release] 创建新的 release..."
	gh release create "$TAG" "${TARBALL[@]}" "${ZIPBALL[@]}" "${CHECKSUM[@]}" --title "$TITLE" "${NOTES_ARG[@]}"
fi

echo "[release] 已完成发布：$TAG"


