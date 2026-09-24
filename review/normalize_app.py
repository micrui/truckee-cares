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
