if __name__ == "__main__":
    import asyncio
    import glob
    import importlib
    import os
    import subprocess
    import sys
    from contextlib import contextmanager
    from datetime import UTC, datetime, timedelta
    from unittest.mock import patch

    # Import-time requirements of the core package; no connection is ever opened here
    # because every CRUD call is faked below. db 15 is the dedicated isolated test db.
    os.environ.setdefault("CAT_CRYPTO_SALT", "test_only_crypto_salt_0123456789")
    os.environ.setdefault("CAT_CRYPTO_KEY", "test_only_crypto_key_0123456789")
    os.environ.setdefault("CAT_JWT_SECRET", "test_only_jwt_secret_0123456789_abcdefghij")
    os.environ.setdefault("CAT_REDIS_DB", "15")

    plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)

    purge = importlib.import_module("purge")

    NOW = datetime(2026, 1, 1, tzinfo=UTC)
    NOW_TS = NOW.timestamp()
    DAY = 86400
    AGENT = "__widget_django__"
    WIDGET_USERNAME = "8b7c360596c94f7493f746cde39c3cb1"
    DEFAULT_PATTERNS = ["^[0-9a-f]{32}$"]

    passed = 0
    failed = []

    def check(name, test):
        global passed
        try:
            test()
        except Exception as exc:
            failed.append((name, exc))
            print(f"FAIL {name}: {exc}")
        else:
            passed += 1
            print(f"PASS {name}")

    def make_user(username=WIDGET_USERNAME, created_at=NOW_TS - 8 * DAY,
                  updated_at=NOW_TS - 8 * DAY, **extra):
        user = {
            "id": "user-id",
            "username": username,
            "permissions": {},
            "metadata": {},
            "created_at": created_at,
            "updated_at": updated_at,
        }
        user.update(extra)
        return user

    @contextmanager
    def fake_crud(users_by_agent, owned_chats=None, fail_delete=()):
        owned_chats = owned_chats or {}
        calls = []
        deleted_users = []
        deleted_conversations = []

        async def get_users(agent_id, with_password=False, with_timestamps=False, limit=None, offset=0):
            return {uid: dict(data) for uid, data in users_by_agent.get(agent_id, {}).items()}

        async def get_owned_chats(agent_id, user_id):
            return list(owned_chats.get((agent_id, user_id), []))

        async def release_conversation(agent_id, chat_id):
            calls.append(("release_conversation", agent_id, chat_id))

        async def delete_conversations(agent_id, user_id):
            calls.append(("delete_conversations", agent_id, user_id))
            deleted_conversations.append(user_id)
            return 3

        async def delete_user(agent_id, user_id):
            calls.append(("delete_user", agent_id, user_id))
            if user_id in fail_delete:
                raise RuntimeError("boom")
            deleted_users.append(user_id)
            return {}

        with patch.object(purge.crud_users, "get_users", get_users), \
                patch.object(purge.crud_conversations, "get_owned_chats", get_owned_chats), \
                patch.object(purge.crud_conversations, "release_conversation", release_conversation), \
                patch.object(purge.crud_conversations, "delete_conversations", delete_conversations), \
                patch.object(purge.crud_users, "delete_user", delete_user):
            yield {
                "calls": calls,
                "deleted_users": deleted_users,
                "deleted_conversations": deleted_conversations,
            }

    def run(
        retention_days=7,
        retention_basis="created_at",
        agent_ids=None,
        username_patterns=None,
        user_role=None,
        dry_run=False,
        now=NOW,
    ):
        return asyncio.run(
            purge.purge_widget_users(
                retention_days=retention_days,
                retention_basis=retention_basis,
                agent_ids=agent_ids if agent_ids is not None else [AGENT],
                username_patterns=(
                    username_patterns
                    if username_patterns is not None
                    else list(DEFAULT_PATTERNS)
                ),
                user_role=user_role,
                dry_run=dry_run,
                now=now,
            )
        )

    def test_username_pattern_selection():
        users = {
            AGENT: {
                "widget": make_user(username=WIDGET_USERNAME),
                "admin": make_user(username="admin"),
                "other": make_user(username="Alice"),
            }
        }
        with fake_crud(users) as fake:
            report = run()
        stats = report["agents"][AGENT]
        assert stats["candidates"] == 1, stats
        assert stats["deleted_users"] == 1, stats
        assert fake["deleted_users"] == ["widget"], fake

    def test_non_matching_username_is_not_selected_even_if_old():
        users = {
            AGENT: {
                "admin": make_user(
                    username="admin", created_at=NOW_TS - 365 * DAY,
                    updated_at=NOW_TS - 365 * DAY,
                ),
            }
        }
        with fake_crud(users) as fake:
            report = run()
        stats = report["agents"][AGENT]
        assert stats["candidates"] == 0, stats
        assert fake["deleted_users"] == [], fake

    def test_optional_role_filter():
        users = {
            AGENT: {
                "anon": make_user(username=WIDGET_USERNAME, role="anonym-user"),
                "staff": make_user(
                    username="f" * 32, role="admin"
                ),
            }
        }
        with fake_crud(users) as fake:
            no_role = run()
        assert no_role["agents"][AGENT]["candidates"] == 2, no_role
        assert sorted(fake["deleted_users"]) == ["anon", "staff"], fake

        with fake_crud(users) as fake:
            only_anon = run(user_role="anonym-user")
        assert only_anon["agents"][AGENT]["candidates"] == 1, only_anon
        assert fake["deleted_users"] == ["anon"], fake

        with fake_crud(users) as fake:
            only_admin = run(user_role="admin")
        assert only_admin["agents"][AGENT]["candidates"] == 1, only_admin
        assert fake["deleted_users"] == ["staff"], fake

    def test_basis_created_at_vs_updated_at():
        users = {
            AGENT: {
                "old_created_recent_update": make_user(
                    created_at=NOW_TS - 30 * DAY, updated_at=NOW_TS - 1 * DAY
                ),
                "recent_created_old_update": make_user(
                    created_at=NOW_TS - 1 * DAY, updated_at=NOW_TS - 30 * DAY
                ),
            }
        }
        with fake_crud(users):
            by_created = run(retention_basis="created_at", dry_run=True)
            by_updated = run(retention_basis="updated_at", dry_run=True)
        assert by_created["agents"][AGENT]["candidates"] == 1, by_created
        assert by_updated["agents"][AGENT]["candidates"] == 1, by_updated
        assert by_created["totals"]["candidates"] == by_updated["totals"]["candidates"] == 1

    def test_age_boundary():
        users = {
            AGENT: {
                "exactly_at_boundary": make_user(created_at=NOW_TS - 7 * DAY),
                "one_second_recent": make_user(created_at=NOW_TS - 7 * DAY + 1),
            }
        }
        with fake_crud(users) as fake:
            report = run(retention_days=7)
        stats = report["agents"][AGENT]
        assert stats["candidates"] == 1, stats
        assert fake["deleted_users"] == ["exactly_at_boundary"], fake

    def test_missing_timestamp_is_skipped():
        users = {
            AGENT: {
                "no_timestamp": {
                    "username": WIDGET_USERNAME,
                }
            }
        }
        with fake_crud(users):
            report = run()
        assert report["agents"][AGENT]["candidates"] == 0, report

    def test_cascade_order():
        users = {AGENT: {"widget": make_user()}}
        owned = {(AGENT, "widget"): ["chat1", "chat2"]}
        with fake_crud(users, owned_chats=owned) as fake:
            report = run()
        assert fake["calls"] == [
            ("release_conversation", AGENT, "chat1"),
            ("release_conversation", AGENT, "chat2"),
            ("delete_conversations", AGENT, "widget"),
            ("delete_user", AGENT, "widget"),
        ], fake["calls"]
        stats = report["agents"][AGENT]
        assert stats["deleted_owners"] == 2, stats
        assert stats["deleted_conversations"] == 3, stats
        assert stats["deleted_users"] == 1, stats

    def test_per_user_isolation():
        users = {
            AGENT: {
                "bad": make_user(),
                "good": make_user(),
            }
        }
        with fake_crud(users, fail_delete={"bad"}) as fake:
            report = run()
        stats = report["agents"][AGENT]
        assert stats["candidates"] == 2, stats
        assert stats["deleted_users"] == 1, stats
        assert fake["deleted_users"] == ["good"], fake
        assert len(stats["errors"]) == 1, stats
        assert "bad" in stats["errors"][0], stats
        assert fake["calls"].count(("delete_user", AGENT, "bad")) == 1
        assert fake["calls"].count(("delete_user", AGENT, "good")) == 1

    def test_dry_run_writes_nothing():
        users = {AGENT: {"widget": make_user()}}
        with fake_crud(users, owned_chats={(AGENT, "widget"): ["chat1"]}) as fake:
            report = run(dry_run=True)
        stats = report["agents"][AGENT]
        assert stats["candidates"] == 1, stats
        assert stats["deleted_users"] == 0, stats
        assert stats["deleted_conversations"] == 0, stats
        assert stats["deleted_owners"] == 0, stats
        assert fake["calls"] == [], fake["calls"]

    def test_get_users_error_is_reported():
        async def boom(agent_id, **kwargs):
            raise RuntimeError("redis down")

        with patch.object(purge.crud_users, "get_users", boom):
            report = run()
        stats = report["agents"][AGENT]
        assert stats["candidates"] == 0, stats
        assert len(stats["errors"]) == 1, stats
        assert "redis down" in stats["errors"][0], stats

    def test_first_purge_runs_immediately():
        main = importlib.import_module("main")
        scheduled = []
        removed = []
        cleanup_hours = 24

        class FakeWhiteRabbit:
            def schedule_interval_job(self, **kwargs):
                scheduled.append(kwargs)

            def remove_job(self, job_id):
                removed.append(job_id)

        class FakePlugin:
            async def load_settings(self):
                return {"cleanup_every_hours": cleanup_hours}

        class FakePluginManager:
            def get_plugin(self):
                return FakePlugin()

        class FakeLizard:
            def __init__(self):
                self.white_rabbit = FakeWhiteRabbit()
                self.plugin_manager = FakePluginManager()

        schedule_hook = getattr(main.after_lizard_bootstrap, "function", main.after_lizard_bootstrap)
        before = datetime.now(UTC)
        asyncio.run(schedule_hook(FakeLizard()))
        after = datetime.now(UTC)

        assert len(scheduled) == 1, scheduled
        kwargs = scheduled[0]
        assert kwargs["hours"] == cleanup_hours, kwargs
        assert kwargs["job_id"] == purge.PURGE_JOB_ID, kwargs

        start_date = kwargs["start_date"]
        interval = timedelta(hours=cleanup_hours)
        # start_date is roughly one interval in the past, so APScheduler's first fire
        # lands at "now" instead of a full interval later.
        assert start_date < before, (start_date, before)
        assert abs((before - start_date) - interval) < timedelta(minutes=1), (
            before, start_date,
        )
        first_fire = start_date + interval
        assert before <= first_fire <= after + timedelta(seconds=2), (start_date, first_fire)
        assert removed == [], removed

        scheduled.clear()

        class DisabledPlugin(FakePlugin):
            async def load_settings(self):
                return {"cleanup_every_hours": 0}

        disabled_lizard = FakeLizard()
        disabled_lizard.plugin_manager = type(
            "PM", (), {"get_plugin": lambda self: DisabledPlugin()}
        )()
        asyncio.run(schedule_hook(disabled_lizard))
        assert scheduled == [], scheduled
        assert removed == [purge.PURGE_JOB_ID], removed

    def test_import_safety():
        files = sorted(glob.glob(os.path.join(plugin_dir, "**", "*.py"), recursive=True))
        assert files, "no python files found"
        for path in files:
            name = os.path.splitext(os.path.basename(path))[0]
            env = dict(os.environ)
            paths = [plugin_dir, os.path.dirname(path)]
            if env.get("PYTHONPATH"):
                paths.append(env["PYTHONPATH"])
            env["PYTHONPATH"] = os.pathsep.join(paths)
            result = subprocess.run(
                [sys.executable, "-c", f"import {name}"],
                cwd=plugin_dir,
                env=env,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"import {name} failed: {result.stderr}"
            assert result.stdout == "", f"import {name} printed on stdout: {result.stdout!r}"

    tests = [
        ("username pattern selection", test_username_pattern_selection),
        ("non-matching username never selected", test_non_matching_username_is_not_selected_even_if_old),
        ("optional role filter", test_optional_role_filter),
        ("created_at vs updated_at selection", test_basis_created_at_vs_updated_at),
        ("age boundary", test_age_boundary),
        ("missing timestamp skipped", test_missing_timestamp_is_skipped),
        ("cascade order (owners -> conversations -> user)", test_cascade_order),
        ("per-user isolation", test_per_user_isolation),
        ("dry-run writes nothing", test_dry_run_writes_nothing),
        ("get_users error reported", test_get_users_error_is_reported),
        ("first purge runs immediately", test_first_purge_runs_immediately),
        ("import safety of every plugin .py", test_import_safety),
    ]

    for test_name, test_fn in tests:
        check(test_name, test_fn)

    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        print("Failures:")
        for test_name, exc in failed:
            print(f"  - {test_name}: {exc}")
        sys.exit(1)

    sys.exit(0)
