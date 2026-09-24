#!/usr/bin/env python3
"""Build the static site into dist/.

Inputs: config/season.json, site/content/*.yaml, site/templates/*.html, site/static/.
Output: dist/ with /en/... and /es/... trees, /admin/, config.json, and a root redirect.

Run: bin/build   (or: .venv/bin/python site/build.py [--base /truckee-cares] [--out dist])
"""
import argparse
import json
import os
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

# Keys of season.json that the browser is allowed to see.
CLIENT_CONFIG_KEYS = [
    "season", "timezone", "opens", "closes", "mode", "announcement",
    "help_phone", "help_email", "service_area", "api_base", "recipients",
]


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


def build(base_path="", out=ROOT / "dist"):
    config = json.load(open(ROOT / "config" / "season.json"))
    base_path = base_path.rstrip("/")
    strings = load_yaml(SITE / "content" / "strings.yaml")
    schools = load_yaml(SITE / "content" / "schools.yaml")

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
                s=s, page=content, config=config, client_config=client_config,
                this_path=f"{base_path}/{lang}/{path}",
                other_path=f"{base_path}/{other}/{path}",
                announcement=(config.get("announcement") or {}).get(lang, ""),
            )

    render("redirect.html", "index.html", base=base_path)
    render("redirect.html", "404.html", base=base_path)
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
