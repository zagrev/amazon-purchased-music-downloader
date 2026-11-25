# Purchased Music Downloader

A Windows app that uses Amazon Music's Purchased page and its normal Download action to save purchased MP3s. It does not download subscription-only music or bypass DRM. Amazon may change the site, so this automation can stop working.

## Setup

Install Python with Tkinter and Microsoft Edge, then run in PowerShell from this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

### Browser selection

Edge is the default. Use `--browser` to select `edge`, `chrome`, `chromium`, `firefox`, or `webkit`:

```powershell
.\.venv\Scripts\python.exe app.py --browser chrome
.\.venv\Scripts\python.exe app.py --help
```

Edge and Chrome use the installed browser. Chromium, Firefox, and WebKit use Playwright-managed binaries, which must be installed first for the chosen browser. For example:

```powershell
.\.venv\Scripts\python.exe -m playwright install firefox
.\.venv\Scripts\python.exe app.py --browser firefox
```

Use `chromium` or `webkit` instead of `firefox` in both commands for those browsers. Playwright's Firefox and WebKit builds are required; an installed Firefox or Safari is not used. If the selected browser is missing, the app reports the launch error rather than switching browsers.

## Use

1. Choose a destination.
2. Click **Open Amazon Music**, then sign in in the separate window of your selected browser. Complete any sign-in challenges yourself.
3. Set **Scroll wait (seconds)** and **No-progress wait (seconds)** if needed, then click **Signed in: scan / rescan**. The app opens Purchased and scrolls to load tracks, waiting between scrolls (default: 1 second). It stops after no new tracks appear for the no-progress wait (default: 30 seconds), resetting that timer whenever more tracks load. The detected count fills **Expected purchased tracks**; this is an estimate, not proof the entire library loaded.
4. Review or correct the count to the **exact full-library total**, then click **Download**. Downloads start only when the loaded count matches. A smaller count does not select a subset of songs. If loading stalls or the count is wrong, correct it or rescan and retry in the same signed-in browser.
5. The browser stays open after completion, errors, or **Stop**, so you can adjust the count and run again without signing in repeatedly. Stop takes effect after the current browser operation. Closing the app closes its browser session.

### Incomplete scans

Amazon loads purchases in batches while scrolling. The old scan stopped after just five seconds without growth, which could mistake a slow batch for the end of the library. If a scan undercounts, try a **Scroll wait** of `3` seconds and a **No-progress wait** of `60` seconds, then rescan in the same signed-in browser. Both waits must be finite positive numbers, and the no-progress wait must be at least as long as the scroll wait. Changes apply to the next scan or download, not an operation already running.

These settings also apply when loading the list before downloading. If you know the complete Purchased list contains 789 tracks, confirm **Expected purchased tracks** as `789` before clicking **Download**; the app will refuse to download a smaller loaded list. Longer waits cannot guarantee completeness if Amazon does not expose all rows or the automated scrolling fails to trigger loading. The count represents track entries on Purchased, not a count of local files.

Files are saved with the Amazon track ID prefixed to Amazon's filename. Existing files are never overwritten. The destination contains `.amazon-music-downloads.json`, a local record used to skip previously downloaded files; missing or empty files are downloaded again. Do not edit the history or move recorded files without understanding that they may be downloaded again. If a download or history write fails, the app stops and reports the error.

Sign-in happens only in the separate browser window; the app does not ask for or save your password. Sign-in cookies and browser storage are retained in an isolated, non-persistent browser context for the current app session only, not saved as a reusable profile. Keep that window open to reuse sign-in; closing the browser or an expired Amazon session can require signing in again. Its track-count check relies on the count you confirm and cannot independently prove Amazon displayed every purchase. Downloads and available formats depend on Amazon and your account.
