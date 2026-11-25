import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from downloader import (
    Cancelled,
    DownloadError,
    PURCHASES_URL,
    collect_tracks,
    destination_name,
    download_tracks,
    open_purchased_page,
    read_manifest,
)


PAGE = """<!doctype html>
<button>Purchased</button>
<div id="list" style="height:250px; overflow:auto">
  <div id="items"></div>
</div>
<div id="menu" style="display:none"><div>Download</div></div>
<script>
let count = 0;
const list = document.getElementById('list');
function add() {
  if (count >= 3) return;
  count++;
  const row = document.createElement('div');
  row.style.height = '180px';
  row.innerHTML = `<button data-testid="IconButton,TrackListItem_ContextMenu"
    id="TRACK${count}" aria-label="More options for song ${count}">Options</button>`;
  document.getElementById('items').appendChild(row);
  row.querySelector('button').onclick = () => document.getElementById('menu').style.display = 'block';
}
add(); add();
list.addEventListener('scroll', () => { if (list.scrollTop + list.clientHeight >= list.scrollHeight - 1) add(); });
document.querySelector('#menu div').onclick = () => {
  document.getElementById('menu').style.display = 'none';
  const a = document.createElement('a');
  a.href = '/test.mp3';
  a.click();
};
</script>"""


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(accept_downloads=True)
        self.context.route(
            PURCHASES_URL,
            lambda route: route.fulfill(status=200, content_type="text/html", body=PAGE),
        )
        self.context.route(
            "https://music.amazon.com/test.mp3",
            lambda route: route.fulfill(
                status=200,
                headers={"Content-Type": "audio/mpeg", "Content-Disposition": 'attachment; filename="song.mp3"'},
                body=b"test audio",
            ),
        )
        self.page = self.context.new_page()
        self.page.goto(PURCHASES_URL)
        self.cancel = threading.Event()

    def tearDown(self):
        self.context.close()

    def test_load_download_resume_and_preserve_existing(self):
        tracks = collect_tracks(self.page, 3, self.cancel, lambda _: None)
        self.assertEqual([track["id"] for track in tracks], ["TRACK1", "TRACK2", "TRACK3"])
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            existing = directory / "TRACK1 - song.mp3"
            existing.write_bytes(b"existing")
            self.assertEqual(download_tracks(self.page, directory, tracks, self.cancel, lambda _: None), 3)
            history = read_manifest(directory)
            self.assertEqual(existing.read_bytes(), b"existing")
            self.assertEqual(len(history), 3)
            self.assertEqual((directory / history["TRACK1"]).read_bytes(), b"test audio")
            self.assertEqual(download_tracks(self.page, directory, tracks, self.cancel, lambda _: None), 3)
            self.assertEqual(len(list(directory.glob("*.mp3"))), 4)
            (directory / history["TRACK2"]).unlink()
            download_tracks(self.page, directory, tracks, self.cancel, lambda _: None)
            self.assertTrue((directory / read_manifest(directory)["TRACK2"]).is_file())

    def test_incorrect_count_and_cancel_stop_before_download(self):
        with self.assertRaisesRegex(DownloadError, "more than the expected"):
            collect_tracks(self.page, 1, self.cancel, lambda _: None)
        self.cancel.set()
        with self.assertRaises(Cancelled):
            collect_tracks(self.page, 3, self.cancel, lambda _: None)

    def test_scan_then_correct_count_and_download_in_same_session(self):
        self.context.add_cookies([{
            "name": "test-session", "value": "signed-in", "url": PURCHASES_URL,
        }])
        tracks = collect_tracks(self.page, None, self.cancel, lambda _: None)
        self.assertEqual(len(tracks), 3)
        with self.assertRaisesRegex(DownloadError, "more than the expected"):
            collect_tracks(self.page, 2, self.cancel, lambda _: None)
        tracks = collect_tracks(self.page, 3, self.cancel, lambda _: None)
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(download_tracks(self.page, temp, tracks, self.cancel, lambda _: None), 3)
        self.assertEqual(self.context.cookies()[0]["value"], "signed-in")

    def test_empty_scan_reports_error(self):
        self.page.locator("#items").evaluate("(e) => e.innerHTML = ''")
        with patch("downloader.time") as clock:
            clock.monotonic.side_effect = [0, 31]
            with self.assertRaisesRegex(DownloadError, "No purchased tracks"):
                collect_tracks(self.page, None, self.cancel, lambda _: None)

    def test_rejects_duplicate_ids_and_wrong_page(self):
        self.page.locator("#TRACK2").evaluate("(e) => e.id = 'TRACK1'")
        with self.assertRaisesRegex(DownloadError, "duplicate"):
            collect_tracks(self.page, 2, self.cancel, lambda _: None)
        self.page.goto("about:blank")
        with self.assertRaisesRegex(DownloadError, "Purchased page"):
            collect_tracks(self.page, 2, self.cancel, lambda _: None)

    def test_open_purchased_page_after_sign_in(self):
        self.page.goto("about:blank")
        open_purchased_page(self.page)
        self.assertEqual(self.page.url, PURCHASES_URL)

    def test_bad_history_and_non_mp3_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / ".amazon-music-downloads.json").write_text('{"TRACK1":"../outside"}')
            with self.assertRaises(DownloadError):
                read_manifest(directory)
        with self.assertRaises(DownloadError):
            destination_name("TRACK1", "song.zip")


if __name__ == "__main__":
    unittest.main()
