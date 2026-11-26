import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
    validate_scan_timing,
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
  row.innerHTML = `<button data-testid="ListItem,Library_PurchasedSongItem" aria-label="song ${count} by Artist">
    <div dir="auto" tabindex="-1" aria-label="song ${count}">song ${count}</div>
    <a href="/artists/ARTIST" aria-label="Artist">Artist</a>
    <div dir="auto" aria-label="03:45">03:45</div>
    </button><div data-testid="ListItemStack"><div><button data-testid="IconButton,TrackListItem_ContextMenu"
    id="TRACK${count}" aria-label="More details on song ${count}">Options</button></div></div>`;
  document.getElementById('items').appendChild(row);
  row.querySelector('button[data-testid="IconButton,TrackListItem_ContextMenu"]').onclick =
    () => document.getElementById('menu').style.display = 'block';
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


VIRTUAL_PAGE = """<!doctype html>
<button>Purchased</button>
<div id="list" style="height:250px; overflow:auto">
  <div id="items" style="position:relative"></div>
</div>
<div id="menu" style="display:none"><div>Download</div></div>
<script>
const total = 789;
const omitted = new Set();
const list = document.getElementById('list');
const items = document.getElementById('items');
items.style.height = `${total * 50}px`;
let selected;
function render() {
  items.replaceChildren();
  const start = Math.floor(list.scrollTop / 50);
  for (let index = start; index < Math.min(total, start + 6); index++) {
    const number = index + 1;
    if (omitted.has(number)) continue;
    const row = document.createElement('div');
    row.style.cssText = `position:absolute; top:${index * 50}px; height:50px`;
    row.innerHTML = `<button data-testid="ListItem,Library_PurchasedSongItem">
      <div dir="auto" tabindex="-1" aria-label="song ${number}">song ${number}</div>
      </button><div data-testid="ListItemStack"><button
      data-testid="IconButton,TrackListItem_ContextMenu" id="TRACK${number}"
      aria-label="More options for song ${number}">Options</button></div>`;
    row.querySelector('button[data-testid="IconButton,TrackListItem_ContextMenu"]').onclick = () => {
      selected = `TRACK${number}`;
      document.getElementById('menu').style.display = 'block';
    };
    items.appendChild(row);
  }
}
render();
list.addEventListener('scroll', render);
document.querySelector('#menu div').onclick = () => {
  document.getElementById('menu').style.display = 'none';
  const a = document.createElement('a');
  a.href = `/test.mp3?track=${selected}`;
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

    def load_virtual_page(self, total=789):
        self.page.route(
            PURCHASES_URL,
            lambda route: route.fulfill(
                status=200, content_type="text/html",
                body=VIRTUAL_PAGE.replace("const total = 789;", f"const total = {total};"),
            ),
        )
        self.page.reload()

    def test_virtual_scan_counts_all_789_tracks_from_middle_and_deduplicates(self):
        self.load_virtual_page()
        self.page.locator("#list").evaluate("(list) => list.scrollTop = 400 * 50")
        self.page.wait_for_function("document.querySelector('#TRACK401') !== null")
        logs = []
        tracks = collect_tracks(
            self.page, None, self.cancel, logs.append, scroll_wait=0.02, loading_wait=0.5,
        )
        self.assertEqual([track["id"] for track in tracks], [f"TRACK{number}" for number in range(1, 790)])
        self.assertLessEqual(self.page.locator('button[data-testid="IconButton,TrackListItem_ContextMenu"]').count(), 6)
        self.assertIn("Loaded 789 tracks.", logs)
        self.assertEqual(self.page.locator("#TRACK1").count(), 0)
        rescanned = collect_tracks(
            self.page, 789, self.cancel, lambda _: None, scroll_wait=0.02, loading_wait=0.5,
        )
        self.assertEqual(rescanned, tracks)

    def test_virtual_tracks_are_found_again_for_correct_downloads(self):
        self.load_virtual_page()
        self.context.route(
            "https://music.amazon.com/test.mp3?track=*",
            lambda route: route.fulfill(
                status=200,
                headers={"Content-Type": "audio/mpeg", "Content-Disposition": 'attachment; filename="song.mp3"'},
                body=route.request.url.rsplit("=", 1)[1].encode(),
            ),
        )
        tracks = collect_tracks(
            self.page, 789, self.cancel, lambda _: None, scroll_wait=0.02, loading_wait=0.5,
        )
        selected = [tracks[index] for index in (0, 399, 788)]
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.assertEqual(download_tracks(
                self.page, directory, selected, self.cancel, lambda _: None,
                scroll_wait=0.02, loading_wait=0.5,
            ), 3)
            history = read_manifest(directory)
            self.assertEqual(set(history), {"TRACK1", "TRACK400", "TRACK789"})
            for track_id, filename in history.items():
                self.assertEqual((directory / filename).read_bytes(), track_id.encode())
            self.assertEqual(download_tracks(
                self.page, directory, selected, self.cancel, lambda _: None,
                scroll_wait=0.02, loading_wait=0.5,
            ), 3)

    def test_missing_virtual_track_stops_without_downloading_wrong_row(self):
        self.load_virtual_page(total=12)
        tracks = collect_tracks(
            self.page, 12, self.cancel, lambda _: None, scroll_wait=0.02, loading_wait=0.5,
        )
        self.page.evaluate("omitted.add(6); render()")
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(DownloadError, "Could not find track TRACK6"):
                download_tracks(
                    self.page, temp, [tracks[5]], self.cancel, lambda _: None,
                    scroll_wait=0.02, loading_wait=0.5,
                )
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_stalled_scroll_is_not_reported_as_complete(self):
        with patch("downloader.scroll_library", return_value={"moved": False, "bottom": False}):
            with self.assertRaisesRegex(DownloadError, "stalled before the end"):
                collect_tracks(
                    self.page, None, self.cancel, lambda _: None, scroll_wait=0.02, loading_wait=0.1,
                )

    def test_load_download_resume_and_preserve_existing(self):
        tracks = collect_tracks(self.page, 3, self.cancel, lambda _: None)
        self.assertEqual([track["id"] for track in tracks], ["TRACK1", "TRACK2", "TRACK3"])
        self.assertEqual([track["label"] for track in tracks], ["song 1", "song 2", "song 3"])
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            existing = directory / "TRACK1 - song.mp3"
            existing.write_bytes(b"existing")
            logs = []
            self.assertEqual(download_tracks(self.page, directory, tracks, self.cancel, logs.append), 3)
            self.assertEqual(logs, [f"{number}/3: downloading song {number}" for number in range(1, 4)])
            history = read_manifest(directory)
            self.assertEqual(existing.read_bytes(), b"existing")
            self.assertEqual(len(history), 3)
            self.assertEqual((directory / history["TRACK1"]).read_bytes(), b"test audio")
            logs.clear()
            self.assertEqual(download_tracks(self.page, directory, tracks, self.cancel, logs.append), 3)
            self.assertEqual(logs, [f"{number}/3: already downloaded: song {number}" for number in range(1, 4)])
            self.assertEqual(len(list(directory.glob("*.mp3"))), 4)
            (directory / history["TRACK2"]).unlink()
            download_tracks(self.page, directory, tracks, self.cancel, lambda _: None)
            self.assertTrue((directory / read_manifest(directory)["TRACK2"]).is_file())

    def test_track_names_come_from_title_not_menu_description(self):
        for title in ["A Song (Live)", "More details on Love", "More options for Love"]:
            with self.subTest(title=title):
                self.page.locator('[data-testid="ListItem,Library_PurchasedSongItem"]').first.locator(
                    'div[tabindex="-1"]'
                ).evaluate("(element, title) => element.setAttribute('aria-label', title)", title)
                self.page.locator("#TRACK1").evaluate("(button) => button.setAttribute('aria-label', 'Unrelated menu text')")
                tracks = collect_tracks(self.page, 2, self.cancel, lambda _: None)
                self.assertEqual(tracks[0]["label"], title)
                self.assertEqual(tracks[1]["label"], "song 2")

    def test_missing_or_ambiguous_title_reports_page_structure_error(self):
        title = self.page.locator('[data-testid="ListItem,Library_PurchasedSongItem"]').first.locator(
            'div[tabindex="-1"]'
        )
        title.evaluate("(element) => element.after(element.cloneNode(true))")
        with self.assertRaisesRegex(DownloadError, "unique track title"):
            collect_tracks(self.page, 2, self.cancel, lambda _: None)
        self.page.locator('[data-testid="ListItem,Library_PurchasedSongItem"]').first.locator(
            'div[tabindex="-1"]'
        ).evaluate_all("elements => elements.forEach(element => element.remove())")
        with self.assertRaisesRegex(DownloadError, "unique track title"):
            collect_tracks(self.page, 2, self.cancel, lambda _: None)

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
        tracks = collect_tracks(self.page, None, self.cancel, lambda _: None, scroll_wait=0.1, loading_wait=0.5)
        self.assertEqual(len(tracks), 3)
        with self.assertRaisesRegex(DownloadError, "more than the expected"):
            collect_tracks(self.page, 2, self.cancel, lambda _: None)
        tracks = collect_tracks(self.page, 3, self.cancel, lambda _: None)
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(download_tracks(self.page, temp, tracks, self.cancel, lambda _: None), 3)
        self.assertEqual(self.context.cookies()[0]["value"], "signed-in")

    def test_scan_waits_for_batch_delayed_beyond_old_five_second_cutoff(self):
        delayed_page = PAGE.replace(
            "list.addEventListener('scroll', () => { if (list.scrollTop + list.clientHeight >= list.scrollHeight - 1) add(); });",
            """let pending = false;
list.addEventListener('scroll', () => {
  if (!pending && count < 3 && list.scrollTop + list.clientHeight >= list.scrollHeight - 1) {
    pending = true;
    setTimeout(() => { add(); pending = false; }, 5500);
  }
});""",
        )
        self.page.route(
            PURCHASES_URL,
            lambda route: route.fulfill(status=200, content_type="text/html", body=delayed_page),
        )
        self.page.reload()
        tracks = collect_tracks(
            self.page, None, self.cancel, lambda _: None, scroll_wait=0.25, loading_wait=7,
        )
        self.assertEqual([track["id"] for track in tracks], ["TRACK1", "TRACK2", "TRACK3"])

    def test_explicit_count_still_rejects_stalled_partial_list(self):
        self.page.evaluate("count = 3")
        with self.assertRaisesRegex(DownloadError, "Only 2 of 3"):
            collect_tracks(self.page, 3, self.cancel, lambda _: None, scroll_wait=0.1, loading_wait=0.5)

    def test_long_scroll_wait_remains_cancellable(self):
        page = Mock()
        page.url = PURCHASES_URL
        page.get_by_role.return_value.count.return_value = 1
        page.locator.return_value.evaluate_all.return_value = [{"id": "TRACK1", "label": "Song"}]
        page.wait_for_timeout.side_effect = lambda _: self.cancel.set()
        with patch("downloader.scroll_library", return_value={"moved": False, "bottom": True}):
            with self.assertRaises(Cancelled):
                collect_tracks(page, None, self.cancel, lambda _: None, scroll_wait=60, loading_wait=120)
        page.wait_for_timeout.assert_called_once_with(250)

    def test_invalid_loading_waits_are_rejected(self):
        for scroll, loading in [(0, 30), (-1, 30), (float("nan"), 30), (1, float("inf")), (5, 1)]:
            with self.subTest(scroll=scroll, loading=loading), self.assertRaises(DownloadError):
                validate_scan_timing(scroll, loading)

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
