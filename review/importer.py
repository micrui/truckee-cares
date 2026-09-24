"""Import JotForm exports (xlsx) from prior seasons into the family database.

Column names vary a little between years; we match on keywords. Each row
becomes an application with source=jotform and status=accepted (we assume
past seasons were served) unless --status says otherwise.
"""
import hashlib
import json
import re
from datetime import datetime

import openpyxl

from . import normalize as N
from .db import connect, log, now
from .normalize_app import normalize_payload

COLS = {
    "date": ["submission date"], "phone": ["mobile"], "home_phone": ["home phone"], "email": ["email"],
    "head": ["head of household"], "mail": ["mailing"], "local": ["local address"],
    "coats": ["coats"], "toys": ["need toys"], "children": ["names", "children needing"], "id": ["submission id"],
}


def col_index(headers):
    idx = {}
    for i, h in enumerate(headers):
        h = (h or "").lower()
        for key, needles in COLS.items():
            if any(n in h for n in needles) and key not in idx:
                if key == "children" and "need toys" in h:
                    continue
                idx[key] = i
    return idx


def split_name(full):
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def parse_address(text):
    text = (text or "").strip()
    z = re.search(r"\b(9\d{4})\b", text)
    city = "Truckee" if "truckee" in text.lower() else ("Soda Springs" if "soda" in text.lower() else "")
    return {"street": text, "unit": "", "city": city, "zip": z.group(1) if z else "", "in_area": bool(city)}


def import_xlsx(path, season, status="accepted", db=None):
    con = db or connect()
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    headers = [str(h) if h is not None else "" for h in next(rows)]
    idx = col_index(headers)
    n_new = n_dup = 0
    for row in rows:
        g = lambda k: (row[idx[k]] if k in idx and idx[k] < len(row) else None)
        sid = g("id")
        if sid is None and not any(row):
            continue
        if sid:
            app_id = f"JF-{season}-{sid}"
        else:  # no Submission ID column: a stable digest of the row, the same on every import
            digest = hashlib.sha1("\x1f".join("" if v is None else str(v) for v in row).encode("utf-8")).hexdigest()
            app_id = f"JF-{season}-{digest[:12]}"
        if con.execute("SELECT 1 FROM applications WHERE id=?", (app_id,)).fetchone():
            n_dup += 1
            continue
        first, last = split_name(str(g("head") or ""))
        sub = g("date")
        submitted = sub.isoformat() if isinstance(sub, datetime) else (str(sub) if sub else f"{season}-11-01T00:00:00")
        children_raw = ""
        for i, h in enumerate(headers):
            if ("names" in h.lower() or "children needing" in h.lower()) and "need toys" not in h.lower() and i < len(row) and row[i]:
                children_raw += str(row[i]) + "\n"
        payload = {
            "version": 1, "season": season, "lang": "", "submitted_at": submitted, "source_note": f"imported from {path}",
            "helper": None,
            "applicant": {"first_name": first, "last_name": last, "phone": str(g("phone") or ""), "other_phone": str(g("home_phone") or ""),
                          "email": str(g("email") or ""), "other_adult": "", "can_text": True, "contact_lang": ""},
            "address": parse_address(str(g("local") or g("mail") or "")),
            "mailing": {"street": str(g("mail") or ""), "city": "", "zip": ""} if g("mail") and g("mail") != g("local") else None,
            "household": {"adults": None, "adult_coat_sizes": []},
            "children": N.parse_children_freetext(children_raw),
            "children_raw": children_raw.strip(),
            "programs": {"food": True, "toys": str(g("toys") or "").lower().startswith("y"), "coats": str(g("coats") or "").lower().startswith("y")},
            "referral": "", "notes": "",
        }
        norm = normalize_payload(payload)
        con.execute("INSERT INTO applications(id,season,source,submitted_at,lang,status,payload,norm,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (app_id, season, "jotform", submitted, "", status, json.dumps(payload, ensure_ascii=False), json.dumps(norm), now()))
        n_new += 1
    log(con, "import", str(path), f"season={season} new={n_new} skipped={n_dup}")
    con.commit()
    return n_new, n_dup
