import os
import sys
from pathlib import Path

os.environ["TCC_JUDGE"] = "none"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from review import normalize as N  # noqa: E402
from review.db import connect  # noqa: E402
from review.match import run_matching, score_pair, load_apps  # noqa: E402
from review.normalize_app import normalize_payload  # noqa: E402
import json  # noqa: E402


def payload(first, last, phone, street, zip_, kids, season="2026", other_adult="", helper=None):
    return {"version": 1, "season": season, "helper": helper,
            "applicant": {"first_name": first, "last_name": last, "phone": phone, "other_phone": "", "email": "", "other_adult": other_adult},
            "address": {"street": street, "unit": "", "city": "Truckee", "zip": zip_, "in_area": zip_ in ("96161", "96160", "96162", "95728")},
            "mailing": None, "household": {"adults": 2, "adult_coat_sizes": []},
            "children": [{"first_name": n, "age": a, "sex": s, "coat": False} for n, a, s in kids],
            "programs": {"food": True, "toys": True, "coats": False}, "referral": "", "notes": ""}


def insert(con, app_id, p, status="new"):
    con.execute("INSERT INTO applications(id,season,source,submitted_at,lang,status,payload,norm) VALUES(?,?,?,?,?,?,?,?)",
                (app_id, p["season"], "web", f"{p['season']}-10-20T00:00:00Z", "es", status, json.dumps(p), json.dumps(normalize_payload(p))))


def test_normalize():
    assert N.phone("(530) 555-0100") == "5305550100"
    assert N.address_key("10315 Hirschdale Road Apt 4") == "10315 hirschdale"
    assert N.address_key("P.O. Box 2955") == "pobox 2955"
    assert N.first_name_key("Lupita") == "guadalupe"
    kids = N.parse_children_freetext("Name: Sofia, Gender: Girl, Age: 7; Name: Mateo, Gender: Boy, Age: 4")
    assert [(k["first_name"], k["age"], k["sex"]) for k in kids] == [("Sofia", 7, "girl"), ("Mateo", 4, "boy")]


def test_two_adults_same_kids_is_certain_duplicate(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    a = payload("Maria", "Garcia", "5305550100", "10000 Donner Pass Rd", "96161", [("Sofia", 7, "girl"), ("Mateo", 4, "boy")])
    b = payload("Jose", "Garcia", "5305550199", "10000 Donner Pass Road", "96161", [("Sofía", 7, "girl"), ("Mateo", 4, "boy")], other_adult="Maria Garcia")
    insert(con, "A1", a); insert(con, "B1", b)
    stats = run_matching(con, "2026", use_judge=False)
    assert stats["pairs"] == 1
    t = con.execute("SELECT * FROM tasks WHERE kind='resolve_duplicate'").fetchone()
    assert t is not None, stats


def test_returning_family_links_with_age_drift(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    prior = payload("Lupe", "Hernandez", "5305550123", "12345 Pine St", "96161", [("Ana", 5, "girl"), ("Luis", 9, "boy")], season="2025")
    cur = payload("Guadalupe", "Hernández", "5305550123", "12345 Pine Street #2", "96161", [("Ana", 6, "girl"), ("Luis", 10, "boy")], season="2026")
    insert(con, "P1", prior, status="accepted"); insert(con, "C1", cur)
    s, reasons = score_pair(*[load_apps(con, "id=?", (i,))[0] for i in ("P1", "C1")])
    assert s >= 0.9, (s, reasons)
    run_matching(con, "2026", use_judge=False)
    app = con.execute("SELECT family_id, status FROM applications WHERE id='C1'").fetchone()
    assert app["family_id"] is not None and app["status"] == "matched"
    fam = con.execute("SELECT seasons_served FROM families").fetchone()
    assert fam["seasons_served"] == 1  # prior accepted season counts, current not yet


def test_helper_phone_does_not_match_and_out_of_area_tasks(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    helper = {"name": "Sra. Maestra", "phone": "5305550999", "org": "TTUSD"}
    a = payload("Rosa", "Lopez", "5305550999", "1 Elm St", "96161", [("Diego", 3, "boy")], helper=helper)
    b = payload("Elena", "Ruiz", "5305550999", "2 Oak St", "89451", [("Sara", 12, "girl")], helper=helper)
    insert(con, "A", a); insert(con, "B", b)
    stats = run_matching(con, "2026", use_judge=False)
    assert stats["pairs"] == 0
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE kind='verify_address' AND app_id='B'").fetchone()[0] == 1


def test_gray_zone_goes_to_human_when_judge_off(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    a = payload("Maria", "Garcia", "5305550100", "10000 Donner Pass Rd", "96161", [("Sofia", 7, "girl")])
    b = payload("Maria", "Garcia", "5305550177", "500 Other St", "96161", [("Sofia", 8, "girl"), ("Nuevo", 1, "boy")])
    insert(con, "A", a); insert(con, "B", b)
    run_matching(con, "2026", use_judge=True)  # TCC_JUDGE=none returns unsure
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE kind IN ('review_match','contact_applicant')").fetchone()[0] == 1


def gray(first, last, phone, street, kids, season):
    """A pair of these scores in the gray zone (same name, last name and zip, one child, different phone and street)."""
    return payload(first, last, phone, street, "96161", kids, season=season)


def test_rejudge_shares_apps_across_pairs_and_links_all_seasons(tmp_path, monkeypatch):
    from review import match
    con = connect(tmp_path / "t.sqlite")
    insert(con, "A", gray("Maria", "Garcia", "5305550100", "10000 Donner Pass Rd", [("Sofia", 7, "girl")], "2026"))
    insert(con, "B1", gray("Maria", "Garcia", "5305550101", "500 Other St", [("Sofia", 6, "girl")], "2025"), status="accepted")
    insert(con, "B2", gray("Maria", "Garcia", "5305550102", "700 Third St", [("Sofia", 5, "girl")], "2024"), status="accepted")
    stats = run_matching(con, "2026", use_judge=True)  # TCC_JUDGE=none: judge returns None
    assert stats["pairs"] == 2 and stats["llm"] == 0 and stats["errors"] == 0
    assert con.execute("SELECT COUNT(*) FROM candidates WHERE verdict='unsure' AND decided_by='rule' AND judge IS NULL").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE kind='review_match' AND status='open'").fetchone()[0] == 2
    same = {"verdict": "same", "confidence": 0.9, "reason": "same mother and child", "suggested_action": "link", "question_for_applicant": ""}
    monkeypatch.setattr(match, "judge_pair", lambda A, B, reasons: dict(same))
    stats = match.rejudge(con, "2026", workers=2)
    assert stats["same"] == 2 and stats["errors"] == 0
    fams = con.execute("SELECT * FROM families").fetchall()
    assert len(fams) == 1 and fams[0]["seasons_served"] == 2 and fams[0]["first_season"] == "2024" and fams[0]["last_season"] == "2026"
    assert {r[0] for r in con.execute("SELECT family_id FROM applications")} == {fams[0]["id"]}
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE status='open'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM candidates WHERE decided_by='llm' AND verdict='same'").fetchone()[0] == 2


def test_judge_error_falls_back_to_rule_and_rejudge_picks_it_up(tmp_path, monkeypatch):
    from review import match
    con = connect(tmp_path / "t.sqlite")
    insert(con, "A", gray("Maria", "Garcia", "5305550100", "10000 Donner Pass Rd", [("Sofia", 7, "girl")], "2026"))
    insert(con, "B", gray("Maria", "Garcia", "5305550101", "500 Other St", [("Sofia", 6, "girl")], "2025"), status="accepted")

    def boom(A, B, reasons):
        raise RuntimeError("no api key")
    monkeypatch.setattr(match, "judge_pair", boom)
    stats = run_matching(con, "2026", use_judge=True)
    assert stats["pairs"] == 1 and stats["errors"] == 1 and stats["llm"] == 0
    c = con.execute("SELECT * FROM candidates").fetchone()
    assert c["verdict"] == "unsure" and c["decided_by"] == "rule" and "judge error" in json.loads(c["judge"])["reason"]
    assert con.execute("SELECT status FROM applications WHERE id='A'").fetchone()[0] == "matched"
    t = con.execute("SELECT * FROM tasks WHERE candidate_id=?", (c["id"],)).fetchone()
    assert t["status"] == "open" and "judge error" in t["detail"]
    # still broken: rejudge leaves the candidate untouched
    assert match.rejudge(con, "2026", workers=1)["errors"] == 1
    assert dict(con.execute("SELECT verdict, decided_by FROM candidates").fetchone()) == {"verdict": "unsure", "decided_by": "rule"}
    # fixed: rejudge decides it
    monkeypatch.setattr(match, "judge_pair", lambda A, B, r: {"verdict": "same", "confidence": 0.9, "reason": "ok", "suggested_action": "link", "question_for_applicant": ""})
    stats = match.rejudge(con, "2026", workers=1)
    assert stats["same"] == 1
    assert con.execute("SELECT family_id FROM applications WHERE id='A'").fetchone()[0] is not None
    assert con.execute("SELECT status FROM tasks WHERE id=?", (t["id"],)).fetchone()[0] == "dismissed"


def test_preview_apps_never_get_a_family(tmp_path):
    from review.match import link_family, resolve_candidate
    con = connect(tmp_path / "t.sqlite")
    prior = payload("Lupe", "Hernandez", "5305550123", "12345 Pine St", "96161", [("Ana", 5, "girl"), ("Luis", 9, "boy")], season="2025")
    pv = payload("Guadalupe", "Hernández", "5305550123", "12345 Pine Street #2", "96161", [("Ana", 6, "girl"), ("Luis", 10, "boy")], season="preview")
    insert(con, "P1", prior, status="accepted"); insert(con, "PV1", pv)
    stats = run_matching(con, "preview", use_judge=False)
    assert stats["rule"] == 1  # a certain match by rule
    assert [r[0] for r in con.execute("SELECT family_id FROM applications")] == [None, None]
    assert con.execute("SELECT COUNT(*) FROM families").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE kind='review_match' AND title LIKE 'Preview match%'").fetchone()[0] == 1
    cid = con.execute("SELECT id FROM candidates").fetchone()[0]
    resolve_candidate(con, cid, "same")  # a human saying "same" still does not link a preview row
    A, B = load_apps(con, "id='P1'")[0], load_apps(con, "id='PV1'")[0]
    assert link_family(con, A, B) is None
    assert [r[0] for r in con.execute("SELECT family_id FROM applications")] == [None, None]


def test_link_family_merges_two_existing_families(tmp_path):
    from review.match import link_family
    con = connect(tmp_path / "t.sqlite")
    for i, season in (("A", "2026"), ("B1", "2025"), ("C", "2026"), ("B2", "2024")):
        insert(con, i, payload("Maria", "Garcia", "5305550100", "1 Elm St", "96161", [("Sofia", 7, "girl")], season=season), status="accepted" if season != "2026" else "new")
    a = lambda i: load_apps(con, "id=?", (i,))[0]  # noqa: E731
    f1 = link_family(con, a("A"), a("B1"))
    f2 = link_family(con, a("C"), a("B2"))
    assert f1 != f2 and con.execute("SELECT COUNT(*) FROM families").fetchone()[0] == 2
    A, B2 = a("A"), a("B2")
    A["family_id"] = None  # a stale in-memory copy: link_family must trust the database, not the dict
    fid = link_family(con, A, B2)
    assert fid == f1  # the older family survives
    assert con.execute("SELECT COUNT(*) FROM families").fetchone()[0] == 1
    assert {r[0] for r in con.execute("SELECT family_id FROM applications")} == {f1}
    fam = con.execute("SELECT * FROM families").fetchone()
    assert fam["seasons_served"] == 2 and fam["first_season"] == "2024" and fam["last_season"] == "2026"
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='merge_family'").fetchone()[0] == 1


def test_purge_local_removes_the_season_and_its_dependents(tmp_path):
    from review.cli import purge_local
    from review.db import set_meta
    con = connect(tmp_path / "t.sqlite")
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    prior = payload("Lupe", "Hernandez", "5305550123", "12345 Pine St", "96161", [("Ana", 5, "girl"), ("Luis", 9, "boy")], season="2025")
    cur = payload("Guadalupe", "Hernández", "5305550123", "12345 Pine Street #2", "96161", [("Ana", 6, "girl"), ("Luis", 10, "boy")], season="2026")
    dup = payload("Jose", "Hernandez", "5305550124", "12345 Pine St", "96161", [("Ana", 6, "girl"), ("Luis", 10, "boy")], season="2026", other_adult="Guadalupe Hernandez")
    insert(con, "P1", prior, status="accepted"); insert(con, "C1", cur); insert(con, "C2", dup)
    run_matching(con, "2026", use_judge=False)
    set_meta(con, "since:2026", "2026-10-20T00:00:00Z"); set_meta(con, "since:2025", "x"); con.commit()
    assert con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] >= 2
    assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] >= 1
    assert con.execute("SELECT COUNT(*) FROM families").fetchone()[0] == 1
    counts = purge_local(con, "2026")
    assert counts["applications"] == 2 and counts["candidates"] >= 2 and counts["tasks"] >= 1
    assert [r[0] for r in con.execute("SELECT id FROM applications")] == ["P1"]
    assert con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM families").fetchone()[0] == 1  # P1 still belongs to it
    assert con.execute("SELECT seasons_served, last_season FROM families").fetchone()[:] == (1, "2025")
    assert con.execute("SELECT value FROM meta WHERE key='since:2026'").fetchone() is None
    assert con.execute("SELECT value FROM meta WHERE key='since:2025'").fetchone()[0] == "x"
    assert con.execute("SELECT COUNT(*) FROM events WHERE ref IN ('C1','C2')").fetchone()[0] == 0
    ev = con.execute("SELECT detail FROM events WHERE kind='purge_local'").fetchone()[0]
    assert json.loads(ev)["applications"] == 2 and "Hern" not in ev
    counts = purge_local(con, "2025")
    assert counts["applications"] == 1 and counts["families"] == 1
    assert con.execute("SELECT COUNT(*) FROM families").fetchone()[0] == 0


def test_import_without_submission_id_is_stable(tmp_path):
    import openpyxl
    from review.importer import import_xlsx
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Submission Date", "Head of Household", "Mobile", "Local Address", "Names and ages of children needing gifts"])
    ws.append(["2025-11-01", "Rosa Prueba", "5305550100", "1 Elm St Truckee 96161", "Name: Diego, Age: 3"])
    ws.append(["2025-11-02", "Ana Prueba", "5305550101", "2 Oak St Truckee 96161", "Name: Luis, Age: 5"])
    path = tmp_path / "x.xlsx"; wb.save(path)
    con = connect(tmp_path / "t.sqlite")
    assert import_xlsx(path, "2025", "accepted", con) == (2, 0)
    ids = sorted(r[0] for r in con.execute("SELECT id FROM applications"))
    assert all(len(i) == len("JF-2025-") + 12 for i in ids)
    assert import_xlsx(path, "2025", "accepted", con) == (0, 2)
    con2 = connect(tmp_path / "t2.sqlite")
    import_xlsx(path, "2025", "accepted", con2)
    assert sorted(r[0] for r in con2.execute("SELECT id FROM applications")) == ids  # same ids in a fresh database
