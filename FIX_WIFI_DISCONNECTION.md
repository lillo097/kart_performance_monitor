# Fix WiFi Disconnection Issue

## Problema
Il Raspberry Pi si connette e disconnette continuamente dall'hotspot iPhone, causando:
- Disconnessioni WiFi ogni 4-5 minuti
- MQTT keepalive timeout (rc=141)
- Sistema inutilizzabile

## Cause Identificate

### 1. WiFi Keepalive Troppo Aggressivo
**File**: `raspi_session_tracker.py:153`
```python
WIFI_KEEPALIVE_INTERVAL_S = 5.0  # ❌ TROPPO FREQUENTE
```

**Problema**: Ping ogni 5s è eccessivo per iOS. L'iPhone interpreta questo come:
- Possibile attacco/scansione di rete
- Consumo batteria anomalo
- Trigger del power management che disconnette il client

### 2. MQTT Keepalive Troppo Basso
**File**: `raspi_session_tracker.py:108`
```python
MQTT_KEEPALIVE_S = 20  # ❌ TROPPO BASSO per WiFi instabile
```

**Problema**: Con WiFi che si disconnette, 20s non dà tempo al riconnect prima del timeout.

### 3. Retained Messages nel Server
**File**: `app.py:2559` e `app.py:2594`
```python
# Session control NON dovrebbe usare retain=True
retain=False  # ✅ Già fixato nel commit 5458719

# Session status USA retain=True
retain=True   # ⚠️ Causa loop se il RPi riceve vecchi messaggi
```

## Soluzioni

### Fix 1: Aumentare WiFi Keepalive Interval
```python
# raspi_session_tracker.py:153
WIFI_KEEPALIVE_INTERVAL_S = 30.0  # Da 5s → 30s
```

**Rationale**: 
- iOS hotspot rimane attivo con ping ogni 30-60s
- Riduce overhead di rete del 83%
- Compatibile con timeout standard iOS

### Fix 2: Aumentare MQTT Keepalive
```python
# raspi_session_tracker.py:108
MQTT_KEEPALIVE_S = 60  # Da 20s → 60s
```

**Rationale**:
- Allineato con app.py (keepalive_s: 60)
- Dà tempo per WiFi reconnect (10s) + buffer
- Standard MQTT per connessioni mobili

### Fix 3: Rimuovere Retain da Session Status
```python
# app.py:2594
retain=False  # Da True → False
```

**Rationale**:
- Evita che il RPi riceva lo stato vecchio al riavvio
- Elimina loop di start/stop indesiderati
- Lo stato deve essere effimero, non persistente

### Fix 4: Aumentare WiFi Check Interval (opzionale)
```python
# raspi_session_tracker.py:149
WIFI_CHECK_INTERVAL_S = 15.0  # Da 10s → 15s
```

**Rationale**: Riduce ulteriormente le chiamate di sistema

## Implementazione Consigliata

### Priority 1 (Critici - fare subito):
1. ✅ `WIFI_KEEPALIVE_INTERVAL_S = 30.0`
2. ✅ `MQTT_KEEPALIVE_S = 60`

### Priority 2 (Raccomandati):
3. ✅ `retain=False` per session_status_topic

### Testing
Dopo le modifiche, monitorare:
```bash
# Sul Raspberry Pi
journalctl -u kart-telemetry -f | grep -E "wifi|mqtt"

# Nel log di sessione
tail -f sessions/*/glo2-telemetry.log | grep -E "wifi|mqtt"
```

**Aspettative**:
- Nessun `wifi_disconnect` per almeno 30+ minuti
- Nessun `mqtt_disconnect rc=141`
- Connessione stabile durante tutta la sessione

## Note Aggiuntive

### Perché iOS disconnette?
1. **Smart Power Management**: Se rileva pattern anomali (ping ogni 5s), iOS assume consumo batteria eccessivo
2. **Inactivity Timeout**: L'hotspot si spegne se non rileva "traffico utile" (ping non conta come utile)
3. **Thermal Protection**: Troppo traffico → CPU iPhone caldo → throttling/disconnect

### Alternativa al Ping
Invece di ping al gateway, considera:
- Lasciare che il traffico MQTT naturale (telemetria ogni 200ms) mantenga viva la connessione
- Se necessario keepalive, aumentare a 60s minimo
