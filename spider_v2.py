"""CLI entry point for scheduled Goofish search tasks."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys

from src.config import STATE_FILE
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.scraper2 import scrape_xianyu


def _has_bound_account(tasks: list[dict]) -> bool:
    return any(
        isinstance(task.get("account_state_file"), str)
        and task["account_state_file"].strip()
        for task in tasks
    )


def _has_any_state_file() -> bool:
    state_dir = os.getenv("ACCOUNT_STATE_DIR", "state").strip().strip('"').strip("'")
    return os.path.isdir(state_dir) and any(
        name.endswith(".json") for name in os.listdir(state_dir)
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="闲鱼最新商品监控与通知")
    parser.add_argument("--debug-limit", type=int, default=0, help="仅调试：每个任务最多处理 N 个商品")
    parser.add_argument("--config", type=str, help="可选 JSON 任务配置文件")
    parser.add_argument("--task-name", type=str, help="只执行指定任务")
    args = parser.parse_args()

    if args.config:
        if not os.path.exists(args.config):
            sys.exit(f"错误: 配置文件 '{args.config}' 不存在。")
        try:
            with open(args.config, "r", encoding="utf-8") as handle:
                tasks_config = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            sys.exit(f"错误: 读取或解析配置文件 '{args.config}' 失败: {exc}")
        if not isinstance(tasks_config, list):
            sys.exit("错误: 任务配置必须是 JSON 数组。")
    else:
        tasks_config = [task.model_dump() for task in await SqliteTaskRepository().find_all()]

    if not os.path.exists(STATE_FILE) and not _has_bound_account(tasks_config) and not _has_any_state_file():
        sys.exit("错误: 未找到登录状态文件。请在 state/ 中添加账号或配置任务账号。")

    active_task_configs = [task for task in tasks_config if task.get("enabled", True)]
    if args.task_name:
        active_task_configs = [
            task for task in active_task_configs if task.get("task_name") == args.task_name
        ]
        if not active_task_configs:
            print(f"没有找到启用的任务: {args.task_name}")
            return
    if not active_task_configs:
        print("没有需要执行的任务，程序退出。")
        return

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    tasks = [
        asyncio.create_task(
            scrape_xianyu(task_config=task_config, debug_limit=args.debug_limit)
        )
        for task_config in active_task_configs
    ]

    async def shutdown_watcher() -> None:
        await stop_event.wait()
        print("收到终止信号，正在优雅退出并取消未完成的扫描任务...")
        for task in tasks:
            if not task.done():
                task.cancel()

    shutdown_task = asyncio.create_task(shutdown_watcher())
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        shutdown_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await shutdown_task

    for task_config, result in zip(active_task_configs, results):
        if isinstance(result, Exception):
            print(f"任务 '{task_config.get('task_name', '未命名任务')}' 失败: {result}")
        else:
            print(f"任务 '{task_config.get('task_name', '未命名任务')}' 完成，处理 {result} 个商品。")


if __name__ == "__main__":
    asyncio.run(main())
