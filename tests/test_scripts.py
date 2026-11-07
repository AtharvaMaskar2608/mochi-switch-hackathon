import subprocess
from pathlib import Path


def test_script_helpers_under_set_e():
    sh = Path(__file__).with_name("test_scripts.sh")
    r = subprocess.run(["bash", str(sh)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().endswith("all ok")


def test_up_pins_switch_to_an_overridable_commit():
    up = (Path(__file__).parent.parent / "scripts" / "up.sh").read_text()
    assert "SWITCH_REF=${SWITCH_REF:-a425936d615a153dc24ee0d309ca515572bfff48}" in up
    assert 'checkout -q "$SWITCH_REF"' in up and "--depth 1" not in up  # a shallow clone can't check out a pin
    assert "SWITCH_DB_PORT" in up and "5432" in up
