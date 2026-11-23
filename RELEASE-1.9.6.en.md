# Jiliu 1.9.6 — Download button moved out of the player, plus a speed you can actually see

> The previous release was about "does it work". This one is about "is it pleasant to use".
> Two browser-side UI changes and one lightweight endpoint; core behaviour is unchanged.

---

## 1 Download button: from inside the video to outside

The button used to sit in the **top-right corner inside** the video. Bilibili, YouTube and
friends put "settings / subtitles / fullscreen / picture-in-picture" in exactly that row, so
the button covered them — every click needed a detour.

It now follows IDM's placement logic and sits **outside the video frame**:

| Order | Candidate position | When it applies |
|---|---|---|
| 1 | Just above the video, right-aligned | There is room above — Bilibili-style pages |
| 2 | Right of the video | Video pinned to the top, no room above |
| 3 | Below the video | Pinned to the top and no room on the right |
| 4 | Left of the video | Video is right-aligned |
| 5 | Inside, top-right, **icon only** | Video fills the viewport — last resort |

Every candidate is clamped to the viewport, so the button never slides off screen.

### Draggable, and it remembers

- Press and drag the button, drop it where you want it.
- The position is stored relative to the **video's top-left corner** (`{dx, dy}` in
  `chrome.storage.local`), so it follows the video when you scroll, zoom, or change
  resolution — it does not drift.
- Once you have dragged it, the default rules above no longer apply: your placement wins.
- Reset: extension popup → **Page widgets** → **Reset widget positions**.

### Details that no longer misbehave

- Hovering the button no longer gets it yanked away by the auto-hide timer (it used to
  vanish from under the cursor after 1.6 s).
- The `click` that ends a drag is swallowed, so you never create a task by accident.
- The button's size is measured once and cached; `mousemove` no longer forces a layout.
- The container lives on a `pointer-events:none` full-screen layer — page clicks are never
  intercepted.

## 2 Progress overlay: speed / current file / total, rotating

While there are unfinished tasks, a 212 px card appears in the bottom-right corner and
switches view every 4 seconds:

| View | Main number | Sub line | Bar |
|---|---|---|---|
| Total speed | `3.7 MB/s` | `2 downloading · 1 queued` | overall % |
| Current file | `63.4%` | filename + `2.5 MB/s · 02:41 left` | that file's % |
| Total progress | `38.9%` | `716 MB / 1.8 GB` | overall % |

**With several files downloading at once, "Current file" rotates between them** and the
caption shows `Current file 1/3`. Otherwise you would only ever see the first task and have
to go back to the main panel to check the others.

Interaction:

- **Drag the title bar** to move it (saved in viewport coordinates; resizing the window only
  pulls it back on screen if it went out of bounds).
- **Click the body** to switch view manually; auto-rotation pauses for 8 seconds.
- Hovering pauses rotation.
- `—` collapses it into a slim bar (speed only); `↗` brings the Jiliu window to the front.
- When everything finishes it does not just vanish: it shows **"✓ All done"** for 5 seconds,
  then hides.
- It hides itself when there is no work or when Jiliu is not running — it never squats on
  your screen.

### One instance per top-level page

The content script is injected into `all_frames`. If every iframe grew its own card, a
single page could sprout seven or eight of them — so the overlay only starts when
`window.top === window.self` and the page is not the local panel (`127.0.0.1` /
`localhost`). The download button is the opposite: it must exist in every frame, because on
some sites the player lives entirely inside an iframe.

### Shadow DOM isolation

Page CSS is a zoo (`div{...}`, `*{box-sizing}`, player resets). Without isolation you end up
playing whack-a-mole with `!important`. Both the button and the card live in an
`attachShadow({mode:'open'})` root with `:host{all:initial}`, so page styles cannot get in
and ours cannot leak out.

## 3 New endpoint: `GET /api/progress`

The overlay needs a speed reading every second, while `/api/state` also assembles tool
probing, licensing, theme and i18n — a dozen blocks that do not change from second to
second.

```json
{
  "ok": true, "version": "1.9.6",
  "counts": {"running": 2, "queued": 1},
  "total": 3, "active": 2, "queued": 1, "paused": 0,
  "speed": 3879731, "speed_text": "3.7 MB/s",
  "done_bytes": 751619276, "size_bytes": 1932735283,
  "done_text": "716 MB", "size_text": "1.8 GB", "percent": 38.9,
  "current": {"filename": "...", "percent": 63.4, "speed_text": "2.5 MB/s",
              "eta_text": "02:41", "done_text": "486 MB", "size_text": "766 MB"},
  "tasks": [ { "id", "filename", "status", "percent", "speed", "speed_text",
               "eta_text", "done_text", "size_text" } — max 8 rows ]
}
```

Two rules:

- `percent` counts **unfinished tasks only** (running / queued / paused / interrupted).
  Including completed history would pin the number at 99.x% forever and look stuck.
- Each task's `done` is clamped with `min(done, size)` before summing — during segment
  retries `done` can briefly exceed `size`, and without clamping the total would read 103%.

### Five tabs do not mean five times the requests

With five pages open polling every 1.5 s you would hit 3.3 req/s. `background.js` caches the
result for 900 ms and merges concurrent calls, so the server sees roughly 1 req/s.

### Works with older servers too

The extension tries `/api/progress` first and falls back to `/api/state`, distilling the same
shape client-side. If you have not upgraded the main program, the overlay still works — it
just transfers a bit more data.

## 4 Toggles

The extension popup gains a **Page widgets** section:

- **Video overlay button** — on by default
- **Progress overlay** — on by default
- **Reset widget positions**

Settings are written to `chrome.storage.local`; the content script listens on `onChanged`,
so changes apply immediately without reloading the page.

---

## Verification

| Item | Result |
|---|---|
| Full regression | **1281 / 1281 passed** (1274 from 1.9.5 + 7 new endpoint assertions) |
| `/api/progress` contract | 7 new assertions in `tests/test_api_features.py`: all fields present, `active`/`queued` match `counts`, percentage in range, `current` carries enough detail |
| Real download aggregation | 24 MB local file throttled to 2 MB/s, sampled every second: `speed 1.22→1.77 MB/s`, `percent 10.2→51.0`, `done 2.44→12.23 MB` — consistent with the task list |
| Button placement | Headless browser screenshots across three layouts: wide video → above; video pinned to top → right; video full-bleed → inside, icon only |
| Overlay rendering | Same screenshots: view caption, `Current file 1/2`, percentage, filename, speed/ETA, progress bar all correct |

## Not in this release

- **Per-site memory** for button/card positions (currently one global position). Per-host
  storage is arguably more correct, but it also means "the habit I built on site A does not
  apply on site B". Keeping one for now.
- **Expandable task list** in the overlay. Currently three numeric views only; for details,
  go back to the main panel.
- The same overlay **inside the main window** (that is a WebView page and already has a top
  status bar).

## Install

**Portable (recommended)** — download `jiliu-portable-1.9.6.zip`, extract anywhere, run
`jiliu.exe`. Nothing is written outside the program folder.

**Single executable** — download `jiliu-1.9.6.exe` and run it. Requires Microsoft Edge
WebView2 Runtime (preinstalled on Windows 10/11).

**Browser extension** — download `jiliu-extension-1.9.6.zip`, extract, then open
`chrome://extensions` (or `edge://extensions`), enable Developer mode, choose **Load
unpacked**, and select the extracted `extension` folder. The popup header should read
`v1.9.6`.

> Verified on Windows 11, Python 3.13, Chrome/Edge 130+. Non-Windows platforms can run from
> source: `python jiliu.py serve`.
