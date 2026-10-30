"""Start the MG400 terminal with Python 3.11 or later."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mg400.terminal_control import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
