import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "site"))
import build  # noqa: E402


def test_build_produces_both_languages(tmp_path):
    out = build.build("/truckee-cares", tmp_path / "dist")
    for lang in ("en", "es"):
        assert (out / lang / "index.html").exists()
        assert (out / lang / "apply" / "index.html").exists()
        assert (out / lang / "privacy" / "index.html").exists()
    cfg = json.loads((out / "config.json").read_text())
    assert cfg["base_path"] == "/truckee-cares"
    assert cfg["recipients"] and cfg["recipients"][0].startswith("age1")
    assert "cname" not in cfg and "ADMIN" not in json.dumps(cfg)
    html = (out / "es" / "apply" / "index.html").read_text()
    assert 'lang="es"' in html and "/truckee-cares/static/js/apply.js" in html
    assert (out / "index.html").exists() and (out / ".nojekyll").exists()


def test_no_third_party_scripts(tmp_path):
    out = build.build("", tmp_path / "dist")
    for f in out.rglob("*.html"):
        t = f.read_text()
        assert "googletagmanager" not in t and "facebook.net" not in t and "cdn." not in t, f


def test_404_guards_against_redirect_loop(tmp_path):
    out = build.build("/truckee-cares", tmp_path / "dist")
    not_found = (out / "404.html").read_text()
    index = (out / "index.html").read_text()
    guard = r"/^\/(en|es)\//.test(path)"
    # An unknown path already under /en/ or /es/ must stay on the 404 page.
    assert guard in not_found
    assert "Esa página no existe" in not_found
    assert 'id="apply-link"' in not_found
    # The root page is a pure redirect: no guard, no alias map, no 404 text.
    assert guard not in index
    assert "applicants" not in index
    assert "no existe" not in index


def test_404_maps_old_wix_slugs(tmp_path):
    out = build.build("", tmp_path / "dist")
    not_found = (out / "404.html").read_text()
    assert '"applicants": "apply/"' in not_found
    assert '"our-team": "about/"' in not_found
    assert '"photos": ""' in not_found


def test_home_photos_carry_dimensions(tmp_path):
    out = build.build("", tmp_path / "dist")
    html = (out / "en" / "index.html").read_text()
    assert 'photos/tcc-1.jpg" alt="" loading="lazy" width="' in html
    for name in build.HOME_PHOTOS:
        size = build.jpeg_size(build.SITE / "static" / "img" / "photos" / name)
        assert size and 0 < size[0] <= 900, (name, size)
