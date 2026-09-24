"""The browser bundle (typage) encrypts; the review tool (pyrage) decrypts."""
import json
import subprocess
import sys
from pathlib import Path

import pyrage
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from review.sync import dearmor  # noqa: E402

NODE_SCRIPT = """
import { Encrypter, armor } from "%s";
const [recipient, text] = process.argv.slice(2);
const e = new Encrypter(); e.addRecipient(recipient);
process.stdout.write(armor.encode(await e.encrypt(text)));
""" % (ROOT / "site" / "static" / "js" / "age.js").as_posix()


@pytest.mark.skipif(subprocess.run(["which", "node"], capture_output=True).returncode != 0, reason="node not installed")
def test_browser_encrypt_python_decrypt(tmp_path):
    ident = pyrage.x25519.Identity.generate()
    recipient = str(ident.to_public())
    payload = json.dumps({"applicant": {"first_name": "Rosa"}, "children": [{"first_name": "Diego", "age": 3}]})
    script = tmp_path / "enc.mjs"; script.write_text(NODE_SCRIPT)
    ct = subprocess.run(["node", str(script), recipient, payload], capture_output=True, text=True, check=True).stdout
    assert ct.startswith("-----BEGIN AGE ENCRYPTED FILE-----")
    plain = pyrage.decrypt(dearmor(ct), [ident]).decode()
    assert json.loads(plain)["children"][0]["first_name"] == "Diego"
    with pytest.raises(pyrage.DecryptError):
        pyrage.decrypt(dearmor(ct), [pyrage.x25519.Identity.generate()])
