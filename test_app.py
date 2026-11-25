import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import App
from downloader import DownloadError, collect_tracks


class SessionTests(unittest.TestCase):
    def test_worker_reuses_browser_after_error_completion_and_stop_until_app_closes(self):
        app = SimpleNamespace(commands=queue.Queue(), messages=queue.Queue(), cancel=threading.Event())
        browser = Mock()
        browser.is_connected.return_value = True
        page = browser.new_context.return_value.new_page.return_value
        page.is_closed.return_value = False
        playwright = Mock()
        playwright.chromium.launch.return_value = browser

        def finish_command():
            logs = []
            while True:
                kind, value = app.messages.get(timeout=5)
                if kind == "done":
                    break
                if kind == "log":
                    logs.append(value)
                if kind == "log" and "unexpected error" in value:
                    self.fail(value)
                if kind == "closed":
                    self.fail("Worker exited before app shutdown")
            browser.close.assert_not_called()
            return logs

        with (
            patch("app.sync_playwright") as start,
            patch("app.open_purchased_page"),
            patch("app.collect_tracks", side_effect=[
                DownloadError("Wrong count"),
                [{"id": "TRACK1", "label": "Song"}],
            ]) as collect,
            patch("app.download_tracks", return_value=1) as download,
        ):
            start.return_value.__enter__.return_value = playwright
            worker = threading.Thread(target=App.worker, args=(app,), daemon=True)
            worker.start()
            try:
                app.commands.put(("open", None))
                finish_command()
                app.commands.put(("scan", None))
                self.assertIn("Stopped: Wrong count", finish_command())
                app.commands.put(("download", ("destination", 1)))
                self.assertIn("Finished: 1 tracks downloaded or previously recorded.", finish_command())
                self.assertEqual(collect.call_count, 2)
                download.assert_called_once()
                playwright.chromium.launch.assert_called_once()
                browser.new_context.assert_called_once_with(accept_downloads=True)
                app.cancel.set()
                # Exercise the real cancellation check without touching Amazon.
                collect.side_effect = collect_tracks
                app.commands.put(("scan", None))
                self.assertIn("Stopped: Stopped by user.", finish_command())
                playwright.chromium.launch.assert_called_once()
            finally:
                app.commands.put(("close", None))
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            browser.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
