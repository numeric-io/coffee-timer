# coffee-timer

Freshness timer for two office coffee carafes on an Adafruit MagTag
(ESP32-S2, 2.9" e-ink, CircuitPython 10.x).

- **Buttons:** the left pair is regular / decaf for the left carafe, the
  right pair is regular / decaf for the right carafe.
- **Screen:** each half shows the type, when it was brewed and minutes
  left, and turns STALE after 60 minutes.
- **Slack:** each brew can post to a channel (`SLACK_WEBHOOK_URL`).
- **Battery:** the board deep-sleeps between events, waking every minute
  only while a pot is fresh.

## Files

| File | Purpose |
|---|---|
| `code.py` | The timer. Copy to the CIRCUITPY drive. |
| `settings.example.toml` | Template for the board's `settings.toml` (Wi-Fi, Slack). The real file holds secrets and is gitignored. |
| `wifi_test.py` | Wi-Fi / DNS / NTP diagnostic: at the serial REPL, `import wifi_test`. |

## Setup

1. Install CircuitPython 10.x and the matching library bundle's
   `adafruit_magtag`, `adafruit_display_text`, `adafruit_display_shapes`,
   `adafruit_ntp`, `adafruit_requests` and `adafruit_connection_manager`
   into `/lib` (plus their dependencies).
2. Copy `code.py` to CIRCUITPY.
3. Copy `settings.example.toml` to CIRCUITPY as `settings.toml` and fill it in.
4. Press RESET.

## Hardware notes

- Silkscreen buttons A-D are `board.BUTTON_A`-`D`. They are not mirrored.
- CircuitPython can't write to CIRCUITPY unless `boot.py` remounts it, so
  timer state lives in `alarm.sleep_memory`.
- CircuitPython's `time` module has no `gmtime`. The RTC is kept in UTC and
  `localtime()` is used in its place.
