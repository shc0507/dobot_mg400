# /// script
# requires-python = ">=3.11"
# dependencies = ["fonttools[woff]>=4.50"]
# ///
"""Build the Chinese console font: IBM Plex Sans SC, cut to the characters that the code uses.

The console loads no remote assets, so it serves its own Chinese face. The output covers only
the CJK characters in `src/orchestrator/web/`, plus CJK punctuation and full-width forms.
A character outside the subset falls back to the system face.
Run this again when you add Chinese strings:

    uv run scripts/console_font.py <plex-sans-sc package dir>

The package is the npm tarball of `@ibm/plex-sans-sc` (version 1.1.0 or later), unpacked.
The SIL Open Font License reserves the name "Plex", so the subset is renamed WuLab Sans SC.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src/orchestrator/web/static/console"
FAMILY = "WuLab Sans SC"
WEIGHTS = {400: "Regular", 600: "SemiBold", 700: "Bold"}
# The same ranges as the unicode-range of the @font-face rules in shell.css.
CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")
ALWAYS = "".join(chr(c) for c in range(0x3000, 0x3040)) + "".join(
    chr(c) for c in range(0xFF01, 0xFF5F)
)


def used_characters(root: Path = ROOT) -> set[str]:
    """Every CJK character in the console sources (Python, JavaScript and HTML)."""
    found: set[str] = set()
    for path in (root / "src/orchestrator/web").rglob("*"):
        if path.suffix in {".py", ".js", ".html"}:
            found |= set(CJK.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return found


def rename(font: TTFont, style: str) -> None:
    names = font["name"]
    full = f"{FAMILY} {style}"
    for record in list(names.names):
        if record.nameID in (16, 17, 21, 22):
            names.removeNames(nameID=record.nameID)
    names.setName(FAMILY, 1, 3, 1, 0x409)
    names.setName(style, 2, 3, 1, 0x409)
    names.setName(f"{full};subset", 3, 3, 1, 0x409)
    names.setName(full, 4, 3, 1, 0x409)
    names.setName(full.replace(" ", ""), 6, 3, 1, 0x409)
    names.setName(
        "Modified from IBM Plex Sans SC (Copyright IBM Corp.) under the SIL Open Font License 1.1:"
        " subset to the WuLab Console strings.",
        10,
        3,
        1,
        0x409,
    )


def build(package: Path) -> None:
    text = "".join(sorted(used_characters() | set(ALWAYS)))
    source = package / "fonts/complete/woff2/hinted"
    for weight, style in WEIGHTS.items():
        options = subset.Options()
        options.flavor = "woff2"
        options.hinting = False
        options.desubroutinize = True
        options.name_IDs = ["*"]
        options.notdef_outline = True
        font = subset.load_font(str(source / f"IBMPlexSansSC-{style}.woff2"), options)
        subsetter = subset.Subsetter(options)
        subsetter.populate(text=text)
        subsetter.subset(font)
        rename(font, style)
        target = OUT / f"wulab-sans-sc-{weight}.woff2"
        subset.save_font(font, str(target), options)
        print(f"{target.relative_to(ROOT)}: {target.stat().st_size // 1024} KiB")
    (OUT / "wulab-sans-sc.txt").write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    build(Path(sys.argv[1]))
