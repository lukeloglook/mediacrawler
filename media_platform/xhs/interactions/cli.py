# -*- coding: utf-8 -*-
# Copyright (c) 2026 MediaCrawler fork contributors
# This file is part of MediaCrawler project.
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1; see LICENSE.
# 声明：本代码仅供学习和研究目的使用，不得用于商业或非法用途。

"""One explicit note and optional reply target per invocation."""

import argparse
import asyncio
import json
from pathlib import Path
import sqlite3

from .audit import Audit
from .models import Interaction, InteractionError
from .browser import run


def parser():
    command = argparse.ArgumentParser(prog="python -m media_platform.xhs.interactions",
                                     description="小红书评论/回复；默认预览，发送须显式 --send。")
    commands = command.add_subparsers(dest="action", required=True)
    for action in ("comment", "reply"):
        item = commands.add_parser(action)
        item.add_argument("--note-url", required=True)
        item.add_argument("--text-file", required=True, type=Path, help="UTF-8 正文文件")
        item.add_argument("--browser-profile", required=True, type=Path)
        if action == "reply":
            item.add_argument("--comment-id", required=True)
        item.add_argument("--send", action="store_true", help="授权发送该条正文一次")
        item.add_argument("--audit-db", type=Path, default=Path("data/interactions/audit.sqlite3"))
        item.add_argument("--screenshot", type=Path, help="保存填写后的页面截图（本地）")
        item.add_argument("--headless", action="store_true")
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    audit = None
    try:
        if args.action == "reply" and not args.comment_id:
            raise InteractionError("回复必须显式提供非空 comment-id。")
        text = args.text_file.read_text(encoding="utf-8-sig")
        interaction = Interaction(args.note_url, text, args.browser_profile,
                                  getattr(args, "comment_id", ""))
        audit = Audit(args.audit_db)
        result = asyncio.run(run(interaction, audit, send=args.send, screenshot=args.screenshot,
                                 headless=args.headless))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["state"] in ("preview", "confirmed") else 2
    except InteractionError as error:
        print(json.dumps({"state": "error", "message": str(error)}, ensure_ascii=False))
        return 2
    except (OSError, UnicodeError, sqlite3.Error):
        print(json.dumps({"state": "error", "message": "正文文件或审计数据库不可读写，请检查路径和权限。"},
                         ensure_ascii=False))
        return 2
    finally:
        if audit is not None:
            audit.close()
