# -*- coding: utf-8 -*-
# Copyright (c) 2026 MediaCrawler fork contributors
# This file is part of MediaCrawler project.
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1; see LICENSE.
# 声明：本代码仅供学习和研究目的使用，不得用于商业或非法用途。

"""Use the native web editor; authorize exactly one matching comment request."""

import asyncio
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4

from .models import InteractionError

EDITOR = ".note-container p#content-textarea[contenteditable=true]"
SUBMIT = ".note-container button.btn.submit"
COMMENT_ENDPOINT = "/api/sns/web/v1/comment/post"


class WriteGuard:
    def __init__(self, interaction, send):
        self.interaction = interaction
        self.send = send
        self.armed = False
        self.dispatched = False
        self.blocked = False
        self.response = None

    def matches(self, request):
        url = urlsplit(request.url)
        if (request.method != "POST" or url.scheme != "https"
                or url.netloc != "edith.xiaohongshu.com"
                or url.path != COMMENT_ENDPOINT):
            return False
        try:
            payload = request.post_data_json
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        return (
            payload.get("note_id") == self.interaction.note_id
            and payload.get("content") == self.interaction.text
            and payload.get("target_comment_id", "") == self.interaction.comment_id
            and not payload.get("images")
            and not payload.get("at_users")
        )

    async def route(self, route):
        request = route.request
        if request.method in ("GET", "HEAD", "OPTIONS"):
            await route.continue_()
        elif self.send and self.armed and not self.dispatched and self.matches(request):
            # Set before allowing the network request: no second write may escape.
            self.dispatched = True
            await route.continue_()
        else:
            if urlsplit(request.url).path == COMMENT_ENDPOINT:
                self.blocked = True
            await route.abort()

    def on_response(self, response):
        if self.dispatched and self.matches(response.request):
            self.response = response


async def confirmed_comment(response):
    """Only a successful native response containing a comment ID confirms delivery."""
    if response is None:
        return None
    try:
        body = await asyncio.wait_for(response.json(), timeout=15)
        if (response.status == 200 and isinstance(body, dict)
                and body.get("success") is True and type(body.get("code")) is int
                and body["code"] == 0):
            comment = body.get("data", {}).get("comment", {})
            comment_id = comment.get("id")
            if isinstance(comment_id, str) and re.fullmatch(r"[0-9a-f]{24}", comment_id):
                return comment_id
    except Exception:
        pass
    return None


async def prepare(page, interaction):
    from playwright.async_api import TimeoutError as BrowserTimeout

    await page.locator(".note-container").wait_for(state="visible", timeout=25000)
    # The container appears before Vue mounts the editor and comment list.
    editor = page.locator(EDITOR)
    await editor.wait_for(state="visible", timeout=15000)
    if interaction.comment_id:
        try:
            await page.locator(".note-container .comment-item").first.wait_for(
                state="visible", timeout=15000)
        except BrowserTimeout:
            raise InteractionError("笔记评论未加载，已停止回复。") from None
        target = page.locator(f".note-container #comment-{interaction.comment_id}")
        for _ in range(9):
            if await target.count() == 1:
                break
            # Bounded lookup: never reply to a substitute comment or expand indefinitely.
            scroller = page.locator(".note-container .note-scroller")
            if await scroller.count() != 1:
                break
            await scroller.evaluate("(e) => { e.scrollTop += 600; }")
            await page.wait_for_timeout(600)
        if await target.count() != 1:
            raise InteractionError("页面未加载指定评论；请核对 ID。当前不会自动展开折叠的二级评论。")
        await target.scroll_into_view_if_needed(timeout=5000)
        reply = target.locator(
            ":scope > .comment-inner-container > .right .interactions .reply"
        )
        if await reply.count() != 1:
            raise InteractionError("指定评论的回复入口不唯一或不可用。")
        await reply.click(timeout=5000)
    if await editor.count() != 1:
        raise InteractionError("评论输入框不可用，请检查登录、评论权限或页面版本。")
    placeholder = page.locator(
        ".note-container .content-edit > .inner-when-not-active"
    )
    if await placeholder.count() == 1 and await placeholder.is_visible():
        await placeholder.click(timeout=5000)
    else:
        await editor.click(timeout=5000)
    await editor.fill(interaction.text, timeout=10000)
    if await editor.inner_text() != interaction.text:
        raise InteractionError("页面输入内容与正文不一致，已停止。")
    submit = page.locator(SUBMIT)
    await submit.wait_for(state="visible", timeout=5000)
    if await submit.count() != 1 or not await submit.is_enabled():
        raise InteractionError("网页发送按钮不可用，已停止。")
    return submit


async def run(interaction, audit, *, send=False, screenshot: Path | None = None, headless=False):
    """Standalone operation with an explicit persistent login directory."""
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(interaction.profile.resolve()), channel="chrome",
                headless=headless, service_workers="block",
                viewport={"width": 1280, "height": 900},
            )
            try:
                page = await context.new_page()
                return await run_on_page(page, interaction, audit, send=send,
                                         screenshot=screenshot)
            finally:
                await context.close()
    except InteractionError:
        raise
    except Exception:
        raise InteractionError("无法启动或关闭浏览器；请检查 Chrome、登录目录占用及本地环境。"
                               "如有发送尝试，请先核查审计及网页。") from None


async def run_on_page(page, interaction, audit, *, send=False, screenshot: Path | None = None):
    """Reuse a crawler's async Page without owning or closing its browser context.

    The caller must create the context with service_workers='block' and use it
    serially: the temporary write guard applies to the whole supplied context.
    """
    guard = WriteGuard(interaction, send)
    operation_id = uuid4().hex
    reserved = False
    terminal = False
    context = page.context
    if context.service_workers:
        raise InteractionError("该页面上下文含 Service Worker，无法保证请求拦截；请使用禁用它的新上下文。")
    installed = False
    try:
        await context.route("**/*", guard.route)
        installed = True
        page.on("response", guard.on_response)
        await page.goto(interaction.note_url, wait_until="domcontentloaded", timeout=30000)
        if urlsplit(page.url).path != urlsplit(interaction.note_url).path:
            raise InteractionError("笔记页面发生跳转，已停止，请核对链接与登录状态。")
        submit = await prepare(page, interaction)
        if screenshot is not None:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(screenshot), full_page=False)
        if not send:
            audit.append(operation_id, "preview", interaction.metadata)
            terminal = True
            return {"state": "preview", "operation_id": operation_id,
                    "note_id": interaction.note_id, "comment_id": interaction.comment_id}
        operation_id = audit.reserve(interaction)
        reserved = True
        guard.armed = True
        await submit.click(timeout=5000)
        for _ in range(100):
            if guard.response is not None or guard.blocked:
                break
            await page.wait_for_timeout(150)
        comment_id = await confirmed_comment(guard.response)
        state = "confirmed" if comment_id else (
            "unknown" if guard.dispatched else "not_dispatched")
        result = {"state": state, "operation_id": operation_id}
        if comment_id:
            result["comment_id"] = comment_id
        else:
            result["message"] = "未确认发送成功；请先核查审计及网页，同一操作不会自动重试。"
        audit.append(operation_id, state, {"comment_id": comment_id,
                                          "dispatched": guard.dispatched})
        terminal = True
        return result
    except Exception as error:
        if reserved and not terminal:
            audit.append(operation_id, "unknown" if guard.dispatched else "not_dispatched",
                         {"dispatched": guard.dispatched, "reason": "browser_error"})
        elif not reserved and not terminal:
            audit.append(operation_id, "error", {**interaction.metadata,
                                                "reason": "preparation_error"})
        if isinstance(error, InteractionError):
            raise
        raise InteractionError(
            "浏览器操作未完成；请检查 Chrome、登录目录占用、网络和页面状态。"
            "如有发送尝试，请先核查审计和网页，勿直接重试。"
        ) from None
    finally:
        if installed:
            page.remove_listener("response", guard.on_response)
            await context.unroute("**/*", guard.route)
