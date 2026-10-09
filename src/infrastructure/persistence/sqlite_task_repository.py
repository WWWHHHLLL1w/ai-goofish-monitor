"""
基于 SQLite 的任务仓储实现。
"""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional

from src.domain.models.task import Task
from src.domain.repositories.task_repository import TaskRepository
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection


def _row_to_task(row) -> Task:
    payload = dict(row)
    payload["enabled"] = bool(payload["enabled"])
    payload["personal_only"] = bool(payload["personal_only"])
    payload["free_shipping"] = bool(payload["free_shipping"])
    payload["is_running"] = bool(payload["is_running"])
    payload["description"] = payload.get("description") or ""
    payload["account_strategy"] = payload.get("account_strategy") or "auto"
    return Task(**payload)


def find_task_by_name_sync(task_name: str) -> Task | None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_name = ? ORDER BY id ASC LIMIT 1",
            (task_name,),
        ).fetchone()
    return _row_to_task(row) if row else None


class SqliteTaskRepository(TaskRepository):
    """基于 SQLite 的任务仓储"""

    def __init__(
        self,
        db_path: str | None = None,
        legacy_config_file: str | None = "config.json",
    ):
        self.db_path = db_path
        self.legacy_config_file = legacy_config_file

    async def find_all(self) -> List[Task]:
        return await asyncio.to_thread(self._find_all_sync)

    async def find_by_id(self, task_id: int) -> Optional[Task]:
        return await asyncio.to_thread(self._find_by_id_sync, task_id)

    async def save(self, task: Task) -> Task:
        return await asyncio.to_thread(self._save_sync, task)

    async def delete(self, task_id: int) -> bool:
        return await asyncio.to_thread(self._delete_sync, task_id)

    def _find_all_sync(self) -> List[Task]:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY id ASC").fetchall()
        return [_row_to_task(row) for row in rows]

    def _find_by_id_sync(self, task_id: int) -> Optional[Task]:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def _save_sync(self, task: Task) -> Task:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            task_id = task.id
            if task_id is None:
                task_id = self._next_task_id(conn)
            payload = self._task_values(task.model_copy(update={"id": task_id}))
            conn.execute(
                """
                INSERT OR REPLACE INTO tasks (
                    id, task_name, enabled, keyword, description, analyze_images,
                    max_pages, personal_only, min_price, max_price, cron,
                    ai_prompt_base_file, ai_prompt_criteria_file, account_state_file,
                    account_strategy, free_shipping, new_publish_option, region,
                    decision_mode, keyword_rules_json, is_running
                ) VALUES (
                    :id, :task_name, :enabled, :keyword, :description, :analyze_images,
                    :max_pages, :personal_only, :min_price, :max_price, :cron,
                    :ai_prompt_base_file, :ai_prompt_criteria_file, :account_state_file,
                    :account_strategy, :free_shipping, :new_publish_option, :region,
                    :decision_mode, :keyword_rules_json, :is_running
                )
                """,
                payload,
            )
            conn.commit()
        return task.model_copy(update={"id": task_id})

    def _delete_sync(self, task_id: int) -> bool:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            task_row = conn.execute(
                "SELECT task_name, keyword FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            cursor = conn.execute(
                "DELETE FROM tasks WHERE id = ?",
                (task_id,),
            )
            if cursor.rowcount:
                task_keys = {str(task_id)}
                if task_row is not None:
                    task_keys.update(
                        {
                            f"{task_id}::{task_row['keyword']}",
                            f"{task_row['task_name']}::{task_row['keyword']}",
                        }
                    )
                for task_key in task_keys:
                    conn.execute(
                        "DELETE FROM latest_item_notifications WHERE task_key = ?",
                        (task_key,),
                    )
                    conn.execute(
                        "DELETE FROM latest_item_notification_state WHERE task_key = ?",
                        (task_key,),
                    )
            conn.commit()
        return cursor.rowcount > 0

    def _next_task_id(self, conn) -> int:
        row = conn.execute("SELECT COALESCE(MAX(id), -1) AS max_id FROM tasks").fetchone()
        max_task_id = int(row["max_id"])
        sequence_row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'task_id_sequence'"
        ).fetchone()
        last_allocated_id = int(sequence_row["seq"]) if sequence_row else -1
        next_task_id = max(max_task_id, last_allocated_id) + 1
        conn.execute("INSERT INTO task_id_sequence (id) VALUES (?)", (next_task_id,))
        return next_task_id

    def _task_values(self, task: Task) -> dict:
        values = task.model_dump()
        values.update(
            {
                "description": "",
                "analyze_images": 0,
                "ai_prompt_base_file": "",
                "ai_prompt_criteria_file": "",
                "decision_mode": "notify",
                "keyword_rules_json": "[]",
            }
        )
        values["enabled"] = int(task.enabled)
        values["personal_only"] = int(task.personal_only)
        values["free_shipping"] = int(task.free_shipping)
        values["is_running"] = int(task.is_running)
        return values
