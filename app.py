import argparse
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from downloader import Cancelled, DownloadError, PURCHASES_URL, collect_tracks, download_tracks, open_purchased_page


BROWSERS = {
    "edge": ("chromium", "msedge"),
    "chrome": ("chromium", "chrome"),
    "chromium": ("chromium", None),
    "firefox": ("firefox", None),
    "webkit": ("webkit", None),
}


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
        self.scanned = False

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
        self.protocol("WM_DELETE_WINDOW", self.close)
        threading.Thread(target=self.worker, daemon=True).start()
        self.after(100, self.poll)

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
        self.submit("scan")

    def download(self):
        try:
            count = int(self.expected.get())
            if count <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Track count", "Enter the full number of purchased tracks (at least 1).")
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
        self.submit("download", (directory, count))

    def update_buttons(self):
        idle = not self.running and not self.closing
        self.start_button.config(state="normal" if idle else "disabled")
        self.scan_button.config(state="normal" if idle and self.browser_open else "disabled")
        self.download_button.config(state="normal" if idle and self.browser_open and self.scanned else "disabled")
        self.stop_button.config(state="normal" if self.running and not self.closing else "disabled")

    def stop(self):
        self.cancel.set()
        self.stop_button.config(state="disabled")
        self.log("Stopping after the current browser operation; the browser will stay open.")

    def close(self):
        if self.closing:
            return
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

        browser = context = page = None
        try:
            with sync_playwright() as playwright:
                try:
                    while True:
                        action, payload = self.commands.get()
                        if action == "close":
                            break
                        try:
                            if browser is None or not browser.is_connected():
                                engine, channel = BROWSERS[self.browser_choice]
                                browser = getattr(playwright, engine).launch(channel=channel, headless=False)
                                context = page = None
                            if context is None:
                                context = browser.new_context(accept_downloads=True)
                            if page is None or page.is_closed():
                                page = context.new_page()
                                page.goto(PURCHASES_URL, wait_until="domcontentloaded", timeout=60000)
                            self.messages.put(("browser", True))
                            if action == "open":
                                page.bring_to_front()
                            else:
                                open_purchased_page(page)
                                if action == "scan":
                                    log("Scanning Purchased. Waiting for the list to stop growing...")
                                    tracks = collect_tracks(page, None, self.cancel, log)
                                    self.messages.put(("scanned", len(tracks)))
                                    log(f"Scan found {len(tracks)} tracks. Check or adjust the count, then click Download. "
                                        "A paused or partial list may undercount your purchases.")
                                elif action == "download":
                                    directory, count = payload
                                    tracks = collect_tracks(page, count, self.cancel, log)
                                    completed = download_tracks(page, directory, tracks, self.cancel, log)
                                    log(f"Finished: {completed} tracks downloaded or previously recorded.")
                        except (DownloadError, Cancelled, PlaywrightError, OSError) as exc:
                            log(f"Stopped: {exc}")
                        except Exception as exc:
                            log(f"Stopped because of an unexpected error ({type(exc).__name__}): {exc}")
                        finally:
                            self.messages.put(("browser", browser is not None and browser.is_connected()))
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
                elif kind == "scanned":
                    self.expected.set(str(value))
                    self.scanned = True
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
