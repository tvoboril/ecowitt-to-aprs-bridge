#!/usr/bin/env python3
"""
Ecowitt GW3000 → APRS-IS Bridge (polling mode)

Polls GW3000 at get_livedata_info every POLL_INTERVAL_SEC seconds,
caches the parsed values, and pushes an APRS weather packet to any
server speaking the standard APRS-IS TCP login protocol every
RF_INTERVAL_MIN minutes — e.g. a WX3in1 Plus 2.0 simple server (for RF
gating) or a real APRS-IS server directly (e.g. noam.aprs2.net:14580).

No inbound ports needed — the bridge only makes outbound connections.
"""

import json
import logging
import os
import re
import socket
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
APRS_SERVER_HOST = os.environ.get("APRS_SERVER_HOST",    "")
APRS_SERVER_PORT = int(os.environ.get("APRS_SERVER_PORT", "14580"))
APRS_LOGIN_USER  = os.environ.get("APRS_LOGIN_USER",      "")
APRS_LOGIN_PASS  = os.environ.get("APRS_LOGIN_PASS",      "-1")
GW3000_URL       = os.environ.get("GW3000_URL",           "")
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL_SEC", "60"))
APRS_CALL        = os.environ.get("APRS_CALL",            "")
APRS_LAT         = os.environ.get("APRS_LAT",             "")
APRS_LON         = os.environ.get("APRS_LON",             "")
APRS_COMMENT     = os.environ.get("APRS_COMMENT",         "")
RF_INTERVAL_MIN  = int(os.environ.get("RF_INTERVAL_MIN",  "10"))

_REQUIRED = ("APRS_SERVER_HOST", "APRS_LOGIN_USER", "GW3000_URL", "APRS_CALL", "APRS_LAT", "APRS_LON")
_missing = [name for name in _REQUIRED if not os.environ.get(name)]
if _missing:
    raise SystemExit(
        f"Missing required environment variable(s): {', '.join(_missing)}. "
        "Copy .env.example to .env and fill in your site-specific values."
    )

# Skip RF beacon if cache is older than this (data source may be down)
MAX_CACHE_AGE_SEC = 1800

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_lock       = threading.Lock()
_cache      = {}    # keys: tempf, humidity, winddir, windspd, gust,
                    #       rain_rate_iph, rain_day_in, baro_inHg, solar_wm2
_cache_time = 0.0   # epoch of last successful poll

# ---------------------------------------------------------------------------
# GW3000 JSON parser
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r'^(-?\d+\.?\d*)')

def _num(val) -> float | None:
    """Extract leading float from strings like '82%', '0.00 mph', '29.20 inHg'."""
    if val is None:
        return None
    m = _NUM_RE.match(str(val).strip())
    return float(m.group(1)) if m else None


def parse_gw3000(data: dict) -> dict:
    """
    Parse a get_livedata_info JSON response into a flat dict of engineering values.

    Field notes (documented here because the source data is imprecise):
      - winddir (0x0A): degrees, 0-359
      - gust (0x0C): real-time gust speed (ITEM_GUSTSPEED)
      - rain_rate_iph (0x0E): rain rate in in/Hr, used as APRS 'r' (last-hour approx)
      - rain_day_in (0x10): rain since local midnight, used as APRS 'P' (and 'p')
      - baro_inHg: relative (sea-level) pressure from wh25[0].rel
      - solar_wm2 (0x15): solar radiation, W/m²
    """
    common = {item["id"]: item for item in data.get("common_list", [])}
    piezo  = {item["id"]: item for item in data.get("piezoRain", [])}
    wh25   = (data.get("wh25") or [{}])[0]

    def cv(section, key):
        return _num(section.get(key, {}).get("val"))

    return {
        "tempf":        _num(common.get("0x02", {}).get("val")),
        "humidity":     _num(common.get("0x07", {}).get("val")),
        "winddir":      _num(common.get("0x0A", {}).get("val")),
        "windspd":      _num(common.get("0x0B", {}).get("val")),
        "gust":         _num(common.get("0x0C", {}).get("val")),
        "rain_rate_iph":_num(piezo.get("0x0E", {}).get("val")),
        "rain_day_in":  _num(piezo.get("0x10", {}).get("val")),
        "baro_inHg":    _num(wh25.get("rel") or wh25.get("abs")),
        "solar_wm2":    _num(common.get("0x15", {}).get("val")),
    }

# ---------------------------------------------------------------------------
# APRS field encoders
# ---------------------------------------------------------------------------

def _mph3(v) -> str:
    if v is None: return "..."
    return f"{min(max(round(v), 0), 999):03d}"

def _temp3(v) -> str:
    if v is None: return "..."
    t = round(v)
    return f"{max(t, -99):03d}" if t < 0 else f"{min(t, 999):03d}"

def _rain3(v_inches) -> str:
    if v_inches is None: return "..."
    return f"{min(round(v_inches * 100), 999):03d}"

def _hum2(v) -> str:
    if v is None: return ".."
    h = round(v)
    return "00" if h >= 100 else f"{h:02d}"

def _baro5(v_inHg) -> str:
    if v_inHg is None: return "....."
    return f"{round(v_inHg * 33.8639 * 10):05d}"

def _wdir3(v) -> str:
    if v is None: return "..."
    return f"{round(v) % 360:03d}"

def _solar(v) -> str:
    if v is None: return "..."
    s = round(v)
    return f"L{min(max(s, 0), 999):03d}" if s < 1000 else f"l{min(s - 1000, 999):03d}"


def build_aprs_packet(wx: dict) -> str:
    """
    Build the full APRS position+weather packet line (no trailing CRLF).

    Destination "APZWXB" is an unregistered/experimental tocall (the
    APZxxx range is reserved in the APRS tocalls registry for hobbyist
    software without an assigned code) — aprs.fi shows this as "Unknown:
    Experimental" rather than a blank "Unknown" for a plain "APRS" tocall.
    TODO: once open-sourced, register a real tocall via a PR to
    aprsorg/aprs-deviceid and swap it in here.
    """
    rain_in = wx.get("rain_day_in")  # since midnight → both p and P

    body = (
        f"!{APRS_LAT}/{APRS_LON}_"
        f"{_wdir3(wx.get('winddir'))}/{_mph3(wx.get('windspd'))}"
        f"g{_mph3(wx.get('gust'))}"
        f"t{_temp3(wx.get('tempf'))}"
        f"r{_rain3(wx.get('rain_rate_iph'))}"   # rain rate ≈ hourly
        f"p{_rain3(rain_in)}"                    # daily ≈ 24h (best available)
        f"P{_rain3(rain_in)}"                    # since midnight
        f"h{_hum2(wx.get('humidity'))}"
        f"b{_baro5(wx.get('baro_inHg'))}"
        f"{_solar(wx.get('solar_wm2'))}"
        + APRS_COMMENT
    )
    return f"{APRS_CALL}>APZWXB,TCPIP*:{body}"

# ---------------------------------------------------------------------------
# APRS-IS-protocol TCP injector
#
# Speaks the standard APRS-IS login handshake, so APRS_SERVER_HOST/PORT can
# point at a WX3in1 Plus 2.0 simple server (for RF gating, login is any
# literal string configured on its "APRS-IS simple server" tab) or at a real
# APRS-IS server (e.g. noam.aprs2.net:14580, with your actual passcode).
# ---------------------------------------------------------------------------

def inject_to_server(packet: str) -> bool:
    """
    Open TCP connection, send CRLF login, wait for 'verified',
    sleep 3 s, send CRLF packet, hold 3 s, close.

    CRLF is mandatory — LF-only produces a silent login error on the WX3in1.
    The 3-second pauses are a WX3in1 quirk (back-to-back sends fail there);
    they're harmless extra latency against a real APRS-IS server.
    """
    login_line  = f"user {APRS_LOGIN_USER} pass {APRS_LOGIN_PASS}\r\n"
    packet_line = f"{packet}\r\n"

    try:
        with socket.create_connection((APRS_SERVER_HOST, APRS_SERVER_PORT), timeout=15) as sock:
            sock.sendall(login_line.encode())
            log.info("Sent login to %s:%d", APRS_SERVER_HOST, APRS_SERVER_PORT)

            # Collect the verified response (up to 5 s)
            sock.settimeout(5)
            response = b""
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(256)
                    if not chunk:
                        break
                    response += chunk
                    if b"verified" in response:
                        break
                except socket.timeout:
                    break

            resp_str = response.decode(errors="replace").strip()
            log.info("Server login response: %s", resp_str)
            if "verified" not in resp_str:
                log.warning("'verified' not seen — continuing after mandatory pause")

            time.sleep(3)   # mandatory pause before packet

            sock.settimeout(15)
            sock.sendall(packet_line.encode())
            log.info("Sent APRS packet: %s", packet)

            time.sleep(3)   # hold open so device can process

        log.info("Injection complete")
        return True

    except OSError as exc:
        log.error("Socket error injecting to server: %s", exc)
        return False

# ---------------------------------------------------------------------------
# Polling thread — updates _cache every POLL_INTERVAL seconds
# ---------------------------------------------------------------------------

def poll_loop():
    log.info("Poll thread started — %s every %d s", GW3000_URL, POLL_INTERVAL)
    while True:
        try:
            with urlopen(GW3000_URL, timeout=10) as resp:
                raw = json.loads(resp.read())
            wx = parse_gw3000(raw)
            with _lock:
                _cache.update(wx)
                global _cache_time
                _cache_time = time.time()
            log.info(
                "Poll OK — temp=%.1f°F hum=%s%% wind=%s°@%smph gust=%smph "
                "rain_rate=%sin/hr rain_day=%sin baro=%sinHg solar=%sW/m2",
                wx.get("tempf") or 0,
                wx.get("humidity"), wx.get("winddir"), wx.get("windspd"),
                wx.get("gust"), wx.get("rain_rate_iph"), wx.get("rain_day_in"),
                wx.get("baro_inHg"), wx.get("solar_wm2"),
            )
        except (URLError, OSError, json.JSONDecodeError, KeyError) as exc:
            log.error("Poll failed: %s", exc)

        time.sleep(POLL_INTERVAL)

# ---------------------------------------------------------------------------
# Beacon thread — transmits to RF every RF_INTERVAL_MIN minutes
# ---------------------------------------------------------------------------

def _fire_beacon():
    with _lock:
        wx        = dict(_cache)
        cache_age = time.time() - _cache_time

    if not wx:
        log.warning("Beacon: no cached data yet — skipping")
        return

    if cache_age > MAX_CACHE_AGE_SEC:
        log.warning("Beacon: cache is %.0f s old (> %d s) — skipping RF TX",
                    cache_age, MAX_CACHE_AGE_SEC)
        return

    packet = build_aprs_packet(wx)
    log.info("Beacon firing (cache age %.0f s) — %s", cache_age, packet)
    inject_to_server(packet)


def beacon_loop():
    interval_sec = RF_INTERVAL_MIN * 60
    log.info("Beacon thread started — RF every %d min", RF_INTERVAL_MIN)

    # Wait for the first successful poll so restart doesn't sit silent for
    # up to RF_INTERVAL_MIN before the first RF transmission.
    while not _cache:
        time.sleep(1)
    _fire_beacon()

    while True:
        time.sleep(interval_sec)
        _fire_beacon()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    for target, fn in [("poll", poll_loop), ("beacon", beacon_loop)]:
        t = threading.Thread(target=fn, daemon=True, name=target)
        t.start()

    log.info(
        "Bridge running — polling %s every %d s, beaconing to %s:%d every %d min as %s",
        GW3000_URL, POLL_INTERVAL, APRS_SERVER_HOST, APRS_SERVER_PORT, RF_INTERVAL_MIN, APRS_CALL,
    )

    # Keep main thread alive; daemon threads exit when main exits
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("Shutting down")
