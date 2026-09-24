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
