"""
持久化最新商品通知队列。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Awaitable, Callable

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection


Notifier = Callable[[dict, str], Awaitable[dict]]
MAX_PENDING_NOTIFICATIONS_PER_RUN = 50


def build_notification_task_key(task_config: dict) -> str:
    task_id = task_config.get("id")
    if task_id is not None:
        return str(task_id)
    return f"{task_config.get('task_name', 'Untitled Task')}::{task_config.get('keyword', '')}"


def _item_key(item: dict) -> str:
    item_id = str(item.get("商品ID") or "").strip()
    if item_id and item_id not in {"未知ID", "未知", "None", "null"}:
        return f"item:{item_id}"
    link = str(item.get("商品链接") or "").strip()
    if link:
        return f"link:{link.split('&', 1)[0]}"
    return ""


def _link_key(item: dict) -> str:
    link = str(item.get("商品链接") or "").strip()
    return link.split("&", 1)[0] if link else ""


def enqueue_latest_item(
    task_key: str,
    item: dict,
    reason: str,
    *,
    notify: bool = True,
) -> bool:
    """记录商品；同任务同商品只入队一次。"""
    item_key = _item_key(item)
    if not task_key or not item_key:
        return False

    now = datetime.now().isoformat()
    payload = dict(item)
    payload.setdefault("获取时间", now)
    title = str(payload.get("商品标题") or "新商品")
    payload.setdefault("通知标题", f"🆕 新发布: {title[:30]}{'...' if len(title) > 30 else ''}")
    status = "pending" if notify else "baseline"
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO latest_item_notifications (
                task_key, item_key, payload_json, reason, status, attempts,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                task_key,
                item_key,
                json.dumps(payload, ensure_ascii=False),
                reason,
                status,
                now,
                now,
            ),
        )
        conn.commit()
        return cursor.rowcount == 1


def get_latest_item_summary(task_key: str) -> dict | None:
    """Return counts and the latest saved item payload for dashboard display."""
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total_items, MAX(created_at) AS latest_crawl_time
            FROM latest_item_notifications
            WHERE task_key = ? AND status != 'baseline'
            """,
            (task_key,),
        ).fetchone()
        latest = conn.execute(
            """
            SELECT payload_json, created_at
            FROM latest_item_notifications
            WHERE task_key = ? AND status != 'baseline'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (task_key,),
        ).fetchone()
    if row is None or int(row["total_items"] or 0) == 0:
        return None
    latest_payload = json.loads(latest["payload_json"]) if latest else {}
    return {
        "total_items": int(row["total_items"]),
        "latest_crawl_time": latest["created_at"] if latest else row["latest_crawl_time"],
        "latest_record": {
            "爬取时间": latest["created_at"] if latest else row["latest_crawl_time"],
            "搜索关键字": "",
            "任务名称": "",
            "商品信息": latest_payload,
        },
    }


def is_baseline_complete(task_key: str) -> bool:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT baseline_complete FROM latest_item_notification_state WHERE task_key = ?",
            (task_key,),
        ).fetchone()
    return bool(row and row["baseline_complete"])


def mark_baseline_complete(task_key: str) -> None:
    now = datetime.now().isoformat()
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO latest_item_notification_state (task_key, baseline_complete, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(task_key) DO UPDATE SET
                baseline_complete = 1, updated_at = excluded.updated_at
            """,
            (task_key, now),
        )
        conn.commit()


def _load_pending(task_key: str) -> list[dict]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, payload_json, reason
            FROM latest_item_notifications
            WHERE task_key = ? AND status = 'pending'
            ORDER BY attempts, updated_at, id
            LIMIT ?
            """,
            (task_key, MAX_PENDING_NOTIFICATIONS_PER_RUN),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "payload": json.loads(row["payload_json"]),
            "reason": row["reason"],
        }
        for row in rows
    ]


def load_latest_item_keys(task_key: str) -> set[str]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT item_key, payload_json FROM latest_item_notifications WHERE task_key = ?",
            (task_key,),
        ).fetchall()
    keys: set[str] = set()
    for row in rows:
        keys.add(str(row["item_key"]))
        try:
            link_key = _link_key(json.loads(row["payload_json"]))
        except (TypeError, json.JSONDecodeError):
            link_key = ""
        if link_key:
            keys.add(link_key)
    return keys


def _record_attempt(notification_id: int, *, sent: bool, error: str = "") -> None:
    now = datetime.now().isoformat()
    with sqlite_connection() as conn:
        conn.execute(
            """
            UPDATE latest_item_notifications
            SET status = ?, attempts = attempts + 1, last_error = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            ("sent" if sent else "pending", error[:1000] or None, now, notification_id),
        )
        conn.commit()


async def deliver_pending_latest_items(task_key: str, notifier: Notifier) -> int:
    """发送待通知商品；保留失败记录，供后续定时运行重试。"""
    sent_count = 0
    for notification in _load_pending(task_key):
        try:
            results = await notifier(notification["payload"], notification["reason"])
            successful = any(
                isinstance(result, dict) and result.get("success") is True
                for result in (results or {}).values()
            )
            if successful:
                _record_attempt(notification["id"], sent=True)
                sent_count += 1
            else:
                _record_attempt(
                    notification["id"],
                    sent=False,
                    error="没有通知渠道确认发送成功",
                )
        except Exception as exc:
            _record_attempt(notification["id"], sent=False, error=str(exc))
    return sent_count
