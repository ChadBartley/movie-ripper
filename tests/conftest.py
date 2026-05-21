"""Put scripts/ on sys.path so tests can ``from transcripts.X import Y``.

Mirrors how the CLI scripts (scripts/fetch_transcripts.py and
scripts/dialogue_identify.py) resolve the same imports — both contexts
see ``transcripts`` as a top-level package.
"""

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
