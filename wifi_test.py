# Wi-Fi / NTP diagnostic for the coffee timer.
# Copy to CIRCUITPY next to code.py. In the serial console press Ctrl-C,
# then any key for the >>> prompt, then type:  import wifi_test
# To run it again without a reset, press Ctrl-D first, then Ctrl-C and
# import it again.

import os

import socketpool
import wifi

print()
print("=== wifi_test ===")
for i in range(1, 10):
    ssid = os.getenv("WIFI_SSID_%d" % i)
    if ssid:
        print("settings: WIFI_SSID_%d = %r" % (i, ssid))
if os.getenv("CIRCUITPY_WIFI_SSID"):
    print("settings: CIRCUITPY_WIFI_SSID is still set (auto-joins every boot)")

ssid = os.getenv("WIFI_SSID_1")
password = os.getenv("WIFI_PASSWORD_1") or ""
channel = int(os.getenv("WIFI_CHANNEL_1") or 0)
if not ssid:
    print("STOP: WIFI_SSID_1 not found -- settings.toml on the board is not the new one")
else:
    # Scan a few times (one pass can miss networks), keyed by access point.
    # Hidden networks show up with a blank name ''.
    aps = {}
    for _ in range(3):
        for n in wifi.radio.start_scanning_networks():
            bssid = ":".join("%02x" % b for b in n.bssid)
            aps[bssid] = (n.ssid, n.channel, n.rssi)
        wifi.radio.stop_scanning_networks()
    print("scan: %d access points seen:" % len(aps))
    for bssid, (name, ch, rssi) in sorted(aps.items(), key=lambda a: -a[1][2]):
        print("  %-24s channel %2d  %4d dBm  %s%s" % (
            repr(name), ch, rssi, bssid, "   <- hidden" if not name else ""))
    if not any(name == ssid for name, _, _ in aps.values()):
        print("scan: %r not broadcast (expected for a hidden network)" % ssid)

    try:
        print("connect: trying %r on %s" % (ssid, "channel %d" % channel if channel else "all channels"))
        wifi.radio.connect(ssid, password, channel=channel, timeout=15)
        print("connect: OK, ip %s, dns %s" % (wifi.radio.ipv4_address, wifi.radio.ipv4_dns))
    except Exception as e:
        print("connect: FAILED %r" % (e,))

    if wifi.radio.connected:
        pool = socketpool.SocketPool(wifi.radio)
        try:
            print("dns: OK, 0.adafruit.pool.ntp.org -> %s"
                  % (pool.getaddrinfo("0.adafruit.pool.ntp.org", 123)[0][4],))
        except Exception as e:
            print("dns: FAILED %r" % (e,))
        try:
            import adafruit_ntp
        except ImportError as e:
            adafruit_ntp = None
            print("ntp: adafruit_ntp library missing from /lib: %r" % (e,))
        if adafruit_ntp:
            for server in ("0.adafruit.pool.ntp.org", "162.159.200.123"):
                try:
                    ntp = adafruit_ntp.NTP(pool, server=server, socket_timeout=5)
                    print("ntp: OK via %s -> %r" % (server, ntp.datetime))
                except Exception as e:
                    print("ntp: FAILED via %s %r" % (server, e))
print("=== done ===")
