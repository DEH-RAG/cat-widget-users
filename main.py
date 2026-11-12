from datetime import UTC, datetime

from cat import BillTheLizard, hook, log

try:
    from .purge import PURGE_JOB_ID, purge_widget_users_job
    from .settings import PluginSettings
except ImportError:
    from purge import PURGE_JOB_ID, purge_widget_users_job
    from settings import PluginSettings


@hook(priority=-100)
async def after_lizard_bootstrap(lizard: BillTheLizard):
    """Schedule the retention cleanup after the core white_rabbit plugin is ready."""
    white_rabbit = getattr(lizard, "white_rabbit", None)
    if white_rabbit is None:
        log.error(
            "[cat_widget_users] white_rabbit is not available; cannot schedule widget user retention cleanup"
        )
        return

    defaults = PluginSettings()
    try:
        settings = await lizard.plugin_manager.get_plugin().load_settings()
    except Exception as exc:
        log.error(f"[cat_widget_users] unable to load settings, using defaults: {exc}")
        settings = {}

    cleanup_every_hours = int(settings.get("cleanup_every_hours", defaults.cleanup_every_hours))

    if cleanup_every_hours <= 0:
        white_rabbit.remove_job(PURGE_JOB_ID)
        return

    white_rabbit.schedule_interval_job(
        job=purge_widget_users_job,
        job_id=PURGE_JOB_ID,
        hours=cleanup_every_hours,
        start_date=datetime.now(UTC),
    )
