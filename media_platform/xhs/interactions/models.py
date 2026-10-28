# -*- coding: utf-8 -*-
# Copyright (c) 2026 MediaCrawler fork contributors
# This file is part of MediaCrawler project.
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1; see LICENSE.
# 声明：本代码仅供学习和研究目的使用，不得用于商业或非法用途。

"""Explicit Xiaohongshu interaction targets; no credentials or text in audit metadata."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import json
import re
from urllib.parse import urlsplit


class InteractionError(Exception):
    """An error safe to display without exposing browser credentials."""


@dataclass(frozen=True)
class Interaction:
    note_url: str
    text: str
    profile: Path
    comment_id: str = ""

    def __post_init__(self):
        try:
            url = urlsplit(self.note_url)
        except ValueError:
            raise InteractionError("小红书笔记链接格式无效。") from None
        if (url.scheme != "https" or url.hostname != "www.xiaohongshu.com"
                or url.netloc != "www.xiaohongshu.com" or url.fragment
                or not re.fullmatch(r"/(?:explore|discovery/item)/[0-9a-f]{24}", url.path)):
            raise InteractionError("需要完整的小红书笔记 HTTPS 链接（explore 或 discovery/item）。")
        if self.comment_id and not re.fullmatch(r"[0-9a-f]{24}", self.comment_id):
            raise InteractionError("回复目标 comment-id 必须是 24 位小写十六进制 ID。")
        if not self.text.strip() or len(self.text) > 500 or "\x00" in self.text:
            raise InteractionError("评论正文须为 1–500 个字符，不能只有空白或包含 NUL。")
        if not self.profile.is_dir():
            raise InteractionError("browser-profile 必须指向已有的本地登录目录。")

    @property
    def note_id(self):
        return urlsplit(self.note_url).path.rsplit("/", 1)[-1]

    @property
    def metadata(self):
        return {
            "platform": "xhs",
            "note_id": self.note_id,
            "comment_id": self.comment_id,
            "text_sha256": sha256(self.text.encode("utf-8")).hexdigest(),
            "profile_sha256": sha256(str(self.profile.resolve()).casefold().encode()).hexdigest(),
        }

    @property
    def fingerprint(self):
        return sha256(json.dumps(self.metadata, sort_keys=True).encode()).hexdigest()
