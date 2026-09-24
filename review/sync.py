"""Pull encrypted submissions from the Worker, decrypt locally, store plaintext
in the local database. Push status changes back so the server row reflects
the decision (it never learns why)."""
import base64
import binascii
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyrage

from .db import add_task, connect, get_meta, set_meta, log, now
from .normalize_app import normalize_payload
from .paths import CONFIG, KEY_DIR


def dearmor(text):
    """age armored (PEM-like) text -> raw bytes. pyrage has no armor helper."""
    body = "".join(l.strip() for l in str(text).splitlines() if l.strip() and not l.startswith("-----"))
    return base64.b64decode(body)


def load_config():
    return json.load(open(CONFIG))


def load_identities():
    """Every age identity this Mac holds. TCC_KEY_FILE names one file; otherwise every
    *.key in ~/.config/truckee-cares is read, so a second board member's key or an old
    season's key can open rows encrypted to it. The secret is never printed."""
    env = os.environ.get("TCC_KEY_FILE")
    paths = [Path(env)] if env else sorted(KEY_DIR.glob("*.key"))
    identities = []
    for path in paths:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if line.startswith("AGE-SECRET-KEY-"):
                identities.append(pyrage.x25519.Identity.from_str(line))
    if not identities:
        where = env or str(KEY_DIR)
        raise SystemExit(f"No age key found in {where}. Run node bin/keygen.mjs <label> to make one (or set TCC_KEY_FILE to a key file).")
    return identities


def admin_token():
    p = KEY_DIR / "admin-token"
    if p.exists():
        return p.read_text().strip()
    raise SystemExit(f"No admin token at {p}. Run bin/set-secret admin-token and paste the Worker's ADMIN_TOKEN.")


def api(method, path, body=None, headers=None):
    cfg = load_config()
    req = urllib.request.Request(cfg["api_base"] + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"authorization": f"Bearer {admin_token()}", "content-type": "application/json", "user-agent": "truckee-cares-review/1", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def parse_when(text, tz):
    """ISO timestamp -> aware datetime. Naive values are read in the season's timezone."""
    d = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=tz)


def in_window(cfg, created_at):
    """True when created_at falls inside [opens, closes] of the season config."""
    try:
        tz = ZoneInfo(cfg.get("timezone") or "UTC")
        when = parse_when(created_at, timezone.utc)
        return parse_when(cfg["opens"], tz) <= when <= parse_when(cfg["closes"], tz)
    except (KeyError, ValueError, TypeError):
        return False


def safe_error(e):
    """Type and message of an exception, never a payload. Messages from the decryptor
    and the JSON parser describe positions, not content; anything else is type only."""
    if isinstance(e, (pyrage.DecryptError, json.JSONDecodeError, ValueError, binascii.Error)):
        return f"{type(e).__name__}: {e}"
    return type(e).__name__


def read_error_task(con, sid, message):
    """One task per unreadable submission id, whatever its status (a dismissed one stays dismissed)."""
    title = f"Could not read submission {sid}"
    if con.execute("SELECT 1 FROM tasks WHERE kind='decrypt_error' AND title=?", (title,)).fetchone():
        return
    add_task(con, "decrypt_error", title, app_id=None, detail=message)


def pull_season(con, season, identities, cfg, preview=False):
    """One season's rows. Returns (new, failed). A row that cannot be read is logged and
    becomes a task; the rest of the page is still stored. The cursor only advances past
    rows before the first failure, so the failed row is fetched again next time (rows
    already stored are skipped by id, so that costs nothing)."""
    since = get_meta(con, f"since:{season}", "")
    data = api("GET", f"/api/admin/submissions?season={season}&since={since}")
    new = failed = 0
    latest, stalled = since, False
    for item in data.get("items") or []:
        sid = str(item.get("id", ""))
        created = str(item.get("created_at", ""))
        inserted = False
        exists = con.execute("SELECT 1 FROM applications WHERE id=?", (sid,)).fetchone() if sid else None
        if sid and not exists:
            stage = "decrypt_error"
            try:
                plain = pyrage.decrypt(dearmor(item.get("ciphertext", "")), identities)
                stage = "bad_payload"
                payload = json.loads(plain)
                if not isinstance(payload, dict) or not isinstance(payload.get("applicant"), dict):
                    raise ValueError("payload is not an application object")
                norm = normalize_payload(payload)
                lang = item.get("lang")
                lang = lang if lang in ("en", "es") else "en"
                con.execute("INSERT INTO applications(id,season,source,submitted_at,lang,status,payload,norm,server_status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (sid, season, "web", created, lang, "new", json.dumps(payload, ensure_ascii=False), json.dumps(norm), item.get("status"), now()))
                new += 1
                inserted = True
            except Exception as e:  # keep going; a bad row becomes a task, not a wall
                failed += 1
                stalled = True
                msg = safe_error(e)
                log(con, stage, sid, msg)
                read_error_task(con, sid, msg)
        if preview and created and in_window(cfg, created):
            add_task(con, "preview_in_window", "Submitted in preview mode during the season; call this family and ask them to reapply",
                     app_id=sid if (inserted or exists) else None, detail=f"submission {sid} at {created}")
        if not stalled and created:
            latest = max(latest, created)
    if latest != since:
        set_meta(con, f"since:{season}", latest)
    log(con, "pull", season, f"new={new} failed={failed}")
    return new, failed


def pull(db=None, season=None):
    """Fetch the season's rows, then the preview rows (encrypted to the same keys).
    Returns (new, failed) over both."""
    con = db or connect()
    cfg = load_config()
    season = season or cfg["season"]
    identities = load_identities()
    new, failed = pull_season(con, season, identities, cfg, preview=(season == "preview"))
    if season != "preview":
        n2, f2 = pull_season(con, "preview", identities, cfg, preview=True)
        new += n2; failed += f2
    con.commit()
    return new, failed


def reset_cursor(con, season):
    con.execute("DELETE FROM meta WHERE key=?", (f"since:{season}",))
    con.commit()


SERVER_STATUS = {"accepted": "accepted", "declined": "declined", "duplicate": "duplicate", "out_of_area": "out_of_area", "matched": "fetched", "new": "fetched", "hold": "fetched"}


def push_statuses(db=None):
    con = db or connect()
    rows = con.execute("SELECT id, status, server_status FROM applications WHERE source='web'").fetchall()
    n = 0
    for r in rows:
        want = SERVER_STATUS.get(r["status"], "fetched")
        if want != (r["server_status"] or ""):
            api("PATCH", f"/api/admin/submissions/{r['id']}", {"status": want})
            con.execute("UPDATE applications SET server_status=? WHERE id=?", (want, r["id"]))
            n += 1
    con.commit()
    return n


def preview_rows_in_window(con, cfg=None):
    """Preview applications submitted while the real season was open: someone used the
    test form for a real request. They must be called before the preview rows are purged."""
    cfg = cfg or load_config()
    rows = con.execute("SELECT id, submitted_at FROM applications WHERE season='preview'").fetchall()
    ids = [r["id"] for r in rows if in_window(cfg, r["submitted_at"])]
    open_tasks = con.execute("SELECT COUNT(*) FROM tasks WHERE kind='preview_in_window' AND status='open'").fetchone()[0]
    return ids, open_tasks


def purge_server(season):
    return api("DELETE", f"/api/admin/season/{season}", headers={"x-confirm": season})
