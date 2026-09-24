"""pull(): one unreadable row never blocks the season, the cursor stops at the first
failure, preview rows submitted during the season become tasks, and nothing decrypted
ever reaches the events or tasks tables."""
import base64
import json
import os
import sys
import urllib.parse
from pathlib import Path

import pyrage

os.environ["TCC_JUDGE"] = "none"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from review import sync  # noqa: E402
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


class Server:
    """Stands in for the Worker: rows per season, filtered by since like the real query."""
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def api(self, method, path, body=None, headers=None):
        u = urllib.parse.urlparse(path); q = urllib.parse.parse_qs(u.query)
        season = q.get("season", [""])[0]; since = q.get("since", [""])[0]
        self.calls.append((method, season, since))
        items = [r for r in self.rows.get(season, []) if r["created_at"] > since]
        return {"season": season, "items": sorted(items, key=lambda r: r["created_at"])}


def setup(monkeypatch, tmp_path, rows, ident=None):
    ident = ident or pyrage.x25519.Identity.generate()
    srv = Server(rows)
    monkeypatch.setattr(sync, "load_config", lambda: dict(CFG))
    monkeypatch.setattr(sync, "load_identities", lambda: [ident])
    monkeypatch.setattr(sync, "api", srv.api)
    return connect(tmp_path / "t.sqlite"), ident, srv


def test_bad_row_does_not_block_and_cursor_stops_before_it(monkeypatch, tmp_path):
    ident = pyrage.x25519.Identity.generate()
    rows = {"2026": [
        {"id": "TCC-A1", "created_at": "2026-10-20T10:00:00.000Z", "lang": "es", "status": "new", "ciphertext": encrypt(ident, good_payload("Ana"))},
        {"id": "TCC-B2", "created_at": "2026-10-20T11:00:00.000Z", "lang": "es", "status": "new", "ciphertext": encrypt(ident, "not json " + SECRET)},
        {"id": "TCC-C3", "created_at": "2026-10-20T12:00:00.000Z", "lang": "en", "status": "new", "ciphertext": encrypt(ident, good_payload("Carla"))},
    ], "preview": []}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    assert sync.pull(con) == (2, 1)
    ids = {r[0] for r in con.execute("SELECT id FROM applications")}
    assert ids == {"TCC-A1", "TCC-C3"}
    assert con.execute("SELECT value FROM meta WHERE key='since:2026'").fetchone()[0] == "2026-10-20T10:00:00.000Z"
    tasks = con.execute("SELECT * FROM tasks WHERE kind='decrypt_error'").fetchall()
    assert len(tasks) == 1 and tasks[0]["title"] == "Could not read submission TCC-B2" and tasks[0]["app_id"] is None
    # second pull: the bad row is fetched again, nothing new, still one task
    assert sync.pull(con) == (0, 1)
    assert srv.calls[-2] == ("GET", "2026", "2026-10-20T10:00:00.000Z")
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE kind='decrypt_error'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 2
    # the plaintext never reached the database outside the applications table
    for table in ("events", "tasks"):
        for r in con.execute(f"SELECT * FROM {table}"):
            assert SECRET not in json.dumps(dict(r))
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='bad_payload' AND ref='TCC-B2'").fetchone()[0] == 2


def test_wrong_key_row_is_a_decrypt_error(monkeypatch, tmp_path):
    ident = pyrage.x25519.Identity.generate(); other = pyrage.x25519.Identity.generate()
    rows = {"2026": [{"id": "TCC-OLD", "created_at": "2026-10-01T00:00:00.000Z", "lang": "es", "ciphertext": encrypt(other, good_payload("Vieja"))}], "preview": []}
    con, _, _ = setup(monkeypatch, tmp_path, rows, ident)
    assert sync.pull(con) == (0, 1)
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='decrypt_error' AND ref='TCC-OLD'").fetchone()[0] == 1
    assert con.execute("SELECT value FROM meta WHERE key='since:2026'").fetchone() is None


def test_bad_shape_is_a_bad_payload(monkeypatch, tmp_path):
    ident = pyrage.x25519.Identity.generate()
    rows = {"2026": [
        {"id": "TCC-LIST", "created_at": "2026-10-20T10:00:00.000Z", "lang": "es", "ciphertext": encrypt(ident, '["not", "an", "object"]')},
        {"id": "TCC-ODD", "created_at": "2026-10-20T11:00:00.000Z", "lang": "es", "ciphertext": encrypt(ident, json.dumps({"applicant": {"first_name": 5, "phone": ["x"]}, "children": "none", "programs": 3, "address": "1 Elm"}))},
    ], "preview": []}
    con, _, _ = setup(monkeypatch, tmp_path, rows, ident)
    assert sync.pull(con) == (1, 1)  # the odd but object-shaped row is stored; the list is not
    norm = json.loads(con.execute("SELECT norm FROM applications WHERE id='TCC-ODD'").fetchone()[0])
    assert norm["n_children"] == 0 and norm["phones"] == [] and norm["first"] == "" and norm["addr_key"] == ""


def test_preview_rows_in_window_become_tasks(monkeypatch, tmp_path):
    ident = pyrage.x25519.Identity.generate(); other = pyrage.x25519.Identity.generate()
    rows = {"2026": [], "preview": [
        {"id": "TCC-PV-IN", "created_at": "2026-10-20T10:00:00.000Z", "lang": "es", "ciphertext": encrypt(ident, good_payload("Dentro"))},
        {"id": "TCC-PV-OUT", "created_at": "2026-09-01T10:00:00.000Z", "lang": "es", "ciphertext": encrypt(ident, good_payload("Fuera"))},
        {"id": "TCC-PV-BAD", "created_at": "2026-10-21T10:00:00.000Z", "lang": "es", "ciphertext": encrypt(other, good_payload("Rota"))},
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


def test_pull_preview_only(monkeypatch, tmp_path):
    ident = pyrage.x25519.Identity.generate()
    rows = {"2026": [{"id": "TCC-REAL", "created_at": "2026-10-20T10:00:00.000Z", "lang": "en", "ciphertext": encrypt(ident, good_payload("Real"))}],
            "preview": [{"id": "TCC-PV", "created_at": "2026-09-20T10:00:00.000Z", "lang": "en", "ciphertext": encrypt(ident, good_payload("Prev"))}]}
    con, _, srv = setup(monkeypatch, tmp_path, rows, ident)
    assert sync.pull(con, season="preview") == (1, 0)
    assert [c[1] for c in srv.calls] == ["preview"]
    assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_load_identities_reads_every_key_file(monkeypatch, tmp_path):
    monkeypatch.delenv("TCC_KEY_FILE", raising=False)
    monkeypatch.setattr(sync, "KEY_DIR", tmp_path)
    import pytest
    with pytest.raises(SystemExit, match="keygen"):
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
    import pytest
    with pytest.raises(SystemExit, match="set-secret"):
        sync.admin_token()
    (tmp_path / "admin-token").write_text("from-file\n")
    assert sync.admin_token() == "from-file"
