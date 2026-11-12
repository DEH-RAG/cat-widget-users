if __name__ == "__main__":
    import asyncio
    import glob
    import importlib
    import os
    import subprocess
    import sys
    from contextlib import contextmanager
    from datetime import UTC, datetime
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

    def make_user(role="anonym-user", created_at=NOW_TS - 8 * DAY, updated_at=NOW_TS - 8 * DAY):
        return {"role": role, "username": "0123456789abcdef0123456789abcdef",
                "created_at": created_at, "updated_at": updated_at}

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
        user_role="anonym-user",
        dry_run=False,
        now=NOW,
    ):
        return asyncio.run(
            purge.purge_widget_users(
                retention_days=retention_days,
                retention_basis=retention_basis,
                agent_ids=agent_ids if agent_ids is not None else [AGENT],
                user_role=user_role,
                dry_run=dry_run,
                now=now,
            )
        )

    def test_role_filter():
        users = {
            AGENT: {
                "widget": make_user(role="anonym-user"),
                "admin": make_user(role="admin"),
                "other": make_user(role="user"),
            }
        }
        with fake_crud(users) as fake:
            report = run()
        stats = report["agents"][AGENT]
        assert stats["candidates"] == 1, stats
        assert stats["deleted_users"] == 1, stats
        assert fake["deleted_users"] == ["widget"], fake

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
                    "role": "anonym-user",
                    "username": "abc",
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
        ("role filter", test_role_filter),
        ("created_at vs updated_at selection", test_basis_created_at_vs_updated_at),
        ("age boundary", test_age_boundary),
        ("missing timestamp skipped", test_missing_timestamp_is_skipped),
        ("cascade order (owners -> conversations -> user)", test_cascade_order),
        ("per-user isolation", test_per_user_isolation),
        ("dry-run writes nothing", test_dry_run_writes_nothing),
        ("get_users error reported", test_get_users_error_is_reported),
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
