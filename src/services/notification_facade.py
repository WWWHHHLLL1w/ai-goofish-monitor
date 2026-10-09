"""Compatibility helpers for sending notifications without AI dependencies."""
from __future__ import annotations

from src.services.notification_service import build_notification_service


async def send_notification(product_data: dict, reason: str) -> dict:
    service = build_notification_service()
    if not service.clients:
        print("警告：未配置通知渠道。")
        return {}

    results = await service.send_notification(product_data, reason)
    for channel, result in results.items():
        if result["success"]:
            print(f"   -> {channel} 通知发送成功。")
        else:
            print(f"   -> {channel} 通知发送失败: {result['message']}")
    return results


async def send_notification_with_retry(product_data: dict, reason: str) -> dict:
    """Retry transient notifier errors without coupling notification to AI code."""
    last_result = {}
    for attempt in range(3):
        try:
            last_result = await send_notification(product_data, reason)
        except Exception as exc:
            print(f"通知调用异常（第 {attempt + 1}/3 次）: {exc}")
            if attempt < 2:
                import asyncio

                await asyncio.sleep(5)
                continue
            return {}
        if any(result.get("success") for result in last_result.values()):
            return last_result
        if attempt < 2:
            import asyncio

            await asyncio.sleep(5)
    return last_result
