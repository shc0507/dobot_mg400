"""Kit --exec entry point; run the GUI test without creating a SimulationApp."""

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("validate_grasp.py")))
