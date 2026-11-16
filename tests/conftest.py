# Run from THIS directory (plugin root's __init__.py imports gateway internals
# and breaks pytest collection if pytest's basedir walk reaches it):
#   cd ~/.hermes/plugins/agy-meter/tests
#   uv run --no-project --with pytest python -m pytest test_agy_meter.py -q
import os
from pathlib import Path

os.environ.setdefault("AGY_METER_DIR", str(Path(__file__).resolve().parent.parent))
