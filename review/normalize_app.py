"""Turn an application payload (web schema v1 or an imported JotForm row) into
the normalized keys the matcher blocks and scores on."""
from . import normalize as N


def normalize_payload(p):
    a = p.get("applicant", {}) or {}
    addr = p.get("address", {}) or {}
    mail = p.get("mailing") or {}
    helper = p.get("helper") or {}
    kids = p.get("children", []) or []
    phones = {N.phone(a.get("phone")), N.phone(a.get("other_phone"))} - {""}
    helper_phone = N.phone(helper.get("phone"))
    phones -= {helper_phone}
    return {
        "first": N.first_name_key(a.get("first_name")),
        "last": N.name(a.get("last_name")),
        "full": N.name(f"{a.get('first_name','')} {a.get('last_name','')}"),
        "other_adult": N.name(a.get("other_adult")),
        "phones": sorted(phones),
        "helper_phone": helper_phone,
        "email": N.clean(a.get("email")),
        "addr": N.address(f"{addr.get('street','')} {addr.get('unit','')}"),
        "addr_key": N.address_key(addr.get("street")),
        "zip": N.zipcode(addr.get("zip")),
        "city": N.clean(addr.get("city")),
        "mail_key": N.address_key(mail.get("street")) if mail else "",
        "children": [{"first": N.first_name_key(c.get("first_name")), "age": c.get("age"), "sex": c.get("sex", "")} for c in kids],
        "child_names": N.children_signature(kids),
        "n_children": len(kids),
        "in_area": bool(addr.get("in_area", True)),
    }
