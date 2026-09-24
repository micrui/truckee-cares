"""Ask Claude whether two applications describe the same family.

Only the fields a volunteer would compare are sent: names, other adult,
children (first name, age, sex), street address, mailing address, season, and
the raw children text for imports. No phone numbers, emails, or notes.
Set TCC_JUDGE=none to skip the model and route every gray-zone pair to a person.
"""
import json
import os

MODEL = os.environ.get("TCC_JUDGE_MODEL", "claude-opus-5")

SYSTEM = """You help volunteers at a small holiday-assistance nonprofit in Truckee, California decide whether two applications were filed by the same family. Applicants are often Spanish-speaking; names get spelled several ways, nicknames are common (Lupe/Guadalupe, Chuy/Jesus), and two adults in one home sometimes both apply. Between seasons children get about one year older and families sometimes move. A volunteer or teacher may file for several families from one phone, so a shared phone alone means little.

Answer like a careful, kind volunteer would. "same" means you would confidently merge these into one family record. "different" means you are confident they are two families. "unsure" means a person should look, or should ask the applicant. Give a one-sentence reason a volunteer can read."""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["same", "different", "unsure"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "suggested_action": {"type": "string", "enum": ["link", "ignore", "ask_human", "contact_applicant"]},
        "question_for_applicant": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reason", "suggested_action", "question_for_applicant"],
    "additionalProperties": False,
}


def view(app):
    p = app["payload"]
    a = p.get("applicant", {})
    addr = p.get("address", {}) or {}
    mail = p.get("mailing") or {}
    kids = [f"{c.get('first_name','?')} ({c.get('age','?')}, {c.get('sex','?')})" for c in p.get("children", [])]
    return {
        "season": app["season"],
        "head_of_household": f"{a.get('first_name','')} {a.get('last_name','')}".strip(),
        "other_adult": a.get("other_adult", ""),
        "helper_filed_it": bool(p.get("helper")),
        "children": kids,
        "children_raw_text": p.get("children_raw", ""),
        "street": f"{addr.get('street','')} {addr.get('unit','')}".strip(),
        "city_zip": f"{addr.get('city','')} {addr.get('zip','')}".strip(),
        "mailing": mail.get("street", "") if mail else "same as street",
    }


def judge_pair(app_a, app_b, reasons):
    if os.environ.get("TCC_JUDGE", "claude") == "none":
        return {"verdict": "unsure", "confidence": 0, "reason": "judge disabled", "suggested_action": "ask_human", "question_for_applicant": ""}
    import anthropic
    client = anthropic.Anthropic()
    user = ("Two applications. Rule-based hints: " + "; ".join(reasons) + "\n\nA:\n" + json.dumps(view(app_a), ensure_ascii=False, indent=1)
            + "\n\nB:\n" + json.dumps(view(app_b), ensure_ascii=False, indent=1) + "\n\nSame family?")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "refusal":
        return {"verdict": "unsure", "confidence": 0, "reason": "model declined", "suggested_action": "ask_human", "question_for_applicant": ""}
    text = next(b.text for b in resp.content if b.type == "text")
    out = json.loads(text)
    out["model"] = MODEL
    return out
