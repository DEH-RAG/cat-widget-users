# Cat Widget Users Retention

Automatically delete throwaway widget users — and the conversations that belong to
them — after a configurable retention period.

## Why

The Django widget creates a fresh anonymous user for every browser session:

- the agent id is configurable (default `__widget_django__`, multiple widgets may use
  different agent ids);
- the role is `anonym-user`;
- the username is a random 32-character hex string;
- the user document lives at `agents:<agent>:users:<user>`;
- conversations live at `agents:<agent>:conversations:<user>:<chat>`.

Conversation **owners** (`agents:<agent>:conversations_owners:<chat>`) never expire on
their own, so they must be deleted together with the user and its conversations.
Without this plugin every widget session leaves permanent Redis keys behind.

## What it does

For each configured agent it selects users whose `role` matches `user_role` and whose
retention timestamp is at least `retention_days` old, then cascades:

1. `get_owned_chats` → `release_conversation` for every owned chat;
2. `delete_conversations` for the user;
3. `delete_user`.

Each user is isolated in its own `try/except`: one failure is recorded and the purge
continues. Users without the selected timestamp are skipped.

## Settings

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `retention_days` | int | `7` | Delete users older than this many days. |
| `retention_basis` | `created_at` \| `updated_at` | `created_at` | Timestamp used to compute age. |
| `agent_ids` | list[str] | `["__widget_django__"]` | Agent ids to clean. |
| `user_role` | str | `anonym-user` | Only users with this role are deleted. |
| `cleanup_every_hours` | int | `24` | Interval of the scheduled job. `0` disables it. |

Settings are read through the plugin settings mechanism (`lizard.plugin_manager
.get_plugin().load_settings()`), so they can be edited from the admin like any other
plugin setting.

## Retention semantics

- **Absolute (`retention_basis = "created_at"`)** — a session user is deleted
  `retention_days` after it was created, regardless of activity. This is the default:
  widget users are meant to be throwaway per-session identities.
- **Sliding (`retention_basis = "updated_at"`)** — the clock restarts every time the
  user document is updated, so an active browser session keeps its user until it has
  been idle for `retention_days`.
- A user exactly at the boundary (`age == retention_days`) **is** deleted.
- Conversation owners are always removed with the user: otherwise the chat ids would
  stay claimed forever.

## Scheduling

`main.py` registers an `after_lizard_bootstrap` hook with `priority=-100`. The core
`white_rabbit` plugin sets `lizard.white_rabbit` in its own `after_lizard_bootstrap`
hook at `priority=0`; hooks run by **descending priority**, so this plugin always runs
after it. The plugin does not shut `white_rabbit` down — it is owned by the core.

The job is scheduled with `schedule_interval_job(job=purge_widget_users_job, ...)`.
The job function is module-level so APScheduler can pickle it. When
`cleanup_every_hours <= 0`, the job is removed instead of scheduled.

### Cron alternative

If you prefer an external scheduler, set `cleanup_every_hours = 0` and run the CLI from
cron, for example:

```cron
0 3 * * * cd /app/cat/plugins/cat_widget_users && /app/venv/bin/python scripts/purge_widget_users.py --apply >> /var/log/widget-purge.log 2>&1
```

## CLI

Run from the plugin directory with the core virtualenv. The script adds the plugin
directory to `sys.path` and imports `purge`, so it works both from the repo checkout
and from the deployed plugin folder.

```bash
python scripts/purge_widget_users.py                 # dry-run (default), exit 2
python scripts/purge_widget_users.py --json          # dry-run machine-readable
python scripts/purge_widget_users.py --apply         # delete
python scripts/purge_widget_users.py \
    --agent __widget_django__ --agent __widget_other__ \
    --retention-days 14 --basis updated_at \
    --role anonym-user --apply
```

Arguments:

- `--agent AGENT_ID` — repeatable; defaults to the settings `agent_ids`.
- `--retention-days N` — defaults to the settings value.
- `--basis {created_at,updated_at}` — defaults to the settings value.
- `--role ROLE` — defaults to the settings value.
- `--dry-run` — default mode; prints the plan and writes nothing.
- `--apply` — performs the deletion.
- `--json` — print the full report as JSON.

Exit codes:

- `0` — apply completed with no errors;
- `1` — apply completed with at least one per-agent/per-user error;
- `2` — aborted (dry-run, nothing written).

## Tests

The test file is a standalone script (nothing runs when it is imported):

```bash
cd /root/git/grinning-cat-core
.venv/bin/python /root/git/plugins/cat-widget-users/tests/test_purge.py
```

It fakes the CRUD modules and covers role filtering, `created_at` vs `updated_at`,
the age boundary, cascade order, per-user isolation, dry-run safety, and import-safety
of every plugin `.py` file.

## Import safety

Every `.py` file shipped in the plugin is imported by the host at activation. All
modules are import-safe: no network, Redis, filesystem writes, or execution at import
time. The CLI body and the test body live under `if __name__ == "__main__":`.

## Future improvement

Cookie / browser-storage retention is not handled here: this plugin only cleans the
server-side Redis state. Reclaiming client-side sessions earlier (or tying a widget
session to a short-lived browser marker) is a possible future improvement and would
require changes in the widget backend, which is intentionally out of scope.
