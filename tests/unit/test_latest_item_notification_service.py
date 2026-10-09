import asyncio

from src.services.latest_item_notification_service import (
    deliver_pending_latest_items,
    enqueue_latest_item,
    is_baseline_complete,
    mark_baseline_complete,
    build_notification_task_key,
)


def _configure_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))


def test_notification_task_key_preserves_zero_id():
    assert build_notification_task_key({"id": 0, "task_name": "Task", "keyword": "x"}) == "0"


def test_enqueue_latest_item_is_deduplicated_per_task(tmp_path, monkeypatch):
    _configure_database(tmp_path, monkeypatch)
    item = {
        "商品ID": "123",
        "商品标题": "相机",
        "商品链接": "https://www.goofish.com/item?id=123&source=search",
    }

    assert enqueue_latest_item("task-a", item, "新商品") is True
    assert enqueue_latest_item("task-a", item, "新商品") is False
    assert enqueue_latest_item("task-b", item, "新商品") is True


def test_failed_notification_is_retried_and_success_is_not_resent(tmp_path, monkeypatch):
    _configure_database(tmp_path, monkeypatch)
    enqueue_latest_item("task-a", {"商品ID": "123", "商品标题": "相机"}, "新商品")
    calls = []

    async def failed_notifier(payload, reason):
        calls.append(payload)
        return {"ntfy": {"success": False}}

    assert asyncio.run(deliver_pending_latest_items("task-a", failed_notifier)) == 0
    assert len(calls) == 1

    async def successful_notifier(payload, reason):
        calls.append(payload)
        return {"ntfy": {"success": True}}

    assert asyncio.run(deliver_pending_latest_items("task-a", successful_notifier)) == 1
    assert asyncio.run(deliver_pending_latest_items("task-a", successful_notifier)) == 0
    assert len(calls) == 2
    assert calls[-1]["通知标题"].startswith("🆕 新发布:")


def test_retry_limit_does_not_starve_newer_pending_notifications(tmp_path, monkeypatch):
    _configure_database(tmp_path, monkeypatch)
    from src.services.latest_item_notification_service import MAX_PENDING_NOTIFICATIONS_PER_RUN

    for item_id in range(MAX_PENDING_NOTIFICATIONS_PER_RUN + 2):
        enqueue_latest_item("task-a", {"商品ID": str(item_id), "商品标题": f"商品 {item_id}"}, "新商品")

    failed_calls = []

    async def failed_notifier(payload, reason):
        failed_calls.append(payload["商品ID"])
        return {"ntfy": {"success": False}}

    assert asyncio.run(deliver_pending_latest_items("task-a", failed_notifier)) == 0
    assert len(failed_calls) == MAX_PENDING_NOTIFICATIONS_PER_RUN

    succeeded_calls = []

    async def successful_notifier(payload, reason):
        succeeded_calls.append(payload["商品ID"])
        return {"ntfy": {"success": True}}

    assert asyncio.run(deliver_pending_latest_items("task-a", successful_notifier)) == MAX_PENDING_NOTIFICATIONS_PER_RUN
    assert succeeded_calls[:2] == [str(MAX_PENDING_NOTIFICATIONS_PER_RUN), str(MAX_PENDING_NOTIFICATIONS_PER_RUN + 1)]


def test_initial_baseline_records_items_without_queueing_them(tmp_path, monkeypatch):
    _configure_database(tmp_path, monkeypatch)
    item = {"商品ID": "123", "商品标题": "现有商品"}

    assert is_baseline_complete("task-a") is False
    assert enqueue_latest_item("task-a", item, "首轮基线", notify=False) is True
    mark_baseline_complete("task-a")

    assert is_baseline_complete("task-a") is True
    assert enqueue_latest_item("task-a", item, "后续扫描", notify=True) is False

    async def notifier(payload, reason):
        raise AssertionError("baseline items must not be sent")

    assert asyncio.run(deliver_pending_latest_items("task-a", notifier)) == 0
