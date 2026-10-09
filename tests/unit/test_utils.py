import asyncio

from src.services.latest_item_notification_service import (
    deliver_pending_latest_items,
    enqueue_latest_item,
    load_latest_item_keys,
)
from src.utils import get_link_unique_key, safe_get


def test_safe_get_nested_and_default():
    data = {"a": {"b": [{"c": "value"}]}}
    assert asyncio.run(safe_get(data, "a", "b", 0, "c")) == "value"
    assert asyncio.run(safe_get(data, "a", "b", 1, "c", default="missing")) == "missing"


def test_get_link_unique_key():
    link = "https://www.goofish.com/item?id=123&foo=bar"
    assert get_link_unique_key(link) == "https://www.goofish.com/item?id=123"


def test_new_item_notification_is_persisted_deduped_and_marked_sent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    item = {
        "商品ID": "item-123",
        "商品标题": "新发布的相机",
        "当前售价": "¥10000",
        "商品链接": "https://www.goofish.com/item?id=123&source=search",
    }
    assert enqueue_latest_item("task-1", item, "匹配监控条件") is True
    assert enqueue_latest_item("task-1", item, "匹配监控条件") is False
    assert "item:item-123" in load_latest_item_keys("task-1")
    calls = []

    async def successful_notifier(payload, reason):
        calls.append((payload, reason))
        return {"ntfy": {"success": True}}

    assert asyncio.run(deliver_pending_latest_items("task-1", successful_notifier)) == 1
    assert asyncio.run(deliver_pending_latest_items("task-1", successful_notifier)) == 0
    assert len(calls) == 1
    assert calls[0][0]["商品标题"] == "新发布的相机"
