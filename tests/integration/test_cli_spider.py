import asyncio
import importlib
import json
import sys


def test_cli_runs_selected_enabled_task_without_reading_prompts(tmp_path, monkeypatch):
    import spider_v2

    importlib.reload(spider_v2)
    config_path = tmp_path / "tasks.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "task_name": "Camera alert",
                    "keyword": "camera",
                    "enabled": True,
                    "ai_prompt_base_file": "missing.txt",
                    "decision_mode": "ai",
                },
                {"task_name": "Disabled", "keyword": "other", "enabled": False},
            ]
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(spider_v2, "STATE_FILE", str(state_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["spider_v2.py", "--config", str(config_path), "--task-name", "Camera alert"],
    )
    called = []

    async def fake_scrape_xianyu(task_config, debug_limit=0):
        called.append((task_config, debug_limit))
        return 1

    monkeypatch.setattr(spider_v2, "scrape_xianyu", fake_scrape_xianyu)
    asyncio.run(spider_v2.main())

    assert len(called) == 1
    assert called[0][0]["task_name"] == "Camera alert"
    assert "ai_prompt_text" not in called[0][0]


def test_cli_rejects_non_array_json_tasks(tmp_path, monkeypatch):
    import spider_v2

    config_path = tmp_path / "tasks.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", "--config", str(config_path)])
    try:
        asyncio.run(spider_v2.main())
    except SystemExit as exc:
        assert "JSON 数组" in str(exc)
        return
    raise AssertionError("non-array JSON task configs must be rejected")
