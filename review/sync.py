"""Pull encrypted submissions from the Worker, decrypt locally, store plaintext
in the local database. Push status changes back so the server row reflects
the decision (it never learns why)."""
import base64
import binascii
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyrage

from .db import add_task, connect, get_meta, set_meta, log, now
from .normalize_app import normalize_payload
from .paths import CONFIG, KEY_DIR

FAILED_CAP = 5000  # ids kept per season in meta failed:<season>; older ones fall off the front
ID_RE = re.compile(r"[A-Z0-9-]+")
ERROR_TITLE = "Could not read submission {sid}"


class ReviewSetupError(RuntimeError):
    """Something this Mac needs is missing (a key file, the admin token). The CLI turns it
    into an exit message; the console shows it on the dashboard and keeps running."""


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
        raise ReviewSetupError(f"No age key found in {where}. Run node bin/keygen.mjs <label> to make one (or set TCC_KEY_FILE to a key file).")
    return identities


def admin_token():
    p = KEY_DIR / "admin-token"
    if p.exists():
        return p.read_text().strip()
    raise ReviewSetupError(f"No admin token at {p}. Run bin/set-secret admin-token and paste the Worker's ADMIN_TOKEN.")


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


# --- tasks for rows that need a person -------------------------------------------------

def error_title(sid):
    return ERROR_TITLE.format(sid=sid)


def error_task_id(title):
    """The submission id a decrypt_error task is about, or None when the title is not ours."""
    prefix = ERROR_TITLE.split("{sid}")[0]
    title = str(title or "")
    if not title.startswith(prefix):
        return None
    sid = title[len(prefix):]
    return sid if ID_RE.fullmatch(sid) else None


def read_error_task(con, sid, message):
    """One task per unreadable submission id, whatever its status (a dismissed one stays dismissed)."""
    title = error_title(sid)
    if con.execute("SELECT 1 FROM tasks WHERE kind='decrypt_error' AND title=?", (title,)).fetchone():
        return
    add_task(con, "decrypt_error", title, app_id=None, detail=message)


def close_error_task(con, sid, resolution):
    con.execute("UPDATE tasks SET status='done', resolved_at=?, resolution=? WHERE kind='decrypt_error' AND title=? AND status='open'",
                (now(), resolution, error_title(sid)))


def preview_task(con, sid, created):
    """One task per preview row submitted during the season, whatever its status, so a
    task someone dismissed is not raised again by the next pull."""
    detail = f"submission {sid} at {created}"
    if con.execute("SELECT 1 FROM tasks WHERE kind='preview_in_window' AND (app_id=? OR detail=?)", (sid, detail)).fetchone():
        return
    known = con.execute("SELECT 1 FROM applications WHERE id=?", (sid,)).fetchone()
    add_task(con, "preview_in_window", "Submitted in preview mode during the season; call this family and ask them to reapply",
             app_id=sid if known else None, detail=detail)


# --- the failed list: ids this Mac could not read ---------------------------------------

def failed_ids(con, season):
    """Ids this Mac could not read, oldest first (meta failed:<season>, a JSON list)."""
    try:
        ids = json.loads(get_meta(con, f"failed:{season}", "[]") or "[]")
    except ValueError:
        ids = []
    return [str(i) for i in ids] if isinstance(ids, list) else []


def set_failed_ids(con, season, ids):
    set_meta(con, f"failed:{season}", json.dumps(list(ids)[-FAILED_CAP:]))


def forget_failed(con, sid):
    """Drop one id from every season's failed list."""
    for r in con.execute("SELECT key FROM meta WHERE key LIKE 'failed:%'").fetchall():
        season = r["key"][len("failed:"):]
        ids = failed_ids(con, season)
        if sid in ids:
            set_failed_ids(con, season, [i for i in ids if i != sid])


# --- pulling ----------------------------------------------------------------------------

def store_item(con, item, season, identities, cfg, preview, failed):
    """One server row into the local database. Returns 'new', 'exists', 'failed' or
    'skipped' (no id). `failed` is the season's failed-id set (a dict, insertion ordered),
    kept current in place: a row that cannot be read joins it; a row that reads on a later
    try leaves it and its error task is closed."""
    sid = str(item.get("id") or "")
    created = str(item.get("created_at") or "")
    if not sid:
        return "skipped"
    result = "exists"
    if not con.execute("SELECT 1 FROM applications WHERE id=?", (sid,)).fetchone():
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
            supersedes = item.get("supersedes") or None
            con.execute("INSERT INTO applications(id,season,source,submitted_at,lang,status,payload,norm,server_status,updated_at,supersedes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, season, "web", created, lang, "new", json.dumps(payload, ensure_ascii=False), json.dumps(norm), item.get("status"), now(), supersedes))
            if supersedes:
                # An edit replaces the earlier application: it leaves the queue, and the new
                # one inherits its family and any decision already made.
                old = con.execute("SELECT family_id, status FROM applications WHERE id=?", (supersedes,)).fetchone()
                if old:
                    inherit = old["status"] if old["status"] in ("accepted", "hold", "declined") else "new"
                    con.execute("UPDATE applications SET family_id=?, status=? WHERE id=?", (old["family_id"], inherit, sid))
                    con.execute("UPDATE applications SET status='superseded', updated_at=? WHERE id=?", (now(), supersedes))
                    con.execute("UPDATE tasks SET status='dismissed', resolved_at=?, resolution='application was edited' WHERE app_id=? AND status='open'", (now(), supersedes))
                    log(con, "superseded", supersedes, f"replaced by {sid}")
            result = "new"
        except Exception as e:  # keep going; a bad row becomes a task, not a wall
            result = "failed"
            msg = safe_error(e)
            log(con, stage, sid, msg)
            read_error_task(con, sid, msg)
            failed[sid] = None
    if result != "failed" and sid in failed:
        failed.pop(sid)
        close_error_task(con, sid, "read on a later pull")
    if preview and created and in_window(cfg, created):
        preview_task(con, sid, created)
    return result


def pull_season(con, season, identities, cfg, preview=False):
    """Every server row of one season from the stored cursor, page after page until the
    server says there is no more. Returns (new, failed). A row that cannot be read is
    logged, becomes one task, and its id goes into meta failed:<season>; the cursor moves
    past it all the same, so a junk row or a flood of them never pins the pull. The cursor
    is the pair (since, since_id): an older database with only `since` gets the rows at
    that timestamp again, and skips the ones it already holds by id. Each page is
    committed as it lands."""
    since = get_meta(con, f"since:{season}", "") or ""
    since_id = get_meta(con, f"since_id:{season}", "") or ""
    failed = dict.fromkeys(failed_ids(con, season))
    new = bad = pages = 0
    q = lambda s: urllib.parse.quote(str(s), safe="")  # noqa: E731
    while True:
        data = api("GET", f"/api/admin/submissions?season={q(season)}&since={q(since)}&since_id={q(since_id)}")
        items = data.get("items") or []
        pages += 1
        for item in items:
            r = store_item(con, item, season, identities, cfg, preview, failed)
            new += r == "new"
            bad += r == "failed"
        before = (since, since_id)
        if items:
            since = str(data.get("next_since") or items[-1].get("created_at") or since)
            since_id = str(data.get("next_id") or items[-1].get("id") or since_id)
            set_meta(con, f"since:{season}", since)
            set_meta(con, f"since_id:{season}", since_id)
        set_failed_ids(con, season, failed)
        con.commit()
        # Stop at the last page, and stop if the server hands back a cursor that did not move.
        if not items or not data.get("has_more") or (since, since_id) == before:
            break
    log(con, "pull", season, f"new={new} failed={bad} pages={pages}")
    con.commit()
    return new, bad


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
    """Forget where the pull left off. The failed list stays: rows fetched again that still
    fail are already recorded, and rows that now read leave it on their own."""
    con.execute("DELETE FROM meta WHERE key IN (?, ?)", (f"since:{season}", f"since_id:{season}"))
    con.commit()


def retry_failed(con, season, identities=None, cfg=None):
    """Fetch each id in failed:<season> on its own and try again: after a new key lands on
    this Mac, or after a row was fixed. Ids gone from the server leave the list. Returns
    (stored, still_failed, gone)."""
    identities = identities or load_identities()
    cfg = cfg or load_config()
    failed = dict.fromkeys(failed_ids(con, season))
    stored = still = gone = 0
    for sid in list(failed):
        if not ID_RE.fullmatch(sid):
            failed.pop(sid); continue
        try:
            data = api("GET", f"/api/admin/submissions/{sid}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            failed.pop(sid); gone += 1
            close_error_task(con, sid, "gone from the server")
            continue
        item = data.get("item") if isinstance(data.get("item"), dict) else data
        r = store_item(con, item, season, identities, cfg, preview=(season == "preview"), failed=failed)
        if r in ("new", "exists"):
            stored += 1
        else:
            still += 1
        set_failed_ids(con, season, failed)
        con.commit()
    set_failed_ids(con, season, failed)
    log(con, "retry_failed", season, f"stored={stored} still_failed={still} gone={gone}")
    con.commit()
    return stored, still, gone


# --- deleting rows on the server ----------------------------------------------------------

def delete_row(con, sid):
    """DELETE one row on the server and close its error task here. The caller updates the
    failed lists. Returns the server's deleted count."""
    if not ID_RE.fullmatch(str(sid or "")):
        raise ValueError("not a submission id")
    r = api("DELETE", f"/api/admin/submissions/{sid}")
    deleted = r.get("deleted") if isinstance(r, dict) else None
    close_error_task(con, sid, "deleted from server")
    log(con, "delete_server", sid, f"deleted={deleted}")
    return deleted


def delete_server(con, sid):
    """Delete one submission from the server (junk, or a test row) and forget it here."""
    deleted = delete_row(con, sid)
    forget_failed(con, sid)
    con.commit()
    return deleted


def delete_failed(con, season):
    """Delete every id in failed:<season> from the server: the spam-flood case. The list
    shrinks as each delete lands, so an interrupted run can be rerun. Returns
    (rows the server deleted, ids tried)."""
    failed = dict.fromkeys(failed_ids(con, season))
    deleted = tried = 0
    for sid in list(failed):
        tried += 1
        deleted += delete_row(con, sid) or 0
        failed.pop(sid)
        if tried % 50 == 0:
            set_failed_ids(con, season, failed); con.commit()
    set_failed_ids(con, season, failed)
    log(con, "delete_failed", season, f"tried={tried} deleted={deleted}")
    con.commit()
    return deleted, tried


# --- statuses back to the server ---------------------------------------------------------

SERVER_STATUS = {"accepted": "accepted", "declined": "declined", "duplicate": "duplicate", "out_of_area": "out_of_area", "matched": "fetched", "new": "fetched", "hold": "fetched", "superseded": "superseded"}


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
