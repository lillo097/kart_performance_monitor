#!/usr/bin/env python3
from __future__ import annotations

import math
import random
import time
from datetime import datetime

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

state = {"speed": 78, "rpm": 9200, "temp": 96.0, "delta": 0.184, "sectors": [18.42, 22.18, 16.77], "best_lap": 57.12, "last_lap": 57.37, "ideal_lap": 56.84, "circuit": "Pista demo", "driver": "Driver 01", "gps": "10/12", "sat": "12"}

HTML = r'''
<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#000000"><title>Kart Performance Monitor</title><style>
:root{--white:#f4f4f4;--muted:#c8c8c8}*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#000;color:#fff;font-family:Arial,Helvetica,sans-serif}body{display:grid;place-items:center}
.dashboard{width:100vw;height:100vh;max-width:932px;max-height:430px;padding:10px 20px 4px;display:grid;grid-template-columns:1fr 1.46fr 1fr;gap:12px;background:#000}.column{min-width:0;display:flex;flex-direction:column}
.topbar{grid-column:1/-1;height:21px;display:flex;justify-content:space-between;align-items:center;font-size:11px;line-height:13px;font-weight:700;white-space:nowrap}.topbar-left,.topbar-right{display:flex;align-items:center;gap:18px}.topbar-right{gap:13px}.dashboard{grid-template-rows:21px minmax(0,1fr)}.column{grid-row:2}
.panel{position:relative;min-height:0;height:100%;border:2px solid var(--white);border-radius:23px}.panel-title{position:absolute;z-index:3;top:-10px;left:50%;transform:translateX(-50%);padding:0 10px;background:#000;white-space:nowrap;font-size:12px;font-weight:800}
.left-panel{display:flex;flex-direction:column;gap:8px;padding:29px 14px 9px}.box{border:2px solid var(--white);border-radius:20px;padding:10px 15px;min-height:64px}.box-title{margin:0;text-align:center;font-size:11px;font-weight:800}#delta{padding-top:8px;text-align:center;font-size:18px;font-weight:800}.delta-positive{color:#ffb7b7}.delta-negative{color:#bfffc5}
/* Last Sector più stretto e spostato verso destra: lascia una fascia libera sul lato notch/Dynamic Island. */
.sector{width:calc(100% - 17px);margin-left:17px;min-height:92px}.sector-values{margin-top:8px;font-size:14px;font-weight:800;line-height:21px}.sector-values span{float:right;font-size:15px}
.modes{display:grid;gap:5px;padding:0 24px;margin-top:auto}.mode{height:20px;border:2px solid var(--white);border-radius:13px;display:grid;place-items:center;font-size:9px;font-weight:800}.mode.active{background:#fff;color:#000}
.telemetry-panel{padding:27px 30px 9px;display:flex;flex-direction:column;align-items:center}.gauge{width:100%}.gauge-track{height:32px;position:relative;border:2px solid var(--white);background:linear-gradient(90deg,#fff 0 var(--fill,0%),transparent var(--fill,0%))}.gauge-track:after{content:"";position:absolute;inset:0;background:repeating-linear-gradient(90deg,transparent 0 13px,#fff 13px 15px);opacity:.82}.needle{position:absolute;z-index:2;top:-10px;height:46px;border-left:3px solid #fff;transition:left .5s}.needle:before{content:"";position:absolute;top:-8px;left:-7px;border-left:7px solid transparent;border-right:7px solid transparent;border-top:9px solid #fff}.gauge-labels{display:flex;justify-content:space-between;margin-top:4px;font-size:11px;font-weight:700}.unit{text-align:center;margin-top:2px;color:var(--muted);font-size:10px;font-weight:700}.speed-row{display:flex;align-items:baseline;gap:13px;margin:auto 0 14px}#speed{font-size:124px;letter-spacing:-7px;line-height:.7;font-weight:800}.speed-caption{font-size:20px;line-height:20px;font-weight:800}.speed-caption small{font-size:18px;font-weight:500}.temp-gauge{width:92%;margin-top:auto}.temp-gauge .gauge-track{height:16px}.temp-gauge .needle{top:-7px;height:30px}.temp-gauge .gauge-labels{font-size:9px}
.right-panel{padding:65px 13px 9px}.lap-box{height:58px;min-height:0;margin-bottom:9px;display:flex;flex-direction:column;justify-content:center}.lap-time{margin-top:5px;text-align:center;font-size:18px;font-weight:800}
@media(max-height:390px){.dashboard{padding-top:6px;grid-template-rows:18px minmax(0,1fr)}.topbar{height:18px}.left-panel{padding-top:25px}.telemetry-panel{padding-top:24px}.right-panel{padding-top:56px}#speed{font-size:114px}.lap-box{height:53px!important;margin-bottom:7px!important}.mode{height:18px}.sector{width:calc(100% - 14px);margin-left:14px}.sector-values{font-size:13px}.sector-values span{font-size:14px}}@media(max-width:700px){.dashboard{padding-left:12px;padding-right:12px;gap:8px}.topbar-left{gap:10px}.topbar-right{gap:8px}}
</style></head><body><main class="dashboard">
<div class="topbar"><div class="topbar-left"><span>Circuit: <span id="circuit"></span></span><span>Driver: <span id="driver"></span></span></div><div class="topbar-right"><span>GPS: <span id="gps"></span></span><span>SAT: <span id="sat"></span></span></div></div>
<section class="column"><div class="panel left-panel"><div class="panel-title">SECTOR</div><div class="box"><h2 class="box-title">DELTA TIME</h2><div id="delta">+0.000 s</div></div><div class="box sector"><h2 class="box-title">LAST SECTOR</h2><div class="sector-values">S1 <span id="s1">--</span><br>S2 <span id="s2">--</span><br>S3 <span id="s3">--</span></div></div><div class="modes"><div class="mode">WARM UP</div><div class="mode active">HAMMER TIME</div><div class="mode">COOL DOWN</div></div></div></section>
<section class="column"><div class="panel telemetry-panel"><div class="panel-title">TELEMETRY</div><div class="gauge"><div class="gauge-track" id="rpmTrack"><div class="needle" id="rpmNeedle"></div></div><div class="gauge-labels"><span>0</span><span>2</span><span>4</span><span>6</span><span>8</span><span>10</span><span>12</span><span>14</span><span>16</span><span>18</span><span>20</span></div><div class="unit">RPM (x1000)</div></div><div class="speed-row"><strong id="speed">0</strong><div class="speed-caption">SPEED<br><small>km/h</small></div></div><div class="gauge temp-gauge"><div class="gauge-track" id="tempTrack"><div class="needle" id="tempNeedle"></div></div><div class="gauge-labels"><span>20°C</span><span>60°C</span><span>100°C</span><span>140°C</span><span>180°C</span><span>220°C</span><span>250°C</span></div><div class="unit">TEMP (°C)</div></div></div></section>
<section class="column"><div class="panel right-panel"><div class="panel-title">LAP</div><div class="box lap-box"><h2 class="box-title">BEST LAP</h2><div id="bestLap" class="lap-time">--:--.---</div></div><div class="box lap-box"><h2 class="box-title">LAST LAP</h2><div id="lastLap" class="lap-time">--:--.---</div></div><div class="box lap-box"><h2 class="box-title">IDEAL LAP</h2><div id="idealLap" class="lap-time">--:--.---</div></div></div></section>
</main><script>
const fmt=v=>{const m=Math.floor(v/60),s=v-m*60;return `${m}:${s.toFixed(3).padStart(6,'0')}`};const gauge=(t,n,p)=>{p=Math.max(0,Math.min(100,p));t.style.setProperty('--fill',p+'%');n.style.left=p+'%'};async function refresh(){try{const d=await(await fetch('/api/telemetry',{cache:'no-store'})).json();speed.textContent=d.speed;circuit.textContent=d.circuit;driver.textContent=d.driver;gps.textContent=d.gps;sat.textContent=d.sat;delta.textContent=(d.delta>=0?'+':'')+d.delta.toFixed(3)+' s';delta.className=d.delta>=0?'delta-positive':'delta-negative';s1.textContent=d.sectors[0].toFixed(2)+' s';s2.textContent=d.sectors[1].toFixed(2)+' s';s3.textContent=d.sectors[2].toFixed(2)+' s';bestLap.textContent=fmt(d.best_lap);lastLap.textContent=fmt(d.last_lap);idealLap.textContent=fmt(d.ideal_lap);gauge(rpmTrack,rpmNeedle,d.rpm/20000*100);gauge(tempTrack,tempNeedle,(d.temp-20)/230*100)}catch(e){console.error(e)}}refresh();setInterval(refresh,750);
</script></body></html>
'''

def clamp(v, a, b): return max(a, min(b, v))
def simulate():
    phase=time.time()/4
    state["speed"]=int(clamp(72+22*math.sin(phase)+random.uniform(-4,4),35,112))
    state["rpm"]=int(clamp(4500+state["speed"]*82+random.uniform(-450,450),2500,14500))
    state["temp"]=round(clamp(state["temp"]+random.uniform(-.7,.8),75,112),1)
    state["delta"]=round(clamp(state["delta"]+random.uniform(-.07,.07),-.650,.650),3)
    state["sectors"]=[round(base+random.uniform(-.15,.18),2) for base in (18.42,22.18,16.77)]
    state["last_lap"]=round(sum(state["sectors"]),3);state["best_lap"]=min(state["best_lap"],state["last_lap"]);state["ideal_lap"]=round(min(state["ideal_lap"],state["best_lap"]-random.uniform(.002,.01)),3);state["updated_at"]=datetime.now().isoformat(timespec="seconds")
    return state
@app.route('/')
def dashboard(): return render_template_string(HTML)
@app.route('/api/telemetry')
def api(): return jsonify(simulate())
if __name__=='__main__': app.run(host='0.0.0.0',port=5001,debug=True)
