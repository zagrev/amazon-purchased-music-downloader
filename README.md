# Purchased Music Downloader

A Windows app that uses Amazon Music's Purchased page and its normal Download action to save purchased MP3s. It does not download subscription-only music or bypass DRM. Amazon may change the site, so this automation can stop working.

## Setup

### Download the Windows release

Download the latest `AmazonPurchasedMusicDownloader-vX.Y.Z-windows-x64.zip` from [GitHub Releases](https://github.com/zagrev/amazon-purchased-music-downloader/releases). Extract the entire ZIP and run `AmazonPurchasedMusicDownloader.exe` inside its folder. Keep the bundled `_internal` folder next to the executable. Python is not required; Microsoft Edge must be installed for the default browser. Chrome can also be selected from PowerShell:

```powershell
.\AmazonPurchasedMusicDownloader.exe --browser chrome
```

The release includes `VERSION.txt`. It is an unsigned Windows x64 application, so Windows may show a SmartScreen warning. Browser binaries are not bundled: use installed Edge/Chrome with the portable release, or follow the source setup below for Playwright-managed Chromium/Firefox/WebKit.

### Run from source

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
3. Set **Scroll wait (seconds)** and **No-progress wait (seconds)** if needed, then click **Signed in: scan / rescan**. The app starts at the top of Purchased and scrolls in overlapping viewport-sized steps, waiting between scrolls (default: 1 second). It remembers unique track IDs even when Amazon removes off-screen rows. "Loaded N tracks" reports the cumulative unique count, not the number of HTML rows currently rendered. The scan stops at the bottom after no new IDs appear for the no-progress wait (default: 30 seconds); discovering tracks or advancing the scroll resets that timer. The detected count fills **Expected purchased tracks**; this is an estimate, not proof the entire library loaded.
4. Review or correct the count to the **exact full-library total**, then click **Download**. The app keeps scanned IDs and titles in memory for the current browser page/session and downloads directly from that list without collecting the whole library again. Downloads start only when your confirmed count matches the cached scan. A smaller count does not select a subset of songs. If tracks are missing, adjust the loading waits and rescan; a count mismatch reports an error rather than triggering an automatic rescan.
5. The browser stays open after completion, errors, or **Stop**, so you can adjust the count and run again without signing in repeatedly. Stop takes effect after the current browser operation. Closing the app closes its browser session.

The app remembers its last normal window position when you close it and starts there next time. Position is stored in `%LOCALAPPDATA%\AmazonPurchasedMusicDownloader\window-position.json`, separately from credentials and music. Window size and maximized/minimized state are not saved. Positions on secondary monitors are supported; if the saved title-bar position is no longer on a connected monitor, the app uses its default placement and logs a notice. Invalid settings or read/write errors are reported. Delete this settings file to reset the saved position.

Set **Between downloads (seconds)** before clicking **Download** to adjust the pace of menu selections. The default is `0.5` seconds; try `3` or `5` for a slower pace. The fixed pause runs after a completed download and before selecting the next track's menu. There is no pause before the first download, after the final download, or for files skipped from download history. Enter a finite number at least `0`; `0` disables the pause. Stop remains responsive during the pause. This is separate from scanning/scroll waits and Amazon's download-start timeout. Changes apply to the next download run, and the value is not saved between app sessions.

### Incomplete scans

Amazon loads purchases in batches and recycles off-screen rows. The scan accumulates track IDs across pages rather than assuming all purchases remain in the HTML. If a scan still undercounts because batches load slowly, try a **Scroll wait** of `3` seconds and a **No-progress wait** of `60` seconds, then rescan in the same signed-in browser. Both waits must be finite positive numbers, and the no-progress wait must be at least as long as the scroll wait. Changes apply to the next scan or download, not an operation already running. A scroll that stalls before the bottom is reported as an error, not a completed scan.

These settings apply to explicit scans and locating recycled track buttons during downloads. The app still scrolls downward and, when needed, returns to the top to find a cached track by its ID; that is not another full metadata scan. It stops with an error if that track cannot be found. If you know the complete Purchased list contains 789 tracks, confirm **Expected purchased tracks** as `789` before clicking **Download**; the app will refuse to download a smaller scanned list. Longer waits cannot guarantee completeness if Amazon does not expose all rows or the automated scrolling fails to trigger loading. The count represents track entries on Purchased, not a count of local files.

Successful scans replace the in-memory cache. Starting a rescan discards the previous list, so a failed or cancelled rescan requires another successful scan. Download errors, Stop during downloading, and completion retain the cache for retrying in the same browser session. Closing or replacing the browser/page invalidates the cache and requires a new scan. Rescan after changing Amazon accounts or purchases, even if the browser window stays open.

Progress messages use the Purchased row's dedicated title element, not the context-menu button description. If a unique title cannot be found, the app reports a page-structure error rather than guessing a name. Files are saved with the Amazon track ID prefixed to Amazon's filename. Existing files are never overwritten. The destination contains `.amazon-music-downloads.json`, a local record used to skip previously downloaded files; missing or empty files are downloaded again. Do not edit the history or move recorded files without understanding that they may be downloaded again. If a download or history write fails, the app stops and reports the error.

Sign-in happens only in the separate browser window; the app does not ask for or save your password. Sign-in cookies and browser storage are retained in an isolated, non-persistent browser context for the current app session only, not saved as a reusable profile. Keep that window open to reuse sign-in; closing the browser or an expired Amazon session can require signing in again. Its track-count check relies on the count you confirm and cannot independently prove Amazon displayed every purchase. Downloads and available formats depend on Amazon and your account.

## CI and automatic releases

GitHub Actions runs `python -m unittest discover -v` on Windows with Python 3.12 and Edge for pull requests targeting `main` and pushes to `main`. Pull requests never publish releases. After tests pass on `main`, the release job builds a portable application with PyInstaller, checks its command-line interface, bundled Playwright driver, GUI startup and shutdown, then publishes the ZIP.

The first release is `v0.1.0`. Later releases increment the highest stable `vMAJOR.MINOR.PATCH` patch number numerically, considering tags and releases (including reserved drafts). After tests pass, the workflow reserves a version by creating a tag at the exact tested commit before packaging; competing releases retry with the next available version. The version is recorded in the ZIP name and `VERSION.txt`, without making an extra version-bump commit on `main`. Different commits can release concurrently, so versions reflect reservation order rather than necessarily commit order. Runs for the same commit are serialized. A rerun reuses its reserved tag, resumes a draft, or skips an already published release. A failed build leaves a reserved tag but no published release; failed uploads leave a draft. Rerun the workflow to resume either case. GitHub determines which release is marked latest.

The workflow can also be run manually on `main` from the Actions tab. To resume an older failed commit, rerun its original workflow run rather than starting a new manual run on the current `main`. GitHub Actions must be enabled, and the release job uses the repository's `GITHUB_TOKEN` with `contents: write` to create releases; no personal access token is needed. The repository/organization must allow that permission.

To build locally on Windows using Python 3.12:

```powershell
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean --onedir --name AmazonPurchasedMusicDownloader app.py
```
