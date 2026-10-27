"""Run the MG400 mode command from a repository checkout."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mg400_agent.control_mode import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
