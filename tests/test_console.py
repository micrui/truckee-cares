"""The console renders hostile and malformed payloads without executing or crashing,
its exports are what a spreadsheet expects, and its server refuses cross-site POSTs."""
import http.client
import json
import os
import sys
import threading
import urllib.parse
from http.server import HTTPServer
from pathlib import Path

os.environ["TCC_JUDGE"] = "none"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from review import console  # noqa: E402
from review.db import add_task, connect  # noqa: E402
from review.normalize_app import normalize_payload  # noqa: E402

X = "<img src=x onerror=alert(1)>"


def hostile_payload():
    """Every string, and every dict key, is the attack. Several values have the wrong type."""
    return {"version": X, "season": X, "helper": {X: X, "name": X, "phone": X, "org": X},
            "applicant": {"first_name": X, "last_name": X, "phone": X, "other_phone": X, "email": X, "other_adult": X, "contact_lang": X, "can_text": X, X: X},
            "address": {"street": X, "unit": X, "city": X, "zip": X, "in_area": X, X: X},
            "mailing": {"street": X, "city": X, "zip": X, X: X},
            "household": {"adults": {"n": X}, "adult_coat_sizes": [X, 3, None, {X: X}], X: X},
            "children": [{"first_name": X, "age": {"a": X}, "sex": X, "coat": X, "coat_size": X, X: X}, X, 7, None],
            "children_raw": X, "programs": {X: True, "food": X, "toys": [X], "coats": {X: X}}, "referral": X, "notes": X}


def wrong_types_payload():
    return {"applicant": {"first_name": 12, "last_name": None, "phone": 5305550100}, "address": "1 Elm St", "mailing": [], "helper": "yes",
            "household": "two", "children": "Ana 4, Luis 7", "programs": ["food"], "notes": {"a": 1}}


def insert(con, app_id, p, season="2026", status="new", lang="es"):
    con.execute("INSERT INTO applications(id,season,source,submitted_at,lang,status,payload,norm) VALUES(?,?,?,?,?,?,?,?)",
                (app_id, season, "web", f"{season}-10-20T00:00:00Z", lang, status, json.dumps(p, ensure_ascii=False), json.dumps(normalize_payload(p))))


def make_db(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    insert(con, "H1", hostile_payload(), status="accepted")
    insert(con, "H2", hostile_payload(), season="2025", status="accepted")
    insert(con, "W1", wrong_types_payload(), status="accepted")
    con.execute("INSERT INTO candidates(id,app_a,app_b,score,reasons,verdict,decided_by,judge) VALUES('c1','H1','H2',0.7,?,'unsure','llm',?)", (json.dumps([X]), json.dumps({"reason": X})))
    add_task(con, "review_match", f"Same family? {X}", app_id="H1", candidate_id="c1", detail=X)
    add_task(con, "review_notes", X, app_id="W1", detail=X)
    add_task(con, "decrypt_error", "Could not read submission " + X, detail=X)
    con.execute("INSERT INTO families(id,created_at,display_name,first_season,last_season,seasons_served,trust) VALUES('f1','2026-01-01',?,?,?,1,1)", (X, X, X))
    con.execute("UPDATE applications SET family_id='f1' WHERE id='H1'")
    # a task whose pair is gone (an older database, before foreign keys were enforced).
    # The pragma only takes effect outside a transaction, so commit first.
    con.commit()
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("INSERT INTO tasks(id,kind,candidate_id,title,detail,status,created_at) VALUES('t-gone','review_match','c-gone','orphan','', 'open','2026-01-01')")
    con.execute("INSERT INTO tasks(id,kind,app_id,title,detail,status,created_at) VALUES('t-gone2','review_notes','A-gone','orphan app','', 'open','2026-01-01')")
    con.execute("PRAGMA foreign_keys=ON")
    con.commit()
    return con


def test_pages_escape_everything_and_never_crash(tmp_path):
    con = make_db(tmp_path)
    outputs = {
        "dashboard": console.dashboard(con),
        "tasks": console.tasks_page(con, {}),
        "apps": console.apps_page(con, {}),
        "app": console.app_page(con, "H1"),
        "app_wrong": console.app_page(con, "W1"),
        "families": console.families_page(con),
        "family": console.families_page(con, "f1"),
        "exports": console.exports_page(con),
        "cards": console.cards_page(con, "2026", mode="pickup"),
    }
    for name, out in outputs.items():
        assert out, name
        assert "<img" not in out, name
        assert "onerror" not in out or "&lt;img" in out, name
        assert "could not be rendered" not in out, name  # tolerant paths, not the error card
    assert "pair no longer available" in outputs["tasks"] and "application no longer available" in outputs["tasks"]
    assert outputs["tasks"].count('name="status" value="dismissed"') == 5  # every task keeps its Done/Dismiss form
    assert "food, toys, coats" in outputs["app"] and X not in outputs["app"]  # programs are a whitelist
    assert console.app_page(con, "nope") is None and console.families_page(con, "nope") is None
    for kind in ("roster", "labels"):
        out = console.export_csv(con, "2026", kind)
        assert "onerror=alert" in out and out.startswith("﻿")  # csv is data, not html; it is quoted by the writer


def test_error_card_names_the_application(tmp_path, monkeypatch):
    con = make_db(tmp_path)
    monkeypatch.setattr(console, "app_card_body", lambda a, con: 1 / 0)
    out = console.app_page(con, "H1")
    assert "Application H1" in out and "ZeroDivisionError" in out


def test_labels_csv_has_unit_and_bom(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    insert(con, "U1", {"applicant": {"first_name": "Ana", "last_name": "Prueba", "contact_lang": "es"}, "address": {"street": "10 Pine St", "unit": "Apt 4", "city": "Truckee", "zip": "96161"}, "mailing": None}, status="accepted")
    insert(con, "M1", {"applicant": {"first_name": "Luis", "last_name": "Prueba"}, "address": {"street": "12 Oak St", "unit": "B", "city": "Truckee", "zip": "96161"}, "mailing": {"street": "PO Box 55", "city": "Truckee", "zip": "96160"}}, status="accepted", lang="en")
    out = console.export_csv(con, "2026", "labels")
    assert out.startswith("﻿")
    rows = [r.split(",") for r in out.lstrip("﻿").splitlines()]
    assert rows[0] == ["id", "name", "address1", "address2", "city", "zip", "lang"]
    by_id = {r[0]: r for r in rows[1:]}
    assert by_id["U1"] == ["U1", "Ana Prueba", "10 Pine St", "Apt 4", "Truckee", "96161", "es"]
    assert by_id["M1"] == ["M1", "Luis Prueba", "PO Box 55", "", "Truckee", "96160", "en"]


def test_search_ignores_accents(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    insert(con, "J1", {"applicant": {"first_name": "José", "last_name": "Núñez"}})
    insert(con, "K1", {"applicant": {"first_name": "Karen", "last_name": "Smith"}})
    out = console.apps_page(con, {"q": ["jose"]})
    assert "/apps/J1" in out and "/apps/K1" not in out
    out = console.apps_page(con, {"q": ["NUNEZ"]})
    assert "/apps/J1" in out
    out = console.apps_page(con, {"q": ["José"]})
    assert "/apps/J1" in out


def test_cards_follow_mode_and_contact_language(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    insert(con, "ES", {"applicant": {"first_name": "Ana", "contact_lang": "es"}, "children": []}, status="accepted", lang="en")
    insert(con, "EN", {"applicant": {"first_name": "Bob", "contact_lang": ""}, "children": []}, status="accepted", lang="en")
    out = console.cards_page(con, "2026", mode="mail")
    assert "Su tarjeta llegará por correo." in out and "Your card will arrive by mail." in out
    out = console.cards_page(con, "2026", mode="deliver")
    assert "Un voluntario le llevará los regalos." in out and "A volunteer will bring your gifts." in out
    out = console.cards_page(con, "2026", mode="pickup")
    assert "Traiga esta tarjeta" in out and "Bring this card on pickup day." in out


def serve_in_thread(path):
    """Like console.serve, on a free port, in a thread. The connection is opened in that
    thread because sqlite3 connections are bound to the thread that made them."""
    ready = threading.Event(); box = {}

    def run():
        con = connect(path)
        srv = HTTPServer(("127.0.0.1", 0), console.make_handler(con, 0))
        srv.RequestHandlerClass = console.make_handler(con, srv.server_address[1])
        box["srv"], box["port"] = srv, srv.server_address[1]
        ready.set()
        srv.serve_forever()
    threading.Thread(target=run, daemon=True).start()
    assert ready.wait(5)
    return box


def test_server_headers_and_csrf(tmp_path):
    make_db(tmp_path).close()
    con = connect(tmp_path / "t.sqlite")  # the test's own connection, for checking what the server wrote
    box = serve_in_thread(tmp_path / "t.sqlite"); srv, port = box["srv"], box["port"]
    try:
        def req(method, path, headers=None, body=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request(method, path, body=body, headers={"host": f"127.0.0.1:{port}", **(headers or {})})
            r = c.getresponse(); data = r.read(); c.close()
            return r, data
        r, data = req("GET", "/")
        assert r.status == 200 and r.getheader("content-security-policy") == console.CSP and b"<img" not in data
        r, data = req("GET", "/tasks")
        assert r.status == 200 and b"<img" not in data
        r, _ = req("GET", "/export/labels.csv?season=2026")
        assert r.getheader("content-type") == "text/csv; charset=utf-8" and r.getheader("content-disposition") == 'attachment; filename="labels-2026.csv"'
        r, _ = req("GET", "/export/roster.csv?season=../x")
        assert r.getheader("content-disposition") == 'attachment; filename="roster-x.csv"'
        r, _ = req("GET", "/apps/nope")
        assert r.status == 404 and r.getheader("content-security-policy") == console.CSP
        form = {"content-type": "application/x-www-form-urlencoded"}
        r, _ = req("POST", "/tasks/t-gone/close", form, "status=done")
        assert r.status == 403  # no origin, no referer
        r, _ = req("POST", "/tasks/t-gone/close", {**form, "origin": "http://evil.example"}, "status=done")
        assert r.status == 403
        r, _ = req("POST", "/tasks/t-gone/close", {**form, "origin": f"http://127.0.0.1:{port}0"}, "status=done")
        assert r.status == 403  # a longer port is a different origin
        r, _ = req("POST", "/tasks/t-gone/close", {**form, "origin": f"http://127.0.0.1:{port}", "host": "evil.example"}, "status=done")
        assert r.status == 403
        assert con.execute("SELECT status FROM tasks WHERE id='t-gone'").fetchone()[0] == "open"
        r, _ = req("POST", "/tasks/t-gone/close", {**form, "referer": f"http://localhost:{port}/tasks"}, "status=done&resolution=ok")
        assert r.status == 303 and r.getheader("location") == "/tasks" and r.getheader("content-security-policy") == console.CSP
        assert con.execute("SELECT status FROM tasks WHERE id='t-gone'").fetchone()[0] == "done"
        r, _ = req("POST", "/apps/H1/decide", {**form, "origin": f"http://127.0.0.1:{port}", "referer": "http://evil.example/x"}, "status=hold")
        assert r.status == 303 and r.getheader("location") == "/apps"
        assert con.execute("SELECT status FROM applications WHERE id='H1'").fetchone()[0] == "hold"
        r, _ = req("POST", "/apps/H1/decide", {**form, "origin": f"http://127.0.0.1:{port}"}, "status=<script>")
        assert r.status == 303 and con.execute("SELECT status FROM applications WHERE id='H1'").fetchone()[0] == "hold"
    finally:
        srv.shutdown(); srv.server_close()


def test_get_error_is_a_500_page_not_a_dropped_connection(tmp_path, monkeypatch):
    """The dashboard is the one page that cannot redirect to the dashboard: a failure there is
    a 500 page. Any other page's failure goes to the dashboard with an error card."""
    make_db(tmp_path).close()
    monkeypatch.setattr(console, "dashboard", lambda con, q=None: 1 / 0)
    monkeypatch.setattr(console, "families_page", lambda con, fid=None: sys.exit("families are gone"))
    box = serve_in_thread(tmp_path / "t.sqlite"); srv, port = box["srv"], box["port"]
    try:
        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5); c.request("GET", path); r = c.getresponse(); body = r.read().decode(); c.close()
            return r, body
        r, body = get("/")
        assert r.status == 500 and "ZeroDivisionError" in body and r.getheader("content-security-policy") == console.CSP
        r, _ = get("/families")
        assert r.status == 303 and r.getheader("location") == "/?error=" + urllib.parse.quote("SystemExit: families are gone")
        r, _ = get("/tasks")  # the server is still up
        assert r.status == 200
    finally:
        srv.shutdown(); srv.server_close()


def test_pull_without_a_key_redirects_with_the_message_and_the_server_survives(tmp_path, monkeypatch):
    from review import sync
    monkeypatch.delenv("TCC_KEY_FILE", raising=False)
    monkeypatch.setattr(sync, "KEY_DIR", tmp_path / "no-keys")
    monkeypatch.setattr(sync, "api", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no key, so the server must not be called")))
    make_db(tmp_path).close()
    con = connect(tmp_path / "t.sqlite")
    box = serve_in_thread(tmp_path / "t.sqlite"); srv, port = box["srv"], box["port"]
    try:
        def req(method, path, body=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request(method, path, body=body, headers={"host": f"127.0.0.1:{port}", "origin": f"http://127.0.0.1:{port}", "content-type": "application/x-www-form-urlencoded"})
            r = c.getresponse(); data = r.read(); c.close()
            return r, data
        r, _ = req("POST", "/actions/pull", "")
        assert r.status == 303
        loc = r.getheader("location")
        assert loc.startswith("/?error=") and "keygen" in urllib.parse.unquote(loc)
        r, data = req("GET", loc)
        assert r.status == 200 and b"The last action failed" in data and b"No age key found" in data and b"<script" not in data
        assert con.execute("SELECT detail FROM events WHERE kind='error' AND ref='/actions/pull'").fetchone()[0].startswith("ReviewSetupError: No age key found")
        # A bare SystemExit from an action gets the same treatment instead of killing the server.
        monkeypatch.setattr(sync, "load_identities", lambda: sys.exit("bye"))
        r, _ = req("POST", "/actions/pull", "")
        assert r.status == 303 and urllib.parse.unquote(r.getheader("location")) == "/?error=SystemExit: bye"
        r, _ = req("GET", "/")
        assert r.status == 200
    finally:
        srv.shutdown(); srv.server_close()


def test_decrypt_error_task_offers_delete_from_server(tmp_path, monkeypatch):
    from review import sync
    con = connect(tmp_path / "t.sqlite")
    add_task(con, "decrypt_error", "Could not read submission TCC-26-JUNK1", detail="DecryptError: no matching keys")
    add_task(con, "decrypt_error", "Could not read submission " + X, detail=X)  # a title that is not an id gets no button
    sync.set_failed_ids(con, "2026", ["TCC-26-JUNK1", "TCC-26-OTHER"])
    con.commit()
    out = console.tasks_page(con, {})
    assert out.count("Delete from server") == 1
    # The id must be typed, not echoed: an empty input with the id in the placeholder, and the two causes spelled out.
    assert 'name="confirm"' in out and 'value="TCC-26-JUNK1"' not in out and "type TCC-26-JUNK1 to delete" in out
    assert "Key mismatch" in out and "Junk or spam" in out and "<img" not in out
    tid = con.execute("SELECT id FROM tasks WHERE title='Could not read submission TCC-26-JUNK1'").fetchone()[0]
    con.close()
    calls = []
    monkeypatch.setattr(sync, "api", lambda method, path, body=None, headers=None: calls.append((method, path)) or {"deleted": 1})
    box = serve_in_thread(tmp_path / "t.sqlite"); srv, port = box["srv"], box["port"]
    try:
        def post(path, body):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", path, body=body, headers={"host": f"127.0.0.1:{port}", "origin": f"http://127.0.0.1:{port}", "content-type": "application/x-www-form-urlencoded"})
            r = c.getresponse(); r.read(); c.close()
            return r
        r = post(f"/tasks/{tid}/delete-server", "confirm=TCC-26-OTHER")  # the echo must match the task's own id
        assert r.status == 303 and calls == []
        r = post(f"/tasks/{tid}/delete-server", "confirm=TCC-26-JUNK1")
        assert r.status == 303 and r.getheader("location") == "/tasks"
        assert calls == [("DELETE", "/api/admin/submissions/TCC-26-JUNK1")]
        con = connect(tmp_path / "t.sqlite")
        t = con.execute("SELECT status, resolution FROM tasks WHERE id=?", (tid,)).fetchone()
        assert (t["status"], t["resolution"]) == ("done", "deleted from server")
        assert sync.failed_ids(con, "2026") == ["TCC-26-OTHER"]
    finally:
        srv.shutdown(); srv.server_close()


def post_form(port, path, body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", path, body=body, headers={"host": f"127.0.0.1:{port}", "origin": f"http://127.0.0.1:{port}", "content-type": "application/x-www-form-urlencoded"})
    r = c.getresponse(); r.read(); c.close()
    return r


def test_superseded_card_links_forward_and_takes_no_decision(tmp_path):
    import pytest
    from review.match import set_decision
    con = connect(tmp_path / "t.sqlite")
    p = {"applicant": {"first_name": "Rosa", "last_name": "Lopez"}, "address": {"street": "1 Elm", "city": "Truckee", "zip": "96161"}}
    insert(con, "OLD", p, status="superseded"); insert(con, "NEW", p); insert(con, "ORPHAN", p)
    con.execute("UPDATE applications SET supersedes='OLD', prior_status='accepted', prior_note='ok' WHERE id='NEW'")
    con.execute("UPDATE applications SET supersedes='GONE' WHERE id='ORPHAN'")
    con.commit()
    out = console.app_page(con, "OLD")
    assert "Replaced by" in out and 'href="/apps/NEW"' in out and "/decide" not in out and "decide on the newer application" in out
    assert 'class="pill superseded"' in out
    out = console.app_page(con, "NEW")
    assert "Replaces" in out and 'href="/apps/OLD"' in out and "before the edit" in out and 'pill accepted' in out and "old note: ok" in out and "/apps/NEW/decide" in out
    out = console.app_page(con, "ORPHAN")
    assert "GONE" in out and "not on this Mac" in out and 'href="/apps/GONE"' not in out
    out = console.apps_page(con, {"status": ["superseded"]})
    assert 'href="/apps/OLD"' in out and 'href="/apps/NEW"' not in out and "<option selected>superseded</option>" in out
    assert "superseded" in console.FILTER_STATUSES and "superseded" not in console.STATUSES
    with pytest.raises(ValueError, match="NEW"):
        set_decision(con, "OLD", "accepted")
    assert con.execute("SELECT status FROM applications WHERE id='OLD'").fetchone()[0] == "superseded"
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='decision_refused' AND ref='OLD'").fetchone()[0] == 1
    con.close()
    box = serve_in_thread(tmp_path / "t.sqlite"); srv, port = box["srv"], box["port"]
    try:
        r = post_form(port, "/apps/OLD/decide", "status=accepted&note=")
        assert r.status == 303 and r.getheader("location").startswith("/?error=") and "replaced" in urllib.parse.unquote(r.getheader("location"))
        con = connect(tmp_path / "t.sqlite")
        assert con.execute("SELECT status FROM applications WHERE id='OLD'").fetchone()[0] == "superseded"
        r = post_form(port, "/apps/NEW/decide", "status=accepted&note=")
        assert r.status == 303 and con.execute("SELECT status FROM applications WHERE id='NEW'").fetchone()[0] == "accepted"
    finally:
        srv.shutdown(); srv.server_close()


def test_note_field_enter_saves_only_the_note_and_an_old_note_does_not_ride_along(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    insert(con, "N1", {"applicant": {"first_name": "Ana", "contact_lang": "es"}, "address": {"street": "1 Elm"}}, status="matched")
    con.commit()
    out = console.app_page(con, "N1")
    hidden = out.index(f'value="{console.NOTE_SAVE}"'); first_real = out.index('value="accepted"')
    assert hidden < first_real and 'class="vh"' in out[hidden - 80:hidden]  # the first submit button is the note-only one, so Enter never accepts
    assert out.count(console.NOTE_RULE) == 2  # in the placeholder and under the field
    assert out.index('<form method="post" action="/apps/N1/decide"') < hidden
    con.close()
    box = serve_in_thread(tmp_path / "t.sqlite"); srv, port = box["srv"], box["port"]
    try:
        con = connect(tmp_path / "t.sqlite")
        state = lambda: tuple(con.execute("SELECT status, note FROM applications WHERE id='N1'").fetchone())  # noqa: E731
        r = post_form(port, "/apps/N1/decide", f"status={console.NOTE_SAVE}&note=hola")
        assert r.status == 303 and state() == ("matched", "hola")
        assert con.execute("SELECT COUNT(*) FROM events WHERE kind='note' AND ref='N1'").fetchone()[0] == 1
        post_form(port, "/apps/N1/decide", "status=needs_info&note=traiga+comprobante")
        assert state() == ("needs_info", "traiga comprobante")
        post_form(port, "/apps/N1/decide", "status=accepted&note=traiga+comprobante")  # the prefilled needs_info note is not kept
        assert state() == ("accepted", "")
        post_form(port, "/apps/N1/decide", "status=needs_info&note=otra+cosa")
        assert state() == ("needs_info", "otra cosa")
        post_form(port, "/apps/N1/decide", "status=accepted&note=recoja+el+12")  # a note typed for this decision is kept
        assert state() == ("accepted", "recoja el 12")
        post_form(port, "/apps/N1/decide", "status=hold&note=recoja+el+12")  # unchanged again: dropped
        assert state() == ("hold", "")
    finally:
        srv.shutdown(); srv.server_close()


def test_dashboard_shows_the_server_season_and_warns_when_config_differs(tmp_path, monkeypatch):
    from review import sync
    con = make_db(tmp_path)
    cfg = {"season": "2026", "mode": "pickup", "opens": "2026-10-15T00:00:00", "closes": "2026-11-15T23:59:59", "api_base": "http://stub"}
    monkeypatch.setattr(sync, "load_config", lambda: dict(cfg))
    monkeypatch.setattr(console, "SERVER", {"info": None, "error": None, "at": None})
    # Startup: the fetch fails, the console still runs and says so.
    monkeypatch.setattr(sync, "fetch_status", lambda timeout=5: (_ for _ in ()).throw(OSError("no network")))
    assert console.refresh_server_info() is None
    out = console.dashboard(con)
    assert "not reached" in out and "OSError" in out and "run git pull" not in out and "mode <b>pickup</b>" in out
    assert "Bring this card on pickup day." in console.cards_page(con, "2026")  # this Mac's config
    # The server agrees with this Mac.
    info = {**{k: cfg[k] for k in console.DRIFT_KEYS}, "open": False, "reason": "not_open", "timezone": "America/Los_Angeles"}
    monkeypatch.setattr(sync, "fetch_status", lambda timeout=5: dict(info))
    assert console.refresh_server_info() == info
    out = console.dashboard(con)
    assert "season <b>2026</b>" in out and "mode <b>pickup</b>" in out and "not open yet" in out and "run git pull" not in out and "card warn" not in out
    # The server was switched to mail and this Mac has not pulled: loud, and the cards follow the server.
    monkeypatch.setattr(sync, "fetch_status", lambda timeout=5: {**info, "mode": "mail", "open": True, "reason": "open"})
    console.refresh_server_info()
    out = console.dashboard(con)
    assert "config on this Mac differs from the server; run git pull" in out and "differs: mode" in out and "card warn" in out and "mode <b>mail</b>" in out
    assert console.config_drift(cfg, console.SERVER["info"]) == ["mode"]
    assert "Your card will arrive by mail." in console.cards_page(con, "2026")
    assert "Bring this card on pickup day." in console.cards_page(con, "2026", mode="pickup")  # an explicit mode still wins
    assert "<img" not in out


def test_labels_fall_back_to_the_home_city_and_zip_for_a_bare_po_box(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    insert(con, "P1", {"applicant": {"first_name": "Luis", "last_name": "Prueba"}, "address": {"street": "12 Oak St", "unit": "B", "city": "Truckee", "zip": "96161"}, "mailing": {"street": "PO Box 55", "city": "", "zip": None}}, status="accepted", lang="en")
    insert(con, "P2", {"applicant": {"first_name": "Ana", "last_name": "Prueba"}, "address": {"street": "1 Elm", "city": "Truckee", "zip": "96161"}, "mailing": {"street": "PO Box 9", "city": "Soda Springs", "zip": "95728"}}, status="accepted", lang="en")
    rows = [r.split(",") for r in console.export_csv(con, "2026", "labels").lstrip("﻿").splitlines()]
    by_id = {r[0]: r for r in rows[1:]}
    assert by_id["P1"] == ["P1", "Luis Prueba", "PO Box 55", "", "Truckee", "96161", "en"]
    assert by_id["P2"] == ["P2", "Ana Prueba", "PO Box 9", "", "Soda Springs", "95728", "en"]
