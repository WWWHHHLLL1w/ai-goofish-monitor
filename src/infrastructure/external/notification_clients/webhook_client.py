"""
通用 Webhook 通知客户端
"""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from src.utils import convert_goofish_link

from .base import NotificationClient, NotificationMessage


class WebhookClient(NotificationClient):
    """通用 Webhook 通知客户端"""

    channel_key = "webhook"
    display_name = "Webhook"

    def __init__(
        self,
        webhook_url: str | None = None,
        webhook_method: str = "POST",
        webhook_headers: str | None = None,
        webhook_content_type: str = "JSON",
        webhook_query_parameters: str | None = None,
        webhook_body: str | None = None,
        pcurl_to_mobile: bool = True,
        webhook_secret: str | None = None,
    ):
        super().__init__(enabled=bool(webhook_url), pcurl_to_mobile=pcurl_to_mobile)
        self.webhook_url = webhook_url
        self.webhook_secret = webhook_secret
        self.webhook_method = (webhook_method or "POST").upper()
        self.webhook_headers = webhook_headers
        self.webhook_content_type = (webhook_content_type or "JSON").upper()
        self.webhook_query_parameters = webhook_query_parameters
        self.webhook_body = webhook_body

    async def send(self, product_data: Dict, reason: str) -> None:
        if not self.is_enabled():
            raise RuntimeError("Webhook 未启用")

        message = self._build_message(product_data, reason)
        print(message)
        headers = self._parse_json(self.webhook_headers, "WEBHOOK_HEADERS", expect_dict=True) or {}
        final_url = self._build_url(message)
        loop = asyncio.get_running_loop()

        if self.webhook_method == "GET":
            await loop.run_in_executor(
                None,
                lambda: self._send_request(
                    "GET",
                    final_url,
                    headers=headers,
                    timeout=15,
                ),
            )
            return

        json_payload, form_payload = self._build_body(message, headers)
        await loop.run_in_executor(
            None,
            lambda: self._send_request(
                "POST",
                final_url,
                headers=headers,
                json=json_payload,
                data=form_payload,
                timeout=15,
            ),
        )

    @staticmethod
    def _send_request(method: str, url: str, **kwargs) -> None:
        """发送不继承进程代理环境的 Webhook 请求。"""
        with requests.Session() as session:
            session.trust_env = False
            response = session.request(method, url, **kwargs)
            response.raise_for_status()
            WebhookClient._raise_for_api_error(response)

    @staticmethod
    def _raise_for_api_error(response: requests.Response) -> None:
        """识别钉钉等接口的 HTTP 200 业务错误。"""
        try:
            payload = response.json()
        except (AttributeError, ValueError):
            return

        if not isinstance(payload, dict):
            return

        error_code = payload.get("errcode")
        if error_code is None or error_code in (0, "0"):
            return

        error_message = payload.get("errmsg") or payload.get("message") or "未知错误"
        raise RuntimeError(
            f"Webhook 接口返回错误: errcode={error_code}, errmsg={error_message}"
        )

    def _build_url(self, message: NotificationMessage) -> str:
        params = self._parse_json(
            self.webhook_query_parameters,
            "WEBHOOK_QUERY_PARAMETERS",
            expect_dict=True,
        ) or {}
        rendered = self._render_template(params, message)
        parsed_url = list(urlparse(self.webhook_url))
        query = dict(parse_qsl(parsed_url[4]))
        query.update(rendered)
        if self.webhook_secret and self._is_dingtalk_url(self.webhook_url):
            timestamp = str(round(time.time() * 1000))
            query["timestamp"] = timestamp
            query["sign"] = self._build_dingtalk_signature(
                timestamp,
                self.webhook_secret,
            )
        parsed_url[4] = urlencode(query)
        return urlunparse(parsed_url)

    @staticmethod
    def _is_dingtalk_url(url: str | None) -> bool:
        hostname = (urlparse(url or "").hostname or "").lower()
        return hostname == "oapi.dingtalk.com"

    @staticmethod
    def _build_dingtalk_signature(timestamp: str, secret: str) -> str:
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    def _build_body(
        self,
        message: NotificationMessage,
        headers: Dict[str, str],
    ) -> tuple[Any | None, Any | None]:
        if not self.webhook_body:
            if self._is_dingtalk_url(self.webhook_url):
                return self._build_dingtalk_markdown_body(message), None
            return None, None

        body_template = self._parse_json(self.webhook_body, "WEBHOOK_BODY")
        if (
            self._is_dingtalk_url(self.webhook_url)
            and isinstance(body_template, dict)
            and str(body_template.get("msgtype", "")).lower() == "text"
        ):
            rendered_body = self._build_dingtalk_markdown_body(
                message,
                at_config=body_template.get("at"),
            )
        else:
            rendered_body = self._render_template(body_template, message)

        if self.webhook_content_type == "JSON":
            if "Content-Type" not in headers and "content-type" not in headers:
                headers["Content-Type"] = "application/json; charset=utf-8"
            return rendered_body, None

        if self.webhook_content_type == "FORM":
            if not isinstance(rendered_body, dict):
                raise ValueError("WEBHOOK_BODY 在 FORM 模式下必须是 JSON 对象")
            if "Content-Type" not in headers and "content-type" not in headers:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            return None, rendered_body

        raise ValueError(f"不支持的 WEBHOOK_CONTENT_TYPE: {self.webhook_content_type}")

    def _build_dingtalk_markdown_body(
        self,
        message: NotificationMessage,
        at_config: Any | None = None,
    ) -> dict[str, Any]:
        """构造正文为 Markdown、底部带按钮的钉钉消息。"""
        title = self._escape_markdown(message.title)
        seller_name = self._escape_markdown(message.seller_name)
        price = self._escape_markdown(message.price)
        publish_time = self._escape_markdown(message.publish_time)
        fetch_time = self._escape_markdown(message.fetch_time)
        region = self._escape_markdown(message.region)
        markdown_lines = []

        image_markdown = self._build_image_markdown(message.image_url)
        if image_markdown:
            markdown_lines.append(image_markdown)

        markdown_lines.extend(
            [
                "",
                f"- **商品主题：** {title}",
                f"- **卖家名称：** {seller_name}",
                f"- **价格：** {price}",
                f"- **发布时间：** {publish_time}",
                f"- **获取时间：** {fetch_time}",
                f"- **地区：** {region}",
            ]
        )

        mobile_link = message.mobile_link
        if not mobile_link and message.desktop_link and message.desktop_link != "#":
            mobile_link = convert_goofish_link(message.desktop_link)

        payload: dict[str, Any] = {
            "msgtype": "actionCard",
            "actionCard": {
                "title": "闲鱼新商品",
                "text": "\n".join(markdown_lines),
                # 使用按钮链接，避免拖选正文时触发整卡片跳转。
                "btns": [
                    {
                        "title": "查看商品",
                        "actionURL": mobile_link or message.desktop_link,
                    }
                ],
            },
        }
        if at_config is not None:
            payload["at"] = self._render_template(at_config, message)
        return payload

    @staticmethod
    def _build_image_markdown(image_url: str | None) -> str:
        image_url = str(image_url or "").strip()
        if not image_url.startswith(("http://", "https://")):
            return ""
        return f"![商品图片]({image_url})"

    @staticmethod
    def _escape_markdown(value: Any) -> str:
        text = str(value or "")
        for character in ("\\", "`", "*", "_"):
            text = text.replace(character, f"\\{character}")
        return text

    def _parse_json(
        self,
        raw_value: str | None,
        field_name: str,
        expect_dict: bool = False,
    ) -> Any | None:
        if not raw_value:
            return None
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} 不是合法 JSON: {exc.msg}") from exc
        if expect_dict and not isinstance(parsed, dict):
            raise ValueError(f"{field_name} 必须是 JSON 对象")
        return parsed

    def _render_template(self, value: Any, message: NotificationMessage) -> Any:
        if isinstance(value, str):
            return self._replace_placeholders(value, message)
        if isinstance(value, list):
            return [self._render_template(item, message) for item in value]
        if isinstance(value, dict):
            return {
                key: self._render_template(item, message)
                for key, item in value.items()
            }
        return value

    def _replace_placeholders(self, value: str, message: NotificationMessage) -> str:
        replacements = {
            "title": message.notification_title,
            "content": message.content,
            "price": message.price,
            "reason": message.reason,
            "desktop_link": message.desktop_link,
            "mobile_link": message.mobile_link or message.desktop_link,
            "image_url": message.image_url or "",
            "image_markdown": self._build_image_markdown(message.image_url),
            "seller_name": message.seller_name,
            "publish_time": message.publish_time,
            "fetch_time": message.fetch_time,
            "region": message.region,
        }
        rendered = value
        for key, replacement in replacements.items():
            rendered = rendered.replace(f"${{{key}}}", replacement)
            rendered = rendered.replace(f"{{{{{key}}}}}", replacement)
        return rendered
