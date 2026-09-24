"""Local admin console: a small server on 127.0.0.1 with server-rendered pages.
No JavaScript framework, no external requests. Pages: dashboard, tasks,
applications, application detail, families, exports."""
import csv
import html
import io
import json
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from .db import now
from .match import load_apps, resolve_candidate, set_decision, run_matching
from .paths import ROOT

E = html.escape
CSS = (ROOT / "site" / "static" / "css" / "site.css").read_text()
EXTRA = """
.wrap{max-width:1100px} table{width:100%;border-collapse:collapse;font-size:.92rem} th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.8rem;font-weight:700;background:var(--card)} .pill.accepted{background:var(--ok);color:#fff}
.pill.duplicate,.pill.declined,.pill.out_of_area{background:var(--err);color:#fff} .pill.new{background:#e0b100;color:#000}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:12px} .pair .card{margin:0} form.inline{display:inline} .btn.small{min-height:36px;padding:4px 12px;font-size:.9rem}
nav a{margin-right:10px} .kv dt{color:var(--muted);font-size:.85rem;margin-top:6px} .kv dd{margin:0}
"""


def page(title, body, con):
    open_tasks = con.execute("SELECT COUNT(*) FROM tasks WHERE status='open'").fetchone()[0]
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{E(title)} · TCC review</title>
<style>{CSS}{EXTRA}</style></head><body><header class="site-header"><div class="wrap row"><a class="brand" href="/">TCC review</a></div>
<nav class="wrap"><a href="/">Dashboard</a><a href="/tasks">Tasks ({open_tasks})</a><a href="/apps">Applications</a><a href="/families">Families</a><a href="/exports">Exports</a></nav></header>
<main class="wrap">{body}</main></body></html>"""


def pill(status):
    return f'<span class="pill {E(status)}">{E(status)}</span>'


def app_card(a, con):
    p = a["payload"]; ap = p.get("applicant", {}); ad = p.get("address", {}) or {}; ml = p.get("mailing"); hp = p.get("helper")
    kids = "".join(f"<li>{E(str(c.get('first_name','')))} · {E(str(c.get('age','?')))} · {E(str(c.get('sex','')))}{' · coat ' + E(str(c.get('coat_size',''))) if c.get('coat') else ''}</li>" for c in p.get("children", []))
    fam = con.execute("SELECT * FROM families WHERE id=?", (a["family_id"],)).fetchone() if a["family_id"] else None
    return f"""<div class="card"><h3><a href="/apps/{E(a['id'])}">{E(a['id'])}</a> {pill(a['status'])} <span class="muted">{E(a['season'])} · {E(a['source'])} · {E(a['submitted_at'][:16])}</span></h3>
<dl class="kv">
<dt>Head of household</dt><dd><strong>{E(ap.get('first_name',''))} {E(ap.get('last_name',''))}</strong>{(' · other adult: ' + E(ap.get('other_adult'))) if ap.get('other_adult') else ''}</dd>
<dt>Phone</dt><dd>{E(ap.get('phone',''))}{' (ok to text)' if ap.get('can_text') else ''}{(' · ' + E(ap.get('other_phone'))) if ap.get('other_phone') else ''}{(' · ' + E(ap.get('email'))) if ap.get('email') else ''}</dd>
{('<dt>Filed by helper</dt><dd>' + E(hp.get('name','')) + ' ' + E(hp.get('phone','')) + ' ' + E(hp.get('org','')) + '</dd>') if hp else ''}
<dt>Address</dt><dd>{E(ad.get('street',''))} {E(ad.get('unit',''))}, {E(ad.get('city',''))} {E(ad.get('zip',''))} {'' if ad.get('in_area', True) else '<span class="pill out_of_area">out of area</span>'}</dd>
{('<dt>Mailing</dt><dd>' + E(ml.get('street','')) + ', ' + E(ml.get('city','')) + ' ' + E(ml.get('zip','')) + '</dd>') if ml else ''}
<dt>Adults</dt><dd>{E(str((p.get('household') or {}).get('adults','?')))}{(' · coats: ' + E(', '.join((p.get('household') or {}).get('adult_coat_sizes', [])))) if (p.get('household') or {}).get('adult_coat_sizes') else ''}</dd>
<dt>Children</dt><dd><ul>{kids or '<li class="muted">none</li>'}</ul>{('<div class="muted">raw: ' + E(p.get('children_raw','')) + '</div>') if p.get('children_raw') else ''}</dd>
<dt>Requested</dt><dd>{', '.join(k for k, v in (p.get('programs') or {}).items() if v)}</dd>
{('<dt>Referral</dt><dd>' + E(p.get('referral')) + '</dd>') if p.get('referral') else ''}
{('<dt>Notes</dt><dd>' + E(p.get('notes')) + '</dd>') if p.get('notes') else ''}
<dt>Family</dt><dd>{('<a href="/families/' + E(fam['id']) + '">' + E(fam['display_name']) + '</a> · seasons served: ' + str(fam['seasons_served']) + ' · trust ' + str(fam['trust'])) if fam else '<span class="muted">not linked</span>'}</dd>
</dl>
<form method="post" action="/apps/{E(a['id'])}/decide" class="inline">
{''.join(f'<button class="btn btn-ghost small" name="status" value="{s}">{s}</button> ' for s in ['accepted', 'hold', 'declined', 'duplicate', 'out_of_area'])}
</form></div>"""


def dashboard(con):
    season_rows = con.execute("SELECT season, status, COUNT(*) n FROM applications GROUP BY season, status ORDER BY season DESC, status").fetchall()
    rows = "".join(f"<tr><td>{E(r['season'])}</td><td>{pill(r['status'])}</td><td>{r['n']}</td></tr>" for r in season_rows)
    tasks = con.execute("SELECT kind, COUNT(*) n FROM tasks WHERE status='open' GROUP BY kind").fetchall()
    trow = "".join(f"<li><a href='/tasks?kind={E(t['kind'])}'>{E(t['kind'])}</a>: {t['n']}</li>" for t in tasks) or "<li class='muted'>none</li>"
    events = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT 12").fetchall()
    erow = "".join(f"<li><span class='muted'>{E(e['at'][:16])}</span> {E(e['kind'])} {E(e['ref'] or '')} <span class='muted'>{E((e['detail'] or '')[:120])}</span></li>" for e in events)
    return page("Dashboard", f"""<h1>Dashboard</h1>
<div class="pair"><div class="card"><h2>Applications</h2><table><tr><th>Season</th><th>Status</th><th>Count</th></tr>{rows}</table></div>
<div class="card"><h2>Open tasks</h2><ul>{trow}</ul>
<form method="post" action="/actions/pull" class="inline"><button class="btn btn-primary small">Pull new applications</button></form>
<form method="post" action="/actions/match" class="inline"><button class="btn btn-secondary small">Run matching</button></form>
<form method="post" action="/actions/push" class="inline"><button class="btn btn-ghost small">Push statuses</button></form></div></div>
<h2>Recent activity</h2><ul>{erow}</ul>""", con)


def tasks_page(con, q):
    kind = q.get("kind", [""])[0]; show = q.get("show", ["open"])[0]
    where = "status=?" + (" AND kind=?" if kind else "")
    args = (show,) + ((kind,) if kind else ())
    rows = con.execute(f"SELECT * FROM tasks WHERE {where} ORDER BY created_at", args).fetchall()
    items = []
    for t in rows:
        body = f"<h3>{E(t['title'])} <span class='pill'>{E(t['kind'])}</span></h3><p style='white-space:pre-wrap'>{E(t['detail'])}</p>"
        if t["candidate_id"]:
            c = con.execute("SELECT * FROM candidates WHERE id=?", (t["candidate_id"],)).fetchone()
            A = load_apps(con, "id=?", (c["app_a"],))[0]; B = load_apps(con, "id=?", (c["app_b"],))[0]
            body += f"<div class='pair'>{app_card(A, con)}{app_card(B, con)}</div>"
            body += f"""<p>Score {c['score']:.2f} · {E(c['verdict'] or '?')} by {E(c['decided_by'] or '?')}</p>
<form method="post" action="/candidates/{E(c['id'])}/resolve" class="inline"><input type="hidden" name="task" value="{E(t['id'])}">
<button class="btn btn-primary small" name="verdict" value="same">Same family</button> <button class="btn btn-ghost small" name="verdict" value="different">Different families</button></form>"""
        elif t["app_id"]:
            A = load_apps(con, "id=?", (t["app_id"],))
            if A: body += app_card(A[0], con)
        body += f"""<form method="post" action="/tasks/{E(t['id'])}/close" class="inline"><input name="resolution" placeholder="note" style="min-height:36px;font:inherit;padding:4px 8px">
<button class="btn btn-ghost small" name="status" value="done">Done</button> <button class="btn btn-ghost small" name="status" value="dismissed">Dismiss</button></form>"""
        items.append(f"<section class='card'>{body}</section>")
    kinds = con.execute("SELECT DISTINCT kind FROM tasks").fetchall()
    filt = " ".join(f"<a href='/tasks?kind={E(k['kind'])}'>{E(k['kind'])}</a>" for k in kinds)
    return page("Tasks", f"<h1>Tasks</h1><p><a href='/tasks'>all open</a> · {filt} · <a href='/tasks?show=done'>done</a></p>" + ("".join(items) or "<p class='muted'>Nothing to do.</p>"), con)


def apps_page(con, q):
    season = q.get("season", [""])[0]; status = q.get("status", [""])[0]; search = q.get("q", [""])[0].lower()
    where, args = ["1=1"], []
    if season: where.append("season=?"); args.append(season)
    if status: where.append("status=?"); args.append(status)
    apps = load_apps(con, " AND ".join(where), tuple(args))
    if search:
        apps = [a for a in apps if search in json.dumps(a["payload"]).lower()]
    apps.sort(key=lambda a: a["submitted_at"], reverse=True)
    rows = "".join(f"""<tr><td><a href="/apps/{E(a['id'])}">{E(a['id'])}</a></td><td>{E(a['season'])}</td><td>{E(a['payload']['applicant'].get('first_name',''))} {E(a['payload']['applicant'].get('last_name',''))}</td>
<td>{E(a['norm']['addr_key'])} {E(a['norm']['zip'])}</td><td>{a['norm']['n_children']}</td><td>{pill(a['status'])}</td><td>{'✓' if a['family_id'] else ''}</td></tr>""" for a in apps)
    return page("Applications", f"""<h1>Applications <span class="muted">{len(apps)}</span></h1>
<form method="get" class="toolbar"><input name="q" value="{E(search)}" placeholder="search" style="min-height:40px;font:inherit;padding:4px 8px"> <input name="season" value="{E(season)}" placeholder="season" size="6" style="min-height:40px;font:inherit;padding:4px 8px">
<select name="status" style="min-height:40px;font:inherit"><option value="">any status</option>{''.join(f'<option {"selected" if s == status else ""}>{s}</option>' for s in ['new','matched','accepted','hold','declined','duplicate','out_of_area'])}</select> <button class="btn btn-ghost small">Filter</button></form>
<table><tr><th>ID</th><th>Season</th><th>Head of household</th><th>Address</th><th>Kids</th><th>Status</th><th>Family</th></tr>{rows}</table>""", con)


def app_page(con, app_id):
    apps = load_apps(con, "id=?", (app_id,))
    if not apps:
        return None
    a = apps[0]
    cands = con.execute("SELECT * FROM candidates WHERE app_a=? OR app_b=? ORDER BY score DESC", (app_id, app_id)).fetchall()
    crow = "".join(f"<li>{E(c['app_b'] if c['app_a'] == app_id else c['app_a'])} · score {c['score']:.2f} · {E(c['verdict'] or '?')} ({E(c['decided_by'] or '')}) · {E(', '.join(json.loads(c['reasons'])))}</li>" for c in cands)
    return page(app_id, f"<h1>{E(app_id)}</h1>{app_card(a, con)}<h2>Candidate matches</h2><ul>{crow or '<li class=muted>none</li>'}</ul>", con)


def families_page(con, fid=None):
    if fid:
        f = con.execute("SELECT * FROM families WHERE id=?", (fid,)).fetchone()
        apps = load_apps(con, "family_id=?", (fid,)); apps.sort(key=lambda a: a["season"])
        return page(f["display_name"], f"<h1>{E(f['display_name'])}</h1><p>Seasons served: {f['seasons_served']} · trust {f['trust']} · {E(f['first_season'] or '')}–{E(f['last_season'] or '')}</p>" + "".join(app_card(a, con) for a in apps), con)
    fams = con.execute("SELECT f.*, (SELECT COUNT(*) FROM applications a WHERE a.family_id=f.id) n FROM families f ORDER BY display_name").fetchall()
    rows = "".join(f"<tr><td><a href='/families/{E(f['id'])}'>{E(f['display_name'])}</a></td><td>{E(f['first_season'] or '')}–{E(f['last_season'] or '')}</td><td>{f['seasons_served']}</td><td>{f['trust']}</td><td>{f['n']}</td></tr>" for f in fams)
    return page("Families", f"<h1>Families <span class='muted'>{len(fams)}</span></h1><table><tr><th>Family</th><th>Seasons</th><th>Served</th><th>Trust</th><th>Applications</th></tr>{rows}</table>", con)


def export_csv(con, season, kind):
    apps = [a for a in load_apps(con, "season=? AND status='accepted'", (season,))]
    out = io.StringIO(); w = csv.writer(out)
    if kind == "labels":
        w.writerow(["id", "name", "address1", "city", "zip", "lang"])
        for a in apps:
            p = a["payload"]; ap = p["applicant"]; m = p.get("mailing") or p.get("address") or {}
            w.writerow([a["id"], f"{ap.get('first_name','')} {ap.get('last_name','')}", m.get("street", ""), m.get("city", ""), m.get("zip", ""), ap.get("contact_lang", a["lang"])])
    else:
        w.writerow(["id", "name", "phone", "text_ok", "lang", "adults", "adult_coats", "children", "child_coats", "food", "toys", "coats", "family_seasons"])
        for a in apps:
            p = a["payload"]; ap = p["applicant"]; h = p.get("household") or {}; pr = p.get("programs") or {}
            fam = con.execute("SELECT seasons_served FROM families WHERE id=?", (a["family_id"],)).fetchone() if a["family_id"] else None
            w.writerow([a["id"], f"{ap.get('first_name','')} {ap.get('last_name','')}", ap.get("phone", ""), "yes" if ap.get("can_text") else "no", ap.get("contact_lang", a["lang"]),
                        h.get("adults", ""), " ".join(h.get("adult_coat_sizes", [])),
                        "; ".join(f"{c.get('first_name','')} {c.get('age','')} {c.get('sex','')}" for c in p.get("children", [])),
                        "; ".join(f"{c.get('first_name','')} {c.get('coat_size','')}" for c in p.get("children", []) if c.get("coat")),
                        pr.get("food"), pr.get("toys"), pr.get("coats"), fam["seasons_served"] if fam else 0])
    return out.getvalue()


def exports_page(con):
    seasons = [r["season"] for r in con.execute("SELECT DISTINCT season FROM applications ORDER BY season DESC")]
    links = "".join(f"<li>{E(s)}: <a href='/export/roster.csv?season={E(s)}'>roster</a> · <a href='/export/labels.csv?season={E(s)}'>mailing labels</a> · <a href='/export/cards?season={E(s)}'>pickup cards (print)</a></li>" for s in seasons)
    return page("Exports", f"<h1>Exports</h1><p>Accepted applications only.</p><ul>{links}</ul>", con)


def cards_page(con, season):
    apps = load_apps(con, "season=? AND status='accepted'", (season,))
    cards = "".join(f"""<div style="border:1px solid #000;padding:12px;break-inside:avoid;margin-bottom:8px"><h3 style="margin:0">Truckee Community Cares {E(season)}</h3>
<p style="font-size:1.4rem;font-weight:800;margin:4px 0">{E(a['id'])}</p><p>{E(a['payload']['applicant'].get('first_name',''))} {E(a['payload']['applicant'].get('last_name',''))}<br>
Children: {len(a['payload'].get('children', []))} · Adults: {E(str((a['payload'].get('household') or {}).get('adults','')))}</p>
<p>{'Traiga esta tarjeta el día de la entrega.' if a['lang'] == 'es' else 'Bring this card on pickup day.'}</p></div>""" for a in apps)
    return f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Cards {E(season)}</title><style>body{{font-family:system-ui;display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:8px}}</style></head><body>{cards}</body></html>"


def serve(con, port=8789):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def send(self, body, ctype="text/html; charset=utf-8", status=200):
            b = body.encode(); self.send_response(status); self.send_header("content-type", ctype); self.send_header("content-length", str(len(b)))
            self.send_header("cache-control", "no-store"); self.end_headers(); self.wfile.write(b)

        def redirect(self, to):
            self.send_response(303); self.send_header("location", to); self.end_headers()

        def do_GET(self):
            u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query); p = u.path
            if p == "/": return self.send(dashboard(con))
            if p == "/tasks": return self.send(tasks_page(con, q))
            if p == "/apps": return self.send(apps_page(con, q))
            if p.startswith("/apps/"):
                body = app_page(con, p.split("/")[2]); return self.send(body) if body else self.send("not found", status=404)
            if p == "/families": return self.send(families_page(con))
            if p.startswith("/families/"): return self.send(families_page(con, p.split("/")[2]))
            if p == "/exports": return self.send(exports_page(con))
            if p == "/export/roster.csv": return self.send(export_csv(con, q.get("season", [""])[0], "roster"), "text/csv")
            if p == "/export/labels.csv": return self.send(export_csv(con, q.get("season", [""])[0], "labels"), "text/csv")
            if p == "/export/cards": return self.send(cards_page(con, q.get("season", [""])[0]))
            self.send("not found", status=404)

        def do_POST(self):
            n = int(self.headers.get("content-length", 0)); form = urllib.parse.parse_qs(self.rfile.read(n).decode())
            g = lambda k: form.get(k, [""])[0]
            p = self.path.split("?")[0]
            if p == "/actions/pull":
                from .sync import pull; pull(con)
            elif p == "/actions/match":
                from .sync import load_config; run_matching(con, load_config()["season"])
            elif p == "/actions/push":
                from .sync import push_statuses; push_statuses(con)
            elif p.startswith("/apps/") and p.endswith("/decide"):
                set_decision(con, p.split("/")[2], g("status"))
                return self.redirect(self.headers.get("referer") or "/apps")
            elif p.startswith("/candidates/") and p.endswith("/resolve"):
                resolve_candidate(con, p.split("/")[2], g("verdict"))
                if g("task"): con.execute("UPDATE tasks SET status='done', resolved_at=?, resolution=? WHERE id=?", (now(), f"human: {g('verdict')}", g("task"))); con.commit()
                return self.redirect("/tasks")
            elif p.startswith("/tasks/") and p.endswith("/close"):
                con.execute("UPDATE tasks SET status=?, resolved_at=?, resolution=? WHERE id=?", (g("status"), now(), g("resolution"), p.split("/")[2])); con.commit()
                return self.redirect("/tasks")
            self.redirect("/")

    srv = HTTPServer(("127.0.0.1", port), H)
    print(f"console at http://127.0.0.1:{port}/  (Ctrl-C to stop)")
    try:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
