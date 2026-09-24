"""Seed the preview season with applications that exercise every branch of the workflow.

  bin/seed-preview            derive returning families from imported prior-season rows
                              plus synthetic duplicate and out-of-area cases

Rows are encrypted to the season recipients exactly as the form does it and POSTed to
the Worker's preview season (TCC-PV-… codes). Nothing here prints applicant details.
"""
import base64
import json
import random
import sys
import urllib.request

import pyrage

from review.db import connect
from review.match import load_apps
from review.sync import load_config

NICK = {"guadalupe": "Lupe", "jose": "Pepe", "francisco": "Paco", "alejandro": "Alex", "jesus": "Chuy", "maria": "Mari", "antonio": "Toño"}


def armor(raw):
    b = base64.b64encode(raw).decode()
    return "-----BEGIN AGE ENCRYPTED FILE-----\n" + "\n".join(b[i:i + 64] for i in range(0, len(b), 64)) + "\n-----END AGE ENCRYPTED FILE-----\n"


def submit(cfg, payload, lang="es"):
    recips = [pyrage.x25519.Recipient.from_str(r) for r in cfg["recipients"]]
    ct = armor(pyrage.encrypt(json.dumps(payload, ensure_ascii=False).encode(), recips))
    req = urllib.request.Request(cfg["api_base"] + "/api/apply", method="POST", data=json.dumps({"season": "preview", "lang": lang, "ciphertext": ct}).encode(),
                                 headers={"content-type": "application/json", "user-agent": "truckee-cares-seed/1", "origin": "https://micrui.github.io"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["id"]


def base_payload(first, last, phone, street, city, zip_, kids, adults=2, other_adult="", helper=None, email=""):
    return {"version": 1, "season": "preview", "lang": "es", "submitted_at": "", "helper": helper,
            "applicant": {"first_name": first, "last_name": last, "phone": phone, "can_text": True, "other_phone": "", "email": email, "other_adult": other_adult, "contact_lang": "es"},
            "address": {"street": street, "unit": "", "city": city, "zip": zip_, "in_area": zip_ in ("96160", "96161", "96162", "95728")},
            "mailing": None, "household": {"adults": adults, "adult_coat_sizes": ["M"]},
            "children": [{"first_name": n, "age": a, "sex": s, "school": "", "coat": i == 0, "coat_size": ""} for i, (n, a, s) in enumerate(kids)],
            "programs": {"food": True, "toys": bool(kids), "coats": True}, "referral": "", "notes": ""}


def from_prior(app, drift=1, nickname=False, second_adult=False, new_baby=False, moved=False, new_phone=False):
    """A next-season application for a family that applied before."""
    p = app["payload"]; a = p["applicant"]; ad = p["address"]
    first = a["first_name"]; last = a["last_name"]
    if nickname:
        first = NICK.get(first.lower(), first)
    kids = [(c["first_name"], (c["age"] or 0) + drift, c.get("sex") or random.choice(["boy", "girl"])) for c in p["children"] if c.get("first_name")]
    if new_baby:
        kids.append((random.choice(["Mateo", "Sofía", "Emma", "Santiago"]), 0, random.choice(["boy", "girl"])))
    street = ad["street"] if not moved else f"{random.randint(10000, 12999)} {random.choice(['Alder Dr', 'Donner Pass Rd', 'Glenshire Dr'])}"
    phone = a["phone"] if not new_phone else "530555" + f"{random.randint(1000, 9999)}"
    other = ""
    if second_adult:
        # The other parent applies for the same children from their own phone.
        other = f"{first} {last}".strip()
        first = random.choice(["Jose", "Carlos", "Luis", "Miguel", "Ana", "Rosa"])
        phone = "530555" + f"{random.randint(1000, 9999)}"
    return base_payload(first, last, phone or "5305550000", street, "Truckee", "96161", kids, other_adult=other)


def main():
    random.seed(7)
    cfg = load_config()
    con = connect()
    prior = [a for a in load_apps(con, "season != 'preview' AND status='accepted'") if a["norm"]["n_children"] >= 2 and a["norm"]["addr_key"] and a["norm"]["phones"]]
    if len(prior) < 4:
        sys.exit("import a prior season first (bin/review import <xlsx> --season 2025)")
    picks = random.sample(prior, 4)
    only = sys.argv[1:]  # optional case-number filter, e.g. "9 10"
    cases = [
        ("returning, same details, kids one year older", from_prior(picks[0])),
        ("returning, nickname, moved, new baby", from_prior(picks[1], nickname=True, moved=True, new_baby=True)),
        ("returning family, first adult", from_prior(picks[2])),
        ("same family, second adult applies for the same kids", from_prior(picks[2], second_adult=True)),
        ("returning but ages typed wrong (drift 3)", from_prior(picks[3], drift=3)),
        ("out of area (Kings Beach)", base_payload("Elena", "Prueba Norte", "5305551212", "8300 N Lake Blvd", "Kings Beach", "96143", [("Nico", 6, "boy")])),
        ("volunteer files for two families from one phone (1/2)", base_payload("Rosa", "Prueba Uno", "5305550999", "10100 Pine Ave", "Truckee", "96161", [("Diego", 3, "boy")], helper={"name": "Sra. Ayudante", "phone": "5305550999", "org": "TTUSD"})),
        ("volunteer files for two families from one phone (2/2)", base_payload("Carmen", "Prueba Dos", "5305550999", "10200 Oak St", "Truckee", "96161", [("Valeria", 9, "girl"), ("Iker", 12, "boy")], helper={"name": "Sra. Ayudante", "phone": "5305550999", "org": "TTUSD"})),
        ("gray zone: nickname, moved, new phone; only the children carry over", from_prior(random.choice(prior), nickname=True, moved=True, new_phone=True)),
        ("gray zone: same last name and zip as a prior family, different children", base_payload("Martín", picks[0]["payload"]["applicant"]["last_name"], "5305553434", "11000 Alder Dr", "Truckee", picks[0]["norm"]["zip"] or "96161", [("Ximena", 4, "girl")])),
    ]
    for i, (label, payload) in enumerate(cases, 1):
        if only and str(i) not in only:
            continue
        print(f"{submit(cfg, payload):16} {i:>2}. {label}")


if __name__ == "__main__":
    main()
