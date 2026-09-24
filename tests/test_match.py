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
