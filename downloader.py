import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeout


PURCHASES_URL = "https://music.amazon.com/my/purchases"
TRACKS = 'button[data-testid="IconButton,TrackListItem_ContextMenu"]'
MANIFEST = ".amazon-music-downloads.json"


class DownloadError(Exception):
    pass


class Cancelled(Exception):
    pass


def check_cancelled(cancel):
    if cancel.is_set():
        raise Cancelled("Stopped by user.")


def validate_purchase_page(page):
    url = urlsplit(page.url)
    if url.scheme != "https" or url.hostname != "music.amazon.com" or url.path.rstrip("/") != "/my/purchases":
        raise DownloadError("Open Amazon Music's Purchased page in the browser before continuing.")
    if page.get_by_role("button", name="Purchased", exact=True).count() != 1:
        raise DownloadError("The Purchased page is not ready. Check sign-in and try again.")


def open_purchased_page(page):
    url = urlsplit(page.url)
    if url.hostname != "music.amazon.com" or url.path.rstrip("/") != "/my/purchases":
        page.goto(PURCHASES_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.get_by_role("button", name="Purchased", exact=True).wait_for(timeout=30000)
    except PlaywrightTimeout as exc:
        raise DownloadError("Could not open Purchased. Check that you completed sign-in.") from exc
    validate_purchase_page(page)


def get_tracks(page):
    tracks = page.locator(TRACKS).evaluate_all("""buttons => buttons.map(button => {
        const item = button.closest('[data-testid="ListItemStack"]')?.parentElement
            .querySelector('[data-testid="ListItem,Library_PurchasedSongItem"]');
        const titles = item?.querySelectorAll('div[dir="auto"][tabindex="-1"][aria-label]');
        return {
            id: button.id,
            label: titles?.length === 1 ? titles[0].getAttribute('aria-label') : null
        };
    })""")
    for track in tracks:
        if not isinstance(track["label"], str) or not track["label"].strip():
            raise DownloadError(
                f"Could not find a unique track title for {track['id']}; "
                "Amazon's Purchased page structure may have changed. No downloads were started."
            )
    return tracks


def validate_scan_timing(scroll_wait, loading_wait):
    if not math.isfinite(scroll_wait) or scroll_wait <= 0:
        raise DownloadError("Scroll wait must be a positive number of seconds.")
    if not math.isfinite(loading_wait) or loading_wait < scroll_wait:
        raise DownloadError("No-progress wait must be at least as long as the scroll wait.")


def validate_download_delay(seconds):
    if not math.isfinite(seconds) or seconds < 0:
        raise DownloadError("Download pause must be a finite number of seconds, at least 0.")


def wait_with_cancel(page, seconds, cancel):
    remaining = seconds * 1000
    while remaining > 0:
        check_cancelled(cancel)
        interval = min(remaining, 250)
        page.wait_for_timeout(interval)
        remaining -= interval


def scroll_library(page, *, to_top=False):
    state = page.evaluate("""({selector, toTop}) => {
        let element = document.querySelector(selector);
        if (!element) return null;
        while (element && !(element.clientHeight > 200 &&
               ['auto', 'scroll'].includes(getComputedStyle(element).overflowY))) {
            element = element.parentElement;
        }
        element = element || document.scrollingElement;
        if (!element) return null;
        const before = element.scrollTop;
        const maximum = Math.max(0, element.scrollHeight - element.clientHeight);
        element.scrollTop = toTop ? 0 : Math.min(maximum, before + element.clientHeight * 0.8);
        return {
            moved: Math.abs(element.scrollTop - before) > 1,
            bottom: element.scrollTop >= maximum - 1
        };
    }""", {"selector": TRACKS, "toTop": to_top})
    if state is None:
        raise DownloadError("Could not find the Purchased list to scroll. Check the browser page and try again.")
    return state


def collect_tracks(page, expected, cancel, log, *, scroll_wait=1, loading_wait=30):
    """Accumulate unique tracks across a virtualized list, starting at the top."""
    validate_scan_timing(scroll_wait, loading_wait)
    deadline = time.monotonic() + loading_wait
    seen = {}
    started = False
    at_bottom = False
    while True:
        check_cancelled(cancel)
        validate_purchase_page(page)
        tracks = get_tracks(page)
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", track["id"] or "") for track in tracks):
            raise DownloadError("Amazon returned a track without a usable ID; no downloads were started.")
        if len({track["id"] for track in tracks}) != len(tracks):
            raise DownloadError("Amazon returned duplicate track IDs; no downloads were started.")
        if tracks and not started:
            started = True
            if scroll_library(page, to_top=True)["moved"]:
                wait_with_cancel(page, scroll_wait, cancel)
                deadline = time.monotonic() + loading_wait
                continue
        previous_count = len(seen)
        for track in tracks:
            seen[track["id"]] = track
        count = len(seen)
        if expected is not None and count > expected:
            raise DownloadError(f"Amazon showed {count} tracks, more than the expected {expected}. Correct the expected count.")
        if count > previous_count:
            deadline = time.monotonic() + loading_wait
            log(f"Loaded {count} tracks." if expected is None else f"Loaded {count} of {expected} expected tracks.")
        if expected is not None and count == expected:
            return list(seen.values())
        if time.monotonic() >= deadline:
            if expected is None:
                if seen and at_bottom:
                    log(f"No new tracks for {loading_wait:g} seconds. "
                        "The scan may be incomplete; confirm the full-library count before downloading.")
                    return list(seen.values())
                if seen:
                    raise DownloadError(
                        f"Found {count} tracks, but scrolling stalled before the end of Purchased. "
                        "Check the browser page and try again."
                    )
                raise DownloadError("No purchased tracks were found. Check sign-in and the Purchased page, then scan again.")
            raise DownloadError(
                f"Only {count} of {expected} expected tracks loaded. "
                f"No new tracks appeared for {loading_wait:g} seconds. "
                "Check the count, increase the loading waits, or check Amazon's Purchased page, then try again."
            )
        if tracks:
            state = scroll_library(page)
            at_bottom = state["bottom"]
            if state["moved"]:
                deadline = time.monotonic() + loading_wait
        wait_with_cancel(page, scroll_wait, cancel)


def find_track_button(page, track_id, cancel, scroll_wait, loading_wait):
    button = page.locator(TRACKS).filter(has=page.locator(f'xpath=self::*[@id="{track_id}"]'))
    deadline = time.monotonic() + loading_wait
    rewound = False
    while True:
        check_cancelled(cancel)
        validate_purchase_page(page)
        count = button.count()
        if count == 1:
            return button
        if count > 1:
            raise DownloadError(f"Amazon returned duplicate buttons for track {track_id}.")
        if time.monotonic() >= deadline:
            raise DownloadError(f"Could not find track {track_id} while scrolling Purchased. No download was started for it.")
        state = scroll_library(page)
        if state["bottom"] and not state["moved"] and not rewound:
            state = scroll_library(page, to_top=True)
            rewound = True
        if state["moved"]:
            deadline = time.monotonic() + loading_wait
        wait_with_cancel(page, scroll_wait, cancel)


def read_manifest(directory):
    path = directory / MANIFEST
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DownloadError(f"Cannot read download history: {exc}") from exc
    if not isinstance(data, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or Path(value).name != value
        or value in ("", ".", "..")
        for key, value in data.items()
    ):
        raise DownloadError("Download history has an invalid format; fix or move it before continuing.")
    return data


def write_manifest(directory, data):
    fd, temp = tempfile.mkstemp(prefix=".amazon-music-history-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temp, directory / MANIFEST)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def destination_name(track_id, suggested):
    name = Path(suggested.replace("\\", "/").split("/")[-1]).name
    if Path(name).suffix.lower() != ".mp3":
        raise DownloadError(f"Amazon offered an unexpected file type for {track_id}: {name!r}")
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name).stem).strip(" .")
    return f"{track_id} - {stem[:120] or 'track'}.mp3"


def save_download(download, directory, track_id):
    if download.failure():
        raise DownloadError(f"Download failed for {track_id}: {download.failure()}")
    name = destination_name(track_id, download.suggested_filename)
    fd, temp = tempfile.mkstemp(prefix=".amazon-music-", suffix=".tmp", dir=directory)
    os.close(fd)
    try:
        download.save_as(temp)
        if os.path.getsize(temp) == 0:
            raise DownloadError(f"Amazon returned an empty file for {track_id}.")
        base = Path(name).stem
        for suffix in range(10000):
            candidate = directory / (name if suffix == 0 else f"{base} ({suffix}).mp3")
            try:
                os.link(temp, candidate)
                return candidate.name
            except FileExistsError:
                continue
        raise DownloadError(f"Could not find a free filename for {track_id}.")
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def download_tracks(page, directory, tracks, cancel, log, *, scroll_wait=1, loading_wait=30, download_delay=0.5):
    validate_scan_timing(scroll_wait, loading_wait)
    validate_download_delay(download_delay)
    directory = Path(directory)
    history = read_manifest(directory)
    completed = 0
    has_downloaded = False
    for number, track in enumerate(tracks, 1):
        check_cancelled(cancel)
        validate_purchase_page(page)
        track_id = track["id"]
        previous = history.get(track_id)
        if previous and (directory / previous).is_file() and (directory / previous).stat().st_size > 0:
            log(f"{number}/{len(tracks)}: already downloaded: {track['label']}")
            completed += 1
            continue
        if has_downloaded:
            wait_with_cancel(page, download_delay, cancel)
        button = find_track_button(page, track_id, cancel, scroll_wait, loading_wait)
        log(f"{number}/{len(tracks)}: downloading {track['label']}")
        button.click(timeout=10000)
        try:
            with page.expect_download(timeout=90000) as info:
                page.get_by_text("Download", exact=True).click(timeout=10000)
            filename = save_download(info.value, directory, track_id)
        except PlaywrightTimeout as exc:
            raise DownloadError(f"Amazon did not start a download for {track['label']}.") from exc
        history[track_id] = filename
        write_manifest(directory, history)
        completed += 1
        has_downloaded = True
    return completed
