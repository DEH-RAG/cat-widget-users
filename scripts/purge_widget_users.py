if __name__ == "__main__":
    import argparse
    import asyncio
    import json
    import os
    import sys

    plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)

    from purge import purge_widget_users
    from settings import PluginSettings

    def build_parser() -> argparse.ArgumentParser:
        defaults = PluginSettings()
        parser = argparse.ArgumentParser(
            description="Delete throwaway widget users and their conversations after a retention period."
        )
        parser.add_argument(
            "--agent",
            dest="agents",
            action="append",
            metavar="AGENT_ID",
            help=(
                "Agent id to clean; repeatable. Defaults to the plugin settings: "
                + ", ".join(defaults.agent_ids)
            ),
        )
        parser.add_argument(
            "--retention-days",
            type=int,
            default=defaults.retention_days,
            help=f"Delete users older than this many days (default: {defaults.retention_days}).",
        )
        parser.add_argument(
            "--basis",
            choices=["created_at", "updated_at"],
            default=defaults.retention_basis,
            help=f"Timestamp used to compute age (default: {defaults.retention_basis}).",
        )
        parser.add_argument(
            "--username-pattern",
            dest="username_patterns",
            action="append",
            metavar="REGEX",
            help=(
                "Only delete users whose username full-matches this regex; repeatable. "
                "Defaults to the plugin settings: " + ", ".join(defaults.username_patterns)
            ),
        )
        parser.add_argument(
            "--role",
            default=defaults.user_role,
            help=(
                "Optional: only delete users with this role "
                f"(default: {defaults.user_role})."
            ),
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Only print the plan, write nothing, and exit with code 2 (default).",
        )
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete the matching users and conversations.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Print the report as JSON.",
        )
        return parser

    def print_human(report: dict, dry_run: bool) -> None:
        header = "DRY-RUN plan (nothing was written)" if dry_run else "Purge report"
        print(header)
        for agent_id, stats in report["agents"].items():
            if dry_run:
                print(f"  {agent_id}: candidates={stats['candidates']}")
            else:
                print(
                    f"  {agent_id}: candidates={stats['candidates']}"
                    f" deleted_users={stats['deleted_users']}"
                    f" deleted_conversations={stats['deleted_conversations']}"
                    f" deleted_owners={stats['deleted_owners']}"
                )
        totals = report["totals"]
        if dry_run:
            print(f"TOTAL candidates={totals['candidates']}")
        else:
            print(
                f"TOTAL candidates={totals['candidates']}"
                f" deleted_users={totals['deleted_users']}"
                f" deleted_conversations={totals['deleted_conversations']}"
                f" deleted_owners={totals['deleted_owners']}"
            )
        for error in totals["errors"]:
            print(f"  error: {error}", file=sys.stderr)

    args = build_parser().parse_args()
    dry_run = not args.apply

    defaults = PluginSettings()
    agent_ids = args.agents or list(defaults.agent_ids)
    username_patterns = args.username_patterns or list(defaults.username_patterns)

    report = asyncio.run(
        purge_widget_users(
            retention_days=args.retention_days,
            retention_basis=args.basis,
            agent_ids=agent_ids,
            username_patterns=username_patterns,
            user_role=args.role,
            dry_run=dry_run,
        )
    )

    if args.json:
        print(json.dumps({"dry_run": dry_run, **report}, indent=2, default=str))
    else:
        print_human(report, dry_run)

    if dry_run:
        sys.exit(2)

    if report["totals"]["errors"]:
        sys.exit(1)

    sys.exit(0)
