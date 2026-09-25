"""Turn an application payload (web schema v1 or an imported JotForm row) into
the normalized keys the matcher blocks and scores on.

The payload comes from a decrypted blob, so nothing about its shape is trusted:
every part is coerced to the type the matcher expects, and a payload that is not
a dict at all is rejected so the caller can log it as bad."""
from . import normalize as N


def as_dict(v):
    return v if isinstance(v, dict) else {}


def as_str(v):
    return "" if v is None else str(v)


def as_int(v):
    """Age as int or None. Accepts 7, "7", 7.0, " 7 "; anything else is unknown."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def child_records(v):
    out = []
    for c in (v if isinstance(v, list) else []):
        if isinstance(c, dict):
            out.append({"first_name": as_str(c.get("first_name")), "age": as_int(c.get("age")), "sex": as_str(c.get("sex"))})
    return out


def _kids(p):
    return child_records(as_dict(p).get("children"))


def _txt(v):
    return as_str(v).strip()


FIELD_VIEWS = [  # what an edit can change, by name, in the order a person would read them
    ("name", lambda p: (_txt(as_dict(p.get("applicant")).get("first_name")), _txt(as_dict(p.get("applicant")).get("last_name")))),
    ("phone", lambda p: N.phone(_txt(as_dict(p.get("applicant")).get("phone")))),
    ("other phone", lambda p: N.phone(_txt(as_dict(p.get("applicant")).get("other_phone")))),
    ("email", lambda p: _txt(as_dict(p.get("applicant")).get("email")).lower()),
    ("other adult", lambda p: _txt(as_dict(p.get("applicant")).get("other_adult"))),
    ("street", lambda p: _txt(as_dict(p.get("address")).get("street"))),
    ("unit", lambda p: _txt(as_dict(p.get("address")).get("unit"))),
    ("city", lambda p: _txt(as_dict(p.get("address")).get("city"))),
    ("zip", lambda p: _txt(as_dict(p.get("address")).get("zip"))),
    ("mailing", lambda p: tuple(_txt(as_dict(p.get("mailing")).get(k)) for k in ("street", "city", "zip"))),
    ("adults", lambda p: _txt(as_dict(p.get("household")).get("adults"))),
    ("adult coat sizes", lambda p: sorted(_txt(x) for x in (as_dict(p.get("household")).get("adult_coat_sizes") or []) if x is not None) if isinstance(as_dict(p.get("household")).get("adult_coat_sizes"), list) else []),
    ("children count", lambda p: len(_kids(p))),
    ("child names", lambda p: [c["first_name"].strip() for c in _kids(p)]),
    ("child ages", lambda p: [c["age"] for c in _kids(p)]),
    ("child sexes", lambda p: [c["sex"].strip() for c in _kids(p)]),
    ("child coats", lambda p: [(bool(c.get("coat")), _txt(c.get("coat_size"))) for c in as_dict(p).get("children", []) if isinstance(c, dict)] if isinstance(as_dict(p).get("children"), list) else []),
    ("programs", lambda p: sorted(k for k, v in as_dict(p.get("programs")).items() if v)),
    ("helper", lambda p: tuple(_txt(as_dict(p.get("helper")).get(k)) for k in ("name", "phone", "org"))),
    ("referral", lambda p: _txt(p.get("referral"))),
    ("notes", lambda p: _txt(p.get("notes"))),
]


def changed_fields(old, new):
    """Names of the fields that differ between two payloads (an application and the edit
    that replaced it). Names only, never values, so the list can go into a task."""
    old, new = as_dict(old), as_dict(new)
    out = []
    for name, view in FIELD_VIEWS:
        try:
            differs = view(old) != view(new)
        except Exception:  # a malformed side counts as changed
            differs = True
        if differs:
            out.append(name)
    return out


def normalize_payload(p):
    if not isinstance(p, dict):
        raise ValueError(f"payload is {type(p).__name__}, not an object")
    a = as_dict(p.get("applicant"))
    addr = as_dict(p.get("address"))
    mail = as_dict(p.get("mailing"))
    helper = as_dict(p.get("helper"))
    kids = child_records(p.get("children"))
    phones = {N.phone(as_str(a.get("phone"))), N.phone(as_str(a.get("other_phone")))} - {""}
    helper_phone = N.phone(as_str(helper.get("phone")))
    phones -= {helper_phone}
    first, last = as_str(a.get("first_name")), as_str(a.get("last_name"))
    in_area = addr.get("in_area", True)
    return {
        "first": N.first_name_key(first),
        "last": N.name(last),
        "full": N.name(f"{first} {last}"),
        "other_adult": N.name(as_str(a.get("other_adult"))),
        "phones": sorted(phones),
        "helper_phone": helper_phone,
        "email": N.clean(as_str(a.get("email"))),
        "addr": N.address(f"{as_str(addr.get('street'))} {as_str(addr.get('unit'))}"),
        "addr_key": N.address_key(as_str(addr.get("street"))),
        "zip": N.zipcode(as_str(addr.get("zip"))),
        "city": N.clean(as_str(addr.get("city"))),
        "mail_key": N.address_key(as_str(mail.get("street"))) if mail else "",
        "children": [{"first": N.first_name_key(c["first_name"]), "age": c["age"], "sex": c["sex"]} for c in kids],
        "child_names": N.children_signature(kids),
        "n_children": len(kids),
        "in_area": bool(in_area) if in_area is not None else True,
    }
