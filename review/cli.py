"""bin/review: the command line for the review tool."""
import argparse
import json
import sys

from .db import connect, log
from .paths import DB_PATH


def main(argv=None):
    p = argparse.ArgumentParser(prog="review", description="Truckee Community Cares local review tool")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("import", help="import a JotForm xlsx export from a prior season"); s.add_argument("xlsx"); s.add_argument("--season", required=True); s.add_argument("--status", default="accepted")
    sub.add_parser("pull", help="fetch and decrypt new web applications")
    s = sub.add_parser("match", help="run matching for a season"); s.add_argument("--season"); s.add_argument("--no-judge", action="store_true")
    sub.add_parser("push", help="push decision statuses to the server")
    sub.add_parser("console", help="open the local admin console").add_argument("--port", type=int, default=8789)
    sub.add_parser("stats", help="counts by season and status")
    s = sub.add_parser("purge-server", help="delete a season from the server (after distribution)"); s.add_argument("season")
    a = p.parse_args(argv)
    con = connect()
    if a.cmd == "import":
        from .importer import import_xlsx
        n, d = import_xlsx(a.xlsx, a.season, a.status, con); print(f"imported {n} (skipped {d} already present)")
    elif a.cmd == "pull":
        from .sync import pull
        print(f"pulled {pull(con)} new applications")
    elif a.cmd == "match":
        from .match import run_matching
        from .sync import load_config
        season = a.season or load_config()["season"]
        print(json.dumps(run_matching(con, season, use_judge=not a.no_judge)))
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
        from .sync import purge_server
        if input(f"Type the season ({a.season}) to delete every server row: ") != a.season:
            sys.exit("aborted")
        print(purge_server(a.season)); log(con, "purge_server", a.season); con.commit()


if __name__ == "__main__":
    main()
