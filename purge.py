from datetime import UTC, datetime
from typing import Any

from cat import BillTheLizard, log
from cat.db.cruds import conversations as crud_conversations
from cat.db.cruds import users as crud_users

try:
    from .settings import PluginSettings
except ImportError:
    from settings import PluginSettings


PURGE_JOB_ID = "cat_widget_users_purge"

SECONDS_PER_DAY = 86400


def _empty_stats() -> dict[str, Any]:
    return {
        "candidates": 0,
        "deleted_users": 0,
        "deleted_conversations": 0,
        "deleted_owners": 0,
        "errors": [],
    }


async def purge_widget_users(
    retention_days: int,
    retention_basis: str,
    agent_ids: list[str],
    user_role: str,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Delete widget users older than ``retention_days`` and cascade their conversations.

    A user is a candidate when its role equals ``user_role`` and its
    ``retention_basis`` timestamp is at least ``retention_days`` old. Rows without
    the timestamp are skipped. Deletion cascades in this order: release the owned
    chats, delete the conversations, then delete the user. Each user is isolated:
    a failure on one is recorded and processing continues with the next.
    """
    now_dt = now or datetime.now(UTC)
    now_ts = now_dt.timestamp()
    retention_seconds = float(retention_days) * SECONDS_PER_DAY

    report: dict[str, Any] = {"agents": {}, "totals": _empty_stats()}

    for agent_id in agent_ids:
        stats = _empty_stats()
        report["agents"][agent_id] = stats

        try:
            users = await crud_users.get_users(
                agent_id, with_password=False, with_timestamps=True
            )
        except Exception as exc:
            stats["errors"].append(f"get_users: {exc}")
            continue

        for user_id, user in (users or {}).items():
            if not isinstance(user, dict) or user.get("role") != user_role:
                continue

            raw_timestamp = user.get(retention_basis)
            if raw_timestamp is None:
                continue
            try:
                timestamp = float(raw_timestamp)
            except (TypeError, ValueError):
                continue

            if now_ts - timestamp < retention_seconds:
                continue

            stats["candidates"] += 1
            if dry_run:
                continue

            try:
                owned_chats = await crud_conversations.get_owned_chats(agent_id, user_id)
                for chat_id in owned_chats:
                    await crud_conversations.release_conversation(agent_id, chat_id)
                    stats["deleted_owners"] += 1

                deleted = await crud_conversations.delete_conversations(agent_id, user_id)
                if isinstance(deleted, int):
                    stats["deleted_conversations"] += deleted

                await crud_users.delete_user(agent_id, user_id)
                stats["deleted_users"] += 1
            except Exception as exc:
                stats["errors"].append(f"{user_id}: {exc}")

    totals = report["totals"]
    for stats in report["agents"].values():
        totals["candidates"] += stats["candidates"]
        totals["deleted_users"] += stats["deleted_users"]
        totals["deleted_conversations"] += stats["deleted_conversations"]
        totals["deleted_owners"] += stats["deleted_owners"]
        totals["errors"].extend(stats["errors"])

    return report


async def purge_widget_users_job() -> None:
    """Scheduled entry point. Must stay module-level so APScheduler can pickle it."""
    lizard = BillTheLizard()
    white_rabbit = getattr(lizard, "white_rabbit", None)
    if white_rabbit is None:
        log.error("[cat_widget_users] white_rabbit is not available; skipping widget user purge")
        return

    if not white_rabbit.acquire_lock(PURGE_JOB_ID):
        return

    try:
        defaults = PluginSettings()
        try:
            settings = await lizard.plugin_manager.get_plugin().load_settings()
        except Exception as exc:
            log.error(f"[cat_widget_users] unable to load settings, using defaults: {exc}")
            settings = {}

        report = await purge_widget_users(
            retention_days=int(settings.get("retention_days", defaults.retention_days)),
            retention_basis=settings.get("retention_basis", defaults.retention_basis),
            agent_ids=list(settings.get("agent_ids", defaults.agent_ids)),
            user_role=settings.get("user_role", defaults.user_role),
        )
        log.info(f"[cat_widget_users] purge completed: {report['totals']}")
    finally:
        white_rabbit.release_lock(PURGE_JOB_ID)
