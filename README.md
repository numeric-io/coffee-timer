# coffee-timer

Freshness timer for two office coffee carafes on an Adafruit MagTag
(ESP32-S2, 2.9" e-ink, CircuitPython 10.x).

- **Buttons:** the left pair is regular / decaf for the left carafe, the
  right pair is regular / decaf for the right carafe.
- **Screen:** each half shows the type, when it was brewed and minutes
  left (in 2-minute steps), and turns STALE after 120 minutes.
- **Slack:** each brew can post to a channel (`SLACK_WEBHOOK_URL`).
- **Battery:** the board deep-sleeps between events, waking every minute
  only while a pot is fresh.
- **Updates:** on each brew the board checks this repo and installs a new
  `app.py` from `main`. A version that crashes is rolled back on its own.
  **Pushing to `main` deploys to the coffee machine.**

## Files

| File | Purpose |
|---|---|
| `app.py` | The timer. The only file the board updates from GitHub. |
| `code.py` | Loader: runs `app.py` and rolls back a crashing update. Installed by hand. |
| `updater.py` | Checks GitHub on each brew, then downloads, validates and installs `app.py`. Installed by hand. |
| `boot.py` | Makes the drive writable by the board, so it can update itself. Hold the leftmost button while pressing RESET to make it writable from a computer instead. Installed by hand. |
| `settings.example.toml` | Template for the board's `settings.toml` (Wi-Fi, Slack). The real file holds secrets and is gitignored. |
| `wifi_test.py` | Wi-Fi / DNS / NTP diagnostic: at the serial REPL, `import wifi_test`. |
| `backup/app.py` | Spare copy of `app.py`. `code.py` restores it if `app.py` ever goes missing; the updater refreshes it when an update is kept. |

## Setup

1. Install CircuitPython 10.x and the matching library bundle's
   `adafruit_display_text`, `adafruit_display_shapes`,
   `adafruit_ntp`, `adafruit_requests` and `adafruit_connection_manager`
   into `/lib` (plus their dependencies).
2. Copy `app.py`, `code.py`, `updater.py` and `boot.py` to CIRCUITPY, plus
   a copy of `app.py` as `backup/app.py`.
3. Copy `settings.example.toml` to CIRCUITPY as `settings.toml` and fill it in.
4. Press RESET.

## How updates work

1. On each brew, after the screen and the Slack post, the board asks
   GitHub for the latest commit on `GITHUB_BRANCH`. If it's new, it
   downloads `app.py`, checks it's valid Python that defines `main()`,
   keeps the old file as `app.py.bak` and swaps in the new one.
2. The next 3 boots are a trial. If the new `app.py` crashes during the
   trial, `code.py` restores `app.py.bak`, reloads, and won't reinstall
   that commit.
3. Timers keep running across an update because they live in sleep
   memory. The exception is a change to the saved-state layout
   (`MEM_FMT`/`MEM_MAGIC` in `app.py`), which clears them once.
4. Only `app.py` updates. Changes to `code.py`, `updater.py`, `boot.py`
   or `/lib` must be copied by hand, in computer-edit mode.
5. Update state is in `update.json` on the drive, and messages go to the
   serial console (`update: ...`).

## Keeping the drive healthy

The board's flash is erased and rewritten in 4 KB chunks, and the drive's
file list lives in one of them. A RESET or power cut in the middle of a
write can wipe that chunk, and every file listed in it disappears at once
(this happened three times while the board logged every wake).

- The board writes nothing in normal use: `WAKE_LOG` is off by default,
  and `update.json` changes only when an update is installed.
- When Wi-Fi fails, the board backs off for an hour (`WIFI_BACKOFF`):
  each brew makes just one quick try (`WIFI_QUICK_TRY`, 5 s) on the
  network that last worked, instead of every network at 8 s apiece. A
  try that succeeds ends the backoff at once, and RESET clears it too, so
  the board never looks frozen and recovers as soon as it's back in range.
- **After copying files from a computer, eject the drive and wait for it
  to disappear before pressing RESET.** macOS can finish writing the file
  list several seconds after a copy appears to be done.
- If the screen says **COFFEE TIMER CAN'T START**, `code.py` couldn't load
  `app.py` and had no spare to restore. Plug the board into a computer.

## Hardware notes

- Silkscreen buttons A-D are `board.BUTTON_A`-`D`. They are not mirrored.
- CircuitPython can't write to CIRCUITPY unless `boot.py` remounts it, so
  timer state lives in `alarm.sleep_memory`.
- CircuitPython's `time` module has no `gmtime`. The RTC is kept in UTC and
  `localtime()` is used in its place.
