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
    import traceback
    traceback.print_exception(exc)
    try:
        with open("/error.txt", "w") as f:
            traceback.print_exception(exc, file=f)
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
