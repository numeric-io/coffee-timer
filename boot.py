# SPDX-License-Identifier: MIT
"""Record the wake reason, then choose who may write the CIRCUITPY drive.

Wake reason: CircuitPython resets the alarm system after boot.py runs,
which can lose which button woke the board by the time code.py asks
(buttons then seem dead). So boot.py notes it first -- before touching any
pin -- in the last byte of sleep_memory, and app.py reads it from there:
  0 = no alarm (reset/power-on), 1-4 = BUTTON_A-D, 5 = some other pin,
  10 = the minute timer, 0xFF = already read by app.py.

Drive: CircuitPython lets either the computer (over USB) or the code write
CIRCUITPY, never both. The timer writes it to install updates from
GitHub, so by default the code gets it and a connected computer sees the
drive read-only. To edit files from a computer: hold the leftmost button
(L REG) while pressing RESET, and keep holding for a second. The drive is
then writable from the computer until the next reset, and updates pause.

Installed by hand; never auto-updated.
"""

import alarm
import board

wake = alarm.wake_alarm
reason = 0
if isinstance(wake, alarm.pin.PinAlarm):
    reason = 5
    for i, pin in enumerate((board.BUTTON_A, board.BUTTON_B,
                             board.BUTTON_C, board.BUTTON_D)):
        if wake.pin == pin:
            reason = i + 1
            break
elif isinstance(wake, alarm.time.TimeAlarm):
    reason = 10
alarm.sleep_memory[len(alarm.sleep_memory) - 1] = reason

import digitalio  # noqa: E402 -- only after the wake reason is saved
import storage  # noqa: E402

button = digitalio.DigitalInOut(board.BUTTON_A)
button.switch_to_input(pull=digitalio.Pull.UP)
computer_edit = not button.value  # buttons read LOW while pressed
button.deinit()

if not computer_edit:
    storage.remount("/", readonly=False)
# No flash writes here: the first write of a wake costs ~0.3 s, and this
# runs before the screen refresh. app.py logs the reset and wake reason
# after the refresh instead.
