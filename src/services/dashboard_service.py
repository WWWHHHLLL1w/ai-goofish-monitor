"""
Dashboard 聚合服务
统一汇总任务、结果文件和最近活动，供首页概览使用。
"""
from __future__ import annotations

from typing import Any

from src.domain.models.task import Task
from src.services.dashboard_payloads import (
    build_empty_summary,
    build_task_state_activities,
    normalize_text,
    parse_timestamp,
    serialize_timestamp,
    sort_key_by_activity_time,
    sort_key_by_latest_time,
    summarize_result_file,
)
from src.services.latest_item_notification_service import (
    build_notification_task_key,
    get_latest_item_summary,
)
from src.services.result_storage_service import list_result_filenames

MAX_RECENT_ACTIVITIES = 8


def _build_summary_metrics(tasks: list[Task], summary_list: list[dict[str, Any]], last_updated_at: Any) -> dict[str, Any]:
    return {
        "enabled_tasks": sum(1 for task in tasks if task.enabled),
        "running_tasks": sum(1 for task in tasks if task.is_running),
        "result_files": sum(1 for item in summary_list if item.get("filename")),
        "scanned_items": sum(
            int(item["total_items"])
            for item in summary_list
            if item.get("filename")
        ),
        "discovered_items": sum(int(item.get("discovered_items", 0)) for item in summary_list),

        "last_updated_at": serialize_timestamp(last_updated_at),
    }


async def build_dashboard_snapshot(tasks: list[Task]) -> dict[str, Any]:
    task_lookup = {normalize_text(task.keyword): task for task in tasks}
    task_summaries: dict[str, dict[str, Any]] = {
        task.task_name: build_empty_summary(task) for task in tasks
    }
    recent_activities = build_task_state_activities(tasks)
    latest_updated_at = None

    for filename in await list_result_filenames():
        summary, activities, file_latest_time = await summarize_result_file(filename, task_lookup)
        if summary:
            task_summaries[summary["task_name"]] = summary
        recent_activities.extend(activities)
        if file_latest_time and (latest_updated_at is None or file_latest_time > latest_updated_at):
            latest_updated_at = file_latest_time

    for task in tasks:
        item_summary = get_latest_item_summary(build_notification_task_key(task.model_dump()))
        if not item_summary:
            continue
        payload = item_summary["latest_record"]
        product = payload.get("商品信息", {}) or {}
        task_summary = task_summaries[task.task_name]
        task_summary.update(
            {
                "discovered_items": item_summary["total_items"],
                "latest_crawl_time": serialize_timestamp(
                    parse_timestamp(item_summary["latest_crawl_time"])
                ),
                "latest_item_title": product.get("商品标题"),
                "latest_item_price": product.get("当前售价"),
            }
        )
        recent_activities.append(
            {
                "id": f"task:{task.id}:latest-item",
                "type": "scan",
                "task_name": task.task_name,
                "keyword": task.keyword,
                "title": str(product.get("商品标题") or task.task_name),
                "status": "发现新商品",
                "detail": str(product.get("当前售价") or ""),
                "filename": None,
                "timestamp": item_summary["latest_crawl_time"],
            }
        )
        item_updated_at = parse_timestamp(item_summary["latest_crawl_time"])
        if item_updated_at and (
            latest_updated_at is None
            or item_updated_at.timestamp() > latest_updated_at.timestamp()
        ):
            latest_updated_at = item_updated_at

    summary_list = sorted(task_summaries.values(), key=sort_key_by_latest_time, reverse=True)
    focus_file = next((item["filename"] for item in summary_list if item.get("filename")), None)
    return {
        "summary": _build_summary_metrics(tasks, summary_list, latest_updated_at),
        "task_summaries": summary_list,
        "recent_activities": sorted(
            recent_activities,
            key=sort_key_by_activity_time,
            reverse=True,
        )[:MAX_RECENT_ACTIVITIES],
        "focus_file": focus_file,
    }
