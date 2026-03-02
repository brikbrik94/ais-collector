# tracking-api

Liest Flugzeug- und Schiffsdaten aus InfluxDB v2 und stellt sie als einfache read-only JSON-API bereit.

Kein externes Framework, keine pip-Dependencies – nur Python stdlib.

## Installation

```bash
chmod +x install.sh
./install.sh

# Token und CORS setzen
sudo nano /etc/tracking-api.env

sudo systemctl enable --now tracking-api
sudo journalctl -u tracking-api -f
```

## Konfiguration (`/etc/tracking-api.env`)

| Variable | Default | Beschreibung |
|----------|---------|--------------|
| `INFLUXDB_URL` | `http://127.0.0.1:8086` | InfluxDB Adresse |
| `INFLUXDB_TOKEN` | – | API Token (read-only reicht) |
| `INFLUXDB_ORG` | `OE5ITH` | Organisation |
| `INFLUXDB_BUCKET` | `tracking` | Bucket |
| `LISTEN_HOST` | `127.0.0.1` | Bind-Adresse (für VPN: `0.0.0.0`) |
| `LISTEN_PORT` | `8787` | Port |
| `CORS_ORIGIN` | `*` | CORS Origin (in Prod einschränken) |
| `MAX_HOURS` | `24` | Maximales Abfragefenster in Stunden |
| `LOG_LEVEL` | `INFO` | Log-Level |

## Endpoints

| Endpoint | Parameter | Beschreibung |
|----------|-----------|--------------|
| `GET /api/health` | – | Healthcheck |
| `GET /api/aircraft` | `?hours=1` | Letzte Position aller Flugzeuge |
| `GET /api/aircraft/{icao}/track` | `?hours=1` | Track eines Flugzeugs |
| `GET /api/vessels` | `?hours=1` | Letzte Position aller Schiffe |
| `GET /api/vessels/{mmsi}/track` | `?hours=1` | Track eines Schiffes |

Der `hours`-Parameter akzeptiert Dezimalwerte: `0.5` = 30 Minuten, `2` = 2 Stunden, `12` = 12 Stunden.

## Nginx

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8787;
}
```

## Bekannte Eigenheiten

- InfluxDB annotated CSV kann mehrere Table-Blöcke mit je eigenem Header enthalten. Der Parser erkennt Header-Zeilen explizit über `parts[1] == "result"` und setzt den Header bei jedem neuen Block zurück.
- Flux Duration-Strings akzeptieren keine Dezimalzahlen (z.B. `1.0h` ist ungültig). Der `hours`-Parameter wird daher intern in ganze Minuten umgerechnet (`60m`, `150m`, etc.).
- Tags wie `Call` (aircraft) oder `shipname` (vessel) sind nicht bei jedem Datensatz vorhanden und dürfen daher nicht im `pivot(rowKey: [...])` stehen.
