# SPDX-License-Identifier: MIT
"""Coffee freshness timer for the Adafruit MagTag (2025 edition, SSD1680).

Two UI designs, selected with DESIGN below:
  "freshness" - one pot per type. Button A brews REGULAR, button D brews DECAF.
                Each side shows a draining coffee cup: the brew level drops,
                the coffee greys out, and the steam wisps fade as it goes stale.
  "carafes"   - two independent carafe timers. A = left carafe regular,
                B = left carafe decaf, C = right carafe regular,
                D = right carafe decaf. The top line of each half shows when
                that pot was brewed ("BREWED 9:42 AM"); a legend along the
                bottom of the screen shows what each button does.

Wall-clock brew times come from NTP over Wi-Fi. Put one or more networks
in settings.toml on the CIRCUITPY drive (see settings.toml.example):
CIRCUITPY_WIFI_SSID / CIRCUITPY_WIFI_PASSWORD first, then optional
WIFI_SSID_1 / WIFI_PASSWORD_1 through WIFI_SSID_9 / WIFI_PASSWORD_9 --
each is tried in order until one connects (about 20 s total at most).
Sync happens after the screen and lights, so Wi-Fi never delays them.
Wi-Fi is only used when there's no valid clock: on first boot, or if the
RTC was lost (retried every 30 min while a pot counts down, at most 3
tries, then it gives up until reset to save battery). The RTC keeps
running through deep sleep, so it isn't re-synced after that. Pacific time
with automatic DST is used for display. Without Wi-Fi
the timer still works, but brew times show "--".

Slack: with SLACK_WEBHOOK_URL in settings.toml, each brew also posts
":coffee: Fresh pot of decaf in the right carafe (brewed 9:42 AM)" to a
channel (see SLACK_ON_BREW).

Power: the board deep-sleeps between events. A button press wakes it via
PinAlarm and records a brew (optionally pulsing the NeoPixels for 10 s --
see BREW_LIGHT) before sleeping again; a TimeAlarm
wakes it every 60 s while a pot is still fresh to tick the countdown. Once
every pot is stale (or nothing
was ever brewed) no timer is set at all: the e-ink image simply holds with
zero power until the next button press. Brew state lives in
alarm.sleep_memory so it survives sleep.

Hardware: CircuitPython 10.x or later is REQUIRED (2025 SSD1680 display).
Install the matching 10.x Adafruit library bundle (adafruit_magtag,
adafruit_display_text, adafruit_display_shapes, adafruit_ntp) into /lib,
then copy this file to the CIRCUITPY drive as app.py, next to code.py (the
loader), updater.py and boot.py. With GITHUB_REPO in settings.toml, this
file then updates itself from GitHub whenever someone brews (updater.py).
Running timers survive an update: they live in sleep_memory, not here.
"""

import math
import struct
import time

import supervisor

supervisor.runtime.autoreload = False  # debug: one run per reset, no reload loops

import alarm
import board
import displayio
import terminalio

try:
    from adafruit_display_text import label
    from adafruit_display_shapes.rect import Rect
    from adafruit_display_shapes.line import Line
    from adafruit_magtag.magtag import MagTag
except ImportError as err:
    # Autoreload is off, so writing this file can't trigger a reload loop.
    # It names the missing library -- the usual cause of "Code done
    # running" with nothing else on screen.
    try:
        with open("/boot_error.txt", "w") as f:
            f.write("ImportError: %s\n" % (err,))
    except Exception:
        pass
    raise

# ---------------------------------------------------------------- config

DESIGN = "carafes"  # "freshness" or "carafes"
FRESH_MINUTES = 120
SYNC_RETRY_TICKS = 30  # re-attempt a failed clock sync after this many ticks
MAX_SYNC_TRIES = 3  # then give up on Wi-Fi until reset, to save battery
# The countdown is shown in steps of this many minutes, and the e-ink only
# refreshes (with its full black/white flash) when what it shows changes:
# 5 -> ~12 flashes per pot instead of 60. Set to 1 for a per-minute display.
DISPLAY_STEP = 2
# Pulse the NeoPixels for 10 s after a brew (green = regular, orange =
# decaf). Off by default: the case hides them, and skipping the pulse keeps
# each brew wake 10 s shorter -- better battery, and the next press is
# picked up sooner.
BREW_LIGHT = False
# Post to Slack on every brew when SLACK_WEBHOOK_URL is set in settings.toml
# (a Slack Workflow Builder "From a webhook" trigger with a `text` variable,
# or an app's incoming webhook -- both take {"text": ...}). Costs a few
# seconds of Wi-Fi per brew; if Wi-Fi or Slack fails, the timer carries on.
SLACK_ON_BREW = True
# Append each wake's progress lines to /wake.log on the drive (readable from
# a computer later), so wakes from real deep sleep -- on a charger or
# battery, where there's no serial console -- can be diagnosed. Capped at
# WAKE_LOG_MAX bytes. Turn off once things are stable: it writes flash on
# every wake.
WAKE_LOG = True
WAKE_LOG_PATH = "/wake.log"
WAKE_LOG_MAX = 16 * 1024

REGULAR, DECAF, NONE = 1, 2, 0
NEVER = 0xFFFFFFFF

WHITE, LIGHT, DARK, BLACK = 0, 1, 2, 3
PALETTE = displayio.Palette(4)
PALETTE[WHITE] = 0xFFFFFF
PALETTE[LIGHT] = 0x999999
PALETTE[DARK] = 0x555555
PALETTE[BLACK] = 0x000000

WIDTH, HEIGHT = 296, 128

if DESIGN == "carafes":
    # button index -> (slot, brew type)
    BUTTON_ACTIONS = {0: (0, REGULAR), 1: (0, DECAF), 2: (1, REGULAR), 3: (1, DECAF)}
    # per-half button legends drawn along the bottom of the screen
    # (the case hides the A-D silkscreen, so name the side instead)
    LEGENDS = ("L REG  L DECAF", "R REG  R DECAF")
else:
    BUTTON_ACTIONS = {0: (0, REGULAR), 3: (1, DECAF)}
    SLOT_NAMES = ("REGULAR", "DECAF")

# Index matches BUTTON_ACTIONS: silkscreen A..D is board.BUTTON_A..D
# (confirmed on hardware -- they are NOT mirrored).
BUTTON_PINS = (board.BUTTON_A, board.BUTTON_B, board.BUTTON_C, board.BUTTON_D)
TYPE_NAMES = {REGULAR: "REGULAR", DECAF: "DECAF", NONE: "--"}

# The brew-type label ("REGULAR"/"DECAF") is drawn as large as fits inside
# one half of the screen, while the brew-time line ("BREWED 25M AGO") stays
# small above it. Different CircuitPython builds can ship different builtin
# fonts, so the scale is measured from the actual font instead of hard-coded.
_TYPE_W = terminalio.FONT.get_bounding_box()[0] * len("REGULAR")
TYPE_SCALE = max(1, min(3, (WIDTH // 2 - 8) // _TYPE_W))
# The big countdown uses the largest scale (up to 6) at which its widest
# value -- the full pot, e.g. "120m" -- fits a screen half with a margin,
# so it stays one size all the way down.
_TIMER_W = terminalio.FONT.get_bounding_box()[0] * len("%dm" % FRESH_MINUTES)
TIMER_SCALE = max(1, min(6, (WIDTH // 2 - 20) // _TIMER_W))
# carafes layout: vertical center of the big countdown. Sits a few px below
# the type label so they don't touch, and still clears the bar at y=100.
TIMER_Y = 67
# Two equal halves around a 2 px divider (an even-width screen can't be
# split evenly by a 1 px line): left 0-146, divider 147-148, right 149-295.
HALF_W = (WIDTH - 2) // 2
DIVIDER_X = HALF_W


def half_center(half):
    """x center of a screen half (0 = left, 1 = right)."""
    return half * (HALF_W + 2) + HALF_W // 2


def divider(parent, y0, y1):
    for x in (DIVIDER_X, DIVIDER_X + 1):
        parent.append(Line(x, y0, x, y1, PALETTE[BLACK]))

# sleep_memory layout (33 of the ESP32-S2's 4096 bytes; the last 34 bytes
# are the last-good Wi-Fi network and boot.py's wake reason):
#   magic, tick_now, brew0, brew1, type0, type1,
#   epoch0, epoch1 (UTC brew times, 0 = clock was invalid),
#   last_sync (UTC epoch of last NTP sync), clock_valid, last_try_tick,
#   sync_tries (failed sync attempts since the clock was last valid)
# CIRCUITPY is read-only to code (no boot.py remount), so state can't go
# in a file.
# Changing this layout means the next version can't read the saved state:
# bump MEM_MAGIC with it, and know that installing that update (by hand or
# from GitHub) clears any running timers once.
MEM_FMT = "<BIIIBBIIIBiB"
MEM_MAGIC = 0xC1
MEM_SIZE = struct.calcsize(MEM_FMT)

# ------------------------------------------------- pure time helpers
# No hardware dependencies: safe to unit-test on a desktop Python.

# CircuitPython's time module has no gmtime(). Its RTC is set to UTC (NTP
# with tz_offset=0) and localtime() applies no timezone, so on the board
# localtime(secs) *is* the UTC breakdown. Desktop Python keeps gmtime.
_utctime = getattr(time, "gmtime", None) or time.localtime


def _weekday(y, m, d):
    """Day of week via Tomohiko Sakamoto's method. 0 = Sunday."""
    t = (0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4)
    y -= m < 3
    return (y + y // 4 - y // 100 + y // 400 + t[m - 1] + d) % 7


def _days_from_civil(y, m, d):
    """Days since 1970-01-01 (Howard Hinnant's algorithm)."""
    y -= m <= 2
    era = (y >= 0 and y or y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def pacific_offset(epoch):
    """UTC offset in hours for America/Los_Angeles at a UTC epoch."""
    g = _utctime(epoch)
    y, m, d = g.tm_year, g.tm_mon, g.tm_mday
    # US DST: second Sunday of March 10:00 UTC -> first Sunday of Nov 09:00 UTC
    mar1 = _weekday(y, 3, 1)
    dst_start = _days_from_civil(y, 3, 1 + (7 - mar1) % 7 + 7) * 86400 + 36000
    nov1 = _weekday(y, 11, 1)
    dst_end = _days_from_civil(y, 11, 1 + (7 - nov1) % 7) * 86400 + 32400
    return -7 if dst_start <= epoch < dst_end else -8


def brew_time_str(epoch):
    """Format a UTC epoch as Pacific wall-clock time, e.g. '9:42 AM'."""
    lt = _utctime(epoch + pacific_offset(epoch) * 3600)
    h = lt.tm_hour
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return "%d:%02d %s" % (h12, lt.tm_min, suffix)


def wifi_networks():
    """(ssid, password, channel) for each network in settings.toml, in
    priority order.

    CIRCUITPY_WIFI_SSID / CIRCUITPY_WIFI_PASSWORD first (note: CircuitPython
    itself also auto-joins this one at every boot for the web workflow),
    then WIFI_SSID_1 / WIFI_PASSWORD_1 through WIFI_SSID_9 / WIFI_PASSWORD_9.
    Gaps in the numbering are fine. Optional WIFI_CHANNEL_n (1-13) pins the
    channel, which helps with hidden networks; 0 or unset scans them all.
    """
    import os
    networks = []
    ssid = os.getenv("CIRCUITPY_WIFI_SSID")
    if ssid:
        networks.append((ssid, os.getenv("CIRCUITPY_WIFI_PASSWORD") or "", 0))
    for i in range(1, 10):
        ssid = os.getenv("WIFI_SSID_%d" % i)
        if ssid:
            networks.append((ssid, os.getenv("WIFI_PASSWORD_%d" % i) or "",
                             int(os.getenv("WIFI_CHANNEL_%d" % i) or 0)))
    # Try the network that last worked first, so one that's out of range
    # (e.g. home Wi-Fi when the board is at the office) doesn't cost its
    # full 8 s connect timeout on every brew.
    last = _last_network()
    for i, net in enumerate(networks):
        if net[0] == last:
            networks.insert(0, networks.pop(i))
            break
    return networks


# sleep_memory's last byte is boot.py's wake reason; the 33 bytes before
# it hold the name of the network that last connected (length + UTF-8).
_NET_LEN = 33


def _last_network():
    mem = alarm.sleep_memory
    start = len(mem) - 1 - _NET_LEN
    n = mem[start]
    if not 0 < n < _NET_LEN:
        return None
    try:
        return bytes(mem[start + 1:start + 1 + n]).decode()
    except Exception:
        return None


def _remember_network(ssid):
    data = ssid.encode()[:_NET_LEN - 1]
    mem = alarm.sleep_memory
    start = len(mem) - 1 - _NET_LEN
    mem[start:start + 1 + len(data)] = bytes([len(data)]) + data


def try_time_sync(budget=20):
    """Try each configured Wi-Fi network in order; set RTC from NTP.

    Returns the epoch on success, None if every network fails. Gives up
    after roughly `budget` seconds in total so a bad network can't hold the
    board awake. Failures are printed for the serial console.
    """
    networks = wifi_networks()
    if not networks:
        return None
    deadline = time.monotonic() + budget
    try:
        import wifi
        import socketpool
        import adafruit_ntp
        import rtc
    except Exception as e:  # best effort: the timer works fine without a clock
        print("time sync unavailable:", repr(e))
        return None
    for ssid, password, channel in networks:
        remaining = deadline - time.monotonic()
        if remaining < 3:
            print("time sync: out of time budget")
            break
        try:
            if not wifi.radio.connected or wifi.radio.ap_info.ssid != ssid:
                wifi.radio.connect(ssid, password, channel=channel,
                                   timeout=min(8, int(remaining)))
            _remember_network(ssid)
            pool = socketpool.SocketPool(wifi.radio)
            try:
                ntp = adafruit_ntp.NTP(pool, tz_offset=0, socket_timeout=5)
            except TypeError:  # older adafruit_ntp without socket_timeout
                ntp = adafruit_ntp.NTP(pool, tz_offset=0)
            rtc.RTC().datetime = ntp.datetime
            print("time sync OK via", ssid)
            return time.time()
        except Exception as e:
            print("time sync failed on %r: %r" % (ssid, e))
    return None


def _wifi_join(deadline):
    """Join the first configured network that works, unless already
    connected. Returns True when connected. Failures go to the serial
    console."""
    import wifi
    if wifi.radio.connected:
        return True
    for ssid, password, channel in wifi_networks():
        remaining = deadline - time.monotonic()
        if remaining < 3:
            break
        try:
            wifi.radio.connect(ssid, password, channel=channel,
                               timeout=min(8, int(remaining)))
            _remember_network(ssid)
            return True
        except Exception as e:
            print("wifi: failed on %r: %r" % (ssid, e))
    return False


BREW_WORDS = {REGULAR: "regular", DECAF: "decaf"}
SIDE_NAMES = ("left", "right")


def brew_message(slot, brew_type, epoch):
    """e.g. ':coffee: Fresh pot of decaf in the right carafe (brewed
    9:42 AM)'. The time is left off when the clock isn't set."""
    msg = ":coffee: Fresh pot of %s in the %s carafe" % (
        BREW_WORDS.get(brew_type, "coffee"), SIDE_NAMES[slot])
    if epoch:
        msg += " (brewed %s)" % brew_time_str(epoch)
    return msg


def post_slack(text, budget=15):
    """POST {"text": text} to SLACK_WEBHOOK_URL. Best effort, gives up after
    roughly `budget` seconds; returns True if Slack accepted it."""
    import os
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        return False
    deadline = time.monotonic() + budget
    try:
        import wifi
        import socketpool
        import ssl
        import adafruit_requests
        if not _wifi_join(deadline):
            print("slack: no Wi-Fi, message not sent")
            return False
        pool = socketpool.SocketPool(wifi.radio)
        session = adafruit_requests.Session(pool, ssl.create_default_context())
        response = session.post(url, json={"text": text},
                                timeout=max(3, deadline - time.monotonic()))
        ok = response.status_code == 200
        print("slack: HTTP %d%s" % (response.status_code,
                                    "" if ok else " " + response.text[:80]))
        response.close()
        return ok
    except Exception as e:
        print("slack: post failed: %r" % (e,))
        return False


def backfill_epochs(now, tick, brews, epochs):
    """Fill in brew timestamps recorded before the clock became valid."""
    for s in (0, 1):
        if brews[s] != NEVER and not epochs[s]:
            epochs[s] = now - (tick - brews[s]) * 60


# ---------------------------------------------------------------- state


def load_state():
    """Return (tick, brews, types, epochs, last_sync, valid, last_try,
    tries, first_boot)."""
    # Copy out as plain bytes: struct can't reliably read sleep_memory
    # in place.
    try:
        (magic, tick, b0, b1, t0, t1, e0, e1,
         last_sync, valid, last_try, tries) = struct.unpack(
             MEM_FMT, bytes(alarm.sleep_memory[0:MEM_SIZE]))
    except Exception:
        magic = 0
    if magic != MEM_MAGIC:
        return (0, [NEVER, NEVER], [NONE, NONE], [0, 0],
                0, False, -10 ** 9, 0, True)
    return (tick, [b0, b1], [t0, t1], [e0, e1],
            last_sync, bool(valid), last_try, tries, False)


def save_state(tick, brews, types, epochs, last_sync, valid, last_try,
               tries):
    alarm.sleep_memory[0:MEM_SIZE] = struct.pack(
        MEM_FMT, MEM_MAGIC, tick, brews[0], brews[1], types[0], types[1],
        int(epochs[0]), int(epochs[1]), int(last_sync), int(valid),
        int(last_try), min(tries, 255))


def minutes_left(tick, brews, slot):
    if brews[slot] == NEVER:
        return None
    left = FRESH_MINUTES - (tick - brews[slot])
    return max(0, left)


def panel(tick, brews, types, epochs, half):
    """What one half of the screen shows: (brew_label, type_name, big_text,
    bar_frac). The countdown is rounded up to DISPLAY_STEP, like a timer:
    with a 2-minute step, 119-120 minutes left shows "120m"."""
    left = minutes_left(tick, brews, half)
    type_name = TYPE_NAMES[types[half]]
    if left is None:
        return ("--", type_name, "--", None)
    shown = -(-left // DISPLAY_STEP) * DISPLAY_STEP
    if epochs[half]:
        brew_label = "BREWED " + brew_time_str(epochs[half])
    elif left == 0:
        brew_label = "BREWED %dH+ AGO" % (FRESH_MINUTES // 60)
    else:
        # no wall clock (Wi-Fi off/failed): count from the minute ticks
        ago = FRESH_MINUTES - shown
        brew_label = "BREWED %dM AGO" % ago if ago else "BREWED JUST NOW"
    if left == 0:
        return (brew_label, type_name, "STALE", 0)
    return (brew_label, type_name, "%dm" % shown, shown / FRESH_MINUTES)


# ---------------------------------------------------------------- drawing helpers


def text(parent, s, x, y, scale=2, color=BLACK):
    t = label.Label(terminalio.FONT, text=s, color=PALETTE[color], scale=scale)
    t.anchor_point = (0.5, 0.5)
    t.anchored_position = (x, y)
    parent.append(t)
    return t


def coffee_cup(parent, cx, y, w, h, frac):
    """Side-view cup. Fill drains, coffee greys out, steam fades with frac."""
    parent.append(Rect(cx - w // 2, y, w, h, outline=PALETTE[BLACK], stroke=2))
    parent.append(Rect(cx + w // 2, y + 8, 10, 16, outline=PALETTE[BLACK], stroke=2))
    if frac is not None and frac > 0:
        shade = BLACK if frac > 0.66 else (DARK if frac > 0.33 else LIGHT)
        fh = max(2, int((h - 6) * frac))
        parent.append(Rect(cx - w // 2 + 3, y + h - 3 - fh, w - 6, fh,
                           fill=PALETTE[shade]))
        # steam wisps fade away as the coffee stales
        wisps = 3 if frac > 0.66 else (2 if frac > 0.33 else 1)
        for i, dx in enumerate((-16, 0, 16)):
            if i >= wisps:
                continue
            x = cx + dx
            pts = [(x, y - 4), (x - 3, y - 10), (x + 3, y - 16), (x, y - 22)]
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                parent.append(Line(x1, y1, x2, y2, PALETTE[DARK]))


def freshness_bar(parent, half, y, w, frac, segs=10):
    """Segmented bar of about width w, centered in a screen half. The right
    half's bar is placed as the exact mirror image of the left's."""
    sw = w // segs
    drawn = segs * sw - 2  # the last segment has no trailing gap
    x = half_center(0) - drawn // 2
    if half:
        x = WIDTH - x - drawn
    for i in range(segs):
        on = frac is not None and (i / segs) < frac
        parent.append(Rect(x + i * sw, y, sw - 2, 6,
                           fill=PALETTE[BLACK if on else LIGHT]))


def render_freshness(group, tick, brews, types, epochs):
    for half in (0, 1):
        cx = half_center(half)
        _, _, big, frac = panel(tick, brews, types, epochs, half)
        text(group, SLOT_NAMES[half], cx, 11, scale=2)
        if frac is None:
            text(group, "--", cx, 40, scale=5, color=DARK)
            text(group, "press " + ("A" if half == 0 else "D"), cx, 66,
                 scale=1, color=DARK)
            coffee_cup(group, cx, 78, 52, 30, None)
            freshness_bar(group, half, 116, 120, None)
        else:
            text(group, big, cx, 40, scale=3 if big == "STALE" else 5)
            coffee_cup(group, cx, 66, 56, 40, frac)
            freshness_bar(group, half, 116, 120, frac)
    divider(group, 6, 122)


def render_carafes(group, tick, brews, types, epochs):
    for half in (0, 1):
        cx = half_center(half)
        brew_label, type_name, big, frac = panel(tick, brews, types, epochs,
                                                 half)
        text(group, brew_label, cx, 10, scale=1, color=DARK)
        text(group, type_name, cx, 28, scale=TYPE_SCALE)
        if frac is None:
            text(group, "--", cx, TIMER_Y, scale=TIMER_SCALE, color=DARK)
        else:
            text(group, big, cx, TIMER_Y,
                 scale=3 if big == "STALE" else TIMER_SCALE)
        freshness_bar(group, half, 100, 120, frac, segs=12)
        text(group, LEGENDS[half], cx, 120, scale=1)
    divider(group, 6, 126)
    group.append(Line(0, 112, 296, 112, PALETTE[BLACK]))


def render(tick, brews, types, epochs):
    group = displayio.Group()
    # Opaque white background: without this the group is transparent and the
    # e-ink shows black.
    bg_bitmap = displayio.Bitmap(WIDTH, HEIGHT, 1)
    bg_palette = displayio.Palette(1)
    bg_palette[0] = 0xFFFFFF
    group.append(displayio.TileGrid(bg_bitmap, pixel_shader=bg_palette))
    if DESIGN == "carafes":
        render_carafes(group, tick, brews, types, epochs)
    else:
        render_freshness(group, tick, brews, types, epochs)
    return group


def safe_refresh(display, timeout=30):
    """Refresh the e-ink display, riding through "Refresh too soon".

    CircuitPython 10's EPaperDisplay raises RuntimeError if refresh() is
    called before the panel is ready -- notably during a settling period
    right after boot, when time_to_refresh already reads 0. The MagTag
    library's own refresh() retries the same way.
    Returns True if the refresh went through, False on timeout (the
    timer keeps working; the screen catches up on the next wake).
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            display.refresh()
            return True
        except RuntimeError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(1)

# ---------------------------------------------------------------- main
# NeoPixel feedback: green glow = regular, orange glow = decaf. NeoPixel
# green runs bright, so orange needs very little of it or it reads yellow.
BREW_LIGHT_COLORS = {REGULAR: 0x00FF00, DECAF: 0xFF2A00}
BREATH_PERIOD = 2.5  # seconds per slow in-and-out breath
FADE_OUT = 1.5  # seconds of final fade to dark


def pulse_brew_light(magtag, brew_type, slot, seconds=10):
    """Pulse the NeoPixels for ~10 s after a brew: green for regular,
    orange for decaf -- but only the two pixels on that carafe's side
    (left pair for the left carafe, right pair for the right). The board
    stays awake for this (a few mA for 10 s is negligible on the
    420 mAh cell), then the pixels are powered back down so deep sleep
    stays at zero draw."""
    color = BREW_LIGHT_COLORS.get(brew_type, 0xFFFFFF)
    # NeoPixel 0 is the rightmost LED (silkscreen #0 is on the right);
    # indices run right-to-left, so the left carafe uses the high indices.
    side = (3, 2) if slot == 0 else (1, 0)
    try:
        magtag.peripherals.neopixel_disable = False
        pixels = magtag.peripherals.neopixels
        pixels.fill(0x000000)
        pixels.brightness = 0.5
        r, g, b = (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF
        start = time.monotonic()
        while True:
            t = time.monotonic() - start
            if t >= seconds:
                break
            envelope = min(1.0, (seconds - t) / FADE_OUT)
            # smooth sine breathing; the second LED trails the first by a
            # quarter breath so the glow sways between them
            for k, i in enumerate(side):
                wave = 0.5 - 0.5 * math.cos(
                    2 * math.pi * (t / BREATH_PERIOD - k * 0.25))
                level = (0.06 + 0.94 * wave) * envelope
                pixels[i] = (int(r * level), int(g * level), int(b * level))
            time.sleep(0.02)
        pixels.fill(0x000000)
        magtag.peripherals.neopixel_disable = True
    except Exception:
        pass  # pixels must never break the timer


_hw = {}  # lets the crash handler release the button pins


def log(wake_start, msg):
    """Timestamped progress line for the serial console (and /wake.log), so
    a wake that gets stuck shows where."""
    line = "[%5.1fs] %s" % (time.monotonic() - wake_start, msg)
    print(line)
    if WAKE_LOG:
        try:
            with open(WAKE_LOG_PATH, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass  # drive not writable by code (no boot.py / edit mode)


def trim_wake_log():
    """Keep /wake.log under WAKE_LOG_MAX by dropping its older half."""
    import os
    try:
        if os.stat(WAKE_LOG_PATH)[6] <= WAKE_LOG_MAX:
            return
        with open(WAKE_LOG_PATH) as f:
            tail = f.read()[-WAKE_LOG_MAX // 2:]
        with open(WAKE_LOG_PATH, "w") as f:
            f.write(tail[tail.find("\n") + 1:])
    except OSError:
        pass


def read_boot_wake():
    """The wake reason boot.py saved in the last byte of sleep_memory
    (0 none, 1-4 BUTTON_A-D, 5 other pin, 10 timer), or None without a
    boot.py that records it. Marks it read so it's never reused."""
    mem = alarm.sleep_memory
    reason = mem[len(mem) - 1]
    mem[len(mem) - 1] = 0xFF
    return None if reason == 0xFF else reason


def main():
    wake_start = time.monotonic()
    magtag = MagTag()
    _hw["magtag"] = magtag
    # The MagTag library powers the NeoPixels on while it starts up. Write
    # black first, so they can't show whatever color they wake up holding,
    # then cut their power.
    try:
        magtag.peripherals.neopixels.fill(0x000000)
    except Exception:
        pass
    magtag.peripherals.neopixel_disable = True

    if WAKE_LOG:
        import microcontroller
        trim_wake_log()
        now = time.localtime()
        try:
            battery = "%.2fV" % magtag.peripherals.battery
        except Exception:
            battery = "?"
        log(wake_start, "=== %04d-%02d-%02d %02d:%02d:%02d UTC  reset=%s  battery=%s"
            % (now.tm_year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min,
               now.tm_sec, str(microcontroller.cpu.reset_reason).split(".")[-1],
               battery))

    (tick, brews, types, epochs, last_sync, valid, last_try, tries,
     first_boot) = load_state()

    wake = alarm.wake_alarm
    boot_wake = read_boot_wake()
    log(wake_start, "wake: alarm.wake_alarm=%r, boot.py saw %r" % (wake, boot_wake))

    # The press is long over by the time boot + imports finish, so the
    # button's live state is useless -- the alarm records which pin fired.
    # Prefer alarm.wake_alarm; fall back to what boot.py recorded, since
    # CircuitPython can lose the wake pin by the time code.py runs.
    pressed = None
    if isinstance(wake, alarm.pin.PinAlarm):
        for i, p in enumerate(BUTTON_PINS):
            if wake.pin == p:
                pressed = i
                break
    if pressed is None and boot_wake is not None and 1 <= boot_wake <= 4:
        pressed = boot_wake - 1
    timer_wake = pressed is None and (
        isinstance(wake, alarm.time.TimeAlarm) or boot_wake == 10)
    if pressed in BUTTON_ACTIONS:
        slot, brew_type = BUTTON_ACTIONS[pressed]
        brews[slot] = tick
        types[slot] = brew_type
        epochs[slot] = time.time() if valid else 0
    elif timer_wake:
        tick += 1

    is_active = any((minutes_left(tick, brews, s) or 0) > 0 for s in (0, 1))

    save_state(tick, brews, types, epochs, last_sync, valid, last_try,
               tries)

    # Both pots share one minute tick, and every refresh redraws both
    # halves. To keep the e-ink flashes down, refresh only on boot, on a
    # brew, when a pot goes stale, and on a shared DISPLAY_STEP-minute
    # beat while anything is counting down -- so two pots brewed a few
    # minutes apart still update together, not on separate minutes. The
    # board still wakes every minute to keep time, just without a flash.
    went_stale = timer_wake and any(
        brews[s] != NEVER and minutes_left(tick, brews, s) == 0
        and minutes_left(tick - 1, brews, s) > 0 for s in (0, 1))
    on_beat = is_active and tick % DISPLAY_STEP == 0
    if first_boot or pressed is not None or went_stale or on_beat:
        display = magtag.graphics.display
        display.root_group = render(tick, brews, types, epochs)
        safe_refresh(display)
        log(wake_start, "screen refreshed")

    # Pulse the brew light AFTER the e-ink refresh, so the screen updates
    # immediately and the 10 s pulse runs last before deep sleep.
    if BREW_LIGHT and pressed in BUTTON_ACTIONS:
        slot, brew_type = BUTTON_ACTIONS[pressed]
        pulse_brew_light(magtag, brew_type, slot)

    # Slack alert, also after the screen so it never delays the display.
    # Its Wi-Fi connection is reused by the clock sync below if needed.
    if SLACK_ON_BREW and pressed in BUTTON_ACTIONS:
        slot, brew_type = BUTTON_ACTIONS[pressed]
        log(wake_start, "slack: posting")
        log(wake_start, "slack: sent=%r"
            % post_slack(brew_message(slot, brew_type, epochs[slot])))

    # --- wall-clock sync (best effort; the timer works without it) ---
    # Done last, after the screen and lights, so a slow or failing network
    # never delays the display. A brew recorded without a clock gets its
    # timestamp backfilled from the minute ticks once a sync succeeds.
    # Wi-Fi is only used when there's no trustworthy clock: first boot,
    # or the RTC was lost (it reads year 2000 after a power loss). The RTC
    # keeps running through deep sleep, so once set it isn't re-synced.
    # If it still fails after MAX_SYNC_TRIES, stop trying until the next
    # reset: the timer falls back to "BREWED 12M AGO" labels.
    if valid and time.localtime().tm_year < 2025:
        valid, tries = False, 0
    if (not valid and tries < MAX_SYNC_TRIES
            and (first_boot or tick - last_try >= SYNC_RETRY_TICKS)):
        last_try = tick
        log(wake_start, "clock: syncing")
        now = try_time_sync()
        log(wake_start, "clock: %s" % ("synced" if now else "no sync"))
        if now is not None:
            valid, last_sync, tries = True, now, 0
            backfill_epochs(now, tick, brews, epochs)
        else:
            tries += 1
    save_state(tick, brews, types, epochs, last_sync, valid, last_try,
               tries)

    # --- self-update from GitHub (updater.py; best effort) ---
    # Only on brews, when Wi-Fi is usually already up for Slack. A new
    # app.py takes effect on the next wake, with the timers intact.
    if pressed in BUTTON_ACTIONS:
        try:
            import updater
            if updater.configured():
                log(wake_start, "update: checking")
                log(wake_start, "update: %s" % updater.check(_wifi_join))
        except ImportError:
            pass

    # Free the button pins before arming PinAlarms: the Peripherals object
    # still holds DigitalInOuts on them, and deep sleep refuses pins that
    # are already claimed. Only the buttons are released -- a full
    # peripherals.deinit() would also cut the NeoPixel power pin loose, so
    # it stays explicitly driven off instead.
    for button in magtag.peripherals.buttons:
        button.deinit()
    magtag.peripherals.neopixel_disable = True

    pin_alarms = [alarm.pin.PinAlarm(pin=p, value=False, pull=True)
                  for p in BUTTON_PINS]
    log(wake_start, "sleeping (%s)" % ("minute timer + buttons" if is_active
                                     else "buttons only"))
    if is_active:
        # measured from the start of this wake, so a slow wake (refresh,
        # light pulse, Wi-Fi) doesn't stretch the minute
        minute = alarm.time.TimeAlarm(
            monotonic_time=max(time.monotonic() + 5, wake_start + 60))
        alarm.exit_and_deep_sleep_until_alarms(minute, *pin_alarms)
    # No pot fresh: set no timer at all. The e-ink image holds with zero
    # power until the next button press.
    alarm.exit_and_deep_sleep_until_alarms(*pin_alarms)


def show_error(err):
    """Render an unhandled exception on the e-ink display.

    Field diagnostic: with no serial attached a crash would otherwise be
    invisible. The message stays on screen; any button restarts the timer.
    """
    try:
        disp = board.DISPLAY
        disp.rotation = 270
        group = displayio.Group()
        # white background, or black text lands on a black screen
        bg_palette = displayio.Palette(1)
        bg_palette[0] = 0xFFFFFF
        group.append(displayio.TileGrid(
            displayio.Bitmap(WIDTH, HEIGHT, 1), pixel_shader=bg_palette))
        lines = ["CRASH: %s  (press any button)" % type(err).__name__]
        msg = str(err) or "<no message>"
        while msg:
            lines.append(msg[:44])
            msg = msg[44:]
        tb = err.__traceback__
        while tb is not None and len(lines) < 10:
            lines.append("  line %d" % tb.tb_lineno)
            tb = tb.tb_next
        y = 6
        for ln in lines[:11]:
            t = label.Label(terminalio.FONT, text=ln, color=0x000000, scale=1)
            t.anchor_point = (0, 0)
            t.anchored_position = (4, y)
            group.append(t)
            y += 11
        disp.root_group = group
        safe_refresh(disp)
    except Exception:
        pass


try:
    main()
except Exception as exc:  # noqa: BLE001 -- diagnostic: surface it
    # A freshly installed update that crashes is rolled back right away.
    try:
        import updater
        if updater.rollback("crashed: %r" % (exc,)):
            supervisor.reload()
    except ImportError:
        pass
    # Save the full traceback to /error.txt so it can be read from the
    # CIRCUITPY drive without a serial console.
    try:
        import traceback
        with open("/error.txt", "w") as f:
            traceback.print_exception(exc, file=f)
    except Exception:
        pass
    show_error(exc)
    # Deep-sleep until a button press restarts code.py, rather than hanging
    # awake (unresponsive, draining the battery) until a manual reset.
    try:
        mt = _hw.get("magtag")
        if mt:
            for button in mt.peripherals.buttons:
                button.deinit()
        alarm.exit_and_deep_sleep_until_alarms(
            *[alarm.pin.PinAlarm(pin=p, value=False, pull=True)
              for p in BUTTON_PINS])
    except Exception:
        while True:
            time.sleep(300)
