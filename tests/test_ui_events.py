"""Regression checks for dashboard event handlers that must survive periodic refreshes."""
import pathlib
import unittest


UI = pathlib.Path(__file__).resolve().parents[1] / "codex_os3" / "ui" / "index.html"


class DashboardEventsTest(unittest.TestCase):
    def test_restart_handler_is_registered_once_outside_activity_refresh(self):
        page = UI.read_text(encoding="utf-8")
        activity = page.split("async function activity(st) {", 1)[1].split("\nconst FIELDS", 1)[0]
        self.assertNotIn('document.addEventListener("click"', activity)
        self.assertEqual(page.count('if (e.target.id === "restart")'), 1)
        self.assertEqual(page.count('if (e.target.id === "toreport")'), 1)


if __name__ == "__main__":
    unittest.main()
