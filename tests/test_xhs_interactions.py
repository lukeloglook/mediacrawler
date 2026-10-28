# -*- coding: utf-8 -*-
# Copyright (c) 2026 MediaCrawler fork contributors
# This file is part of MediaCrawler project.
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1; see LICENSE.
# 声明：本代码仅供学习和研究目的使用，不得用于商业或非法用途。

import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from media_platform.xhs.interactions.audit import Audit
from media_platform.xhs.interactions.cli import main, parser
from media_platform.xhs.interactions.models import Interaction, InteractionError
from media_platform.xhs.interactions.browser import (
    WriteGuard, confirmed_comment, prepare, run, run_on_page,
)

NOTE = "69169a300000000019025aa0"
TARGET = "6916caae00000000330273be"
CHILD = "69310599000000001d01de7d"
URL = f"https://www.xiaohongshu.com/explore/{NOTE}?xsec_token=local-test"
POST = "https://edith.xiaohongshu.com/api/sns/web/v1/comment/post"


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.item = Interaction(URL, "测试正文", self.root)

    def make_route(self, payload=None, url=POST, method="POST"):
        route = Mock()
        route.continue_ = AsyncMock()
        route.abort = AsyncMock()
        route.request = Mock(url=url, method=method,
                             post_data_json=payload or {"note_id": NOTE, "content": "测试正文"})
        return route

    def response(self, body=None, status=200):
        return Mock(status=status, json=AsyncMock(return_value=body if body is not None else {
            "success": True, "code": 0, "data": {"comment": {"id": CHILD}}
        }))

    def test_invalid_urls_and_ids_fail_closed(self):
        for url in (
            f"http://www.xiaohongshu.com/explore/{NOTE}",
            f"https://www.xiaohongshu.com.evil.com/explore/{NOTE}",
            f"https://user@www.xiaohongshu.com/explore/{NOTE}",
            f"https://www.xiaohongshu.com:443/explore/{NOTE}",
            f"https://www.xiaohongshu.com/explore/{NOTE}#comment",
            "https://www.xiaohongshu.com/explore",
            "https://[invalid/explore",
        ):
            with self.subTest(url=url), self.assertRaises(InteractionError):
                Interaction(url, "正文", self.root)
        for target in ("bad", TARGET + "'", "A" * 24):
            with self.subTest(target=target), self.assertRaises(InteractionError):
                Interaction(URL, "正文", self.root, target)

    def test_text_and_profile_validation(self):
        for text in ("", " \n", "字" * 501, "正文\x00"):
            with self.subTest(text=text[:20]), self.assertRaises(InteractionError):
                Interaction(URL, text, self.root)
        with self.assertRaises(InteractionError):
            Interaction(URL, "正文", self.root / "missing")

    def test_fingerprint_ignores_access_token_but_tracks_target_text_profile(self):
        same = Interaction(URL.replace("local-test", "renewed"), "测试正文", self.root)
        self.assertEqual(self.item.fingerprint, same.fingerprint)
        self.assertNotEqual(self.item.fingerprint,
                            Interaction(URL, "不同正文", self.root).fingerprint)
        self.assertNotEqual(self.item.fingerprint,
                            Interaction(URL, "测试正文", self.root, TARGET).fingerprint)
        self.assertNotIn("local-test", json.dumps(self.item.metadata))
        self.assertNotIn("测试正文", json.dumps(self.item.metadata))

    def test_audit_reservation_is_durable_and_atomic(self):
        path = self.root / "audit.sqlite3"
        first = Audit(path)
        second = Audit(path)
        try:
            operation = first.reserve(self.item)
            with self.assertRaises(InteractionError):
                second.reserve(self.item)
            first.append(operation, "unknown", {"dispatched": True})
            with self.assertRaises(sqlite3.IntegrityError):
                first.db.execute("DELETE FROM events")
            with self.assertRaises(sqlite3.IntegrityError):
                first.db.execute("UPDATE events SET state='confirmed'")
            self.assertEqual(first.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
        finally:
            first.close()
            second.close()
        reopened = Audit(path)
        try:
            with self.assertRaises(InteractionError):
                reopened.reserve(self.item)
        finally:
            reopened.close()

    def test_preview_blocks_all_writes_even_if_armed(self):
        guard = WriteGuard(self.item, False)
        guard.armed = True
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            route = self.make_route(method=method)
            asyncio.run(guard.route(route))
            route.abort.assert_called_once()
            route.continue_.assert_not_called()
        self.assertFalse(guard.dispatched)

    def test_one_matching_send_and_no_early_or_repeated_dispatch(self):
        guard = WriteGuard(self.item, True)
        early = self.make_route()
        asyncio.run(guard.route(early))
        early.abort.assert_called_once()
        guard.armed = True
        first, repeated = self.make_route(), self.make_route()
        asyncio.run(guard.route(first))
        asyncio.run(guard.route(repeated))
        first.continue_.assert_called_once()
        repeated.abort.assert_called_once()
        self.assertTrue(guard.dispatched)

    def test_wrong_note_reply_text_host_or_attachments_are_blocked(self):
        item = Interaction(URL, "测试正文", self.root, TARGET)
        correct = {"note_id": NOTE, "content": "测试正文", "target_comment_id": TARGET}
        variants = (
            {**correct, "note_id": CHILD},
            {**correct, "content": "测试正文 "},
            {**correct, "target_comment_id": CHILD},
            {"note_id": NOTE, "content": "测试正文"},
            {**correct, "images": ["unexpected"]},
            {**correct, "at_users": ["unexpected"]},
        )
        for payload in variants:
            guard = WriteGuard(item, True)
            guard.armed = True
            route = self.make_route(payload)
            asyncio.run(guard.route(route))
            route.abort.assert_called_once()
            self.assertFalse(guard.dispatched)
        guard = WriteGuard(item, True)
        guard.armed = True
        route = self.make_route(correct, POST.replace("edith.", "unknown."))
        asyncio.run(guard.route(route))
        route.abort.assert_called_once()

    def test_response_requires_success_and_comment_id(self):
        self.assertEqual(asyncio.run(confirmed_comment(self.response())), CHILD)
        for body in (
            {"success": False, "code": 0, "data": {"comment": {"id": CHILD}}},
            {"success": True, "code": False, "data": {"comment": {"id": CHILD}}},
            {"success": True, "code": 0, "data": {}},
            {"success": True, "code": 0, "data": {"comment": {"id": "bad"}}},
            {"success": True, "code": 0, "data": None},
        ):
            self.assertIsNone(asyncio.run(confirmed_comment(self.response(body))))
        self.assertIsNone(asyncio.run(confirmed_comment(self.response(status=403))))
        malformed = self.response()
        malformed.json.side_effect = ValueError("bad json")
        self.assertIsNone(asyncio.run(confirmed_comment(malformed)))
        self.assertIsNone(asyncio.run(confirmed_comment(None)))

    def browser_fixture(self, click=None):
        context, page, submit = Mock(), Mock(), Mock()
        page.url = URL
        page.context = context
        context.service_workers = []
        context.new_page = AsyncMock(return_value=page)
        context.route = AsyncMock()
        context.unroute = AsyncMock()
        context.close = AsyncMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        engine = Mock()
        engine.chromium.launch_persistent_context = AsyncMock(return_value=context)
        manager = Mock()
        manager.__aenter__ = AsyncMock(return_value=engine)
        manager.__aexit__ = AsyncMock(return_value=False)
        submit.click = AsyncMock(side_effect=click)
        return manager, context, page, submit

    def test_preview_never_clicks_submit_or_reserves(self):
        manager, context, _, submit = self.browser_fixture()
        audit = Audit(self.root / "preview.sqlite3")
        self.addCleanup(audit.close)
        with patch("playwright.async_api.async_playwright", return_value=manager), \
                patch("media_platform.xhs.interactions.browser.prepare",
                      new=AsyncMock(return_value=submit)):
            result = asyncio.run(run(self.item, audit))
        self.assertEqual(result["state"], "preview")
        submit.click.assert_not_called()
        self.assertEqual(audit.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        context.close.assert_called_once()
        context.unroute.assert_awaited_once()
        page = context.new_page.return_value
        page.remove_listener.assert_called_once()

    def run_simulated_send(self, dispatch, response=None, fail=False):
        manager, context, page, submit = self.browser_fixture()
        audit = Audit(self.root / f"{dispatch}-{fail}.sqlite3")
        self.addCleanup(audit.close)

        async def click(**kwargs):
            handler = context.route.call_args.args[1]
            guard = handler.__self__
            self.assertTrue(guard.armed)
            self.assertEqual(audit.db.execute(
                "SELECT state FROM events ORDER BY sequence LIMIT 1").fetchone()[0], "submitting")
            if dispatch:
                await handler(self.make_route())
                if response:
                    response.request = self.make_route().request
                    guard.on_response(response)
            if fail:
                raise RuntimeError("credentials must never appear in diagnostics")
        submit.click.side_effect = click
        with patch("playwright.async_api.async_playwright", return_value=manager), \
                patch("media_platform.xhs.interactions.browser.prepare",
                      new=AsyncMock(return_value=submit)):
            if fail:
                with self.assertRaises(InteractionError) as caught:
                    asyncio.run(run(self.item, audit, send=True))
                self.assertNotIn("credentials", str(caught.exception))
                result = None
            else:
                result = asyncio.run(run(self.item, audit, send=True))
        context.close.assert_called_once()
        return result, audit

    def test_send_confirmed_unknown_and_not_dispatched(self):
        result, _ = self.run_simulated_send(True, self.response())
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(result["comment_id"], CHILD)
        # A separate audit DB isolates the scenario; the production default is stable.
        self.root = self.root / "unknown"
        self.root.mkdir()
        result, audit = self.run_simulated_send(True)
        self.assertEqual(result["state"], "unknown")
        with self.assertRaises(InteractionError):
            audit.reserve(self.item)
        result, _ = self.run_simulated_send(False)
        self.assertEqual(result["state"], "not_dispatched")

    def test_browser_failure_after_dispatch_stays_unknown(self):
        _, audit = self.run_simulated_send(True, fail=True)
        self.assertEqual(audit.db.execute(
            "SELECT state FROM events ORDER BY sequence DESC LIMIT 1").fetchone()[0], "unknown")

    def test_browser_failure_before_dispatch_keeps_reservation(self):
        _, audit = self.run_simulated_send(False, fail=True)
        self.assertEqual(audit.db.execute(
            "SELECT state FROM events ORDER BY sequence DESC LIMIT 1").fetchone()[0], "not_dispatched")
        with self.assertRaises(InteractionError):
            audit.reserve(self.item)

    def test_borrowed_page_reuses_context_and_removes_only_own_guard(self):
        _, context, page, submit = self.browser_fixture()
        audit = Audit(self.root / "borrowed.sqlite3")
        self.addCleanup(audit.close)
        with patch("media_platform.xhs.interactions.browser.prepare",
                   new=AsyncMock(return_value=submit)):
            result = asyncio.run(run_on_page(page, self.item, audit))
        self.assertEqual(result["state"], "preview")
        context.close.assert_not_called()
        submit.click.assert_not_called()
        handler = context.route.call_args.args[1]
        context.unroute.assert_awaited_once_with("**/*", handler)
        page.remove_listener.assert_called_once()

    def test_service_worker_context_is_rejected_before_navigation_or_send(self):
        _, context, page, _ = self.browser_fixture()
        context.service_workers = [Mock()]
        audit = Audit(self.root / "workers.sqlite3")
        self.addCleanup(audit.close)
        with self.assertRaises(InteractionError):
            asyncio.run(run_on_page(page, self.item, audit, send=True))
        page.goto.assert_not_called()
        context.route.assert_not_called()
        self.assertEqual(audit.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)

    def test_cli_defaults_to_preview_and_requires_reply_target(self):
        args = parser().parse_args(["comment", "--note-url", URL,
                                    "--text-file", "text.txt", "--browser-profile", str(self.root)])
        self.assertFalse(args.send)
        text = self.root / "text.txt"
        text.write_text("测试正文", encoding="utf-8-sig")
        with patch("media_platform.xhs.interactions.cli.run",
                   new=AsyncMock(return_value={"state": "preview"})) as browser, \
                patch("builtins.print"):
            self.assertEqual(main(["reply", "--note-url", URL, "--text-file", str(text),
                                   "--browser-profile", str(self.root), "--comment-id", TARGET,
                                   "--audit-db", str(self.root / "cli.sqlite3")]), 0)
        self.assertEqual(browser.call_args.args[0].comment_id, TARGET)
        self.assertFalse(browser.call_args.kwargs["send"])


class NativeEditorTests(unittest.IsolatedAsyncioTestCase):
    """Real browser DOM fixture; no platform connection or message transmission."""

    async def test_comment_and_nested_reply_targeting(self):
        from playwright.async_api import async_playwright

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = temp.name
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(f"""
                    <div class="note-container">
                      <div class="note-scroller" style="height:300px;overflow:auto">
                        <div id="comment-{TARGET}" class="comment-item">
                          <div class="comment-inner-container"><div class="right">
                            <div class="interactions"><button class="reply"
                              onclick="window.selected='{TARGET}'">回复</button></div>
                          </div></div>
                          <div id="comment-{CHILD}" class="comment-item-sub">
                            <div class="comment-inner-container"><div class="right">
                              <div class="interactions"><button class="reply"
                                onclick="window.selected='{CHILD}'">回复</button></div>
                            </div></div>
                          </div>
                        </div>
                      </div>
                      <div class="content-edit">
                        <p id="content-textarea" contenteditable="true"></p>
                        <div class="inner-when-not-active"
                          onclick="window.expanded=true;this.remove()">评论</div>
                      </div>
                      <button class="btn submit" onclick="window.sent=true">发送</button>
                    </div>
                """)
                profile = Path(directory)
                await prepare(page, Interaction(URL, "一级评论测试", profile))
                self.assertEqual(await page.locator("#content-textarea").inner_text(), "一级评论测试")
                self.assertTrue(await page.evaluate("window.expanded"))
                for target in (TARGET, CHILD):
                    await prepare(page, Interaction(URL, "回复测试", profile, target))
                    self.assertEqual(await page.evaluate("window.selected"), target)
                    self.assertFalse(await page.evaluate("Boolean(window.sent)"))
                await page.locator(f"#comment-{TARGET}").evaluate(
                    "(e) => { e.id='comment-000000000000000000000000'; }")
                with self.assertRaises(InteractionError):
                    await prepare(page, Interaction(URL, "缺失目标", profile, TARGET))
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
