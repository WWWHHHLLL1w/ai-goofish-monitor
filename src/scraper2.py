"""Playwright-based scheduled search and new-item notification runner."""
from __future__ import annotations

import asyncio
import json
import os
import random
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

from playwright.async_api import (
    Response,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from src.config import LOGIN_IS_EDGE, RUN_HEADLESS, RUNNING_IN_DOCKER, STATE_FILE
from src.failure_guard import FailureGuard
from src.parsers import _parse_search_results_json
from src.rotation import RotationItem, RotationPool, load_state_files, parse_proxy_pool
from src.services.account_strategy_service import resolve_account_runtime_plan
from src.services.latest_item_notification_service import (
    build_notification_task_key,
    deliver_pending_latest_items,
    enqueue_latest_item,
    is_baseline_complete,
    load_latest_item_keys,
    mark_baseline_complete,
)
from src.services.notification_facade import send_notification_with_retry
from src.services.search_pagination import advance_search_page, is_search_results_response
from src.utils import get_link_unique_key, log_time, random_sleep


class RiskControlError(Exception):
    pass


class LoginRequiredError(Exception):
    """闲鱼将请求重定向到登录流程时抛出。"""


FAILURE_GUARD = FailureGuard()
EDGE_DOCKER_WARNING_PRINTED = False


def _is_login_url(url: str) -> bool:
    lowered = (url or "").lower()
    return "passport.goofish.com" in lowered or "mini_login" in lowered


def _resolve_browser_channel() -> str:
    global EDGE_DOCKER_WARNING_PRINTED
    if RUNNING_IN_DOCKER:
        if LOGIN_IS_EDGE and not EDGE_DOCKER_WARNING_PRINTED:
            print("检测到 LOGIN_IS_EDGE=true，但 Docker 镜像未内置 Edge，将改用 Chromium。")
            EDGE_DOCKER_WARNING_PRINTED = True
        return "chromium"
    return "msedge" if LOGIN_IS_EDGE else "chrome"


def _format_failure_reason(reason: str, limit: int = 500) -> str:
    cleaned = " ".join(str(reason or "未知错误").split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


async def _notify_task_failure(
    task_config: dict, reason: str, *, cookie_path: Optional[str]
) -> None:
    task_name = task_config.get("task_name", "未命名任务")
    keyword = task_config.get("keyword", "")
    formatted_reason = _format_failure_reason(reason)
    pause_immediately = any(
        marker in formatted_reason
        for marker in ("未找到可用的代理地址", "未找到可用的登录状态文件")
    )
    result = FAILURE_GUARD.record_failure(
        task_name,
        formatted_reason,
        cookie_path=cookie_path,
        min_failures_to_pause=1 if pause_immediately else None,
    )
    if not result.get("should_notify"):
        print(
            f"[FailureGuard] 任务 '{task_name}' 失败计数 "
            f"{result.get('consecutive_failures')}/{FAILURE_GUARD.threshold}，暂不通知。"
        )
        return

    paused_until = result.get("paused_until")
    await send_notification_with_retry(
        {
            "商品标题": f"[任务异常] {task_name}",
            "当前售价": "N/A",
            "商品链接": "#",
        },
        f"任务运行失败(连续 {result.get('consecutive_failures')}/{FAILURE_GUARD.threshold} 次): {formatted_reason}\n"
        f"关键词: {keyword or 'N/A'}\n"
        f"已暂停重试到: {paused_until.strftime('%Y-%m-%d %H:%M:%S') if paused_until else 'N/A'}\n"
        "更新登录态/cookies 后将自动恢复。",
    )


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _get_rotation_settings(task_config: dict) -> dict:
    account_cfg = task_config.get("account_rotation") or {}
    proxy_cfg = task_config.get("proxy_rotation") or {}
    return {
        "account_enabled": _as_bool(
            account_cfg.get("enabled"), _as_bool(os.getenv("ACCOUNT_ROTATION_ENABLED"))
        ),
        "account_mode": (account_cfg.get("mode") or os.getenv("ACCOUNT_ROTATION_MODE", "per_task")).lower(),
        "account_state_dir": account_cfg.get("state_dir") or os.getenv("ACCOUNT_STATE_DIR", "state"),
        "account_retry_limit": max(1, _as_int(account_cfg.get("retry_limit"), _as_int(os.getenv("ACCOUNT_ROTATION_RETRY_LIMIT"), 2))),
        "account_blacklist_ttl": max(0, _as_int(account_cfg.get("blacklist_ttl_sec"), _as_int(os.getenv("ACCOUNT_BLACKLIST_TTL"), 300))),
        "proxy_enabled": _as_bool(
            proxy_cfg.get("enabled"), _as_bool(os.getenv("PROXY_ROTATION_ENABLED"))
        ),
        "proxy_mode": (proxy_cfg.get("mode") or os.getenv("PROXY_ROTATION_MODE", "per_task")).lower(),
        "proxy_pool": proxy_cfg.get("proxy_pool") or os.getenv("PROXY_POOL", ""),
        "proxy_retry_limit": max(1, _as_int(proxy_cfg.get("retry_limit"), _as_int(os.getenv("PROXY_ROTATION_RETRY_LIMIT"), 2))),
        "proxy_blacklist_ttl": max(0, _as_int(proxy_cfg.get("blacklist_ttl_sec"), _as_int(os.getenv("PROXY_BLACKLIST_TTL"), 300))),
    }


def _default_context_options() -> dict:
    return {
        "user_agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "permissions": ["geolocation"],
        "geolocation": {"longitude": 121.4737, "latitude": 31.2304},
        "color_scheme": "light",
    }


def _clean_kwargs(options: dict) -> dict:
    return {key: value for key, value in options.items() if value is not None}


def _looks_like_mobile(user_agent: str) -> Optional[bool]:
    if not user_agent:
        return None
    lowered = user_agent.lower()
    return "mobile" in lowered or "android" in lowered or "iphone" in lowered


def _build_context_overrides(snapshot: dict) -> dict:
    overrides = {}
    env = snapshot.get("env") or {}
    headers = snapshot.get("headers") or {}
    page_info = snapshot.get("page") or {}
    storage = snapshot.get("storage") or {}

    user_agent = env.get("userAgent") or env.get("user_agent")
    if user_agent:
        overrides["user_agent"] = user_agent
        is_mobile = _looks_like_mobile(user_agent)
        if is_mobile is not None:
            overrides["is_mobile"] = is_mobile
            overrides["has_touch"] = is_mobile

    viewport = env.get("viewport") or page_info.get("viewport")
    if isinstance(viewport, dict) and viewport.get("width") and viewport.get("height"):
        overrides["viewport"] = {
            "width": int(viewport["width"]),
            "height": int(viewport["height"]),
        }
    scale = env.get("devicePixelRatio") or env.get("device_scale_factor")
    if scale:
        try:
            overrides["device_scale_factor"] = float(scale)
        except (TypeError, ValueError):
            pass
    language = env.get("language") or env.get("locale")
    if language:
        overrides["locale"] = str(language).split(",")[0].split("-")[0] + "-CN"

    timezone = env.get("timezone") or env.get("timezone_id")
    if timezone:
        overrides["timezone_id"] = timezone
    screen = env.get("screen") or {}
    if isinstance(screen, dict):
        width = screen.get("width")
        height = screen.get("height")
        if width and height:
            try:
                overrides["screen"] = {"width": int(width), "height": int(height)}
            except (TypeError, ValueError):
                pass
    if isinstance(storage, dict):
        if storage.get("localStorage") is not None:
            overrides["_captured_local_storage"] = storage["localStorage"]
        if storage.get("sessionStorage") is not None:
            overrides["_captured_session_storage"] = storage["sessionStorage"]
    if isinstance(headers, dict):
        overrides["_captured_headers"] = headers
    return overrides


def _build_extra_headers(raw_headers: Optional[dict]) -> dict:
    if not raw_headers:
        return {}
    excluded = {
        "accept-encoding", "connection", "content-length", "cookie", "host",
        "origin", "referer", "te", "trailer", "transfer-encoding", "upgrade",
        "upgrade-insecure-requests", "user-agent",
    }
    return {
        str(key).strip(): str(value)
        for key, value in raw_headers.items()
        if key
        and str(key).strip().lower() not in excluded
        and not str(key).strip().lower().startswith(("sec-", "proxy-"))
        and value is not None
    }


def _is_session_cookie(cookie: dict) -> bool:
    name = str(cookie.get("name") or "").lower()
    return "cookie" in name or name in {"_m_h5_tk", "_m_h5_tk_enc", "unb", "cookie2"}


def _extract_cookie_values(storage_state: dict) -> dict[str, str]:
    values = {}
    for cookie in storage_state.get("cookies", []):
        if not isinstance(cookie, dict) or not _is_session_cookie(cookie):
            continue
        name, value = str(cookie.get("name") or "").strip(), str(cookie.get("value") or "").strip()
        if name and value:
            values[name] = value
    return values


def _merge_captured_cookies(storage_state: dict, cookies: list[dict]) -> dict:
    merged = json.loads(json.dumps(storage_state))
    by_key = {
        (str(cookie.get("name") or ""), str(cookie.get("domain") or ""), str(cookie.get("path") or "/")): cookie
        for cookie in merged.get("cookies", [])
        if isinstance(cookie, dict)
    }
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        key = (str(cookie.get("name") or ""), str(cookie.get("domain") or ""), str(cookie.get("path") or "/"))
        if all(key):
            by_key[key] = cookie
    merged["cookies"] = list(by_key.values())
    return merged


def _format_cookie_header(cookie_values: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in sorted(cookie_values.items()))


def _format_cookie_header_from_snapshot(snapshot: dict) -> str:
    if not isinstance(snapshot, dict):
        return ""
    raw_header = snapshot.get("cookie") or snapshot.get("cookie_header")
    if isinstance(raw_header, str) and raw_header.strip():
        return raw_header.strip()
    return _format_cookie_header(_extract_cookie_values(snapshot))


def _build_context_overrides_from_snapshot(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict):
        return {}
    if any(key in snapshot for key in ("env", "headers", "page", "storage")):
        return _build_context_overrides(snapshot)
    return {}


def _resolve_snapshot_storage_state(snapshot: dict, state_file: str) -> tuple[dict | str, dict]:
    context_kwargs = _default_context_options()
    if not isinstance(snapshot, dict):
        return state_file, context_kwargs
    if any(key in snapshot for key in ("env", "headers", "page", "storage")):
        cookies = snapshot.get("cookies", [])
        if not isinstance(cookies, list):
            cookies = []
        storage_state = {"cookies": cookies}
        context_kwargs.update(_build_context_overrides(snapshot))
        captured_headers = context_kwargs.pop("_captured_headers", None)
        context_kwargs.pop("_captured_local_storage", None)
        context_kwargs.pop("_captured_session_storage", None)
        headers = _build_extra_headers(captured_headers)
        if headers:
            context_kwargs["extra_http_headers"] = headers
        return storage_state, _clean_kwargs(context_kwargs)
    if "cookies" in snapshot or "origins" in snapshot:
        return snapshot, context_kwargs
    return state_file, context_kwargs


def _get_rotation_settings(task_config: dict) -> dict:
    account_cfg = task_config.get("account_rotation") or {}
    proxy_cfg = task_config.get("proxy_rotation") or {}
    return {
        "account_enabled": _as_bool(
            account_cfg.get("enabled"), _as_bool(os.getenv("ACCOUNT_ROTATION_ENABLED"))
        ),
        "account_mode": str(account_cfg.get("mode") or os.getenv("ACCOUNT_ROTATION_MODE", "per_task")).lower(),
        "account_state_dir": account_cfg.get("state_dir") or os.getenv("ACCOUNT_STATE_DIR", "state"),
        "account_retry_limit": max(1, _as_int(account_cfg.get("retry_limit"), _as_int(os.getenv("ACCOUNT_ROTATION_RETRY_LIMIT"), 2))),
        "account_blacklist_ttl": max(0, _as_int(account_cfg.get("blacklist_ttl_sec"), _as_int(os.getenv("ACCOUNT_BLACKLIST_TTL"), 300))),
        "proxy_enabled": _as_bool(
            proxy_cfg.get("enabled"), _as_bool(os.getenv("PROXY_ROTATION_ENABLED"))
        ),
        "proxy_mode": str(proxy_cfg.get("mode") or os.getenv("PROXY_ROTATION_MODE", "per_task")).lower(),
        "proxy_pool": proxy_cfg.get("proxy_pool") or os.getenv("PROXY_POOL", ""),
        "proxy_retry_limit": max(1, _as_int(proxy_cfg.get("retry_limit"), _as_int(os.getenv("PROXY_ROTATION_RETRY_LIMIT"), 2))),
        "proxy_blacklist_ttl": max(0, _as_int(proxy_cfg.get("blacklist_ttl_sec"), _as_int(os.getenv("PROXY_BLACKLIST_TTL"), 300))),
    }


def _get_task_account_state_file(task_config: dict) -> Optional[str]:
    value = task_config.get("account_state_file")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return STATE_FILE if os.path.exists(STATE_FILE) else None


def _get_rotation_plan(task_config: dict) -> dict:
    settings = _get_rotation_settings(task_config)
    account_items = load_state_files(settings["account_state_dir"])
    account_plan = resolve_account_runtime_plan(
        strategy=task_config.get("account_strategy"),
        account_state_file=task_config.get("account_state_file"),
        has_root_state_file=os.path.exists(STATE_FILE),
        available_account_files=account_items,
    )
    if account_plan["prefer_root_state"]:
        account_items = [STATE_FILE]
        settings["account_enabled"] = False
    elif account_plan["use_account_pool"]:
        settings["account_enabled"] = True
    else:
        settings["account_enabled"] = False

    return {
        "settings": settings,
        "account_items": account_items,
        "forced_account": account_plan["forced_account"],
        "account_pool": RotationPool(
            account_items, settings["account_blacklist_ttl"], "account"
        ),
        "proxy_pool": RotationPool(
            parse_proxy_pool(settings["proxy_pool"]),
            settings["proxy_blacklist_ttl"],
            "proxy",
        ),
    }


def _select_rotation_item(
    pool: RotationPool,
    current: Optional[RotationItem],
    *,
    mode: str,
    enabled: bool,
    force_new: bool = False,
    forced: Optional[str] = None,
) -> Optional[RotationItem]:
    if forced:
        return RotationItem(value=forced)
    if not enabled:
        if current:
            return current
        return RotationItem(value=STATE_FILE) if os.path.exists(STATE_FILE) else None
    if mode == "per_task" and current and not force_new:
        return current
    return pool.pick_random() or current


async def _apply_region_filter(page, region_filter: str):
    trigger = page.get_by_text("区域", exact=True)
    if not await trigger.count():
        raise RuntimeError("未找到区域筛选触发器。")
    await trigger.first.click()
    await random_sleep(1.5, 2)

    popovers = page.locator("div.ant-popover")
    popover = popovers.filter(
        has=page.locator(".areaWrap--FaZHsn8E, [class*='areaWrap']")
    ).last
    if not await popover.count():
        popover = popovers.filter(has=page.get_by_text("重新定位")).last
    if not await popover.count():
        popover = popovers.filter(has=page.get_by_text("查看")).last
    if not await popover.count():
        raise RuntimeError("未找到区域筛选弹窗。")
    await popover.wait_for(state="visible", timeout=5000)

    area_wrap = popover.locator(".areaWrap--FaZHsn8E, [class*='areaWrap']").first
    await area_wrap.wait_for(state="visible", timeout=3000)
    columns = area_wrap.locator(":scope > div")
    region_parts = [part.strip() for part in region_filter.split("/") if part.strip()]
    if not region_parts:
        raise RuntimeError("区域筛选配置为空。")

    async def click_option(column, text_value: str, description: str) -> None:
        option = column.locator(".provItem--QAdOx8nD", has_text=text_value).first
        if not await option.count():
            raise RuntimeError(f"未找到{description} '{text_value}'。")
        await option.click()
        await random_sleep(1.5, 2)

    if len(region_parts) >= 1:
        await click_option(columns.nth(0), region_parts[0], "省份")
    if len(region_parts) >= 2:
        await click_option(columns.nth(1), region_parts[1], "城市")
    if len(region_parts) >= 3:
        await click_option(columns.nth(2), region_parts[2], "区/县")

    search_button = popover.locator("div.searchBtn--Ic6RKcAb").first
    if not await search_button.count():
        raise RuntimeError("未找到区域筛选提交按钮。")
    async with page.expect_response(is_search_results_response, timeout=20000) as response_info:
        await search_button.click()
        await random_sleep(2, 3)
    return await response_info.value


async def _run_scrape_attempt(
    *,
    task_config: dict,
    state_file: str,
    proxy_server: Optional[str],
    notification_task_key: str,
    baseline_complete: bool,
    processed_links: set[str],
    debug_limit: int = 0,
) -> tuple[int, bool]:
    processed_count = 0
    scan_complete = debug_limit <= 0
    has_successful_search_page = False
    stop_scanning = False
    max_pages = max(1, int(task_config.get("max_pages", 1) or 1))
    keyword = str(task_config.get("keyword") or "").strip()
    personal_only = task_config.get("personal_only", False)
    min_price = task_config.get("min_price")
    max_price = task_config.get("max_price")
    free_shipping = task_config.get("free_shipping", False)
    new_publish_option = str(task_config.get("new_publish_option") or "").strip()
    if new_publish_option == "__none__":
        new_publish_option = ""
    region_filter = str(task_config.get("region") or "").strip()

    if not os.path.exists(state_file):
        raise FileNotFoundError(f"登录状态文件不存在: {state_file}")
    try:
        with open(state_file, "r", encoding="utf-8") as handle:
            snapshot_data = json.load(handle)
    except Exception as exc:
        raise RuntimeError(f"读取登录态失败: {exc}") from exc

    storage_state, context_kwargs = _resolve_snapshot_storage_state(snapshot_data, state_file)
    async with async_playwright() as playwright:
        launch_kwargs = {
            "headless": RUN_HEADLESS,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
            "channel": _resolve_browser_channel(),
        }
        if proxy_server:
            launch_kwargs["proxy"] = {"server": proxy_server}

        browser = await playwright.chromium.launch(**launch_kwargs)
        try:
            context = await browser.new_context(storage_state=storage_state, **_clean_kwargs(context_kwargs))
            try:
                await context.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en-US', 'en']});
                    window.chrome = {runtime: {}, loadTimes: function() {}, csi: function() {}};
                    Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 5});
                    const originalQuery = window.navigator.permissions.query;
                    window.navigator.permissions.query = (parameters) => (
                        parameters.name === 'notifications' ?
                            Promise.resolve({state: Notification.permission}) :
                            originalQuery(parameters)
                    );
                    """
                )
                page = await context.new_page()
                if not RUN_HEADLESS:
                    await page.bring_to_front()
                try:
                    log_time("步骤 0 - 访问闲鱼首页...")
                    await page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=30000)
                    await random_sleep(1, 2)
                    await page.evaluate("window.scrollBy(0, Math.random() * 500 + 200)")
                    await random_sleep(1, 2)

                    params = {"q": keyword}
                    search_url = f"https://www.goofish.com/search?{urlencode(params)}"
                    async with page.expect_response(is_search_results_response, timeout=30000) as response_info:
                        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
                    if _is_login_url(page.url):
                        raise LoginRequiredError(f"登录状态失效: {page.url}")
                    initial_response = await response_info.value
                    if not initial_response.ok:
                        raise RuntimeError("初始搜索结果响应无效。")

                    try:
                        await page.wait_for_selector("text=新发布", timeout=15000)
                    except PlaywrightTimeoutError as exc:
                        if _is_login_url(page.url):
                            raise LoginRequiredError(f"登录状态失效: {page.url}") from exc
                        raise RuntimeError("搜索结果页未能正常加载。") from exc
                    await random_sleep(1, 3)

                    baxia_dialog = page.locator("div.baxia-dialog-mask")
                    middleware_widget = page.locator("div.J_MIDDLEWARE_FRAME_WIDGET")
                    for locator, reason in (
                        (baxia_dialog, "baxia-dialog"),
                        (middleware_widget, "J_MIDDLEWARE_FRAME_WIDGET"),
                    ):
                        try:
                            await locator.wait_for(state="visible", timeout=2000)
                        except PlaywrightTimeoutError:
                            continue
                        raise RiskControlError(reason)

                    try:
                        await page.click("div[class*='closeIconBg']", timeout=3000)
                    except PlaywrightTimeoutError:
                        pass

                    final_response = None
                    if new_publish_option:
                        try:
                            await page.click("text=新发布")
                            await random_sleep(1, 2)
                            async with page.expect_response(is_search_results_response, timeout=20000) as response_info:
                                await page.click(f"text={new_publish_option}")
                                await random_sleep(2, 4)
                            final_response = await response_info.value
                        except Exception as exc:
                            raise RuntimeError(f"新发布筛选 '{new_publish_option}' 未能成功应用: {exc}") from exc

                    if personal_only:
                        try:
                            async with page.expect_response(is_search_results_response, timeout=20000) as response_info:
                                await page.click("text=个人闲置")
                                await random_sleep(2, 4)
                            final_response = await response_info.value
                        except Exception as exc:
                            raise RuntimeError(f"个人闲置筛选未能成功应用: {exc}") from exc

                    if free_shipping:
                        try:
                            async with page.expect_response(is_search_results_response, timeout=20000) as response_info:
                                await page.click("text=包邮")
                                await random_sleep(2, 4)
                            final_response = await response_info.value
                        except Exception as exc:
                            raise RuntimeError(f"包邮筛选未能成功应用: {exc}") from exc

                    if region_filter:
                        # Keep the existing region UI automation until it is factored
                        # into a tested helper; a failed configured filter must fail closed.
                        final_response = await _apply_region_filter(page, region_filter)

                    if min_price or max_price:
                        price_container = page.locator('div[class*="search-price-input-container"]').first
                        if not await price_container.is_visible():
                            raise RuntimeError("价格输入区不可用，不能确认价格筛选已应用。")
                        if min_price:
                            await price_container.get_by_placeholder("¥").first.fill(str(min_price))
                        if max_price:
                            await price_container.get_by_placeholder("¥").nth(1).fill(str(max_price))
                        async with page.expect_response(is_search_results_response, timeout=20000) as response_info:
                            await page.keyboard.press("Tab")
                        final_response = await response_info.value

                    current_response = final_response or initial_response
                    if not current_response.ok:
                        raise RuntimeError("已配置搜索筛选返回无效响应。")

                    for page_num in range(1, max_pages + 1):
                        if page_num > 1:
                            advance = await advance_search_page(page=page, page_num=page_num)
                            if not advance.advanced:
                                if advance.stop_reason == "no_next_button":
                                    break
                                scan_complete = False
                                break
                            current_response = advance.response
                        if current_response is None or not current_response.ok:
                            scan_complete = False
                            break

                        payload = await current_response.json()
                        data = payload.get("data") if isinstance(payload, dict) else None
                        raw_items = data.get("resultList") if isinstance(data, dict) else None
                        if not isinstance(raw_items, list):
                            raise RuntimeError(f"第 {page_num} 页搜索结果结构无效。")
                        items = await _parse_search_results_json(payload, f"第 {page_num} 页")
                        if not items:
                            if raw_items:
                                scan_complete = False
                            elif page_num == 1:
                                has_successful_search_page = True
                            break
                        has_successful_search_page = True

                        for item_index, item in enumerate(items, 1):
                            if debug_limit > 0 and processed_count >= debug_limit:
                                scan_complete = False
                                stop_scanning = True
                                break
                            link_key = get_link_unique_key(str(item.get("商品链接") or ""))
                            if not link_key or link_key in processed_links:
                                continue
                            item["获取时间"] = datetime.now().isoformat()
                            title = str(item.get("商品标题") or "新商品")
                            item["通知标题"] = f"🆕 新发布: {title[:30]}{'...' if len(title) > 30 else ''}"
                            queued = enqueue_latest_item(
                                notification_task_key,
                                item,
                                f"符合搜索和筛选条件\n任务: {task_config.get('task_name', '未命名任务')}",
                                notify=baseline_complete,
                            )
                            processed_links.add(link_key)
                            processed_count += 1
                            if baseline_complete:
                                log_time(f"第 {page_num} 页 {item_index}/{len(items)}：新商品已持久化入通知队列 (queued={queued})")
                            else:
                                log_time(f"第 {page_num} 页 {item_index}/{len(items)}：记录首轮基线商品。")
                        if stop_scanning:
                            break
                        if page_num < max_pages:
                            await random_sleep(10, 15)

                    baseline_scan_complete = (
                        not stop_scanning and scan_complete and has_successful_search_page
                    )
                except PlaywrightTimeoutError as exc:
                    if _is_login_url(page.url):
                        raise LoginRequiredError(f"登录状态失效: {page.url}") from exc
                    raise RuntimeError(f"搜索操作超时: {exc}") from exc
                finally:
                    await page.close()
            finally:
                await context.close()
        finally:
            await asyncio.sleep(1)
            await browser.close()

    return processed_count, baseline_scan_complete


async def scrape_xianyu(task_config: dict, debug_limit: int = 0) -> int:
    """抓取符合任务条件的最新商品，并可靠地去重、通知。"""
    notification_task_key = build_notification_task_key(task_config)
    baseline_complete = is_baseline_complete(notification_task_key)
    processed_links = load_latest_item_keys(notification_task_key) if baseline_complete else set()
    rotation = _get_rotation_plan(task_config)
    settings = rotation["settings"]
    account_pool = rotation["account_pool"]
    proxy_pool = rotation["proxy_pool"]
    forced_account = rotation["forced_account"]
    selected_account: Optional[RotationItem] = None
    selected_proxy: Optional[RotationItem] = None

    task_name = task_config.get("task_name", "未命名任务")
    pause_cookie_path = _get_task_account_state_file(task_config)
    decision = FAILURE_GUARD.should_skip_start(task_name, cookie_path=pause_cookie_path)
    if decision.skip:
        if decision.should_notify:
            await send_notification_with_retry(
                {"商品标题": f"[任务暂停] {task_name}", "当前售价": "N/A", "商品链接": "#"},
                f"任务处于暂停状态，将跳过执行。原因: {decision.reason}\n"
                f"连续失败: {decision.consecutive_failures}/{FAILURE_GUARD.threshold}\n"
                f"暂停到: {decision.paused_until.strftime('%Y-%m-%d %H:%M:%S') if decision.paused_until else 'N/A'}",
            )
        return 0

    attempt_limit = max(settings["account_retry_limit"], settings["proxy_retry_limit"], 1)
    last_error = ""
    last_state_path: Optional[str] = None
    processed_count = 0
    completed_scan = False

    for attempt in range(1, attempt_limit + 1):
        if attempt == 1:
            selected_account = _select_rotation_item(
                account_pool, selected_account, mode=settings["account_mode"],
                enabled=settings["account_enabled"], forced=forced_account,
            )
            selected_proxy = _select_rotation_item(
                proxy_pool, selected_proxy, mode=settings["proxy_mode"],
                enabled=settings["proxy_enabled"],
            )
        else:
            if settings["account_enabled"] and settings["account_mode"] == "on_failure":
                account_pool.mark_bad(selected_account, last_error)
                selected_account = _select_rotation_item(
                    account_pool, selected_account, mode=settings["account_mode"],
                    enabled=True, force_new=True, forced=forced_account,
                )
            if settings["proxy_enabled"] and settings["proxy_mode"] == "on_failure":
                proxy_pool.mark_bad(selected_proxy, last_error)
                selected_proxy = _select_rotation_item(
                    proxy_pool, selected_proxy, mode=settings["proxy_mode"],
                    enabled=True, force_new=True,
                )

        if not selected_account:
            last_error = "未找到可用的登录状态文件，无法继续执行任务。"
            break
        if settings["proxy_enabled"] and not selected_proxy:
            last_error = "未找到可用的代理地址，无法继续执行任务。"
            break

        state_path = selected_account.value
        last_state_path = state_path
        proxy_server = selected_proxy.value if selected_proxy else None
        try:
            processed_count, completed_scan = await _run_scrape_attempt(
                task_config=task_config,
                state_file=state_path,
                proxy_server=proxy_server,
                notification_task_key=notification_task_key,
                baseline_complete=baseline_complete,
                processed_links=processed_links,
                debug_limit=debug_limit,
            )
            last_error = ""
            FAILURE_GUARD.record_success(task_name)
            break
        except LoginRequiredError as exc:
            last_error = str(exc)
            break
        except RiskControlError as exc:
            last_error = str(exc)
            break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            print(f"抓取尝试 {attempt}/{attempt_limit} 失败: {last_error}")

    if last_error:
        await _notify_task_failure(task_config, last_error, cookie_path=last_state_path)
        return processed_count

    if not baseline_complete:
        if not completed_scan:
            log_time("首次扫描不完整，不完成基线；下次运行继续建立基线。")
            return processed_count
        mark_baseline_complete(notification_task_key)
        log_time("首次扫描基线已完成，下次运行开始推送新商品。")

    try:
        if baseline_complete or processed_count > 0:
            sent_count = await deliver_pending_latest_items(
                notification_task_key, send_notification_with_retry
            )
            log_time(f"本次成功发送 {sent_count} 条新商品通知。")
    except Exception as exc:
        log_time(f"待发送通知处理失败，队列已保留供后续运行重试: {exc}")
    return processed_count
