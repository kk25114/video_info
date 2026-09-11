#!/usr/bin/env python3
"""在 Windows Chrome 配置中手动登录小红书，并保存后续发布所需的登录态。"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright  # type: ignore


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UPLOAD_URL = "https://creator.xiaohongshu.com/publish/publish"
DEFAULT_STATE_PATH = REPO_ROOT / "xiaohongshu_playwright" / "storage_state.json"
IS_WINDOWS = os.name == "nt"
AUTOMATION_PROFILE_DIR = REPO_ROOT / "xiaohongshu_playwright" / "browser_profile"


def default_chrome_user_data_dir() -> Path:
    """返回当前 Windows Chrome/Edge 的 User Data 根目录。

    登录态保存在浏览器自己的配置中，才能复用用户平时登录的小红书账号。
    在非 Windows 环境或浏览器尚未安装时回退到仓库内的隔离目录，便于测试和
    在 WSL 中运行登录脚本。
    """
    # 仅在 Windows 进程中读取 Windows 环境变量；WSL 中即使映射了
    # LOCALAPPDATA，也不能把 Windows 的用户目录交给 Linux Chrome 使用。
    local_app_data = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    if local_app_data:
        chrome_dir = Path(local_app_data) / "Google" / "Chrome" / "User Data"
        if chrome_dir.is_dir():
            return chrome_dir
        edge_dir = Path(local_app_data) / "Microsoft" / "Edge" / "User Data"
        if edge_dir.is_dir():
            return edge_dir
    return AUTOMATION_PROFILE_DIR


DEFAULT_CHROME_USER_DATA_DIR = default_chrome_user_data_dir()
DEFAULT_CHROME_PROFILE_DIRECTORY = os.environ.get("VIDEO_INFO_CHROME_PROFILE_DIRECTORY", "Default")
# 兼容发布器和外部脚本此前导入的名称。
DEFAULT_PROFILE_DIR = DEFAULT_CHROME_USER_DATA_DIR


def resolve_browser_executable(explicit_path: Optional[str] = None) -> Optional[str]:
    """查找 Windows 常见的 Chrome/Edge 安装位置，也兼容 PATH。"""
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path).expanduser())

    for command in ("chrome.exe", "msedge.exe", "google-chrome", "chromium", "chromium-browser"):
        resolved = shutil.which(command)
        if resolved:
            candidates.append(Path(resolved))

    for base_var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(base_var)
        if not base:
            continue
        candidates.extend(
            [
                Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


async def login(args: argparse.Namespace) -> None:
    browser_path = resolve_browser_executable(args.browser_path)
    if not browser_path:
        raise RuntimeError("未找到 Chrome 或 Edge。请安装浏览器，或用 --browser-path 指定其 exe 路径。")

    profile_dir = Path(args.user_data_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=browser_path,
            headless=args.headless,
            slow_mo=args.slow_mo,
            locale="zh-CN",
            args=[f"--profile-directory={args.profile_directory}"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(args.upload_url, wait_until="domcontentloaded", timeout=60_000)

        print(f"🌐 浏览器: {browser_path}")
        print(f"👤 Chrome 配置：{profile_dir} ({args.profile_directory})")
        if IS_WINDOWS and profile_dir != AUTOMATION_PROFILE_DIR:
            print("⚠️ 若 Chrome 已经打开，请先关闭全部 Chrome 窗口，再运行此脚本。")
        print("请在打开的小红书创作服务平台中完成短信/扫码登录。")
        print("登录成功并确认进入发布页后，回到本终端按回车保存登录态。")
        input("👉 完成登录后按回车继续…")

        state_path = Path(args.state_path).expanduser().resolve()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(state_path))
        print(f"✅ 已保存登录态：{state_path}")
        print(f"✅ 已保存浏览器资料目录：{profile_dir}")
        await context.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="小红书创作服务平台登录态保存工具")
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH), help="保存 storage_state.json 的路径")
    parser.add_argument(
        "--user-data-dir",
        default=str(DEFAULT_CHROME_USER_DATA_DIR),
        help="Chrome/Edge 的 User Data 根目录；默认复用当前 Windows Chrome 配置",
    )
    parser.add_argument(
        "--profile-directory",
        default=DEFAULT_CHROME_PROFILE_DIRECTORY,
        help="User Data 下的配置目录名称，例如 Default 或 Profile 1",
    )
    parser.add_argument("--browser-path", help="Chrome 或 Edge 可执行文件路径")
    parser.add_argument("--upload-url", default=DEFAULT_UPLOAD_URL, help="登录后打开的发布页")
    parser.add_argument("--slow-mo", type=int, default=0, help="浏览器操作减速毫秒数")
    parser.add_argument("--headless", action="store_true", help="无头模式（登录通常不建议启用）")
    return parser


if __name__ == "__main__":
    try:
        asyncio.run(login(build_parser().parse_args()))
    except KeyboardInterrupt:
        print("⚠️ 已取消登录。", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"💥 登录态保存失败：{exc}", file=sys.stderr)
        sys.exit(2)
