"""pull(): pages through every server row, one unreadable row never blocks the season
and never pins the cursor, unreadable ids are kept in failed:<season> for retry-failed
and delete-failed, preview rows submitted during the season become tasks once, and
nothing decrypted ever reaches the events or tasks tables."""
import base64
import builtins
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import pyrage
import pytest

os.environ["TCC_JUDGE"] = "none"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from review import cli, sync  # noqa: E402
from review.db import connect  # noqa: E402

CFG = {"season": "2026", "timezone": "America/Los_Angeles", "opens": "2026-10-15T00:00:00", "closes": "2026-11-15T23:59:59", "mode": "pickup", "api_base": "http://stub"}
SECRET = "plaintext-that-must-never-be-logged"


def armor(raw):
    b = base64.b64encode(raw).decode()
    return "-----BEGIN AGE ENCRYPTED FILE-----\n" + "\n".join(b[i:i + 64] for i in range(0, len(b), 64)) + "\n-----END AGE ENCRYPTED FILE-----\n"


def encrypt(ident, text):
    return armor(pyrage.encrypt(text.encode(), [ident.to_public()]))


def good_payload(first):
    return json.dumps({"version": 1, "applicant": {"first_name": first, "last_name": "Prueba", "phone": "5305550100", "contact_lang": "es"},
                       "address": {"street": "1 Elm St", "unit": "", "city": "Truckee", "zip": "96161", "in_area": True},
                       "children": [{"first_name": "Ana", "age": 4, "sex": "girl"}], "programs": {"food": True}}, ensure_ascii=False)


def row(sid, created, ident, text, lang="es", **extra):
    return {"id": sid, "created_at": created, "lang": lang, "status": "new", "note": "", "ciphertext": encrypt(ident, text), **extra}


class Server:
    """Stands in for the Worker: keyset pages per season exactly like the real query
    (created_at > since OR (created_at = since AND id > since_id), ordered, LIMIT page),
    one row by id, delete by id, PATCH of status and note (409 on a superseded row, like
    the Worker). Every call is recorded; every PATCH body too."""
    def __init__(self, rows, page=500):
        self.rows = rows
        self.page = page
        self.calls = []
        self.patches = []

    def find(self, sid):
        for rows in self.rows.values():
            for r in rows:
                if r["id"] == sid:
                    return rows, r
        return None, None

    def status(self, sid):
        _, r = self.find(sid)
        return (r["status"], r.get("note", "")) if r else None

    def api(self, method, path, body=None, headers=None):
        u = urllib.parse.urlparse(path); q = urllib.parse.parse_qs(u.query)
        m = re.fullmatch(r"/api/admin/submissions/([A-Z0-9-]+)", u.path)
        if m:
            sid = m.group(1)
            self.calls.append((method, sid))
            rows, r = self.find(sid)
            if method == "DELETE":
                if r:
                    rows.remove(r); return {"deleted": 1}
                return {"deleted": 0}
            if method == "PATCH":
                self.patches.append((sid, body["status"], body.get("note", "")))
                if not r:
                    return {"ok": True, "changed": 0}
                if r["status"] == "superseded" and body["status"] != "superseded":
                    nxt = next((x["id"] for rs in self.rows.values() for x in rs if x.get("supersedes") == sid), None)
                    raise urllib.error.HTTPError(path, 409, "superseded", {}, io.BytesIO(json.dumps({"error": "superseded", "superseded_by": nxt}).encode()))
                r["status"] = body["status"]; r["note"] = str(body.get("note") or "").strip()[:200]
                return {"ok": True, "changed": 1}
            if r:
                return {"item": r}
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        season = q.get("season", [""])[0]; since = q.get("since", [""])[0]; since_id = q.get("since_id", [""])[0]
        self.calls.append((method, season, since, since_id))
        items = sorted((r for r in self.rows.get(season, []) if r["created_at"] > since or (r["created_at"] == since and r["id"] > since_id)),
                       key=lambda r: (r["created_at"], r["id"]))[:self.page]
        last = items[-1] if items else None
        return {"season": season, "items": items, "next_since": last["created_at"] if last else since,
                "next_id": last["id"] if last else since_id, "has_more": len(items) >= self.page}


def setup(monkeypatch, tmp_path, rows, ident=None, page=500):
    ident = ident or pyrage.x25519.Identity.generate()
    srv = Server(rows, page)
    monkeypatch.setattr(sync, "load_config", lambda: dict(CFG))
    monkeypatch.setattr(sync, "load_identities", lambda: [ident])
    monkeypatch.setattr(sync, "api", srv.api)
    return connect(tmp_path / "t.sqlite"), ident, srv


def meta(con, key):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def test_bad_row_does_not_block_and_the_cursor_moves_past_it(monkeypatch, tmp_path):
    ident = pyrage.x25519.Identity.generate()
    rows = {"2026": [
        row("TCC-A1", "2026-10-20T10:00:00.000Z", ident, good_payload("Ana")),
        row("TCC-B2", "2026-10-20T11:00:00.000Z", ident, "not json " + SECRET),
        row("TCC-C3", "2026-10-20T12:00:00.000Z", ident, good_payload("Carla"), lang="en"),
    ], "preview": []}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    assert sync.pull(con) == (2, 1)
    assert {r[0] for r in con.execute("SELECT id FROM applications")} == {"TCC-A1", "TCC-C3"}
    # The cursor is past the bad row, both parts stored; the bad id is in the failed list.
    assert meta(con, "since:2026") == "2026-10-20T12:00:00.000Z"
    assert meta(con, "since_id:2026") == "TCC-C3"
    assert sync.failed_ids(con, "2026") == ["TCC-B2"]
    tasks = con.execute("SELECT * FROM tasks WHERE kind='decrypt_error'").fetchall()
    assert len(tasks) == 1 and tasks[0]["title"] == "Could not read submission TCC-B2" and tasks[0]["app_id"] is None
    # Second pull: asks from the cursor, gets nothing, so the bad row is not fetched again and nothing is repeated.
    assert sync.pull(con) == (0, 0)
    assert srv.calls[-2] == ("GET", "2026", "2026-10-20T12:00:00.000Z", "TCC-C3")
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE kind='decrypt_error'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='bad_payload' AND ref='TCC-B2'").fetchone()[0] == 1
    # The plaintext never reached the database outside the applications table.
    for table in ("events", "tasks", "meta"):
        for r in con.execute(f"SELECT * FROM {table}"):
            assert SECRET not in json.dumps(dict(r))
    # A reset fetches everything again: stored rows are skipped by id, the bad row fails again
    # but stays one task and one failed id.
    sync.reset_cursor(con, "2026")
    assert sync.pull(con) == (0, 1)
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE kind='decrypt_error'").fetchone()[0] == 1
    assert sync.failed_ids(con, "2026") == ["TCC-B2"]
    assert meta(con, "since_id:2026") == "TCC-C3"


def test_pull_pages_until_has_more_is_false(monkeypatch, tmp_path):
    ident = pyrage.x25519.Identity.generate()
    # Five rows sharing one timestamp, pages of two: only (created_at, id) paging gets them all.
    rows = {"2026": [row(f"TCC-R{i}", "2026-10-20T10:00:00.000Z", ident, good_payload(f"R{i}")) for i in range(1, 6)], "preview": []}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident, page=2)
    assert sync.pull(con) == (5, 0)
    season_calls = [c for c in srv.calls if c[1] == "2026"]
    assert [(c[2], c[3]) for c in season_calls] == [("", ""), ("2026-10-20T10:00:00.000Z", "TCC-R2"), ("2026-10-20T10:00:00.000Z", "TCC-R4")]
    assert con.execute("SELECT COUNT(*) FROM applications WHERE season='2026'").fetchone()[0] == 5
    assert meta(con, "since_id:2026") == "TCC-R5"
    assert "pages=3" in con.execute("SELECT detail FROM events WHERE kind='pull' AND ref='2026'").fetchone()[0]
    # A sixth row lands: one more page, nothing repeated.
    rows["2026"].append(row("TCC-R6", "2026-10-20T10:00:01.000Z", ident, good_payload("R6")))
    assert sync.pull(con) == (1, 0)
    assert [c for c in srv.calls if c[1] == "2026"][-1] == ("GET", "2026", "2026-10-20T10:00:00.000Z", "TCC-R5")


def test_a_cursor_without_since_id_is_tolerated(monkeypatch, tmp_path):
    """An older database stored only since:<season>. The server then repeats the rows at
    that timestamp; the ones already held are skipped by id and the cursor gains its id part."""
    ident = pyrage.x25519.Identity.generate()
    t = "2026-10-20T10:00:00.000Z"
    rows = {"2026": [row("TCC-A", t, ident, good_payload("A")), row("TCC-B", t, ident, good_payload("B")), row("TCC-C", "2026-10-20T10:00:01.000Z", ident, good_payload("C"))], "preview": []}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    con.execute("INSERT INTO applications(id,season,source,submitted_at,lang,status,payload,norm) VALUES('TCC-A','2026','web',?,'es','new','{}','{}')", (t,))
    con.execute("INSERT INTO meta(key,value) VALUES('since:2026',?)", (t,)); con.commit()
    assert sync.pull(con) == (2, 0)
    assert srv.calls[0] == ("GET", "2026", t, "")
    assert con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 3
    assert (meta(con, "since:2026"), meta(con, "since_id:2026")) == ("2026-10-20T10:00:01.000Z", "TCC-C")


def test_wrong_key_row_lands_in_failed_and_retry_reads_it_with_the_new_key(monkeypatch, tmp_path):
    ident = pyrage.x25519.Identity.generate(); other = pyrage.x25519.Identity.generate()
    rows = {"2026": [row("TCC-OLD", "2026-10-01T00:00:00.000Z", other, good_payload("Vieja"))], "preview": []}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    assert sync.pull(con) == (0, 1)
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='decrypt_error' AND ref='TCC-OLD'").fetchone()[0] == 1
    assert sync.failed_ids(con, "2026") == ["TCC-OLD"]
    assert meta(con, "since:2026") == "2026-10-01T00:00:00.000Z"  # the cursor moved past it
    # Still the wrong key: the retry fetches just that id and it still fails; one task, one id.
    assert sync.retry_failed(con, "2026") == (0, 1, 0)
    assert srv.calls[-1] == ("GET", "TCC-OLD")
    assert sync.failed_ids(con, "2026") == ["TCC-OLD"]
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE kind='decrypt_error'").fetchone()[0] == 1
    # The right key arrives on this Mac: the row is stored, leaves the list, and its task closes.
    monkeypatch.setattr(sync, "load_identities", lambda: [ident, other])
    assert sync.retry_failed(con, "2026") == (1, 0, 0)
    assert con.execute("SELECT id FROM applications").fetchone()[0] == "TCC-OLD"
    assert sync.failed_ids(con, "2026") == []
    t = con.execute("SELECT status, resolution FROM tasks WHERE kind='decrypt_error'").fetchone()
    assert (t["status"], t["resolution"]) == ("done", "read on a later pull")
    # An id that is gone from the server is dropped from the list; junk in the list is dropped too.
    sync.set_failed_ids(con, "2026", ["TCC-GONE", "not an id"]); con.commit()
    assert sync.retry_failed(con, "2026") == (0, 0, 1)
    assert sync.failed_ids(con, "2026") == []


def test_failed_list_is_capped(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    sync.set_failed_ids(con, "2026", [f"TCC-{i:05d}" for i in range(sync.FAILED_CAP + 3)])
    ids = sync.failed_ids(con, "2026")
    assert len(ids) == sync.FAILED_CAP and ids[0] == "TCC-00003" and ids[-1] == f"TCC-{sync.FAILED_CAP + 2:05d}"
    con.execute("UPDATE meta SET value='not json' WHERE key='failed:2026'")
    assert sync.failed_ids(con, "2026") == []


def test_delete_server_and_delete_failed_need_a_typed_confirmation(monkeypatch, tmp_path):
    ident = pyrage.x25519.Identity.generate(); other = pyrage.x25519.Identity.generate()
    rows = {"2026": [
        row("TCC-GOOD", "2026-10-20T10:00:00.000Z", ident, good_payload("Buena")),
        row("TCC-J1", "2026-10-20T10:00:01.000Z", other, "junk"),
        row("TCC-J2", "2026-10-20T10:00:02.000Z", other, "junk"),
    ], "preview": []}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    monkeypatch.setattr(cli, "connect", lambda: con)
    assert sync.pull(con) == (1, 2)
    assert sync.failed_ids(con, "2026") == ["TCC-J1", "TCC-J2"]
    deletes = lambda: [c for c in srv.calls if c[0] == "DELETE"]  # noqa: E731
    # delete-server: the wrong id typed aborts and sends nothing.
    monkeypatch.setattr(builtins, "input", lambda prompt="": "TCC-J2")
    with pytest.raises(SystemExit, match="aborted"):
        cli.main(["delete-server", "TCC-J1"])
    assert deletes() == []
    monkeypatch.setattr(builtins, "input", lambda prompt="": "TCC-J1")
    cli.main(["delete-server", "TCC-J1"])
    assert deletes() == [("DELETE", "TCC-J1")]
    assert [r["id"] for r in rows["2026"]] == ["TCC-GOOD", "TCC-J2"]
    assert sync.failed_ids(con, "2026") == ["TCC-J2"]
    assert con.execute("SELECT status, resolution FROM tasks WHERE title='Could not read submission TCC-J1'").fetchone()[:] == ("done", "deleted from server")
    # delete-failed: the season must be typed back; then every remaining failed id goes.
    sync.set_failed_ids(con, "2026", ["TCC-J2", "TCC-ALREADY-GONE"]); con.commit()
    monkeypatch.setattr(builtins, "input", lambda prompt="": "2025")
    with pytest.raises(SystemExit, match="aborted"):
        cli.main(["delete-failed", "--season", "2026"])
    assert deletes() == [("DELETE", "TCC-J1")]
    monkeypatch.setattr(builtins, "input", lambda prompt="": "2026")
    cli.main(["delete-failed", "--season", "2026"])
    assert deletes() == [("DELETE", "TCC-J1"), ("DELETE", "TCC-J2"), ("DELETE", "TCC-ALREADY-GONE")]
    assert [r["id"] for r in rows["2026"]] == ["TCC-GOOD"]
    assert sync.failed_ids(con, "2026") == []
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE kind='decrypt_error' AND status='open'").fetchone()[0] == 0
    assert con.execute("SELECT detail FROM events WHERE kind='delete_failed'").fetchone()[0] == "tried=2 deleted=1"
    # Nothing left to delete: says so instead of asking.
    with pytest.raises(SystemExit, match="no unreadable rows"):
        cli.main(["delete-failed", "--season", "2026"])
    # The good row was never touched.
    assert con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
    # An id that is not an id never reaches the server.
    with pytest.raises(ValueError):
        sync.delete_server(con, "../season/2026")


def test_bad_shape_is_a_bad_payload(monkeypatch, tmp_path):
    ident = pyrage.x25519.Identity.generate()
    rows = {"2026": [
        row("TCC-LIST", "2026-10-20T10:00:00.000Z", ident, '["not", "an", "object"]'),
        row("TCC-ODD", "2026-10-20T11:00:00.000Z", ident, json.dumps({"applicant": {"first_name": 5, "phone": ["x"]}, "children": "none", "programs": 3, "address": "1 Elm"})),
    ], "preview": []}
    con, _, _ = setup(monkeypatch, tmp_path, rows, ident)
    assert sync.pull(con) == (1, 1)  # the odd but object-shaped row is stored; the list is not
    norm = json.loads(con.execute("SELECT norm FROM applications WHERE id='TCC-ODD'").fetchone()[0])
    assert norm["n_children"] == 0 and norm["phones"] == [] and norm["first"] == "" and norm["addr_key"] == ""


def test_preview_rows_in_window_become_tasks(monkeypatch, tmp_path):
    ident = pyrage.x25519.Identity.generate(); other = pyrage.x25519.Identity.generate()
    rows = {"2026": [], "preview": [
        row("TCC-PV-IN", "2026-10-20T10:00:00.000Z", ident, good_payload("Dentro")),
        row("TCC-PV-OUT", "2026-09-01T10:00:00.000Z", ident, good_payload("Fuera")),
        row("TCC-PV-BAD", "2026-10-21T10:00:00.000Z", other, good_payload("Rota")),
    ]}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    assert sync.pull(con) == (2, 1)
    assert [c[1] for c in srv.calls] == ["2026", "preview"]
    assert con.execute("SELECT season FROM applications WHERE id='TCC-PV-IN'").fetchone()[0] == "preview"
    t = con.execute("SELECT * FROM tasks WHERE kind='preview_in_window' ORDER BY app_id").fetchall()
    assert [(x["app_id"]) for x in t] == [None, "TCC-PV-IN"]
    assert all("reapply" in x["title"] for x in t)
    sync.pull(con)  # again: still one task per row
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE kind='preview_in_window'").fetchone()[0] == 2
    ids, open_tasks = sync.preview_rows_in_window(con, CFG)
    assert ids == ["TCC-PV-IN"] and open_tasks == 2


def test_dismissed_preview_task_is_not_raised_again(monkeypatch, tmp_path):
    ident = pyrage.x25519.Identity.generate(); other = pyrage.x25519.Identity.generate()
    rows = {"2026": [], "preview": [
        row("TCC-PV-IN", "2026-10-20T10:00:00.000Z", ident, good_payload("Dentro")),
        row("TCC-PV-BAD", "2026-10-21T10:00:00.000Z", other, good_payload("Rota")),
    ]}
    con, _, _ = setup(monkeypatch, tmp_path, rows, ident)
    sync.pull(con)
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE kind='preview_in_window'").fetchone()[0] == 2
    con.execute("UPDATE tasks SET status='dismissed' WHERE kind='preview_in_window'"); con.commit()
    sync.pull(con)  # nothing new from the cursor
    sync.reset_cursor(con, "preview"); sync.pull(con)  # every row fetched again, readable and not
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE kind='preview_in_window'").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE kind='preview_in_window' AND status='dismissed'").fetchone()[0] == 2


def test_pull_preview_only(monkeypatch, tmp_path):
    ident = pyrage.x25519.Identity.generate()
    rows = {"2026": [row("TCC-REAL", "2026-10-20T10:00:00.000Z", ident, good_payload("Real"), lang="en")],
            "preview": [row("TCC-PV", "2026-09-20T10:00:00.000Z", ident, good_payload("Prev"), lang="en")]}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    assert sync.pull(con, season="preview") == (1, 0)
    assert [c[1] for c in srv.calls] == ["preview"]
    assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_purge_local_forgets_the_cursor_and_the_failed_list(monkeypatch, tmp_path):
    ident = pyrage.x25519.Identity.generate(); other = pyrage.x25519.Identity.generate()
    rows = {"2026": [row("TCC-OK", "2026-10-20T10:00:00.000Z", ident, good_payload("Ok")), row("TCC-NO", "2026-10-20T11:00:00.000Z", other, "x")], "preview": []}
    con, _, _ = setup(monkeypatch, tmp_path, rows, ident)
    sync.pull(con)
    assert {r[0] for r in con.execute("SELECT key FROM meta")} >= {"since:2026", "since_id:2026", "failed:2026"}
    cli.purge_local(con, "2026")
    assert {r[0] for r in con.execute("SELECT key FROM meta") if r[0].endswith(":2026")} == set()


def test_load_identities_reads_every_key_file(monkeypatch, tmp_path):
    monkeypatch.delenv("TCC_KEY_FILE", raising=False)
    monkeypatch.setattr(sync, "KEY_DIR", tmp_path)
    with pytest.raises(sync.ReviewSetupError, match="keygen"):
        sync.load_identities()
    a = pyrage.x25519.Identity.generate(); b = pyrage.x25519.Identity.generate()
    (tmp_path / "season-2026.key").write_text(f"# comment\n{a}\n")
    (tmp_path / "lynette-2026.key").write_text(f"{b}\n")
    (tmp_path / "admin-token").write_text("not a key\n")
    got = {str(i.to_public()) for i in sync.load_identities()}
    assert got == {str(a.to_public()), str(b.to_public())}
    monkeypatch.setenv("TCC_KEY_FILE", str(tmp_path / "lynette-2026.key"))
    assert [str(i.to_public()) for i in sync.load_identities()] == [str(b.to_public())]


def test_admin_token_is_file_only(monkeypatch, tmp_path):
    monkeypatch.setenv("TCC_ADMIN_TOKEN", "from-env")
    monkeypatch.setattr(sync, "KEY_DIR", tmp_path)
    with pytest.raises(sync.ReviewSetupError, match="set-secret"):
        sync.admin_token()
    (tmp_path / "admin-token").write_text("from-file\n")
    assert sync.admin_token() == "from-file"


def test_cli_turns_a_setup_error_into_an_exit_message(monkeypatch, tmp_path):
    """The command line exits with the message; the exception type is for the console."""
    monkeypatch.delenv("TCC_KEY_FILE", raising=False)
    monkeypatch.setattr(sync, "KEY_DIR", tmp_path / "empty")
    monkeypatch.setattr(sync, "load_config", lambda: dict(CFG))
    monkeypatch.setattr(sync, "api", lambda *a, **k: pytest.fail("no key, so the server must not be called"))
    monkeypatch.setattr(cli, "connect", lambda: connect(tmp_path / "t.sqlite"))
    assert issubclass(sync.ReviewSetupError, RuntimeError)
    with pytest.raises(SystemExit) as ex:
        cli.main(["pull"])
    assert "keygen" in str(ex.value)


# --- edits (supersedes), pushing, and a second Mac ------------------------------------------

def app_payload(first="Rosa", last="Lopez", phone="5305550100", street="1 Elm", zip_="96161", kids=(("Ana", 4, "girl"),), adults=2):
    return {"version": 1, "applicant": {"first_name": first, "last_name": last, "phone": phone, "contact_lang": "es"},
            "address": {"street": street, "unit": "", "city": "Truckee", "zip": zip_, "in_area": zip_ in ("96161", "96160", "96162", "95728")},
            "mailing": None, "household": {"adults": adults, "adult_coat_sizes": []},
            "children": [{"first_name": n, "age": a, "sex": s, "coat": False} for n, a, s in kids], "programs": {"food": True, "toys": True}}


def local(con, sid):
    r = con.execute("SELECT status, note, server_status, server_note, family_id, supersedes, prior_status, prior_note FROM applications WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None


def open_tasks(con, **where):
    sql = "SELECT * FROM tasks WHERE status='open'" + "".join(f" AND {k}=?" for k in where)
    return [dict(r) for r in con.execute(sql, tuple(where.values())).fetchall()]


def supersede_on_server(srv, old_id, new_row):
    """What the Worker does when an edit lands: the new row names the old one, the old one is superseded."""
    srv.rows["2026"].append(new_row)
    _, r = srv.find(old_id); r["status"] = "superseded"


def test_edit_of_a_decided_application_starts_over_with_a_task(monkeypatch, tmp_path):
    """The old row is superseded (open tasks dismissed, pairs void, nothing pushed for it
    again). The replacement keeps the family, records the old decision and note, starts as
    'new' with one review_edit task naming the fields that changed, and is matched."""
    from review.db import add_task
    from review.match import run_matching, set_decision
    ident = pyrage.x25519.Identity.generate()
    p = app_payload()
    rows = {"2026": [row("TCC-26-AAAAA", "2026-10-20T10:00:00Z", ident, json.dumps(p))], "preview": []}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    assert sync.pull(con) == (1, 0)
    run_matching(con, "2026", use_judge=False)
    con.execute("INSERT INTO families(id,created_at,display_name) VALUES('fam-1','x','Lopez, Rosa')")
    con.execute("UPDATE applications SET family_id='fam-1' WHERE id='TCC-26-AAAAA'")
    set_decision(con, "TCC-26-AAAAA", "accepted", "ok")
    add_task(con, "verify_address", "check", app_id="TCC-26-AAAAA"); con.commit()
    assert sync.push_statuses(con) == 1 and srv.status("TCC-26-AAAAA") == ("accepted", "ok")
    edited = app_payload(street="2 Oak", kids=(("Ana", 4, "girl"), ("Luis", 7, "boy")))
    supersede_on_server(srv, "TCC-26-AAAAA", row("TCC-26-BBBBB", "2026-10-20T11:00:00Z", ident, json.dumps(edited), supersedes="TCC-26-AAAAA"))
    assert sync.pull(con) == (1, 0)
    old, new = local(con, "TCC-26-AAAAA"), local(con, "TCC-26-BBBBB")
    assert (old["status"], old["server_status"]) == ("superseded", "superseded")
    assert (new["status"], new["family_id"], new["supersedes"], new["prior_status"], new["prior_note"], new["note"]) == ("new", "fam-1", "TCC-26-AAAAA", "accepted", "ok", "")
    assert open_tasks(con, app_id="TCC-26-AAAAA") == []
    assert con.execute("SELECT resolution FROM tasks WHERE app_id='TCC-26-AAAAA'").fetchone()[0] == "application was edited"
    (t,) = open_tasks(con, app_id="TCC-26-BBBBB")
    assert t["kind"] == "review_edit" and t["title"] == "Edited after accepted: re-check"
    assert "street" in t["detail"] and "children count" in t["detail"] and "child names" in t["detail"] and "phone" not in t["detail"]
    assert "was accepted" in t["detail"] and "Old note: ok" in t["detail"]
    for table in ("events", "tasks"):  # names, streets and the like never leave the applications table
        for r in con.execute(f"SELECT * FROM {table}"):
            assert "Oak" not in json.dumps(dict(r)) and "Luis" not in json.dumps(dict(r))
    stats = run_matching(con, "2026", use_judge=False)
    assert stats["apps"] == 1 and stats["pairs"] == 0  # the replacement is examined; the dead row is never a candidate
    assert local(con, "TCC-26-BBBBB")["status"] == "matched"
    # Push: the old row is left alone (the server set superseded); the new one is reported as fetched.
    assert sync.push_statuses(con) == 1 and srv.patches[-1] == ("TCC-26-BBBBB", "fetched", "")
    assert srv.status("TCC-26-AAAAA") == ("superseded", "ok")
    set_decision(con, "TCC-26-BBBBB", "accepted", "")
    assert sync.push_statuses(con) == 1 and srv.status("TCC-26-BBBBB") == ("accepted", "")
    assert con.execute("SELECT status, resolution FROM tasks WHERE id=?", (t["id"],)).fetchone()[:] == ("done", "decided: accepted")
    assert sync.push_statuses(con) == 0


def test_needs_info_answered_by_an_edit_gets_a_task_with_the_question(monkeypatch, tmp_path):
    from review.match import run_matching, set_decision
    ident = pyrage.x25519.Identity.generate()
    rows = {"2026": [row("TCC-26-AAAAA", "2026-10-20T10:00:00Z", ident, json.dumps(app_payload()))], "preview": []}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    sync.pull(con); run_matching(con, "2026", use_judge=False)
    set_decision(con, "TCC-26-AAAAA", "needs_info", "traiga comprobante de domicilio")
    assert sync.push_statuses(con) == 1
    supersede_on_server(srv, "TCC-26-AAAAA", row("TCC-26-BBBBB", "2026-11-20T11:00:00Z", ident, json.dumps(app_payload(zip_="96160")), supersedes="TCC-26-AAAAA"))
    sync.pull(con)  # an answer may arrive after the season closes; it is an edit like any other
    new = local(con, "TCC-26-BBBBB")
    assert (new["status"], new["note"], new["prior_status"], new["prior_note"]) == ("new", "", "needs_info", "traiga comprobante de domicilio")
    (t,) = open_tasks(con, app_id="TCC-26-BBBBB")
    assert t["title"] == "Answered needs_info by editing" and "We asked: traiga comprobante de domicilio" in t["detail"] and "zip" in t["detail"]
    assert run_matching(con, "2026", use_judge=False)["apps"] == 1


def test_resend_without_changes_skips_matching_and_keeps_the_pair(monkeypatch, tmp_path):
    """The family tapped Replace and send without changing anything: the new row takes over
    the old row's candidate pair and its open duplicate task, is not matched again, and
    gets one low-priority task."""
    from review.match import run_matching
    ident = pyrage.x25519.Identity.generate()
    p = app_payload(kids=(("Ana", 4, "girl"), ("Luis", 7, "boy")))
    other = app_payload(first="Jose", kids=(("Ana", 4, "girl"), ("Luis", 7, "boy")))  # same phone, address and children: a certain duplicate
    rows = {"2026": [row("TCC-26-AAAAA", "2026-10-20T10:00:00Z", ident, json.dumps(p)), row("TCC-26-XXXXX", "2026-10-20T10:30:00Z", ident, json.dumps(other))], "preview": []}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    sync.pull(con)
    stats = run_matching(con, "2026", use_judge=False)
    assert stats["pairs"] == 1 and len(open_tasks(con, kind="resolve_duplicate")) == 1
    supersede_on_server(srv, "TCC-26-AAAAA", row("TCC-26-BBBBB", "2026-10-20T11:00:00Z", ident, json.dumps(p), supersedes="TCC-26-AAAAA"))
    sync.pull(con)
    assert local(con, "TCC-26-BBBBB")["status"] == "matched" and local(con, "TCC-26-AAAAA")["status"] == "superseded"
    c = con.execute("SELECT app_a, app_b, verdict FROM candidates").fetchall()
    assert [tuple(x) for x in c] == [("TCC-26-BBBBB", "TCC-26-XXXXX", "same")]
    dup = open_tasks(con, kind="resolve_duplicate")
    assert len(dup) == 1 and dup[0]["app_id"] == "TCC-26-BBBBB"
    (t,) = open_tasks(con, kind="review_edit")
    assert t["title"] == "Resent without changes" and t["app_id"] == "TCC-26-BBBBB" and "was matched" in t["detail"]
    stats = run_matching(con, "2026", use_judge=False)
    assert stats["apps"] == 0 and stats["pairs"] == 0  # nothing new to examine, nothing re-scored
    assert len(open_tasks(con)) == 2


def test_tasks_from_the_other_side_of_a_pair_are_dismissed_and_the_pair_voided(monkeypatch, tmp_path):
    from review.db import add_task
    from review.match import run_matching
    ident = pyrage.x25519.Identity.generate()
    kids = (("Ana", 4, "girl"), ("Luis", 7, "boy"))
    rows = {"2026": [row("TCC-26-AAAAA", "2026-10-20T10:00:00Z", ident, json.dumps(app_payload(kids=kids))),
                     row("TCC-26-XXXXX", "2026-10-20T10:30:00Z", ident, json.dumps(app_payload(first="Jose", kids=kids)))], "preview": []}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    sync.pull(con); run_matching(con, "2026", use_judge=False)
    cid = con.execute("SELECT id FROM candidates").fetchone()[0]
    add_task(con, "review_match", "asked from the other side", app_id="TCC-26-XXXXX", candidate_id=cid); con.commit()
    assert len(open_tasks(con)) == 2
    supersede_on_server(srv, "TCC-26-AAAAA", row("TCC-26-BBBBB", "2026-10-20T11:00:00Z", ident, json.dumps(app_payload(street="9 Pine", kids=kids)), supersedes="TCC-26-AAAAA"))
    sync.pull(con)
    assert open_tasks(con) == []
    assert {r[0] for r in con.execute("SELECT resolution FROM tasks")} == {"application was edited"}
    assert con.execute("SELECT verdict, decided_by FROM candidates WHERE id=?", (cid,)).fetchone()[:] == ("void", "system")
    stats = run_matching(con, "2026", use_judge=False)
    assert stats["pairs"] == 1  # a fresh pair for the replacement, one task
    assert [t["app_id"] for t in open_tasks(con, kind="resolve_duplicate")] == ["TCC-26-BBBBB"]


def test_superseded_row_arriving_after_its_replacement_is_dead_on_arrival(monkeypatch, tmp_path):
    from review.match import run_matching
    ident = pyrage.x25519.Identity.generate(); other = pyrage.x25519.Identity.generate(); third = pyrage.x25519.Identity.generate()
    rows = {"2026": [
        row("TCC-26-AAAAA", "2026-10-20T10:00:00Z", other, json.dumps(app_payload())),   # unreadable here for now; an older worker left its status 'new'
        row("TCC-26-BBBBB", "2026-10-20T11:00:00Z", ident, json.dumps(app_payload(street="2 Oak")), supersedes="TCC-26-AAAAA"),
        row("TCC-26-CCCCC", "2026-10-20T12:00:00Z", third, json.dumps(app_payload(first="Eva")), status="superseded"),  # replaced on the server; its edit never reached us
    ], "preview": []}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    assert sync.pull(con) == (1, 2)
    assert local(con, "TCC-26-BBBBB")["status"] == "new" and local(con, "TCC-26-BBBBB")["prior_status"] is None
    # An unreadable row the server already superseded asks nobody for anything.
    assert sync.failed_ids(con, "2026") == ["TCC-26-AAAAA"]
    assert [t["title"] for t in open_tasks(con, kind="decrypt_error")] == ["Could not read submission TCC-26-AAAAA"]
    # The key arrives: the old row is stored dead, because its replacement is already here.
    monkeypatch.setattr(sync, "load_identities", lambda: [ident, other, third])
    assert sync.retry_failed(con, "2026") == (1, 0, 0)
    assert (local(con, "TCC-26-AAAAA")["status"], local(con, "TCC-26-AAAAA")["server_status"]) == ("superseded", "superseded")
    sync.reset_cursor(con, "2026"); sync.pull(con)
    assert local(con, "TCC-26-CCCCC")["status"] == "superseded"
    assert con.execute("SELECT detail FROM events WHERE kind='superseded' AND ref='TCC-26-CCCCC'").fetchone()[0] == "replaced on the server"
    stats = run_matching(con, "2026", use_judge=False)
    assert stats["apps"] == 1 and stats["pairs"] == 0
    assert sync.push_statuses(con) == 1 and [p[0] for p in srv.patches] == ["TCC-26-BBBBB"]


def test_second_mac_inherits_decisions_and_pushes_only_its_own_changes(monkeypatch, tmp_path):
    from review.match import run_matching, set_decision
    ident = pyrage.x25519.Identity.generate()
    rows = {"2026": [row(f"TCC-{x}", f"2026-10-20T1{i}:00:00Z", ident, json.dumps(app_payload(first=x, phone=f"530555010{i}", street=f"{i} Elm"))) for i, x in enumerate("ABC")], "preview": []}
    conA, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    long_note = "  traiga comprobante de domicilio  " + "x" * 300
    assert sync.pull(conA) == (3, 0)
    run_matching(conA, "2026", use_judge=False)
    set_decision(conA, "TCC-A", "accepted", "ok"); set_decision(conA, "TCC-B", "needs_info", long_note)
    assert sync.push_statuses(conA) == 3
    note200 = long_note.strip()[:200]
    assert srv.status("TCC-A") == ("accepted", "ok") and srv.status("TCC-B") == ("needs_info", note200) and srv.status("TCC-C") == ("fetched", "")
    assert sync.push_statuses(conA) == 0
    # A second Mac with a fresh database pulls the same server: it inherits the decisions.
    conB = connect(tmp_path / "b.sqlite")
    assert sync.pull(conB) == (3, 0)
    a, b, c = local(conB, "TCC-A"), local(conB, "TCC-B"), local(conB, "TCC-C")
    assert (a["status"], a["note"], a["server_status"], a["server_note"]) == ("accepted", "ok", "accepted", "ok")
    assert (b["status"], b["note"]) == ("needs_info", note200)
    assert (c["status"], c["note"], c["server_status"]) == ("new", "", "fetched")
    run_matching(conB, "2026", use_judge=False)
    before = len(srv.patches)
    assert sync.push_statuses(conB) == 0 and len(srv.patches) == before  # nothing to say: nothing changed here
    assert srv.status("TCC-A") == ("accepted", "ok") and srv.status("TCC-B") == ("needs_info", note200)
    # One decision on the second Mac: only that row goes.
    set_decision(conB, "TCC-C", "declined", "")
    assert sync.push_statuses(conB) == 1 and srv.patches[before:] == [("TCC-C", "declined", "")]
    # A row this Mac never decided is never pushed over a server decision, even with a stale local status.
    conB.execute("UPDATE applications SET status='matched', note='' WHERE id='TCC-A'"); conB.commit()
    assert sync.push_statuses(conB) == 0 and srv.status("TCC-A") == ("accepted", "ok")
    # The first Mac's cache says C is fetched: push sends nothing; resync brings the decision in.
    assert sync.push_statuses(conA) == 0
    st = sync.resync(conA)
    assert st["inherited"] == 1 and st["refreshed"] == 3 and st["missing"] == 0
    c = local(conA, "TCC-C")
    assert (c["status"], c["server_status"]) == ("declined", "declined")
    # A different local decision stands unless --overwrite is asked for.
    set_decision(conB, "TCC-A", "declined", "no")
    assert sync.resync(conB)["overwritten"] == 0 and local(conB, "TCC-A")["status"] == "declined"
    assert sync.resync(conB, overwrite=True)["overwritten"] == 1
    assert (local(conB, "TCC-A")["status"], local(conB, "TCC-A")["note"]) == ("accepted", "ok")
    assert sync.push_statuses(conB) == 0


def test_push_sends_note_changes_and_never_touches_superseded_rows(monkeypatch, tmp_path):
    from review.match import run_matching, set_decision, set_note
    ident = pyrage.x25519.Identity.generate()
    rows = {"2026": [row("TCC-A", "2026-10-20T10:00:00Z", ident, json.dumps(app_payload())),
                     row("TCC-B", "2026-10-20T11:00:00Z", ident, json.dumps(app_payload(first="Eva", phone="5305550199", street="4 Fir")))], "preview": []}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    sync.pull(con); run_matching(con, "2026", use_judge=False)
    set_decision(con, "TCC-A", "needs_info", "uno")
    assert sync.push_statuses(con) == 2 and srv.status("TCC-A") == ("needs_info", "uno")
    set_decision(con, "TCC-A", "needs_info", "dos")
    assert sync.push_statuses(con) == 1 and srv.patches[-1] == ("TCC-A", "needs_info", "dos")
    set_note(con, "TCC-A", "tres")
    assert sync.push_statuses(con) == 1 and srv.status("TCC-A") == ("needs_info", "tres") and local(con, "TCC-A")["status"] == "needs_info"
    assert sync.push_statuses(con) == 0
    # 'superseded' is the server's word: a row marked so here is never sent.
    con.execute("UPDATE applications SET status='superseded' WHERE id='TCC-A'"); con.commit()
    n = len(srv.patches)
    assert sync.push_statuses(con) == 0 and len(srv.patches) == n
    # The family replaced B on the server since the last pull: the worker answers 409, the
    # local row is retired, the push goes on, and nothing on the server moved.
    supersede_on_server(srv, "TCC-B", row("TCC-B2", "2026-10-21T11:00:00Z", ident, json.dumps(app_payload(first="Eva", phone="5305550199", street="5 Fir")), supersedes="TCC-B"))
    set_decision(con, "TCC-B", "accepted", "")
    assert sync.push_statuses(con) == 0
    assert srv.patches[-1] == ("TCC-B", "accepted", "") and srv.status("TCC-B") == ("superseded", "")
    b = local(con, "TCC-B")
    assert (b["status"], b["server_status"]) == ("superseded", "superseded")
    assert con.execute("SELECT detail FROM events WHERE kind='superseded' AND ref='TCC-B'").fetchone()[0] == "replaced by TCC-B2"
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='push_skipped' AND ref='TCC-B'").fetchone()[0] == 1
    assert sync.pull(con) == (1, 0) and local(con, "TCC-B2")["status"] == "new"


def test_server_status_map_covers_every_console_status():
    from review.console import FILTER_STATUSES
    worker_words = {"new", "fetched", "needs_info", "accepted", "declined", "duplicate", "out_of_area", "superseded"}  # STATUSES in worker/src/index.js
    assert set(FILTER_STATUSES) <= set(sync.SERVER_STATUS)
    assert set(sync.SERVER_STATUS.values()) <= worker_words
    assert set(sync.DECIDED) <= worker_words and "fetched" not in sync.DECIDED and "new" not in sync.DECIDED


def test_cli_resync_reports_counts(monkeypatch, tmp_path, capsys):
    from review.match import set_decision
    ident = pyrage.x25519.Identity.generate()
    rows = {"2026": [row("TCC-A", "2026-10-20T10:00:00Z", ident, json.dumps(app_payload()), status="accepted", note="ok")], "preview": []}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    monkeypatch.setattr(cli, "connect", lambda: con)
    sync.pull(con)
    assert (local(con, "TCC-A")["status"], local(con, "TCC-A")["note"]) == ("accepted", "ok")  # seeded from the server
    set_decision(con, "TCC-A", "declined", "")
    cli.main(["resync"])
    assert "0 overwritten" in capsys.readouterr().out and local(con, "TCC-A")["status"] == "declined"
    cli.main(["resync", "--overwrite", "--season", "2026"])
    assert "1 overwritten" in capsys.readouterr().out and local(con, "TCC-A")["status"] == "accepted"
    assert sync.push_statuses(con) == 0
