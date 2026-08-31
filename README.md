# Kart Performance Monitor

Dashboard locale per telemetria kart:

- Garmin GLO 2 → ESP32 via Bluetooth Classic
- ESP32 → MQTT via Wi-Fi
- Mac → ricezione MQTT, sessione, telemetria JSON, dashboard HTTP
- iPhone 14 Pro → display landscape sul volante

## Avvio

```bash
source .venv/bin/activate
python app.py
```

Dashboard locale:

```text
http://127.0.0.1:8080
```

Dalla stessa rete Wi-Fi, su iPhone:

```text
http://IP_DEL_MAC:8080
```

## MQTT payload atteso

```json
{
  "satellites_used": 6,
  "latitude": 37.52084172,
  "longitude": 15.06603652,
  "speed_kmph": 0.26,
  "fix_valid": true,
  "fix_quality": 2,
  "fix_type": 3,
  "hdop": 1.2
}
```

## Stato prima release

- [x] MQTT subscriber
- [x] Dashboard mobile/landscape
- [x] Selezione pilota
- [x] Start / pausa / riprendi / stop
- [x] Telemetria GPS salvata durante sessione
- [x] Autosave JSON
- [ ] Start/finish virtuale
- [ ] Giro corrente e best lap
- [ ] Settori
- [ ] Theoretical best
- [ ] Pit lane logic
- [ ] Debug map/calibrazione pista
