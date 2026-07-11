"""Vercel entrypoint for the Enterprise Dashboard."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
CHATBOT_DIR = PROJECT_DIR / "chatbot"

# A Vercel Function's deployed files are read-only. Seed the editable workbook
# and SQLite databases in the function's writable scratch directory instead.
if os.environ.get("VERCEL"):
    data_dir = Path("/tmp/enterprise-dashboard")
    data_dir.mkdir(parents=True, exist_ok=True)
    workbook = data_dir / "sample_data.xlsx"
    if not workbook.exists():
        shutil.copy2(PROJECT_DIR / "sample_data.xlsx", workbook)
    os.environ.setdefault("DASHBOARD_DATA_DIR", str(data_dir))
    os.environ.setdefault("DASHBOARD_DB_PATH", str(data_dir / "chatbot_data.db"))
    os.environ.setdefault("DASHBOARD_AUTH_DB_PATH", str(data_dir / "auth_data.db"))

# chatbot/server.py also supports direct execution and therefore imports its
# sibling init_db module by name.
sys.path.insert(0, str(CHATBOT_DIR))

from chatbot.server import DashboardHandler  # noqa: E402


# Keep this as an explicit class declaration: Vercel's static entrypoint
# detector does not recognize an imported class assigned to an alias.
class handler(DashboardHandler):
    """Handle all dashboard routes in a Vercel Python Function."""

    def _normalize_vercel_path(self) -> None:
        """Translate Vercel's internal function mount path to the site root."""
        if self.path in {"/server", "/server.py"}:
            self.path = "/"

    def do_GET(self) -> None:
        self._normalize_vercel_path()
        super().do_GET()

    def do_POST(self) -> None:
        self._normalize_vercel_path()
        super().do_POST()
