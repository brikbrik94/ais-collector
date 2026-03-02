#!/usr/bin/env python3
"""
InfluxDB v2 → HTTP JSON API

Exposes aircraft and vessel tracking data from InfluxDB as a simple
read-only JSON API for use by map frontends (e.g. karte.oe5ith.at).

Part of oe5ith-tracker-ingest
"""

import json
import gzip
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError

# ---------------------------------------------------------------------------
# Configuration (via environment variables)
# ---------------------------------------------------------------------------

INFLUXDB_URL    = os.getenv("INFLUXDB_URL",    "http://127.0.0.1:8086")
INFLUXDB_TOKEN  = os.getenv("INFLUXDB_TOKEN",  "")
INFLUXDB_ORG    = os.getenv("INFLUXDB_ORG",    "OE5ITH")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "tracking")
LISTEN_HOST     = os.getenv("LISTEN_HOST",     "127.0.0.1")
LISTEN_PORT     = int(os.getenv("LISTEN_PORT", "8787"))
CORS_ORIGIN     = os.getenv("CORS_ORIGIN",     "*")
LOG_LEVEL       = os.getenv("LOG_LEVEL",       "INFO")
MAX_HOURS       = float(os.getenv("MAX_HOURS", "24"))
AC_DB_URL       = os.getenv("AC_DB_URL", "https://downloads.adsbexchange.com/downloads/basic-ac-db.json.gz")
AC_DB_REFRESH   = int(os.getenv("AC_DB_REFRESH_HOURS", "24")) * 3600

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("tracking-api")

# ---------------------------------------------------------------------------
# ADS-B Exchange aircraft database
# ---------------------------------------------------------------------------

_ac_db: dict = {}
_ac_db_lock = threading.Lock()


def _load_ac_db() -> None:
    global _ac_db
    log.info("Loading aircraft DB from %s ...", AC_DB_URL)
    try:
        req = Request(AC_DB_URL, headers={"User-Agent": "oe5ith-tracking-api/1.0"})
        with urlopen(req, timeout=60) as resp:
            compressed = resp.read()
        raw = gzip.decompress(compressed).decode("utf-8")

        db = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            icao = entry.get("icao", "")
            if not icao:
                continue
            db[icao.upper()] = {
                "registration": entry.get("reg", ""),
                "type":         entry.get("short_type", "") or entry.get("icaotype", ""),
                "desc":         entry.get("model", "") or entry.get("manufacturer", ""),
                "operator":     entry.get("ownop", ""),
                "military":     entry.get("mil", False),
                "year":         entry.get("year", ""),
            }

        with _ac_db_lock:
            _ac_db = db

        log.info("Aircraft DB loaded: %d entries", len(db))

    except Exception as e:
        log.warning("Failed to load aircraft DB: %s", e)


def _ac_db_refresh_loop() -> None:
    while True:
        time.sleep(AC_DB_REFRESH)
        _load_ac_db()


def ac_lookup(icao: str) -> dict:
    with _ac_db_lock:
        return _ac_db.get(icao.upper(), {})


# ---------------------------------------------------------------------------
# InfluxDB Flux query helper
# ---------------------------------------------------------------------------

def flux_query(query: str) -> list[dict]:
    url = f"{INFLUXDB_URL}/api/v2/query?org={INFLUXDB_ORG}"
    payload = json.dumps({"query": query, "type": "flux"}).encode()

    req = Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Token {INFLUXDB_TOKEN}")
    req.add_header("Content-Type",  "application/json")
    req.add_header("Accept",        "application/csv")

    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except URLError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            pass
        log.error("InfluxDB query failed: %s | %s", e, body)
        raise RuntimeError(f"InfluxDB error: {e} | {body}") from e

    return _parse_flux_csv(raw)


def _parse_flux_csv(csv_text: str) -> list[dict]:
    rows = []
    headers = []
    for line in csv_text.splitlines():
        if not line:
            continue
        if line.startswith("#"):
            continue
        parts = line.split(",")
        if parts[0] == "" and len(parts) > 1 and parts[1] == "result":
            headers = parts
            continue
        if parts[0] == "" and headers:
            row = dict(zip(headers, parts))
            for k in ("", "result", "table"):
                row.pop(k, None)
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

def _clamp_hours(hours: float) -> float:
    return max(0.0167, min(hours, MAX_HOURS))

def _flux_duration(hours: float) -> str:
    minutes = max(1, round(hours * 60))
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# Aircraft queries
# ---------------------------------------------------------------------------

def query_aircraft_latest(hours: float) -> list[dict]:
    h = _clamp_hours(hours)
    dur = _flux_duration(h)
    q = f"""
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -{dur})
  |> filter(fn: (r) => r._measurement == "aircraft")
  |> filter(fn: (r) => r._field == "Lat" or r._field == "Long" or
                        r._field == "Alt" or r._field == "Spd" or
                        r._field == "Trak" or r._field == "Vsi")
  |> last()
  |> pivot(rowKey: ["_time", "Icao"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "Icao", "Call", "Lat", "Long", "Alt", "Spd", "Trak", "Vsi"])
"""
    rows = flux_query(q)
    out = []
    for r in rows:
        try:
            icao = r.get("Icao", "")
            db   = ac_lookup(icao)
            out.append({
                "icao":         icao,
                "callsign":     r.get("Call", ""),
                "registration": db.get("registration", ""),
                "type":         db.get("type", ""),
                "desc":         db.get("desc", ""),
                "operator":     db.get("operator", ""),
                "military":     db.get("military", False),
                "year":         db.get("year", ""),
                "lat":          _f(r.get("Lat")),
                "lon":          _f(r.get("Long")),
                "alt":          _f(r.get("Alt")),
                "speed":        _f(r.get("Spd")),
                "track":        _f(r.get("Trak")),
                "vsi":          _f(r.get("Vsi")),
                "time":         r.get("_time", ""),
            })
        except (ValueError, KeyError):
            continue
    return [x for x in out if x["lat"] is not None and x["lon"] is not None]


def query_aircraft_track(icao: str, hours: float) -> dict:
    h = _clamp_hours(hours)
    dur = _flux_duration(h)
    icao_safe = icao.upper().replace('"', "")
    q = f"""
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -{dur})
  |> filter(fn: (r) => r._measurement == "aircraft")
  |> filter(fn: (r) => r.Icao == "{icao_safe}")
  |> filter(fn: (r) => r._field == "Lat" or r._field == "Long" or
                        r._field == "Alt" or r._field == "Spd" or r._field == "Trak")
  |> pivot(rowKey: ["_time", "Icao"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "Lat", "Long", "Alt", "Spd", "Trak"])
  |> sort(columns: ["_time"])
"""
    rows = flux_query(q)
    db   = ac_lookup(icao)
    out  = []
    for r in rows:
        try:
            out.append({
                "time":  r.get("_time", ""),
                "lat":   _f(r["Lat"]),
                "lon":   _f(r["Long"]),
                "alt":   _f(r.get("Alt")),
                "speed": _f(r.get("Spd")),
                "track": _f(r.get("Trak")),
            })
        except (ValueError, KeyError):
            continue
    track = [x for x in out if x["lat"] is not None and x["lon"] is not None]
    return {
        "icao":         icao.upper(),
        "registration": db.get("registration", ""),
        "type":         db.get("type", ""),
        "desc":         db.get("desc", ""),
        "operator":     db.get("operator", ""),
        "military":     db.get("military", False),
        "year":         db.get("year", ""),
        "track":        track,
    }


# ---------------------------------------------------------------------------
# Vessel queries
# ---------------------------------------------------------------------------

def query_vessels_latest(hours: float) -> list[dict]:
    h = _clamp_hours(hours)
    dur = _flux_duration(h)
    q = f"""
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -{dur})
  |> filter(fn: (r) => r._measurement == "vessel")
  |> filter(fn: (r) => r._field == "lat" or r._field == "lon" or
                        r._field == "speed" or r._field == "course" or
                        r._field == "heading" or r._field == "status")
  |> last()
  |> pivot(rowKey: ["_time", "mmsi"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "mmsi", "shipname", "shiptype", "country",
                     "lat", "lon", "speed", "course", "heading", "status"])
"""
    rows = flux_query(q)
    out = []
    for r in rows:
        try:
            out.append({
                "mmsi":    r.get("mmsi", ""),
                "name":    r.get("shipname", ""),
                "type":    r.get("shiptype", ""),
                "country": r.get("country", ""),
                "lat":     _f(r.get("lat")),
                "lon":     _f(r.get("lon")),
                "speed":   _f(r.get("speed")),
                "course":  _f(r.get("course")),
                "heading": _f(r.get("heading")),
                "status":  _i(r.get("status")),
                "time":    r.get("_time", ""),
            })
        except (ValueError, KeyError):
            continue
    return [x for x in out if x["lat"] is not None and x["lon"] is not None]


def query_vessel_track(mmsi: str, hours: float) -> list[dict]:
    h = _clamp_hours(hours)
    dur = _flux_duration(h)
    mmsi_safe = mmsi.replace('"', "")
    q = f"""
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -{dur})
  |> filter(fn: (r) => r._measurement == "vessel")
  |> filter(fn: (r) => r.mmsi == "{mmsi_safe}")
  |> filter(fn: (r) => r._field == "lat" or r._field == "lon" or
                        r._field == "speed" or r._field == "course")
  |> pivot(rowKey: ["_time", "mmsi"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "lat", "lon", "speed", "course"])
  |> sort(columns: ["_time"])
"""
    rows = flux_query(q)
    out = []
    for r in rows:
        try:
            out.append({
                "time":   r.get("_time", ""),
                "lat":    _f(r["lat"]),
                "lon":    _f(r["lon"]),
                "speed":  _f(r.get("speed")),
                "course": _f(r.get("course")),
            })
        except (ValueError, KeyError):
            continue
    return [x for x in out if x["lat"] is not None and x["lon"] is not None]


# ---------------------------------------------------------------------------
# Stats queries
# ---------------------------------------------------------------------------

def _parse_duration_min(first_seen: str, last_seen: str):
    try:
        fmt = "%Y-%m-%dT%H:%M:%S"
        t0 = datetime.strptime(first_seen[:19], fmt)
        t1 = datetime.strptime(last_seen[:19],  fmt)
        return round((t1 - t0).total_seconds() / 60, 1)
    except Exception:
        return None


def query_stats_aircraft(hours: float, limit: int = 20) -> dict:
    h   = _clamp_hours(hours)
    dur = _flux_duration(h)
    q = f"""
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -{dur})
  |> filter(fn: (r) => r._measurement == "aircraft")
  |> filter(fn: (r) => r._field == "Lat")
  |> group(columns: ["Icao"])
  |> reduce(
      identity: {{count: 0, first_seen: "", last_seen: ""}},
      fn: (r, accumulator) => ({{
          count:      accumulator.count + 1,
          first_seen: if accumulator.first_seen == "" or r._time < accumulator.first_seen
                      then string(v: r._time) else accumulator.first_seen,
          last_seen:  if accumulator.last_seen == "" or r._time > accumulator.last_seen
                      then string(v: r._time) else accumulator.last_seen,
      }})
  )
  |> keep(columns: ["Icao", "count", "first_seen", "last_seen"])
"""
    rows = flux_query(q)

    entries = []
    for r in rows:
        icao = r.get("Icao", "")
        if not icao:
            continue
        db  = ac_lookup(icao)
        first_seen = r.get("first_seen", "")
        last_seen  = r.get("last_seen",  "")
        entries.append({
            "icao":         icao,
            "registration": db.get("registration", ""),
            "type":         db.get("type", ""),
            "desc":         db.get("desc", ""),
            "operator":     db.get("operator", ""),
            "military":     db.get("military", False),
            "count":        _i(r.get("count")) or 0,
            "first_seen":   first_seen,
            "last_seen":    last_seen,
            "duration_min": _parse_duration_min(first_seen, last_seen),
        })

    top_count    = sorted(entries, key=lambda x: x["count"],                          reverse=True)[:limit]
    top_duration = sorted([e for e in entries if e["duration_min"] is not None],
                          key=lambda x: x["duration_min"],                             reverse=True)[:limit]

    return {
        "hours":        hours,
        "unique_count": len(entries),
        "top_count":    top_count,
        "top_duration": top_duration,
    }


def query_stats_vessels(hours: float, limit: int = 20) -> dict:
    h   = _clamp_hours(hours)
    dur = _flux_duration(h)
    q = f"""
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -{dur})
  |> filter(fn: (r) => r._measurement == "vessel")
  |> filter(fn: (r) => r._field == "lat")
  |> group(columns: ["mmsi", "shipname", "country", "shiptype"])
  |> reduce(
      identity: {{count: 0, first_seen: "", last_seen: ""}},
      fn: (r, accumulator) => ({{
          count:      accumulator.count + 1,
          first_seen: if accumulator.first_seen == "" or r._time < accumulator.first_seen
                      then string(v: r._time) else accumulator.first_seen,
          last_seen:  if accumulator.last_seen == "" or r._time > accumulator.last_seen
                      then string(v: r._time) else accumulator.last_seen,
      }})
  )
  |> keep(columns: ["mmsi", "shipname", "country", "shiptype", "count", "first_seen", "last_seen"])
"""
    rows = flux_query(q)

    entries = []
    for r in rows:
        mmsi = r.get("mmsi", "")
        if not mmsi:
            continue
        first_seen = r.get("first_seen", "")
        last_seen  = r.get("last_seen",  "")
        entries.append({
            "mmsi":         mmsi,
            "name":         r.get("shipname", ""),
            "country":      r.get("country", ""),
            "type":         r.get("shiptype", ""),
            "count":        _i(r.get("count")) or 0,
            "first_seen":   first_seen,
            "last_seen":    last_seen,
            "duration_min": _parse_duration_min(first_seen, last_seen),
        })

    top_count    = sorted(entries, key=lambda x: x["count"],                          reverse=True)[:limit]
    top_duration = sorted([e for e in entries if e["duration_min"] is not None],
                          key=lambda x: x["duration_min"],                             reverse=True)[:limit]

    return {
        "hours":        hours,
        "unique_count": len(entries),
        "top_count":    top_count,
        "top_duration": top_duration,
    }


# ---------------------------------------------------------------------------
# Type helpers
# ---------------------------------------------------------------------------

def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "", "null") else None
    except (ValueError, TypeError):
        return None

def _i(v) -> int | None:
    try:
        return int(float(v)) if v not in (None, "", "null") else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.debug("HTTP %s", fmt % args)

    def _get_hours(self, qs: dict, default: float = 1.0) -> float:
        try:
            return float(qs.get("hours", [default])[0])
        except (ValueError, TypeError):
            return default

    def _get_int(self, qs: dict, key: str, default: int) -> int:
        try:
            return int(qs.get(key, [default])[0])
        except (ValueError, TypeError):
            return default

    def _send_json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type",   "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Cache-Control",  "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, msg: str):
        self._send_json({"error": msg}, status)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin",  CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        qs     = parse_qs(parsed.query)

        try:
            if path == "/api/health":
                with _ac_db_lock:
                    db_size = len(_ac_db)
                self._send_json({
                    "status": "ok",
                    "time":   datetime.now(timezone.utc).isoformat(),
                    "ac_db_entries": db_size,
                })

            elif path == "/api/aircraft":
                hours = self._get_hours(qs, 1.0)
                data  = query_aircraft_latest(hours)
                self._send_json({"hours": hours, "count": len(data), "aircraft": data})

            elif path.startswith("/api/aircraft/") and path.endswith("/track"):
                icao  = path.split("/")[3]
                hours = self._get_hours(qs, 1.0)
                data  = query_aircraft_track(icao, hours)
                self._send_json({"hours": hours, "count": len(data["track"]), **data})

            elif path == "/api/stats/aircraft":
                hours = self._get_hours(qs, 24.0)
                limit = self._get_int(qs, "limit", 20)
                data  = query_stats_aircraft(hours, limit)
                self._send_json(data)

            elif path == "/api/vessels":
                hours = self._get_hours(qs, 1.0)
                data  = query_vessels_latest(hours)
                self._send_json({"hours": hours, "count": len(data), "vessels": data})

            elif path.startswith("/api/vessels/") and path.endswith("/track"):
                mmsi  = path.split("/")[3]
                hours = self._get_hours(qs, 1.0)
                data  = query_vessel_track(mmsi, hours)
                self._send_json({"mmsi": mmsi, "hours": hours, "count": len(data), "track": data})

            elif path == "/api/stats/vessels":
                hours = self._get_hours(qs, 24.0)
                limit = self._get_int(qs, "limit", 20)
                data  = query_stats_vessels(hours, limit)
                self._send_json(data)

            else:
                self._error(404, "Not found")

        except RuntimeError as e:
            self._error(502, str(e))
        except Exception as e:
            log.exception("Unhandled error")
            self._error(500, "Internal server error")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not INFLUXDB_TOKEN:
        log.error("INFLUXDB_TOKEN is not set. Exiting.")
        raise SystemExit(1)

    log.info("Tracking API starting on %s:%d", LISTEN_HOST, LISTEN_PORT)
    log.info("  InfluxDB: %s  org=%s  bucket=%s", INFLUXDB_URL, INFLUXDB_ORG, INFLUXDB_BUCKET)
    log.info("  Max query window: %.0fh", MAX_HOURS)

    threading.Thread(target=_load_ac_db,         daemon=True).start()
    threading.Thread(target=_ac_db_refresh_loop, daemon=True).start()

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
