"""Command line entry point: `congress-watch <command>`."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .briefing import build_brief
from .store import Store
from .telem_client import TelemClient, TelemError, extract_results, has_warning


# ---------------------------------------------------------------- helpers
def load_topics(path: str) -> tuple[list[dict], dict[str, str]]:
    cfg = yaml.safe_load(Path(path).read_text()) or {}
    defaults = cfg.get("defaults") or {}
    topics = []
    for t in cfg.get("topics") or []:
        if not t.get("key") or not t.get("query"):
            raise SystemExit(f"Every topic needs a key and a query: {t}")
        topics.append({**defaults, **t})
    labels = {t["key"]: t.get("label", t["key"]) for t in topics}
    return topics, labels


def _expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp <= datetime.now(timezone.utc)


_flag = has_warning


# --------------------------------------------------------------- commands
def cmd_setup(args, client: TelemClient, store: Store) -> int:
    topics, _ = load_topics(args.topics)
    existing = {m["topic_key"]: m for m in store.monitors()}
    wanted = {t["key"] for t in topics}

    for t in topics:
        cur = existing.get(t["key"])
        reusable = (
            cur is not None
            and not args.force
            and cur["query"] == t["query"]
            and cur["status"] == "active"
            and not _expired(cur["expires_at"])
        )
        if reusable:
            if cur["frequency"] != t.get("frequency"):
                client.update_monitor(cur["monitor_id"], frequency=t["frequency"])
                store.save_monitor(t["key"], {"id": cur["monitor_id"], "query": cur["query"],
                                              "frequency": t["frequency"], "status": "active",
                                              "expires_at": cur["expires_at"]})
                print(f"~ {t['key']}: frequency -> {t['frequency']}")
            else:
                print(f"= {t['key']}: already active")
            continue
        if cur is not None:
            try:
                client.delete_monitor(cur["monitor_id"])
            except TelemError:
                pass  # already gone / expired
        m = client.create_monitor(
            t["query"],
            frequency=t.get("frequency", "1d"),
            include_domains=t.get("include_domains"),
            ttl=t.get("ttl", "30d"),
            webhook_url=args.webhook_url,
            search_num_results=t.get("search_num_results"),
            metadata={"project": "congress-watch", "topic": t["key"]},
        )
        m.setdefault("query", t["query"])
        m.setdefault("frequency", t.get("frequency"))
        store.save_monitor(t["key"], m)
        print(f"+ {t['key']}: created {m['id']} ({m.get('frequency')}, expires {m.get('expires_at', '?')})")

    for key in set(existing) - wanted:
        if args.prune:
            try:
                client.delete_monitor(existing[key]["monitor_id"])
            except TelemError:
                pass
            store.delete_monitor(key)
            print(f"- {key}: removed (no longer in topics file)")
        else:
            print(f"! {key}: not in topics file (use --prune to delete it)")
    return 0


def cmd_poll(args, client: TelemClient, store: Store) -> int:
    total = 0
    waiting = []
    raw_dir = Path(args.raw) if getattr(args, "raw", None) else None
    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)
    monitors = store.monitors()
    print(f"Database: {store.path.resolve()}  ({len(monitors)} monitors)")
    if not monitors:
        print("No monitors in this database. Run `congress-watch setup` here, "
              "or run poll from the folder where you ran setup (or pass --db).")
        return 1
    if not any(m["status"] == "active" for m in monitors):
        print("All monitors are marked inactive. Run `congress-watch setup` to recreate them.")
    for m in monitors:
        if m["status"] != "active":
            print(f"{m['topic_key']:<22} skipped (status: {m['status']})")
            continue
        new = 0
        note = ""
        for round_no in range(args.max_rounds):
            try:
                resp = client.poll_monitor(m["monitor_id"])
            except TelemError as exc:
                if raw_dir:
                    (raw_dir / f"{m['topic_key']}_{round_no}_error.txt").write_text(str(exc))
                if "404" in str(exc):
                    store.set_monitor_status(m["topic_key"], "ended")
                    print(f"! {m['topic_key']}: monitor not found; run `setup` to recreate")
                else:
                    print(f"! {m['topic_key']}: poll failed: {exc}")
                break
            if raw_dir:
                (raw_dir / f"{m['topic_key']}_{round_no}.json").write_text(
                    json.dumps(resp, indent=2, default=str))
            new += store.add_results(m["topic_key"], extract_results(resp))
            if _flag(resp, "first_run_pending"):
                note = "  (first run still in progress)"
                waiting.append(m["topic_key"])
                break
            if _flag(resp, "run_failed"):
                print(f"! {m['topic_key']}: latest Telem run failed")
            if _flag(resp, "monitor_inactive"):
                store.set_monitor_status(m["topic_key"], "ended")
                print(f"! {m['topic_key']}: monitor inactive (expired?); run `setup` to renew")
                break
            if not _flag(resp, "more_runs_pending"):
                break
        total += new
        print(f"{m['topic_key']:<22} +{new}{note}")
    print(f"Done: {total} new results stored.")
    if waiting:
        print(f"{len(waiting)} monitor(s) haven't finished their first run yet. "
              "Poll again in a few minutes.")
    if raw_dir:
        print(f"Raw poll responses saved in {raw_dir}/")
    return 0


def cmd_brief(args, client: TelemClient | None, store: Store) -> int:
    _, labels = load_topics(args.topics)
    path = build_brief(
        store,
        client,
        labels,
        out_dir=args.out,
        fetch_top=args.fetch_top,
        mark=not args.dry_run,
    )
    if path is None:
        print("No new results since the last briefing.")
    else:
        print(f"Briefing written: {path} (and {path.with_suffix('.html').name})")
    return 0


def cmd_status(args, client: TelemClient | None, store: Store) -> int:
    rows = store.monitors()
    if not rows:
        print("No monitors yet. Run `congress-watch setup`.")
        return 0
    pending = {}
    for r in store.unbriefed():
        pending[r["topic_key"]] = pending.get(r["topic_key"], 0) + 1
    if getattr(args, "remote", False):
        if client is None:
            client = TelemClient()
        print(f"{'topic':<22}{'status':<9}{'agent':<11}{'last polled':<22}next run")
        for m in rows:
            try:
                info = client.get_monitor(m["monitor_id"])
            except TelemError as exc:
                print(f"{m['topic_key']:<22}error: {exc}")
                continue
            agent = (info.get("agent_state") or {}).get("status") or "-"
            nxt = info.get("next_run_at") or (info.get("agent_state") or {}).get("next_run_at") or "-"
            print(f"{m['topic_key']:<22}{info.get('status', '?'):<9}{agent:<11}"
                  f"{(info.get('last_polled_at') or '-'):<22}{nxt}")
        return 0
    print(f"{'topic':<22}{'status':<9}{'freq':<6}{'unbriefed':<10}expires")
    for m in rows:
        status = "expired" if _expired(m["expires_at"]) else m["status"]
        print(f"{m['topic_key']:<22}{status:<9}{(m['frequency'] or ''):<6}"
              f"{pending.get(m['topic_key'], 0):<10}{m['expires_at'] or '-'}")
    return 0


def cmd_search(args, client: TelemClient, store: Store) -> int:
    for i, r in enumerate(client.search(args.query, num_results=args.n), 1):
        print(f"{i}. {r.get('title')}\n   {r.get('url')}  ({r.get('provider')})")
    return 0


def cmd_teardown(args, client: TelemClient, store: Store) -> int:
    for m in store.monitors():
        try:
            client.delete_monitor(m["monitor_id"])
        except TelemError as exc:
            print(f"! {m['topic_key']}: {exc}")
        store.delete_monitor(m["topic_key"])
        print(f"- {m['topic_key']}")
    return 0


def cmd_run(args, client: TelemClient, store: Store) -> int:
    cmd_poll(args, client, store)
    return cmd_brief(args, client, store)


# ------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="congress-watch", description=__doc__)
    p.add_argument("--db", default="congress_watch.db", help="SQLite file (default: %(default)s)")
    p.add_argument("--topics", default="topics.yaml", help="topics file (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="create or renew one Telem monitor per topic")
    s.add_argument("--webhook-url", help="https URL to receive results in real time")
    s.add_argument("--force", action="store_true", help="recreate every monitor")
    s.add_argument("--prune", action="store_true", help="delete monitors not in the topics file")

    def add_poll_opts(sp):
        sp.add_argument("--max-rounds", type=int, default=3, help="re-poll while runs are pending")
        sp.add_argument("--raw", metavar="DIR", help="save each raw poll response as JSON in DIR")

    def add_brief_opts(sp):
        sp.add_argument("--out", default="briefs", help="output folder (default: %(default)s)")
        sp.add_argument("--fetch-top", type=int, default=5,
                        help="max articles fetched per section for full text (default: %(default)s)")
        sp.add_argument("--dry-run", action="store_true", help="don't mark items as briefed")

    add_poll_opts(sub.add_parser("poll", help="collect new results from all monitors"))
    add_brief_opts(sub.add_parser("brief", help="write a briefing from unbriefed results"))
    r = sub.add_parser("run", help="poll, then brief (good for a daily cron job)")
    add_poll_opts(r)
    add_brief_opts(r)
    st = sub.add_parser("status", help="show monitors and pending counts")
    st.add_argument("--remote", action="store_true",
                    help="ask Telem for each monitor's agent state and next run")
    q = sub.add_parser("search", help="one-off Telem search")
    q.add_argument("query")
    q.add_argument("-n", type=int, default=5)
    sub.add_parser("teardown", help="delete all monitors")
    w = sub.add_parser("webhook", help="run a webhook receiver")
    w.add_argument("--port", type=int, default=8787)
    return p


def main(argv: list[str] | None = None, client: TelemClient | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "webhook":
        from .webhook import serve

        serve(args.db, args.port)
        return 0

    store = Store(args.db)
    needs_client = not (
        (args.cmd == "status" and not args.remote) or args.cmd == "brief"
    )
    if client is None and needs_client:
        try:
            client = TelemClient()
        except TelemError as exc:
            print(exc, file=sys.stderr)
            return 2
    if client is None and args.cmd == "brief" and os.environ.get("TELEM_API_KEY"):
        client = TelemClient()  # lets briefs fetch article ledes

    handler = {
        "setup": cmd_setup, "poll": cmd_poll, "brief": cmd_brief, "status": cmd_status,
        "search": cmd_search, "teardown": cmd_teardown, "run": cmd_run,
    }[args.cmd]
    try:
        return handler(args, client, store)
    except TelemError as exc:
        print(f"Telem error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
