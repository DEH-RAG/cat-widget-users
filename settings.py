from typing import Literal

from pydantic import BaseModel

from cat import plugin


class PluginSettings(BaseModel):
    retention_days: int = 7
    retention_basis: Literal["created_at", "updated_at"] = "created_at"
    agent_ids: list[str] = ["__widget_django__"]
    user_role: str = "anonym-user"
    cleanup_every_hours: int = 24


@plugin
def settings_schema():
    return PluginSettings.model_json_schema()
