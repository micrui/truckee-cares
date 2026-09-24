"""bin/review: the command line for the review tool."""
import argparse
import json
import sys

from .db import connect, log
from .paths import DB_PATH
from .sync import ReviewSetupError


def purge_local(con, season):
    """Delete a season from the local database, children first (foreign keys are on):
    tasks that point at the season's candidates or applications, the candidates, the
    applications, then families left with no applications, the events that named a
    purged id (real seasons only), and the pull cursor. Returns counts."""
    ids = [r[0] for r in con.execute("SELECT id FROM applications WHERE season=?", (season,))]
    fams = [r[0] for r in con.execute("SELECT DISTINCT family_id FROM applications WHERE season=? AND family_id IS NOT NULL", (season,))]
    cand_where = "app_a IN (SELECT id FROM applications WHERE season=?) OR app_b IN (SELECT id FROM applications WHERE season=?)"
    counts = {}
    counts["tasks"] = con.execute(f"DELETE FROM tasks WHERE candidate_id IN (SELECT id FROM candidates WHERE {cand_where})", (season, season)).rowcount
    counts["tasks"] += con.execute("DELETE FROM tasks WHERE app_id IN (SELECT id FROM applications WHERE season=?)", (season,)).rowcount
    counts["candidates"] = con.execute(f"DELETE FROM candidates WHERE {cand_where}", (season, season)).rowcount
    con.execute("UPDATE applications SET family_id=NULL WHERE season=?", (season,))
    counts["applications"] = con.execute("DELETE FROM applications WHERE season=?", (season,)).rowcount
    from .match import recompute_family
    for fid in fams:
        if con.execute("SELECT 1 FROM families WHERE id=?", (fid,)).fetchone():
            recompute_family(con, fid)
    counts["families"] = con.execute("DELETE FROM families WHERE NOT EXISTS (SELECT 1 FROM applications a WHERE a.family_id=families.id)").rowcount
    counts["events"] = 0
    if season != "preview" and ids:
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            counts["events"] += con.execute(f"DELETE FROM events WHERE ref IN ({','.join('?' * len(part))})", part).rowcount
    con.execute("DELETE FROM meta WHERE key IN (?, ?, ?)", (f"since:{season}", f"since_id:{season}", f"failed:{season}"))
    log(con, "purge_local", season, json.dumps(counts))
    con.commit()
    return counts


def run(argv=None):
    p = argparse.ArgumentParser(prog="review", description="Truckee Community Cares local review tool")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("import", help="import a JotForm xlsx export from a prior season"); s.add_argument("xlsx"); s.add_argument("--season", required=True); s.add_argument("--status", default="accepted")
    s = sub.add_parser("pull", help="fetch and decrypt new web applications (the season, then preview)"); s.add_argument("--season")
    s.add_argument("--reset", action="store_true", help="forget the pull cursor first, so every server row is fetched again (rows already stored are skipped)")
    s = sub.add_parser("match", help="run matching for a season"); s.add_argument("--season"); s.add_argument("--no-judge", action="store_true")
    s.add_argument("--all", action="store_true", help="match every application of the season, not only new ones (e.g. an imported season)")
    sub.add_parser("push", help="push decision statuses to the server")
    s = sub.add_parser("rejudge", help="ask the judge about pairs left unsure by rule"); s.add_argument("--season", required=True); s.add_argument("--workers", type=int, default=8)
    sub.add_parser("console", help="open the local admin console").add_argument("--port", type=int, default=8789)
    sub.add_parser("stats", help="counts by season and status")
    s = sub.add_parser("purge-server", help="delete a season from the server (after distribution, or 'preview')"); s.add_argument("season")
    s.add_argument("--force", action="store_true", help="for preview: delete even if some preview rows were submitted during the real season")
    s = sub.add_parser("purge-local", help="delete a season's applications from the local database"); s.add_argument("season")
    s = sub.add_parser("retry-failed", help="fetch again just the rows this Mac could not read (after a new key lands here)"); s.add_argument("--season")
    s = sub.add_parser("delete-server", help="delete one submission from the server (junk or a test row); asks you to type the id"); s.add_argument("id")
    s = sub.add_parser("delete-failed", help="delete every row this Mac could not read from the server (a spam flood); asks you to type the season"); s.add_argument("--season", required=True)
    a = p.parse_args(argv)
    con = connect()
    if a.cmd == "import":
        from .importer import import_xlsx
        n, d = import_xlsx(a.xlsx, a.season, a.status, con); print(f"imported {n} (skipped {d} already present)")
    elif a.cmd == "pull":
        from .sync import load_config, pull, reset_cursor
        season = a.season or load_config()["season"]
        if a.reset:
            for s_ in {season, "preview"} if season != "preview" else {"preview"}:
                reset_cursor(con, s_)
        new, failed = pull(con, season=season)
        print(f"pulled {new} new applications; {failed} could not be read (expected for rows submitted before this key existed)")
    elif a.cmd == "match":
        from .match import run_matching
        from .sync import load_config
        season = a.season or load_config()["season"]
        print(json.dumps(run_matching(con, season, use_judge=not a.no_judge, include_all=a.all)))
    elif a.cmd == "rejudge":
        from .match import rejudge
        print(json.dumps(rejudge(con, a.season, a.workers)))
    elif a.cmd == "push":
        from .sync import push_statuses
        print(f"pushed {push_statuses(con)} status changes")
    elif a.cmd == "console":
        from .console import serve
        serve(con, a.port)
    elif a.cmd == "stats":
        for r in con.execute("SELECT season, status, COUNT(*) n FROM applications GROUP BY season, status ORDER BY season, status"):
            print(f"{r['season']:>6} {r['status']:<12} {r['n']}")
        print("open tasks:", con.execute("SELECT COUNT(*) FROM tasks WHERE status='open'").fetchone()[0], "| db:", DB_PATH)
    elif a.cmd == "purge-server":
        from .sync import preview_rows_in_window, purge_server
        if a.season == "preview" and not a.force:
            ids, open_tasks = preview_rows_in_window(con)
            if ids or open_tasks:
                sys.exit(f"{len(ids)} preview application(s) were submitted while the season was open and {open_tasks} preview_in_window task(s) are open. "
                         "Call those families first (see the tasks page), then rerun with --force.")
        if input(f"Type the season ({a.season}) to delete every server row: ") != a.season:
            sys.exit("aborted")
        print(purge_server(a.season)); log(con, "purge_server", a.season); con.commit()
    elif a.cmd == "purge-local":
        counts = purge_local(con, a.season)
        print(f"deleted for {a.season}: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    elif a.cmd == "retry-failed":
        from .sync import load_config, retry_failed
        season = a.season or load_config()["season"]
        stored, still, gone = retry_failed(con, season)
        print(f"{season}: stored {stored}, still unreadable {still}, gone from the server {gone}")
    elif a.cmd == "delete-server":
        from .sync import delete_server
        if input(f"Type the id ({a.id}) to delete it from the server: ") != a.id:
            sys.exit("aborted")
        print(f"deleted {delete_server(con, a.id)} row(s)")
    elif a.cmd == "delete-failed":
        from .sync import delete_failed, failed_ids
        ids = failed_ids(con, a.season)
        if not ids:
            sys.exit(f"no unreadable rows recorded for {a.season} on this Mac")
        print(f"{len(ids)} row(s) of {a.season} could not be read on this Mac. A missing key on this Mac lands rows here too; "
              "delete only when you are sure they are junk (see docs/runbook.md, Undecryptable rows).")
        if input(f"Type the season ({a.season}) to delete all {len(ids)} from the server: ") != a.season:
            sys.exit("aborted")
        deleted, tried = delete_failed(con, a.season)
        print(f"deleted {deleted} row(s) of {tried} tried")


def main(argv=None):
    """A missing key or token is a message and exit code 1, not a traceback."""
    try:
        run(argv)
    except ReviewSetupError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
