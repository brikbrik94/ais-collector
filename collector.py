#!/usr/bin/env python3
"""
AIS-Catcher → InfluxDB v2 Collector

Polls the AIS-Catcher ships.json endpoint at a configurable interval
and writes vessel position data to InfluxDB v2.

Part of oe5ith-tracker-ingest
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# Configuration (via environment variables)
# ---------------------------------------------------------------------------

AIS_URL = os.getenv("AIS_URL", "http://100.64.0.3:8100/ships.json")
INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://127.0.0.1:8086")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN", "")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "OE5ITH")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "tracking")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))  # seconds
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("ais-collector")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

running = True


def shutdown_handler(signum, frame):
    global running
    log.info("Shutdown signal received, stopping...")
    running = False


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

# ---------------------------------------------------------------------------
# InfluxDB line protocol helpers
# ---------------------------------------------------------------------------


def escape_tag(value: str) -> str:
    """Escape special characters in tag values."""
    return value.replace(",", r"\,").replace("=", r"\=").replace(" ", r"\ ")


def escape_field_str(value: str) -> str:
    """Escape special characters in field string values."""
    return value.replace('"', r"\"").replace("\\", "\\\\")


def build_line_protocol(ship: dict, timestamp_ns: int) -> str | None:
    """
    Convert a single ship dict from AIS-Catcher to InfluxDB line protocol.

    Measurement: vessel
    Tags: mmsi, shipname, shiptype, mmsi_type, country
    Fields: lat, lon, speed, course, heading, status, draught,
            level (signal), count, distance, bearing
    """
    mmsi = ship.get("mmsi")
    lat = ship.get("lat")
    lon = ship.get("lon")

    # Skip entries without MMSI or position
    if mmsi is None or lat is None or lon is None:
        return None

    # Skip entries with zero/default position
    if lat == 0.0 and lon == 0.0:
        return None

    # --- Tags ---
    tags = [f"mmsi={escape_tag(str(mmsi))}"]

    shipname = ship.get("shipname", "").strip()
    if shipname:
        tags.append(f"shipname={escape_tag(shipname)}")

    country = ship.get("country", "").strip()
    if country:
        tags.append(f"country={escape_tag(country)}")

    shiptype = ship.get("shiptype")
    if shiptype is not None and shiptype != 0:
        tags.append(f"shiptype={escape_tag(str(shiptype))}")

    mmsi_type = ship.get("mmsi_type")
    if mmsi_type is not None:
        tags.append(f"mmsi_type={escape_tag(str(mmsi_type))}")

    shipclass = ship.get("shipclass")
    if shipclass is not None:
        tags.append(f"shipclass={escape_tag(str(shipclass))}")

    # --- Fields ---
    fields = [
        f"lat={lat}",
        f"lon={lon}",
    ]

    # Numeric fields — only add if present and not null
    numeric_fields = {
        "speed": "speed",
        "course": "course",
        "heading": "heading",
        "status": "status",
        "draught": "draught",
        "level": "signal_level",
        "count": "msg_count",
        "distance": "distance",
        "bearing": "bearing",
        "ppm": "ppm",
        "to_bow": "to_bow",
        "to_stern": "to_stern",
        "to_port": "to_port",
        "to_starboard": "to_starboard",
    }

    for json_key, field_name in numeric_fields.items():
        value = ship.get(json_key)
        if value is not None:
            fields.append(f"{field_name}={value}")

    # String fields
    callsign = ship.get("callsign", "").strip()
    if callsign:
        fields.append(f'callsign="{escape_field_str(callsign)}"')

    destination = ship.get("destination", "").strip()
    if destination:
        fields.append(f'destination="{escape_field_str(destination)}"')

    # --- Build line ---
    tag_str = ",".join(tags)
    field_str = ",".join(fields)

    return f"vessel,{tag_str} {field_str} {timestamp_ns}"


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def fetch_ships() -> list[dict]:
    """Fetch ship data from AIS-Catcher JSON endpoint."""
    try:
        req = Request(AIS_URL, headers={"Accept": "application/json"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("ships", [])
    except (URLError, json.JSONDecodeError, OSError) as e:
        log.warning("Failed to fetch AIS data: %s", e)
        return []


# ---------------------------------------------------------------------------
# InfluxDB writing
# ---------------------------------------------------------------------------


def write_to_influxdb(lines: list[str]) -> bool:
    """Write lines of line protocol to InfluxDB v2."""
    if not lines:
        return True

    payload = "\n".join(lines).encode("utf-8")

    params = urlencode({"org": INFLUXDB_ORG, "bucket": INFLUXDB_BUCKET, "precision": "ns"})
    url = f"{INFLUXDB_URL}/api/v2/write?{params}"

    req = Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Token {INFLUXDB_TOKEN}")
    req.add_header("Content-Type", "text/plain; charset=utf-8")

    try:
        with urlopen(req, timeout=10) as resp:
            if resp.status == 204:
                return True
            else:
                log.warning("InfluxDB write returned status %d", resp.status)
                return False
    except URLError as e:
        log.error("Failed to write to InfluxDB: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    if not INFLUXDB_TOKEN:
        log.error("INFLUXDB_TOKEN is not set. Exiting.")
        sys.exit(1)

    log.info("AIS Collector starting")
    log.info("  AIS source:    %s", AIS_URL)
    log.info("  InfluxDB:      %s", INFLUXDB_URL)
    log.info("  Org/Bucket:    %s / %s", INFLUXDB_ORG, INFLUXDB_BUCKET)
    log.info("  Poll interval: %ds", POLL_INTERVAL)

    consecutive_errors = 0

    while running:
        timestamp_ns = int(time.time() * 1e9)

        ships = fetch_ships()

        if ships:
            lines = []
            for ship in ships:
                line = build_line_protocol(ship, timestamp_ns)
                if line:
                    lines.append(line)

            if lines:
                success = write_to_influxdb(lines)
                if success:
                    log.debug("Written %d vessel positions to InfluxDB", len(lines))
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    log.warning("Write failed (%d consecutive errors)", consecutive_errors)
            else:
                log.debug("No valid vessel positions to write")
        else:
            log.debug("No ships received from AIS-Catcher")

        # Sleep in small increments for responsive shutdown
        for _ in range(POLL_INTERVAL * 10):
            if not running:
                break
            time.sleep(0.1)

    log.info("AIS Collector stopped")


if __name__ == "__main__":
    main()
