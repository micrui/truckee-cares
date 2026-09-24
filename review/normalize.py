"""Normalization helpers. Ported from the 2025 one-off dedup script and kept
deterministic so the matcher's candidate generation is explainable."""
import re
import unicodedata

NICKNAMES = {
    "alex": "alejandro", "ale": "alejandro", "beto": "alberto", "chuy": "jesus", "nacho": "ignacio",
    "pepe": "jose", "paco": "francisco", "pancho": "francisco", "lupe": "guadalupe", "lupita": "guadalupe",
    "mari": "maria", "toni": "antonio", "tono": "antonio", "memo": "guillermo", "lalo": "eduardo",
    "chava": "salvador", "nico": "nicolas", "manu": "manuel", "fer": "fernando", "fernanda": "fernanda",
    "mike": "michael", "bill": "william", "bob": "robert", "liz": "elizabeth", "kate": "katherine",
    "katie": "katherine", "jenny": "jennifer", "jen": "jennifer", "chris": "christopher", "tony": "anthony",
}
STREET_ABBR = [
    (" street", " st"), (" avenue", " ave"), (" road", " rd"), (" drive", " dr"), (" boulevard", " blvd"),
    (" place", " pl"), (" lane", " ln"), (" trail", " trl"), (" court", " ct"), (" circle", " cir"),
    (" way", " way"), (" apartment", " apt"), (" unit", " apt"), (" suite", " apt"), (" number", " "), (" no ", " "),
    ("p.o. box", "pobox"), ("po box", "pobox"), ("p o box", "pobox"), ("apartado", "pobox"),
]


def clean(text):
    text = "" if text is None else str(text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.lower().split())


def phone(value):
    d = re.sub(r"\D", "", str(value or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def name(value):
    t = re.sub(r"[^a-z ]", " ", clean(value))
    return " ".join(t.split())


def first_name_key(value):
    """First token of a first name, nickname-expanded."""
    n = name(value)
    if not n:
        return ""
    first = n.split()[0]
    return NICKNAMES.get(first, first)


def address(value):
    t = clean(value)
    for old, new in STREET_ABBR:
        t = t.replace(old, new)
    t = re.sub(r"#", " apt ", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return " ".join(t.split())


def address_key(value):
    """House number + first street token. Survives apt formatting and typos in the street suffix."""
    a = address(value)
    m = re.match(r"(\d+[a-z]?)\s+([a-z0-9]+)", a)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    m = re.search(r"pobox\s*(\d+)", a)
    if m:
        return f"pobox {m.group(1)}"
    return ""


def zipcode(value):
    d = re.sub(r"\D", "", str(value or ""))
    return d[:5] if len(d) >= 5 else ""


def child_key(first, age):
    return f"{first_name_key(first)}:{int(age) if str(age).strip().isdigit() else '?'}"


def children_signature(children):
    """Sorted set of first-name keys, ages left out. Used for blocking only."""
    return sorted({first_name_key(c.get("first_name", "")) for c in children if c.get("first_name")})


def parse_children_freetext(raw):
    """Parse the JotForm free-text children field into records. Best effort;
    the LLM judge reads the raw text too, so this only needs to be decent."""
    if not raw or not str(raw).strip():
        return []
    text = str(raw).replace("\r", "\n")
    out = []
    for seg in re.split(r"[;\n]+|(?<=\d)\s*,\s*(?=[A-Za-z])", text):
        seg = seg.strip(" -,")
        if not seg:
            continue
        nm = re.search(r"(?:name|nombre)[^:]*:\s*([^,;]+)", seg, re.I)
        ag = re.search(r"(?:age|edad)[^:]*:\s*(\d{1,2})", seg, re.I) or re.search(r"\b(\d{1,2})\s*(?:yrs?|years?|años|anos|y\.?o\.?)?\b", seg)
        sx = re.search(r"(?:gender|sex|sexo|genero|género)[^:]*:\s*([^,;]+)", seg, re.I)
        n = nm.group(1).strip() if nm else re.sub(r"\d.*", "", seg).strip(" -:,")
        if not n and not ag:
            continue
        sex = ""
        sxt = (sx.group(1) if sx else seg).lower()
        if re.search(r"\b(girl|female|nina|niña|f|mujer)\b", sxt): sex = "girl"
        elif re.search(r"\b(boy|male|nino|niño|m|hombre)\b", sxt): sex = "boy"
        out.append({"first_name": n[:40], "age": int(ag.group(1)) if ag else None, "sex": sex})
    return out
