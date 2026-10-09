"""
通知客户端基类
定义通知客户端的统一接口
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Dict

from src.utils import convert_goofish_link


def _format_notification_time(value: object, default: str = "未知") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        return text


@dataclass(frozen=True)
class NotificationMessage:
    title: str
    price: str
    reason: str
    desktop_link: str
    mobile_link: str | None
    notification_title: str
    content: str
    image_url: str | None
    seller_name: str
    publish_time: str
    fetch_time: str
    region: str


class NotificationClient(ABC):
    """通知客户端抽象基类"""

    channel_key = "unknown"
    display_name = "未知渠道"

    def __init__(self, enabled: bool = False, pcurl_to_mobile: bool = True):
        self._enabled = enabled
        self._pcurl_to_mobile = pcurl_to_mobile

    def is_enabled(self) -> bool:
        """检查客户端是否启用"""
        return self._enabled

    @abstractmethod
    async def send(self, product_data: Dict, reason: str) -> bool:
        """
        发送通知

        Args:
            product_data: 商品数据
            reason: 推荐原因

        Returns:
            是否发送成功
        """
        raise NotImplementedError

    def _build_message(self, product_data: Dict, reason: str) -> NotificationMessage:
        """格式化消息内容"""
        title = str(product_data.get('商品标题') or 'N/A')
        price = str(product_data.get('当前售价') or 'N/A')
        desktop_link = str(product_data.get('商品链接') or '#')
        mobile_link = None

        if self._pcurl_to_mobile and desktop_link and desktop_link != "#":
            mobile_link = convert_goofish_link(desktop_link)

        if product_data.get("通知标题"):
            content_lines = [
                f"价格: {price}",
                f"地区: {str(product_data.get('发货地区') or '未知').strip()}",
                f"发布时间: {str(product_data.get('发布时间') or '未知').strip()}",
                f"发现时间: {_format_notification_time(product_data.get('获取时间'))}",
                f"详情: {reason}",
            ]
        else:
            content_lines = [
                f"价格: {price}",
                f"原因: {reason}",
            ]
        if mobile_link:
            content_lines.append(f"手机端链接: {mobile_link}")
            content_lines.append(f"电脑端链接: {desktop_link}")
        else:
            content_lines.append(f"链接: {desktop_link}")

        short_title = title[:30]
        suffix = "..." if len(title) > 30 else ""
        notification_title = str(
            product_data.get("通知标题") or f"🚨 新推荐! {short_title}{suffix}"
        )

        main_image = product_data.get('商品主图链接')
        if not main_image:
            image_list = product_data.get('商品图片列表', [])
            if image_list:
                main_image = image_list[0]

        seller_name = str(product_data.get("卖家昵称") or "未知").strip()
        publish_time = str(product_data.get("发布时间") or "未知").strip()
        fetch_time = _format_notification_time(
            product_data.get("获取时间")
            or product_data.get("爬取时间")
            or datetime.now().isoformat()
        )
        region = str(product_data.get("发货地区") or "未知").strip()

        return NotificationMessage(
            title=title,
            price=price,
            reason=reason,
            desktop_link=desktop_link,
            mobile_link=mobile_link,
            notification_title=notification_title,
            content="\n".join(content_lines),
            image_url=main_image,
            seller_name=seller_name,
            publish_time=publish_time,
            fetch_time=fetch_time,
            region=region,
        )
