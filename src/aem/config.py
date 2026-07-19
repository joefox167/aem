"""Settings from environment (secrets/deployment) and YAML (rules/behavior)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEM_")

    db_path: str = "/data/aem.db"
    config_file: str = "/config/config.yaml"
    ntfy_url: str = ""
    ntfy_ops_url: str = ""
    gmail_user: str = ""
    gmail_app_password: str = ""
    digest_to: str = ""
    base_url: str = "https://aem.home.arpa"
    scheduler_enabled: bool = True


class QuietHours(BaseModel):
    start: str = "22:00"
    end: str = "08:00"


class CollectorConfig(BaseModel):
    enabled: bool = True


class RuleMatch(BaseModel):
    change_type: str | list[str] | None = None
    kind: str | list[str] | None = None
    format: str | list[str] | None = None
    ticket_status_to: str | list[str] | None = None
    venue: str | list[str] | None = None


class Rule(BaseModel):
    match: RuleMatch = Field(default_factory=RuleMatch)
    action: str = "digest"  # immediate | digest | ignore
    always: bool = False    # bypass quiet hours


DEFAULT_RULES = [
    Rule(match=RuleMatch(change_type="ticket_status", ticket_status_to=["on_sale", "presale"]),
         action="immediate", always=True),
    Rule(match=RuleMatch(change_type="added"), action="immediate"),
    Rule(match=RuleMatch(change_type=["updated", "removed", "ticket_status"]), action="digest"),
]


class AppConfig(BaseModel):
    timezone: str = "America/Chicago"
    poll_minute: int = 7
    digest_hour: int = 8
    removal_threshold: int = 3
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    favorite_venues: list[str] = Field(default_factory=list)
    collectors: dict[str, CollectorConfig] = Field(default_factory=dict)
    rules: list[Rule] = Field(default_factory=lambda: list(DEFAULT_RULES))


def load_app_config(path: str | Path) -> AppConfig:
    p = Path(path)
    if not p.exists():
        return AppConfig()
    data = yaml.safe_load(p.read_text()) or {}
    return AppConfig.model_validate(data)
