import argparse
import ctypes
import json
import os
import queue
import tempfile
import threading
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from downloader import (
    Cancelled, DownloadError, PURCHASES_URL, collect_tracks, download_tracks,
    open_purchased_page, validate_download_delay, validate_scan_timing,
)


BROWSERS = {
    "edge": ("chromium", "msedge"),
    "chrome": ("chromium", "chrome"),
    "chromium": ("chromium", None),
    "firefox": ("firefox", None),
    "webkit": ("webkit", None),
}


def window_position_path():
    local = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    return Path(local) / "AmazonPurchasedMusicDownloader" / "window-position.json"


def read_window_position(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict) or any(
        type(data.get(axis)) is not int or not -(2**31) <= data[axis] <= 2**31 - 101
        for axis in ("x", "y")
    ):
        raise ValueError("Window position must contain valid integer x and y coordinates.")
    return data["x"], data["y"]


def write_window_position(path, position):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".window-position-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(dict(zip(("x", "y"), position)), file)
            file.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def window_position_visible(position):
    x, y = position
    title_bar = wintypes.RECT(x, y, x + 100, y + 30)
    monitor_from_rect = ctypes.windll.user32.MonitorFromRect
    monitor_from_rect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
    monitor_from_rect.restype = wintypes.HANDLE
    return bool(monitor_from_rect(ctypes.byref(title_bar), 0))


class App(tk.Tk):
    def __init__(self, browser="edge"):
        if browser not in BROWSERS:
            raise ValueError(f"Unsupported browser: {browser}")
        super().__init__()
        self.browser_choice = browser
        self.title("Purchased Music Downloader")
        self.geometry("800x560")
        self.minsize(740, 450)
        self.messages = queue.Queue()
        self.commands = queue.Queue()
        self.cancel = threading.Event()
        self.running = False
        self.closing = False
        self.session_closed = False
        self.browser_open = False
        self.scanned_count = None
        self.position_file = window_position_path()
        self.window_position = None

        default_font = font.nametofont("TkDefaultFont")
        default_font.configure(family="Segoe UI", size=12)

        font.nametofont("TkTextFont").configure(family="Segoe UI", size=12)
        font.nametofont("TkFixedFont").configure(family="Consolas", size=12)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Amazon Purchased Music", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text=f"Selected browser: {browser}. Your sign-in is kept only until this app closes.",
            wraplength=720,
        ).pack(anchor="w", pady=(6, 12))

        folder_row = ttk.Frame(frame)
        folder_row.pack(fill="x")
        ttk.Label(folder_row, text="Save to:").pack(side="left")
        self.folder = tk.StringVar(value=str(Path.home() / "Music" / "Amazon Purchases"))
        ttk.Entry(folder_row, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(folder_row, text="Browse...", command=self.browse).pack(side="left")

        count_row = ttk.Frame(frame)
        count_row.pack(fill="x", pady=12)
        ttk.Label(count_row, text="Expected purchased tracks:").pack(side="left")
        self.expected = tk.StringVar()
        ttk.Entry(count_row, textvariable=self.expected, width=8).pack(side="left", padx=8)
        ttk.Label(count_row, text="Scan first, then check or correct the full-library count.").pack(side="left")

        timing_row = ttk.Frame(frame)
        timing_row.pack(fill="x", pady=(0, 12))
        ttk.Label(timing_row, text="Scroll wait (seconds):").pack(side="left")
        self.scroll_wait = tk.StringVar(value="1")
        ttk.Entry(timing_row, textvariable=self.scroll_wait, width=6).pack(side="left", padx=8)
        ttk.Label(timing_row, text="No-progress wait (seconds):").pack(side="left")
        self.loading_wait = tk.StringVar(value="30")
        ttk.Entry(timing_row, textvariable=self.loading_wait, width=6).pack(side="left", padx=8)

        delay_row = ttk.Frame(frame)
        delay_row.pack(fill="x", pady=(0, 12))
        ttk.Label(delay_row, text="Between downloads (seconds):").pack(side="left")
        self.download_delay = tk.StringVar(value="0.5")
        ttk.Entry(delay_row, textvariable=self.download_delay, width=6).pack(side="left", padx=8)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="Open Amazon Music", command=self.start)
        self.start_button.pack(side="left")
        self.scan_button = ttk.Button(buttons, text="Signed in: scan / rescan", command=self.scan, state="disabled")
        self.scan_button.pack(side="left", padx=8)
        self.download_button = ttk.Button(buttons, text="Download", command=self.download, state="disabled")
        self.download_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.log_box = tk.Text(frame, height=15, wrap="word", state="disabled")
        self.log_box.pack(fill="both", expand=True, pady=(14, 0))
        self.restore_window_position()
        self.bind("<Configure>", self.remember_window_position, add="+")
        self.protocol("WM_DELETE_WINDOW", self.close)
        threading.Thread(target=self.worker, daemon=True).start()
        self.after(100, self.poll)

    def restore_window_position(self):
        try:
            position = read_window_position(self.position_file)
        except (OSError, ValueError) as exc:
            self.log(f"Could not restore window position: {exc}. Using the default position.")
            return
        if position is not None:
            if window_position_visible(position):
                self.geometry(f"+{position[0]}+{position[1]}")
            else:
                self.log("The saved window position is off-screen. Using the default position.")

    def remember_window_position(self, event=None):
        if (event is None or event.widget is self) and self.state() == "normal":
            self.window_position = self.winfo_x(), self.winfo_y()

    def browse(self):
        selected = filedialog.askdirectory(parent=self, initialdir=self.folder.get())
        if selected:
            self.folder.set(selected)

    def submit(self, action, payload=None):
        self.running = True
        self.cancel.clear()
        self.update_buttons()
        self.commands.put((action, payload))

    def start(self):
        self.log(f"Opening {self.browser_choice}. Sign in, then click 'Signed in: scan / rescan'.")
        self.submit("open")

    def scan(self):
        timing = self.read_scan_timing()
        if timing is not None:
            self.submit("scan", timing)

    def read_scan_timing(self):
        try:
            timing = (float(self.scroll_wait.get()), float(self.loading_wait.get()))
            validate_scan_timing(*timing)
            return timing
        except (ValueError, DownloadError) as exc:
            messagebox.showerror("Loading waits", f"Invalid loading waits: {exc}")
            return None

    def download(self):
        try:
            count = int(self.expected.get())
            if count <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Track count", "Enter the full number of purchased tracks (at least 1).")
            return
        if self.scanned_count is None:
            messagebox.showerror("Scan required", "Scan Purchased in the current browser session before downloading.")
            return
        if count != self.scanned_count:
            messagebox.showerror(
                "Track count",
                f"The cached scan contains {self.scanned_count} tracks, but you entered {count}. "
                "Check the full-library count and rescan if tracks are missing.",
            )
            return
        timing = self.read_scan_timing()
        if timing is None:
            return
        try:
            delay = float(self.download_delay.get())
            validate_download_delay(delay)
        except (ValueError, DownloadError) as exc:
            messagebox.showerror("Download pause", f"Invalid download pause: {exc}")
            return
        if not self.folder.get().strip():
            messagebox.showerror("Save folder", "Choose a save folder.")
            return
        directory = Path(self.folder.get()).expanduser()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Save folder", str(exc))
            return
        self.submit("download", (directory, count, *timing, delay))

    def update_buttons(self):
        idle = not self.running and not self.closing
        self.start_button.config(state="normal" if idle else "disabled")
        self.scan_button.config(state="normal" if idle and self.browser_open else "disabled")
        self.download_button.config(
            state="normal" if idle and self.browser_open and self.scanned_count is not None else "disabled",
        )
        self.stop_button.config(state="normal" if self.running and not self.closing else "disabled")

    def stop(self):
        self.cancel.set()
        self.stop_button.config(state="disabled")
        self.log("Stopping after the current browser operation; the browser will stay open.")

    def close(self):
        if self.closing:
            return
        self.remember_window_position()
        if self.window_position is not None:
            try:
                write_window_position(self.position_file, self.window_position)
            except OSError as exc:
                self.log(f"Could not save window position: {exc}")
                messagebox.showerror("Window position", f"Could not save window position: {exc}", parent=self)
        if self.session_closed:
            self.destroy()
            return
        self.closing = True
        self.cancel.set()
        self.update_buttons()
        self.log("Closing the browser session after the current operation finishes...")
        self.commands.put(("close", None))

    def worker(self):
        def log(message):
            self.messages.put(("log", message))

        def invalidate_scan():
            nonlocal cached_tracks
            cached_tracks = None
            self.messages.put(("scan-invalidated", None))

        browser = context = page = None
        cached_tracks = None
        try:
            with sync_playwright() as playwright:
                try:
                    while True:
                        action, payload = self.commands.get()
                        if action == "close":
                            break
                        try:
                            if browser is None or not browser.is_connected():
                                invalidate_scan()
                                engine, channel = BROWSERS[self.browser_choice]
                                browser = getattr(playwright, engine).launch(channel=channel, headless=False)
                                context = page = None
                            if context is None:
                                invalidate_scan()
                                context = browser.new_context(accept_downloads=True)
                            if page is None or page.is_closed():
                                invalidate_scan()
                                page = context.new_page()
                                page.goto(PURCHASES_URL, wait_until="domcontentloaded", timeout=60000)
                            self.messages.put(("browser", True))
                            if action == "open":
                                page.bring_to_front()
                            else:
                                if action == "scan":
                                    invalidate_scan()
                                open_purchased_page(page)
                                if action == "scan":
                                    scroll_wait, loading_wait = payload
                                    log(f"Scanning Purchased: {scroll_wait:g}s between scrolls, "
                                        f"{loading_wait:g}s without new tracks before stopping.")
                                    cached_tracks = collect_tracks(
                                        page, None, self.cancel, log,
                                        scroll_wait=scroll_wait, loading_wait=loading_wait,
                                    )
                                    self.messages.put(("scanned", len(cached_tracks)))
                                    log(f"Scan found {len(cached_tracks)} tracks. Check or adjust the count, then click Download. "
                                        "A paused or partial list may undercount your purchases.")
                                elif action == "download":
                                    directory, count, scroll_wait, loading_wait, download_delay = payload
                                    if cached_tracks is None:
                                        raise DownloadError(
                                            "Scan Purchased in the current browser session before downloading."
                                        )
                                    if count != len(cached_tracks):
                                        raise DownloadError(
                                            f"The cached scan contains {len(cached_tracks)} tracks, but you entered {count}. "
                                            "Check the full-library count and rescan if tracks are missing."
                                        )
                                    log(f"Using cached scan of {len(cached_tracks)} tracks; no full rescan needed.")
                                    log(f"Pause between downloads: {download_delay:g} seconds.")
                                    completed = download_tracks(
                                        page, directory, cached_tracks, self.cancel, log,
                                        scroll_wait=scroll_wait, loading_wait=loading_wait,
                                        download_delay=download_delay,
                                    )
                                    log(f"Finished: {completed} tracks downloaded or previously recorded.")
                        except (DownloadError, Cancelled, PlaywrightError, OSError) as exc:
                            log(f"Stopped: {exc}")
                        except Exception as exc:
                            log(f"Stopped because of an unexpected error ({type(exc).__name__}): {exc}")
                        finally:
                            connected = browser is not None and browser.is_connected()
                            if not connected or page is None or page.is_closed():
                                invalidate_scan()
                            self.messages.put(("browser", connected))
                            self.messages.put(("done", None))
                finally:
                    if browser is not None and browser.is_connected():
                        browser.close()
        except Exception as exc:
            log(f"Browser session failed ({type(exc).__name__}): {exc}. Close and restart the app.")
            self.messages.put(("failed", None))
        finally:
            self.messages.put(("closed", None))

    def log(self, text):
        self.log_box.config(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def poll(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "log":
                    self.log(value)
                elif kind == "browser":
                    self.browser_open = value
                    if not value:
                        self.scanned_count = None
                elif kind == "scan-invalidated":
                    self.scanned_count = None
                elif kind == "scanned":
                    self.expected.set(str(value))
                    self.scanned_count = value
                elif kind == "done":
                    self.running = False
                    self.update_buttons()
                elif kind == "failed":
                    self.running = True
                    self.update_buttons()
                    self.stop_button.config(state="disabled")
                elif kind == "closed":
                    self.session_closed = True
                    if self.closing:
                        self.destroy()
                        return
        except queue.Empty:
            pass
        self.after(100, self.poll)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download purchased MP3s from Amazon Music.")
    parser.add_argument(
        "--browser", choices=BROWSERS, default="edge",
        help="Browser to use (default: edge). Chrome and Edge must be installed; other choices need Playwright browser binaries.",
    )
    args = parser.parse_args(argv)
    App(browser=args.browser).mainloop()


if __name__ == "__main__":
    main()
