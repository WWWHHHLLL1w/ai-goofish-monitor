import asyncio
import sys
from types import SimpleNamespace

from src.services.process_service import ProcessService


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None
        self._done = asyncio.Event()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def finish(self, returncode: int = 0):
        self.returncode = returncode
        self._done.set()

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)


class FakeOutputStream:
    def __init__(self, lines):
        self.lines = iter(lines)

    async def readline(self):
        try:
            return next(self.lines)
        except StopIteration:
            return b""


def test_process_service_marks_task_stopped_when_process_exits(monkeypatch, tmp_path):
    fake_process = FakeProcess(pid=4321)
    events = []

    async def run_scenario():
        service = ProcessService()
        service.failure_guard.should_skip_start = lambda *args, **kwargs: SimpleNamespace(
            skip=False,
            should_notify=False,
            reason="",
            consecutive_failures=0,
            paused_until=None,
        )

        stopped = asyncio.Event()

        async def on_started(task_id: int):
            events.append(("started", task_id))

        async def on_stopped(task_id: int):
            events.append(("stopped", task_id))
            stopped.set()

        service.set_lifecycle_hooks(on_started=on_started, on_stopped=on_stopped)

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return fake_process

        monkeypatch.setattr(
            "src.services.process_service.build_task_log_path",
            lambda task_id, _task_name: str(tmp_path / f"task-{task_id}.log"),
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        started = await service.start_task(0, "task-a")
        assert started is True
        assert events == [("started", 0)]
        assert service.is_running(0) is True

        fake_process.finish(0)
        await asyncio.wait_for(stopped.wait(), timeout=1)

        assert ("stopped", 0) in events
        assert service.is_running(0) is False

    asyncio.run(run_scenario())


def test_process_service_reindexes_runtime_maps_after_delete():
    service = ProcessService()
    proc_a = object()
    proc_c = object()
    watcher_a = object()
    watcher_c = object()

    service.processes = {0: proc_a, 2: proc_c}
    service.log_paths = {0: "a.log", 2: "c.log"}
    service.task_names = {0: "A", 2: "C"}
    watcher_output_a = object()
    watcher_output_c = object()
    service.output_watchers = {0: watcher_output_a, 2: watcher_output_c}
    service.exit_watchers = {0: watcher_a, 2: watcher_c}

    service.reindex_after_delete(1)

    assert service.processes == {0: proc_a, 1: proc_c}
    assert service.log_paths == {0: "a.log", 1: "c.log"}
    assert service.task_names == {0: "A", 1: "C"}
    assert service.output_watchers == {0: watcher_output_a, 1: watcher_output_c}
    assert service.exit_watchers == {0: watcher_a, 1: watcher_c}


def test_process_service_forwards_child_output_to_log_and_console(capsys, tmp_path):
    async def run_scenario():
        service = ProcessService()
        service.task_names[0] = "task-a"
        process = SimpleNamespace(
            stdout=FakeOutputStream([b"first line\n", "中文日志\n"]),
        )
        log_path = tmp_path / "task-a.log"
        with log_path.open("w", encoding="utf-8") as log_handle:
            await service._forward_process_output(0, process, log_handle)

        assert log_path.read_text(encoding="utf-8") == "first line\n中文日志\n"

    asyncio.run(run_scenario())
    captured = capsys.readouterr()
    assert "[爬虫:task-a] first line\n" in captured.out
    assert "[爬虫:task-a] 中文日志\n" in captured.out


def test_process_service_adds_debug_limit_arg_when_env_enabled(monkeypatch):
    monkeypatch.setenv("SPIDER_DEBUG_LIMIT", "1")
    service = ProcessService()

    command = service._build_spawn_command("task-a")

    assert command == [
        sys.executable,
        "-u",
        "spider_v2.py",
        "--task-name",
        "task-a",
        "--debug-limit",
        "1",
    ]


def test_process_service_syncs_dotenv_to_child_environment(monkeypatch):
    service = ProcessService()
    monkeypatch.setenv("RUN_HEADLESS", "true")
    monkeypatch.setattr(
        "src.services.process_service.env_manager.read_env",
        lambda: {"RUN_HEADLESS": "false", "LOGIN_IS_EDGE": "false"},
    )

    child_env = service._build_child_env()

    assert child_env["RUN_HEADLESS"] == "false"
    assert child_env["LOGIN_IS_EDGE"] == "false"
    assert child_env["PYTHONUTF8"] == "1"
