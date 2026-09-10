import json
import os

import yaml
from dotenv import load_dotenv
from flask import Flask, request, render_template_string, jsonify

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKS_DIR = os.path.join(BASE_DIR, "config", "tracks")
ENV_PATH = os.path.join(BASE_DIR, ".env")

# Carica le variabili dal file .env nella root del progetto.
# Se il file non esiste, load_dotenv() non fa nulla e non solleva errori.
load_dotenv(ENV_PATH)

MAPBOX_TOKEN = os.environ.get("MAPBOX_TOKEN")

if not MAPBOX_TOKEN:
    raise RuntimeError(
        "MAPBOX_TOKEN non impostato. "
        f"Crealo nel file {ENV_PATH} (es. MAPBOX_TOKEN=pk.xxxx) "
        "oppure esportalo come variabile d'ambiente."
    )

app = Flask(__name__)

HTML = r"""
<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <title>Kart Monitor - Crea Pista</title>
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">

  <script src="https://api.mapbox.com/mapbox-gl-js/v3.0.0/mapbox-gl.js"></script>
  <link href="https://api.mapbox.com/mapbox-gl-js/v3.0.0/mapbox-gl.css" rel="stylesheet">
  <script src="https://api.mapbox.com/mapbox-gl-js/plugins/mapbox-gl-geocoder/v5.0.0/mapbox-gl-geocoder.min.js"></script>
  <link rel="stylesheet" href="https://api.mapbox.com/mapbox-gl-js/plugins/mapbox-gl-geocoder/v5.0.0/mapbox-gl-geocoder.css">
  <script src="https://cdn.jsdelivr.net/npm/@turf/turf@6/turf.min.js"></script>
  <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>

  <style>
    :root {
      --black: #050505; --panel: #111214; --panel-soft: #1a1b1e;
      --border: #2b2d32; --text: #f7f7f8; --muted: #92959d;
      --green: #31dc82; --red: #ff3b30; --purple: #f24ddb; --blue: #319cdc; --yellow: #ffd700;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 0; background: var(--black); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .topbar {
      display: flex; align-items: center; justify-content: space-between;
      padding: 12px 20px; border-bottom: 1px solid var(--border);
      background: rgba(15, 16, 18, 0.96); position: sticky; top: 0; z-index: 100;
    }
    .brand h1 { margin: 0; font-size: 14px; text-transform: uppercase; letter-spacing: 0.1em; color: #fff; }
    .container { padding: 20px; max-width: 900px; margin: 0 auto; padding-bottom: 100px; }

    .panel {
      border: 1px solid var(--border); border-radius: 10px; background: var(--panel);
      padding: 20px; margin-bottom: 20px;
    }
    .panel-title {
      color: var(--muted); font-size: 11px; font-weight: 900; letter-spacing: 0.1em; text-transform: uppercase;
      padding-bottom: 10px; border-bottom: 1px solid var(--border); margin-bottom: 15px;
      display: flex; justify-content: space-between; align-items: center;
    }
    .form-row { display: flex; gap: 15px; margin-bottom: 15px; flex-wrap: wrap; }
    .form-group { flex: 1; min-width: 200px; }
    .form-group label { display: block; font-size: 10px; color: var(--muted); font-weight: 800; text-transform: uppercase; margin-bottom: 6px; }
    .form-group input, .form-group select {
      width: 100%; padding: 8px 10px; background: #1e1f23; border: 1px solid var(--border);
      border-radius: 6px; color: #fff; font-size: 13px;
    }
    .form-group input:focus { outline: none; border-color: var(--green); }

    .help-text {
      font-size: 10px;
      color: var(--muted);
      margin-top: 4px;
      line-height: 1.4;
    }

    .btn {
      padding: 8px 12px; background: #202124; border: 1px solid var(--border); color: #e9e9eb;
      border-radius: 6px; font-size: 10px; font-weight: 900; cursor: pointer; text-transform: uppercase;
    }
    .btn:hover { background: #2b2d32; }
    .btn-green { background: rgba(22, 131, 72, 0.45); border-color: rgba(49, 220, 130, 0.52); color: #fff; }
    .btn-blue { background: rgba(22, 98, 131, 0.45); border-color: rgba(49, 172, 220, 0.52); color: #fff; }
    .btn-red { background: rgba(147, 24, 35, 0.42); border-color: rgba(255, 94, 105, 0.52); color: #fff; }

    .summary-card { background: var(--panel-soft); border: 1px dashed var(--border); padding: 15px; border-radius: 8px; margin-bottom: 10px; }
    .summary-card p { margin: 5px 0; font-size: 12px; color: #ccc; }

    #map-modal {
      display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0,0,0,0.95); z-index: 1000;
    }
    .map-layout { display: flex; width: 100%; height: 100%; flexDirection: row-reverse; }
    .map-sidebar {
      width: 320px; background: #0f1012; border-left: 1px solid var(--border);
      display: flex; flex-direction: column;
    }
    .map-sidebar-header {
      padding: 15px; border-bottom: 1px solid var(--border);
    }
    .map-sidebar-header h2 { margin: 0; font-size: 12px; color: #fff; text-transform: uppercase; }
    .map-sidebar-content { flex: 1; overflow-y: auto; padding: 15px; }
    .map-sidebar-footer { padding: 15px; border-top: 1px solid var(--border); }

    .map-main { flex: 1; position: relative; }
    #map { width: 100%; height: 100%; }

    .point-item {
      padding: 10px; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 10px;
      cursor: grab; background: #1a1b1e; transition: all 0.2s; position: relative;
    }
    .point-item:active { cursor: grabbing; }
    .point-item.active { border-color: var(--green); background: rgba(49, 220, 130, 0.1); }
    .point-item.filled .dot { background: var(--green); }
    .point-item.drag-over { border-top: 2px solid var(--blue); }

    .point-title { font-size: 11px; font-weight: bold; display: flex; align-items: center; gap: 8px; color: #fff; }
    .point-coord { font-size: 10px; color: var(--muted); margin-top: 4px; padding-left: 16px; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #444; flex-shrink: 0; }

    .drag-handle { cursor: grab; font-size: 14px; margin-right: 5px; color: #555; }

    .map-top-bar {
      position: absolute; top: 10px; left: 10px; right: 10px;
      display: flex; justify-content: space-between; pointer-events: none; z-index: 10;
    }
    .map-instructions { background: rgba(0,0,0,0.85); color: #fff; padding: 10px 15px; border-radius: 6px; font-size: 12px; pointer-events: auto; }

    .mapboxgl-ctrl-geocoder { min-width: 250px; pointer-events: auto; }
  </style>
</head>
<body>
{% raw %}
  <div id="app">
    <header class="topbar">
      <div class="brand"><h1>Kart Performance Monitor - yaml editor</h1></div>
      <div style="display:flex; gap:10px; align-items:center;">
        <select v-model="selectedTrack" style="background:#1e1f23; border:1px solid var(--border); color:#fff; padding:8px 10px; border-radius:6px;">
          <option value="">-- Seleziona pista --</option>
          <option v-for="t in availableTracks" :value="t">{{ t }}</option>
        </select>
        <button class="btn" @click="loadSelectedTrack()">Carica</button>
        <button class="btn" @click="nuovaPista()">Nuova Pista</button>
        <button class="btn btn-blue" @click="apriPreviewTotale(false)">Anteprima Pista Mappa</button>
        <button class="btn btn-green" @click="iniziaSalvataggio()">Salva {{ track.id || 'pista' }}.yaml</button>
      </div>
    </header>

    <div class="container">

      <!-- GENERAL INFO -->
      <div class="panel">
        <div class="panel-title">Informazioni Generali</div>
        <div class="form-row">
          <div class="form-group">
            <label>ID Pista</label>
            <input type="text" v-model="track.id">
            <div class="help-text">Identificativo univoco usato nel nome del file YAML e nei log.</div>
          </div>
          <div class="form-group">
            <label>Nome Pista</label>
            <input type="text" v-model="track.name">
            <div class="help-text">Nome descrittivo mostrato nell'interfaccia utente.</div>
          </div>
        </div>
      </div>

      <!-- TIMING -->
      <div class="panel">
        <div class="panel-title">Parametri Timing & GPS</div>
        <div class="form-row">
          <div class="form-group">
            <label>Min Lap Time (s)</label>
            <input type="number" step="0.1" v-model.number="timing.minimum_lap_time_s">
            <div class="help-text">Tempo minimo tra giri consecutivi per evitare doppie letture dello stesso passaggio.</div>
          </div>
          <div class="form-group">
            <label>Crossing Cooldown (s)</label>
            <input type="number" step="0.1" v-model.number="timing.crossing_cooldown_s">
            <div class="help-text">Tempo di cooldown dopo un attraversamento per ignorare rilevamenti ravvicinati.</div>
          </div>
          <div class="form-group">
            <label>Min Speed (km/h)</label>
            <input type="number" step="0.1" v-model.number="timing.crossing_minimum_speed_kmph">
            <div class="help-text">Velocità minima richiesta per considerare valido l'attraversamento (evita falsi positivi da fermo).</div>
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>Min Satellites</label>
            <input type="number" v-model.number="timing.min_satellites">
            <div class="help-text">Numero minimo di satelliti per considerare il fix GPS affidabile.</div>
          </div>
          <div class="form-group">
            <label>Max HDOP</label>
            <input type="number" step="0.1" v-model.number="timing.max_hdop">
            <div class="help-text">HDOP massimo accettabile (diluizione della precisione orizzontale). Valori più bassi = maggiore precisione.</div>
          </div>
          <div class="form-group">
            <label>Min Fix Type</label>
            <input type="number" v-model.number="timing.min_fix_type">
            <div class="help-text">Tipo di fix GPS minimo richiesto (es. 3 = fix 3D, 4 = DGPS, 5 = RTK).</div>
          </div>
        </div>
      </div>

      <!-- PIT BOX -->
      <div class="panel">
        <div class="panel-title">
          Pit Box
          <button class="btn btn-blue" @click="edita('pitbox')">Modifica su Mappa</button>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>Forma</label>
            <select v-model="pit_box.shape">
              <option value="circle">Cerchio (Centro + Raggio)</option>
              <option value="polygon">Poligono (Punti multipli)</option>
            </select>
            <div class="help-text">Scegli come definire l'area del pit box: cerchio semplice o poligono personalizzato.</div>
          </div>
          <div class="form-group" v-if="pit_box.shape === 'circle'">
            <label>Radius (m)</label>
            <input type="number" step="0.1" v-model.number="pit_box.radius_m">
            <div class="help-text">Raggio del cerchio in metri (solo per forma cerchio).</div>
          </div>
          <div class="form-group">
            <label>Exit Hysteresis (m)</label>
            <input type="number" step="0.1" v-model.number="pit_box.exit_hysteresis_m">
            <div class="help-text">Distanza di isteresi in uscita per evitare che un kart entri/esca ripetutamente dal pit box.</div>
          </div>
          <div class="form-group">
            <label>Min Samples Confirm</label>
            <input type="number" v-model.number="pit_box.minimum_samples_to_confirm">
            <div class="help-text">Numero minimo di campioni GPS consecutivi dentro il pit box per confermare l'ingresso.</div>
          </div>
        </div>
        <div class="summary-card">
          <div v-if="pit_box.shape==='circle'">
            <p><strong>Centro:</strong> {{ formatC(pit_box.center) }}</p>
          </div>
          <div v-else>
            <p><strong>Poligono:</strong> {{ pit_box.polygon.length }} punti</p>
            <ul style="margin:5px 0 0 15px; padding:0; list-style:none;">
              <li v-for="(pt, idx) in pit_box.polygon" :key="idx" style="font-size:11px; color:#aaa;">
                P{{idx+1}}: {{ pt.lat.toFixed(6) }}, {{ pt.lon.toFixed(6) }}
              </li>
            </ul>
          </div>
        </div>
      </div>

      <!-- PIT LANE -->
      <div class="panel">
        <div class="panel-title">
          Pit Lane (Percorso)
          <button class="btn btn-blue" @click="edita('pitlane')">Modifica su Mappa</button>
        </div>
        <div class="summary-card">
          <p><strong>Punti tracciati:</strong> {{ pit_lane.path.length }}</p>
          <ul style="margin:5px 0 0 15px; padding:0; list-style:none;">
            <li v-for="(pt, idx) in pit_lane.path" :key="idx" style="font-size:11px; color:#aaa;">
              P{{idx+1}}: {{ pt.lat.toFixed(6) }}, {{ pt.lon.toFixed(6) }}
            </li>
          </ul>
          <div class="help-text" style="margin-top:10px;">Sequenza di punti che definisce il percorso della pit lane. Se si forniscono solo due punti, verranno usati come start/end; con più punti viene tracciata la polilinea completa.</div>
        </div>
      </div>

      <!-- START/FINISH -->
      <div class="panel">
        <div class="panel-title">
          Linea Traguardo (Start/Finish)
          <button class="btn btn-blue" @click="edita('sf')">Modifica su Mappa</button>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>ID</label>
            <input type="text" v-model="start_finish.id">
            <div class="help-text">Identificativo breve (es. "SF").</div>
          </div>
          <div class="form-group">
            <label>Nome</label>
            <input type="text" v-model="start_finish.name">
            <div class="help-text">Nome descrittivo della linea.</div>
          </div>
          <div class="form-group">
            <label>Direction</label>
            <select v-model="start_finish.direction">
              <option value="forward">forward</option><option value="backward">backward</option>
            </select>
            <div class="help-text">Direzione di attraversamento valida: 'forward' (da prima a dopo) o 'backward'.</div>
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>Extension (m)</label>
            <input type="number" step="0.1" v-model.number="start_finish.line_extension_each_side_m">
            <div class="help-text">Estensione della linea oltre i punti A e B per intercettare il kart anche se taglia largo.</div>
          </div>
          <div class="form-group">
            <label>Tolerance (m)</label>
            <input type="number" step="0.1" v-model.number="start_finish.crossing_tolerance_m">
            <div class="help-text">Tolleranza laterale per considerare l'attraversamento vicino alla linea.</div>
          </div>
        </div>
        <div class="summary-card">
          <p><strong>Punto A:</strong> {{ formatC(start_finish.a) }}</p>
          <p><strong>Punto B:</strong> {{ formatC(start_finish.b) }}</p>
          <p><strong>Ref Prima:</strong> {{ formatC(start_finish.direction_reference.before) }}</p>
          <p><strong>Ref Dopo:</strong> {{ formatC(start_finish.direction_reference.after) }}</p>
        </div>
      </div>

      <!-- SECTORS -->
      <div class="panel">
        <div class="panel-title">
          Settori (Traguardi intermedi)
          <button class="btn" @click="aggiungiSettore()">+ Aggiungi Settore</button>
        </div>
        <div class="panel" style="background:#151619; border:1px solid #333;" v-for="(sec, idx) in sectors" :key="sec.id">
          <div class="panel-title" style="border:none; margin-bottom:0; padding-bottom:0;">
            {{ sec.name }}
            <div>
              <button class="btn btn-blue" style="margin-right:8px;" @click="edita('sector', idx)">Imposta su Mappa</button>
              <button class="btn btn-red" @click="rimuoviSettore(idx)">Rimuovi</button>
            </div>
          </div>
          <div class="form-row" style="margin-top:15px;">
            <div class="form-group">
              <label>ID</label>
              <input type="text" v-model="sec.id">
              <div class="help-text">Identificativo breve (es. "S1").</div>
            </div>
            <div class="form-group">
              <label>Nome</label>
              <input type="text" v-model="sec.name">
              <div class="help-text">Nome descrittivo del settore.</div>
            </div>
            <div class="form-group">
              <label>Direction</label>
              <select v-model="sec.direction">
                <option value="forward">forward</option>
                <option value="backward">backward</option>
              </select>
              <div class="help-text">Direzione di attraversamento valida: 'forward' o 'backward'.</div>
            </div>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label>Extension (m)</label>
              <input type="number" step="0.1" v-model.number="sec.line_extension_each_side_m">
              <div class="help-text">Estensione della linea oltre i punti A e B.</div>
            </div>
            <div class="form-group">
              <label>Tolerance (m)</label>
              <input type="number" step="0.1" v-model.number="sec.crossing_tolerance_m">
              <div class="help-text">Tolleranza laterale per considerare l'attraversamento vicino alla linea.</div>
            </div>
          </div>
          <div class="summary-card">
            <p><strong>Punto A:</strong> {{ formatC(sec.a) }} | <strong>Punto B:</strong> {{ formatC(sec.b) }}</p>
          </div>
        </div>
      </div>
    </div>

    <!-- MODALE MAPPA (EDIT & PREVIEW) -->
    <div id="map-modal" :style="{ display: mapModalOpen ? 'block' : 'none' }">
      <div class="map-layout">

        <div class="map-main">
          <div class="map-top-bar">
            <div class="map-instructions" style="max-width:400px;">
              <span v-if="mapPreviewAndSaveMode">
                <strong>FASE FINALE:</strong> Controlla bene l'anteprima 3D qui sotto per accertarti che tutte le zone di sorveglianza e confini della pista siano tracciati correttamente.<br><br>Se confermi il tracciato, esso verrà salvato definitivamente nel server.
              </span>
              <span v-else-if="mapPreviewMode">
                Modalità Anteprima. Ispeziona i punti salvati.
              </span>
              <span v-else>
                <strong>Modalità Edit:</strong> {{ mapTitle }}<br>
                Clicca in mappa per assegnare il punto attualmente attivo. Trascina i punti per affinare.
              </span>
            </div>
            <div id="geocoder-box"></div>
          </div>
          <div id="map"></div>
        </div>

        <!-- Colonna dei Punti (Sidebar a Destra per Drag & Drop comodo) -->
        <div class="map-sidebar">
          <div class="map-sidebar-header">
            <h2>{{ mapTitle }}</h2>
          </div>
          <div class="map-sidebar-content" v-if="!mapPreviewMode" 
               @dragover.prevent 
               @drop.prevent="dropSuColonna($event)">

            <p v-if="mapSetup.allowAdd" style="font-size:10px; color:#fff; margin-top:0;">
              Suggerimento: Puoi riordinare i punti trascinandoli tra loro. Utile per formare i poligoni o polilinee correttamente.
            </p>

            <div v-for="(pt, idx) in mapPoints" :key="pt._id" 
                 class="point-item" 
                 :class="{ active: mapActivePointIndex === idx, filled: pt.lat !== null, 'drag-over': dragTargetIndex === idx }"

                 :draggable="mapSetup.allowAdd"
                 @dragstart="dragStart(idx, $event)"
                 @dragover.prevent="dragOver(idx, $event)"
                 @dragleave="dragTargetIndex = null"
                 @drop.stop.prevent="drop(idx, $event)"

                 @click="selezionaPunto(idx)">

              <div class="point-title">
                <span v-if="mapSetup.allowAdd" class="drag-handle">☰</span>
                <div class="dot"></div> {{ pt._labelPrefix ? (pt._labelPrefix + ' ' + (idx+1)) : pt.label }}
                <button v-if="mapSetup.allowAdd" class="btn btn-red" style="padding:2px 6px; font-size:8px; margin-left:auto;" @click.stop="rimuoviPunto(idx)">X</button>
              </div>

              <div class="point-coord">
                {{ pt.lat !== null ? `${pt.lat.toFixed(6)}, ${pt.lon.toFixed(6)}` : 'In attesa di click...' }}
              </div>
            </div>

            <button v-if="mapSetup.allowAdd" class="btn" style="width:100%; margin-top:10px;" @click="aggiungiPunto()">+ Aggiungi Punto</button>
          </div>

          <!-- Modifica Footer a seconda della modalità -->
          <div class="map-sidebar-footer">
            <template v-if="mapPreviewAndSaveMode">
              <button class="btn" style="width:100%; margin-bottom:10px;" @click="chiudiMappa()">Indietro</button>
              <button class="btn btn-green" style="width:100%; padding: 15px 0; font-size:12px;" @click="confermaSalvataggio()">Conferma e Salva Definitivamente</button>
            </template>
            <template v-else-if="mapPreviewMode">
               <button class="btn" style="width:100%;" @click="chiudiMappa()">Chiudi Anteprima</button>
            </template>
            <template v-else>
              <button class="btn" style="width:100%; margin-bottom:10px;" @click="chiudiMappa()">Annulla</button>
              <button class="btn btn-blue" style="width:100%;" @click="confermaMappa()">Termina Selezione</button>
            </template>
          </div>
        </div>

      </div>
    </div>
  </div> <!-- /#app -->
{% endraw %}
  <script>
    const MAPBOX_TOKEN = "{{ MAPBOX_TOKEN }}";
{% raw %}
    const { createApp } = Vue;

    const generateId = () => Math.random().toString(36).substring(2, 9);

    createApp({
      data() {
        return {
          track: { id: "prima-pista", name: "Prima Pista" },
          timing: { minimum_lap_time_s: 15.0, crossing_cooldown_s: 12.0, crossing_minimum_speed_kmph: 5.0, min_satellites: 4, max_hdop: 3.5, min_fix_type: 3 },
          pit_box: { 
            shape: 'polygon', center: {lat: null, lon: null}, radius_m: 20.0, 
            polygon: [], exit_hysteresis_m: 4.0, minimum_samples_to_confirm: 3 
          },
          pit_lane: { path: [] },
          start_finish: {
            id: "SF", name: "Start / Finish", direction: "forward",
            line_extension_each_side_m: 18.0, crossing_tolerance_m: 12.0,
            a: {lat: null, lon: null}, b: {lat: null, lon: null},
            direction_reference: { before: {lat: null, lon: null}, after: {lat: null, lon: null} },
          },
          sectors: [
            { id: "S1", name: "Settore 1", direction: "forward", line_extension_each_side_m: 16.0, crossing_tolerance_m: 12.0, a: {lat: null, lon: null}, b: {lat: null, lon: null} }
          ],

          // Map States
          mapboxMap: null,
          markers: [], 
          markerElements: [], // Nuovo array per tenere traccia degli elementi DOM dei marker
          previewArrowMarkers: [], // Nuovo array per i marker freccia in anteprima
          mapModalOpen: false,
          mapTitle: "Mappa",
          mapSetup: { target: null, sectorIdx: -1, allowAdd: false },
          mapPoints: [], 
          mapActivePointIndex: 0,
          mapPreviewMode: false,
          mapPreviewAndSaveMode: false,

          // Drag and Drop Drag State
          draggedPointIdx: null,
          dragTargetIndex: null,

          // Track loading
          availableTracks: [],
          selectedTrack: '',
        }
      },
      mounted() {
        this.loadTrackList();
      },
      watch: {
        mapPoints: {
          deep: true,
          handler() { this.disegnaLineeGuida(); }
        }
      },
      methods: {
        formatC(pt) {
          if(!pt || pt.lat === null || isNaN(pt.lat)) return "Non impostato";
          return `${pt.lat.toFixed(6)}, ${pt.lon.toFixed(6)}`;
        },
        aggiungiSettore() {
          const idx = this.sectors.length + 1;
          this.sectors.push({
            id: `S${idx}`, name: `Settore ${idx}`, direction: 'forward',
            line_extension_each_side_m: 16.0, crossing_tolerance_m: 12.0,
            a: {lat: null, lon: null}, b: {lat: null, lon: null}
          });
        },
        rimuoviSettore(idx) {
          this.sectors.splice(idx, 1);
        },

        edita(target, sectorIdx = -1) {
          this.mapModalOpen = true;
          this.mapPreviewMode = false;
          this.mapPreviewAndSaveMode = false;
          this.mapTitle = target.toUpperCase();
          this.mapSetup = { target, sectorIdx, allowAdd: false };
          this.mapPoints = [];
          this.mapActivePointIndex = 0;

          if(target === 'sf') {
            this.mapTitle = "Modifica Start/Finish";
            const sf = this.start_finish;
            this.mapPoints = [
              { _id: generateId(), label: "Estremo Linea A", lat: sf.a.lat, lon: sf.a.lon },
              { _id: generateId(), label: "Estremo Linea B", lat: sf.b.lat, lon: sf.b.lon },
              { _id: generateId(), label: "Ref. Prima Traguardo", lat: sf.direction_reference.before.lat, lon: sf.direction_reference.before.lon },
              { _id: generateId(), label: "Ref. Dopo Traguardo", lat: sf.direction_reference.after.lat, lon: sf.direction_reference.after.lon },
            ];
          } else if(target === 'sector') {
            const sec = this.sectors[sectorIdx];
            this.mapTitle = `Modifica ${sec.name}`;
            this.mapPoints = [
              { _id: generateId(), label: "Estremo Linea A", lat: sec.a.lat, lon: sec.a.lon },
              { _id: generateId(), label: "Estremo Linea B", lat: sec.b.lat, lon: sec.b.lon }
            ];
          } else if(target === 'pitbox') {
            this.mapTitle = "Modifica Pit Box";
            if(this.pit_box.shape === 'circle') {
              this.mapPoints = [ { _id: generateId(), label: "Centro Pit Box", lat: this.pit_box.center.lat, lon: this.pit_box.center.lon } ];
            } else {
              this.mapSetup.allowAdd = true;
              this.pit_box.polygon.forEach((pt, i) => {
                this.mapPoints.push({ _id: generateId(), _labelPrefix: `Corner`, lat: pt.lat, lon: pt.lon });
              });
              if(this.mapPoints.length === 0) {
                 // Metto 4 corner placeholder
                 [1,2,3,4].forEach(n => this.mapPoints.push({ _id: generateId(), _labelPrefix: `Corner`, lat: null, lon: null }));
              }
            }
          } else if(target === 'pitlane') {
            this.mapTitle = "Modifica Percorso Pit Lane";
            this.mapSetup.allowAdd = true;
            this.pit_lane.path.forEach((pt, i) => {
              this.mapPoints.push({ _id: generateId(), _labelPrefix: `Punto Path`, lat: pt.lat, lon: pt.lon });
            });
            if(this.mapPoints.length === 0) {
              this.mapPoints.push({ _id: generateId(), _labelPrefix: `Punto Path`, lat: null, lon: null });
              this.mapPoints.push({ _id: generateId(), _labelPrefix: `Punto Path`, lat: null, lon: null });
            }
          }

          this.$nextTick(() => { this.initMap(); });
        },

        aggiungiPunto() {
          let prefix = this.mapSetup.target === 'pitbox' ? "Corner" : "Punto Path";
          this.mapPoints.push({ _id: generateId(), _labelPrefix: prefix, lat: null, lon: null });
          this.mapActivePointIndex = this.mapPoints.length - 1;
        },

        rimuoviPunto(idx) {
          this.mapPoints.splice(idx, 1);
          if(this.mapActivePointIndex >= this.mapPoints.length) this.mapActivePointIndex = Math.max(0, this.mapPoints.length -1);
          this.ricostruisciMarkers();
        },

        chiudiMappa() {
          this.mapModalOpen = false;
          this.clearPreviewMarkers(); // Pulisce i marker freccia in anteprima
        },

        confermaMappa() {
          const pts = this.mapPoints;
          const target = this.mapSetup.target;

          if(target === 'sf') {
            this.start_finish.a = { lat: pts[0].lat, lon: pts[0].lon };
            this.start_finish.b = { lat: pts[1].lat, lon: pts[1].lon };
            this.start_finish.direction_reference.before = { lat: pts[2].lat, lon: pts[2].lon };
            this.start_finish.direction_reference.after = { lat: pts[3].lat, lon: pts[3].lon };
          } else if(target === 'sector') {
            this.sectors[this.mapSetup.sectorIdx].a = { lat: pts[0].lat, lon: pts[0].lon };
            this.sectors[this.mapSetup.sectorIdx].b = { lat: pts[1].lat, lon: pts[1].lon };
          } else if(target === 'pitbox') {
            if(this.pit_box.shape === 'circle') {
              this.pit_box.center = { lat: pts[0].lat, lon: pts[0].lon };
            } else {
              this.pit_box.polygon = pts.map(p => ({ lat: p.lat, lon: p.lon })).filter(p => p.lat !== null);
            }
          } else if(target === 'pitlane') {
            this.pit_lane.path = pts.map(p => ({ lat: p.lat, lon: p.lon })).filter(p => p.lat !== null);
          }
          this.chiudiMappa();
        },

        // Nuovo metodo per selezionare un punto dalla sidebar e aggiornare l'evidenziazione sulla mappa
        selezionaPunto(idx) {
          this.mapActivePointIndex = idx;
          this.updateMarkerColors(); // Aggiorna i colori dei marker senza ricostruirli
        },

        // Aggiorna i colori dei marker esistenti in base all'indice attivo
        updateMarkerColors() {
          this.markerElements.forEach((el, i) => {
            if (i === this.mapActivePointIndex) {
              el.style.backgroundColor = '#31dc82'; // verde per attivo
            } else {
              el.style.backgroundColor = '#f24ddb'; // viola per normale
            }
          });
        },

        // DRAG AND DROP METHODS
        dragStart(idx, event) {
          this.draggedPointIdx = idx;
          event.dataTransfer.effectAllowed = 'move';
        },
        dragOver(idx, event) {
          if(idx !== this.draggedPointIdx) this.dragTargetIndex = idx;
        },
        drop(idx, event) {
          this.dragTargetIndex = null;
          if (this.draggedPointIdx !== null && this.draggedPointIdx !== idx) {
            const movedItem = this.mapPoints.splice(this.draggedPointIdx, 1)[0];
            this.mapPoints.splice(idx, 0, movedItem);
            if (this.mapActivePointIndex === this.draggedPointIdx) {
              this.mapActivePointIndex = idx;
            } else if (this.mapActivePointIndex > this.draggedPointIdx && this.mapActivePointIndex <= idx) {
              this.mapActivePointIndex--;
            } else if (this.mapActivePointIndex < this.draggedPointIdx && this.mapActivePointIndex >= idx) {
              this.mapActivePointIndex++;
            }
            this.$nextTick(() => { this.ricostruisciMarkers(); });
          }
          this.draggedPointIdx = null;
        },
        dropSuColonna(event) {
          // If dropped generally on the column but not on an item, push to end
          if (this.dragTargetIndex === null && this.draggedPointIdx !== null) {
            const movedItem = this.mapPoints.splice(this.draggedPointIdx, 1)[0];
            this.mapPoints.push(movedItem);
            this.ricostruisciMarkers();
          }
          this.draggedPointIdx = null;
        },

        initMap() {
          if(this.mapboxMap) {
            this.mapboxMap.resize();
            this.ricostruisciMarkers();
            return;
          }

          mapboxgl.accessToken = MAPBOX_TOKEN;
          this.mapboxMap = new mapboxgl.Map({
            container: 'map', style: 'mapbox://styles/mapbox/satellite-streets-v12',
            center: [13.1540, 41.2743], zoom: 12
          });

          const geocoder = new MapboxGeocoder({ accessToken: mapboxgl.accessToken, mapboxgl: mapboxgl, marker: false });
          document.getElementById('geocoder-box').appendChild(geocoder.onAdd(this.mapboxMap));

          this.mapboxMap.on('click', (e) => {
            if(this.mapPreviewMode) return;
            const idx = this.mapActivePointIndex;
            if(idx < 0 || idx >= this.mapPoints.length) return;

            let pt = this.mapPoints[idx];
            pt.lat = e.lngLat.lat;
            pt.lon = e.lngLat.lng;

            if(idx < this.mapPoints.length - 1 && this.mapPoints[idx+1].lat === null) {
              this.mapActivePointIndex = idx + 1;
            }
            this.ricostruisciMarkers();
          });

          this.mapboxMap.on('load', () => {
            this.ricostruisciMarkers();
          });
        },

        ricostruisciMarkers() {
          // Rimuovi vecchi marker
          this.markers.forEach(m => m.remove());
          this.markers = [];
          this.markerElements = []; // Reset

          let bounds = new mapboxgl.LngLatBounds();

          this.mapPoints.forEach((pt, idx) => {
            if(pt.lat === null || isNaN(pt.lat)) return;
            bounds.extend([pt.lon, pt.lat]);

            const el = document.createElement('div');
            el.className = 'dot';
            el.style.width = '16px'; el.style.height = '16px';
            el.style.backgroundColor = (idx === this.mapActivePointIndex && !this.mapPreviewMode) ? '#31dc82' : '#f24ddb';
            el.style.border = '2px solid #fff';
            el.style.cursor = 'grab';

            const marker = new mapboxgl.Marker({ element: el, draggable: !this.mapPreviewMode })
              .setLngLat([pt.lon, pt.lat])
              .setPopup(new mapboxgl.Popup({offset:15, closeButton:false}).setText(pt._labelPrefix ? (pt._labelPrefix + ' ' + (idx+1)) : pt.label))
              .addTo(this.mapboxMap);

            marker.on('dragstart', () => { this.mapActivePointIndex = idx; this.updateMarkerColors(); });
            marker.on('dragend', () => {
              const lngLat = marker.getLngLat();
              pt.lat = lngLat.lat; pt.lon = lngLat.lng;
              this.disegnaLineeGuida();
            });
            el.addEventListener('click', (e) => {
               if(!this.mapPreviewMode) { 
                 this.mapActivePointIndex = idx; 
                 this.updateMarkerColors(); 
                 e.stopPropagation(); 
               }
            });

            this.markers.push(marker);
            this.markerElements.push(el);
          });

          this.disegnaLineeGuida();

          if(!bounds.isEmpty() && !this.mapPreviewMode) {
             // Only fit bounds if they are not too close
             if(bounds.getNorthEast().distanceTo(bounds.getSouthWest()) > 10) {
                 this.mapboxMap.fitBounds(bounds, {padding: 50, maxZoom: 18});
             } else {
                 this.mapboxMap.flyTo({ center: bounds.getCenter(), zoom: 18 });
             }
          }
        },

        disegnaLineeGuida() {
          if(!this.mapboxMap || !this.mapboxMap.isStyleLoaded()) return;

          if(this.mapboxMap.getSource('guidelines')) {
            this.mapboxMap.removeLayer('guidelines-line');
            this.mapboxMap.removeLayer('guidelines-poly');
            this.mapboxMap.removeSource('guidelines');
          }

          if(this.mapPreviewMode) return; 

          const validPoints = this.mapPoints.filter(p => p.lat !== null).map(p => [p.lon, p.lat]);
          if(validPoints.length < 2) return;

          let geojson = { type: 'FeatureCollection', features: [] };

          if(this.mapSetup.target === 'pitbox' && this.pit_box.shape === 'polygon') {
            if(validPoints.length > 2) {
              const polyPoints = [...validPoints, validPoints[0]];
              geojson.features.push(turf.polygon([polyPoints]));
            } else {
              geojson.features.push(turf.lineString(validPoints));
            }
          } else if(this.mapSetup.target === 'pitlane') {
            geojson.features.push(turf.lineString(validPoints));
          } else if(this.mapSetup.target === 'sf' || this.mapSetup.target === 'sector') {
            if(this.mapPoints[0].lat !== null && this.mapPoints[1].lat !== null) {
              geojson.features.push(turf.lineString([ [this.mapPoints[0].lon, this.mapPoints[0].lat], [this.mapPoints[1].lon, this.mapPoints[1].lat] ]));
            }
            if(this.mapSetup.target === 'sf' && this.mapPoints[2].lat !== null && this.mapPoints[3].lat !== null) {
              const l = turf.lineString([ [this.mapPoints[2].lon, this.mapPoints[2].lat], [this.mapPoints[3].lon, this.mapPoints[3].lat] ]);
              l.properties = { isRef: true };
              geojson.features.push(l);
            }
          }

          if(geojson.features.length > 0) {
            this.mapboxMap.addSource('guidelines', { type: 'geojson', data: geojson });
            this.mapboxMap.addLayer({
              id: 'guidelines-poly', type: 'fill', source: 'guidelines',
              filter: ['==', '$type', 'Polygon'], paint: {'fill-color': '#f24ddb', 'fill-opacity': 0.3}
            });
            this.mapboxMap.addLayer({
              id: 'guidelines-line', type: 'line', source: 'guidelines',
              filter: ['==', '$type', 'LineString'], paint: {
                'line-color': ['case', ['boolean', ['get', 'isRef'], false], '#888', '#31dc82'],
                'line-width': ['case', ['boolean', ['get', 'isRef'], false], 2, 4],
                'line-dasharray': ['case', ['boolean', ['get', 'isRef'], false], [2, 2], [1]]
              }
            });
          }
        },

        apriPreviewTotale(isDaSalvataggio = false) {
          this.mapTitle = isDaSalvataggio ? "Conferma Tracciato YAML" : "Anteprima Completa YAML";
          this.mapModalOpen = true;
          this.mapPreviewMode = true;
          this.mapPreviewAndSaveMode = isDaSalvataggio;
          this.mapPoints = [];
          this.clearPreviewMarkers(); // Pulisci eventuali marker freccia precedenti
          this.$nextTick(() => { 
            if (!this.mapboxMap) {
              this.initMap();
              this.mapboxMap.once('load', () => this.disegnaAnteprima());
            } else {
              this.mapboxMap.resize();
              if (this.mapboxMap.isStyleLoaded()) {
                this.disegnaAnteprima();
              } else {
                this.mapboxMap.once('load', () => this.disegnaAnteprima());
              }
            }
          });
        },

        clearPreviewMarkers() {
          this.previewArrowMarkers.forEach(m => m.remove());
          this.previewArrowMarkers = [];
        },

        disegnaAnteprima() {
          if(!this.mapboxMap || !this.mapboxMap.isStyleLoaded()) {
            setTimeout(() => this.disegnaAnteprima(), 200);
            return;
          }
          // Pulisci marker esistenti (edit) e frecce precedenti
          this.markers.forEach(m => m.remove());
          this.markers = [];
          this.markerElements = [];
          this.clearPreviewMarkers();

          const p = this.generaPayload();
          let features = [];

          if(p.pit_box.center && p.pit_box.center.lat) {
             try { features.push(turf.circle([p.pit_box.center.lon, p.pit_box.center.lat], p.pit_box.radius_m, {units: 'meters', properties: {type: 'pitbox'}})); } catch(e){}
          } else if(p.pit_box.polygon && p.pit_box.polygon.length > 2) {
             const coords = p.pit_box.polygon.map(pt => [pt.lon, pt.lat]);
             coords.push(coords[0]);
             try { features.push(turf.polygon([coords], {type: 'pitbox'})); } catch(e){}
          }

          if(p.pit_lane.path && p.pit_lane.path.length >= 2) {
             const coords = p.pit_lane.path.map(pt => [pt.lon, pt.lat]);
             try { features.push(turf.lineString(coords, {type: 'pitlane'})); } catch(e){}
          } else if(p.pit_lane.start && p.pit_lane.end && p.pit_lane.start.lat) {
             try { features.push(turf.lineString([ [p.pit_lane.start.lon, p.pit_lane.start.lat], [p.pit_lane.end.lon, p.pit_lane.end.lat] ], {type: 'pitlane'})); } catch(e){}
          }

          if(p.start_finish.a && p.start_finish.a.lat && p.start_finish.b && p.start_finish.b.lat) {
             try { features.push(turf.lineString([ [p.start_finish.a.lon, p.start_finish.a.lat], [p.start_finish.b.lon, p.start_finish.b.lat] ], {type:'sf'})); } catch(e){}
          }

          p.sectors.forEach(s => {
             if(s.a && s.a.lat && s.b && s.b.lat) {
               try { features.push(turf.lineString([ [s.a.lon, s.a.lat], [s.b.lon, s.b.lat] ], {type:'sector', text: s.name})); } catch(e){}
             }
          });

          // Aggiungi linea di direzione (prima-dopo) per start/finish se presente
          if (p.start_finish.direction_reference.before && p.start_finish.direction_reference.after &&
              p.start_finish.direction_reference.before.lat && p.start_finish.direction_reference.after.lat) {
            const before = p.start_finish.direction_reference.before;
            const after = p.start_finish.direction_reference.after;
            features.push(turf.lineString([
              [before.lon, before.lat],
              [after.lon, after.lat]
            ], {type: 'direction_ref'}));
          }

          if(this.mapboxMap.getSource('guidelines')) {
            this.mapboxMap.removeLayer('guidelines-line');
            this.mapboxMap.removeLayer('guidelines-poly');
            this.mapboxMap.removeSource('guidelines');
          }

          if(features.length > 0) {
            const col = turf.featureCollection(features);
            this.mapboxMap.addSource('guidelines', { type: 'geojson', data: col });
            this.mapboxMap.addLayer({ id: 'guidelines-poly', type: 'fill', source: 'guidelines', filter: ['==','type','pitbox'], paint: {'fill-color': '#f24ddb', 'fill-opacity': 0.3} });
            this.mapboxMap.addLayer({ id: 'guidelines-line', type: 'line', source: 'guidelines', filter: ['in', 'type', 'pitlane', 'sector', 'sf', 'direction_ref'], paint: {
                'line-color': ['match', ['get', 'type'],
                    'pitlane', '#319cdc',
                    'sector', '#ffd700',
                    'sf', '#31dc82',
                    'direction_ref', '#ffaa00', // colore arancio per direzione
                    '#fff'],
                'line-width': ['match', ['get', 'type'],
                    'sf', 6,
                    'direction_ref', 3,
                    4],
                'line-dasharray': ['match', ['get', 'type'],
                    'direction_ref', [2, 2],
                    [1]]
            }});
            const bbox = turf.bbox(col);
            this.mapboxMap.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], {padding: 50, maxZoom: 18});
          } else { 
            if(this.mapPreviewAndSaveMode) alert("Nessun frammento tracciato utile trovato, ma salvataggio possibile.");
            else alert("Nessun frammento tracciato."); 
          }

          // Aggiungi freccia per direzione start/finish
          if (p.start_finish.direction_reference.before && p.start_finish.direction_reference.after &&
              p.start_finish.direction_reference.before.lat && p.start_finish.direction_reference.after.lat) {
            const before = p.start_finish.direction_reference.before;
            const after = p.start_finish.direction_reference.after;
            const bearing = turf.bearing(
              turf.point([before.lon, before.lat]),
              turf.point([after.lon, after.lat])
            );
            // Crea elemento freccia
            const arrowEl = document.createElement('div');
            arrowEl.innerHTML = '➤';
            arrowEl.style.fontSize = '24px';
            arrowEl.style.color = '#ffaa00';
            arrowEl.style.transform = `rotate(${bearing - 90}deg)`;
            arrowEl.style.transformOrigin = 'center';
            arrowEl.style.cursor = 'default';

            const arrowMarker = new mapboxgl.Marker({ element: arrowEl, draggable: false })
              .setLngLat([after.lon, after.lat])
              .addTo(this.mapboxMap);
            this.previewArrowMarkers.push(arrowMarker);
          }
        },

        generaPayload() {
          const sanitize = (pt) => {
            if (pt && typeof pt.lat === 'number' && typeof pt.lon === 'number' && !isNaN(pt.lat) && !isNaN(pt.lon)) {
              return { lat: parseFloat(pt.lat.toFixed(6)), lon: parseFloat(pt.lon.toFixed(6)) };
            }
            return null;
          };

          const payload = {
            track: {
              id: this.track.id,
              name: this.track.name
            },
            timing: { ...this.timing },
            pit_box: {
              exit_hysteresis_m: this.pit_box.exit_hysteresis_m,
              minimum_samples_to_confirm: this.pit_box.minimum_samples_to_confirm
            },
            pit_lane: {},
            start_finish: {
              id: this.start_finish.id,
              name: this.start_finish.name,
              direction: this.start_finish.direction,
              line_extension_each_side_m: this.start_finish.line_extension_each_side_m,
              crossing_tolerance_m: this.start_finish.crossing_tolerance_m,
              a: sanitize(this.start_finish.a),
              b: sanitize(this.start_finish.b),
              direction_reference: {
                before: sanitize(this.start_finish.direction_reference.before),
                after: sanitize(this.start_finish.direction_reference.after)
              }
            },
            sectors: this.sectors.map(s => ({
              id: s.id,
              name: s.name,
              direction: s.direction,
              line_extension_each_side_m: s.line_extension_each_side_m,
              crossing_tolerance_m: s.crossing_tolerance_m,
              a: sanitize(s.a),
              b: sanitize(s.b)
            }))
          };

          if (this.pit_box.shape === 'circle') {
            payload.pit_box.center = sanitize(this.pit_box.center) || { lat: null, lon: null };
            payload.pit_box.radius_m = this.pit_box.radius_m;
          } else {
            payload.pit_box.polygon = (this.pit_box.polygon || [])
              .map(sanitize)
              .filter(p => p !== null);
          }

          const validPath = (this.pit_lane.path || [])
            .map(sanitize)
            .filter(p => p !== null);
          payload.pit_lane.start = null;
          payload.pit_lane.end = null;

          if (validPath.length >= 2) {
            payload.pit_lane.start = validPath[0];
            payload.pit_lane.end = validPath[validPath.length - 1];
            if (validPath.length > 2) {
              payload.pit_lane.path = validPath;
            }
          } else if (validPath.length === 1) {
            payload.pit_lane.start = validPath[0];
          }

          return payload;
        },

        iniziaSalvataggio() {
          const payload = this.generaPayload();
          if(!payload.track.id) { alert("ID Pista mancante!"); return; }
          this.apriPreviewTotale(true);
        },

        async confermaSalvataggio() {
          const payload = this.generaPayload();
          try {
            const r = await fetch('/api/save-track', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
            const data = await r.json();
            if(data.success) {
               alert("Salvato in: " + data.path);
               this.chiudiMappa();
               this.loadTrackList(); // aggiorna la lista dopo il salvataggio
            } else {
               alert("Errore nel salvataggio: " + data.error);
            }
          } catch(e) { alert("Errore connessione con il file system."); }
        },

        // Nuove funzioni per caricare tracce
        async loadTrackList() {
          try {
            const r = await fetch('/api/list-tracks');
            const data = await r.json();
            if (data.tracks) {
              this.availableTracks = data.tracks;
            } else if (data.error) {
              console.error('Errore lista tracce:', data.error);
            }
          } catch(e) {
            console.error('Errore connessione:', e);
          }
        },

        async loadSelectedTrack() {
          if (!this.selectedTrack) {
            alert('Seleziona una pista da caricare.');
            return;
          }
          try {
            const r = await fetch(`/api/load-track/${this.selectedTrack}`);
            if (!r.ok) {
              const err = await r.json();
              alert('Errore caricamento: ' + (err.error || r.statusText));
              return;
            }
            const data = await r.json();
            this.caricaPayload(data);
            alert('Caricata con successo!');
          } catch(e) {
            alert('Errore di connessione.');
          }
        },

        caricaPayload(data) {
          // Reset e popola i campi dal payload ricevuto
          this.track.id = data.track?.id || "";
          this.track.name = data.track?.name || "";

          // Timing
          const t = data.timing || {};
          this.timing = {
            minimum_lap_time_s: t.minimum_lap_time_s ?? 15.0,
            crossing_cooldown_s: t.crossing_cooldown_s ?? 12.0,
            crossing_minimum_speed_kmph: t.crossing_minimum_speed_kmph ?? 5.0,
            min_satellites: t.min_satellites ?? 4,
            max_hdop: t.max_hdop ?? 3.5,
            min_fix_type: t.min_fix_type ?? 3
          };

          // Pit box
          const pb = data.pit_box || {};
          this.pit_box.exit_hysteresis_m = pb.exit_hysteresis_m ?? 4.0;
          this.pit_box.minimum_samples_to_confirm = pb.minimum_samples_to_confirm ?? 3;

          if (pb.center && pb.radius_m !== undefined) {
            this.pit_box.shape = 'circle';
            this.pit_box.center = { lat: pb.center.lat, lon: pb.center.lon };
            this.pit_box.radius_m = pb.radius_m;
            this.pit_box.polygon = [];
          } else {
            this.pit_box.shape = 'polygon';
            this.pit_box.polygon = (pb.polygon || []).map(p => ({ lat: p.lat, lon: p.lon }));
            this.pit_box.center = { lat: null, lon: null };
            this.pit_box.radius_m = 20.0;
          }

          // Pit lane
          this.pit_lane.path = [];
          if (data.pit_lane?.path && Array.isArray(data.pit_lane.path)) {
            this.pit_lane.path = data.pit_lane.path.map(p => ({ lat: p.lat, lon: p.lon }));
          } else if (data.pit_lane?.start && data.pit_lane?.end) {
            this.pit_lane.path = [
              { lat: data.pit_lane.start.lat, lon: data.pit_lane.start.lon },
              { lat: data.pit_lane.end.lat, lon: data.pit_lane.end.lon }
            ];
          }

          // Start/Finish
          const sf = data.start_finish || {};
          this.start_finish.id = sf.id || "SF";
          this.start_finish.name = sf.name || "Start / Finish";
          this.start_finish.direction = sf.direction || "forward";
          this.start_finish.line_extension_each_side_m = sf.line_extension_each_side_m ?? 18.0;
          this.start_finish.crossing_tolerance_m = sf.crossing_tolerance_m ?? 12.0;
          this.start_finish.a = sf.a ? { lat: sf.a.lat, lon: sf.a.lon } : { lat: null, lon: null };
          this.start_finish.b = sf.b ? { lat: sf.b.lat, lon: sf.b.lon } : { lat: null, lon: null };
          this.start_finish.direction_reference.before = sf.direction_reference?.before ? 
              { lat: sf.direction_reference.before.lat, lon: sf.direction_reference.before.lon } : { lat: null, lon: null };
          this.start_finish.direction_reference.after = sf.direction_reference?.after ? 
              { lat: sf.direction_reference.after.lat, lon: sf.direction_reference.after.lon } : { lat: null, lon: null };

          // Settori
          this.sectors = (data.sectors || []).map(s => ({
            id: s.id || "",
            name: s.name || "",
            direction: s.direction || "forward",
            line_extension_each_side_m: s.line_extension_each_side_m ?? 16.0,
            crossing_tolerance_m: s.crossing_tolerance_m ?? 12.0,
            a: s.a ? { lat: s.a.lat, lon: s.a.lon } : { lat: null, lon: null },
            b: s.b ? { lat: s.b.lat, lon: s.b.lon } : { lat: null, lon: null }
          }));
        },

        // Nuova funzione per creare una pista da zero
        nuovaPista() {
          this.track = { id: "prima-pista", name: "Prima Pista" };
          this.timing = { minimum_lap_time_s: 15.0, crossing_cooldown_s: 12.0, crossing_minimum_speed_kmph: 5.0, min_satellites: 4, max_hdop: 3.5, min_fix_type: 3 };
          this.pit_box = { shape: 'polygon', center: {lat: null, lon: null}, radius_m: 20.0, polygon: [], exit_hysteresis_m: 4.0, minimum_samples_to_confirm: 3 };
          this.pit_lane = { path: [] };
          this.start_finish = {
            id: "SF", name: "Start / Finish", direction: "forward",
            line_extension_each_side_m: 18.0, crossing_tolerance_m: 12.0,
            a: {lat: null, lon: null}, b: {lat: null, lon: null},
            direction_reference: { before: {lat: null, lon: null}, after: {lat: null, lon: null} }
          };
          this.sectors = [
            { id: "S1", name: "Settore 1", direction: "forward", line_extension_each_side_m: 16.0, crossing_tolerance_m: 12.0, a: {lat: null, lon: null}, b: {lat: null, lon: null} }
          ];
          this.selectedTrack = ''; // reset selezione
          this.mapModalOpen = false; // chiudi eventuale mappa
        }
      } 
    }).mount('#app');
{% endraw %}
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML, MAPBOX_TOKEN=MAPBOX_TOKEN)


@app.route("/api/save-track", methods=["POST"])
def save_track():
    try:
        data = request.json
        track_id = data.get("track", {}).get("id")
        if not track_id:
            return jsonify({"success": False, "error": "track ID mancante"}), 400

        filename = f"{track_id}.yaml"
        file_path = os.path.join(TRACKS_DIR, filename)
        os.makedirs(TRACKS_DIR, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)

        return jsonify({"success": True, "path": file_path})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/list-tracks", methods=["GET"])
def list_tracks():
    try:
        if not os.path.isdir(TRACKS_DIR):
            return jsonify({"tracks": []})
        files = [f[:-5] for f in os.listdir(TRACKS_DIR) if f.endswith(".yaml")]
        return jsonify({"tracks": files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/load-track/<track_id>", methods=["GET"])
def load_track(track_id):
    try:
        filename = f"{track_id}.yaml"
        file_path = os.path.join(TRACKS_DIR, filename)
        if not os.path.exists(file_path):
            return jsonify({"error": "File non trovato"}), 404
        with open(file_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = 5002
    print(f"Avvio strumento creazione pista su http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)