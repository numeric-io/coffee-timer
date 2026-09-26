# SPDX-License-Identifier: MIT
"""Choose who may write the CIRCUITPY drive for this boot.

CircuitPython lets either the computer (over USB) or the code write the
drive, never both. The timer needs to write it to install updates from
GitHub, so by default the code gets it and a connected computer sees the
drive read-only.

To edit files from a computer: hold the leftmost button (L REG) while
pressing RESET, and keep holding for a second. The drive is then
writable from the computer until the next reset, and updates pause.

Installed by hand; never auto-updated.
"""

import board
import digitalio
import storage

button = digitalio.DigitalInOut(board.BUTTON_A)
button.switch_to_input(pull=digitalio.Pull.UP)
computer_edit = not button.value  # buttons read LOW while pressed
button.deinit()

if not computer_edit:
    storage.remount("/", readonly=False)
