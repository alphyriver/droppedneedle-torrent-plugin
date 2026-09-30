"""Run against an unmodified DroppedNeedle checkout, supplied explicitly."""

import os
import sys
from pathlib import Path

upstream = Path(os.environ["DROPPEDNEEDLE_SOURCE"]).resolve()
sys.path.insert(0, str(upstream / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
