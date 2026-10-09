import asyncio

from src.domain.models.task import Task
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.latest_item_notification_service import (
    enqueue_latest_item,
    is_baseline_complete,
    mark_baseline_complete,
)


def test_create_list_update_delete_task(api_client, api_context, sample_task_payload):
    response = api_client.post("/api/tasks/", json=sample_task_payload)
    assert response.status_code == 200
    created = response.json()["task"]
    assert created["task_name"] == sample_task_payload["task_name"]
    assert "decision_mode" not in created
    assert created["next_run_at"] == "2026-03-19T08:15:00+08:00"

    response = api_client.get("/api/tasks")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["keyword"] == sample_task_payload["keyword"]

    response = api_client.patch("/api/tasks/0", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["task"]["enabled"] is False
    assert response.json()["task"]["next_run_at"] is None

    assert api_client.delete("/api/tasks/0").status_code == 200
    assert api_client.get("/api/tasks").json() == []


def test_start_stop_task_updates_status(api_client, api_context, sample_task_payload):
    assert api_client.post("/api/tasks/", json=sample_task_payload).status_code == 200
    assert api_client.post("/api/tasks/start/0").status_code == 200
    assert api_client.get("/api/tasks/0").json()["is_running"] is True
    assert api_client.post("/api/tasks/stop/0").status_code == 200
    assert api_client.get("/api/tasks/0").json()["is_running"] is False
    assert api_context["process_service"].started == [(0, sample_task_payload["task_name"])]
    assert api_context["process_service"].stopped == [0]


def test_create_monitor_task_without_ai_or_keyword_rules(api_client):
    response = api_client.post(
        "/api/tasks/",
        json={"task_name": "Newest camera", "keyword": "camera", "enabled": True},
    )
    assert response.status_code == 200
    task = response.json()["task"]
    assert task["keyword"] == "camera"
    assert "decision_mode" not in task


def test_ai_generation_endpoints_are_removed(api_client):
    assert api_client.post(
        "/api/tasks/generate",
        json={"task_name": "Camera", "keyword": "camera"},
    ).status_code == 405
    assert api_client.get("/api/tasks/generate-jobs/unknown").status_code == 404


def test_delete_task_stops_runtime_and_cleans_notification_state(
    api_client, api_context, sample_task_payload, monkeypatch
):
    monkeypatch.setenv("APP_DATABASE_FILE", str(api_context["db_path"]))
    assert api_client.post("/api/tasks/", json=sample_task_payload).status_code == 200
    assert api_client.post("/api/tasks/start/0").status_code == 200
    enqueue_latest_item("0", {"商品ID": "old", "商品标题": "旧商品"}, "new item")
    mark_baseline_complete("0")

    response = api_client.delete("/api/tasks/0")

    assert response.status_code == 200
    assert is_baseline_complete("0") is False
    with sqlite_connection(str(api_context["db_path"])) as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) AS count FROM latest_item_notifications WHERE task_key = '0'"
        ).fetchone()["count"]
    assert remaining == 0
    assert api_context["process_service"].stopped == [0]


def test_deleted_task_ids_are_not_reused(tmp_path):
    repository = SqliteTaskRepository(db_path=str(tmp_path / "tasks.sqlite3"), legacy_config_file=None)
    first = Task(task_name="A", keyword="camera", enabled=True, max_pages=1, personal_only=False)
    asyncio.run(repository.save(first))
    assert asyncio.run(repository.delete(0))
    second = Task(task_name="B", keyword="camera", enabled=True, max_pages=1, personal_only=False)
    assert asyncio.run(repository.save(second)).id == 1
