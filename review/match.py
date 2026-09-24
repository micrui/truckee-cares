"""Candidate generation, scoring, and the decision ladder.

  1. Blocking: an application is compared only with applications sharing a phone,
     an address key, a last name + zip, or two child first names.
  2. Scoring: explainable points per shared signal, with rules for helper phones
     and age drift between seasons.
  3. Ladder: certain -> link by rule; gray zone -> ask the judge; judge unsure
     -> task for a person. Same-season matches raise a duplicate task; prior-season
     matches attach the application to the existing family (continuity).
"""
import json
from itertools import combinations

from rapidfuzz import fuzz

from .db import add_task, log, new_id, now
from .judge import judge_pair

CERTAIN = 0.90
GRAY_LOW = 0.45


def children_overlap(a, b, season_gap):
    """Fraction of A's children that appear in B allowing age drift of season_gap ± 1."""
    if not a or not b:
        return 0.0
    hits = 0
    for ca in a:
        for cb in b:
            if not ca["first"] or not cb["first"]:
                continue
            name_ok = ca["first"] == cb["first"] or fuzz.ratio(ca["first"], cb["first"]) >= 85
            if not name_ok:
                continue
            if ca["age"] is None or cb["age"] is None:
                hits += 0.7
                break
            expected = ca["age"] + season_gap
            if abs(cb["age"] - expected) <= 1:
                hits += 1
                break
    return hits / max(len(a), len(b))


def score_pair(A, B):
    """Returns (score 0..1, reasons)."""
    a, b = A["norm"], B["norm"]
    gap = int(B["season"]) - int(A["season"]) if A["season"].isdigit() and B["season"].isdigit() else 0
    pts, reasons = 0.0, []
    shared_phone = set(a["phones"]) & set(b["phones"])
    if shared_phone:
        pts += 0.45; reasons.append("same phone")
    if a["email"] and a["email"] == b["email"]:
        pts += 0.35; reasons.append("same email")
    if a["addr_key"] and a["addr_key"] == b["addr_key"]:
        pts += 0.35; reasons.append("same street number and street")
    elif a["addr"] and b["addr"] and fuzz.token_set_ratio(a["addr"], b["addr"]) >= 90:
        pts += 0.25; reasons.append("address text nearly identical")
    if a["mail_key"] and a["mail_key"] == b["mail_key"]:
        pts += 0.2; reasons.append("same mailing address")
    full = fuzz.token_set_ratio(a["full"], b["full"]) if a["full"] and b["full"] else 0
    if full >= 92:
        pts += 0.3; reasons.append("same head of household name")
    elif full >= 75:
        pts += 0.15; reasons.append("similar head of household name")
    cross = [(a["other_adult"], b["full"]), (b["other_adult"], a["full"])]
    if any(x and y and fuzz.token_set_ratio(x, y) >= 85 for x, y in cross):
        pts += 0.3; reasons.append("one lists the other as the other adult")
    if a["last"] and a["last"] == b["last"] and a["zip"] and a["zip"] == b["zip"]:
        pts += 0.1; reasons.append("same last name and zip")
    ov = children_overlap(a["children"], b["children"], gap)
    if ov >= 0.99 and len(a["children"]) >= 2:
        pts += 0.45; reasons.append("all children match (name and age)")
    elif ov >= 0.5:
        pts += 0.25; reasons.append(f"{int(ov*100)}% of children match")
    elif a["children"] and b["children"] and ov == 0:
        pts -= 0.2; reasons.append("children do not match")
    if a["helper_phone"] and a["helper_phone"] == b["helper_phone"]:
        reasons.append("same helper filed both (phone ignored)")
    return round(max(0.0, min(1.0, pts)), 2), reasons


def block_keys(n):
    keys = set()
    for p in n["phones"]:
        keys.add(("phone", p))
    if n["addr_key"]:
        keys.add(("addr", n["addr_key"]))
    if n["mail_key"]:
        keys.add(("mail", n["mail_key"]))
    if n["last"] and n["zip"]:
        keys.add(("lastzip", n["last"], n["zip"]))
    if n["email"]:
        keys.add(("email", n["email"]))
    for pair in combinations(sorted(n["child_names"]), 2):
        keys.add(("kids", *pair))
    if n["full"]:
        keys.add(("name", n["full"]))
    return keys


def load_apps(con, where="1=1", args=()):
    rows = con.execute(f"SELECT * FROM applications WHERE {where}", args).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["payload"] = json.loads(d["payload"]); d["norm"] = json.loads(d["norm"]); out.append(d)
    return out


def run_matching(con, season, use_judge=True):
    """Match every unprocessed application of `season` against everything else."""
    all_apps = load_apps(con)
    by_id = {a["id"]: a for a in all_apps}
    index = {}
    for a in all_apps:
        for k in block_keys(a["norm"]):
            index.setdefault(k, set()).add(a["id"])
    todo = [a for a in all_apps if a["season"] == season and a["status"] == "new"]
    stats = {"apps": len(todo), "pairs": 0, "rule": 0, "llm": 0, "tasks": 0}
    for A in todo:
        cands = set()
        for k in block_keys(A["norm"]):
            cands |= index.get(k, set())
        cands.discard(A["id"])
        for bid in sorted(cands):
            B = by_id[bid]
            lo, hi = sorted([A["id"], bid])
            if con.execute("SELECT 1 FROM candidates WHERE app_a=? AND app_b=?", (lo, hi)).fetchone():
                continue
            score, reasons = score_pair(A, B) if A["season"] <= B["season"] else score_pair(B, A)
            if score < GRAY_LOW:
                continue
            stats["pairs"] += 1
            cid = new_id("cand")
            verdict, by, judged = None, None, None
            if score >= CERTAIN:
                verdict, by = "same", "rule"; stats["rule"] += 1
            elif use_judge:
                judged = judge_pair(A, B, reasons); stats["llm"] += 1
                verdict, by = judged["verdict"], "llm"
            else:
                verdict, by = "unsure", "rule"
            con.execute("INSERT INTO candidates(id,app_a,app_b,score,reasons,verdict,decided_by,judge,decided_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (cid, lo, hi, score, json.dumps(reasons), verdict, by, json.dumps(judged) if judged else None, now()))
            apply_verdict(con, cid, A, B, verdict, reasons, judged, stats)
        if A["status"] == "new":
            con.execute("UPDATE applications SET status='matched', updated_at=? WHERE id=?", (now(), A["id"]))
        A["status"] = "matched"
        if not A["norm"]["in_area"]:
            add_task(con, "verify_address", f"Out of area: {A['payload']['applicant'].get('first_name','')} {A['payload']['applicant'].get('last_name','')} ({A['norm']['city'] or '?'} {A['norm']['zip']})", app_id=A["id"],
                     detail="Address is outside Truckee and Soda Springs. Decline, or accept if it is a known edge case.")
            stats["tasks"] += 1
        if A["payload"].get("notes"):
            add_task(con, "review_notes", f"Applicant note from {A['payload']['applicant'].get('first_name','')}", app_id=A["id"], detail=A["payload"]["notes"])
            stats["tasks"] += 1
    log(con, "match", season, json.dumps(stats))
    con.commit()
    return stats


def apply_verdict(con, cid, A, B, verdict, reasons, judged, stats):
    label = lambda X: f"{X['payload']['applicant'].get('first_name','')} {X['payload']['applicant'].get('last_name','')} ({X['season']})"
    if verdict == "same":
        if A["season"] == B["season"]:
            add_task(con, "resolve_duplicate", f"Duplicate this season: {label(A)} and {label(B)}", app_id=A["id"], candidate_id=cid,
                     detail="; ".join(reasons) + (f"\nJudge: {judged['reason']}" if judged else ""))
            stats["tasks"] += 1
        else:
            link_family(con, A, B)
    elif verdict == "unsure":
        q = (judged or {}).get("question_for_applicant", "")
        kind = "contact_applicant" if (judged or {}).get("suggested_action") == "contact_applicant" and q else "review_match"
        add_task(con, kind, f"Same family? {label(A)} vs {label(B)}", app_id=A["id"], candidate_id=cid,
                 detail="; ".join(reasons) + (f"\nJudge: {judged['reason']}" if judged else "") + (f"\nAsk: {q}" if q else ""))
        stats["tasks"] += 1


def link_family(con, A, B):
    """Attach A to B's family (creating it if needed). Continuity: seasons_served and trust come from accepted prior apps."""
    fid = B["family_id"] or A["family_id"]
    if not fid:
        fid = new_id("fam")
        p = B["payload"]["applicant"]
        con.execute("INSERT INTO families(id,created_at,display_name,first_season,last_season) VALUES(?,?,?,?,?)",
                    (fid, now(), f"{p.get('last_name','')}, {p.get('first_name','')}".strip(", "), min(A["season"], B["season"]), max(A["season"], B["season"])))
    for X in (A, B):
        if not X["family_id"]:
            con.execute("UPDATE applications SET family_id=? WHERE id=?", (fid, X["id"]))
            X["family_id"] = fid
    recompute_family(con, fid)
    return fid


def recompute_family(con, fid):
    rows = con.execute("SELECT season, status FROM applications WHERE family_id=?", (fid,)).fetchall()
    seasons = sorted({r["season"] for r in rows})
    served = len({r["season"] for r in rows if r["status"] == "accepted"})
    problems = sum(1 for r in rows if r["status"] in ("duplicate", "out_of_area", "declined"))
    con.execute("UPDATE families SET first_season=?, last_season=?, seasons_served=?, trust=? WHERE id=?",
                (seasons[0] if seasons else None, seasons[-1] if seasons else None, served, served - problems, fid))


def set_decision(con, app_id, status, note=""):
    con.execute("UPDATE applications SET status=?, updated_at=? WHERE id=?", (status, now(), app_id))
    r = con.execute("SELECT family_id FROM applications WHERE id=?", (app_id,)).fetchone()
    if r and r["family_id"]:
        recompute_family(con, r["family_id"])
    log(con, "decision", app_id, f"{status} {note}".strip())
    con.commit()


def resolve_candidate(con, cid, verdict):
    """A human overrides or confirms a candidate pair."""
    c = con.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
    A = load_apps(con, "id=?", (c["app_a"],))[0]; B = load_apps(con, "id=?", (c["app_b"],))[0]
    con.execute("UPDATE candidates SET verdict=?, decided_by='human', decided_at=? WHERE id=?", (verdict, now(), cid))
    if verdict == "same" and A["season"] != B["season"]:
        link_family(con, A, B)
    con.commit()
