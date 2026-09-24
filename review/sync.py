"""Pull encrypted submissions from the Worker, decrypt locally, store plaintext
in the local database. Push status changes back so the server row reflects
the decision (it never learns why)."""
import base64
import json
import os
import urllib.request

import pyrage

from .db import connect, get_meta, set_meta, log, now
from .normalize_app import normalize_payload
from .paths import CONFIG, KEY_DIR


def dearmor(text):
    """age armored (PEM-like) text -> raw bytes. pyrage has no armor helper."""
    body = "".join(l.strip() for l in text.splitlines() if l.strip() and not l.startswith("-----"))
    return base64.b64decode(body)


def load_config():
    return json.load(open(CONFIG))


def load_identity(season):
    path = os.environ.get("TCC_KEY_FILE") or (KEY_DIR / f"season-{season}.key")
    for line in open(path):
        line = line.strip()
        if line.startswith("AGE-SECRET-KEY-"):
            return pyrage.x25519.Identity.from_str(line)
    raise SystemExit(f"no AGE-SECRET-KEY in {path}")


def admin_token():
    tok = os.environ.get("TCC_ADMIN_TOKEN")
    if tok:
        return tok
    p = KEY_DIR / "admin-token"
    if p.exists():
        return p.read_text().strip()
    raise SystemExit("set TCC_ADMIN_TOKEN or write ~/.config/truckee-cares/admin-token")


def api(method, path, body=None, headers=None):
    cfg = load_config()
    req = urllib.request.Request(cfg["api_base"] + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"authorization": f"Bearer {admin_token()}", "content-type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def pull(db=None, season=None):
    con = db or connect()
    cfg = load_config()
    season = season or cfg["season"]
    identity = load_identity(season)
    since = get_meta(con, f"since:{season}", "")
    data = api("GET", f"/api/admin/submissions?season={season}&since={since}")
    n = 0
    latest = since
    for item in data["items"]:
        latest = max(latest, item["created_at"])
        if con.execute("SELECT 1 FROM applications WHERE id=?", (item["id"],)).fetchone():
            continue
        try:
            plain = pyrage.decrypt(dearmor(item["ciphertext"]), [identity])
        except Exception as e:  # keep going; a bad row becomes a task
            log(con, "decrypt_error", item["id"], str(e))
            continue
        payload = json.loads(plain)
        norm = normalize_payload(payload)
        con.execute("INSERT INTO applications(id,season,source,submitted_at,lang,status,payload,norm,server_status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (item["id"], season, "web", item["created_at"], item.get("lang", "en"), "new",
                     json.dumps(payload, ensure_ascii=False), json.dumps(norm), item.get("status"), now()))
        n += 1
    set_meta(con, f"since:{season}", latest)
    log(con, "pull", season, f"new={n}")
    con.commit()
    return n


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


def purge_server(season):
    return api("DELETE", f"/api/admin/season/{season}", headers={"x-confirm": season})
