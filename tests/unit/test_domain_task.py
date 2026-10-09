import pytest

from src.domain.models.task import Task, TaskCreate, TaskUpdate


def test_task_can_start_and_stop():
    task = Task(
        id=1,
        task_name="Sony A7M4",
        enabled=True,
        keyword="sony a7m4",
        max_pages=2,
        personal_only=True,
    )
    assert task.can_start() is True
    assert task.can_stop() is False

    running = task.model_copy(update={"is_running": True})
    assert running.can_start() is False
    assert running.can_stop() is True


def test_task_apply_update():
    task = Task(
        id=1,
        task_name="Sony A7M4",
        enabled=True,
        keyword="sony a7m4",
        max_pages=2,
        personal_only=True,
    )
    updated = task.apply_update(TaskUpdate(enabled=False, max_pages=5))
    assert updated.enabled is False
    assert updated.max_pages == 5
    assert updated.task_name == task.task_name


def test_monitor_task_defaults_need_no_ai_or_keyword_rules():
    task = TaskCreate(task_name="Camera", keyword="camera")
    assert task.task_name == "Camera"
    assert task.keyword == "camera"
    assert task.account_strategy == "auto"


def test_cron_is_validated_and_fixed_account_requires_state_file():
    task = TaskCreate(
        task_name="Camera",
        keyword="camera",
        cron="@daily",
        account_state_file="state/account.json",
    )
    assert task.cron == "0 0 * * *"
    assert task.account_strategy == "fixed"

    with pytest.raises(ValueError, match="固定账号模式下必须选择账号"):
        TaskCreate(
            task_name="Camera",
            keyword="camera",
            account_strategy="fixed",
        )
