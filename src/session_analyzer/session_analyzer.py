#!/usr/bin/env python3
r"""
Session Analyzer — tool web standalone in un unico file.

Avvio (da terminale con env già impostate):
    python session_analyzer.py

Avvio (con file .env accanto a questo script):
    crea un file ".env" nella stessa cartella con:
        MAPBOX_TOKEN=pk.xxx
        SESSIONS_DIR=C:\percorso\sessions
    poi lancia:
        python session_analyzer.py

Variabili d'ambiente riconosciute:
    MAPBOX_TOKEN     token Mapbox (pk.xxx). Necessario per la mappa.
    SESSIONS_DIR     cartella delle sessioni. Default: ../sessions
                     (relativa a questo file .py).
    ANALYZER_HOST    host di ascolto (default 127.0.0.1)
    ANALYZER_PORT    porta di ascolto (default 5001)
"""

import json
import os
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request


# =============================================================
# Bootstrap: .env + variabili d'ambiente
# =============================================================

BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv_if_present():
    """Cerca .env in ordine: accanto allo script, poi nella root del progetto."""
    candidates = [
        BASE_DIR / ".env",             # lib/.env
        BASE_DIR.parent / ".env",      # <root progetto>/.env  ← il tuo
        Path.cwd() / ".env",           # eventuale .env nella cwd
    ]

    env_path = None
    for c in candidates:
        if c.is_file():
            env_path = c
            break

    if env_path is None:
        print("[env] nessun file .env trovato in:",
              ", ".join(str(c) for c in candidates))
        return

    print(f"[env] carico {env_path}")

    try:
        text = env_path.read_text(encoding="utf-8-sig")  # gestisce BOM
    except OSError as e:
        print(f"[env] errore lettura: {e}")
        return

    loaded = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]

        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1

    print(f"[env] {loaded} variabili caricate")

load_dotenv_if_present()


# =============================================================
# Config
# =============================================================

DEFAULT_FOLDER = os.environ.get(
    "SESSIONS_DIR",
    str(BASE_DIR.parent / "sessions"),
)

MAPBOX_TOKEN = os.environ.get("MAPBOX_TOKEN", "").strip()

HOST = os.environ.get("ANALYZER_HOST", "127.0.0.1")
PORT = int(os.environ.get("ANALYZER_PORT", "5001"))

app = Flask(__name__)


HTML = r"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Session Analyzer</title>

<link href="https://api.mapbox.com/mapbox-gl-js/v3.0.0/mapbox-gl.css" rel="stylesheet"/>
<script src="https://api.mapbox.com/mapbox-gl-js/v3.0.0/mapbox-gl.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>

<style>
:root{
    --bg:#0b0d10;
    --panel:#14181d;
    --panel-2:#1a1f26;
    --border:#242a33;
    --text:#e6edf3;
    --muted:#8b96a3;
    --accent:#7c5cff;
    --accent-2:#22d3ee;
    --green:#22c55e;
    --yellow:#eab308;
    --red:#ef4444;
}
*{box-sizing:border-box;}
html,body{
    margin:0;padding:0;height:100%;
    background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    font-size:14px;overflow:hidden;
}
button,input,select{font-family:inherit;font-size:inherit;color:inherit;}
button{
    background:var(--panel-2);border:1px solid var(--border);border-radius:8px;
    padding:8px 12px;cursor:pointer;transition:background .15s,border-color .15s;
}
button:hover:not(:disabled){background:#232a33;border-color:#333c47;}
button:disabled{opacity:.5;cursor:not-allowed;}
input[type="text"]{
    background:var(--panel-2);border:1px solid var(--border);border-radius:8px;
    padding:8px 10px;width:100%;outline:none;transition:border-color .15s;
}
input[type="text"]:focus{border-color:var(--accent);}

.app{display:grid;grid-template-columns:300px 1fr;height:100vh;overflow:hidden;}

.sidebar{
    background:var(--panel);border-right:1px solid var(--border);
    display:flex;flex-direction:column;overflow:hidden;min-height:0;
}
.sidebar-inner{padding:16px;overflow-y:auto;flex:1;min-height:0;}
.sidebar h1{font-size:16px;margin:0 0 4px;letter-spacing:.3px;}
.sidebar .subtitle{color:var(--muted);font-size:12px;margin-bottom:16px;}

.section{margin-bottom:20px;}
.section-title{
    font-size:11px;text-transform:uppercase;letter-spacing:1.2px;color:var(--muted);
    margin-bottom:8px;display:flex;justify-content:space-between;align-items:center;
}
.section-title .count{
    background:var(--panel-2);padding:1px 7px;border-radius:10px;font-size:10px;color:var(--text);
}

.row{display:flex;gap:6px;align-items:center;}
.row > *{min-width:0;}
.row .grow{flex:1;}

.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;}
.muted{color:var(--muted);font-size:11px;line-height:1.4;word-break:break-all;}

.status-line{
    display:flex;align-items:center;gap:6px;font-size:11px;margin-top:6px;
}
.dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto;}
.dot.ok{background:var(--green);box-shadow:0 0 6px rgba(34,197,94,.6);}
.dot.err{background:var(--red);}

.session-list{display:flex;flex-direction:column;gap:4px;max-height:230px;overflow-y:auto;}
.session-item{
    padding:8px 10px;border-radius:8px;cursor:pointer;
    border:1px solid transparent;transition:background .12s;
}
.session-item:hover{background:var(--panel-2);}
.session-item.active{background:rgba(124,92,255,.14);border-color:rgba(124,92,255,.4);}
.session-item .name{
    font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.session-item .meta{
    font-size:11px;color:var(--muted);margin-top:2px;
    display:flex;justify-content:space-between;gap:8px;
}

.lap-list{display:flex;flex-direction:column;gap:4px;max-height:340px;overflow-y:auto;}
.lap-item{
    display:grid;grid-template-columns:auto auto 1fr auto;align-items:center;gap:8px;
    padding:7px 10px;border-radius:8px;cursor:pointer;user-select:none;
    border:1px solid transparent;transition:background .12s;
}
.lap-item:hover{background:var(--panel-2);}
.lap-item.checked{background:rgba(124,92,255,.10);border-color:rgba(124,92,255,.30);}
.lap-item.invalid{opacity:.55;}
.lap-swatch{width:12px;height:12px;border-radius:3px;flex:0 0 auto;}
.lap-num{font-weight:700;font-size:13px;min-width:26px;}
.lap-time{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;}
.lap-badge{font-size:10px;padding:1px 6px;border-radius:6px;background:var(--panel-2);color:var(--muted);}
.lap-badge.valid{color:var(--green);background:rgba(34,197,94,.10);}
.lap-badge.invalid{color:var(--red);background:rgba(239,68,68,.10);}

.main{display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden;}
.main-scroll{overflow-y:auto;padding:16px;flex:1;min-height:0;}

.panel{
    background:var(--panel);border:1px solid var(--border);border-radius:12px;
    overflow:hidden;margin-bottom:16px;
}
.panel-head{
    padding:10px 14px;border-bottom:1px solid var(--border);
    font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:1.1px;
    color:var(--muted);display:flex;justify-content:space-between;align-items:center;
}
.panel-body{padding:14px;}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;}
.stat{background:var(--panel-2);border-radius:10px;padding:12px 14px;}
.stat-label{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:4px;}
.stat-value{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;}
.stat-value.small{font-size:15px;}

.map-wrap{
    position:relative;height:520px;border-radius:12px;overflow:hidden;
    background:#0e1116;border:1px solid var(--border);margin-bottom:16px;
    transition:border-radius .2s;
}
#map{position:absolute;inset:0;}

/* Modalità schermo intero mappa */
.map-wrap.fullscreen{
    position:fixed;
    inset:0;
    height:100vh;
    width:100vw;
    margin:0;
    border-radius:0;
    border:none;
    z-index:9999;
}

.map-overlay{
    position:absolute;inset:0;background:rgba(11,13,16,.92);
    display:flex;flex-direction:column;align-items:center;justify-content:center;
    padding:32px;text-align:center;z-index:5;backdrop-filter:blur(4px);
}
.map-overlay.hidden{display:none;}
.map-overlay h3{margin:0 0 8px;font-size:16px;}
.map-overlay p{color:var(--muted);font-size:13px;max-width:420px;margin:0 0 16px;line-height:1.5;}
.map-overlay code{
    background:var(--panel-2);padding:2px 6px;border-radius:4px;
    font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;
}

.map-fs-btn{
    position:absolute;top:12px;left:12px;z-index:6;
    width:36px;height:36px;padding:0;
    display:flex;align-items:center;justify-content:center;
    background:rgba(11,13,16,.75);border:1px solid var(--border);
    border-radius:8px;cursor:pointer;font-size:16px;line-height:1;
    backdrop-filter:blur(6px);color:var(--text);
    transition:background .15s,border-color .15s;
}
.map-fs-btn:hover{background:rgba(35,42,51,.9);border-color:#333c47;}

.legend{
    position:absolute;bottom:12px;left:12px;
    background:rgba(11,13,16,.85);border:1px solid var(--border);border-radius:8px;
    padding:8px 10px;display:flex;flex-direction:column;gap:4px;z-index:4;
    max-height:180px;overflow-y:auto;font-size:12px;backdrop-filter:blur(6px);
    min-width:200px;
}
.map-wrap.fullscreen .legend{max-height:60vh;}
.legend-item{display:flex;align-items:center;gap:8px;}
.legend-item .swatch{width:14px;height:3px;border-radius:2px;}

/* =====================================================
   CHART
   ===================================================== */

.charts-grid{display:grid;grid-template-columns:1fr;gap:16px;}

.chart-panel{
    background:var(--panel);
    border:1px solid var(--border);
    border-radius:12px;
    padding:14px;
    display:flex;
    flex-direction:column;
    transition:border-radius .2s;
    min-height:0;
}

.chart-header{
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:8px;
    margin-bottom:10px;
    flex:0 0 auto;
}

.chart-title{
    font-size:12px;
    text-transform:uppercase;
    letter-spacing:1.1px;
    color:var(--muted);
    margin:0;
}

.chart-fs-btn{
    width:30px;height:30px;padding:0;
    display:flex;align-items:center;justify-content:center;
    background:var(--panel-2);border:1px solid var(--border);
    border-radius:6px;cursor:pointer;font-size:14px;line-height:1;
    color:var(--text);
    transition:background .15s,border-color .15s;
    flex:0 0 auto;
}
.chart-fs-btn:hover{background:#232a33;border-color:#333c47;}

.chart-container{position:relative;height:280px;flex:0 0 auto;min-height:0;}

/* Modalità schermo intero grafico */
.chart-panel.fullscreen{
    position:fixed;
    inset:0;
    z-index:9999;
    border-radius:0;
    border:none;
    background:var(--bg);
    padding:20px;
    margin:0;
}
.chart-panel.fullscreen .chart-container{
    flex:1 1 auto;
    height:auto;
    min-height:0;
}

.tables-grid{display:grid;grid-template-columns:1fr;gap:16px;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);}
th{
    font-size:11px;text-transform:uppercase;letter-spacing:.8px;
    color:var(--muted);font-weight:600;background:var(--panel-2);
}
td.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums;}
tr.lap-row:hover{background:var(--panel-2);cursor:pointer;}
tr.lap-row.selected{background:rgba(124,92,255,.08);}

.empty-state{text-align:center;padding:40px 20px;color:var(--muted);font-size:13px;}

.badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:600;}
.badge.valid{background:rgba(34,197,94,.14);color:var(--green);}
.badge.invalid{background:rgba(239,68,68,.14);color:var(--red);}
.badge.best{background:rgba(124,92,255,.16);color:#b9a3ff;}
.badge.improved{background:rgba(34,197,94,.14);color:var(--green);}
.badge.slower{background:rgba(234,179,8,.14);color:var(--yellow);}

.error{
    background:rgba(239,68,68,.10);border:1px solid rgba(239,68,68,.3);
    color:#ffb1b1;padding:10px 12px;border-radius:8px;font-size:12px;margin-bottom:12px;
}

.toast{
    position:fixed;bottom:20px;right:20px;background:var(--panel);
    border:1px solid var(--border);padding:10px 14px;border-radius:10px;
    z-index:100;font-size:13px;opacity:0;transform:translateY(10px);
    transition:opacity .2s,transform .2s;pointer-events:none;
}
.toast.show{opacity:1;transform:translateY(0);}

::-webkit-scrollbar{width:8px;height:8px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:#2a313b;border-radius:4px;}
::-webkit-scrollbar-thumb:hover{background:#353d48;}
</style>
</head>

<body>
<div class="app">

    <aside class="sidebar">
        <div class="sidebar-inner">

            <h1>Session Analyzer</h1>
            <div class="subtitle">Leggi, confronta, visualizza.</div>

            <div class="section">
                <div class="section-title">Mapbox</div>
                <div class="status-line" id="tokenStatus">
                    <span class="dot err"></span>
                    <span>Verifica in corso…</span>
                </div>
            </div>

            <div class="section">
                <div class="section-title">Cartella sessioni</div>
                <div class="row">
                    <input id="folderInput" type="text" class="grow mono"/>
                    <button id="refreshBtn" title="Ricarica elenco">↻</button>
                </div>
                <div class="muted" id="folderResolved" style="margin-top:6px;"></div>
            </div>

            <div class="section">
                <div class="section-title">
                    Sessioni <span class="count" id="sessionCount">0</span>
                </div>
                <div class="session-list" id="sessionList">
                    <div class="empty-state" style="padding:16px 4px;">
                        Nessuna sessione caricata.
                    </div>
                </div>
            </div>

            <div class="section">
                <div class="section-title">
                    Giri
                    <span>
                        <button id="selectAllBtn" style="padding:2px 8px;font-size:10px;" disabled>tutti</button>
                        <button id="clearAllBtn" style="padding:2px 8px;font-size:10px;" disabled>nessuno</button>
                    </span>
                </div>
                <div class="lap-list" id="lapList">
                    <div class="empty-state" style="padding:16px 4px;">
                        Seleziona una sessione.
                    </div>
                </div>
            </div>

        </div>
    </aside>

    <main class="main">
        <div class="main-scroll">

            <div id="errorBox"></div>

            <div class="panel">
                <div class="panel-head">
                    <span>Panoramica sessione</span>
                    <span id="sessionMeta" style="color:var(--muted);text-transform:none;letter-spacing:0;">—</span>
                </div>
                <div class="panel-body">
                    <div class="stats">
                        <div class="stat">
                            <div class="stat-label">Driver</div>
                            <div class="stat-value small" id="statDriver">—</div>
                        </div>
                        <div class="stat">
                            <div class="stat-label">Circuito</div>
                            <div class="stat-value small" id="statTrack">—</div>
                        </div>
                        <div class="stat">
                            <div class="stat-label">Stato</div>
                            <div class="stat-value small" id="statStatus">—</div>
                        </div>
                        <div class="stat">
                            <div class="stat-label">Giri validi</div>
                            <div class="stat-value" id="statLaps">—</div>
                        </div>
                        <div class="stat">
                            <div class="stat-label">Best lap</div>
                            <div class="stat-value" id="statBest">—</div>
                        </div>
                        <div class="stat">
                            <div class="stat-label">Ideal lap</div>
                            <div class="stat-value" id="statIdeal">—</div>
                        </div>
                        <div class="stat">
                            <div class="stat-label">Durata</div>
                            <div class="stat-value small" id="statDuration">—</div>
                        </div>
                        <div class="stat">
                            <div class="stat-label">Campioni</div>
                            <div class="stat-value small" id="statSamples">—</div>
                        </div>
                    </div>
                </div>
            </div>

            <div class="map-wrap" id="mapWrap">
                <div id="map"></div>
                <button class="map-fs-btn" id="mapFsBtn" title="Schermo intero (ESC per uscire)">⛶</button>
                <div class="legend" id="legend" style="display:none;"></div>
                <div class="map-overlay" id="mapOverlay">
                    <h3>Caricamento…</h3>
                    <p>Attendere configurazione.</p>
                </div>
            </div>

            <div class="charts-grid">
                <div class="chart-panel" id="chartPanelSpeed">
                    <div class="chart-header">
                        <div class="chart-title">Velocità (km/h) vs distanza (m)</div>
                        <button class="chart-fs-btn" data-chart="speed" title="Schermo intero (ESC per uscire)">⛶</button>
                    </div>
                    <div class="chart-container"><canvas id="speedChart"></canvas></div>
                </div>
                <div class="chart-panel" id="chartPanelRpm">
                    <div class="chart-header">
                        <div class="chart-title">RPM vs distanza (m)</div>
                        <button class="chart-fs-btn" data-chart="rpm" title="Schermo intero (ESC per uscire)">⛶</button>
                    </div>
                    <div class="chart-container"><canvas id="rpmChart"></canvas></div>
                </div>
                <div class="chart-panel" id="chartPanelDelta">
                    <div class="chart-header">
                        <div class="chart-title">Delta live (s) vs distanza (m)</div>
                        <button class="chart-fs-btn" data-chart="delta" title="Schermo intero (ESC per uscire)">⛶</button>
                    </div>
                    <div class="chart-container"><canvas id="deltaChart"></canvas></div>
                </div>
            </div>

            <div class="chart-panel" id="chartPanelDataAge" style="margin-top:16px;">
                <div class="chart-header">
                    <div class="chart-title">Età del dato MQTT (s) vs tempo sessione</div>
                    <button class="chart-fs-btn" data-chart="dataAge" title="Schermo intero (ESC per uscire)">⛶</button>
                </div>
                <div id="dataAgeSummary" class="muted" style="margin-bottom:10px;">
                    Carica una sessione per vedere i campioni MQTT.
                </div>
                <div class="chart-container"><canvas id="dataAgeChart"></canvas></div>
            </div>

            <div class="panel" style="margin-top:16px;">
                <div class="panel-head">
                    <span>Intervalli MQTT superiori a 1 secondo</span>
                    <span id="dataAgeAnomalyCount" style="text-transform:none;letter-spacing:0;color:var(--muted);font-weight:400;">—</span>
                </div>
                <div class="panel-body" style="padding:0;max-height:340px;overflow:auto;">
                    <table>
                        <thead>
                            <tr>
                                <th style="width:220px;">Orario</th>
                                <th style="width:160px;">Tempo sessione</th>
                                <th>Età del dato</th>
                            </tr>
                        </thead>
                        <tbody id="dataAgeAnomaliesBody">
                            <tr><td colspan="3" class="empty-state">Nessuna sessione caricata.</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="tables-grid" style="margin-top:16px;">
                <div class="panel">
                    <div class="panel-head">
                        <span>Giri</span>
                        <span style="text-transform:none;letter-spacing:0;color:var(--muted);font-weight:400;">
                            clicca una riga per selezionare il giro
                        </span>
                    </div>
                    <div class="panel-body" style="padding:0;">
                        <table>
                            <thead>
                                <tr>
                                    <th style="width:60px;">Giro</th>
                                    <th style="width:110px;">Tempo</th>
                                    <th>S1</th>
                                    <th>S2</th>
                                    <th>S3</th>
                                    <th style="width:110px;">Distanza</th>
                                    <th style="width:110px;">Campioni</th>
                                    <th style="width:110px;">Stato</th>
                                </tr>
                            </thead>
                            <tbody id="lapsTableBody">
                                <tr><td colspan="8" class="empty-state">Nessuna sessione caricata.</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>

                <div class="panel">
                    <div class="panel-head">
                        <span>Eventi</span>
                        <span style="text-transform:none;letter-spacing:0;color:var(--muted);font-weight:400;">
                            settori, giri completati, cambi stato
                        </span>
                    </div>
                    <div class="panel-body" style="padding:0;">
                        <table>
                            <thead>
                                <tr>
                                    <th style="width:70px;">Giro</th>
                                    <th style="width:140px;">Evento</th>
                                    <th style="width:180px;">Orario</th>
                                    <th>Dettagli</th>
                                </tr>
                            </thead>
                            <tbody id="eventsTableBody">
                                <tr><td colspan="4" class="empty-state">Nessun evento.</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

        </div>
    </main>

</div>

<div class="toast" id="toast"></div>

<script>
const PALETTE = [
    "#7c5cff", "#22d3ee", "#f472b6", "#22c55e",
    "#eab308", "#f97316", "#ef4444", "#a78bfa",
    "#14b8a6", "#38bdf8", "#fb7185", "#84cc16",
];

const state = {
    folder: "",
    sessions: [],
    session: null,
    selectedLaps: new Set(),
    mapboxReady: false,
    map: null,
    mapLoaded: false,
    charts: { speed: null, rpm: null, delta: null, dataAge: null },
    config: null,
    isMapFullscreen: false,
};

function $(id){ return document.getElementById(id); }

function fmtTime(seconds){
    if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
    const m = Math.floor(seconds / 60);
    const s = seconds - m * 60;
    return m + ":" + s.toFixed(3).padStart(6, "0");
}

function fmtDuration(seconds){
    if (!Number.isFinite(seconds)) return "—";
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return h + "h " + m + "m";
    if (m > 0) return m + "m " + s + "s";
    return s + "s";
}

function fmtBytes(bytes){
    if (!Number.isFinite(bytes)) return "—";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + " KB";
    return (bytes/1024/1024).toFixed(2) + " MB";
}

function fmtDate(epoch){
    if (!Number.isFinite(epoch)) return "—";
    const d = new Date(epoch * 1000);
    return d.toLocaleString("it-IT", {
        day: "2-digit", month: "2-digit",
        hour: "2-digit", minute: "2-digit",
    });
}

function toast(msg, ms){
    const el = $("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove("show"), ms || 2200);
}

function showError(html){
    $("errorBox").innerHTML = html
        ? '<div class="error">' + html + '</div>'
        : "";
}

function escapeHtml(s){
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

/* ============================================================
   FULLSCREEN — mappa + grafici
   ============================================================ */

function toggleMapFullscreen(){
    const wrap = $("mapWrap");

    if (!state.isMapFullscreen){
        if (wrap.requestFullscreen){
            wrap.requestFullscreen().catch(() => applyMapFullscreenClass(true));
        } else if (wrap.webkitRequestFullscreen){
            wrap.webkitRequestFullscreen();
        } else {
            applyMapFullscreenClass(true);
        }
    } else {
        if (document.exitFullscreen){
            document.exitFullscreen().catch(() => applyMapFullscreenClass(false));
        } else if (document.webkitExitFullscreen){
            document.webkitExitFullscreen();
        } else {
            applyMapFullscreenClass(false);
        }
    }
}

function applyMapFullscreenClass(on){
    const wrap = $("mapWrap");
    const btn = $("mapFsBtn");
    state.isMapFullscreen = on;
    if (on){
        wrap.classList.add("fullscreen");
        btn.textContent = "✕";
        btn.title = "Esci da schermo intero (ESC)";
    } else {
        wrap.classList.remove("fullscreen");
        btn.textContent = "⛶";
        btn.title = "Schermo intero (ESC per uscire)";
    }
    setTimeout(() => {
        if (state.map) state.map.resize();
    }, 120);
}

function toggleChartFullscreen(panelEl){
    if (panelEl.classList.contains("fullscreen")){
        if (document.exitFullscreen){
            document.exitFullscreen().catch(() => applyChartFullscreenClass(panelEl, false));
        } else if (document.webkitExitFullscreen){
            document.webkitExitFullscreen();
        } else {
            applyChartFullscreenClass(panelEl, false);
        }
    } else {
        if (panelEl.requestFullscreen){
            panelEl.requestFullscreen().catch(() => applyChartFullscreenClass(panelEl, true));
        } else if (panelEl.webkitRequestFullscreen){
            panelEl.webkitRequestFullscreen();
        } else {
            applyChartFullscreenClass(panelEl, true);
        }
    }
}

function applyChartFullscreenClass(panelEl, on){
    document.querySelectorAll(".chart-panel").forEach(p => {
        const isTarget = (p === panelEl) && on;
        p.classList.toggle("fullscreen", isTarget);
        const b = p.querySelector(".chart-fs-btn");
        if (b){
            b.textContent = isTarget ? "✕" : "⛶";
            b.title = isTarget ? "Esci da schermo intero (ESC)"
                               : "Schermo intero (ESC per uscire)";
        }
    });
    // Fai ridimensionare Chart.js dopo la transizione
    setTimeout(() => {
        Object.keys(state.charts).forEach(k => {
            const c = state.charts[k];
            if (c) c.resize();
        });
    }, 150);
}

function onFullscreenChange(){
    const fsEl = document.fullscreenElement || document.webkitFullscreenElement;

    // --- Mappa ---
    const wrap = $("mapWrap");
    const mapIsFs = (fsEl === wrap);
    if (mapIsFs && !state.isMapFullscreen){
        applyMapFullscreenClass(true);
    } else if (!mapIsFs && state.isMapFullscreen && !fsEl){
        applyMapFullscreenClass(false);
    }

    // --- Grafici ---
    const chartPanels = document.querySelectorAll(".chart-panel");
    let anyChartFs = false;
    chartPanels.forEach(p => {
        if (fsEl === p){
            anyChartFs = true;
            if (!p.classList.contains("fullscreen")){
                applyChartFullscreenClass(p, true);
            }
        } else if (p.classList.contains("fullscreen") && fsEl !== p){
            // Se era in fullscreen e non lo è più, ripristina
            p.classList.remove("fullscreen");
            const b = p.querySelector(".chart-fs-btn");
            if (b){
                b.textContent = "⛶";
                b.title = "Schermo intero (ESC per uscire)";
            }
        }
    });

    // Se siamo usciti da fullscreen nativo e nessun chart è fullscreen, ridimensiona
    if (!fsEl && !anyChartFs){
        setTimeout(() => {
            Object.keys(state.charts).forEach(k => {
                const c = state.charts[k];
                if (c) c.resize();
            });
            if (state.map) state.map.resize();
        }, 150);
    } else if (anyChartFs){
        setTimeout(() => {
            Object.keys(state.charts).forEach(k => {
                const c = state.charts[k];
                if (c) c.resize();
            });
        }, 150);
    }
}

document.addEventListener("fullscreenchange", onFullscreenChange);
document.addEventListener("webkitfullscreenchange", onFullscreenChange);

/* ============================================================
   CONFIG / MAPBOX
   ============================================================ */

async function loadConfig(){
    const r = await fetch("/api/config", { cache: "no-store" });
    const cfg = await r.json();
    state.config = cfg;

    const tokenEl = $("tokenStatus");
    if (cfg.has_mapbox_token){
        tokenEl.innerHTML =
            '<span class="dot ok"></span>' +
            '<span>Token caricato da <strong>MAPBOX_TOKEN</strong></span>';
    } else {
        tokenEl.innerHTML =
            '<span class="dot err"></span>' +
            '<span>MAPBOX_TOKEN non impostato — vedi istruzioni</span>';
    }

    $("folderInput").value = cfg.default_folder || "";
    if (cfg.default_folder_exists){
        $("folderResolved").textContent = "✓ " + cfg.default_folder;
    } else {
        $("folderResolved").innerHTML =
            '<span style="color:var(--red);">✗ cartella non trovata</span><br>' +
            escapeHtml(cfg.default_folder || "");
    }

    if (cfg.has_mapbox_token){
        initMapbox(cfg.mapbox_token);
    } else {
        state.mapboxReady = false;
        updateMapOverlay();
    }
}

function initMapbox(token){
    if (typeof mapboxgl === "undefined"){
        showError("Mapbox GL JS non caricato. Controlla la connessione.");
        return;
    }
    try {
        mapboxgl.accessToken = token;
        state.mapboxReady = true;

        state.map = new mapboxgl.Map({
            container: "map",
            style: "mapbox://styles/mapbox/dark-v11",
            center: [0, 0],
            zoom: 2,
            attributionControl: false,
        });

        state.map.addControl(
            new mapboxgl.NavigationControl({ showCompass: false }), "top-right"
        );
        state.map.addControl(new mapboxgl.AttributionControl({ compact: true }));

        state.map.on("load", () => {
            state.mapLoaded = true;
            renderMapLayers();
        });

        state.map.on("error", (e) => {
            console.error("Mapbox error", e);
            const msg = (e && e.error && e.error.message) || "";
            if (/401|403|Unauthorized|Forbidden/i.test(msg)){
                showError(
                    "Token Mapbox rifiutato. Controlla la variabile " +
                    "<code>MAPBOX_TOKEN</code> e riavvia il tool."
                );
            }
        });
    } catch (e){
        showError("Errore inizializzazione Mapbox: " + escapeHtml(e.message));
    }
}

/* ============================================================
   MAPPA — gestione layer giro singolo / multiplo
   ============================================================ */

function lerp(a, b, t){ return a + (b - a) * t; }

function buildSpeedGradient(speeds){
    const finite = speeds.filter(Number.isFinite);
    if (!finite.length){
        return "#7c5cff";
    }
    const min = Math.min.apply(null, finite);
    const max = Math.max.apply(null, finite);
    const range = (max - min) || 1;

    const n = speeds.length;
    const stops = [];
    const maxStops = 80;
    const step = Math.max(1, Math.floor(n / maxStops));

    function pushStop(i){
        const s = speeds[i];
        if (!Number.isFinite(s)) return;
        const t = (s - min) / range;
        const progress = n > 1 ? i / (n - 1) : 0;

        let r, g, b;
        if (t < 0.5){
            const k = t * 2;
            r = lerp(239, 234, k);
            g = lerp(68, 179, k);
            b = lerp(68, 8, k);
        } else {
            const k = (t - 0.5) * 2;
            r = lerp(234, 34, k);
            g = lerp(179, 197, k);
            b = lerp(8, 94, k);
        }
        stops.push(progress, "rgb(" + Math.round(r) + "," + Math.round(g) + "," + Math.round(b) + ")");
    }

    for (let i = 0; i < n; i += step) pushStop(i);
    if ((n - 1) % step !== 0) pushStop(n - 1);

    if (stops.length < 4){
        stops.length = 0;
        stops.push(0, "rgb(239,68,68)", 1, "rgb(34,197,94)");
    }

    return ["interpolate", ["linear"], ["line-progress"]].concat(stops);
}

function clearMapLayers(){
    const map = state.map;
    if (!map || !state.mapLoaded) return;
    const style = map.getStyle();
    if (!style) return;
    const layers = style.layers || [];
    for (const l of layers){
        if (l.id.startsWith("lap-layer-") && map.getLayer(l.id)){
            map.removeLayer(l.id);
        }
    }
    const sources = Object.keys(style.sources || {});
    for (const s of sources){
        if (s.startsWith("lap-src-") && map.getSource(s)){
            map.removeSource(s);
        }
    }
}

function addGradientLap(lap, coords, speedAt){
    const map = state.map;
    const id = lap.lap_number;
    const srcId = "lap-src-" + id;
    const layerId = "lap-layer-" + id;

    if (!map.getSource(srcId)){
        map.addSource(srcId, {
            type: "geojson",
            lineMetrics: true,
            data: { type: "FeatureCollection", features: [] },
        });
    }
    if (!map.getLayer(layerId)){
        map.addLayer({
            id: layerId,
            type: "line",
            source: srcId,
            layout: { "line-join": "round", "line-cap": "round" },
            paint: {
                "line-width": [
                    "interpolate", ["linear"], ["zoom"],
                    10, 3, 14, 5, 18, 8,
                ],
                "line-opacity": 0.95,
                "line-gradient": buildSpeedGradient(speedAt),
            },
        });
    }
    map.getSource(srcId).setData({
        type: "FeatureCollection",
        features: [{
            type: "Feature",
            properties: { lap: lap.lap_number },
            geometry: { type: "LineString", coordinates: coords },
        }],
    });
}

function addSolidLap(lap, coords, color){
    const map = state.map;
    const id = lap.lap_number;
    const srcId = "lap-src-" + id;
    const layerId = "lap-layer-" + id;

    if (!map.getSource(srcId)){
        map.addSource(srcId, {
            type: "geojson",
            data: { type: "FeatureCollection", features: [] },
        });
    }
    if (!map.getLayer(layerId)){
        map.addLayer({
            id: layerId,
            type: "line",
            source: srcId,
            layout: { "line-join": "round", "line-cap": "round" },
            paint: {
                "line-color": color,
                "line-width": [
                    "interpolate", ["linear"], ["zoom"],
                    10, 2, 14, 3, 18, 5,
                ],
                "line-opacity": 0.9,
            },
        });
    }
    map.getSource(srcId).setData({
        type: "FeatureCollection",
        features: [{
            type: "Feature",
            properties: { lap: lap.lap_number },
            geometry: { type: "LineString", coordinates: coords },
        }],
    });
}

function renderMapLayers(){
    const map = state.map;
    if (!map || !state.mapLoaded) return;

    clearMapLayers();

    const session = state.session;
    if (!session){
        updateMapOverlay();
        updateLegend();
        return;
    }

    const laps = getSessionLaps();
    const selected = [];
    laps.forEach((lap, idx) => {
        if (state.selectedLaps.has(lap.lap_number)) selected.push({ lap, idx });
    });

    const gradientMode = selected.length === 1;

    let bbox = null;

    selected.forEach(({ lap, idx }) => {
        const tp = lap.track_points || {};
        const lats = tp.latitude || [];
        const lons = tp.longitude || [];
        const speeds = tp.speed_kmph || [];
        if (lats.length < 2 || lons.length < 2) return;

        const coords = [];
        const speedAt = [];
        for (let i = 0; i < lats.length; i++){
            const la = lats[i], lo = lons[i];
            if (typeof la === "number" && typeof lo === "number" && !(la === 0 && lo === 0)){
                coords.push([lo, la]);
                speedAt.push(typeof speeds[i] === "number" ? speeds[i] : null);
                if (!bbox) bbox = [lo, la, lo, la];
                else {
                    bbox[0] = Math.min(bbox[0], lo);
                    bbox[1] = Math.min(bbox[1], la);
                    bbox[2] = Math.max(bbox[2], lo);
                    bbox[3] = Math.max(bbox[3], la);
                }
            }
        }
        if (coords.length < 2) return;

        const hasSpeeds = gradientMode && speedAt.some(Number.isFinite);
        if (hasSpeeds){
            addGradientLap(lap, coords, speedAt);
        } else {
            addSolidLap(lap, coords, PALETTE[idx % PALETTE.length]);
        }
    });

    if (bbox){
        map.fitBounds(
            [[bbox[0], bbox[1]], [bbox[2], bbox[3]]],
            { padding: 60, duration: 500, maxZoom: 18 }
        );
    }

    updateMapOverlay();
    updateLegend();
}

function updateMapOverlay(){
    const overlay = $("mapOverlay");
    if (!state.config || !state.config.has_mapbox_token){
        overlay.classList.remove("hidden");
        overlay.innerHTML =
            '<h3>Mapbox non configurato</h3>' +
            '<p>Imposta <code>MAPBOX_TOKEN</code> in un file <code>.env</code> ' +
            'accanto a <code>session_analyzer.py</code>, oppure nelle ' +
            'variabili d\'ambiente di sistema.</p>' +
            '<p><code>MAPBOX_TOKEN=pk.xxx</code></p>' +
            '<p>Poi riavvia il tool. Token gratuito su ' +
            '<a href="https://account.mapbox.com/" target="_blank" ' +
            'style="color:var(--accent-2);">account.mapbox.com</a>.</p>';
        return;
    }
    if (state.selectedLaps.size === 0){
        overlay.classList.remove("hidden");
        overlay.innerHTML =
            '<h3>Nessun giro selezionato</h3>' +
            '<p>Seleziona uno o più giri dalla barra laterale ' +
            'per visualizzare le traiettorie sulla mappa.</p>' +
            '<p style="margin-top:12px;color:var(--muted);font-size:12px;">' +
            '1 giro → colorazione per velocità (rosso=lento, verde=veloce)<br>' +
            '2+ giri → colori distinti per confronto</p>';
        return;
    }
    overlay.classList.add("hidden");
}

function updateLegend(){
    const legend = $("legend");
    const laps = getSessionLaps();
    if (!laps.length || state.selectedLaps.size === 0){
        legend.style.display = "none";
        return;
    }

    const selected = [];
    laps.forEach((lap, idx) => {
        if (state.selectedLaps.has(lap.lap_number)) selected.push({ lap, idx });
    });
    if (!selected.length){
        legend.style.display = "none";
        return;
    }

    const items = [];

    if (selected.length === 1){
        const lap = selected[0].lap;
        const speeds = (lap.track_points && lap.track_points.speed_kmph) || [];
        const finite = speeds.filter(Number.isFinite);
        const lapLabel = lap.is_warmup
            ? "Percorso iniziale"
            : "Giro " + lap.lap_number + (lap.is_in_progress ? " (in corso)" : "");
        if (finite.length){
            const min = Math.round(Math.min.apply(null, finite));
            const max = Math.round(Math.max.apply(null, finite));
            items.push(
                '<div class="legend-item"><strong>' + lapLabel +
                ' — velocità (' + fmtTime(lap.lap_time_s) + ')</strong></div>' +
                '<div style="display:flex;align-items:center;gap:6px;margin-top:6px;">' +
                    '<span class="mono" style="font-size:10px;">' + min + '</span>' +
                    '<span style="flex:1;min-width:90px;height:8px;border-radius:4px;' +
                    'background:linear-gradient(to right,#ef4444,#eab308,#22c55e);"></span>' +
                    '<span class="mono" style="font-size:10px;">' + max + '</span>' +
                '</div>' +
                '<div class="muted" style="font-size:10px;text-align:center;">' +
                    'km/h · rosso=lento/freno · verde=veloce</div>'
            );
        } else {
            items.push(
                '<div class="legend-item">' +
                    '<span class="swatch" style="background:' +
                    PALETTE[selected[0].idx % PALETTE.length] + ';"></span>' +
                    '<span>' + lapLabel + ' — ' + fmtTime(lap.lap_time_s) + '</span>' +
                '</div>'
            );
        }
    } else {
        selected.forEach(({ lap, idx }) => {
            const color = PALETTE[idx % PALETTE.length];
            const label = lap.is_warmup
                ? "Percorso iniziale"
                : "Giro " + lap.lap_number + (lap.is_in_progress ? " (in corso)" : "");
            items.push(
                '<div class="legend-item">' +
                    '<span class="swatch" style="background:' + color + ';"></span>' +
                    '<span>' + label + ' — ' + fmtTime(lap.lap_time_s) + '</span>' +
                '</div>'
            );
        });
    }

    legend.innerHTML = items.join("");
    legend.style.display = "flex";
}

/* ============================================================
   CHART
   ============================================================ */

const chartCommonOptions = {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    parsing: false,
    normalized: true,
    interaction: { mode: "nearest", axis: "x", intersect: false },
    plugins: {
        legend: { display: false },
        tooltip: {
            callbacks: {
                title: (items) => {
                    if (!items.length) return "";
                    return "Distanza: " + Math.round(items[0].parsed.x) + " m";
                },
            },
        },
    },
    scales: {
        x: {
            type: "linear",
            title: { display: true, text: "Distanza (m)", color: "#8b96a3" },
            ticks: { color: "#8b96a3", maxTicksLimit: 10 },
            grid: { color: "#1e242c" },
        },
        y: {
            ticks: { color: "#8b96a3" },
            grid: { color: "#1e242c" },
        },
    },
};

function buildDataset(lap, idx, keyY){
    const tp = lap.track_points || {};
    const dist = tp.lap_distance_m || [];
    const ys = tp[keyY] || [];
    const n = Math.min(dist.length, ys.length);
    const data = [];
    for (let i = 0; i < n; i++){
        const x = dist[i], y = ys[i];
        if (typeof x === "number" && typeof y === "number" && Number.isFinite(y)){
            data.push({ x, y });
        }
    }
    const color = PALETTE[idx % PALETTE.length];
    return {
        label: lap.is_warmup
            ? "Percorso iniziale"
            : "Giro " + lap.lap_number + (lap.is_in_progress ? " (in corso)" : ""),
        data,
        borderColor: color,
        backgroundColor: color + "22",
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0.2,
    };
}

function renderCharts(){
    const selected = getSelectedLaps();
    renderChart("speed", "speedChart", selected, "speed_kmph", "km/h");
    renderChart("rpm", "rpmChart", selected, "as5600_rpm", "RPM");
    renderChart("delta", "deltaChart", selected, "delta_live_s", "Δ (s)");
    renderDataAge();
}

function lapEventMarkers(lap){
    const tp = lap.track_points || {};
    const epochs = tp.received_at_epoch || [];
    const elapsed = tp.lap_elapsed_s || [];
    const distances = tp.lap_distance_m || [];
    const pointCount = Math.min(epochs.length, elapsed.length, distances.length);
    const points = [];
    for (let i = 0; i < pointCount; i++){
        const epoch = Number(epochs[i]);
        const lapElapsed = Number(elapsed[i]);
        const distance = Number(distances[i]);
        if (Number.isFinite(epoch) && Number.isFinite(lapElapsed) && Number.isFinite(distance)){
            points.push({ epoch, lapElapsed, distance });
        }
    }

    const startEpoch = Number(lap.started_at_epoch);
    const events = Array.isArray(lap.events) ? lap.events : [];
    const markers = [];
    let previousDistance = 0;
    for (const event of events){
        if (event.event !== "sector_completed" && event.event !== "lap_completed") continue;

        const eventEpoch = event.at ? Date.parse(event.at) / 1000 : NaN;
        if (!Number.isFinite(eventEpoch)) continue;

        let distance;
        if (
            event.event === "lap_completed"
            && lap.lap_distance_m != null
            && Number.isFinite(Number(lap.lap_distance_m))
        ){
            distance = Number(lap.lap_distance_m);
        } else {
            if (points.length < 2) continue;
            let afterIndex = points.findIndex(point => point.epoch >= eventEpoch);
            if (afterIndex < 0) afterIndex = points.length - 1;
            const beforeIndex = Math.max(0, afterIndex - 1);
            const before = points[beforeIndex];
            const after = points[afterIndex];
            const span = after.epoch - before.epoch;
            const fraction = span > 0
                ? Math.max(0, Math.min(1, (eventEpoch - before.epoch) / span))
                : 0;
            distance = before.distance + (after.distance - before.distance) * fraction;
        }

        const isFinish = event.event === "lap_completed";
        const intervalDistance = Math.max(0, distance - previousDistance);
        const label = isFinish
            ? "FIN " + distance.toFixed(0) + "m (+" + intervalDistance.toFixed(0) + "m)"
            : "S" + event.sector_number + " " + distance.toFixed(0) +
              "m (+" + intervalDistance.toFixed(0) + "m)";
        const previousMarker = markers[markers.length - 1];
        if (
            isFinish
            && previousMarker
            && Math.abs(distance - previousMarker.x) <= 1
        ){
            previousMarker.label += " / FIN";
        } else {
            markers.push({ x: distance, label });
        }
        previousDistance = distance;
    }
    return markers;
}

function sessionEventMarkers(laps){
    const session = state.session;
    if (!session) return [];
    const startEpoch = session.started_at ? Date.parse(session.started_at) / 1000 : NaN;
    if (!Number.isFinite(startEpoch)) return [];

    const markers = [];
    for (const lap of laps){
        const lapMarkers = [];
        for (const event of Array.isArray(lap.events) ? lap.events : []){
            if (event.event !== "sector_completed" && event.event !== "lap_completed") continue;
            const eventEpoch = event.at ? Date.parse(event.at) / 1000 : NaN;
            if (!Number.isFinite(eventEpoch)) continue;
            const elapsed = Math.max(0, eventEpoch - startEpoch);
            const isFinish = event.event === "lap_completed";
            const previousMarker = lapMarkers[lapMarkers.length - 1];
            if (
                isFinish
                && previousMarker
                && Math.abs(elapsed - previousMarker.x) <= 0.25
            ){
                previousMarker.label += " / FIN";
                continue;
            }
            lapMarkers.push({
                x: elapsed,
                label: (isFinish ? "FIN" : "S" + event.sector_number) +
                    " @" + elapsed.toFixed(1) + "s",
            });
        }
        markers.push(...lapMarkers);
    }
    return markers;
}

function eventMarkerPlugin(markers){
    return {
        id: "sessionEventMarkers",
        afterDatasetsDraw(chart){
            if (!markers.length) return;
            const xScale = chart.scales.x;
            const area = chart.chartArea;
            const ctx = chart.ctx;
            ctx.save();
            ctx.font = "10px ui-monospace, Menlo, Consolas, monospace";
            ctx.textBaseline = "top";
            ctx.setLineDash([4, 4]);
            for (let i = 0; i < markers.length; i++){
                const marker = markers[i];
                if (marker.x < xScale.min || marker.x > xScale.max) continue;
                const x = xScale.getPixelForValue(marker.x);
                ctx.strokeStyle = "rgba(234,179,8,0.7)";
                ctx.fillStyle = "#eab308";
                ctx.beginPath();
                ctx.moveTo(x, area.top);
                ctx.lineTo(x, area.bottom);
                ctx.stroke();
                ctx.fillText(marker.label, x + 3, area.top + 3 + (i % 4) * 11);
            }
            ctx.restore();
        },
    };
}

function dataAgeSamples(){
    const samples = state.session && state.session.data_age_samples;
    if (!samples || !Array.isArray(samples.at_epoch) || !Array.isArray(samples.data_age_s)){
        return [];
    }

    const count = Math.min(samples.at_epoch.length, samples.data_age_s.length);
    const points = [];
    for (let i = 0; i < count; i++){
        if (samples.at_epoch[i] == null || samples.data_age_s[i] == null) continue;
        const at = Number(samples.at_epoch[i]);
        const age = Number(samples.data_age_s[i]);
        if (Number.isFinite(at) && Number.isFinite(age)){
            points.push({ at, age });
        }
    }
    return points;
}

function renderDataAge(){
    const allLaps = getSessionLaps();
    const selectedLaps = allLaps.filter(lap =>
        state.selectedLaps.has(lap.lap_number)
    );
    const showFullSession = (
        allLaps.length === 0
        || selectedLaps.length === allLaps.length
    );
    const rawPoints = dataAgeSamples();
    const threshold = 1;
    const startEpoch = state.session && state.session.started_at
        ? Date.parse(state.session.started_at) / 1000
        : NaN;
    const origin = Number.isFinite(startEpoch)
        ? startEpoch
        : (rawPoints.length ? rawPoints[0].at : 0);
    const plottedSeries = showFullSession
        ? [{
            label: "Età del dato MQTT",
            color: "#22d3ee",
            points: rawPoints,
        }]
        : selectedLaps.map(({ lap_number, started_at_epoch, ended_at_epoch }, index) => {
            const lapStart = Number(started_at_epoch);
            const lapEnd = Number(ended_at_epoch);
            const points = Number.isFinite(lapStart) && Number.isFinite(lapEnd)
                ? rawPoints.filter(point => point.at >= lapStart && point.at <= lapEnd)
                : [];
            return {
                label: "Giro " + lap_number,
                color: PALETTE[index % PALETTE.length],
                points,
            };
        });
    const plottedPoints = plottedSeries.flatMap(series => series.points);
    const chartDatasets = plottedSeries.map(series => ({
        label: series.label,
        data: series.points.map(point => ({
            x: Math.max(0, point.at - origin),
            y: point.age,
        })),
        borderColor: series.color,
        backgroundColor: series.color + "22",
        borderWidth: 1.5,
        pointRadius: series.points.map(point => point.age > threshold ? 3 : 0),
        pointHoverRadius: 4,
        pointBackgroundColor: series.points.map(
            point => point.age > threshold ? "#ef4444" : series.color
        ),
        tension: 0,
    }));
    const chartPoints = chartDatasets.flatMap(dataset => dataset.data);
    const points = plottedPoints;
    const anomalies = points.filter(point => point.age > threshold);
    const maxAge = points.reduce(
        (maximum, point) => Math.max(maximum, point.age),
        0
    );

    $("dataAgeSummary").textContent = points.length
        ? (showFullSession
            ? "Intera sessione · "
            : selectedLaps.length === 1
                ? "Giro " + selectedLaps[0].lap_number + " · "
                : selectedLaps.length + " giri selezionati · ") +
          points.length.toLocaleString("it-IT") + " campioni · massimo " +
          maxAge.toFixed(3) + " s · " + anomalies.length.toLocaleString("it-IT") +
          " campioni oltre 1 s"
        : selectedLaps.length === 0 && allLaps.length
            ? "Seleziona almeno un giro per vedere i relativi campioni."
            : "Questa sessione non contiene campioni di età del dato nei giri selezionati.";
    $("dataAgeAnomalyCount").textContent = anomalies.length
        ? anomalies.length.toLocaleString("it-IT") + " superamenti"
        : "nessun superamento";
    $("dataAgeAnomaliesBody").innerHTML = anomalies.length
        ? anomalies.map(point =>
            '<tr>' +
                '<td class="mono">' + new Date(point.at * 1000).toLocaleString("it-IT", { hour12: false }) + '</td>' +
                '<td class="mono">' + Math.max(0, point.at - origin).toFixed(3) + ' s</td>' +
                '<td class="mono" style="color:var(--red);font-weight:700;">' + point.age.toFixed(3) + ' s</td>' +
            '</tr>'
        ).join("")
        : '<tr><td colspan="3" class="empty-state">' +
          (points.length ? "Nessun campione oltre 1 secondo." : "Nessun dato disponibile.") +
          '</td></tr>';

    const canvas = $("dataAgeChart");
    const chartData = [...chartDatasets];
    if (chartPoints.length){
        const lastX = chartPoints[chartPoints.length - 1].x;
        chartData.push({
            label: "Soglia 1 s",
            data: [{ x: 0, y: threshold }, { x: lastX, y: threshold }],
            borderColor: "#ef4444",
            borderDash: [6, 4],
            borderWidth: 1,
            pointRadius: 0,
        });
    }
    const eventMarkers = sessionEventMarkers(
        showFullSession ? allLaps : selectedLaps
    );
    const lastSampleX = chartPoints.length
        ? chartPoints[chartPoints.length - 1].x
        : 0;
    const lastMarkerX = eventMarkers.reduce(
        (maximum, marker) => Math.max(maximum, marker.x),
        0
    );

    const options = JSON.parse(JSON.stringify(chartCommonOptions));
    options.interaction = { mode: "nearest", axis: "x", intersect: false };
    options.plugins.tooltip.callbacks = {
        title: items => {
            if (!items.length) return "";
            return "Tempo sessione: " + items[0].parsed.x.toFixed(3) + " s";
        },
    };
    options.scales.x.title.text = "Tempo dalla partenza (s)";
    if (!showFullSession && selectedLaps.length){
        const lapStarts = selectedLaps.map(lap => Number(lap.started_at_epoch))
            .filter(Number.isFinite);
        const lapEnds = selectedLaps.map(lap => Number(lap.ended_at_epoch))
            .filter(Number.isFinite);
        if (lapStarts.length){
            options.scales.x.min = Math.max(0, Math.min(...lapStarts) - origin);
        } else {
            options.scales.x.min = 0;
        }
        if (lapEnds.length){
            options.scales.x.max = Math.max(...lapEnds) - origin;
        }
    } else {
        options.scales.x.min = 0;
    }
    const dataMaxX = Math.max(lastSampleX, lastMarkerX);
    if (dataMaxX > 0){
        options.scales.x.max = Number.isFinite(options.scales.x.max)
            ? Math.max(options.scales.x.max, dataMaxX)
            : dataMaxX;
    }
    options.scales.y.title = { display: true, text: "Età del dato (s)", color: "#8b96a3" };
    options.scales.y.beginAtZero = true;
    options.plugins.legend.display = true;

    if (state.charts.dataAge) state.charts.dataAge.destroy();
    state.charts.dataAge = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: { datasets: chartData },
        options,
        plugins: [eventMarkerPlugin(eventMarkers)],
    });
}

function renderChart(id, canvasId, selected, keyY, yLabel){
    const ctx = $(canvasId).getContext("2d");

    const datasets = selected.map(({ lap, idx }) => {
        const ds = buildDataset(lap, idx, keyY);
        if (keyY === "delta_live_s"){
            ds.borderDash = [6, 4];
            ds.spanGaps = true;
        }
        return ds;
    });
    const eventMarkers = id === "speed"
        ? selected.flatMap(({ lap }) => lapEventMarkers(lap))
        : [];

    const options = JSON.parse(JSON.stringify(chartCommonOptions));
    options.scales.y.title = { display: true, text: yLabel, color: "#8b96a3" };

    if (state.charts[id]) state.charts[id].destroy();

    state.charts[id] = new Chart(ctx, {
        type: "line",
        data: { datasets },
        options,
        plugins: [eventMarkerPlugin(eventMarkers)],
    });
}

/* ============================================================
   TABELLE
   ============================================================ */

function renderLapsTable(){
    const tbody = $("lapsTableBody");
    const laps = getSessionLaps();
    if (!state.session || !laps.length){
        tbody.innerHTML = '<tr><td colspan="8" class="empty-state">Nessuna sessione caricata.</td></tr>';
        return;
    }

    const rows = [];
    laps.forEach((lap, idx) => {
        const selected = state.selectedLaps.has(lap.lap_number) ? " selected" : "";
        const sectors = lap.sectors_s || [];
        const s1 = sectors[0] != null ? sectors[0].toFixed(3) : "—";
        const s2 = sectors[1] != null ? sectors[1].toFixed(3) : "—";
        const s3 = sectors[2] != null ? sectors[2].toFixed(3) : "—";
        const swatch = '<span class="lap-swatch" style="display:inline-block;background:' +
            PALETTE[idx % PALETTE.length] +
            ';width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:middle;"></span>';
        const nPoints = (lap.track_points && lap.track_points.latitude)
            ? lap.track_points.latitude.length : 0;
        const status = lap.is_warmup
            ? '<span class="badge">Percorso iniziale</span>'
            : lap.is_in_progress
                ? '<span class="badge">In corso</span>'
                : lap.valid
                    ? '<span class="badge valid">Valido</span>'
                    : '<span class="badge invalid">' + escapeHtml(lap.aborted_reason || "invalido") + '</span>';
        const label = lap.is_warmup ? "Pista" : "#" + lap.lap_number;

        rows.push(
            '<tr class="lap-row' + selected + '" data-lap="' + lap.lap_number + '">' +
                '<td class="mono">' + swatch + label + '</td>' +
                '<td class="mono">' + fmtTime(lap.lap_time_s) + '</td>' +
                '<td class="mono">' + s1 + '</td>' +
                '<td class="mono">' + s2 + '</td>' +
                '<td class="mono">' + s3 + '</td>' +
                '<td class="mono">' + (lap.lap_distance_m != null ? Math.round(lap.lap_distance_m) + ' m' : '—') + '</td>' +
                '<td class="mono">' + nPoints + '</td>' +
                '<td>' + status + '</td>' +
            '</tr>'
        );
    });
    tbody.innerHTML = rows.join("");

    tbody.querySelectorAll("tr.lap-row").forEach(tr => {
        tr.addEventListener("click", () => {
            toggleLap(parseInt(tr.getAttribute("data-lap"), 10));
        });
    });
}

function renderEventsTable(){
    const tbody = $("eventsTableBody");
    const session = state.session;
    if (!session){
        tbody.innerHTML = '<tr><td colspan="4" class="empty-state">Nessun evento.</td></tr>';
        return;
    }

    const rows = [];
    getSessionLaps().forEach(lap => {
        if (!state.selectedLaps.has(lap.lap_number)) return;
        const events = Array.isArray(lap.events) ? lap.events : [];
        for (const ev of events){
            const at = ev.at
                ? new Date(ev.at).toLocaleTimeString("it-IT", { hour12: false })
                : "—";
            rows.push(
                '<tr>' +
                    '<td class="mono">' + lap.lap_number + '</td>' +
                    '<td>' + escapeHtml(ev.event || "—") + '</td>' +
                    '<td class="mono">' + escapeHtml(at) + '</td>' +
                    '<td>' + formatEventDetails(ev) + '</td>' +
                '</tr>'
            );
        }
    });

    tbody.innerHTML = rows.length
        ? rows.join("")
        : '<tr><td colspan="4" class="empty-state">Nessun evento per i giri selezionati.</td></tr>';
}

function formatEventDetails(ev){
    const parts = [];
    if (ev.sector_number != null) parts.push("Settore " + ev.sector_number);
    if (ev.sector_time_s != null) parts.push("tempo " + Number(ev.sector_time_s).toFixed(3) + " s");
    if (ev.status) parts.push('<span class="badge ' + ev.status + '">' + escapeHtml(ev.status) + '</span>');
    if (ev.lap_time_s != null) parts.push("tempo giro " + Number(ev.lap_time_s).toFixed(3) + " s");
    if (ev.reason) parts.push("motivo: " + escapeHtml(ev.reason));
    if (ev.from_state && ev.to_state) parts.push(escapeHtml(ev.from_state) + " → " + escapeHtml(ev.to_state));
    if (ev.valid === true) parts.push('<span class="badge valid">valido</span>');
    else if (ev.valid === false) parts.push('<span class="badge invalid">non valido</span>');
    return parts.join(" · ") || "—";
}

function renderStats(){
    const s = state.session;
    if (!s){
        ["statDriver","statTrack","statStatus","statLaps","statBest",
         "statIdeal","statDuration","statSamples"].forEach(id => $(id).textContent = "—");
        $("sessionMeta").textContent = "—";
        return;
    }
    $("statDriver").textContent = s.driver ? s.driver.name : "—";
    $("statTrack").textContent = s.track ? s.track.name : "—";
    $("statStatus").textContent = s.status || "—";

    const validLaps = (s.laps || []).filter(l => l.valid).length;
    const totalLaps = (s.laps || []).length;
    $("statLaps").textContent = validLaps + " / " + totalLaps;
    $("statBest").textContent = fmtTime(s.best_lap_time_s);
    $("statIdeal").textContent = fmtTime(s.ideal_lap_time_s);
    $("statDuration").textContent = fmtDuration(s.session_elapsed_s);

    const samples = s.summary ? s.summary.raw_samples_received : null;
    $("statSamples").textContent = samples != null ? samples.toLocaleString("it-IT") : "—";

    $("sessionMeta").textContent = s.started_at
        ? new Date(s.started_at).toLocaleString("it-IT") : "—";
}

function getSelectedLaps(){
    const laps = getSessionLaps();
    const out = [];
    laps.forEach((lap, idx) => {
        if (state.selectedLaps.has(lap.lap_number)) out.push({ lap, idx });
    });
    return out;
}

function getSessionLaps(){
    const session = state.session;
    if (!session) return [];

    const toNumber = value => {
        const number = Number(value);
        return value !== null && value !== undefined && Number.isFinite(number)
            ? number
            : null;
    };
    const laps = Array.isArray(session.laps) ? [...session.laps] : [];
    const warmup = session.warmup_and_pitlane_raw;

    if (warmup){
        const fields = [
            "received_at_epoch", "latitude", "longitude", "speed_kmph",
            "satellites_used", "hdop", "temperature_c", "as5600_rpm", "ir_rpm",
        ];
        const points = {};
        if (Array.isArray(warmup)){
            for (const field of fields){
                points[field] = warmup.map(point => toNumber(point && point[field]));
            }
        } else {
            for (const field of fields){
                points[field] = Array.isArray(warmup[field])
                    ? warmup[field].map(toNumber)
                    : [];
            }
        }

        const count = Math.max(0, ...fields.map(field => points[field].length));
        if (count){
            for (const field of fields){
                while (points[field].length < count) points[field].push(null);
            }
            points.lap_elapsed_s = [];
            points.lap_distance_m = [];
            points.delta_live_s = Array(count).fill(null);

            const firstEpoch = points.received_at_epoch.find(Number.isFinite);
            let distance = 0;
            let previousCoordinate = null;
            for (let i = 0; i < count; i++){
                const latitude = points.latitude[i];
                const longitude = points.longitude[i];
                if (Number.isFinite(latitude) && Number.isFinite(longitude)
                    && !(latitude === 0 && longitude === 0)){
                    if (previousCoordinate){
                        const toRadians = degrees => degrees * Math.PI / 180;
                        const dLat = toRadians(latitude - previousCoordinate.latitude);
                        const dLon = toRadians(longitude - previousCoordinate.longitude);
                        const a = Math.min(1, Math.max(0,
                            Math.sin(dLat / 2) ** 2
                            + Math.cos(toRadians(previousCoordinate.latitude))
                            * Math.cos(toRadians(latitude))
                            * Math.sin(dLon / 2) ** 2
                        ));
                        distance += 6371000 * 2 * Math.atan2(
                            Math.sqrt(a), Math.sqrt(1 - a)
                        );
                    }
                    previousCoordinate = { latitude, longitude };
                }
                const epoch = points.received_at_epoch[i];
                points.lap_elapsed_s.push(
                    Number.isFinite(epoch) && Number.isFinite(firstEpoch)
                        ? Math.max(0, epoch - firstEpoch)
                        : null
                );
                points.lap_distance_m.push(distance);
            }

            const epochs = points.received_at_epoch.filter(Number.isFinite);
            laps.unshift({
                lap_number: -1,
                lap_time_s: epochs.length > 1 ? epochs[epochs.length - 1] - epochs[0] : null,
                lap_distance_m: distance,
                valid: false,
                is_warmup: true,
                started_at_epoch: epochs.length ? epochs[0] : null,
                ended_at_epoch: epochs.length ? epochs[epochs.length - 1] : null,
                sectors_s: [],
                events: [],
                track_points: points,
            });
        }
    }

    const current = session.current_lap_in_progress;
    if (current && current.track_points){
        const trackPoints = current.track_points;
        const epochs = Array.isArray(trackPoints.received_at_epoch)
            ? trackPoints.received_at_epoch.map(toNumber).filter(Number.isFinite)
            : [];
        const elapsed = Array.isArray(trackPoints.lap_elapsed_s)
            ? trackPoints.lap_elapsed_s.map(toNumber).filter(Number.isFinite)
            : [];
        const startedAt = toNumber(current.started_at_epoch);
        const endedAt = epochs.length
            ? epochs[epochs.length - 1]
            : (startedAt !== null && elapsed.length ? startedAt + elapsed[elapsed.length - 1] : null);
        const lapNumber = toNumber(current.lap_number);
        if (lapNumber !== null && !laps.some(lap => lap.lap_number === lapNumber)){
            const distances = Array.isArray(trackPoints.lap_distance_m)
                ? trackPoints.lap_distance_m.map(toNumber).filter(Number.isFinite)
                : [];
            laps.push({
                ...current,
                sectors_s: current.current_lap_sectors_s || [],
                lap_time_s: startedAt !== null && endedAt !== null
                    ? Math.max(0, endedAt - startedAt)
                    : (elapsed.length ? elapsed[elapsed.length - 1] : null),
                ended_at_epoch: endedAt,
                lap_distance_m: toNumber(current.current_lap_distance_m)
                    ?? (distances.length ? distances[distances.length - 1] : null),
                valid: false,
                is_in_progress: true,
            });
        }
    }

    return laps;
}

function toggleLap(lapNumber){
    if (state.selectedLaps.has(lapNumber)) state.selectedLaps.delete(lapNumber);
    else state.selectedLaps.add(lapNumber);
    renderLapList();
    renderLapsTable();
    renderEventsTable();
    renderMapLayers();
    renderCharts();
}

function selectAllLaps(){
    const laps = getSessionLaps();
    state.selectedLaps.clear();
    for (const lap of laps) state.selectedLaps.add(lap.lap_number);
    renderLapList();
    renderLapsTable();
    renderEventsTable();
    renderMapLayers();
    renderCharts();
}

function clearAllLaps(){
    state.selectedLaps.clear();
    renderLapList();
    renderLapsTable();
    renderEventsTable();
    renderMapLayers();
    renderCharts();
}

/* ============================================================
   SESSIONI
   ============================================================ */

async function loadSessionList(){
    const folder = $("folderInput").value.trim();
    if (!folder) return;
    showError("");
    try {
        const r = await fetch(
            "/api/list_sessions?folder=" + encodeURIComponent(folder),
            { cache: "no-store" }
        );
        const data = await r.json();

        if (!data.ok){
            showError(escapeHtml(data.error || "Errore caricamento cartella."));
            $("sessionList").innerHTML =
                '<div class="empty-state" style="padding:16px 4px;">Errore.</div>';
            $("sessionCount").textContent = "0";
            return;
        }

        state.folder = data.folder;
        state.sessions = data.files || [];
        $("folderInput").value = data.folder;
        $("folderResolved").textContent = "✓ " + data.folder + " (" + state.sessions.length + " file)";
        renderSessionList();

        if (state.sessions.length === 0){
            showError(
                "La cartella <code>" + escapeHtml(data.folder) + "</code> " +
                "non contiene file <code>.json</code>."
            );
        } else {
            toast(state.sessions.length + " sessioni trovate");
        }
    } catch (e){
        showError("Errore di rete: " + escapeHtml(e.message));
    }
}

function renderSessionList(){
    const list = $("sessionList");
    if (!state.sessions.length){
        list.innerHTML =
            '<div class="empty-state" style="padding:16px 4px;">' +
            'Nessun file .json in questa cartella.</div>';
        $("sessionCount").textContent = "0";
        return;
    }
    $("sessionCount").textContent = state.sessions.length;

    list.innerHTML = state.sessions.map(f => {
        const active = state.session && state.session._path === f.path ? " active" : "";
        return (
            '<div class="session-item' + active + '" data-path="' + escapeHtml(f.path) + '">' +
                '<div class="name">' + escapeHtml(f.name) + '</div>' +
                '<div class="meta">' +
                    '<span>' + fmtDate(f.mtime) + '</span>' +
                    '<span>' + fmtBytes(f.size_bytes) + '</span>' +
                '</div>' +
            '</div>'
        );
    }).join("");

    list.querySelectorAll(".session-item").forEach(el => {
        el.addEventListener("click", () => loadSession(el.getAttribute("data-path")));
    });
}

async function loadSession(path){
    showError("");
    try {
        const r = await fetch(
            "/api/load_session?path=" + encodeURIComponent(path),
            { cache: "no-store" }
        );
        const data = await r.json();
        if (!data.ok){
            showError(escapeHtml(data.error || "Errore caricamento sessione."));
            return;
        }

        const session = data.session;
        session._path = path;
        state.session = session;
        state.selectedLaps.clear();

        getSessionLaps().forEach(lap => state.selectedLaps.add(lap.lap_number));

        renderSessionList();
        renderStats();
        renderLapList();
        renderLapsTable();
        renderEventsTable();
        renderMapLayers();
        renderCharts();

        toast("Sessione caricata: " + (session.driver ? session.driver.name : "—"));
    } catch (e){
        showError("Errore di rete: " + escapeHtml(e.message));
    }
}

function renderLapList(){
    const list = $("lapList");
    const s = state.session;
    const laps = getSessionLaps();

    if (!s || !laps.length){
        list.innerHTML =
            '<div class="empty-state" style="padding:16px 4px;">Seleziona una sessione.</div>';
        $("selectAllBtn").disabled = true;
        $("clearAllBtn").disabled = true;
        return;
    }

    $("selectAllBtn").disabled = false;
    $("clearAllBtn").disabled = false;

    list.innerHTML = laps.map((lap, idx) => {
        const checked = state.selectedLaps.has(lap.lap_number);
        const classes = [
            "lap-item",
            checked ? "checked" : "",
            !lap.valid && !lap.is_in_progress && !lap.is_warmup ? "invalid" : "",
        ]
            .filter(Boolean).join(" ");
        const badge = lap.is_warmup ? "raw" : lap.is_in_progress ? "in corso" : lap.valid ? "ok" : "no";
        const label = lap.is_warmup ? "Pista" : "#" + lap.lap_number;
        return (
            '<div class="' + classes + '" data-lap="' + lap.lap_number + '">' +
                '<span class="lap-swatch" style="background:' + PALETTE[idx % PALETTE.length] + ';"></span>' +
                '<span class="lap-num">' + label + '</span>' +
                '<span class="lap-time">' + fmtTime(lap.lap_time_s) + '</span>' +
                '<span class="lap-badge ' + (lap.valid ? "valid" : lap.is_in_progress || lap.is_warmup ? "" : "invalid") + '">' +
                    badge + '</span>' +
            '</div>'
        );
    }).join("");

    list.querySelectorAll(".lap-item").forEach(el => {
        el.addEventListener("click", () => {
            toggleLap(parseInt(el.getAttribute("data-lap"), 10));
        });
    });
}

async function init(){
    $("refreshBtn").addEventListener("click", loadSessionList);
    $("folderInput").addEventListener("keydown", (e) => {
        if (e.key === "Enter") loadSessionList();
    });
    $("selectAllBtn").addEventListener("click", selectAllLaps);
    $("clearAllBtn").addEventListener("click", clearAllLaps);
    $("mapFsBtn").addEventListener("click", toggleMapFullscreen);

    // Pulsanti fullscreen per i grafici
    document.querySelectorAll(".chart-fs-btn").forEach(btn => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            const panel = btn.closest(".chart-panel");
            if (panel) toggleChartFullscreen(panel);
        });
    });

    try {
        await loadConfig();
    } catch (e){
        showError("Impossibile contattare il server: " + escapeHtml(e.message));
        return;
    }

    renderStats();
    loadSessionList();
}

document.addEventListener("DOMContentLoaded", init);
</script>
</body>
</html>
"""


# =============================================================
# Backend
# =============================================================

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/config")
def api_config():
    default_exists = False
    try:
        default_exists = Path(DEFAULT_FOLDER).expanduser().is_dir()
    except Exception:
        default_exists = False

    return jsonify({
        "has_mapbox_token": bool(MAPBOX_TOKEN),
        "mapbox_token": MAPBOX_TOKEN,
        "default_folder": DEFAULT_FOLDER,
        "default_folder_exists": default_exists,
    })


@app.route("/api/list_sessions")
def api_list_sessions():
    folder = request.args.get("folder", "").strip() or DEFAULT_FOLDER

    try:
        p = Path(folder).expanduser().resolve()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Percorso non valido: {e}"})

    if not p.exists():
        return jsonify({"ok": False, "error": f"Il percorso non esiste: {p}"})

    if not p.is_dir():
        return jsonify({"ok": False, "error": f"Non è una cartella: {p}"})

    files = []
    try:
        for f in p.glob("*.json"):
            try:
                st = f.stat()
                files.append({
                    "name": f.name,
                    "path": str(f),
                    "size_bytes": st.st_size,
                    "mtime": st.st_mtime,
                })
            except OSError:
                continue
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    files.sort(key=lambda x: x["mtime"], reverse=True)

    return jsonify({"ok": True, "folder": str(p), "files": files})


@app.route("/api/load_session")
def api_load_session():
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"ok": False, "error": "Parametro 'path' mancante."}), 400

    try:
        p = Path(path).expanduser().resolve()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Percorso non valido: {e}"}), 400

    if not p.is_file():
        return jsonify({"ok": False, "error": f"File non trovato: {p}"}), 404

    if p.suffix.lower() != ".json":
        return jsonify({"ok": False, "error": "Il file non è un .json."}), 400

    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "error": f"JSON non valido: {e}"}), 400
    except OSError as e:
        return jsonify({"ok": False, "error": f"Errore lettura: {e}"}), 500

    return jsonify({"ok": True, "session": data})


# =============================================================
# Main
# =============================================================

if __name__ == "__main__":
    print()
    print("=== Session Analyzer ===")
    print(f"SESSIONS_DIR: {DEFAULT_FOLDER}")

    try:
        _exists = Path(DEFAULT_FOLDER).expanduser().is_dir()
    except Exception:
        _exists = False

    print(f"Cartella: {'OK trovata' if _exists else 'ERRORE non trovata'}")

    if MAPBOX_TOKEN:
        print(f"MAPBOX_TOKEN: OK ({MAPBOX_TOKEN[:8]}...{MAPBOX_TOKEN[-4:]})")
    else:
        print("MAPBOX_TOKEN: MANCANTE - la mappa non funzionera")

    print()
    print(f"Apri il browser su: http://{HOST}:{PORT}")
    print()

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )