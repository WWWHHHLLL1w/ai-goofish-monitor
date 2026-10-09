"""Task models for scheduled Goofish search and notification."""
from __future__ import annotations

from enum import Enum
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.core.cron_utils import validate_cron_expression
from src.services.account_strategy_service import (
    clean_account_state_file,
    normalize_account_strategy,
)


class TaskStatus(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    SCHEDULED = "scheduled"


def _normalize_optional_string(value):
    if value in {"", "null", "undefined", None}:
        return None
    return value


def _normalize_price_value(value):
    if _normalize_optional_string(value) is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return value


def _normalize_task_payload(payload: Any) -> Any:
    if payload is None or not isinstance(payload, dict):
        return payload
    values = dict(payload)
    values["account_state_file"] = clean_account_state_file(values.get("account_state_file"))
    values["account_strategy"] = normalize_account_strategy(
        values.get("account_strategy"), values.get("account_state_file")
    )
    return values


def _validate_cron(value: Optional[str]) -> Optional[str]:
    return validate_cron_expression(value)


class _TaskFields(BaseModel):
    model_config = ConfigDict(use_enum_values=True, extra="ignore")

    task_name: str
    enabled: bool = True
    keyword: str
    description: Optional[str] = ""
    max_pages: int = 3
    personal_only: bool = True
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    cron: Optional[str] = None
    account_state_file: Optional[str] = None
    account_strategy: Literal["auto", "fixed", "rotate"] = "auto"
    free_shipping: bool = True
    new_publish_option: Optional[str] = None
    region: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_payload(cls, values):
        return _normalize_task_payload(values)

    @field_validator("min_price", "max_price", mode="before")
    @classmethod
    def convert_price_to_str(cls, value):
        return _normalize_price_value(value)

    @field_validator("cron", "new_publish_option", "region", mode="before")
    @classmethod
    def normalize_optional_fields(cls, value):
        return _normalize_optional_string(value)

    @field_validator("account_state_file", mode="before")
    @classmethod
    def normalize_account_file(cls, value):
        return clean_account_state_file(value)

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value):
        return _validate_cron(value)

    @model_validator(mode="after")
    def validate_account_strategy(self):
        if self.account_strategy == "fixed" and not self.account_state_file:
            raise ValueError("固定账号模式下必须选择账号。")
        return self


class Task(_TaskFields):
    id: Optional[int] = None
    is_running: bool = False

    def can_start(self) -> bool:
        return self.enabled and not self.is_running

    def can_stop(self) -> bool:
        return self.is_running

    def apply_update(self, update: "TaskUpdate") -> "Task":
        return self.model_copy(update=update.model_dump(exclude_unset=True))


class TaskCreate(_TaskFields):
    pass


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_name: Optional[str] = None
    enabled: Optional[bool] = None
    keyword: Optional[str] = None
    description: Optional[str] = None
    max_pages: Optional[int] = None
    personal_only: Optional[bool] = None
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    cron: Optional[str] = None
    account_state_file: Optional[str] = None
    account_strategy: Optional[Literal["auto", "fixed", "rotate"]] = None
    free_shipping: Optional[bool] = None
    new_publish_option: Optional[str] = None
    region: Optional[str] = None
    is_running: Optional[bool] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_payload(cls, values):
        return _normalize_task_payload(values)

    @field_validator("min_price", "max_price", mode="before")
    @classmethod
    def convert_price_to_str(cls, value):
        return _normalize_price_value(value)

    @field_validator("cron", "new_publish_option", "region", mode="before")
    @classmethod
    def normalize_optional_fields(cls, value):
        return _normalize_optional_string(value)

    @field_validator("account_state_file", mode="before")
    @classmethod
    def normalize_account_file(cls, value):
        return clean_account_state_file(value)

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value):
        return _validate_cron(value)
