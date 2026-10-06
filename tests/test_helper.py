import json
import os
import tempfile
import unittest
from pathlib import Path

from support import FIXTURES, argv_log, load_helper, run_senpai


class HelperCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.log = self.tmp / "argv.jsonl"
        self.env = {
            "FAKE_ANI_PY_LOG": str(self.log),
            "SENPAI_RUNTIME_DIR": str(self.tmp / "run"),
            "FAKE_NOTIFY_LOG": str(self.tmp / "notify.log"),
            "PATH": f"{FIXTURES}:{os.environ.get('PATH', '')}",
            "HOME": str(self.tmp),
        }

    def senpai(self, *args, **env):
        return run_senpai(*args, env={**self.env, **env})

    def calls(self):
        return argv_log(self.log) if self.log.exists() else []


class TestReadCommands(HelperCase):
    def test_search_passes_the_query_and_provider_through(self):
        rc, payload, _ = self.senpai("search", "one piece", "--provider", "animeav1")
        self.assertEqual(rc, 0)
        self.assertEqual(payload["results"][0]["id"], "one-piece-100")
        self.assertEqual(self.calls(), [["--json", "--provider", "animeav1", "one piece"]])

    def test_search_with_the_auto_provider_adds_no_flag(self):
        self.senpai("search", "one piece", "--provider", "auto")
        self.assertEqual(self.calls(), [["--json", "one piece"]])

    def test_an_ani_py_error_is_passed_on_with_exit_code_1(self):
        rc, payload, _ = self.senpai("search", "nothing here")
        self.assertEqual(rc, 1)
        self.assertEqual(payload, {"error": "No results found."})

    def test_episodes_selects_by_provider_and_id(self):
        rc, payload, _ = self.senpai("episodes", "hianime", "one-piece-100", "--title", "One Piece")
        self.assertEqual(rc, 0)
        self.assertEqual(payload["anime"]["title"], "One Piece")
        self.assertEqual(len(payload["episodes"]), 5)
        self.assertEqual(self.calls(), [["--json", "--select", "hianime:one-piece-100", "--list-episodes", "--title", "One Piece"]])

    def test_home_continues_after_a_finished_episode_and_lists_recents(self):
        rc, payload, _ = self.senpai("home")
        self.assertEqual(rc, 0)
        self.assertEqual(payload["continue"], {
            "provider": "hianime", "id": "one-piece-100", "title": "One Piece",
            "episode": "5", "completed": True, "resume": False,
        })
        self.assertEqual([r["id"] for r in payload["recent"]], ["one-piece-100", "frieren-481"])

    def test_home_resumes_an_unfinished_episode(self):
        history = self.tmp / "history.json"
        history.write_text(json.dumps([
            {"provider": "hianime", "id": "frieren-481", "title": "Frieren", "episode": "7", "completed": False},
        ]), encoding="utf-8")
        _, payload, _ = self.senpai("home", FAKE_HISTORY=str(history))
        self.assertEqual(payload["continue"]["episode"], "7")
        self.assertTrue(payload["continue"]["resume"])

    def test_home_with_an_empty_history(self):
        history = self.tmp / "history.json"
        history.write_text("[]", encoding="utf-8")
        _, payload, _ = self.senpai("home", FAKE_HISTORY=str(history))
        self.assertEqual(payload, {"continue": None, "recent": []})

    def test_continue_after_a_half_episode_number(self):
        helper = load_helper()
        self.assertEqual(helper.next_episode("12.5"), "13")
        self.assertEqual(helper.next_episode("12"), "13")
        self.assertEqual(helper.next_episode("special"), "special")
        entry = helper.continue_entry([{"provider": "p", "id": "x", "title": "X", "episode": "12.5", "completed": True}])
        self.assertEqual((entry["episode"], entry["resume"]), ("13", False))

    def test_recent_keeps_one_row_per_anime_and_at_most_twenty(self):
        helper = load_helper()
        entries = [{"provider": "p", "id": f"a{i % 25}", "title": "T", "episode": "1", "completed": True} for i in range(60)]
        recent = helper.recent_entries(entries)
        self.assertEqual(len(recent), 20)
        self.assertEqual(len({r["id"] for r in recent}), 20)

    def test_a_slow_ani_py_is_reported_as_a_timeout(self):
        helper = load_helper()
        with self.assertRaises(helper.SenpaiError) as caught:
            helper.ani_py(["slow"], str(FIXTURES / "fake-ani-py"), timeout=0.5, env={"FAKE_HANG": "3"})
        self.assertIn("longer than", str(caught.exception))

    def test_a_missing_ani_py_is_a_clear_error(self):
        rc, payload, _ = self.senpai("search", "x", SENPAI_ANI_PY=str(self.tmp / "nope.py"))
        self.assertEqual(rc, 1)
        self.assertIn("ani_py.py not found", payload["error"])


if __name__ == "__main__":
    unittest.main()
