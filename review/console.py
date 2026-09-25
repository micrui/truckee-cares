"""Local admin console: a small server on 127.0.0.1 with server-rendered pages.
No JavaScript framework, no external requests. Pages: dashboard, tasks,
applications, application detail, families, exports.

Payloads are decrypted blobs, so every value is escaped and every part is read
as if it might be missing or the wrong type. One bad application renders as an
error card; it never takes the page or the connection down."""
import csv
import html
import io
import json
import re
import traceback
import unicodedata
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from .db import log, now
from .match import load_apps, resolve_candidate, set_decision, set_note, run_matching
from .paths import ROOT
from .sync import error_task_id

E = lambda v: html.escape("" if v is None else str(v))  # noqa: E731
PROGRAMS = ("food", "toys", "coats")
STATUSES = ("new", "matched", "accepted", "hold", "needs_info", "declined", "duplicate", "out_of_area")  # what a person may set
FILTER_STATUSES = STATUSES + ("superseded",)  # what the list can show; superseded is set by the server, never by hand
NOTE_SAVE = "__note__"  # the decide form's first (hidden) button: Enter in the note field saves the note and keeps the status
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'"
CSS = (ROOT / "site" / "static" / "css" / "site.css").read_text()
EXTRA = """
.wrap{max-width:1100px} table{width:100%;border-collapse:collapse;font-size:.92rem} th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.8rem;font-weight:700;background:var(--card)} .pill.accepted{background:var(--ok);color:#fff}
.pill.duplicate,.pill.declined,.pill.out_of_area{background:var(--err);color:#fff} .pill.new{background:#e0b100;color:#000}
.pill.superseded{background:var(--muted);color:#fff;text-decoration:line-through}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:12px} .pair .card{margin:0} form.inline{display:inline} .btn.small{min-height:36px;padding:4px 12px;font-size:.9rem}
nav a{margin-right:10px} .kv dt{color:var(--muted);font-size:.85rem;margin-top:6px} .kv dd{margin:0} .card.error{border:1px solid var(--err)}
.card.warn{border:2px solid #e0b100} .vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0;padding:0;margin:-1px}
.hint{color:var(--muted);font-size:.85rem;margin-top:4px}
"""
CARD_TEXT = {  # season mode -> language -> what the pickup card says
    "pickup": {"en": "Bring this card on pickup day.", "es": "Traiga esta tarjeta el día de la entrega."},
    "mail": {"en": "Your card will arrive by mail.", "es": "Su tarjeta llegará por correo."},
    "deliver": {"en": "A volunteer will bring your gifts.", "es": "Un voluntario le llevará los regalos."},
}
NOTE_RULE = "Enter saves the note only. The note belongs to needs_info: choosing another status clears it unless you type a new one."

# What the deployed Worker says about itself (GET /api/status), fetched once when the
# console starts. The dashboard compares it with this Mac's config/season.json.
SERVER = {"info": None, "error": None, "at": None}
DRIFT_KEYS = ("season", "mode", "opens", "closes")


def refresh_server_info():
    from .sync import fetch_status
    try:
        SERVER["info"] = fetch_status(); SERVER["error"] = None
    except Exception as e:  # the console works without the server; the dashboard says so
        SERVER["info"] = None; SERVER["error"] = f"{type(e).__name__}: {e}"[:200]
    SERVER["at"] = now()
    return SERVER["info"]


def config_drift(cfg, info):
    """Keys where config/season.json on this Mac disagrees with the deployed Worker."""
    if not isinstance(info, dict):
        return []
    return [k for k in DRIFT_KEYS if str(d(cfg).get(k) or "") != str(info.get(k) or "")]


def server_mode():
    """The season mode as the Worker has it, when the console could reach it."""
    info = SERVER.get("info")
    mode = info.get("mode") if isinstance(info, dict) else None
    return mode if mode in CARD_TEXT else None


def d(v):
    """dict or {}"""
    return v if isinstance(v, dict) else {}


def dl(v):
    """list of dicts, or []"""
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def sl(v):
    """list of strings, or []"""
    return [str(x) for x in v if x is not None] if isinstance(v, list) else []


def fold(text):
    """Lowercase, accents stripped, so 'jose' finds 'José'."""
    t = unicodedata.normalize("NFKD", "" if text is None else str(text))
    return "".join(ch for ch in t if not unicodedata.combining(ch)).lower()


def page(title, body, con):
    open_tasks = con.execute("SELECT COUNT(*) FROM tasks WHERE status='open'").fetchone()[0]
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{E(title)} · TCC review</title>
<style>{CSS}{EXTRA}</style></head><body><header class="site-header"><div class="wrap row"><a class="brand" href="/">TCC review</a></div>
<nav class="wrap"><a href="/">Dashboard</a><a href="/tasks">Tasks ({open_tasks})</a><a href="/apps">Applications</a><a href="/families">Families</a><a href="/exports">Exports</a></nav></header>
<main class="wrap">{body}</main></body></html>"""


def error_page(title, detail=""):
    """Rendered without touching the database, for when the database is the problem."""
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>{E(title)} · TCC review</title><style>{CSS}{EXTRA}</style></head>
<body><main class="wrap"><h1>{E(title)}</h1><pre style="white-space:pre-wrap">{E(detail)}</pre><p><a href="/">Dashboard</a></p></main></body></html>"""


def error_card(what, e):
    return f"<div class='card error'><h3>{E(what)}</h3><p class='muted'>could not be rendered: {E(type(e).__name__)}: {E(e)}</p></div>"


def pill(status):
    s = E(status)
    return f'<span class="pill {s}">{s}</span>'


def name_of(ap):
    return f"{E(ap.get('first_name'))} {E(ap.get('last_name'))}".strip()


def app_card(a, con):
    try:
        return app_card_body(a, con)
    except Exception as e:  # one bad application never hides the rest of the page
        return error_card(f"Application {d(a).get('id', '?')}", e)


def app_card_body(a, con):
    p = d(a.get("payload")); ap = d(p.get("applicant")); ad = d(p.get("address")); ml = d(p.get("mailing")); hp = d(p.get("helper"))
    hh = d(p.get("household")); pr = d(p.get("programs"))
    kids = "".join(f"<li>{E(c.get('first_name'))} · {E(c.get('age', '?'))} · {E(c.get('sex'))}{(' · coat ' + E(c.get('coat_size'))) if c.get('coat') else ''}</li>" for c in dl(p.get("children")))
    fam = con.execute("SELECT * FROM families WHERE id=?", (a.get("family_id"),)).fetchone() if a.get("family_id") else None
    coats = sl(hh.get("adult_coat_sizes"))
    superseded = a.get("status") == "superseded"
    edits = ""
    if a.get("supersedes"):
        have = con.execute("SELECT 1 FROM applications WHERE id=?", (a["supersedes"],)).fetchone()
        link = f'<a href="/apps/{E(a["supersedes"])}">{E(a["supersedes"])}</a>' if have else f'{E(a["supersedes"])} <span class="muted">(not on this Mac)</span>'
        edits += f"<dt>Replaces</dt><dd>{link} (edited by the applicant)"
        edits += (f' · before the edit: {pill(a.get("prior_status"))}' + (f' · old note: {E(a.get("prior_note"))}' if a.get("prior_note") else "")) if a.get("prior_status") else ""
        edits += "</dd>"
    newer = con.execute("SELECT id, submitted_at FROM applications WHERE supersedes=? ORDER BY submitted_at DESC", (a["id"],)).fetchone() if superseded else None
    if superseded:
        edits += "<dt>Replaced by</dt><dd>" + (f'<a href="/apps/{E(newer["id"])}">{E(newer["id"])}</a> on {E(str(newer["submitted_at"] or "")[:16])}' if newer else '<span class="muted">a later edit not on this Mac</span>') + " (edited by the applicant)</dd>"
    if superseded:
        actions = "<p class='muted'>Replaced by the applicant's edit. No decisions here; decide on the newer application.</p>"
    else:
        actions = f"""<form method="post" action="/apps/{E(a['id'])}/decide" class="inline">
<button type="submit" class="vh" name="status" value="{NOTE_SAVE}" tabindex="-1" aria-hidden="true">Save note</button>
{''.join(f'<button type="submit" class="btn btn-ghost small" name="status" value="{s}">{s}</button> ' for s in ['accepted', 'hold', 'needs_info', 'declined', 'duplicate', 'out_of_area'])}
<div style="margin-top:6px"><input name="note" value="{E(a.get('note') or '')}" maxlength="200" placeholder="note the family sees on the status page, in their language ({E((p.get('applicant') or {}).get('contact_lang') or a.get('lang') or 'es')}); no personal details. {E(NOTE_RULE)}" style="width:100%;min-height:36px;font:inherit;padding:4px 8px">
<div class="hint">{E(NOTE_RULE)}</div></div>
</form>"""
    return f"""<div class="card"><h3><a href="/apps/{E(a['id'])}">{E(a['id'])}</a> {pill(a.get('status'))} <span class="muted">{E(a.get('season'))} · {E(a.get('source'))} · {E(str(a.get('submitted_at') or '')[:16])}</span></h3>
<dl class="kv">
{edits}
<dt>Head of household</dt><dd><strong>{name_of(ap)}</strong>{(' · other adult: ' + E(ap.get('other_adult'))) if ap.get('other_adult') else ''}</dd>
<dt>Phone</dt><dd>{E(ap.get('phone'))}{' (ok to text)' if ap.get('can_text') else ''}{(' · ' + E(ap.get('other_phone'))) if ap.get('other_phone') else ''}{(' · ' + E(ap.get('email'))) if ap.get('email') else ''}</dd>
{('<dt>Filed by helper</dt><dd>' + E(hp.get('name')) + ' ' + E(hp.get('phone')) + ' ' + E(hp.get('org')) + '</dd>') if hp else ''}
<dt>Address</dt><dd>{E(ad.get('street'))} {E(ad.get('unit'))}, {E(ad.get('city'))} {E(ad.get('zip'))} {'' if ad.get('in_area', True) else '<span class="pill out_of_area">out of area</span>'}</dd>
{('<dt>Mailing</dt><dd>' + E(ml.get('street')) + ', ' + E(ml.get('city')) + ' ' + E(ml.get('zip')) + '</dd>') if ml else ''}
<dt>Adults</dt><dd>{E(hh.get('adults', '?'))}{(' · coats: ' + E(', '.join(coats))) if coats else ''}</dd>
<dt>Children</dt><dd><ul>{kids or '<li class="muted">none</li>'}</ul>{('<div class="muted">raw: ' + E(p.get('children_raw')) + '</div>') if p.get('children_raw') else ''}</dd>
<dt>Requested</dt><dd>{', '.join(k for k in PROGRAMS if pr.get(k))}</dd>
{('<dt>Referral</dt><dd>' + E(p.get('referral')) + '</dd>') if p.get('referral') else ''}
{('<dt>Notes</dt><dd>' + E(p.get('notes')) + '</dd>') if p.get('notes') else ''}
<dt>Family</dt><dd>{('<a href="/families/' + E(fam['id']) + '">' + E(fam['display_name']) + '</a> · seasons served: ' + E(fam['seasons_served']) + ' · trust ' + E(fam['trust'])) if fam else '<span class="muted">not linked</span>'}</dd>
</dl>
{actions}</div>"""


def dashboard(con, q=None):
    """`q` is the parsed query string; an `error` in it (set by a failed action) is shown as a card."""
    err = (q or {}).get("error", [""])[0]
    ecard = f"<div class='card error'><h3>The last action failed</h3><p style='white-space:pre-wrap'>{E(err)}</p></div>" if err else ""
    season_rows = con.execute("SELECT season, status, COUNT(*) n FROM applications GROUP BY season, status ORDER BY season DESC, status").fetchall()
    rows = "".join(f"<tr><td>{E(r['season'])}</td><td>{pill(r['status'])}</td><td>{E(r['n'])}</td></tr>" for r in season_rows)
    tasks = con.execute("SELECT kind, COUNT(*) n FROM tasks WHERE status='open' GROUP BY kind").fetchall()
    trow = "".join(f"<li><a href='/tasks?kind={E(urllib.parse.quote(t['kind']))}'>{E(t['kind'])}</a>: {E(t['n'])}</li>" for t in tasks) or "<li class='muted'>none</li>"
    events = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT 12").fetchall()
    erow = "".join(f"<li><span class='muted'>{E(str(e['at'] or '')[:16])}</span> {E(e['kind'])} {E(e['ref'])} <span class='muted'>{E(str(e['detail'] or '')[:120])}</span></li>" for e in events)
    return page("Dashboard", f"""<h1>Dashboard</h1>{ecard}{server_card()}
<div class="pair"><div class="card"><h2>Applications</h2><table><tr><th>Season</th><th>Status</th><th>Count</th></tr>{rows}</table></div>
<div class="card"><h2>Open tasks</h2><ul>{trow}</ul>
<form method="post" action="/actions/pull" class="inline"><button class="btn btn-primary small">Pull new applications</button></form>
<form method="post" action="/actions/match" class="inline"><button class="btn btn-secondary small">Run matching</button></form>
<form method="post" action="/actions/push" class="inline"><button class="btn btn-ghost small">Push statuses</button></form></div></div>
<h2>Recent activity</h2><ul>{erow}</ul>""", con)


def server_card():
    """What the Worker says the season is, and a loud line when this Mac's config differs."""
    try:
        from .sync import load_config
        cfg = load_config()
    except Exception as e:
        cfg = {}; cfg_err = f"{type(e).__name__}: {e}"
    else:
        cfg_err = ""
    info = SERVER.get("info")
    if isinstance(info, dict):
        reason = info.get("reason") or ""
        gate = "open" if info.get("open") else "not open yet" if reason == "not_open" else "closed" if reason == "closed" else f"closed ({E(reason or '?')})"
        line = f"Server: season <b>{E(info.get('season'))}</b> · mode <b>{E(info.get('mode'))}</b> · {gate} · {E(info.get('opens'))} to {E(info.get('closes'))}"
        drift = config_drift(cfg, info)
        warn = f"<p><b>config on this Mac differs from the server; run git pull</b> (differs: {E(', '.join(drift))}; the console and the cards follow the server).</p>" if drift else ""
    else:
        line = f"Server: not reached ({E(SERVER.get('error') or 'not checked yet')}). This Mac's config/season.json is used: mode <b>{E(cfg.get('mode'))}</b>, season <b>{E(cfg.get('season'))}</b>."
        warn = f"<p><b>{E(cfg_err)}</b></p>" if cfg_err else ""
    return f"<div class='card{' warn' if warn else ''}'><p>{line} <span class='muted'>checked {E(str(SERVER.get('at') or '')[:16])}</span></p>{warn}</div>"


def close_form(t):
    return f"""<form method="post" action="/tasks/{E(t['id'])}/close" class="inline"><button type="submit" disabled hidden tabindex="-1" aria-hidden="true"></button><input name="resolution" placeholder="note" style="min-height:36px;font:inherit;padding:4px 8px">
<button class="btn btn-ghost small" name="status" value="done">Done</button> <button class="btn btn-ghost small" name="status" value="dismissed">Dismiss</button></form>"""


def task_card(t, con):
    body = f"<h3>{E(t['title'])} <span class='pill'>{E(t['kind'])}</span></h3><p style='white-space:pre-wrap'>{E(t['detail'])}</p>"
    if t["candidate_id"]:
        c = con.execute("SELECT * FROM candidates WHERE id=?", (t["candidate_id"],)).fetchone()
        A = load_apps(con, "id=?", (c["app_a"],)) if c else []
        B = load_apps(con, "id=?", (c["app_b"],)) if c else []
        if c and A and B:
            body += f"<div class='pair'>{app_card(A[0], con)}{app_card(B[0], con)}</div>"
            body += f"<p>Score {float(c['score'] or 0):.2f} · {E(c['verdict'] or '?')} by {E(c['decided_by'] or '?')}</p>"
            if c["verdict"] == "void" or A[0].get("status") == "superseded" or B[0].get("status") == "superseded":
                body += "<p class='muted'>One side was replaced by the applicant's edit; the pair is void. Matching raises a fresh pair for the new application.</p>"
            else:
                same_season = A[0].get("season") == B[0].get("season")
                body += f"""<form method="post" action="/candidates/{E(c['id'])}/resolve" class="inline"><input type="hidden" name="task" value="{E(t['id'])}">
<button class="btn btn-primary small" name="verdict" value="same">Same family</button> <button class="btn btn-ghost small" name="verdict" value="different">Different families</button></form>"""
                if same_season:
                    body += "<p class='hint'>Same family this season: the two are linked and the later one (or the one not accepted) is marked duplicate, so only one can be accepted.</p>"
        else:
            body += "<p class='muted'>pair no longer available</p>"
    elif t["app_id"]:
        A = load_apps(con, "id=?", (t["app_id"],))
        body += app_card(A[0], con) if A else "<p class='muted'>application no longer available</p>"
    if t["kind"] == "decrypt_error":
        sid = error_task_id(t["title"])
        if sid:  # junk or spam: remove the row from the server. The id must be typed back; the handler compares it exactly.
            body += f"""<p class='muted'>Two causes. <b>Key mismatch</b> (sent before this Mac's key was in the live site, or this Mac has the wrong key file): nothing to delete; another board Mac can read it, and <code>bin/review retry-failed</code> stores it here once the right key is on this Mac. <b>Junk or spam</b>: only if another board Mac with the season key cannot read it either. Deleting is permanent.</p>
<form method="post" action="/tasks/{E(t['id'])}/delete-server" class="inline"><input name="confirm" placeholder="type {E(sid)} to delete" autocomplete="off" style="min-height:36px;font:inherit;padding:4px 8px">
<button class="btn btn-ghost small">Delete from server</button></form> """
    return body + close_form(t)


def tasks_page(con, q):
    kind = q.get("kind", [""])[0]; show = q.get("show", ["open"])[0]
    where = "status=?" + (" AND kind=?" if kind else "")
    args = (show,) + ((kind,) if kind else ())
    rows = con.execute(f"SELECT * FROM tasks WHERE {where} ORDER BY created_at", args).fetchall()
    items = []
    for t in rows:
        try:
            body = task_card(t, con)
        except Exception as e:
            body = error_card(f"Task {t['id']}", e) + close_form(t)
        items.append(f"<section class='card'>{body}</section>")
    kinds = con.execute("SELECT DISTINCT kind FROM tasks").fetchall()
    filt = " ".join(f"<a href='/tasks?kind={E(urllib.parse.quote(k['kind']))}'>{E(k['kind'])}</a>" for k in kinds)
    return page("Tasks", f"<h1>Tasks</h1><p><a href='/tasks'>all open</a> · {filt} · <a href='/tasks?show=done'>done</a></p>" + ("".join(items) or "<p class='muted'>Nothing to do.</p>"), con)


def app_row(a):
    ap = d(d(a.get("payload")).get("applicant")); n = d(a.get("norm"))
    return f"""<tr><td><a href="/apps/{E(a['id'])}">{E(a['id'])}</a></td><td>{E(a.get('season'))}</td><td>{name_of(ap)}</td>
<td>{E(n.get('addr_key'))} {E(n.get('zip'))}</td><td>{E(n.get('n_children', '?'))}</td><td>{pill(a.get('status'))}</td><td>{'✓' if a.get('family_id') else ''}</td></tr>"""


def apps_page(con, q):
    season = q.get("season", [""])[0]; status = q.get("status", [""])[0]; search = q.get("q", [""])[0]
    where, args = ["1=1"], []
    if season: where.append("season=?"); args.append(season)
    if status: where.append("status=?"); args.append(status)
    apps = load_apps(con, " AND ".join(where), tuple(args))
    if search.strip():
        needle = fold(search.strip())
        apps = [a for a in apps if needle in fold(json.dumps(a["payload"], ensure_ascii=False))]
    apps.sort(key=lambda a: str(a.get("submitted_at") or ""), reverse=True)
    rows = []
    for a in apps:
        try:
            rows.append(app_row(a))
        except Exception as e:
            rows.append("<tr><td colspan='7'>" + error_card(f"Application {a.get('id', '?')}", e) + "</td></tr>")
    return page("Applications", f"""<h1>Applications <span class="muted">{len(apps)}</span></h1>
<form method="get" class="toolbar"><input name="q" value="{E(search)}" placeholder="search" style="min-height:40px;font:inherit;padding:4px 8px"> <input name="season" value="{E(season)}" placeholder="season" size="6" style="min-height:40px;font:inherit;padding:4px 8px">
<select name="status" style="min-height:40px;font:inherit"><option value="">any status</option>{''.join(f'<option {"selected" if s == status else ""}>{s}</option>' for s in FILTER_STATUSES)}</select> <button class="btn btn-ghost small">Filter</button></form>
<table><tr><th>ID</th><th>Season</th><th>Head of household</th><th>Address</th><th>Kids</th><th>Status</th><th>Family</th></tr>{''.join(rows)}</table>""", con)


def reasons_of(c):
    try:
        return ", ".join(str(x) for x in json.loads(c["reasons"] or "[]"))
    except (ValueError, TypeError):
        return str(c["reasons"] or "")


def app_page(con, app_id):
    apps = load_apps(con, "id=?", (app_id,))
    if not apps:
        return None
    a = apps[0]
    cands = con.execute("SELECT * FROM candidates WHERE app_a=? OR app_b=? ORDER BY score DESC", (app_id, app_id)).fetchall()
    crow = "".join(f"<li><a href='/apps/{E(c['app_b'] if c['app_a'] == app_id else c['app_a'])}'>{E(c['app_b'] if c['app_a'] == app_id else c['app_a'])}</a> · score {float(c['score'] or 0):.2f} · {E(c['verdict'] or '?')} ({E(c['decided_by'])}) · {E(reasons_of(c))}</li>" for c in cands)
    return page(app_id, f"<h1>{E(app_id)}</h1>{app_card(a, con)}<h2>Candidate matches</h2><ul>{crow or '<li class=muted>none</li>'}</ul>", con)


def families_page(con, fid=None):
    if fid:
        f = con.execute("SELECT * FROM families WHERE id=?", (fid,)).fetchone()
        if not f:
            return None
        apps = load_apps(con, "family_id=?", (fid,)); apps.sort(key=lambda a: str(a.get("season") or ""))
        return page(f["display_name"], f"<h1>{E(f['display_name'])}</h1><p>Seasons served: {E(f['seasons_served'])} · trust {E(f['trust'])} · {E(f['first_season'])}–{E(f['last_season'])}</p>" + "".join(app_card(a, con) for a in apps), con)
    fams = con.execute("SELECT f.*, (SELECT COUNT(*) FROM applications a WHERE a.family_id=f.id) n FROM families f ORDER BY display_name").fetchall()
    rows = "".join(f"<tr><td><a href='/families/{E(f['id'])}'>{E(f['display_name'])}</a></td><td>{E(f['first_season'])}–{E(f['last_season'])}</td><td>{E(f['seasons_served'])}</td><td>{E(f['trust'])}</td><td>{E(f['n'])}</td></tr>" for f in fams)
    return page("Families", f"<h1>Families <span class='muted'>{len(fams)}</span></h1><table><tr><th>Family</th><th>Seasons</th><th>Served</th><th>Trust</th><th>Applications</th></tr>{rows}</table>", con)


def contact_lang(a):
    """The applicant's chosen contact language, else the form language, else English."""
    ap = d(d(a.get("payload")).get("applicant"))
    lang = ap.get("contact_lang") or a.get("lang") or "en"
    return "es" if str(lang).lower().startswith("es") else "en"


def export_csv(con, season, kind):
    """Accepted applications as CSV, with a BOM so Excel and Numbers read UTF-8 names."""
    apps = load_apps(con, "season=? AND status='accepted'", (season,))
    out = io.StringIO(); w = csv.writer(out)
    if kind == "labels":
        w.writerow(["id", "name", "address1", "address2", "city", "zip", "lang"])
        for a in apps:
            try:
                p = d(a.get("payload")); ap = d(p.get("applicant")); ml = d(p.get("mailing")); ad = d(p.get("address"))
                if ml:  # an explicit mailing address is one line (a PO box or a full street line); no unit field.
                    # The form makes the mailing city and ZIP optional: a bare PO box line takes the home city and ZIP.
                    w.writerow([a["id"], f"{ap.get('first_name', '') or ''} {ap.get('last_name', '') or ''}".strip(), ml.get("street", "") or "", "",
                                str(ml.get("city") or "").strip() or ad.get("city", "") or "", str(ml.get("zip") or "").strip() or ad.get("zip", "") or "", contact_lang(a)])
                else:
                    w.writerow([a["id"], f"{ap.get('first_name', '') or ''} {ap.get('last_name', '') or ''}".strip(), ad.get("street", "") or "", ad.get("unit", "") or "", ad.get("city", "") or "", ad.get("zip", "") or "", contact_lang(a)])
            except Exception as e:
                w.writerow([a.get("id", "?"), f"ERROR {type(e).__name__}", "", "", "", "", ""])
    else:
        w.writerow(["id", "name", "phone", "text_ok", "lang", "adults", "adult_coats", "children", "child_coats", "food", "toys", "coats", "family_seasons"])
        for a in apps:
            try:
                p = d(a.get("payload")); ap = d(p.get("applicant")); h = d(p.get("household")); pr = d(p.get("programs")); kids = dl(p.get("children"))
                fam = con.execute("SELECT seasons_served FROM families WHERE id=?", (a["family_id"],)).fetchone() if a.get("family_id") else None
                w.writerow([a["id"], f"{ap.get('first_name', '') or ''} {ap.get('last_name', '') or ''}".strip(), ap.get("phone", "") or "", "yes" if ap.get("can_text") else "no", contact_lang(a),
                            h.get("adults", "") if h.get("adults") is not None else "", " ".join(sl(h.get("adult_coat_sizes"))),
                            "; ".join(f"{c.get('first_name', '') or ''} {c.get('age', '') if c.get('age') is not None else ''} {c.get('sex', '') or ''}".strip() for c in kids),
                            "; ".join(f"{c.get('first_name', '') or ''} {c.get('coat_size', '') or ''}".strip() for c in kids if c.get("coat")),
                            "yes" if pr.get("food") else "no", "yes" if pr.get("toys") else "no", "yes" if pr.get("coats") else "no", fam["seasons_served"] if fam else 0])
            except Exception as e:
                w.writerow([a.get("id", "?"), f"ERROR {type(e).__name__}"] + [""] * 11)
    return "﻿" + out.getvalue()


def exports_page(con):
    seasons = [r["season"] for r in con.execute("SELECT DISTINCT season FROM applications ORDER BY season DESC")]
    links = "".join(f"<li>{E(s)}: <a href='/export/roster.csv?season={E(urllib.parse.quote(s))}'>roster</a> · <a href='/export/labels.csv?season={E(urllib.parse.quote(s))}'>mailing labels</a> · <a href='/export/cards?season={E(urllib.parse.quote(s))}'>pickup cards (print)</a></li>" for s in seasons)
    return page("Exports", f"<h1>Exports</h1><p>Accepted applications only.</p><ul>{links}</ul>", con)


def cards_page(con, season, mode=None):
    """One printable card per accepted application. The message follows the season mode
    (pickup, mail, deliver) in the applicant's contact language. The mode is the deployed
    Worker's when the console reached it, else this Mac's config."""
    if mode is None:
        mode = server_mode()
    if mode is None:
        from .sync import load_config
        mode = load_config().get("mode", "pickup")
    text = CARD_TEXT.get(mode, CARD_TEXT["pickup"])
    apps = load_apps(con, "season=? AND status='accepted'", (season,))
    cards = []
    for a in apps:
        try:
            p = d(a.get("payload")); ap = d(p.get("applicant")); hh = d(p.get("household"))
            cards.append(f"""<div style="border:1px solid #000;padding:12px;break-inside:avoid;margin-bottom:8px"><h3 style="margin:0">Truckee Community Cares {E(season)}</h3>
<p style="font-size:1.4rem;font-weight:800;margin:4px 0">{E(a['id'])}</p><p>{name_of(ap)}<br>
Children: {len(dl(p.get('children')))} · Adults: {E(hh.get('adults', ''))}</p>
<p>{E(text[contact_lang(a)])}</p></div>""")
        except Exception as e:
            cards.append(error_card(f"Application {a.get('id', '?')}", e))
    return f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Cards {E(season)}</title><style>body{{font-family:system-ui;display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:8px}}</style></head><body>{''.join(cards)}</body></html>"


def safe_filename(text):
    return re.sub(r"[^A-Za-z0-9_-]", "", str(text or ""))[:40] or "export"


def make_handler(con, port):
    """The request handler class, bound to one connection and one port."""
    origins = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
    hosts = (f"127.0.0.1:{port}", f"localhost:{port}")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def send(self, body, ctype="text/html; charset=utf-8", status=200, extra=None):
            b = body.encode("utf-8"); self.send_response(status); self.send_header("content-type", ctype); self.send_header("content-length", str(len(b)))
            self.send_header("cache-control", "no-store"); self.send_header("content-security-policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers(); self.wfile.write(b)

        def send_csv(self, body, filename):
            self.send(body, "text/csv; charset=utf-8", extra={"content-disposition": f'attachment; filename="{safe_filename(filename)}.csv"'})

        def redirect(self, to):
            self.send_response(303); self.send_header("location", to); self.send_header("content-security-policy", CSP); self.send_header("content-length", "0"); self.end_headers()

        def same_origin(self):
            """A POST must come from this console: Host is ours and Origin (or Referer) is ours.
            That keeps a page in another tab from submitting decisions here."""
            if self.headers.get("host", "") not in hosts:
                return False
            src = self.headers.get("origin") or self.headers.get("referer") or ""
            return any(src == o or src.startswith(o + "/") for o in origins)

        def fail(self, e, where):
            """Any error, SystemExit included: log it, undo uncommitted work, and show it on the
            dashboard as an error card. The server keeps running. The dashboard itself failing
            is the one case that cannot redirect, so it renders the 500 page instead."""
            msg = f"{type(e).__name__}: {e}"[:500]
            try:
                con.rollback()
            except Exception:
                pass
            try:
                log(con, "error", where, msg); con.commit()
            except Exception:
                pass
            if where == "/":
                return self.send(error_page("Something went wrong", f"{msg}\n\n{traceback.format_exc()}"), status=500)
            self.redirect("/?error=" + urllib.parse.quote(msg))

        def do_GET(self):
            try:
                self.route_get()
            except (Exception, SystemExit) as e:
                self.fail(e, self.path.split("?")[0])

        def route_get(self):
            u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query); p = u.path
            season = q.get("season", [""])[0]
            if p == "/": return self.send(dashboard(con, q))
            if p == "/tasks": return self.send(tasks_page(con, q))
            if p == "/apps": return self.send(apps_page(con, q))
            if p.startswith("/apps/"):
                body = app_page(con, p.split("/")[2]); return self.send(body) if body else self.send(error_page("Not found"), status=404)
            if p == "/families": return self.send(families_page(con))
            if p.startswith("/families/"):
                body = families_page(con, p.split("/")[2]); return self.send(body) if body else self.send(error_page("Not found"), status=404)
            if p == "/exports": return self.send(exports_page(con))
            if p == "/export/roster.csv": return self.send_csv(export_csv(con, season, "roster"), f"roster-{season}")
            if p == "/export/labels.csv": return self.send_csv(export_csv(con, season, "labels"), f"labels-{season}")
            if p == "/export/cards": return self.send(cards_page(con, season))
            self.send(error_page("Not found"), status=404)

        def do_POST(self):
            try:
                if not self.same_origin():
                    return self.send(error_page("Forbidden", "This request did not come from the console page."), status=403)
                self.route_post()
            except (Exception, SystemExit) as e:  # an action failed part way, or a missing key: say so on the dashboard
                self.fail(e, self.path.split("?")[0])

        def route_post(self):
            n = int(self.headers.get("content-length", 0)); form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            g = lambda k: form.get(k, [""])[0]  # noqa: E731
            p = self.path.split("?")[0]
            if p in ("/actions/pull", "/actions/match", "/actions/push"):
                if p == "/actions/pull":
                    from .sync import pull; pull(con); refresh_server_info()
                elif p == "/actions/match":
                    from .sync import load_config; run_matching(con, load_config()["season"])
                else:
                    from .sync import push_statuses; push_statuses(con)
                return self.redirect("/")
            if p.startswith("/tasks/") and p.endswith("/delete-server"):
                t = con.execute("SELECT * FROM tasks WHERE id=?", (p.split("/")[2],)).fetchone()
                sid = error_task_id(t["title"]) if t and t["kind"] == "decrypt_error" else None
                if sid and g("confirm") == sid:
                    from .sync import delete_server; delete_server(con, sid)
                return self.redirect("/tasks")
            if p.startswith("/apps/") and p.endswith("/decide"):
                status = g("status"); app_id = p.split("/")[2]; note = g("note")
                if status == NOTE_SAVE:
                    set_note(con, app_id, note)  # Enter in the note field: the note, and only the note
                elif status in STATUSES:
                    if status != "needs_info":
                        # The note belongs to needs_info. Under any other status it is kept only
                        # when the person typed something new; the prefilled old note is dropped.
                        old = con.execute("SELECT status, note FROM applications WHERE id=?", (app_id,)).fetchone()
                        if old is not None and status != old["status"] and note.strip() == (old["note"] or "").strip():
                            note = ""
                    set_decision(con, app_id, status, note)
                ref = self.headers.get("referer") or ""
                return self.redirect(ref if any(ref.startswith(o + "/") for o in origins) else "/apps")
            if p.startswith("/candidates/") and p.endswith("/resolve"):
                verdict = g("verdict")
                t = con.execute("SELECT status FROM tasks WHERE id=?", (g("task"),)).fetchone() if g("task") else None
                if verdict in ("same", "different") and not (t and t["status"] != "open"):  # a task already closed is not resolved twice
                    outcome = resolve_candidate(con, p.split("/")[2], verdict)
                    if g("task"): con.execute("UPDATE tasks SET status='done', resolved_at=?, resolution=? WHERE id=?", (now(), f"human: {outcome}", g("task"))); con.commit()
                return self.redirect("/tasks")
            if p.startswith("/tasks/") and p.endswith("/close"):
                status = g("status")
                if status in ("done", "dismissed"):
                    con.execute("UPDATE tasks SET status=?, resolved_at=?, resolution=? WHERE id=?", (status, now(), g("resolution"), p.split("/")[2])); con.commit()
                return self.redirect("/tasks")
            self.redirect("/")

    return H


def serve(con, port=8789):
    refresh_server_info()  # the dashboard shows the Worker's season and mode next to this Mac's config
    srv = HTTPServer(("127.0.0.1", port), make_handler(con, port))
    print(f"console at http://127.0.0.1:{port}/  (Ctrl-C to stop)")
    try:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
