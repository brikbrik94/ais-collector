# oe5ith-tracker-ingest

Sammelt ADS-B Flugzeug- und AIS Schiffsdaten und speichert sie in InfluxDB v2 zur historischen Auswertung und Visualisierung. Stellt die Daten zusätzlich über eine einfache JSON-API für Webfrontends bereit.

## Architektur

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
                                                                        │
                                                                   ┌────▼────────┐
                                                                   │ tracking-   │
                                                                   │ api         │
                                                                   │ (Python)    │
                                                                   └────┬────────┘
                                                                        │ JSON
                                                                   ┌────▼────────┐
                                                                   │  Browser /  │
                                                                   │  MapLibre   │
                                                                   └─────────────┘
```

## Komponenten

### ADS-B → InfluxDB (via readsb built-in Telegraf)

Der `docker-readsb-protobuf` Container enthält Telegraf und schreibt direkt in InfluxDB wenn die entsprechenden Umgebungsvariablen gesetzt sind. Keine zusätzliche Software notwendig.

**Umgebungsvariablen in `/opt/adsb/docker-compose.yml`:**

```yaml
- INFLUXDBURL=http://172.17.0.1:8086
- INFLUXDB_V2=true
- INFLUXDB_V2_BUCKET=tracking
- INFLUXDB_V2_TOKEN=<token>
- INFLUXDB_V2_ORG=OE5ITH
```

**Geschriebene Measurements:** `aircraft`, `readsb`

---

### AIS → InfluxDB (`ais-collector`)

Leichtgewichtiger Python-Dienst der den AIS-Catcher `ships.json`-Endpoint pollt und Schiffspositionen per Line Protocol in InfluxDB v2 schreibt.

- Keine externen Abhängigkeiten (nur Python stdlib)
- Konfigurierbares Poll-Intervall
- Läuft als systemd-Service in einem Python venv
- Graceful Shutdown

**Geschriebenes Measurement:** `vessel`

**Installation:**
```bash
chmod +x install.sh
./install.sh

sudo nano /etc/ais-collector.env   # INFLUXDB_TOKEN setzen

sudo systemctl enable --now ais-collector
sudo journalctl -u ais-collector -f
```

---

### InfluxDB → JSON API (`api`)

Python HTTP-Server der InfluxDB-Daten als einfache read-only JSON-API für Webfrontends bereitstellt. Der InfluxDB-Token bleibt serverseitig – kein Credentials-Leak ins Frontend.

- Keine externen Abhängigkeiten (nur Python stdlib)
- CORS-Header konfigurierbar
- Läuft als systemd-Service in einem Python venv
- `?hours=N` Parameter für alle Endpoints (min: ~1min, max: konfigurierbar)

**Installation:**
```bash
cd api
chmod +x install.sh
./install.sh

sudo nano /etc/tracking-api.env    # INFLUXDB_TOKEN und CORS_ORIGIN setzen

sudo systemctl enable --now tracking-api
sudo journalctl -u tracking-api -f
```

**Nginx Reverse Proxy** (empfohlen – API hinter HTTPS stellen):
```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8787;
}
```

---

## API Endpoints

Basis-URL: `http://127.0.0.1:8787` (lokal) bzw. via nginx `https://karte.oe5ith.at/api/`

| Method | Endpoint | Parameter | Beschreibung |
|--------|----------|-----------|--------------|
| GET | `/api/health` | – | Healthcheck |
| GET | `/api/aircraft` | `hours=1` | Letzte Position aller Flugzeuge |
| GET | `/api/aircraft/{icao}/track` | `hours=1` | Positionstrack eines Flugzeugs |
| GET | `/api/vessels` | `hours=1` | Letzte Position aller Schiffe |
| GET | `/api/vessels/{mmsi}/track` | `hours=1` | Positionstrack eines Schiffes |

**Beispiele:**
```bash
# Alle Flugzeuge der letzten 30 Minuten
curl "http://127.0.0.1:8787/api/aircraft?hours=0.5"

# Track eines Flugzeugs über 2 Stunden
curl "http://127.0.0.1:8787/api/aircraft/3C6586/track?hours=2"

# Alle Schiffe der letzten Stunde
curl "http://127.0.0.1:8787/api/vessels?hours=1"

# Track eines Schiffes über 6 Stunden
curl "http://127.0.0.1:8787/api/vessels/203244088/track?hours=6"
```

**Beispiel Response `/api/aircraft?hours=1`:**
```json
{
  "hours": 1.0,
  "count": 42,
  "aircraft": [
    {
      "icao": "3C6586",
      "callsign": "AUA100",
      "lat": 48.2372,
      "lon": 14.1823,
      "alt": 32000.0,
      "speed": 420.0,
      "track": 275.0,
      "vsi": -64.0,
      "time": "2026-03-02T08:42:00Z"
    }
  ]
}
```

---

## InfluxDB Schema

### Measurement: `aircraft` (geschrieben von readsb/Telegraf)

| Typ   | Key   | Beschreibung             |
|-------|-------|--------------------------|
| Tag   | Icao  | ICAO Hex-Code            |
| Tag   | Call  | Callsign                 |
| Field | Alt   | Barometrische Höhe (ft)  |
| Field | GAlt  | Geometrische Höhe (ft)   |
| Field | Lat   | Breitengrad              |
| Field | Long  | Längengrad               |
| Field | Spd   | Groundspeed (kts)        |
| Field | Trak  | Track/Heading (°)        |
| Field | Vsi   | Vertikalgeschwindigkeit (ft/min) |
| Field | Sig   | Signalpegel              |

### Measurement: `vessel` (geschrieben von ais-collector)

| Typ   | Key          | Beschreibung             |
|-------|--------------|--------------------------|
| Tag   | mmsi         | MMSI Kennung             |
| Tag   | shipname     | Schiffsname              |
| Tag   | country      | Flaggenstaat             |
| Tag   | shiptype     | Schiffstyp-Code          |
| Field | lat          | Breitengrad              |
| Field | lon          | Längengrad               |
| Field | speed        | Fahrt über Grund (kts)   |
| Field | course       | Kurs über Grund (°)      |
| Field | heading      | Rechtweisender Kurs (°)  |
| Field | status       | Navigationsstatus        |
| Field | signal_level | Empfangspegel            |
| Field | distance     | Entfernung zur Station   |

### Measurement: `readsb` (geschrieben von readsb/Telegraf)

Feeder-Statistiken: Nachrichten/Sek, Flugzeuganzahl, Signalpegel, CPU-Auslastung, etc.

---

## Datenvolumen

- **ADS-B:** ~17 MB/Stunde (~400 MB/Tag) mit vollständigem Positions-Logging
- **AIS:** abhängig vom Schiffsverkehr, ca. 1–5 MB/Tag

---

## Verzeichnisstruktur

```
oe5ith-tracker-ingest/
├── README.md
├── collector.py                # AIS → InfluxDB Collector
├── ais-collector.service       # systemd Unit
├── ais-collector.env.example
├── install.sh                  # Installation ais-collector
└── api/
    ├── README.md
    ├── api.py                  # InfluxDB → JSON API
    ├── tracking-api.service    # systemd Unit
    ├── tracking-api.env.example
    └── install.sh              # Installation tracking-api
```

---

## Lizenz

MIT
