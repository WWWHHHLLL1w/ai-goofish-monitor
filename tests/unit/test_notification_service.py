import asyncio
import base64
import hashlib
import hmac
from urllib.parse import parse_qsl, urlparse

import pytest

from src.infrastructure.external.notification_clients.base import NotificationClient
from src.infrastructure.external.notification_clients.webhook_client import WebhookClient
from src.services.notification_service import NotificationService


class _OkClient(NotificationClient):
    channel_key = "ok"
    display_name = "OK"

    async def send(self, product_data, reason):
        return None


class _FailClient(NotificationClient):
    channel_key = "fail"
    display_name = "FAIL"

    async def send(self, product_data, reason):
        raise RuntimeError("boom")


def test_notification_service_collects_success_and_failure_results():
    service = NotificationService([_OkClient(enabled=True), _FailClient(enabled=True)])

    results = asyncio.run(
        service.send_notification({"商品标题": "Sony A7M4"}, "价格合适")
    )

    assert results["ok"]["success"] is True
    assert results["ok"]["message"] == "发送成功"
    assert results["fail"]["success"] is False
    assert results["fail"]["message"] == "boom"


def test_webhook_client_renders_json_templates(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"errcode": 0, "errmsg": "ok"}

    def _fake_request(session, method, url, **kwargs):
        captured["trust_env"] = session.trust_env
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        captured["json"] = kwargs.get("json")
        captured["data"] = kwargs.get("data")
        captured["timeout"] = kwargs.get("timeout")
        return _FakeResponse()

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setattr("requests.Session.request", _fake_request)

    client = WebhookClient(
        webhook_url="https://hooks.example.com/notify",
        webhook_method="POST",
        webhook_headers='{"Authorization":"Bearer token"}',
        webhook_content_type="JSON",
        webhook_query_parameters='{"task":"{{title}}"}',
        webhook_body='{"message":"{{content}}","link":"{{desktop_link}}"}',
        pcurl_to_mobile=False,
    )

    asyncio.run(
        client.send(
            {
                "商品标题": "Sony A7M4",
                "当前售价": "9999",
                "商品链接": "https://www.goofish.com/item/123",
            },
            "价格合适",
        )
    )

    assert captured["trust_env"] is False
    assert captured["method"] == "POST"
    assert "task=%F0%9F%9A%A8+%E6%96%B0%E6%8E%A8%E8%8D%90%21+Sony+A7M4" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer token"
    assert captured["json"]["message"].startswith("价格: 9999")
    assert captured["json"]["link"] == "https://www.goofish.com/item/123"
    assert captured["data"] is None


def test_dingtalk_text_body_is_upgraded_to_action_card_with_mobile_button(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"errcode": 0, "errmsg": "ok"}

    def _fake_request(session, method, url, **kwargs):
        captured["json"] = kwargs.get("json")
        return _FakeResponse()

    monkeypatch.setattr("requests.Session.request", _fake_request)

    client = WebhookClient(
        webhook_url="https://oapi.dingtalk.com/robot/send",
        webhook_body='{"msgtype":"text","text":{"content":"{{content}}"}}',
        pcurl_to_mobile=True,
    )

    asyncio.run(
        client.send(
            {
                "商品标题": "Sony A7M4 全画幅相机",
                "卖家昵称": "相机卖家",
                "当前售价": "9999",
                "发布时间": "2026-08-28 14:20",
                "获取时间": "2026-08-28T14:47:00",
                "发货地区": "上海",
                "商品链接": "https://www.goofish.com/item?id=123",
                "商品主图链接": "https://img.example.com/item.jpg",
            },
            "价格合适",
        )
    )

    payload = captured["json"]
    action_card = payload["actionCard"]
    markdown = action_card["text"]
    assert payload["msgtype"] == "actionCard"
    assert markdown.startswith("![商品图片](https://img.example.com/item.jpg)")
    assert "- **商品主题：** Sony A7M4 全画幅相机" in markdown
    assert "- **卖家名称：** 相机卖家" in markdown
    assert "- **价格：** 9999" in markdown
    assert "- **发布时间：** 2026-08-28 14:20" in markdown
    assert "- **获取时间：** 2026-08-28 14:47:00" in markdown
    assert "- **地区：** 上海" in markdown
    assert "价格合适" not in markdown
    assert markdown.index("![商品图片]") < markdown.index("- **商品主题：**")
    assert markdown.index("- **商品主题：**") < markdown.index("- **卖家名称：**")
    assert "singleTitle" not in action_card
    assert "singleURL" not in action_card
    assert len(action_card["btns"]) == 1
    assert action_card["btns"][0]["title"] == "查看商品"
    button_url = action_card["btns"][0]["actionURL"]
    assert button_url.startswith("https://pages.goofish.com/sharexy?")
    assert "id%22%3A123" in button_url


def test_webhook_client_reports_api_level_errors(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"errcode": 310000, "errmsg": "关键词校验失败"}

    def _fake_request(session, method, url, **kwargs):
        return _FakeResponse()

    monkeypatch.setattr("requests.Session.request", _fake_request)

    client = WebhookClient(
        webhook_url="https://oapi.dingtalk.com/robot/send",
        webhook_body='{"msgtype":"text","text":{"content":"hello"}}',
        pcurl_to_mobile=False,
    )

    with pytest.raises(RuntimeError, match="errcode=310000"):
        asyncio.run(client.send({"商品标题": "test"}, "reason"))


def test_webhook_client_signs_dingtalk_requests(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"errcode": 0, "errmsg": "ok"}

    def _fake_request(session, method, url, **kwargs):
        captured["url"] = url
        return _FakeResponse()

    monkeypatch.setattr("requests.Session.request", _fake_request)
    monkeypatch.setattr(
        "src.infrastructure.external.notification_clients.webhook_client.time.time",
        lambda: 1700000000.0,
    )

    client = WebhookClient(
        webhook_url="https://oapi.dingtalk.com/robot/send",
        webhook_secret="test-secret",
        webhook_query_parameters='{"access_token":"token"}',
        webhook_body='{"msgtype":"text","text":{"content":"hello"}}',
        pcurl_to_mobile=False,
    )

    asyncio.run(client.send({"商品标题": "test"}, "reason"))

    query = dict(parse_qsl(urlparse(captured["url"]).query))
    timestamp = "1700000000000"
    string_to_sign = f"{timestamp}\ntest-secret"
    expected_sign = base64.b64encode(
        hmac.new(
            b"test-secret",
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")

    assert query["access_token"] == "token"
    assert query["timestamp"] == timestamp
    assert query["sign"] == expected_sign
