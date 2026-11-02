"""Check USB permissions and identities without enumerating host devices."""

import re
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "config/udev"
EXPECTED = {
    "camera": [("video4linux", "f326", "1306", "HU12321178")],
    "collector": [("tty", "0403", "6001", "BG02Q5TN")],
    "furnace": [("tty", "0403", "6015", "DU0DT9G8"), ("tty", "0403", "6001", "ABBCE4JW")],
    "microscope": [
        ("tty", "2341", "0043", "035353433353512070D1"),
        ("usb", "0547", "1375", None),
        ("usb", "0547", "1376", None),
    ],
    "stage": [("tty", "2341", "0043", "1453230313235111D1E0")],
    # The P1 serial string carries trailing non-printing bytes, so the rule uses a prefix.
    "thermal": [("usb", "3474", "45c2", "P1016021DG040100208*")],
}
# The sysfs serial value read from the P1 on Olares (H-03, 2026-11-02).
P1_SYSFS_SERIAL = "P1016021DG040100208\x01 \x1e\x01"


def test_inventory_has_one_template_per_device_service():
    assert {path.name for path in TEMPLATES.glob("*.rules.in")} == {
        f"70-mg400-{name}.rules.in" for name in EXPECTED
    }


@pytest.mark.parametrize("service", EXPECTED)
def test_device_rules_keep_inventory_identities_and_restricted_permissions(service):
    text = (TEMPLATES / f"70-mg400-{service}.rules.in").read_text()
    rules = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert len(rules) == len(EXPECTED[service])
    for rule, (subsystem, vendor, product, serial) in zip(rules, EXPECTED[service], strict=True):
        attribute = "ATTR" if subsystem == "usb" else "ATTRS"
        selectors = [
            f'SUBSYSTEM=="{subsystem}"',
            f'{attribute}{{idVendor}}=="{vendor}"',
            f'{attribute}{{idProduct}}=="{product}"',
        ]
        if subsystem == "usb":
            selectors.append('ENV{DEVTYPE}=="usb_device"')
        if serial is not None:
            selectors.append(f'{attribute}{{serial}}=="{serial}"')
        expected = {*selectors, 'MODE="0660"', 'GROUP="@SERVICE_GROUP@"'}
        assert set(rule.split(", ")) == expected
        assert re.search(r'ATTRS?\{idVendor\}=="[0-9a-f]{4}"', rule)
        assert re.search(r'ATTRS?\{idProduct\}=="[0-9a-f]{4}"', rule)
        # No device opens, reset helpers, broad aliases, or world access in udev.
        assert "RUN" not in rule and "PROGRAM" not in rule and "SYMLINK" not in rule


def test_thermal_rule_matches_the_p1_serial_with_its_trailing_bytes():
    # udev matches ATTR values as shell-style patterns and strips only trailing whitespace.
    text = (TEMPLATES / "70-mg400-thermal.rules.in").read_text()
    (pattern,) = re.findall(r'ATTR\{serial\}=="([^"]*)"', text)
    assert fnmatchcase(P1_SYSFS_SERIAL.rstrip(" \t\n\r"), pattern)
    assert fnmatchcase("P1016021DG040100208", pattern)
    assert not fnmatchcase("P1016021DG040100209", pattern)
    assert not fnmatchcase("XP1016021DG040100208", pattern)
    # An exact match on the visible serial would never grant access to this camera.
    assert not fnmatchcase(P1_SYSFS_SERIAL, "P1016021DG040100208")
