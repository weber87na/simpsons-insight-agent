from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
TEST_RUNTIME = Path(tempfile.mkdtemp(prefix="simpsons-insight-agent-tests-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(TEST_RUNTIME / 'test.db').as_posix()}"

alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
alembic_config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
command.upgrade(alembic_config, "head")

atexit.register(shutil.rmtree, TEST_RUNTIME, True)
