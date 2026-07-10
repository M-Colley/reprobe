import sys
from pathlib import Path

# Allow running the test suite from a source checkout without `pip install`.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
