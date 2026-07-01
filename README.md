# Ecowitt → APRS-IS Bridge (polling mode)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Polls the Ecowitt GW3000 live-data endpoint every 60 s, builds an APRS weather
packet, and pushes it every 10 minutes to any server speaking the standard
APRS-IS TCP login protocol. That's typically one of:

- a **WX3in1 Plus 2.0 simple server**, for actual RF transmission on
  144.390 MHz — this was the original use case; or
- a **real APRS-IS server** (e.g. `noam.aprs2.net:14580`), to inject the
  packet straight into the APRS-IS network without any radio hardware at all.

Point `APRS_SERVER_HOST`/`APRS_SERVER_PORT` at whichever you want; the login
handshake and packet format are identical either way.

If your GW3000 also has a Customized upload slot pointed elsewhere (e.g. Home
Assistant), leave it alone — this bridge polls the gateway's local HTTP API
independently and doesn't touch that configuration.

---

## Quick start

```bash
cd ~/wx-aprs
cp .env.example .env
$EDITOR .env   # fill in your GW3000/WX3in1 IPs, callsign, and coordinates
docker compose up -d --build
docker compose logs -f
```

No firewall rules needed — the bridge makes only outbound connections.

---

## Verify delivery

### 1 — Watch bridge logs

```bash
docker compose logs -f wx-aprs-bridge
```

Expect a `Poll OK` line every 60 s and a `Beacon firing` + `Injection complete`
line every 10 minutes.

### 2 — If targeting a WX3in1 (RF transmission)

```bash
telnet <APRS_SERVER_HOST> 23
# login: admin / admin
debug rf on
```

When the bridge injects a packet you'll see:
```
(RF) Sending beacon: YOURCALL-13>APZWXB,TCPIP*:!3337.57N/08639.76W_...
```
and the TX LED on the radio connector briefly keys. **This is the only definitive
proof of RF TX** — aprs.fi alone is not sufficient (the WX3in1 also has its own
APRS-IS uplink).

### 3 — aprs.fi

Within a minute, your `APRS_CALL` should appear at the configured
`APRS_LAT`/`APRS_LON` with live weather values — whether you're gating through
RF or injecting straight into APRS-IS.

---

## Manual / forced test

To quickly verify parsing without waiting for the configured interval,
temporarily set `RF_INTERVAL_MIN=1` in `.env`, rebuild, watch for a beacon,
then set it back.

To check what the parser produces from the live GW3000 right now:

```bash
curl -s http://<GW3000_IP>/get_livedata_info | python3 -c "
import json, sys
sys.path.insert(0, '/path/to/wx-aprs')
import bridge
data = json.load(sys.stdin)
wx = bridge.parse_gw3000(data)
print(wx)
print(bridge.build_aprs_packet(wx))
"
```

---

## Environment variables

All configuration lives in `.env` (copy `.env.example` to get started — see
Quick start above). `.env` is gitignored since it contains your LAN IPs,
callsign, and coordinates.

| Variable | Example | Description |
|----------|---------|-------------|
| `GW3000_URL` | `http://192.168.1.115/get_livedata_info` | GW3000 local poll endpoint |
| `POLL_INTERVAL_SEC` | `60` | How often to poll the GW3000 |
| `APRS_SERVER_HOST` | `192.168.1.100` | WX3in1 IP, or an APRS-IS server hostname |
| `APRS_SERVER_PORT` | `14580` | TCP port (14580 for both WX3in1 simple-server and standard APRS-IS) |
| `APRS_LOGIN_USER` | `yourcall` | Literal username on the WX3in1 simple-server tab, or your callsign for real APRS-IS |
| `APRS_LOGIN_PASS` | `-1` | Literal password on the WX3in1 simple-server tab, or your real APRS-IS passcode |
| `APRS_CALL` | `YOURCALL-13` | APRS callsign-SSID in the transmitted packet |
| `APRS_LAT` | `3337.57N` | Latitude in APRS DDMM.mmH format |
| `APRS_LON` | `08639.76W` | Longitude in APRS DDDMM.mmH format |
| `APRS_COMMENT` | *(empty)* | Optional trailing comment on the weather packet |
| `RF_INTERVAL_MIN` | `10` | Minutes between beacons |

`GW3000_URL`, `APRS_SERVER_HOST`, `APRS_LOGIN_USER`, `APRS_CALL`, `APRS_LAT`,
and `APRS_LON` are required — the bridge refuses to start if any are unset.

**Note:** if you point this at a real APRS-IS server, `APRS_LOGIN_PASS` must be
your actual APRS-IS passcode for `APRS_LOGIN_USER` (the callsign in the login
line, not necessarily `APRS_CALL` — servers validate the passcode against the
login callsign). An invalid passcode logs in unverified and your packets will
be silently dropped by the server.

The packet's AX.25 destination (tocall) is `APZWXB` — an unregistered code
in the `APZxxx` range reserved for hobbyist software without an assigned
tocall (aprs.fi shows this as "Unknown: Experimental"). Once this project
has a real name/repo, register a proper tocall via a PR to
[aprsorg/aprs-deviceid](https://github.com/aprsorg/aprs-deviceid) and swap
it into `build_aprs_packet()`.

---

## Field mapping and caveats

| APRS field | Source | Notes |
|------------|--------|-------|
| `t` temp | `common_list[0x02]` (°F) | Outdoor sensor |
| `h` humidity | `common_list[0x07]` (%) | Outdoor sensor |
| `_ddd` wind dir | `common_list[0x0A]` (°) | |
| `sss` wind spd | `common_list[0x0B]` (mph) | Sustained |
| `g` gust | `common_list[0x0C]` (mph) | Real-time gust (ITEM_GUSTSPEED) |
| `r` rain/hr | `piezoRain[0x0E]` (in/Hr) | Rain rate — approximate hourly accumulation |
| `p` rain 24h | `piezoRain[0x10]` (in) | Daily total used as 24h proxy |
| `P` since midnight | `piezoRain[0x10]` (in) | Rain since local midnight |
| `b` pressure | `wh25[0].rel` (inHg) | Sea-level; converted to tenths of mbar |
| `L`/`l` solar | `common_list[0x15]` (W/m²) | `L<nnn>` under 1000 W/m², `l<nnn>` for value−1000 at/above |

---

## Known gotchas

- **CRLF is mandatory** — the WX3in1 simple server silently returns `# login error` on LF-only input.
- **3-second pause** between login and packet is a WX3in1 quirk (back-to-back sends fail there); harmless extra latency against a real APRS-IS server.
- Against a WX3in1, login credentials are the literal strings on its "APRS-IS simple server" tab, not an APRS-IS passcode. Against real APRS-IS, use your actual callsign and passcode.
- **aprs.fi ≠ RF TX** — if targeting a WX3in1, it also uploads to APRS-IS on its own. Only telnet `(RF) Sending` / TX LED proves RF actually went out.
- If broadcasting alongside other APRS objects (repeaters, digipeaters) at nearby coordinates, keep your weather station's lat/lon distinct enough to avoid icon stacking on aprs.fi.
- On restart, the bridge waits for the first successful GW3000 poll and fires an immediate beacon rather than waiting a full `RF_INTERVAL_MIN`.
