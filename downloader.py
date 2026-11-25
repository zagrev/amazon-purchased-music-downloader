import json
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
    return page.locator(TRACKS).evaluate_all("""buttons => buttons.map(button => ({
        id: button.id,
        label: button.getAttribute('aria-label')
    }))""")


def collect_tracks(page, expected, cancel, log):
    """Scan until loading settles, or require an explicit full-library count."""
    deadline = time.monotonic() + 30
    largest = 0
    while True:
        check_cancelled(cancel)
        validate_purchase_page(page)
        tracks = get_tracks(page)
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", track["id"] or "") for track in tracks):
            raise DownloadError("Amazon returned a track without a usable ID; no downloads were started.")
        if len({track["id"] for track in tracks}) != len(tracks):
            raise DownloadError("Amazon returned duplicate track IDs; no downloads were started.")
        if expected is not None and len(tracks) > expected:
            raise DownloadError(f"Amazon showed {len(tracks)} tracks, more than the expected {expected}. Correct the expected count.")
        if expected is not None and len(tracks) == expected:
            return tracks
        if len(tracks) > largest:
            largest = len(tracks)
            deadline = time.monotonic() + (5 if expected is None else 30)
            log(f"Loaded {largest} tracks." if expected is None else f"Loaded {largest} of {expected} expected tracks.")
        if time.monotonic() >= deadline:
            if expected is None:
                if tracks:
                    return tracks
                raise DownloadError("No purchased tracks were found. Check sign-in and the Purchased page, then scan again.")
            raise DownloadError(
                f"Only {len(tracks)} of {expected} expected tracks loaded. "
                "Check the count, your connection, and Amazon's Purchased page, then try again."
            )
        if tracks:
            page.locator(TRACKS).last.scroll_into_view_if_needed(timeout=10000)
            page.evaluate("""selector => {
                let element = document.querySelector(selector);
                while (element && !(element.scrollHeight > element.clientHeight + 10 &&
                       element.clientHeight > 200 && getComputedStyle(element).overflowY === 'auto')) {
                    element = element.parentElement;
                }
                if (element) element.scrollTop = element.scrollHeight;
            }""", TRACKS)
        page.wait_for_timeout(1000)


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


def download_tracks(page, directory, tracks, cancel, log):
    directory = Path(directory)
    history = read_manifest(directory)
    completed = 0
    for number, track in enumerate(tracks, 1):
        check_cancelled(cancel)
        validate_purchase_page(page)
        track_id = track["id"]
        previous = history.get(track_id)
        if previous and (directory / previous).is_file() and (directory / previous).stat().st_size > 0:
            log(f"{number}/{len(tracks)}: already downloaded: {track['label']}")
            completed += 1
            continue
        button = page.locator(TRACKS).filter(has=page.locator(f'xpath=self::*[@id="{track_id}"]'))
        if button.count() != 1:
            raise DownloadError(f"Track {track_id} is no longer visible in the loaded library.")
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
        page.wait_for_timeout(500)
    return completed
