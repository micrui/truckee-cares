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
from .normalize_app import changed_fields, normalize_payload
from .paths import CONFIG, KEY_DIR

FAILED_CAP = 5000  # ids kept per season in meta failed:<season>; older ones fall off the front
ID_RE = re.compile(r"[A-Z0-9-]+")
ERROR_TITLE = "Could not read submission {sid}"
MAX_NOTE = 200
# Statuses the worker holds that are decisions. A pull seeds a local row from them, so a
# second Mac inherits what the first decided instead of starting from 'new'.
DECIDED = ("accepted", "declined", "duplicate", "out_of_area", "needs_info", "superseded")
# Local statuses that mean "nobody on this Mac has decided yet".
UNDECIDED = ("new", "matched")
# Local statuses that count as a decision when an edit replaces the application.
LOCAL_DECISIONS = ("accepted", "hold", "declined", "duplicate", "out_of_area", "needs_info")


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


def fetch_status(timeout=5):
    """GET /api/status, no token: the season, dates, mode and gate as the deployed Worker
    sees them. The console shows it next to this Mac's config/season.json."""
    cfg = load_config()
    req = urllib.request.Request(cfg["api_base"] + "/api/status", headers={"user-agent": "truckee-cares-review/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    if not isinstance(data, dict):
        raise ValueError("status is not an object")
    return data


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

def server_fields(item):
    """(status, note) as the worker holds them for one row. An older worker sends no note."""
    status = str(item.get("status") or "new")
    note = str(item.get("note") or "").strip()[:MAX_NOTE]
    return status, note


def retire_row(con, old_id, reason):
    """The old side of an edit leaves every queue: its open tasks are dismissed, including
    tasks raised from the other side of a pair that involves it, and those pairs are voided
    so a person is never asked about a dead application."""
    con.execute("""UPDATE tasks SET status='dismissed', resolved_at=?, resolution=?
                   WHERE status='open' AND (app_id=? OR candidate_id IN (SELECT id FROM candidates WHERE app_a=? OR app_b=?))""",
                (now(), reason, old_id, old_id, old_id))
    con.execute("UPDATE candidates SET verdict='void', decided_by='system', decided_at=? WHERE (app_a=? OR app_b=?) AND IFNULL(verdict,'')!='void'",
                (now(), old_id, old_id))


def transfer_row(con, old_id, new_id):
    """A resend that changed nothing: the new row takes over the old row's candidate pairs
    (with their verdicts, human ones included) and its open tasks, so nothing the board was
    asked or has answered is lost and the judge is not billed again."""
    for c in con.execute("SELECT id, app_a, app_b FROM candidates WHERE app_a=? OR app_b=?", (old_id, old_id)).fetchall():
        other = c["app_b"] if c["app_a"] == old_id else c["app_a"]
        lo, hi = sorted([new_id, other])
        if other == new_id or con.execute("SELECT 1 FROM candidates WHERE app_a=? AND app_b=?", (lo, hi)).fetchone():
            con.execute("UPDATE candidates SET verdict='void', decided_by='system', decided_at=? WHERE id=?", (now(), c["id"]))
            continue
        con.execute("UPDATE candidates SET app_a=?, app_b=? WHERE id=?", (lo, hi, c["id"]))
    con.execute("UPDATE tasks SET app_id=? WHERE app_id=? AND status='open'", (new_id, old_id))


def mark_superseded(con, old_id, new_id=None):
    """The server replaced old_id (the applicant edited it). Local status and the cached
    server status both say so; push never sends anything for it again."""
    con.execute("UPDATE applications SET status='superseded', server_status='superseded', updated_at=? WHERE id=?", (now(), old_id))
    log(con, "superseded", old_id, f"replaced by {new_id}" if new_id else "replaced on the server")


def edit_task(con, sid, old, changed):
    """One task for the person who decided on the old row: what the applicant changed,
    and, for needs_info, what we had asked for."""
    prior = old["status"]
    what = "Changed: " + (", ".join(changed) if changed else "nothing that matters")
    tail = f" Replaces {old['id']} (was {prior})."
    if prior == "needs_info":
        title = "Answered needs_info by editing"
        detail = f"We asked: {old['note'] or '(no note)'}\n{what}.{tail} Check that the edit covers it, then decide. If not, set needs_info again with a note."
    else:
        title = f"Edited after {prior}: re-check"
        detail = f"{what}.{tail}" + (f" Old note: {old['note']}" if old["note"] else "") + " Compare with the old application and decide again."
    add_task(con, "review_edit", title, app_id=sid, detail=detail)


def apply_edit(con, sid, payload, old):
    """A replacement arrived and the row it replaces is on this Mac. The old row leaves the
    queue. The new row keeps the family, records what the old row's decision was, and
    starts over as 'new' so it is matched and looked at again; a decision reached elsewhere
    (the server already holds one for the new row) is kept as is. A resend that changed
    nothing skips matching: it takes over the old row's pairs and tasks and gets one
    low-priority task instead."""
    changed = changed_fields(old["payload"], payload)
    prior, old_note = old["status"], old["note"] or ""
    row = con.execute("SELECT status FROM applications WHERE id=?", (sid,)).fetchone()
    decided_elsewhere = row["status"] in DECIDED
    con.execute("UPDATE applications SET family_id=?, prior_status=?, prior_note=? WHERE id=?", (old["family_id"], prior, old_note, sid))
    if changed:
        retire_row(con, old["id"], "application was edited")
        if not decided_elsewhere and prior in LOCAL_DECISIONS:
            edit_task(con, sid, old, changed)
    else:
        transfer_row(con, old["id"], sid)
        if not decided_elsewhere:
            if prior != "new":  # the old row was matched already; its pairs came along, so matching is not repeated
                con.execute("UPDATE applications SET status='matched', updated_at=? WHERE id=?", (now(), sid))
            add_task(con, "review_edit", "Resent without changes", app_id=sid,
                     detail=f"The applicant sent {old['id']} again without changing anything (it was {prior}" + (f", note: {old_note}" if old_note else "") + "). Set the status again if it still applies.")
    mark_superseded(con, old["id"], sid)
    log(con, "edit", sid, f"replaces {old['id']} (was {prior}); changed: " + (", ".join(changed) or "nothing"))


def store_item(con, item, season, identities, cfg, preview, failed):
    """One server row into the local database. Returns 'new', 'exists', 'failed' or
    'skipped' (no id). `failed` is the season's failed-id set (a dict, insertion ordered),
    kept current in place: a row that cannot be read joins it; a row that reads on a later
    try leaves it and its error task is closed.

    The worker's status and note are recorded as server_status and server_note. When the
    worker already holds a decision (another Mac pushed it), the local row starts from that
    decision instead of 'new', so pushing from this Mac changes nothing on the server."""
    sid = str(item.get("id") or "")
    created = str(item.get("created_at") or "")
    if not sid:
        return "skipped"
    result = "exists"
    srv_status, srv_note = server_fields(item)
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
            local = srv_status if srv_status in DECIDED else "new"
            con.execute("INSERT INTO applications(id,season,source,submitted_at,lang,status,note,payload,norm,server_status,server_note,updated_at,supersedes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, season, "web", created, lang, local, srv_note, json.dumps(payload, ensure_ascii=False), json.dumps(norm), srv_status, srv_note, now(), supersedes))
            if supersedes:
                old = con.execute("SELECT id, family_id, status, note, payload FROM applications WHERE id=?", (supersedes,)).fetchone()
                if old and old["status"] != "superseded":
                    old = dict(old); old["payload"] = json.loads(old["payload"])
                    apply_edit(con, sid, payload, old)
                elif old:
                    con.execute("UPDATE applications SET family_id=IFNULL(family_id, ?) WHERE id=?", (old["family_id"], sid))
            if local == "superseded":
                nxt = con.execute("SELECT id FROM applications WHERE supersedes=?", (sid,)).fetchone()
                log(con, "superseded", sid, f"replaced by {nxt['id']}" if nxt else "replaced on the server")
            else:
                # The edit may already be here (this row failed to decrypt on an earlier pull, or
                # the pages came out of order): then this row is the dead one.
                nxt = con.execute("SELECT id FROM applications WHERE supersedes=?", (sid,)).fetchone()
                if nxt:
                    retire_row(con, sid, "application was edited")
                    mark_superseded(con, sid, nxt["id"])
            result = "new"
        except Exception as e:  # keep going; a bad row becomes a task, not a wall
            result = "failed"
            msg = safe_error(e)
            log(con, stage, sid, msg)
            if srv_status == "superseded":
                return "failed"  # a dead row nobody needs to read: no task, not retried
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

SERVER_STATUS = {"accepted": "accepted", "declined": "declined", "duplicate": "duplicate", "out_of_area": "out_of_area", "matched": "fetched", "new": "fetched", "hold": "fetched", "needs_info": "needs_info", "superseded": "superseded"}


def http_body(e):
    """The JSON body of an HTTPError, or {}."""
    try:
        data = json.loads(e.read())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def push_statuses(db=None):
    """Send this Mac's decisions to the worker. Only rows whose status or note differ from
    what the worker was last seen holding are sent. A row this Mac has not decided never
    overwrites a decision another Mac pushed, and 'superseded' is never sent: the worker
    sets that itself when an edit lands. A 409 from the worker means the family replaced
    the row since the last pull; the local row is marked superseded and the push goes on.
    Returns the number of rows sent."""
    con = db or connect()
    rows = con.execute("SELECT id, status, server_status, note, server_note FROM applications WHERE source='web'").fetchall()
    n = 0
    for r in rows:
        srv = r["server_status"] or ""
        if r["status"] == "superseded" or srv == "superseded":
            continue
        if r["status"] in UNDECIDED and srv in DECIDED:
            continue  # not decided here; the server holds another Mac's decision
        want = SERVER_STATUS.get(r["status"], "fetched")
        note = (r["note"] or "").strip()[:MAX_NOTE]
        if want == srv and note == (r["server_note"] or ""):
            continue
        try:
            api("PATCH", f"/api/admin/submissions/{r['id']}", {"status": want, "note": note})
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise
            by = http_body(e).get("superseded_by")
            by = by if isinstance(by, str) and ID_RE.fullmatch(by) else None
            retire_row(con, r["id"], "application was edited")
            mark_superseded(con, r["id"], by)
            log(con, "push_skipped", r["id"], "replaced on the server; pull to get the new one")
            continue
        con.execute("UPDATE applications SET server_status=?, server_note=? WHERE id=?", (want, note, r["id"]))
        n += 1
    con.commit()
    return n


def resync(con, overwrite=False, season=None):
    """Refresh server_status and server_note for every web row on this Mac from the
    worker's admin list, season by season. A row the worker holds a decision for and this
    Mac has not decided takes that decision (a second Mac catching up); with overwrite,
    every row takes the worker's decision, even over a different local one. A row the
    worker marks superseded is retired here. Returns counts."""
    where, args = "source='web'", ()
    if season:
        where += " AND season=?"; args = (season,)
    seasons = [r[0] for r in con.execute(f"SELECT DISTINCT season FROM applications WHERE {where}", args)]
    stats = {"checked": 0, "refreshed": 0, "inherited": 0, "overwritten": 0, "superseded": 0, "missing": 0}
    q = lambda s: urllib.parse.quote(str(s), safe="")  # noqa: E731
    for s in seasons:
        remote, since, since_id = {}, "", ""
        while True:
            data = api("GET", f"/api/admin/submissions?season={q(s)}&since={q(since)}&since_id={q(since_id)}")
            items = data.get("items") or []
            for it in items:
                if isinstance(it, dict) and it.get("id"):
                    remote[str(it["id"])] = it
            before = (since, since_id)
            if items:
                since = str(data.get("next_since") or items[-1].get("created_at") or since)
                since_id = str(data.get("next_id") or items[-1].get("id") or since_id)
            if not items or not data.get("has_more") or (since, since_id) == before:
                break
        for r in con.execute("SELECT id, status, note FROM applications WHERE source='web' AND season=?", (s,)).fetchall():
            stats["checked"] += 1
            it = remote.get(r["id"])
            if not it:
                stats["missing"] += 1
                log(con, "resync_missing", r["id"], "not on the server")
                continue
            srv_status, srv_note = server_fields(it)
            con.execute("UPDATE applications SET server_status=?, server_note=? WHERE id=?", (srv_status, srv_note, r["id"]))
            stats["refreshed"] += 1
            if srv_status == "superseded":
                if r["status"] != "superseded":
                    nxt = con.execute("SELECT id FROM applications WHERE supersedes=?", (r["id"],)).fetchone()
                    retire_row(con, r["id"], "application was edited")
                    mark_superseded(con, r["id"], nxt["id"] if nxt else None)
                    stats["superseded"] += 1
            elif srv_status in DECIDED:
                same = r["status"] == srv_status and (r["note"] or "") == srv_note
                if r["status"] in UNDECIDED:
                    con.execute("UPDATE applications SET status=?, note=?, updated_at=? WHERE id=?", (srv_status, srv_note, now(), r["id"]))
                    log(con, "resync_inherited", r["id"], srv_status)
                    stats["inherited"] += 1
                elif overwrite and not same:
                    con.execute("UPDATE applications SET status=?, note=?, updated_at=? WHERE id=?", (srv_status, srv_note, now(), r["id"]))
                    log(con, "resync_overwritten", r["id"], f"{r['status']} -> {srv_status}")
                    stats["overwritten"] += 1
        con.commit()
    if stats["inherited"] or stats["overwritten"]:
        from .match import recompute_family
        for fid in [x[0] for x in con.execute("SELECT DISTINCT family_id FROM applications WHERE family_id IS NOT NULL")]:
            recompute_family(con, fid)
    log(con, "resync", ",".join(seasons), json.dumps(stats))
    con.commit()
    return stats


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
