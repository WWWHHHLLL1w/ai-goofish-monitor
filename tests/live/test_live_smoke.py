from __future__ import annotations

import time
from pathlib import Path

import pytest
import requests

from src.services.latest_item_notification_service import (
    build_notification_task_key,
    load_latest_item_keys,
)

pytestmark = pytest.mark.live
REQUEST_TIMEOUT_SECONDS = 60
TASK_POLL_INTERVAL_SECONDS = 2


def api_request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
    return session.request(method=method, url=url, **kwargs)


def fetch_task(session: requests.Session, base_url: str, task_id: int) -> dict:
    response = api_request(session, "get", f"{base_url}/api/tasks/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


def find_task_log(workspace: Path, task_id: int) -> Path | None:
    matches = sorted((workspace / "logs").glob(f"*_{task_id}.log"))
    return matches[0] if matches else None


def read_task_log(workspace: Path, task_id: int) -> tuple[Path | None, str]:
    path = find_task_log(workspace, task_id)
    if path is None:
        return None, ""
    return path, path.read_text(encoding="utf-8", errors="ignore")


def wait_for_task_running(session, base_url: str, task_id: int, timeout_seconds: int) -> dict:
    deadline = time.monotonic() + timeout_seconds
    latest = {}
    while time.monotonic() < deadline:
        latest = fetch_task(session, base_url, task_id)
        if latest.get("is_running"):
            return latest
        time.sleep(TASK_POLL_INTERVAL_SECONDS)
    pytest.fail(f"任务 {task_id} 未在预期时间内进入运行态: {latest}")


def wait_for_task_completion(session, base_url: str, task_id: int, timeout_seconds: int, workspace: Path) -> dict:
    deadline = time.monotonic() + timeout_seconds
    latest = {}
    while time.monotonic() < deadline:
        latest = fetch_task(session, base_url, task_id)
        if not latest.get("is_running"):
            return latest
        time.sleep(TASK_POLL_INTERVAL_SECONDS)
    path, text = read_task_log(workspace, task_id)
    pytest.fail(f"任务 {task_id} 未结束。log={path}\n{text[-4000:]}")


def delete_task_safely(session: requests.Session, base_url: str, task_id: int) -> None:
    response = api_request(session, "delete", f"{base_url}/api/tasks/{task_id}")
    assert response.status_code in {200, 404}, response.text


def build_live_task_payload(account_state_file: Path, task_name: str, keyword: str) -> dict:
    return {
        "task_name": task_name,
        "enabled": True,
        "keyword": keyword,
        "max_pages": 1,
        "personal_only": True,
        "account_state_file": str(account_state_file),
        "account_strategy": "fixed",
        "new_publish_option": "最新",
    }


def test_live_preflight_smoke(live_server):
    with requests.Session() as session:
        response = api_request(session, "get", f"{live_server.base_url}/health")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "healthy"
        assert live_server.account_state_file.exists()


def test_live_real_traffic_task_smoke(live_server):
    payload = build_live_task_payload(
        live_server.account_state_file,
        live_server.settings.task_name,
        live_server.settings.keyword,
    )
    with requests.Session() as session:
        response = api_request(session, "post", f"{live_server.base_url}/api/tasks/", json=payload)
        assert response.status_code == 200, response.text
        task_id = response.json()["task"]["id"]
        task_key = build_notification_task_key({**payload, "id": task_id})
        try:
            start = api_request(session, "post", f"{live_server.base_url}/api/tasks/start/{task_id}")
            assert start.status_code == 200, start.text
            wait_for_task_running(session, live_server.base_url, task_id, min(live_server.settings.timeout_seconds, 30))
            final_task = wait_for_task_completion(
                session, live_server.base_url, task_id, live_server.settings.timeout_seconds, live_server.workspace
            )
            assert final_task["is_running"] is False
            keys = load_latest_item_keys(task_key)
            assert keys, f"任务未持久化发现任何商品键：{task_key}"
            path, log_text = read_task_log(live_server.workspace, task_id)
            assert path is not None
            assert "已加入通知队列" in log_text or "基线商品" in log_text or "成功发送" in log_text
        finally:
            delete_task_safely(session, live_server.base_url, task_id)
