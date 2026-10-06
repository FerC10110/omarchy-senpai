import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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


def wait_for(predicate, seconds=6.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class TestPlayback(HelperCase):
    PLAY = ("play", "hianime", "one-piece-100", "-e", "4", "--title", "One Piece")

    def run_dir(self):
        return self.tmp / "run"

    def test_play_builds_the_headless_argv_from_the_settings(self):
        rc, payload, _ = self.senpai(
            *self.PLAY, "--sub-lang", "latino,es,en", "--quality", "1080", "--dub",
            "--auto-next", "--no-sub-search", "--skip",
        )
        self.assertEqual(rc, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(wait_for(lambda: self.calls()))
        self.assertEqual(self.calls()[0], [
            "--json", "--headless", "--select", "hianime:one-piece-100", "-e", "4", "--title", "One Piece",
            "--ipc-socket", str(self.run_dir() / "mpv.sock"),
            "--sub-lang", "latino,es,en", "-q", "1080", "--dub", "--auto-next", "--no-sub-search", "--skip",
        ])

    def test_play_passes_the_title_verbatim(self):
        title = "Frieren: Beyond Journey's End & \"Dan Da Dan\""
        self.senpai("play", "hianime", "frieren-481", "-e", "1", "--title", title)
        self.assertTrue(wait_for(lambda: self.calls()))
        argv = self.calls()[0]
        self.assertEqual(argv[argv.index("--title") + 1], title)
        state = json.loads((self.run_dir() / "now-playing.json").read_text(encoding="utf-8"))
        self.assertEqual(state["title"], title)

    def test_play_writes_now_playing_and_clears_it_when_ani_py_ends(self):
        rc, payload, _ = self.senpai(*self.PLAY)
        state_file = self.run_dir() / "now-playing.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["pid"], payload["pid"])
        self.assertEqual((state["provider"], state["id"], state["episode"], state["title"]), ("hianime", "one-piece-100", "4", "One Piece"))
        self.assertEqual(state["socket"], str(self.run_dir() / "mpv.sock"))
        self.assertTrue(wait_for(lambda: not state_file.exists()))
        events = [json.loads(l) for l in (self.run_dir() / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["event"] for e in events], ["resolving", "playing", "completed", "ended"])

    def test_play_stops_the_previous_playback_first(self):
        self.senpai(*self.PLAY, FAKE_SLEEP="30")
        first = json.loads((self.run_dir() / "now-playing.json").read_text(encoding="utf-8"))
        self.assertTrue(wait_for(lambda: len(self.calls()) == 1))
        rc, payload, _ = self.senpai("play", "hianime", "frieren-481", "-e", "2", "--title", "Frieren", FAKE_SLEEP="30")
        self.assertEqual(rc, 0)
        second = json.loads((self.run_dir() / "now-playing.json").read_text(encoding="utf-8"))
        self.assertNotEqual(first["pid"], second["pid"])
        self.assertEqual((second["id"], second["episode"]), ("frieren-481", "2"))
        helper = load_helper()
        self.assertTrue(wait_for(lambda: not helper.pid_alive(first["pid"])))
        self.senpai("stop")

    def test_stop_without_a_socket_ends_the_supervisor_and_the_player(self):
        _, payload, _ = self.senpai(*self.PLAY, FAKE_SLEEP="30")
        helper = load_helper()
        self.assertTrue(helper.pid_alive(payload["pid"]))
        rc, stopped, _ = self.senpai("stop")
        self.assertEqual(rc, 0)
        self.assertEqual(stopped, {"ok": True, "stopped": True})
        self.assertTrue(wait_for(lambda: not helper.pid_alive(payload["pid"])))
        self.assertFalse((self.run_dir() / "now-playing.json").exists())

    def test_stop_with_nothing_playing_is_fine(self):
        rc, payload, _ = self.senpai("stop")
        self.assertEqual((rc, payload), (0, {"ok": True, "stopped": False}))

    def test_status_drops_a_playback_whose_supervisor_died(self):
        run = self.run_dir()
        run.mkdir(parents=True, exist_ok=True)
        (run / "now-playing.json").write_text(json.dumps({"pid": 999999, "title": "Ghost", "episode": "1"}), encoding="utf-8")
        rc, payload, _ = self.senpai("status")
        self.assertEqual(rc, 0)
        self.assertIsNone(payload["playing"])
        self.assertFalse((run / "now-playing.json").exists())

    def test_status_reports_the_live_playback(self):
        self.senpai(*self.PLAY, FAKE_SLEEP="30")
        _, payload, _ = self.senpai("status")
        self.assertEqual(payload["playing"]["title"], "One Piece")
        self.senpai("stop")

    def test_continue_plays_the_next_episode_from_the_history(self):
        rc, payload, _ = self.senpai("continue", "--sub-lang", "latino,es,en")
        self.assertEqual(rc, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(wait_for(lambda: len(self.calls()) == 2))
        play_argv = self.calls()[1]
        self.assertEqual(play_argv[play_argv.index("--select") + 1], "hianime:one-piece-100")
        self.assertEqual(play_argv[play_argv.index("-e") + 1], "5")
        self.assertEqual(play_argv[play_argv.index("--title") + 1], "One Piece")

    def test_continue_with_an_empty_history_is_an_error(self):
        history = self.tmp / "history.json"
        history.write_text("[]", encoding="utf-8")
        rc, payload, _ = self.senpai("continue", FAKE_HISTORY=str(history))
        self.assertEqual((rc, payload), (1, {"error": "History is empty."}))


class TestDownloads(HelperCase):
    def test_download_runs_detached_records_its_state_and_notifies(self):
        rc, payload, _ = self.senpai("download", "hianime", "one-piece-100", "-e", "1-2", "--title", "One Piece", "--download-dir", str(self.tmp / "dl"))
        self.assertEqual(rc, 0)
        self.assertTrue(payload["ok"])
        state_file = self.tmp / "run" / "downloads" / f"{payload['id']}.json"
        self.assertTrue(wait_for(lambda: state_file.exists() and json.loads(state_file.read_text(encoding="utf-8")).get("state") == "done"))
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual((state["title"], state["episodes"], state["rc"]), ("One Piece", "1-2", 0))
        self.assertTrue(wait_for(lambda: self.calls()))
        argv = self.calls()[0]
        self.assertIn("-d", argv)
        self.assertNotIn("--ipc-socket", argv)
        self.assertNotIn("--auto-next", argv)
        self.assertTrue(wait_for(lambda: (self.tmp / "notify.log").exists()))
        self.assertIn("One Piece 1-2 downloaded", (self.tmp / "notify.log").read_text(encoding="utf-8"))

    def test_download_directory_reaches_ani_py_through_the_environment(self):
        helper = load_helper()
        with patch.dict(os.environ, {"HOME": str(self.tmp)}):
            self.assertEqual(helper.default_download_dir(), self.tmp / "Downloads")
            (self.tmp / "Videos" / "anime").mkdir(parents=True)
            self.assertEqual(helper.default_download_dir(), self.tmp / "Videos" / "anime")

    def test_a_failed_download_is_marked_failed_and_notified(self):
        events = json.dumps([{"event": "downloading", "episode": "1"}, {"event": "download-failed", "episode": "1", "rc": 1}, {"event": "ended", "rc": 1}])
        _, payload, _ = self.senpai("download", "hianime", "one-piece-100", "-e", "1", "--title", "One Piece", FAKE_EVENTS=events)
        state_file = self.tmp / "run" / "downloads" / f"{payload['id']}.json"
        self.assertTrue(wait_for(lambda: state_file.exists() and json.loads(state_file.read_text(encoding="utf-8")).get("state") == "failed"))
        self.assertTrue(wait_for(lambda: (self.tmp / "notify.log").exists()))
        self.assertIn("download failed", (self.tmp / "notify.log").read_text(encoding="utf-8"))

    def test_status_lists_downloads_newest_first_and_marks_dead_ones_failed(self):
        run = self.tmp / "run" / "downloads"
        run.mkdir(parents=True)
        (run / "20260101-000000-1.json").write_text(json.dumps({"id": "20260101-000000-1", "pid": 999999, "state": "running", "title": "Old"}), encoding="utf-8")
        (run / "20260102-000000-1.json").write_text(json.dumps({"id": "20260102-000000-1", "pid": os.getpid(), "state": "done", "title": "New"}), encoding="utf-8")
        _, payload, _ = self.senpai("status")
        self.assertEqual([(d["title"], d["state"]) for d in payload["downloads"]], [("New", "done"), ("Old", "failed")])


if __name__ == "__main__":
    unittest.main()
