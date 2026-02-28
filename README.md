# oe5ith-tracker-ingest

Collects ADS-B aircraft and AIS vessel tracking data and stores it in InfluxDB v2 for historical analysis and visualization.

## Architecture

```
┌─────────────────┐     Beast/TCP      ┌──────────┐   built-in    ┌────────────┐
│ ADS-B Feeders   │ ──────────────────► │  readsb  │ ──Telegraf──► │            │
│ (RPi / Debian)  │   via Headscale    │ (Docker)  │              │            │
└─────────────────┘                     └──────────┘              │ InfluxDB   │
                                                                   │ v2         │
┌─────────────────┐     HTTP/JSON       ┌───────────────┐         │            │
│ AIS-Catcher     │ ──────────────────► │ ais-collector │ ───────► │            │
│ (RPi 4)         │   via Headscale    │ (Python)      │         │            │
└─────────────────┘                     └───────────────┘         └────────────┘
```

## Components

### ADS-B → InfluxDB (via readsb built-in Telegraf)

The `docker-readsb-protobuf` container includes Telegraf and writes directly to InfluxDB when configured with the appropriate environment variables. No additional software needed.

**Environment variables for `/opt/adsb/docker-compose.yml`:**

```yaml
- INFLUXDBURL=http://172.17.0.1:8086
- INFLUXDB_V2=true
- INFLUXDB_V2_BUCKET=tracking
- INFLUXDB_V2_TOKEN=<token>
- INFLUXDB_V2_ORG=OE5ITH
```

**Measurements written:** `aircraft`, `readsb`

### AIS → InfluxDB (ais-collector)

A lightweight Python script that polls the AIS-Catcher `ships.json` endpoint and writes vessel positions to InfluxDB v2 using the line protocol API.

- No external dependencies (Python stdlib only)
- Configurable poll interval
- Runs as a systemd service in a Python venv
- Graceful shutdown handling

**Measurement written:** `vessel`

## Installation

### Prerequisites

- InfluxDB v2 running with a `tracking` bucket
- API token with write access to the `tracking` bucket
- Python 3.11+

### AIS Collector

```bash
cd ais-collector
chmod +x install.sh
./install.sh

# Edit the config
sudo nano /etc/ais-collector.env

# Start the service
sudo systemctl enable --now ais-collector

# Check logs
sudo journalctl -u ais-collector -f
```

## InfluxDB Schema

### Measurement: `aircraft` (written by readsb/Telegraf)

| Type  | Key       | Description              |
|-------|-----------|--------------------------|
| Tag   | Icao      | ICAO hex code            |
| Tag   | Call      | Callsign                 |
| Field | Alt       | Barometric altitude (ft) |
| Field | GAlt      | Geometric altitude (ft)  |
| Field | Lat       | Latitude                 |
| Field | Long      | Longitude                |
| Field | Spd       | Ground speed (kts)       |
| Field | Trak      | Track/heading (°)        |
| Field | Vsi       | Vertical speed (ft/min)  |
| Field | Sig       | Signal level             |

### Measurement: `vessel` (written by ais-collector)

| Type  | Key          | Description              |
|-------|--------------|--------------------------|
| Tag   | mmsi         | MMSI identifier          |
| Tag   | shipname     | Vessel name              |
| Tag   | country      | Flag state               |
| Tag   | shiptype     | Ship type code           |
| Field | lat          | Latitude                 |
| Field | lon          | Longitude                |
| Field | speed        | Speed over ground (kts)  |
| Field | course       | Course over ground (°)   |
| Field | heading      | True heading (°)         |
| Field | status       | Navigation status        |
| Field | signal_level | Receive signal level     |
| Field | distance     | Distance from station    |

### Measurement: `readsb` (written by readsb/Telegraf)

Feeder statistics: messages/sec, aircraft count, signal levels, CPU usage, etc.

## Data Volume Estimates

- **ADS-B:** ~17 MB/hour (~400 MB/day) with full aircraft position logging
- **AIS:** depends on vessel traffic, estimated ~1-5 MB/day

## License

MIT
