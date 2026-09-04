#!/usr/bin/env python3
from __future__ import annotations

import math
import time
from datetime import datetime

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

state = {
    "speed": 78, "rpm": 9200, "temp": 96.0, "delta": 0.184,
    "sectors": [18.42, 22.18, 16.77], "best_lap": 57.12,
    "last_lap": 57.37, "ideal_lap": 56.84,
    "circuit": "Pista demo", "driver": "Driver 01", "gps": "10/12", "sat": "12",
    "event_id": 0, "last_event_at": 0.0, "event_step": 0, "sector_event": None,
}

HTML = r'''
<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#000000"><title>Kart Performance Monitor</title><style>
:root{--white:#f4f4f4;--muted:#c8c8c8;--purple:#d500f9;--green:#00a651;--yellow:#d9a300}*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#000;color:#fff;font-family:Arial,Helvetica,sans-serif}body{display:grid;place-items:center}
.dashboard{width:100vw;height:100vh;max-width:932px;max-height:430px;padding:10px 20px 4px;display:grid;grid-template-columns:1fr 1.46fr 1fr;gap:12px;background:#000}.column{min-width:0;display:flex;flex-direction:column}.topbar{grid-column:1/-1;height:21px;display:flex;justify-content:space-between;align-items:center;font-size:11px;line-height:13px;font-weight:700;white-space:nowrap}.topbar-left,.topbar-right{display:flex;align-items:center;gap:18px}.topbar-left{margin-left:10px}.topbar-right{gap:13px;margin-right:10px}.dashboard{grid-template-rows:21px minmax(0,1fr)}.column{grid-row:2}
.panel{position:relative;min-height:0;height:100%;border:2px solid var(--white);border-radius:23px}.panel-title{position:absolute;z-index:3;top:-10px;left:50%;transform:translateX(-50%);padding:0 10px;background:#000;white-space:nowrap;font-size:12px;font-weight:800}.left-panel{display:flex;flex-direction:column;gap:8px;padding:29px 14px 9px}.box{border:2px solid var(--white);border-radius:20px;padding:10px 15px;min-height:64px}.box-title{margin:0;text-align:center;font-size:11px;font-weight:800}#delta{padding-top:8px;text-align:center;font-size:18px;font-weight:800}.delta-positive{color:#ffb7b7}.delta-negative{color:#bfffc5}.sector{width:calc(100% - 17px);margin-left:17px;min-height:92px}.sector-values{margin-top:8px;font-size:14px;font-weight:800;line-height:21px}.sector-values span{float:right;font-size:15px}
/* Modalità grandi e spostate leggermente più verso il basso. */
.modes{display:grid;gap:7px;padding:0 16px;margin-top:auto;margin-bottom:11px}.mode{height:25px;border:2px solid var(--white);border-radius:14px;display:grid;place-items:center;font-size:11px;font-weight:800;letter-spacing:.1px}.mode.active{background:#fff;color:#000}
.telemetry-panel{padding:27px 30px 9px;display:flex;flex-direction:column;align-items:center}.gauge{width:100%}.gauge-track{height:32px;position:relative;border:2px solid var(--white);background:linear-gradient(90deg,#fff 0 var(--fill,0%),transparent var(--fill,0%))}.gauge-track:after{content:"";position:absolute;inset:0;background:repeating-linear-gradient(90deg,transparent 0 13px,#fff 13px 15px);opacity:.82}.needle{position:absolute;z-index:2;top:-10px;height:46px;border-left:3px solid #fff;transition:left .5s}.needle:before{content:"";position:absolute;top:-8px;left:-7px;border-left:7px solid transparent;border-right:7px solid transparent;border-top:9px solid #fff}.gauge-labels{display:flex;justify-content:space-between;margin-top:4px;font-size:11px;font-weight:700}.unit{text-align:center;margin-top:2px;color:var(--muted);font-size:10px;font-weight:700}.speed-row{display:flex;align-items:baseline;gap:13px;margin:auto 0 14px}#speed{font-size:124px;letter-spacing:-7px;line-height:.7;font-weight:800}.speed-caption{font-size:20px;line-height:20px;font-weight:800}.speed-caption small{font-size:18px;font-weight:500}.temp-gauge{width:92%;margin-top:auto}.temp-gauge .gauge-track{height:16px}.temp-gauge .needle{top:-7px;height:30px}.temp-gauge .gauge-labels{font-size:10px}
.right-panel{padding:65px 13px 9px}.lap-box{height:58px;min-height:0;margin-bottom:9px;display:flex;flex-direction:column;justify-content:center}.lap-time{margin-top:5px;text-align:center;font-size:18px;font-weight:800}
.sector-popup{position:fixed;z-index:30;left:50%;top:50%;width:min(53vw,495px);height:min(27vw,240px);min-width:380px;min-height:178px;transform:translate(-50%,-50%) scale(.92);display:grid;place-items:center;text-align:center;border:5px solid #fff;border-radius:30px;opacity:0;visibility:hidden;pointer-events:none;box-shadow:0 0 38px rgba(255,255,255,.30);transition:opacity .18s ease,transform .18s ease,visibility .18s}.sector-popup.show{opacity:1;visibility:visible;transform:translate(-50%,-50%) scale(1)}.sector-popup.purple{background:var(--purple)}.sector-popup.green{background:var(--green)}.sector-popup.yellow{background:var(--yellow)}.popup-result{padding:8px 18px;color:#fff;font-size:clamp(55px,10vw,94px);line-height:1;font-weight:900;letter-spacing:-2px;font-variant-numeric:tabular-nums;white-space:nowrap;text-shadow:0 3px 1px rgba(0,0,0,.2)}
@media(max-height:390px){.dashboard{padding-top:6px;grid-template-rows:18px minmax(0,1fr)}.topbar{height:18px}.left-panel{padding-top:25px}.telemetry-panel{padding-top:24px}.right-panel{padding-top:56px}#speed{font-size:114px}.lap-box{height:53px!important;margin-bottom:7px!important}.mode{height:21px;font-size:10px}.modes{gap:4px;padding:0 20px;margin-bottom:8px}.sector{width:calc(100% - 14px);margin-left:14px}.sector-values{font-size:13px}.sector-values span{font-size:14px}.sector-popup{height:185px}}@media(max-width:700px){.dashboard{padding-left:12px;padding-right:12px;gap:8px}.topbar-left{gap:10px;margin-left:7px}.topbar-right{gap:8px;margin-right:7px}.sector-popup{width:84vw;height:48vw;min-width:0;min-height:0}.popup-result{font-size:17vw}}
</style></head><body>
<main class="dashboard"><div class="topbar"><div class="topbar-left"><span>Circuit: <span id="circuit"></span></span><span>Driver: <span id="driver"></span></span></div><div class="topbar-right"><span>GPS: <span id="gps"></span></span><span>SAT: <span id="sat"></span></span></div></div>
<section class="column"><div class="panel left-panel"><div class="panel-title">SECTOR</div><div class="box"><h2 class="box-title">DELTA TIME <span style="font-size:8px;letter-spacing:.6px">LIVE</span></h2><div id="delta">+0.000 s</div></div><div class="box sector"><h2 class="box-title">LAST SECTOR</h2><div class="sector-values">S1 <span id="s1">--</span><br>S2 <span id="s2">--</span><br>S3 <span id="s3">--</span></div></div><div class="modes"><div class="mode">WARM UP</div><div class="mode active">HAMMER TIME</div><div class="mode">COOL DOWN</div></div></div></section>
<section class="column"><div class="panel telemetry-panel"><div class="panel-title">TELEMETRY</div><div class="gauge"><div class="gauge-track" id="rpmTrack"><div class="needle" id="rpmNeedle"></div></div><div class="gauge-labels"><span>0</span><span>2</span><span>4</span><span>6</span><span>8</span><span>10</span><span>12</span><span>14</span><span>16</span><span>18</span><span>20</span></div><div class="unit">RPM (x1000)</div></div><div class="speed-row"><strong id="speed">0</strong><div class="speed-caption">SPEED<br><small>km/h</small></div></div><div class="gauge temp-gauge"><div class="gauge-track" id="tempTrack"><div class="needle" id="tempNeedle"></div></div><div class="gauge-labels"><span>20°C</span><span>40°C</span><span>60°C</span><span>80°C</span><span>100°C</span><span>120°C</span><span>150°C</span></div><div class="unit">TEMP (°C)</div></div></div></section>
<section class="column"><div class="panel right-panel"><div class="panel-title">LAP</div><div class="box lap-box"><h2 class="box-title">BEST LAP</h2><div id="bestLap" class="lap-time">--:--.---</div></div><div class="box lap-box"><h2 class="box-title">LAST LAP</h2><div id="lastLap" class="lap-time">--:--.---</div></div><div class="box lap-box"><h2 class="box-title">IDEAL LAP</h2><div id="idealLap" class="lap-time">--:--.---</div></div></div></section></main>
<div id="sectorPopup" class="sector-popup"><div id="popupResult" class="popup-result">+0.000 s</div></div>
<script>
const fmt=v=>{const m=Math.floor(v/60),s=v-m*60;return `${m}:${s.toFixed(3).padStart(6,'0')}`};const gauge=(t,n,p)=>{p=Math.max(0,Math.min(100,p));t.style.setProperty('--fill',p+'%');n.style.left=p+'%'};let lastEventId=0,popupTimer=null;function showPopup(event){const p=document.getElementById('sectorPopup');const color=event.type==='best'?'purple':event.delta<0?'green':'yellow';p.className='sector-popup '+color;popupResult.textContent=(event.delta>=0?'+':'')+event.delta.toFixed(3)+' s';requestAnimationFrame(()=>p.classList.add('show'));clearTimeout(popupTimer);popupTimer=setTimeout(()=>p.classList.remove('show'),5000)}async function refresh(){try{const d=await(await fetch('/api/telemetry',{cache:'no-store'})).json();speed.textContent=d.speed;circuit.textContent=d.circuit;driver.textContent=d.driver;gps.textContent=d.gps;sat.textContent=d.sat;delta.textContent=(d.delta>=0?'+':'')+d.delta.toFixed(3)+' s';delta.className=d.delta>=0?'delta-positive':'delta-negative';s1.textContent=d.sectors[0].toFixed(2)+' s';s2.textContent=d.sectors[1].toFixed(2)+' s';s3.textContent=d.sectors[2].toFixed(2)+' s';bestLap.textContent=fmt(d.best_lap);lastLap.textContent=fmt(d.last_lap);idealLap.textContent=fmt(d.ideal_lap);gauge(rpmTrack,rpmNeedle,d.rpm/20000*100);gauge(tempTrack,tempNeedle,(d.temp-20)/130*100);if(d.sector_event&&d.sector_event.id!==lastEventId){lastEventId=d.sector_event.id;showPopup(d.sector_event)}}catch(e){console.error(e)}}refresh();setInterval(refresh,350);
</script></body></html>
'''

def clamp(v, a, b): return max(a, min(b, v))
def simulate():
    now=time.time();phase=now/4
    state["speed"]=int(clamp(72+22*math.sin(phase)+3*math.sin(phase*2.7),35,112));state["rpm"]=int(clamp(4500+state["speed"]*82,2500,14500));state["temp"]=round(clamp(93+8*math.sin(now/18),20,150),1);state["delta"]=round(.3*math.sin(now/3),3);state["sectors"]=[round(base+.12*math.sin(now/3+i),3) for i,base in enumerate((18.42,22.18,16.77))];state["last_lap"]=round(sum(state["sectors"]),3);state["best_lap"]=min(state["best_lap"],state["last_lap"]);state["ideal_lap"]=round(min(state["ideal_lap"],state["best_lap"]-.004),3)
    if now-state["last_event_at"]>=6:
        # Demo: perdita gialla, guadagno verde, miglior settore viola. I valori popup sono delta realistici.
        sequence=[{"type":"delta","delta":+.218},{"type":"delta","delta":-.143},{"type":"best","delta":-.350}]
        state["event_id"]+=1;event=sequence[state["event_step"]];state["sector_event"]={"id":state["event_id"],**event};state["event_step"]=(state["event_step"]+1)%len(sequence);state["last_event_at"]=now
    state["updated_at"]=datetime.now().isoformat(timespec="seconds");return state
@app.route('/')
def dashboard(): return render_template_string(HTML)
@app.route('/api/telemetry')
def api(): return jsonify(simulate())
if __name__=='__main__': app.run(host='0.0.0.0',port=5001,debug=True)
#!/usr/bin/env python3
"""
Kart Live Dashboard - MQTT -> Flask-SocketIO -> browser.

Regole:
- Un sensore e' ONLINE solo se:
  1) ricevo ORA un messaggio MQTT non-retained
     su sensors2mqtt-glo2/esp32/status/<component> con {"sensor":"...","present":true}, E
  2) sono passati <= 5 minuti dall'ultimo messaggio (telemetria o status) per quel componente.
- Se ricevo {"sensor":"...","present":false} -> OFFLINE (immediato).
- I messaggi retained vengono IGNORATI: non cambiano lo stato.
- Se non ricevo messaggi per > 5 minuti -> OFFLINE per timeout.
"""


import json
import os
import time
from collections import deque
from datetime import datetime
from threading import Lock


import paho.mqtt.client as mqtt
from flask import Flask, render_template_string
from flask_socketio import SocketIO


MQTT_BROKER = os.getenv("MQTT_BROKER", "broker.hivemq.com")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "sensors2mqtt-glo2/esp32/location")
STATUS_TOPIC_PREFIX = os.getenv(
    "STATUS_TOPIC_PREFIX",
    "sensors2mqtt-glo2/esp32/status"
)
WEB_PORT = int(os.getenv("WEB_PORT", "5001"))

# Token Mapbox (puoi metterlo anche in env: MAPBOX_TOKEN)
MAPBOX_TOKEN = os.getenv(
    "MAPBOX_TOKEN",
    "REDACTED_MAPBOX_TOKEN"
)


STATUS_KEY_BY_COMPONENT = {
    "hotspot": "hotspot",
    "mqtt": "mqtt",
    "ntc": "ntc",
    "as5600": "as5600",
    "ir_rpm": "ir_rpm",
    "gps": "gps",
}


# Timeout dopo il quale un sensore e' considerato OFFLINE
SENSOR_OFFLINE_TIMEOUT_S = 5 * 60  # 5 minuti


app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "kart-live-dashboard")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


lock = Lock()
history = deque(maxlen=90)


# last_seen[component] = timestamp (float) dell'ultimo messaggio (telemetria o status)
last_seen = {
    "hotspot": None,
    "mqtt": None,
    "ntc": None,
    "as5600": None,
    "ir_rpm": None,
    "gps": None,
}


latest = {
    "connected": False,
    "updated_at": None,
    "satellites_used": None,
    "latitude": None,
    "longitude": None,
    "speed_kmph": None,
    "fix_valid": False,
    "fix_quality": None,
    "fix_type": None,
    "hdop": None,
    "temperature_c": None,
    "as5600_angle_deg": None,
    "as5600_rpm": None,
    "as5600_magnet_ok": False,
    "ir_rpm": None,
    "ir_total_pulses": None,
    "sensor_status": {
        "hotspot": False,
        "mqtt": False,
        "ntc": False,
        "as5600": False,
        "ir_rpm": False,
        "gps": False,
    },
}


HTML = r"""
<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#06080d">
  <title>Kart Telemetry</title>

  <!-- Mapbox GL JS -->
  <script src="https://api.mapbox.com/mapbox-gl-js/v3.9.0/mapbox-gl.js"></script>
  <link href="https://api.mapbox.com/mapbox-gl-js/v3.9.0/mapbox-gl.css" rel="stylesheet">

  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>


  <style>
    :root {
      --bg: #06080d;
      --line: #273344;
      --text: #f4f7fb;
      --muted: #8491a5;
      --green: #38eb89;
      --blue: #57a7ff;
      --orange: #ff9d3d;
      --red: #ff5964;
      --purple: #b28dff;
    }


    * { box-sizing: border-box; }


    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system,
                   BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(900px 550px at 0% -5%, rgba(42,105,183,.30), transparent 68%),
        radial-gradient(800px 500px at 100% 0%, rgba(54,184,126,.12), transparent 62%),
        var(--bg);
    }


    .wrap {
      width: min(1240px, calc(100% - 28px));
      margin: auto;
      padding: 22px 0 42px;
    }


    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 18px;
    }


    h1 {
      margin: 0;
      font-size: clamp(1.5rem, 4.5vw, 2.35rem);
      letter-spacing: -.075em;
    }


    .accent { color: var(--green); }


    .sub {
      margin-top: 5px;
      color: var(--muted);
      font-size: .8rem;
      letter-spacing: .025em;
    }


    .status {
      display: flex;
      align-items: center;
      gap: 9px;
      padding: 8px 11px;
      border: 1px solid var(--line);
      background: rgba(14,20,29,.88);
      border-radius: 999px;
      font-size: .78rem;
      white-space: nowrap;
    }


    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--red);
      box-shadow: 0 0 13px var(--red);
    }


    .dot.ok {
      background: var(--green);
      box-shadow: 0 0 13px var(--green);
    }


    .grid {
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 12px;
    }


    .card {
      position: relative;
      overflow: hidden;
      padding: 17px;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: linear-gradient(145deg, rgba(23,31,44,.98), rgba(11,15,22,.98));
      box-shadow: 0 16px 40px rgba(0,0,0,.20);
    }


    .card::after {
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background: linear-gradient(115deg, rgba(255,255,255,.04), transparent 32%);
    }


    .gps-card {
      grid-column: span 12;
      min-height: 265px;
    }


    .rpm, .temp, .angle {
      grid-column: span 4;
      min-height: 208px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }


    .health {
      grid-column: span 7;
      min-height: 142px;
    }


    .meta-card {
      grid-column: span 5;
      min-height: 142px;
    }


    .label {
      color: var(--muted);
      font-size: .69rem;
      font-weight: 800;
      letter-spacing: .13em;
      text-transform: uppercase;
    }


    .sensor-status {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin-top: 8px;
      padding: 5px 8px;
      border-radius: 999px;
      background: rgba(255,89,100,.13);
      color: #ff9198;
      font-size: .67rem;
      font-weight: 850;
      letter-spacing: .04em;
      text-transform: uppercase;
    }


    .sensor-status .dot {
      width: 7px;
      height: 7px;
      box-shadow: none;
    }


    .sensor-status.active {
      background: rgba(56,235,137,.14);
      color: var(--green);
    }


    .sensor-status.active .dot {
      background: var(--green);
      box-shadow: 0 0 9px var(--green);
    }


    .sensor-status.offline {
      background: rgba(255,89,100,.13);
      color: #ff9198;
    }


    .sensor-status.offline .dot {
      background: var(--red);
      box-shadow: 0 0 9px var(--red);
    }


    .gps-layout {
      display: grid;
      grid-template-columns: 1.1fr 1.25fr 1fr;
      gap: 14px;
      align-items: stretch;
      margin-top: 14px;
    }


    .gps-speed {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      padding: 15px;
      border-radius: 15px;
      background: rgba(39,51,68,.36);
    }


    .gps-speed .value { margin-top: 9px; }


    .gps-info {
      padding: 15px;
      border-radius: 15px;
      background: rgba(39,51,68,.36);
    }


    .gps-info .coord {
      margin-top: 8px;
      font-size: clamp(1rem, 2.6vw, 1.45rem);
      letter-spacing: -.04em;
      font-variant-numeric: tabular-nums;
    }


    .stats {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 9px;
    }


    .stat {
      padding: 11px;
      border-radius: 12px;
      background: rgba(39,51,68,.48);
    }


    .stat span {
      display: block;
      color: var(--muted);
      font-size: .63rem;
      font-weight: 750;
      letter-spacing: .08em;
      text-transform: uppercase;
    }


    .stat strong {
      display: block;
      margin-top: 5px;
      font-size: 1.19rem;
      letter-spacing: -.045em;
      font-variant-numeric: tabular-nums;
    }


    .value {
      margin-top: 14px;
      font-size: clamp(2.65rem, 7vw, 5.1rem);
      font-weight: 850;
      letter-spacing: -.08em;
      line-height: .88;
      font-variant-numeric: tabular-nums;
    }


    .value.rpmv { color: var(--purple); }
    .value.tempv { color: var(--orange); }


    .unit {
      margin-left: 5px;
      color: var(--muted);
      font-size: .27em;
      font-weight: 750;
      letter-spacing: -.01em;
    }


    .meta {
      color: var(--muted);
      font-size: .78rem;
    }


    .alert {
      min-height: 18px;
      margin-top: 8px;
      color: var(--red);
      font-size: .73rem;
      font-weight: 750;
    }


    .alert:empty { display: none; }


    .bar {
      height: 7px;
      margin-top: 14px;
      overflow: hidden;
      border-radius: 999px;
      background: #273345;
    }


    .bar > div {
      height: 100%;
      width: 0%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--blue), #8fc7ff);
      transition: width .18s ease;
    }


    .temp .bar > div {
      background: linear-gradient(90deg, #ffcf55, var(--orange), var(--red));
    }


    .gauge {
      display: flex;
      align-items: center;
      gap: 17px;
      margin-top: 12px;
    }


    .dial {
      width: 102px;
      height: 102px;
      flex: 0 0 auto;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background: conic-gradient(var(--green) 0deg, #263247 0deg);
      position: relative;
    }


    .dial::after {
      content: "";
      position: absolute;
      inset: 8px;
      border-radius: 50%;
      background: #111822;
    }


    .dial-text {
      z-index: 1;
      text-align: center;
      font-size: 1.3rem;
      font-weight: 850;
      letter-spacing: -.06em;
    }


    .dial-text small {
      display: block;
      color: var(--muted);
      font-size: .55rem;
      letter-spacing: .05em;
    }


    .details { min-width: 0; }


    .details .big {
      font-weight: 800;
      font-size: 1.5rem;
      letter-spacing: -.05em;
    }


    .tag {
      display: inline-flex;
      margin-top: 8px;
      padding: 5px 8px;
      border-radius: 999px;
      font-size: .69rem;
      font-weight: 800;
      background: #213147;
      color: var(--blue);
    }


    .tag.good {
      color: #07140d;
      background: var(--green);
    }


    .tag.bad {
      color: #2a080c;
      background: var(--red);
    }


    .rows {
      margin-top: 8px;
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 9px;
    }


    .row {
      padding: 10px;
      border-radius: 12px;
      background: rgba(39,51,68,.42);
      color: var(--muted);
      font-size: .73rem;
    }


    .row span { display: block; }


    .row b {
      display: block;
      margin-top: 5px;
      color: var(--text);
      font-weight: 800;
    }


    .state-text.active { color: var(--green); }
    .state-text.offline { color: #ff9198; }
    .state-text.error { color: var(--red); }


    .history {
      margin-top: 12px;
      width: 100%;
      height: 37px;
      opacity: .85;
    }


    .footer {
      margin-top: 16px;
      color: var(--muted);
      font-size: .75rem;
      text-align: center;
    }


    /* Mappa Mapbox */
    .map-card {
      grid-column: span 12;
      min-height: 380px;
      position: relative;
      overflow: hidden;
    }


    #map {
      width: 100%;
      height: 360px;
      border-radius: 14px;
      overflow: hidden;
    }


    .map-controls {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
      flex-wrap: wrap;
    }


    .map-controls label {
      color: var(--muted);
      font-size: .72rem;
      font-weight: 700;
      letter-spacing: .06em;
      text-transform: uppercase;
    }


    .map-controls select {
      appearance: none;
      padding: 6px 9px;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: rgba(23,31,44,.95);
      color: var(--text);
      font-size: .78rem;
      font-weight: 600;
      outline: none;
    }


    @media (max-width: 860px) {
      .gps-layout { grid-template-columns: 1fr 1fr; }
      .gps-info { grid-column: span 2; }
      .rpm, .temp, .angle { grid-column: span 6; }
      .health, .meta-card, .map-card { grid-column: span 12; }
      #map { height: 320px; }
    }


    @media (max-width: 590px) {
      .wrap {
        width: min(100% - 18px, 1240px);
        padding-top: 14px;
      }


      header { align-items: flex-start; }
      .grid { gap: 9px; }
      .gps-layout {
        grid-template-columns: 1fr;
        gap: 9px;
      }


      .gps-info { grid-column: auto; }
      .rpm, .temp, .angle, .health, .meta-card, .map-card { grid-column: span 12; }
      .gps-card { min-height: auto; }
      .rpm, .temp, .angle { min-height: 172px; }


      .card {
        padding: 15px;
        border-radius: 15px;
      }


      .value { font-size: clamp(3.2rem, 16vw, 5rem); }
      .gps-speed .value { font-size: clamp(3.4rem, 18vw, 5.2rem); }
      .rows { grid-template-columns: 1fr 1fr; }
      #map { height: 260px; }
    }
  </style>
</head>


<body>
  <main class="wrap">
    <header>
      <div>
        <h1>KART <span class="accent">TELEMETRY</span></h1>
        <div class="sub">ESP32 · GARMIN GLO 2 · NTC · AS5600 · IR RPM</div>
      </div>


      <div class="status">
        <span id="dot" class="dot"></span>
        <span id="status">In attesa dati</span>
      </div>
    </header>


    <section class="grid">
      <article class="card gps-card">
        <div class="label">GPS · Garmin GLO 2</div>


        <div class="sensor-status offline" id="gps-sensor-status">
          <span class="dot"></span>
          <span>Offline</span>
        </div>


        <div class="gps-layout">
          <div class="gps-speed">
            <div class="label">Velocità</div>


            <div class="value">
              <span id="speed">--</span>
              <span class="unit">km/h</span>
            </div>


            <div>
              <div class="meta">Fix <span id="fix">--</span></div>
              <div class="bar"><div id="speed-bar"></div></div>
            </div>
          </div>


          <div class="gps-info">
            <div class="label">Posizione GPS</div>


            <div class="coord">
              <span id="lat">--</span>,
              <span id="lon">--</span>
            </div>


            <div class="tag" id="gps-status">Fix GPS: --</div>
            <div class="alert" id="gps-alert"></div>
          </div>


          <div class="stats">
            <div class="stat"><span>Satelliti</span><strong id="sats">--</strong></div>
            <div class="stat"><span>HDOP</span><strong id="hdop">--</strong></div>
            <div class="stat"><span>Fix type</span><strong id="fix-type">--</strong></div>
            <div class="stat"><span>Qualità</span><strong id="fix-quality">--</strong></div>
          </div>
        </div>
      </article>


      <article class="card rpm">
        <div>
          <div class="label">RPM · Sensore IR</div>
          <div class="sensor-status offline" id="ir-sensor-status">
            <span class="dot"></span>
            <span>Offline</span>
          </div>
        </div>


        <div class="value rpmv">
          <span id="ir-rpm">--</span>
          <span class="unit">rpm</span>
        </div>


        <div class="meta">
          Marker ottico · impulsi:
          <span id="ir-pulses">--</span>
        </div>
      </article>


      <article class="card temp">
        <div>
          <div class="label">Temperatura</div>
          <div class="sensor-status offline" id="ntc-sensor-status">
            <span class="dot"></span>
            <span>Offline</span>
          </div>
        </div>


        <div class="value tempv">
          <span id="temp-value">--</span>
          <span class="unit">°C</span>
        </div>


        <div>
          <div class="meta">NTC 10K B3950 · GPIO34</div>
          <div class="bar"><div id="temp-bar"></div></div>
        </div>
      </article>


      <article class="card angle">
        <div class="label">AS5600 · Posizione magnete</div>


        <div class="sensor-status offline" id="as-sensor-status">
          <span class="dot"></span>
          <span>Offline</span>
        </div>


        <div class="gauge">
          <div class="dial" id="dial">
            <div class="dial-text">
              <span id="angle">--</span>
              <small>GRADI</small>
            </div>
          </div>


          <div class="details">
            <div class="big">
              <span id="as-rpm">--</span>
              <span class="meta">rpm</span>
            </div>


            <div class="meta" style="margin-top:4px">
              Velocità angolare diagnostica
            </div>


            <div class="tag" id="magnet-status">Magnete: --</div>
            <div class="alert" id="as-alert"></div>
          </div>
        </div>


        <div class="bar">
          <div id="angle-bar"
               style="background:linear-gradient(90deg, var(--green), #76f7bc)">
          </div>
        </div>
      </article>


      <article class="card health">
        <div class="label">Sistema · Status MQTT</div>


        <div class="rows">
          <div class="row"><span>Hotspot</span><b id="hotspot-state">OFFLINE</b></div>
          <div class="row"><span>MQTT ESP32</span><b id="mqtt-state">OFFLINE</b></div>
          <div class="row"><span>GPS / GLO 2</span><b id="gps-state">OFFLINE</b></div>
          <div class="row"><span>NTC</span><b id="ntc-state">OFFLINE</b></div>
          <div class="row"><span>AS5600</span><b id="as-state">OFFLINE</b></div>
          <div class="row"><span>IR RPM</span><b id="ir-state">OFFLINE</b></div>
          <div class="row"><span>Dashboard MQTT</span><b id="stream-state">OFFLINE</b></div>
          <div class="row"><span>Ultimo payload</span><b id="updated">--</b></div>
        </div>
      </article>


      <article class="card meta-card">
        <div class="label">Trend velocità · ultimi campioni</div>


        <svg class="history" viewBox="0 0 100 32" preserveAspectRatio="none">
          <polyline
            id="spark"
            fill="none"
            stroke="#58a6ff"
            stroke-width="2"
            points=""
          />
        </svg>


        <div class="meta">
          Status: da MQTT (ignoro retained)
        </div>
      </article>


      <!-- MAPPA MAPBOX -->
      <article class="card map-card">
        <div class="label">Mappa · Posizione GPS</div>
        <div id="map"></div>
        <div class="map-controls">
          <label for="map-style">Stile</label>
          <select id="map-style">
            <option value="streets">Normale (colorata)</option>
            <option value="satellite">Satellite</option>
            <option value="satellite-streets">Satellite + strade</option>
          </select>
        </div>
      </article>
    </section>


    <div class="footer">Kart performance monitor · Dashboard live</div>
  </main>


  <script>
    const byId = (id) => document.getElementById(id);


    const fmt = (value, digits = 1) => {
      const n = Number(value);
      return Number.isFinite(n) ? n.toFixed(digits) : "--";
    };


    const integer = (value) => {
      const n = Number(value);
      return Number.isFinite(n)
        ? Math.round(n).toLocaleString("it-IT")
        : "--";
    };


    const clamp = (n, a, b) => Math.max(a, Math.min(b, n));


    function setBadge(id, active) {
      const el = byId(id);


      el.className = `sensor-status ${active ? "active" : "offline"}`;


      el.innerHTML = `
        <span class="dot"></span>
        <span>${active ? "Attivo" : "Offline"}</span>
      `;
    }


    function setSystemState(id, active, errorText = "") {
      const el = byId(id);


      el.textContent = active ? "ATTIVO" : (errorText || "OFFLINE");


      el.className = `state-text ${
        active ? "active" : (errorText ? "error" : "offline")
      }`;
    }


    // Inizializzazione mappa Mapbox
    const MAPBOX_TOKEN = "{{ mapbox_token }}";

    mapboxgl.accessToken = MAPBOX_TOKEN;

    // Stili disponibili
    const MAP_STYLES = {
      streets: 'mapbox://styles/mapbox/streets-v12',
      satellite: 'mapbox://styles/mapbox/satellite-v9',
      'satellite-streets': 'mapbox://styles/mapbox/satellite-streets-v12',
    };

    const map = new mapboxgl.Map({
      container: 'map',
      style: MAP_STYLES.streets, // default: normale colorata
      center: [12.4964, 41.9028], // Roma di default
      zoom: 10,
      attributionControl: true,
    });

    const markerEl = document.createElement('div');
    markerEl.style.width = '16px';
    markerEl.style.height = '16px';
    markerEl.style.borderRadius = '50%';
    markerEl.style.background = '#38eb89';
    markerEl.style.boxShadow = '0 0 10px #38eb89';
    markerEl.style.border = '2px solid #06080d';

    let marker = null;

    function updateMapPosition(lat, lon) {
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        return;
      }

      if (!marker) {
        marker = new mapboxgl.Marker(markerEl)
          .setLngLat([lon, lat])
          .addTo(map);

        map.setCenter([lon, lat]);
        map.setZoom(13);
      } else {
        marker.setLngLat([lon, lat]);
        map.easeTo({
          center: [lon, lat],
          duration: 600,
        });
      }
    }

    // Selettore stile mappa
    const mapStyleSelect = byId('map-style');

    mapStyleSelect.addEventListener('change', () => {
      const chosen = mapStyleSelect.value; // 'streets', 'satellite', 'satellite-streets'
      const newStyle = MAP_STYLES[chosen];
      if (!newStyle) return;

      map.setStyle(newStyle, {
        diff: true,
      });
    });


    function render(d) {
      const streamAlive = Boolean(d.connected);
      const statuses = d.sensor_status || {};


      const speed = Number(d.speed_kmph);
      const temp = Number(d.temperature_c);
      const angle = Number(d.as5600_angle_deg);
      const irRpm = Number(d.ir_rpm);
      const asRpm = Number(d.as5600_rpm);


      const gpsPresent = Boolean(statuses.gps);
      const ntcPresent = Boolean(statuses.ntc);
      const asPresent = Boolean(statuses.as5600);
      const irPresent = Boolean(statuses.ir_rpm);
      const hotspotPresent = Boolean(statuses.hotspot);
      const espMqttPresent = Boolean(statuses.mqtt);


      const magnetOk = Boolean(d.as5600_magnet_ok);
      const gpsValid = Boolean(d.fix_valid);


      byId("speed").textContent = fmt(speed, 1);
      byId("temp-value").textContent = fmt(temp, 1);
      byId("ir-rpm").textContent = integer(irRpm);
      byId("ir-pulses").textContent = integer(d.ir_total_pulses);
      byId("angle").textContent = fmt(angle, 1);
      byId("as-rpm").textContent = fmt(asRpm, 1);


      byId("sats").textContent = d.satellites_used ?? "--";
      byId("hdop").textContent = fmt(d.hdop, 1);
      byId("fix-type").textContent = d.fix_type ?? "--";
      byId("fix-quality").textContent = d.fix_quality ?? "--";
      byId("lat").textContent = fmt(d.latitude, 6);
      byId("lon").textContent = fmt(d.longitude, 6);
      byId("updated").textContent = d.updated_at || "--";


      byId("speed-bar").style.width =
        `${clamp(speed / 120 * 100, 0, 100)}%`;


      byId("temp-bar").style.width =
        `${clamp((temp - 20) / 100 * 100, 0, 100)}%`;


      byId("angle-bar").style.width =
        `${clamp(angle / 360 * 100, 0, 100)}%`;


      byId("dial").style.background =
        `conic-gradient(var(--green) ${clamp(angle, 0, 360)}deg, #263247 0deg)`;


      setBadge("gps-sensor-status", gpsPresent);
      setBadge("ntc-sensor-status", ntcPresent);
      setBadge("as-sensor-status", asPresent);
      setBadge("ir-sensor-status", irPresent);


      byId("fix").textContent = gpsValid
        ? "GPS valido"
        : "GPS non valido";


      byId("gps-status").textContent =
        `Fix GPS: ${gpsValid ? "VALIDO" : "NON VALIDO"}`;


      byId("gps-status").className =
        `tag ${gpsValid ? "good" : "bad"}`;


      byId("gps-alert").textContent =
        gpsPresent && !gpsValid
          ? "GPS collegato, ma fix non ancora valido."
          : "";


      byId("magnet-status").textContent = magnetOk
        ? "Magnete: OK"
        : "Magnete: da controllare";


      byId("magnet-status").className =
        `tag ${magnetOk ? "good" : "bad"}`;


      byId("as-alert").textContent =
        asPresent && !magnetOk
          ? "Campo magnetico non valido: centra o avvicina il magnete."
          : "";


      setSystemState("hotspot-state", hotspotPresent);
      setSystemState("mqtt-state", espMqttPresent);


      setSystemState(
        "gps-state",
        gpsPresent,
        gpsPresent && !gpsValid ? "NO FIX" : ""
      );


      setSystemState("ntc-state", ntcPresent);


      setSystemState(
        "as-state",
        asPresent,
        asPresent && !magnetOk ? "MAGNETE" : ""
      );


      setSystemState("ir-state", irPresent);
      setSystemState("stream-state", streamAlive);


      byId("status").textContent = streamAlive
        ? "Telemetria attiva"
        : "In attesa dati";


      byId("dot").classList.toggle("ok", streamAlive);


      renderSparkline(d.history || []);


      // Aggiorna mappa
      const lat = Number(d.latitude);
      const lon = Number(d.longitude);
      if (Number.isFinite(lat) && Number.isFinite(lon) && gpsValid) {
        updateMapPosition(lat, lon);
      }
    }


    function renderSparkline(values) {
      const clean = values.map(Number).filter(Number.isFinite);


      if (!clean.length) {
        byId("spark").setAttribute("points", "");
        return;
      }


      const max = Math.max(1, ...clean);
      const min = Math.min(...clean);
      const spread = Math.max(1, max - min);


      const points = clean.map((v, i) => {
        const x = clean.length === 1
          ? 0
          : i * 100 / (clean.length - 1);


        const y = 29 - ((v - min) / spread) * 26;


        return `${x.toFixed(2)},${y.toFixed(2)}`;
      }).join(" ");


      byId("spark").setAttribute("points", points);
    }


    function setOfflineUi() {
      byId("status").textContent = "Dashboard disconnessa";
      byId("dot").classList.remove("ok");


      setBadge("gps-sensor-status", false);
      setBadge("ntc-sensor-status", false);
      setBadge("as-sensor-status", false);
      setBadge("ir-sensor-status", false);


      setSystemState("hotspot-state", false);
      setSystemState("mqtt-state", false);
      setSystemState("gps-state", false);
      setSystemState("ntc-state", false);
      setSystemState("as-state", false);
      setSystemState("ir-state", false);
      setSystemState("stream-state", false);
    }


    const socket = io();


    socket.on("telemetry", render);
    socket.on("snapshot", render);
    socket.on("disconnect", setOfflineUi);
  </script>
</body>
</html>
"""


def compute_sensor_online(component: str, present: bool) -> bool:
    """
    Un sensore e' considerato ONLINE se:
    - present == True (da status MQTT), E
    - sono passati <= SENSOR_OFFLINE_TIMEOUT_S dall'ultimo messaggio (telemetria o status).
    """
    ls = last_seen.get(component)
    if ls is None:
        return False

    now = time.time()
    age = now - ls
    if age > SENSOR_OFFLINE_TIMEOUT_S:
        return False

    return present


def snapshot():
    now = time.time()

    with lock:
        data = dict(latest)
        data["sensor_status"] = dict(latest["sensor_status"])
        data["history"] = list(history)

        # Calcolo stato "online reale" per ogni sensore (present + timeout 5min)
        online_status = {}
        for comp, key in STATUS_KEY_BY_COMPONENT.items():
            present = data["sensor_status"].get(key, False)
            online = compute_sensor_online(comp, present)
            online_status[key] = online

        # Sovrascrivo sensor_status con la versione "online reale"
        data["sensor_status"] = online_status

    timestamp = data["updated_at"]

    if timestamp is not None:
        data["connected"] = (now - timestamp) < 3.0
        data["updated_at"] = datetime.fromtimestamp(timestamp).strftime(
            "%H:%M:%S"
        )
    else:
        data["connected"] = False

    return data


@app.route("/")
def index():
    # Passo il token Mapbox al template
    return render_template_string(HTML, mapbox_token=MAPBOX_TOKEN)


@socketio.on("connect")
def on_socket_connect():
    socketio.emit("snapshot", snapshot())


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        print(f"[MQTT] Connessione rifiutata: {reason_code}")
        return

    status_wildcard = f"{STATUS_TOPIC_PREFIX}/#"

    print(f"[MQTT] Connesso a {MQTT_BROKER}:{MQTT_PORT}")
    print(f"[MQTT] Subscribe telemetria: {MQTT_TOPIC}")
    print(f"[MQTT] Subscribe status:     {status_wildcard}")

    client.subscribe([
        (MQTT_TOPIC, 0),
        (status_wildcard, 0),
    ])


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    print(f"[MQTT] Disconnesso: {reason_code}")


def handle_telemetry(incoming):
    allowed = {
        "satellites_used",
        "latitude",
        "longitude",
        "speed_kmph",
        "fix_valid",
        "fix_quality",
        "fix_type",
        "hdop",
        "temperature_c",
        "as5600_angle_deg",
        "as5600_rpm",
        "as5600_magnet_ok",
        "ir_rpm",
        "ir_total_pulses",
    }

    now = time.time()

    with lock:
        for key in allowed:
            if key in incoming:
                latest[key] = incoming[key]

        latest["updated_at"] = now
        latest["connected"] = True

        # Aggiorno last_seen per i sensori "di misura"
        # Considero che la telemetria tenga vivi: ntc, ir_rpm, as5600, gps
        for comp in ["ntc", "ir_rpm", "as5600", "gps"]:
            last_seen[comp] = now

        speed = incoming.get("speed_kmph")

        if isinstance(speed, (int, float)):
            history.append(float(speed))

    data = snapshot()

    print(
        "[DATA] "
        f"gps={data['speed_kmph']} km/h | "
        f"temp={data['temperature_c']} C | "
        f"IR={data['ir_rpm']} rpm | "
        f"AS={data['as5600_angle_deg']} deg | "
        f"sats={data['satellites_used']}"
    )

    socketio.emit("telemetry", data)


def handle_status(topic, incoming, retained: bool):
    # Se e' retained, lo ignoro completamente: non deve cambiare lo stato.
    if retained:
        print(f"[STATUS] RETAINED ignorato: {topic}")
        return

    component = incoming.get("sensor")
    present = incoming.get("present")

    if not isinstance(component, str) or component not in STATUS_KEY_BY_COMPONENT:
        print(
            f"[STATUS] Ignorato topic={topic}: "
            f"sensor non valido: {component!r}"
        )
        return

    if not isinstance(present, bool):
        print(
            f"[STATUS] Ignorato topic={topic}: "
            f"present non booleano: {present!r}"
        )
        return

    expected_topic = f"{STATUS_TOPIC_PREFIX}/{component}"

    if topic != expected_topic:
        print(
            f"[STATUS] Ignorato topic inatteso: {topic} "
            f"(atteso {expected_topic})"
        )
        return

    key = STATUS_KEY_BY_COMPONENT[component]

    now = time.time()

    with lock:
        # Aggiorno last_seen per questo componente
        last_seen[component] = now

        # Se arriva un messaggio FRESCO, fisso lo stato esattamente come dice il payload.
        latest["sensor_status"][key] = present

    print(
        f"[STATUS] {component} -> "
        f"{'ONLINE' if present else 'OFFLINE'} (fresco)"
    )

    socketio.emit("telemetry", snapshot())


def on_message(client, userdata, message):
    try:
        incoming = json.loads(message.payload.decode("utf-8"))

        if not isinstance(incoming, dict):
            raise ValueError("il payload non e' un oggetto JSON")

    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"[MQTT] Payload ignorato ({exc}): {message.payload!r}")
        return

    if message.topic == MQTT_TOPIC:
        handle_telemetry(incoming)
        return

    if message.topic.startswith(f"{STATUS_TOPIC_PREFIX}/"):
        handle_status(
            topic=message.topic,
            incoming=incoming,
            retained=bool(message.retain),
        )
        return

    print(f"[MQTT] Topic ignorato: {message.topic}")


def start_mqtt():
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="kart-dashboard-python",
    )

    client.reconnect_delay_set(min_delay=1, max_delay=15)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    client.connect_async(
        MQTT_BROKER,
        MQTT_PORT,
        keepalive=30,
    )

    client.loop_start()

    return client


if __name__ == "__main__":
    mqtt_client = start_mqtt()

    print(f"[WEB] Dashboard disponibile su http://127.0.0.1:{WEB_PORT}")
    print(
        f"[STATUS] Stato sensori: fisso SOLO con messaggi MQTT FRESCHI "
        f"e timeout {SENSOR_OFFLINE_TIMEOUT_S/60:.0f} minuti."
    )
    print("[STATUS] I messaggi MQTT retained vengono IGNORATI.")
    print(f"[MAP] Mappa Mapbox abilitata con token: {MAPBOX_TOKEN[:8]}...")

    try:
        socketio.run(
            app,
            host="0.0.0.0",
            port=WEB_PORT,
            allow_unsafe_werkzeug=True,
        )
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        #ciao