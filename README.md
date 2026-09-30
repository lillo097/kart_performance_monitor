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

Quando una sessione passa a `running`, il server crea un GUID e lo propaga
al Raspberry sul topic MQTT `sensors2mqtt-glo2/session/control`. Lo stesso
`session_guid` viene salvato nei JSON e aggiunto ai nomi dei file di sessione;
la cartella dei log Raspberry viene associata al GUID. Le sessioni non avviate
non ricevono un identificativo.

## Centro di comando Raspberry

La dashboard e l’agente Raspberry sono due processi separati collegati al
broker MQTT pubblico `broker.hivemq.com`. Broker, topic, `device_id`, porta
dashboard e servizi sono hardcoded in
`src/raspi_command_center/settings.py`: non ci sono file JSON, chiavi,
password o variabili d’ambiente da configurare. Non viene usato SSH tra i due
host. L’agente sul Pi deve restare attivo anche quando `kart-telemetry.service`
è fermo.

**Attenzione:** il broker pubblico non autentica né cifra i messaggi. Log,
stato e comandi possono essere osservati o pubblicati da terzi che conoscono
i topic; qualcuno potrebbe quindi avviare, fermare o riavviare i due servizi.
L’agente limita le azioni ai servizi previsti, rifiuta messaggi MQTT retained e
scarta richieste scadute o duplicate, ma queste misure non sostituiscono
autenticazione e riservatezza. Usa la dashboard solo tramite VPN privata
(ad esempio Tailscale Serve, non Funnel) e non impiegarla per ambienti critici.

### Prima installazione dell’agente sul Raspberry

Una sola volta, sul Pi:

```bash
cd /home/pi/kart_performance_monitor
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Installa l’unit di esempio (correggi `WorkingDirectory` e `ExecStart` se il
repository è in un percorso diverso):

```bash
sudo cp etc/systemd/system/kart-command-agent.service /etc/systemd/system/
sudo usermod -aG systemd-journal pi
sudo visudo -f /etc/sudoers.d/kart-command-agent
```

Nel file sudoers, dopo aver controllato il percorso con `command -v systemctl`,
inserisci questa regola:

```text
pi ALL=(root) NOPASSWD: /usr/bin/systemctl start kart-telemetry.service, /usr/bin/systemctl stop kart-telemetry.service, /usr/bin/systemctl restart kart-telemetry.service, /usr/bin/systemctl start sync-logs.service, /usr/bin/systemctl stop sync-logs.service, /usr/bin/systemctl restart sync-logs.service
```

Quindi avvia e abilita il processo residente:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kart-command-agent.service
sudo systemctl status kart-command-agent.service --no-pager
```

### Avvio della dashboard sul server

In un terminale dalla root del repository:

```bash
./venv/bin/python -m src.raspi_command_center.app
```

La dashboard ascolta su `0.0.0.0:8081`, quindi su tutte le interfacce di rete
del computer. `0.0.0.0` è l'indirizzo di bind, non quello da digitare nel
browser: apri `http://127.0.0.1:8081/` in locale oppure
`http://IP-DEL-COMPUTER:8081/` da un altro dispositivo. La dashboard si
connette automaticamente; per arrestarla, premi `Ctrl+C` nel terminale:
l'app principale e la telemetria continuano a funzionare.

La porta dashboard predefinita è `8081`. Stato e comandi viaggiano in JSON
MQTT in chiaro; l’agente accetta solo start, stop,
restart e log per i due servizi configurati. Non cancella log o sessioni.

### Aggiornamenti successivi

Pubblica il nuovo codice dal server con `git push`; sul Raspberry esegui:

```bash
cd /home/pi/kart_performance_monitor
git pull
sudo systemctl restart kart-command-agent.service
```

Riavvia anche il processo dashboard sul server (`Ctrl+C` e rilancio del
comando di avvio) per caricare eventuali modifiche lato interfaccia. Non
servono copie di file di configurazione o chiavi.

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
