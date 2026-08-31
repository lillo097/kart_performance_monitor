#!/usr/bin/env python3

import math
import time
from datetime import datetime

from flask import Flask, jsonify, render_template_string


HOST = "0.0.0.0"
PORT = 8090

app = Flask(__name__)

STARTED_AT = time.monotonic()

SECTOR_DURATIONS = [10.850, 12.150, 8.950]
BEST_SECTORS = [10.620, 11.930, 8.740]

STATE = {
    "mode": "auto",
    "last_mode_change": time.monotonic(),
}

LAST_SECTOR_RESULTS = [
    {"value": None, "status": ""},
    {"value": None, "status": ""},
    {"value": None, "status": ""},
]


HTML = r"""
<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <title>Kart Dashboard Layout Sandbox</title>
  <meta
    name="viewport"
    content="width=device-width, initial-scale=1, viewport-fit=cover, maximum-scale=1, user-scalable=no"
  >
  <style>
    :root {
      --black: #050505;
      --panel: #111214;
      --panel-soft: #17181b;
      --border: #2b2d32;
      --text: #f7f7f8;
      --muted: #92959d;
      --green: #31dc82;
      --green-soft: rgba(49, 220, 130, 0.18);
      --red: #ff3b30;
      --red-soft: rgba(255, 59, 48, 0.20);
      --purple: #f24ddb;
      --purple-bright: #ff80eb;
      --purple-soft: rgba(242, 77, 219, 0.20);
      --yellow: #ffd700;
      --yellow-soft: rgba(255, 215, 0, 0.18);
      --safe-left: max(10px, env(safe-area-inset-left));
      --safe-right: max(10px, env(safe-area-inset-right));
      --safe-top: max(6px, env(safe-area-inset-top));
      --safe-bottom: max(6px, env(safe-area-inset-bottom));
    }

    * {
      box-sizing: border-box;
    }

    html,
    body {
      width: 100%;
      height: 100%;
      margin: 0;
      overflow: hidden;
      background: var(--black);
      color: var(--text);
      font-family:
        -apple-system,
        BlinkMacSystemFont,
        "SF Pro Display",
        "Segoe UI",
        sans-serif;
    }

    body {
      padding:
        var(--safe-top)
        var(--safe-right)
        var(--safe-bottom)
        var(--safe-left);
    }

    button {
      font: inherit;
    }

    .app {
      width: 100%;
      height: 100%;
      display: grid;
      grid-template-rows: 40px minmax(0, 1fr) 44px;
      overflow: hidden;
      background:
        radial-gradient(
          circle at 50% 55%,
          #17181d 0%,
          #080809 50%,
          #040404 100%
        );
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-width: 0;
      padding: 0 8px;
      border-bottom: 1px solid var(--border);
    }

    .brand {
      min-width: 0;
    }

    .track-name {
      overflow: hidden;
      color: #e1e2e6;
      font-size: 11px;
      font-weight: 900;
      letter-spacing: 0.12em;
      text-overflow: ellipsis;
      text-transform: uppercase;
      white-space: nowrap;
    }

    .driver-name {
      margin-top: 2px;
      color: var(--muted);
      font-size: 9px;
      white-space: nowrap;
    }

    .top-status {
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .status-badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 22px;
      padding: 3px 8px;
      border: 1px solid rgba(49, 220, 130, 0.7);
      border-radius: 6px;
      color: #dbffea;
      background: rgba(22, 131, 72, 0.24);
      font-size: 9px;
      font-weight: 900;
      letter-spacing: 0.08em;
      white-space: nowrap;
    }

    .gps {
      color: var(--green);
      font-size: 9px;
      font-weight: 800;
      text-align: right;
      white-space: nowrap;
    }

    .layout {
      min-width: 0;
      min-height: 0;
      display: grid;
      grid-template-columns:
        minmax(190px, 0.85fr)
        minmax(220px, 1.15fr)
        minmax(210px, 0.95fr);
      gap: 5px;
      padding: 4px;
    }

    .panel {
      min-width: 0;
      min-height: 0;
      overflow: hidden;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: rgba(17, 18, 20, 0.93);
    }

    .panel-heading {
      color: var(--muted);
      font-size: 9px;
      font-weight: 900;
      letter-spacing: 0.15em;
      text-transform: uppercase;
    }

    /* LEFT: sectors */

    .sectors-panel {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
      gap: 10px;
      padding: 10px 10px 12px;
    }

    .sector-current {
      display: flex;
      flex-direction: column;
      justify-content: center;
      min-height: 0;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 9px;
      background: rgba(8, 8, 9, 0.74);
    }

    .sector-kicker {
      color: var(--muted);
      font-size: 9px;
      font-weight: 900;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }

    .sector-id {
      margin-top: 6px;
      color: #fff;
      font-size: clamp(28px, 3.6vw, 44px);
      font-weight: 900;
      line-height: 0.9;
      letter-spacing: -0.06em;
      font-variant-numeric: tabular-nums;
    }

    .sector-state {
      min-height: 14px;
      margin-top: 8px;
      color: var(--muted);
      font-size: 9px;
      font-weight: 900;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }

    .sector-time {
      margin-top: 6px;
      color: #fff;
      font-size: clamp(28px, 3.8vw, 52px);
      font-weight: 900;
      line-height: 0.9;
      letter-spacing: -0.04em;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }

    .sector-current.result-best {
      border-color: var(--purple);
      background:
        radial-gradient(
          ellipse at center,
          var(--purple-soft) 0%,
          rgba(66, 15, 72, 0.12) 46%,
          rgba(8, 8, 9, 0.76) 84%
        );
      box-shadow:
        inset 0 0 30px rgba(242, 77, 219, 0.18),
        0 0 20px rgba(242, 77, 219, 0.24);
    }

    .sector-current.result-improved {
      border-color: var(--green);
      background:
        radial-gradient(
          ellipse at center,
          var(--green-soft) 0%,
          rgba(7, 69, 38, 0.12) 46%,
          rgba(8, 8, 9, 0.76) 84%
        );
      box-shadow:
        inset 0 0 30px rgba(49, 220, 130, 0.16),
        0 0 20px rgba(49, 220, 130, 0.20);
    }

    .sector-current.result-slower {
      border-color: var(--yellow);
      background:
        radial-gradient(
          ellipse at center,
          var(--yellow-soft) 0%,
          rgba(82, 68, 0, 0.12) 46%,
          rgba(8, 8, 9, 0.76) 84%
        );
      box-shadow:
        inset 0 0 30px rgba(255, 215, 0, 0.14),
        0 0 20px rgba(255, 215, 0, 0.18);
    }

    .sector-current.result-best .sector-time,
    .sector-current.result-best .sector-state {
      color: var(--purple-bright);
      text-shadow: 0 0 14px rgba(242, 77, 219, 0.34);
    }

    .sector-current.result-improved .sector-time,
    .sector-current.result-improved .sector-state {
      color: var(--green);
      text-shadow: 0 0 14px rgba(49, 220, 130, 0.26);
    }

    .sector-current.result-slower .sector-time,
    .sector-current.result-slower .sector-state {
      color: #ffb700;
      text-shadow: 0 0 14px rgba(255, 215, 0, 0.26);
    }

    .sector-history {
      display: grid;
      gap: 8px;
    }

    .sector-history-title {
      color: var(--muted);
      font-size: 9px;
      font-weight: 900;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }

    .sector-row {
      display: grid;
      grid-template-columns: 26px minmax(0, 1fr);
      gap: 6px;
      align-items: center;
    }

    .sector-row-name {
      color: var(--muted);
      font-size: 11px;
      font-weight: 900;
      letter-spacing: 0.08em;
    }

    .sector-row-value {
      color: #dfe0e3;
      font-size: clamp(14px, 1.8vw, 22px);
      font-weight: 900;
      line-height: 1;
      text-align: right;
      font-variant-numeric: tabular-nums;
    }

    .sector-row-value.best {
      color: var(--purple-bright);
      text-shadow: 0 0 11px rgba(242, 77, 219, 0.26);
    }

    .sector-row-value.improved {
      color: var(--green);
    }

    .sector-row-value.slower {
      color: #ffb700;
      text-shadow: 0 0 10px rgba(255, 215, 0, 0.22);
    }

    /* CENTER: speed */

    .speed-panel {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 10px;
      background:
        radial-gradient(
          ellipse at center,
          rgba(255, 255, 255, 0.045) 0%,
          rgba(17, 18, 20, 0.94) 62%
        );
    }

    .speed-label {
      color: var(--muted);
      font-size: clamp(10px, 1.25vw, 14px);
      font-weight: 900;
      letter-spacing: 0.16em;
      text-transform: uppercase;
    }

    .speed-value {
      margin: 6px 0 4px;
      color: #fff;
      font-size: clamp(86px, 13.5vw, 190px);
      font-weight: 900;
      line-height: 0.78;
      letter-spacing: -0.09em;
      font-variant-numeric: tabular-nums;
      text-shadow: 0 0 27px rgba(255, 255, 255, 0.18);
    }

    .speed-unit {
      color: #a6a9af;
      font-size: clamp(11px, 1.35vw, 16px);
      font-weight: 900;
      letter-spacing: 0.17em;
    }

    .track-state {
      margin-top: 14px;
      padding: 4px 8px;
      border: 1px solid rgba(49, 220, 130, 0.66);
      border-radius: 6px;
      color: #ddffeb;
      background: rgba(22, 131, 72, 0.20);
      font-size: 9px;
      font-weight: 900;
      letter-spacing: 0.10em;
    }

    /* RIGHT: lap */

    .lap-panel {
      display: grid;
      grid-template-rows: auto auto minmax(0, 1fr);
      gap: 10px;
      padding: 10px 10px 12px;
    }

    .lap-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }

    .lap-number-block {
      display: flex;
      align-items: baseline;
      gap: 7px;
    }

    .lap-number-label {
      color: var(--muted);
      font-size: 9px;
      font-weight: 900;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }

    .lap-number {
      color: #fff;
      font-size: clamp(38px, 4.8vw, 68px);
      font-weight: 900;
      line-height: 0.85;
      letter-spacing: -0.07em;
    }

    .lap-valid {
      padding: 3px 6px;
      border: 1px solid rgba(49, 220, 130, 0.65);
      border-radius: 5px;
      color: #d8ffe7;
      background: rgba(37, 199, 106, 0.2);
      font-size: 8px;
      font-weight: 900;
      letter-spacing: 0.08em;
    }

    .lap-metric {
      min-width: 0;
      padding: 8px 9px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: rgba(8, 8, 9, 0.54);
    }

    .lap-metric-label {
      color: var(--muted);
      font-size: 9px;
      font-weight: 900;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }

    .lap-metric-value {
      margin-top: 6px;
      color: #fff;
      font-size: clamp(20px, 2.7vw, 36px);
      font-weight: 900;
      line-height: 0.93;
      letter-spacing: -0.03em;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }

    .lap-metrics {
      display: grid;
      gap: 14px;
      align-content: start;
    }

    .lap-metric.best-lap {
      margin-top: 0;
      border-color: rgba(242, 77, 219, 0.84);
      background:
        linear-gradient(
          135deg,
          rgba(242, 77, 219, 0.22),
          rgba(65, 10, 70, 0.56)
        );
      box-shadow:
        inset 0 0 22px rgba(242, 77, 219, 0.11),
        0 0 16px rgba(242, 77, 219, 0.11);
    }

    .lap-metric.best-lap .lap-metric-label {
      color: #ffb4ef;
    }

    .lap-metric.best-lap .lap-metric-value {
      color: var(--purple-bright);
      text-shadow: 0 0 14px rgba(242, 77, 219, 0.35);
    }

    /* Controls */

    .controls {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 4px 6px;
      border-top: 1px solid var(--border);
      background: rgba(15, 16, 18, 0.98);
    }

    .control {
      min-width: 86px;
      height: 34px;
      padding: 0 10px;
      border: 1px solid var(--border);
      border-radius: 6px;
      color: #e9e9eb;
      background: #202124;
      font-size: 9px;
      font-weight: 900;
      letter-spacing: 0.04em;
      cursor: pointer;
    }

    .control.green {
      border-color: rgba(49, 220, 130, 0.52);
      background: rgba(22, 131, 72, 0.45);
    }

    .control.red {
      border-color: rgba(255, 94, 105, 0.52);
      background: rgba(147, 24, 35, 0.42);
    }

    .control.purple {
      border-color: rgba(242, 77, 219, 0.55);
      background: rgba(104, 19, 111, 0.45);
    }

    @media (max-width: 840px) {
      .layout {
        grid-template-columns:
          minmax(165px, 0.80fr)
          minmax(190px, 1.10fr)
          minmax(185px, 0.95fr);
        gap: 4px;
        padding: 3px;
      }

      .sectors-panel,
      .lap-panel {
        padding: 8px 8px 10px;
      }

      .sector-current {
        padding: 8px 10px;
      }

      .controls {
        gap: 4px;
      }

      .control {
        min-width: 74px;
        padding: 0 7px;
        font-size: 8px;
      }
    }

    @media (orientation: portrait) {
      .app {
        min-height: 100dvh;
        height: auto;
        grid-template-rows: 44px auto 50px;
        overflow-y: auto;
      }

      html,
      body {
        overflow: auto;
      }

      .layout {
        grid-template-columns: 1fr;
        grid-template-rows: 260px 320px 340px;
      }

      .speed-panel {
        grid-row: 1;
      }

      .sectors-panel {
        grid-row: 2;
      }

      .lap-panel {
        grid-row: 3;
      }
    }
  </style>
</head>
<body>
  <main class="app">
    <header class="topbar">
      <div class="brand">
        <div class="track-name">Prima Pista · Layout Sandbox</div>
        <div class="driver-name">Pilota: Livio · Dati simulati</div>
      </div>

      <div class="top-status">
        <div id="gps" class="gps">GPS: FIX 3D · SAT 10 · HDOP 0.75</div>
        <div id="session-status" class="status-badge">IN CORSO</div>
      </div>
    </header>

    <section class="layout">
      <aside class="panel sectors-panel">
        <div class="panel-heading">Settori</div>

        <div id="sector-current" class="sector-current">
          <div class="sector-kicker">Settore corrente</div>
          <div id="sector-id" class="sector-id">S1</div>
          <div id="sector-state" class="sector-state">TEMPO IN CORSO</div>
          <div id="sector-time" class="sector-time">00:00.000</div>
        </div>

        <div class="sector-history">
          <div class="sector-history-title">Ultimi settori</div>

          <div class="sector-row">
            <div class="sector-row-name">S1</div>
            <div id="history-s1" class="sector-row-value">—</div>
          </div>

          <div class="sector-row">
            <div class="sector-row-name">S2</div>
            <div id="history-s2" class="sector-row-value">—</div>
          </div>

          <div class="sector-row">
            <div class="sector-row-name">S3</div>
            <div id="history-s3" class="sector-row-value">—</div>
          </div>
        </div>
      </aside>

      <section class="panel speed-panel">
        <div class="speed-label">Velocità</div>
        <div id="speed-value" class="speed-value">0</div>
        <div class="speed-unit">KM/H</div>
        <div id="track-state" class="track-state">IN PISTA</div>
      </section>

      <aside class="panel lap-panel">
        <div class="lap-top">
          <div class="lap-number-block">
            <div class="lap-number-label">Giro</div>
            <div id="lap-number" class="lap-number">1</div>
          </div>
          <div id="lap-valid" class="lap-valid">VALIDO</div>
        </div>

        <div class="lap-metric">
          <div class="lap-metric-label">Giro corrente</div>
          <div id="current-lap-time" class="lap-metric-value">00:00.000</div>
        </div>

        <div class="lap-metrics">
          <div class="lap-metric">
            <div class="lap-metric-label">Ultimo giro</div>
            <div id="last-lap-time" class="lap-metric-value">—</div>
          </div>

          <div class="lap-metric best-lap">
            <div class="lap-metric-label">Miglior giro</div>
            <div id="best-lap-time" class="lap-metric-value">—</div>
          </div>
        </div>
      </aside>
    </section>

    <footer class="controls">
      <button class="control" onclick="setMode('auto')">AUTO</button>
      <button class="control purple" onclick="setMode('best')">BEST SETTORE</button>
      <button class="control green" onclick="setMode('improved')">DELTA −</button>
      <button class="control red" onclick="setMode('slower')">DELTA +</button>
      <button class="control" onclick="setMode('pitlane')">PIT LANE</button>
    </footer>
  </main>

  <script>
    const ui = {
      speed: document.getElementById("speed-value"),
      sectorCurrent: document.getElementById("sector-current"),
      sectorId: document.getElementById("sector-id"),
      sectorTime: document.getElementById("sector-time"),
      sectorState: document.getElementById("sector-state"),
      historyS1: document.getElementById("history-s1"),
      historyS2: document.getElementById("history-s2"),
      historyS3: document.getElementById("history-s3"),
      lapNumber: document.getElementById("lap-number"),
      currentLap: document.getElementById("current-lap-time"),
      lastLap: document.getElementById("last-lap-time"),
      bestLap: document.getElementById("best-lap-time"),
      trackState: document.getElementById("track-state"),
      gps: document.getElementById("gps"),
      sessionStatus: document.getElementById("session-status"),
    };

    function formatTime(value) {
      if (value === null || value === undefined) {
        return "—";
      }

      const seconds = Math.max(0, Number(value));
      const minutes = Math.floor(seconds / 60);
      const secondPart = Math.floor(seconds % 60);
      const milliseconds = Math.floor(
        (seconds - Math.floor(seconds)) * 1000
      );

      return (
        String(minutes).padStart(2, "0") +
        ":" +
        String(secondPart).padStart(2, "0") +
        "." +
        String(milliseconds).padStart(3, "0")
      );
    }

    function formatDelta(value) {
      const delta = Number(value);
      return (delta < 0 ? "−" : "+") + Math.abs(delta).toFixed(3);
    }

    function updateHistory(history) {
      const targets = [
        ui.historyS1,
        ui.historyS2,
        ui.historyS3,
      ];

      for (let index = 0; index < targets.length; index += 1) {
        const item = history[index] || {};
        const target = targets[index];

        target.textContent = formatTime(item.value);
        target.className = "sector-row-value " + (item.status || "");
      }
    }

    function update(data) {
      ui.speed.textContent = Math.round(data.speed_kmph);
      ui.lapNumber.textContent = data.lap_number;
      ui.currentLap.textContent = formatTime(data.current_lap_time_s);
      ui.lastLap.textContent = formatTime(data.last_lap_time_s);
      ui.bestLap.textContent = formatTime(data.best_lap_time_s);

      ui.sectorId.textContent = "S" + data.current_sector_number;
      ui.sectorTime.textContent = data.show_delta
        ? formatDelta(data.delta_s)
        : formatTime(data.current_sector_elapsed_s);

      ui.sectorState.textContent = "TEMPO IN CORSO";

      ui.sectorCurrent.className =
        "sector-current " + (data.show_delta
          ? "result-" + data.sector_status
          : "");

      ui.trackState.textContent = data.track_state_label;
      ui.gps.textContent = data.gps_label;
      ui.sessionStatus.textContent = data.session_label;

      updateHistory(data.history);
    }

    async function refresh() {
      try {
        const response = await fetch("/api/live", {
          cache: "no-store",
        });

        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }

        update(await response.json());
      } catch (error) {
        console.error(error);
      }
    }

    async function setMode(mode) {
      await fetch("/api/mode/" + mode, {
        method: "POST",
      });
      await refresh();
    }

    refresh();
    window.setInterval(refresh, 150);
  </script>
</body>
</html>
"""


def clamp(value, low, high):
    return max(low, min(high, value))


def format_gps_label(mode):
    if mode == "pitlane":
        return "GPS: FIX 3D · SAT 10 · HDOP 0.75"

    return "GPS: FIX 3D · SAT 10 · HDOP 0.75"


def auto_snapshot(elapsed_s):
    lap_duration_s = sum(SECTOR_DURATIONS)
    lap_number = int(elapsed_s // lap_duration_s) + 1
    lap_elapsed_s = elapsed_s % lap_duration_s

    boundaries = [
        SECTOR_DURATIONS[0],
        SECTOR_DURATIONS[0] + SECTOR_DURATIONS[1],
        lap_duration_s,
    ]

    sector_index = 0
    sector_start_s = 0.0

    if lap_elapsed_s >= boundaries[0]:
        sector_index = 1
        sector_start_s = boundaries[0]

    if lap_elapsed_s >= boundaries[1]:
        sector_index = 2
        sector_start_s = boundaries[1]

    sector_elapsed_s = lap_elapsed_s - sector_start_s

    speed_base = [58.0, 63.0, 55.0][sector_index]
    speed_kmph = speed_base + math.sin(elapsed_s * 1.3) * 6.0

    show_delta = False
    delta_s = None
    sector_status = ""
    sector_label = ""

    just_closed_sector = None
    if abs(lap_elapsed_s - boundaries[0]) < 1.1:
        just_closed_sector = 0
    elif abs(lap_elapsed_s - boundaries[1]) < 1.1:
        just_closed_sector = 1
    elif lap_elapsed_s < 1.1 and elapsed_s > 1.1:
        just_closed_sector = 2

    if just_closed_sector is not None:
        pattern = lap_number % 3

        if pattern == 1:
            sector_status = "best"
            sector_label = "NUOVO RIFERIMENTO"
            LAST_SECTOR_RESULTS[just_closed_sector] = {
                "value": BEST_SECTORS[just_closed_sector],
                "status": "best",
            }
        elif pattern == 2:
            sector_status = "improved"
            sector_label = "GUADAGNO"
            delta_s = -0.300 - 0.050 * just_closed_sector
            LAST_SECTOR_RESULTS[just_closed_sector] = {
                "value": BEST_SECTORS[just_closed_sector] + delta_s,
                "status": "improved",
            }
        else:
            sector_status = "slower"
            sector_label = "PERDITA"
            delta_s = 0.260 + 0.060 * just_closed_sector
            LAST_SECTOR_RESULTS[just_closed_sector] = {
                "value": BEST_SECTORS[just_closed_sector] + delta_s,
                "status": "slower",
            }

        show_delta = True

    history = [dict(item) for item in LAST_SECTOR_RESULTS]

    last_lap_time_s = None
    best_lap_time_s = None

    if lap_number >= 2:
        last_lap_time_s = lap_duration_s - 0.220
        best_lap_time_s = lap_duration_s - 0.460

    return {
        "speed_kmph": clamp(speed_kmph, 20.0, 78.0),
        "lap_number": lap_number,
        "current_lap_time_s": lap_elapsed_s,
        "last_lap_time_s": last_lap_time_s,
        "best_lap_time_s": best_lap_time_s,
        "current_sector_number": sector_index + 1,
        "current_sector_elapsed_s": sector_elapsed_s,
        "track_state_label": "IN PISTA",
        "gps_label": format_gps_label("auto"),
        "session_label": "IN CORSO",
        "show_delta": show_delta,
        "delta_s": delta_s,
        "sector_status": sector_status,
        "sector_label": sector_label,
        "history": history,
    }


def manual_snapshot(mode):
    now = time.monotonic()
    pulse = (math.sin(now * 1.8) + 1.0) / 2.0

    config = {
        "best": {
            "status": "best",
            "label": "NUOVO RIFERIMENTO",
            "time": 10.620,
            "sector": 1,
        },
        "improved": {
            "status": "improved",
            "label": "GUADAGNO",
            "delta": -0.030,
            "sector": 2,
        },
        "slower": {
            "status": "slower",
            "label": "PERDITA",
            "delta": 0.420,
            "sector": 3,
        },
    }

    if mode == "pitlane":
        return {
            "speed_kmph": 20.0 + pulse * 2.0,
            "lap_number": 3,
            "current_lap_time_s": None,
            "last_lap_time_s": 33.845,
            "best_lap_time_s": 32.991,
            "current_sector_number": 1,
            "current_sector_elapsed_s": None,
            "track_state_label": "IN PIT LANE",
            "gps_label": format_gps_label("pitlane"),
            "session_label": "IN CORSO",
            "show_delta": False,
            "delta_s": None,
            "sector_status": "",
            "sector_label": "NESSUN SETTORE",
            "history": [
                {"value": 10.620, "status": "best"},
                {"value": 12.220, "status": ""},
                {"value": 8.740, "status": "best"},
            ],
        }

    item = config[mode]

    return {
        "speed_kmph": 56.0 + pulse * 8.0,
        "lap_number": 3,
        "current_lap_time_s": 19.420,
        "last_lap_time_s": 33.845,
        "best_lap_time_s": 32.991,
        "current_sector_number": item["sector"],
        "current_sector_elapsed_s": 4.876,
        "track_state_label": "IN PISTA",
        "gps_label": format_gps_label(mode),
        "session_label": "IN CORSO",
        "show_delta": True,
        "delta_s": item.get("delta"),
        "sector_status": item["status"],
        "sector_label": item["label"],
        "history": [
            {
                "value": 10.620,
                "status": "best",
            },
            {
                "value": 11.630 if mode == "improved" else 12.220,
                "status": "improved" if mode == "improved" else "",
            },
            {
                "value": 9.160 if mode == "slower" else 8.740,
                "status": "slower" if mode == "slower" else "best",
            },
        ],
    }


@app.get("/")
def dashboard():
    return render_template_string(HTML)


@app.get("/api/live")
def api_live():
    mode = STATE["mode"]

    if mode == "auto":
        return jsonify(auto_snapshot(time.monotonic() - STARTED_AT))

    return jsonify(manual_snapshot(mode))


@app.post("/api/mode/<mode>")
def api_mode(mode):
    valid_modes = {"auto", "best", "improved", "slower", "pitlane"}

    if mode not in valid_modes:
        return jsonify({
            "ok": False,
            "error": "Modalità non valida.",
        }), 400

    STATE["mode"] = mode
    STATE["last_mode_change"] = time.monotonic()

    return jsonify({
        "ok": True,
        "mode": mode,
    })


if __name__ == "__main__":
    print()
    print("=== Kart Dashboard Layout Sandbox ===")
    print(f"Apri sul Mac:    http://127.0.0.1:{PORT}")
    print(f"API mock:        http://127.0.0.1:{PORT}/api/live")
    print("Questa sandbox non modifica app.py, MQTT o file della dashboard reale.")
    print()

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
