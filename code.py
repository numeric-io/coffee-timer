# SPDX-License-Identifier: MIT
"""Loader: runs the coffee timer (app.py) and rolls back bad updates.

CircuitPython runs this file at every boot and every wake from deep
sleep. It stays tiny and is installed by hand -- never auto-updated -- so
it can always recover from a broken app.py pushed to GitHub.
"""

import supervisor

supervisor.runtime.autoreload = False  # one run per reset, no reload loops

try:
    import updater
except ImportError:  # updates not installed: just run the timer
    updater = None

if updater:
    updater.begin_boot()

try:
    import app  # runs the timer; normally ends in deep sleep
except Exception as exc:  # noqa: BLE001 -- a crash while loading app.py
    if updater and updater.rollback("crashed while loading: %r" % (exc,)):
        supervisor.reload()  # run the restored app.py
    import time
    import traceback
    report = "".join(traceback.format_exception(exc))
    try:
        # Only rewrite error.txt when the error changes: every flash write
        # risks the drive's file list if RESET is pressed mid-write.
        try:
            with open("/error.txt") as f:
                same = f.read() == report
        except OSError:
            same = False
        if not same:
            with open("/error.txt", "w") as f:
                f.write(report)
    except Exception:
        pass
    # Put the reason on the e-ink screen too: otherwise a board that can't
    # start just sleeps again, the screen never changes, and even RESET
    # seems dead. Uses CircuitPython's built-in text console, so it works
    # even if /lib is damaged. The console shows its last ~10 lines, so the
    # traceback goes first and the plain-English summary last.
    print(report)
    missing = isinstance(exc, ImportError) and "app" in str(exc)
    print("COFFEE TIMER CAN'T START")
    print("app.py is missing from the board." if missing
          else "app.py crashed while loading (see above).")
    print("Plug into a computer to repair it.")
    try:
        import board
        import displayio
        board.DISPLAY.root_group = displayio.CIRCUITPYTHON_TERMINAL
        for _ in range(10):  # e-ink may say "refresh too soon" right after boot
            try:
                board.DISPLAY.refresh()
                break
            except RuntimeError:
                time.sleep(1)
    except Exception:
        pass
    # Don't sit awake draining the battery: sleep until a button press
    # retries.
    import alarm
    import board
    alarm.exit_and_deep_sleep_until_alarms(
        *[alarm.pin.PinAlarm(pin=p, value=False, pull=True)
          for p in (board.BUTTON_A, board.BUTTON_B,
                    board.BUTTON_C, board.BUTTON_D)])
