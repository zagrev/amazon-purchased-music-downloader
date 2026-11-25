import io
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import App, main
from downloader import DownloadError, collect_tracks


class SessionTests(unittest.TestCase):
    def test_worker_reuses_browser_after_error_completion_and_stop_until_app_closes(self):
        app = SimpleNamespace(
            commands=queue.Queue(), messages=queue.Queue(), cancel=threading.Event(), browser_choice="edge",
        )
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
                app.commands.put(("scan", (3, 60)))
                self.assertIn("Stopped: Wrong count", finish_command())
                app.commands.put(("download", ("destination", 1, 3, 60)))
                self.assertIn("Finished: 1 tracks downloaded or previously recorded.", finish_command())
                self.assertEqual(collect.call_count, 2)
                for call in collect.call_args_list:
                    self.assertEqual(call.kwargs, {"scroll_wait": 3, "loading_wait": 60})
                download.assert_called_once()
                playwright.chromium.launch.assert_called_once()
                browser.new_context.assert_called_once_with(accept_downloads=True)
                app.cancel.set()
                # Exercise the real cancellation check without touching Amazon.
                collect.side_effect = collect_tracks
                app.commands.put(("scan", (3, 60)))
                self.assertIn("Stopped: Stopped by user.", finish_command())
                playwright.chromium.launch.assert_called_once()
            finally:
                app.commands.put(("close", None))
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            browser.close.assert_called_once()


class BrowserSelectionTests(unittest.TestCase):
    def test_command_line_default_and_selections(self):
        for args, expected in [
            ([], "edge"),
            (["--browser", "edge"], "edge"),
            (["--browser", "chrome"], "chrome"),
            (["--browser", "chromium"], "chromium"),
            (["--browser", "firefox"], "firefox"),
            (["--browser", "webkit"], "webkit"),
        ]:
            with self.subTest(args=args), patch("app.App") as app:
                main(args)
                app.assert_called_once_with(browser=expected)
                app.return_value.mainloop.assert_called_once()

    def test_invalid_browser_exits_before_creating_window(self):
        with patch("app.App") as app, patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as result:
                main(["--browser", "unsupported"])
            self.assertEqual(result.exception.code, 2)
            self.assertIn("invalid choice", stderr.getvalue())
            app.assert_not_called()

    def test_worker_launches_selected_engine_and_channel(self):
        for choice, engine, channel in [
            ("edge", "chromium", "msedge"),
            ("chrome", "chromium", "chrome"),
            ("chromium", "chromium", None),
            ("firefox", "firefox", None),
            ("webkit", "webkit", None),
        ]:
            with self.subTest(browser=choice), patch("app.sync_playwright") as start:
                app = SimpleNamespace(
                    commands=queue.Queue(), messages=queue.Queue(),
                    cancel=threading.Event(), browser_choice=choice,
                )
                app.commands.put(("open", None))
                app.commands.put(("close", None))
                playwright = start.return_value.__enter__.return_value
                browser = getattr(playwright, engine).launch.return_value
                App.worker(app)
                getattr(playwright, engine).launch.assert_called_once_with(channel=channel, headless=False)
                browser.new_context.assert_called_once_with(accept_downloads=True)
                browser.close.assert_called_once()
                logs = []
                while not app.messages.empty():
                    kind, value = app.messages.get_nowait()
                    if kind == "log":
                        logs.append(value)
                self.assertEqual(logs, [])


if __name__ == "__main__":
    unittest.main()
