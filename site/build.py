#!/usr/bin/env python3
"""Build the static site into dist/.

Inputs: config/season.json, site/content/*.yaml, site/templates/*.html, site/static/.
Output: dist/ with /en/... and /es/... trees, /admin/, config.json, and a root redirect.

Run: bin/build   (or: .venv/bin/python site/build.py [--base /truckee-cares] [--out dist])
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

import markdown
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
LANGS = ["en", "es"]

# Slug -> template. Content for each slug lives in site/content/<slug>.yaml.
PAGES = {
    "index": "home.html",
    "apply": "apply.html",
    "programs": "page.html",
    "donate": "page.html",
    "volunteer": "page.html",
    "about": "page.html",
    "privacy": "page.html",
}

# Photos in the home page strip, in order. Files live in site/static/img/photos/.
HOME_PHOTOS = ["tcc-1.jpg", "tcc-2.jpg", "tcc-3.jpg", "tcc-4.jpg"]

# Keys of season.json that the browser is allowed to see.
CLIENT_CONFIG_KEYS = [
    "season", "timezone", "opens", "closes", "mode", "announcement",
    "help_phone", "help_email", "service_area", "api_base", "recipients",
]

# JPEG "start of frame" markers. Any of them carries the image height and width.
JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def md(text):
    return markdown.markdown(text or "", extensions=["smarty"])


def localize(node, lang):
    """Return node with every {en:, es:} leaf collapsed to the chosen language."""
    if isinstance(node, dict):
        if set(node.keys()) <= set(LANGS) and node:
            return node.get(lang) or node.get("en") or ""
        return {k: localize(v, lang) for k, v in node.items()}
    if isinstance(node, list):
        return [localize(v, lang) for v in node]
    return node


def jpeg_size(path):
    """(width, height) of a JPEG read from its start-of-frame marker, or None.

    Enough for width/height attributes on <img>, so the browser reserves the space
    before the file arrives. No image library needed.
    """
    data = Path(path).read_bytes()
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # markers without a length
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in JPEG_SOF:
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            return width, height
        i += 2 + length
    return None


def home_photos():
    photos = []
    for name in HOME_PHOTOS:
        f = SITE / "static" / "img" / "photos" / name
        size = jpeg_size(f) if f.exists() else None
        photos.append({"file": name, "width": size[0] if size else None, "height": size[1] if size else None})
    return photos


def build(base_path="", out=ROOT / "dist"):
    config = json.load(open(ROOT / "config" / "season.json"))
    if not config.get("recipients"):
        raise SystemExit("config/season.json has no recipients: the form would encrypt to nobody. Run node bin/keygen.mjs first.")
    base_path = base_path.rstrip("/")
    strings = load_yaml(SITE / "content" / "strings.yaml")
    schools = load_yaml(SITE / "content" / "schools.yaml")
    photos = home_photos()

    env = Environment(
        loader=FileSystemLoader(SITE / "templates"),
        undefined=StrictUndefined,
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["md"] = md

    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(SITE / "static", out / "static")

    # Cache busting. GitHub Pages serves static files with a ten-minute cache; a stale
    # apply.js next to a fresh apply-strings.js would break the form. Every script URL,
    # including the module imports inside apply.js, carries a hash of the js directory.
    h = hashlib.sha256()
    for f in sorted((SITE / "static" / "js").glob("*.js")) + sorted((SITE / "static" / "css").glob("*.css")):
        h.update(f.read_bytes())
    asset_v = h.hexdigest()[:10]
    apply_js = out / "static" / "js" / "apply.js"
    apply_js.write_text(re.sub(r'from "\./([a-z-]+\.js)"', rf'from "./\1?v={asset_v}"', apply_js.read_text()))

    client_config = {k: config[k] for k in CLIENT_CONFIG_KEYS if k in config}
    client_config["base_path"] = base_path
    client_config["schools"] = schools.get("schools", [])
    (out / "config.json").write_text(json.dumps(client_config, indent=2, ensure_ascii=False))

    def render(template, dest, **ctx):
        dest = out / dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(env.get_template(template).render(**ctx), encoding="utf-8")

    for lang in LANGS:
        s = localize(strings, lang)
        for slug, template in PAGES.items():
            content = localize(load_yaml(SITE / "content" / f"{slug}.yaml"), lang)
            dest = f"{lang}/index.html" if slug == "index" else f"{lang}/{slug}/index.html"
            other = "es" if lang == "en" else "en"
            path = "" if slug == "index" else f"{slug}/"
            render(
                template, dest,
                lang=lang, other_lang=other, slug=slug, base=base_path,
                s=s, page=content, config=config, client_config=client_config, asset_v=asset_v,
                photos=photos,
                this_path=f"{base_path}/{lang}/{path}",
                other_path=f"{base_path}/{other}/{path}",
                announcement=(config.get("announcement") or {}).get(lang, ""),
            )

    # The root is a pure redirect to the visitor's language. The 404 page is the same
    # template plus a guard: an unknown path already under /en/ or /es/ must not redirect
    # again (it would loop), and old Wix addresses are mapped to the new pages.
    render("redirect.html", "index.html", base=base_path, asset_v=asset_v, is_404=False)
    render("redirect.html", "404.html", base=base_path, asset_v=asset_v, is_404=True)
    (out / ".nojekyll").write_text("")
    if config.get("cname"):
        (out / "CNAME").write_text(config["cname"] + "\n")
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default=os.environ.get("BASE_PATH", ""),
                   help="URL prefix when hosted under a subpath, e.g. /truckee-cares")
    p.add_argument("--out", default=str(ROOT / "dist"))
    a = p.parse_args(argv)
    out = build(a.base, a.out)
    n = sum(1 for _ in out.rglob("*.html"))
    print(f"built {n} html files into {out} (base='{a.base}')")


if __name__ == "__main__":
    sys.exit(main())
