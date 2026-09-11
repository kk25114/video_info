#!/usr/bin/env python3
"""通过小红书创作服务平台上传并发布单个视频笔记。

Windows 下默认复用当前 Chrome 的 User Data/Default 配置，因此可直接使用
用户平时已经登录的小红书账号。也可以显式传入隔离的用户目录或 storage state；
如果登录失效或站点要求人机验证，脚本会停止并提示用户手动处理。
"""

from __future__ import annotations

import argparse
import asyncio
import math
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

from playwright.async_api import (  # type: ignore
    Browser,
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from auto_desc import generate_summary_and_topics
from login_and_save_state import (
    DEFAULT_CHROME_PROFILE_DIRECTORY,
    DEFAULT_CHROME_USER_DATA_DIR,
    DEFAULT_STATE_PATH,
    DEFAULT_UPLOAD_URL,
    resolve_browser_executable,
)


LOGIN_PAGE_MARKERS = ("解锁创作者专属功能", "短信登录", "登 录")
PROCESSING_MARKERS = ("上传中", "视频上传中", "视频转码中", "正在转码", "处理中")
UPLOAD_FAILURE_MARKERS = ("上传失败", "转码失败", "上传出错")
UPLOAD_READY_MARKERS = ("上传成功", "上传完成", "替换视频", "修改封面", "添加封面", "重新上传")
SUCCESS_MARKERS = ("发布成功", "笔记发布成功", "发布完成")


def xhs_text_length(value: str) -> int:
    """按小红书网页端的标题计数逻辑计算长度。"""
    units = sum(2 if ord(char) > 127 or char == "^" else 1 for char in value)
    return math.ceil(units / 2)


def trim_xhs_text(value: str, limit: int) -> str:
    """保留完整字符地裁剪到小红书标题计数上限。"""
    kept: list[str] = []
    for char in value.strip():
        candidate = "".join([*kept, char])
        if xhs_text_length(candidate) > limit:
            break
        kept.append(char)
    return "".join(kept).strip()


def trim_plain_text(value: str, limit: int) -> str:
    """按正文字符数裁剪，同时不拆开代理项/换行。"""
    return value.strip()[:limit]


def build_caption(
    video_path: Path,
    title: Optional[str],
    desc: Optional[str],
    auto_desc: bool,
    auto_desc_max_chars: int,
    title_limit: int,
    desc_limit: int,
) -> tuple[str, str]:
    """计算标题与正文；标题不混入话题，正文保留摘要与话题。"""
    summary = ""
    topics = ""
    if auto_desc:
        summary, topics = generate_summary_and_topics(auto_desc_max_chars)
        print(f"📝 自动摘要：{summary}")
        print(f"🏷️ 自动话题：{topics}")

    resolved_title = title or summary or video_path.stem
    resolved_desc = desc or (f"{summary}\n{topics}".strip() if summary else resolved_title)
    return trim_xhs_text(resolved_title, title_limit), trim_plain_text(resolved_desc, desc_limit)


async def visible_text(page: Page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=3_000)
    except Exception:
        return ""


async def ensure_logged_in(page: Page) -> None:
    if "/login" in page.url:
        raise RuntimeError("小红书当前未登录或登录态已失效。请先运行 login_and_save_state.py 重新登录。")
    page_text = await visible_text(page)
    if any(marker in page_text for marker in LOGIN_PAGE_MARKERS):
        raise RuntimeError("小红书当前未登录或登录态已失效。请先运行 login_and_save_state.py 重新登录。")


async def first_visible_locator(page: Page, selectors: Iterable[str], timeout_ms: int = 5_000) -> Locator:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    selector_list = [selector for selector in selectors if selector]
    while asyncio.get_running_loop().time() < deadline:
        for selector in selector_list:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if await candidate.is_visible():
                        return candidate
                except Exception:
                    continue
        await page.wait_for_timeout(250)
    raise PlaywrightTimeoutError(f"未找到可见元素：{selector_list}")


async def video_file_input(page: Page, explicit_selector: Optional[str], timeout_ms: int) -> Locator:
    selectors = [
        explicit_selector or "",
        "input.upload-input[type='file'][accept*='.mp4']",
        "input[type='file'][accept*='.mp4']",
        "input[type='file'][accept*='video']",
    ]
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        for selector in selectors:
            if not selector:
                continue
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    accept = (await candidate.get_attribute("accept") or "").lower()
                    if explicit_selector or any(token in accept for token in (".mp4", "video", ".mov")):
                        return candidate
                except Exception:
                    continue
        await page.wait_for_timeout(300)
    raise PlaywrightTimeoutError("未找到小红书的视频上传控件。")


async def wait_for_upload_ready(page: Page, timeout_ms: int) -> None:
    """等待网页端已完成上传及首帧/转码准备，避免在禁用态点击发布。"""
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    last_text = ""
    while asyncio.get_running_loop().time() < deadline:
        text = await visible_text(page)
        last_text = text
        if any(marker in text for marker in UPLOAD_FAILURE_MARKERS):
            raise RuntimeError("小红书页面报告上传或转码失败，请在浏览器中检查后重试。")
        has_ready_marker = any(marker in text for marker in UPLOAD_READY_MARKERS)
        no_processing = not any(marker in text for marker in PROCESSING_MARKERS)
        if has_ready_marker and no_processing:
            return
        await page.wait_for_timeout(800)

    hint = " ".join(last_text.split())[:300]
    raise PlaywrightTimeoutError(f"等待视频上传/转码完成超时。页面状态：{hint}")


async def fill_input(locator: Locator, value: str, label: str) -> None:
    await locator.click()
    await locator.fill(value)
    await locator.press("Tab")
    readback = await locator.input_value()
    if value and trim_xhs_text(value, 8) not in readback:
        raise RuntimeError(f"{label}填写后未能读回内容，已停止以避免误发布。")
    print(f"✅ 已填写{label}：{value}")


async def fill_description(locator: Locator, value: str) -> None:
    await locator.click()
    await locator.fill(value)
    await locator.press("Tab")
    readback = await locator.inner_text()
    expected = re.sub(r"\s+", "", value)[:8]
    actual = re.sub(r"\s+", "", readback)
    if expected and expected not in actual:
        raise RuntimeError("正文填写后未能读回内容，已停止以避免误发布。")
    print(f"✅ 已填写正文：{value}")


async def find_enabled_publish_button(page: Page, timeout_ms: int, explicit_selector: Optional[str]) -> Locator:
    """仅匹配文案正好为“发布”的启用按钮，避免误点其他发布入口。"""
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        candidates: list[Locator] = []
        if explicit_selector:
            candidates.append(page.locator(explicit_selector))
        candidates.extend(
            [
                page.get_by_role("button", name="发布", exact=True),
                page.locator("xhs-publish-btn button"),
            ]
        )
        for group in candidates:
            count = await group.count()
            for index in range(count):
                candidate = group.nth(index)
                try:
                    if await candidate.is_visible() and await candidate.is_enabled():
                        text = (await candidate.inner_text()).strip()
                        if explicit_selector or text == "发布":
                            return candidate
                except Exception:
                    continue
        await page.wait_for_timeout(700)
    raise PlaywrightTimeoutError("发布按钮未在规定时间内变为可点击状态；请检查必填项、上传状态或页面提示。")


async def wait_for_publish_success(page: Page, original_url: str, timeout_ms: int) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        page_text = await visible_text(page)
        if any(marker in page_text for marker in SUCCESS_MARKERS):
            return
        # 成功后部分账号会跳转离开发布页；登录页不属于成功。
        if page.url != original_url and "/publish/publish" not in page.url and "/login" not in page.url:
            return
        await page.wait_for_timeout(700)
    raise PlaywrightTimeoutError("点击发布后未检测到成功提示或成功跳转，请到浏览器中核对实际状态。")


async def save_state(context: BrowserContext, state_path: Optional[str], enabled: bool, reason: str) -> None:
    if not enabled or not state_path:
        return
    path = Path(state_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(path))
    print(f"🗂️ 已更新登录态（{reason}）：{path}")


async def save_screenshot(page: Page, output_path: Optional[str]) -> None:
    if not output_path:
        return
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(path), full_page=True)
    print(f"🖼️ 已保存截图：{path}")


async def publish(args: argparse.Namespace) -> None:
    video_path = Path(args.video).expanduser().resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"视频文件不存在：{video_path}")

    title, desc = build_caption(
        video_path,
        args.title,
        args.desc,
        args.auto_desc,
        args.auto_desc_max_chars,
        args.title_limit,
        args.desc_limit,
    )
    if not title:
        raise ValueError("标题为空，已停止发布。")
    if not desc:
        raise ValueError("正文为空，已停止发布。")

    browser_path = resolve_browser_executable(args.browser_path)
    if not browser_path:
        raise RuntimeError("未找到 Chrome 或 Edge。请安装浏览器，或通过 --browser-path 指定 exe 路径。")

    state_path = Path(args.state_path).expanduser().resolve() if args.state_path else None
    storage_state = str(state_path) if state_path and state_path.exists() else None
    if state_path and args.save_state and not storage_state:
        print(f"⚠️ 登录态文件不存在：{state_path}")

    async with async_playwright() as playwright:
        browser: Optional[Browser] = None
        context: BrowserContext
        if args.user_data_dir:
            profile_dir = Path(args.user_data_dir).expanduser().resolve()
            profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                context = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    executable_path=browser_path,
                    headless=args.headless,
                    slow_mo=args.slow_mo,
                    locale="zh-CN",
                    args=[f"--profile-directory={args.profile_directory}"] if args.profile_directory else [],
                )
            except Exception as exc:
                raise RuntimeError(
                    f"无法启动 Chrome 配置：{profile_dir} ({args.profile_directory})。"
                    "请关闭全部 Chrome 窗口后重试，或通过 --user-data-dir 指定可用配置。"
                ) from exc
        else:
            browser = await playwright.chromium.launch(
                executable_path=browser_path,
                headless=args.headless,
                slow_mo=args.slow_mo,
            )
            context = await browser.new_context(storage_state=storage_state, locale="zh-CN")

        page = context.pages[0] if context.pages else await context.new_page()
        try:
            print(f"🌐 浏览器：{browser_path}")
            await page.goto(args.upload_url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1_500)
            await ensure_logged_in(page)

            file_input = await video_file_input(page, args.file_input_selector, args.input_timeout * 1000)
            print(f"📤 上传视频：{video_path.name}")
            await file_input.set_input_files(str(video_path))
            await save_state(context, args.state_path, args.save_state, "进入发布页")
            await wait_for_upload_ready(page, args.upload_timeout * 1000)
            print("✅ 视频已上传并完成页面处理。")

            if args.caption_mode in ("both", "title"):
                title_locator = await first_visible_locator(
                    page,
                    [
                        args.title_selector or "",
                        "input[placeholder='填写标题会有更多赞哦']",
                        "input[placeholder*='填写标题']",
                        "textarea[placeholder*='标题']",
                    ],
                    timeout_ms=args.field_timeout * 1000,
                )
                await fill_input(title_locator, title, "标题")

            if args.caption_mode in ("both", "desc"):
                desc_locator = await first_visible_locator(
                    page,
                    [
                        args.desc_selector or "",
                        ".ProseMirror[contenteditable='true']",
                        ".tiptap-container [contenteditable='true']",
                    ],
                    timeout_ms=args.field_timeout * 1000,
                )
                await fill_description(desc_locator, desc)

            if args.no_publish:
                print("ℹ️ --no-publish 已启用：内容已填好，未点击发布。")
                await save_screenshot(page, args.screenshot)
                await save_state(context, args.state_path, args.save_state, "保存未发布编辑内容")
                return

            if args.pause_before_publish:
                input("请在浏览器中检查内容；确认后按回车继续点击发布…")

            publish_button = await find_enabled_publish_button(
                page,
                args.publish_ready_timeout * 1000,
                args.publish_selector,
            )
            original_url = page.url
            await publish_button.click()
            print("🚀 已点击小红书发布按钮，等待结果…")
            await wait_for_publish_success(page, original_url, args.success_timeout * 1000)
            print("🎉 小红书发布成功！")
            await save_screenshot(page, args.screenshot)
            await save_state(context, args.state_path, args.save_state, "发布成功")
        except Exception:
            # 失败现场同样留图，便于在平台页面改版时更新选择器。
            if args.screenshot:
                failure_path = str(Path(args.screenshot).with_name(Path(args.screenshot).stem + "_failed.png"))
                try:
                    await save_screenshot(page, failure_path)
                except Exception:
                    pass
            raise
        finally:
            await context.close()
            if browser is not None:
                await browser.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="小红书创作服务平台自动发布视频笔记")
    parser.add_argument("--video", required=True, help="待上传的视频文件路径")
    parser.add_argument("--title", help="笔记标题；未提供时使用自动摘要或文件名")
    parser.add_argument("--desc", help="笔记正文；未提供时使用自动摘要和话题")
    parser.add_argument("--auto-desc", action="store_true", help="从最新文稿调用 DeepSeek 生成摘要和话题")
    parser.add_argument("--auto-desc-max-chars", type=int, default=50, help="自动摘要最大字符数")
    parser.add_argument("--title-limit", type=int, default=20, help="小红书标题最大字符数")
    parser.add_argument("--desc-limit", type=int, default=1000, help="小红书正文最大字符数")
    parser.add_argument("--caption-mode", choices=("title", "desc", "both"), default="both", help="填写标题/正文的位置")

    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH), help="保存的 Playwright 登录态路径")
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
    parser.add_argument("--save-state", action="store_true", help="进入发布页和发布后回写登录态")
    parser.add_argument("--browser-path", help="Chrome 或 Edge 可执行文件路径")
    parser.add_argument("--upload-url", default=DEFAULT_UPLOAD_URL, help="小红书官方发布页 URL")

    parser.add_argument("--file-input-selector", help="覆盖视频上传 input 选择器")
    parser.add_argument("--title-selector", help="覆盖标题输入框选择器")
    parser.add_argument("--desc-selector", help="覆盖正文输入框选择器")
    parser.add_argument("--publish-selector", help="覆盖发布按钮选择器")
    parser.add_argument("--input-timeout", type=int, default=60, help="等待上传控件超时（秒）")
    parser.add_argument("--upload-timeout", type=int, default=900, help="等待视频上传/转码超时（秒）")
    parser.add_argument("--field-timeout", type=int, default=60, help="等待标题/正文输入框超时（秒）")
    parser.add_argument("--publish-ready-timeout", type=int, default=120, help="等待发布按钮可点击超时（秒）")
    parser.add_argument("--success-timeout", type=int, default=120, help="等待发布成功超时（秒）")

    parser.add_argument("--no-publish", action="store_true", help="只上传并填充内容，不点击发布")
    parser.add_argument("--pause-before-publish", action="store_true", help="点击发布前等待人工确认")
    parser.add_argument("--screenshot", help="保存成功/失败现场截图的路径")
    parser.add_argument("--slow-mo", type=int, default=0, help="浏览器操作减速毫秒数")
    parser.add_argument("--headless", action="store_true", help="无头模式；遇到登录/人机验证时请不要启用")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    try:
        asyncio.run(publish(build_parser().parse_args(argv)))
    except PlaywrightTimeoutError as exc:
        print(f"⏱️ 操作超时：{exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"💥 发布失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
