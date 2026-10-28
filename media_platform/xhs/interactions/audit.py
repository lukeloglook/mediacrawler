# -*- coding: utf-8 -*-
# Copyright (c) 2026 MediaCrawler fork contributors
# This file is part of MediaCrawler project.
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1; see LICENSE.
# 声明：本代码仅供学习和研究目的使用，不得用于商业或非法用途。

"""Append-only events and durable, atomic single-attempt reservations."""

from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
from uuid import uuid4

from .models import InteractionError


class Audit:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS attempts (
                fingerprint TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                state TEXT NOT NULL,
                details TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS immutable_events_update
            BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'append only'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_events_delete
            BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'append only'); END;
        """)

    def append(self, operation_id, state, details):
        with self.db:
            self._insert(operation_id, state, details)

    def _insert(self, operation_id, state, details):
        self.db.execute(
            "INSERT INTO events(operation_id, occurred_at, state, details) VALUES (?, ?, ?, ?)",
            (operation_id, datetime.now(timezone.utc).isoformat(), state,
             json.dumps(details, ensure_ascii=False)),
        )

    def reserve(self, interaction):
        operation_id = uuid4().hex
        try:
            with self.db:
                self.db.execute("INSERT INTO attempts VALUES (?, ?)",
                                (interaction.fingerprint, operation_id))
                self._insert(operation_id, "submitting", interaction.metadata)
        except sqlite3.IntegrityError:
            raise InteractionError(
                "该登录目录下相同目标与正文已有发送尝试；已拦截重复操作，请先人工核查审计及网页。"
            ) from None
        return operation_id

    def close(self):
        self.db.close()
