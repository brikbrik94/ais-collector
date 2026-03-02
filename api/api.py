#!/usr/bin/env python3
"""
InfluxDB v2 → HTTP JSON API

Exposes aircraft and vessel tracking data from InfluxDB as a simple
read-only JSON API for use by map frontends (e.g. karte.oe5ith.at).

Part of oe5ith-tracker-ingest
"""

import json
import logging
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError

# ---------------------------------------------------------------------------
# Configuration (via environment variables)
# ---------------------------------------------------------------------------

INFLUXDB_URL   = os.getenv("INFLUXDB_URL",   "http://127.0.0.1:8086")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN", "")
INFLUXDB_ORG   = os.getenv("INFLUXDB_ORG",   "OE5ITH")
INFLUXDB_BUCKET= os.getenv("INFLUXDB_BUCKET","tracking")
LISTEN_HOST    = os.getenv("LISTEN_HOST",    "127.0.0.1")
LISTEN_PORT    = int(os.getenv("LISTEN_PORT", "8787"))
CORS_ORIGIN    = os.getenv("CORS_ORIGIN",    "*")   # restrict in prod e.g. "https://karte.oe5ith.at"
LOG_LEVEL      = os.getenv("LOG_LEVEL",      "INFO")
MAX_HOURS      = float(os.getenv("MAX_HOURS", "24")) # safety cap

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("tracking-api")

# ---------------------------------------------------------------------------
# InfluxDB Flux query helper
# ---------------------------------------------------------------------------

def flux_query(query: str) -> list[dict]:
    """
    Run a Flux query against InfluxDB v2 and return rows as a list of dicts.
    Uses the CSV response format and parses it manually (no external deps).
    """
    url = f"{INFLUXDB_URL}/api/v2/query?org={INFLUXDB_ORG}"
    payload = json.dumps({"query": query, "type": "flux"}).encode()

    req = Request(url, data=payload, method="POST")
    req.add_header("Authorization",  f"Token {INFLUXDB_TOKEN}")
    req.add_header("Content-Type",   "application/json")
    req.add_header("Accept",         "application/csv")

    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except URLError as e:
        log.error("InfluxDB query failed: %s", e)
        raise RuntimeError(f"InfluxDB unreachable: {e}") from e

    return _parse_flux_csv(raw)


def _parse_flux_csv(csv_text: str) -> list[dict]:
    """Parse InfluxDB annotated CSV into a list of plain dicts."""
    rows = []
    headers = []
    for line in csv_text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if parts[0] == "":  # annotation rows start with empty first cell
            continue
        if not headers:
            headers = parts
            continue
        row = dict(zip(headers, parts))
        # Remove InfluxDB internal columns
        for k in ("", "result", "table"):
            row.pop(k, None)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Query builders
# ---------------------------------------------------------------------------

def _clamp_hours(hours: float) -> float:
    return max(0.0167, min(hours, MAX_HOURS))  # min ~1 min, max cap


def query_aircraft_latest(hours: float) -> list[dict]:
    """Last known position of every aircraft seen in the time window."""
    h = _clamp_hours(hours)
    q = f"""
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -{h}h)
  |> filter(fn: (r) => r._measurement == "aircraft")
  |> filter(fn: (r) => r._field == "Lat" or r._field == "Long" or
                        r._field == "Alt" or r._field == "Spd" or
                        r._field == "Trak" or r._field == "Vsi")
  |> last()
  |> pivot(rowKey: ["_time", "Icao", "Call"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "Icao", "Call", "Lat", "Long", "Alt", "Spd", "Trak", "Vsi"])
"""
    rows = flux_query(q)
    out = []
    for r in rows:
        try:
            out.append({
                "icao":      r.get("Icao", ""),
                "callsign":  r.get("Call", ""),
                "lat":       _f(r.get("Lat")),
                "lon":       _f(r.get("Long")),
                "alt":       _f(r.get("Alt")),
                "speed":     _f(r.get("Spd")),
                "track":     _f(r.get("Trak")),
                "vsi":       _f(r.get("Vsi")),
                "time":      r.get("_time", ""),
            })
        except (ValueError, KeyError):
            continue
    return [x for x in out if x["lat"] is not None and x["lon"] is not None]


def query_aircraft_track(icao: str, hours: float) -> list[dict]:
    """Full position track for a single aircraft."""
    h = _clamp_hours(hours)
    icao_safe = icao.upper().replace('"', "")
    q = f"""
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -{h}h)
  |> filter(fn: (r) => r._measurement == "aircraft")
  |> filter(fn: (r) => r.Icao == "{icao_safe}")
  |> filter(fn: (r) => r._field == "Lat" or r._field == "Long" or
                        r._field == "Alt" or r._field == "Spd" or r._field == "Trak")
  |> pivot(rowKey: ["_time", "Icao", "Call"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "Lat", "Long", "Alt", "Spd", "Trak"])
  |> sort(columns: ["_time"])
"""
    rows = flux_query(q)
    out = []
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
    return [x for x in out if x["lat"] is not None and x["lon"] is not None]


def query_vessels_latest(hours: float) -> list[dict]:
    """Last known position of every vessel seen in the time window."""
    h = _clamp_hours(hours)
    q = f"""
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -{h}h)
  |> filter(fn: (r) => r._measurement == "vessel")
  |> filter(fn: (r) => r._field == "lat" or r._field == "lon" or
                        r._field == "speed" or r._field == "course" or
                        r._field == "heading" or r._field == "status")
  |> last()
  |> pivot(rowKey: ["_time", "mmsi", "shipname", "shiptype", "country"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "mmsi", "shipname", "shiptype", "country",
                     "lat", "lon", "speed", "course", "heading", "status"])
"""
    rows = flux_query(q)
    out = []
    for r in rows:
        try:
            out.append({
                "mmsi":     r.get("mmsi", ""),
                "name":     r.get("shipname", ""),
                "type":     r.get("shiptype", ""),
                "country":  r.get("country", ""),
                "lat":      _f(r.get("lat")),
                "lon":      _f(r.get("lon")),
                "speed":    _f(r.get("speed")),
                "course":   _f(r.get("course")),
                "heading":  _f(r.get("heading")),
                "status":   _i(r.get("status")),
                "time":     r.get("_time", ""),
            })
        except (ValueError, KeyError):
            continue
    return [x for x in out if x["lat"] is not None and x["lon"] is not None]


def query_vessel_track(mmsi: str, hours: float) -> list[dict]:
    """Full position track for a single vessel."""
    h = _clamp_hours(hours)
    mmsi_safe = mmsi.replace('"', "")
    q = f"""
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -{h}h)
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

ROUTES = {
    # method  path-prefix                  handler
    "/api/aircraft":         "aircraft_latest",
    "/api/aircraft/":        "aircraft_track",   # /api/aircraft/{icao}/track
    "/api/vessels":          "vessels_latest",
    "/api/vessels/":         "vessel_track",      # /api/vessels/{mmsi}/track
    "/api/health":           "health",
}

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.debug("HTTP %s", fmt % args)

    def _get_hours(self, qs: dict, default: float = 1.0) -> float:
        try:
            return float(qs.get("hours", [default])[0])
        except (ValueError, TypeError):
            return default

    def _send_json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, msg: str):
        self._send_json({"error": msg}, status)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        qs     = parse_qs(parsed.query)

        try:
            # --- Health ---
            if path == "/api/health":
                self._send_json({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})

            # --- Aircraft latest ---
            elif path == "/api/aircraft":
                hours = self._get_hours(qs, 1.0)
                data  = query_aircraft_latest(hours)
                self._send_json({"hours": hours, "count": len(data), "aircraft": data})

            # --- Aircraft track: /api/aircraft/{icao}/track ---
            elif path.startswith("/api/aircraft/") and path.endswith("/track"):
                icao  = path.split("/")[3]
                hours = self._get_hours(qs, 1.0)
                data  = query_aircraft_track(icao, hours)
                self._send_json({"icao": icao, "hours": hours, "count": len(data), "track": data})

            # --- Vessels latest ---
            elif path == "/api/vessels":
                hours = self._get_hours(qs, 1.0)
                data  = query_vessels_latest(hours)
                self._send_json({"hours": hours, "count": len(data), "vessels": data})

            # --- Vessel track: /api/vessels/{mmsi}/track ---
            elif path.startswith("/api/vessels/") and path.endswith("/track"):
                mmsi  = path.split("/")[3]
                hours = self._get_hours(qs, 1.0)
                data  = query_vessel_track(mmsi, hours)
                self._send_json({"mmsi": mmsi, "hours": hours, "count": len(data), "track": data})

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

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
