# Purchased Music Downloader

A Windows app that uses Amazon Music's Purchased page and its normal Download action to save purchased MP3s. It does not download subscription-only music or bypass DRM. Amazon may change the site, so this automation can stop working.

## Setup

Install Python with Tkinter and Microsoft Edge, then run in PowerShell from this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

## Use

1. Choose a destination.
2. Click **Open Amazon Music**, then sign in in the separate Edge window. Complete any sign-in challenges yourself.
3. Click **Signed in: scan / rescan**. The app opens Purchased and scrolls to load tracks until the count stops growing for five seconds. The detected count fills **Expected purchased tracks**; this is an estimate, not proof the entire library loaded.
4. Review or correct the count to the **exact full-library total**, then click **Download**. Downloads start only when the loaded count matches. A smaller count does not select a subset of songs. If loading stalls or the count is wrong, correct it or rescan and retry in the same signed-in browser.
5. Edge stays open after completion, errors, or **Stop**, so you can adjust the count and run again without signing in repeatedly. Stop takes effect after the current browser operation. Closing the app closes its Edge session.

Files are saved with the Amazon track ID prefixed to Amazon's filename. Existing files are never overwritten. The destination contains `.amazon-music-downloads.json`, a local record used to skip previously downloaded files; missing or empty files are downloaded again. Do not edit the history or move recorded files without understanding that they may be downloaded again. If a download or history write fails, the app stops and reports the error.

Sign-in happens only in the separate browser window; the app does not ask for or save your password. Sign-in cookies and browser storage are retained in an isolated, non-persistent browser context for the current app session only, not saved as a reusable profile. Keep that window open to reuse sign-in; closing Edge or an expired Amazon session can require signing in again. Its track-count check relies on the count you confirm and cannot independently prove Amazon displayed every purchase. Downloads and available formats depend on Amazon and your account.
