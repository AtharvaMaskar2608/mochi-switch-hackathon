import subprocess
from pathlib import Path


def test_script_helpers_under_set_e():
    sh = Path(__file__).with_name("test_scripts.sh")
    r = subprocess.run(["bash", str(sh)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().endswith("all ok")
