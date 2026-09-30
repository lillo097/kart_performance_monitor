(function () {
  "use strict";

  const element = (id) => document.getElementById(id);

  const ui = {
    trackName: element("track-name"),
    driverName: element("driver-name"),
    sessionState: element("session-state"),
    backButton: element("back-button"),

    speedValue: element("speed-value"),

    lapNumber: element("lap-number"),
    lapValid: element("lap-valid"),
    currentLapTime: element("current-lap-time"),
    lastLapTime: element("last-lap-time"),
    bestLapTime: element("best-lap-time"),
    bestLapPanel: element("best-lap-panel"),

    sectorCurrent: element("sector-live-panel"),
    sectorId: element("sector-id"),
    sectorTime: element("sector-live-time"),
    sectorState: element("sector-live-status"),

    sectorHistoryS1: element("sector-history-s1"),
    sectorHistoryS2: element("sector-history-s2"),
    sectorHistoryS3: element("sector-history-s3"),

    trackState: element("track-status"),
    gpsMini: element("gps-mini"),

    startButton: element("start-button"),
    pauseButton: element("pause-button"),
    resumeButton: element("resume-button"),
    stopButton: element("stop-button"),
  };

  const driverId = new URLSearchParams(
    window.location.search
  ).get("driver");

  const SECTOR_RESULT_SHOW_MS = 5000;

  let visibleResultKey = null;
  let visibleResultUntil = 0;

  function formatTime(seconds) {
    if (
      seconds === null ||
      seconds === undefined ||
      !Number.isFinite(Number(seconds))
    ) {
      return "—";
    }

    const value = Math.max(0, Number(seconds));
    const minutes = Math.floor(value / 60);
    const secondsPart = Math.floor(value % 60);
    const milliseconds = Math.floor(
      (value - Math.floor(value)) * 1000
    );

    return (
      String(minutes).padStart(2, "0") +
      ":" +
      String(secondsPart).padStart(2, "0") +
      "." +
      String(milliseconds).padStart(3, "0")
    );
  }

  function formatSectorValue(seconds) {
    if (
      seconds === null ||
      seconds === undefined ||
      !Number.isFinite(Number(seconds))
    ) {
      return "—";
    }

    const value = Math.max(0, Number(seconds));

    if (value < 60) {
      return value.toFixed(3);
    }

    const minutes = Math.floor(value / 60);
    const secondsPart = value - minutes * 60;

    return (
      String(minutes) +
      ":" +
      secondsPart.toFixed(3).padStart(6, "0")
    );
  }

  function formatDelta(seconds) {
    if (
      seconds === null ||
      seconds === undefined ||
      !Number.isFinite(Number(seconds))
    ) {
      return "—";
    }

    const value = Number(seconds);
    const sign = value < 0 ? "−" : "+";
    const absolute = Math.abs(value);

    if (absolute < 60) {
      return sign + absolute.toFixed(3);
    }

    const minutes = Math.floor(absolute / 60);
    const secondsPart = absolute - minutes * 60;

    return (
      sign +
      String(minutes) +
      ":" +
      secondsPart.toFixed(3).padStart(6, "0")
    );
  }

  function updateSessionState(status) {
    const labels = {
      idle: "IN ATTESA",
      running: "RUNNING",
      paused: "IN PAUSA",
      stopped: "STOP",
    };

    ui.sessionState.textContent =
      labels[status] || "IN ATTESA";

    ui.sessionState.className =
      "status-badge " + (status || "");
  }

  function updateTrackState(trackState) {
    let label = "POSIZIONE N/D";
    let cls = "";

    if (trackState === "box") {
      label = "IN BOX";
      cls = "pit";
    } else if (trackState === "warmup") {
      label = "WARM-UP";
      cls = "warmup";
    } else if (trackState === "on_track") {
      label = "IN PISTA";
    } else if (trackState === "pitlane") {
      label = "IN PIT LANE";
    }

    ui.trackState.textContent = label;
    ui.trackState.className = "track-state " + cls;
  }

  function updateLapValidity(lastLap) {
    if (!lastLap) {
      ui.lapValid.textContent = "—";
      ui.lapValid.className = "lap-valid";
      return;
    }

    ui.lapValid.textContent = lastLap.valid ? "VALIDO" : "NON VALIDO";
    ui.lapValid.className =
      "lap-valid " + (lastLap.valid ? "" : "invalid");
  }

  function updateBestLap(bestLapTime) {
    ui.bestLapTime.textContent = formatTime(bestLapTime);

    ui.bestLapPanel.classList.toggle(
      "best-lap",
      bestLapTime !== null && bestLapTime !== undefined
    );
  }

  function bestStatus(value, bestValue) {
    if (
      value === null ||
      value === undefined ||
      bestValue === null ||
      bestValue === undefined
    ) {
      return "";
    }

    return Math.abs(
      Number(value) - Number(bestValue)
    ) <= 0.001
      ? "best"
      : "";
  }

  function setSectorHistoryElement(
    target,
    value,
    status
  ) {
    if (!target) {
      return;
    }

    target.textContent = formatTime(value);
    target.className =
      "sector-row-value " + (status || "");
  }

  function updateSectorHistory(session) {
    const values = Array.isArray(
      session.current_lap_sectors_s
    )
      ? session.current_lap_sectors_s.slice(0, 3)
      : [];

    const bestValues = Array.isArray(
      session.best_sector_times_s
    )
      ? session.best_sector_times_s.slice(0, 3)
      : [];

    while (values.length < 3) {
      values.push(null);
    }

    while (bestValues.length < 3) {
      bestValues.push(null);
    }

    const result =
      session.last_sector_result || null;

    const resultSector = result
      ? Number(result.sector_number)
      : null;

    const resultStatus = result
      ? result.status
      : "";

    const statusFor = (index) => {
      if (resultSector === index + 1) {
        return resultStatus;
      }

      return bestStatus(
        values[index],
        bestValues[index]
      );
    };

    setSectorHistoryElement(
      ui.sectorHistoryS1,
      values[0],
      statusFor(0)
    );

    setSectorHistoryElement(
      ui.sectorHistoryS2,
      values[1],
      statusFor(1)
    );

    setSectorHistoryElement(
      ui.sectorHistoryS3,
      values[2],
      statusFor(2)
    );
  }

  function showSectorResult(result) {
    const sectorNumber = Number(result.sector_number);
    const status = result.status || "neutral";

    ui.sectorId.textContent = "S" + sectorNumber;

    if (status === "best") {
      ui.sectorTime.textContent =
        formatSectorValue(result.sector_time_s);
      ui.sectorState.textContent = "NUOVO RIFERIMENTO";
    } else {
      ui.sectorTime.textContent =
        formatDelta(result.delta_s);
      ui.sectorState.textContent =
        status === "improved" ? "GUADAGNO" : "PERDITA";
    }

    ui.sectorCurrent.className =
      "sector-current result-" + status;
  }

  function showLiveSector(session) {
    const sectorNumber = Number(
      session.current_sector_number
    );

    const sectorElapsed =
      session.current_sector_elapsed_s;

    const timerRunning =
      session.current_lap_timer_running === true;

    if (
      timerRunning &&
      Number.isInteger(sectorNumber) &&
      sectorNumber >= 1 &&
      sectorNumber <= 3 &&
      sectorElapsed !== null &&
      sectorElapsed !== undefined &&
      Number.isFinite(Number(sectorElapsed))
    ) {
      ui.sectorId.textContent = "S" + sectorNumber;
      ui.sectorTime.textContent = formatTime(sectorElapsed);
      ui.sectorState.textContent = "TEMPO IN CORSO";
      ui.sectorCurrent.className = "sector-current";
      return;
    }

    ui.sectorId.textContent = "S1";
    ui.sectorTime.textContent = "—";

    if (session.track_state === "box") {
      ui.sectorState.textContent = "IN BOX";
    } else if (session.track_state === "pitlane") {
      ui.sectorState.textContent = "IN PIT LANE";
    } else if (session.track_state === "warmup") {
      ui.sectorState.textContent = "WARM-UP";
    } else {
      ui.sectorState.textContent = "NESSUN SETTORE";
    }

    ui.sectorCurrent.className = "sector-current";
  }

  function getResultKey(result) {
    if (!result) {
      return null;
    }

    return [
      result.sector_number,
      result.completed_at_epoch,
      result.sector_time_s,
      result.delta_s,
      result.status,
    ].join(":");
  }

  function updateSectorPanel(session) {
    const result =
      session.last_sector_result || null;

    const key = getResultKey(result);

    if (key !== visibleResultKey) {
      visibleResultKey = key;
      visibleResultUntil = key
        ? Date.now() + SECTOR_RESULT_SHOW_MS
        : 0;
    }

    if (
      result &&
      Date.now() < visibleResultUntil
    ) {
      showSectorResult(result);
      return;
    }

    showLiveSector(session);
  }

  function updateGps(gps, runtime) {
    const valid =
      gps && gps.fix_valid === true;

    const fixType = Number(
      gps && gps.fix_type ? gps.fix_type : 0
    );

    const fix = valid
      ? (fixType >= 3 ? "FIX 3D" : "FIX")
      : "NO FIX";

    const satellites =
      gps && gps.satellites_used != null
        ? gps.satellites_used
        : 0;

    const hdop =
      gps && gps.hdop != null
        ? Number(gps.hdop).toFixed(2)
        : "—";

    const age =
      runtime && runtime.data_age_s != null
        ? " | " +
          Number(runtime.data_age_s).toFixed(1) +
          "s"
        : "";

    ui.gpsMini.textContent =
      "GPS: " + fix + " · Sat: " + satellites + " | HDOP: " + hdop + age;
  }

  function updateButtons(status) {
    ui.startButton.disabled =
      status === "running" ||
      status === "paused";

    ui.pauseButton.disabled =
      status !== "running";

    ui.resumeButton.disabled =
      status !== "paused";

    ui.stopButton.disabled =
      status !== "running" &&
      status !== "paused";
  }

  function updateDashboard(data) {
    const gps = data.gps || {};
    const runtime = data.runtime || {};
    const session = data.session || {};
    const track = data.track || {};

    ui.trackName.textContent =
      track.name || "Pista non configurata";

    ui.driverName.textContent = session.driver
      ? "Pilota: " +
        (session.driver.name || "—")
      : "Pilota: —";

    const status = session.status || "idle";

    updateSessionState(status);

    ui.speedValue.textContent = Math.round(
      Number(gps.speed_kmph) || 0
    );

    ui.lapNumber.textContent =
      session.current_lap_number || 0;

    ui.currentLapTime.textContent =
      formatTime(
        session.display_lap_time_s != null
          ? session.display_lap_time_s
          : session.current_lap_time_s
      );

    // Ultimo giro
    const lastLap = session.last_lap || null;
    const lastLapTime =
      lastLap && lastLap.lap_time_s != null
        ? lastLap.lap_time_s
        : null;

    ui.lastLapTime.textContent = formatTime(lastLapTime);

    updateBestLap(session.best_lap_time_s);
    updateTrackState(session.track_state);
    updateLapValidity(lastLap);
    updateSectorHistory(session);
    updateSectorPanel(session);
    updateGps(gps, runtime);
    updateButtons(status);
  }

  async function fetchLive() {
    const response = await fetch("/api/live", {
      cache: "no-store",
      headers: {
        Accept: "application/json",
      },
    });

    if (!response.ok) {
      throw new Error("HTTP " + response.status);
    }

    return response.json();
  }

  async function refresh() {
    try {
      updateDashboard(await fetchLive());
    } catch (error) {
      console.error("Errore dashboard:", error);
    }
  }

  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: body
        ? { "Content-Type": "application/json" }
        : {},
      body: body
        ? JSON.stringify(body)
        : undefined,
    });

    const data = await response.json();

    if (!response.ok || data.ok === false) {
      throw new Error(
        data.error || "Errore HTTP " + response.status
      );
    }

    return data;
  }

  ui.startButton.addEventListener("click", async () => {
    if (!driverId) {
      alert("Pilota non selezionato.");
      return;
    }

    try {
      await post("/api/session/start", {
        driver_id: driverId,
      });

      await refresh();
    } catch (error) {
      alert("Errore avvio: " + error.message);
    }
  });

  ui.pauseButton.addEventListener("click", async () => {
    try {
      await post("/api/session/pause");
      await refresh();
    } catch (error) {
      alert("Errore pausa: " + error.message);
    }
  });

  ui.resumeButton.addEventListener("click", async () => {
    try {
      await post("/api/session/resume");
      await refresh();
    } catch (error) {
      alert("Errore ripresa: " + error.message);
    }
  });

  ui.stopButton.addEventListener("click", async () => {
    if (!window.confirm("Fermare la sessione?")) {
      return;
    }

    try {
      await post("/api/session/stop");
      await refresh();
    } catch (error) {
      alert("Errore stop: " + error.message);
    }
  });

  ui.backButton.addEventListener("click", () => {
    window.location.href = "/";
  });

  refresh();
  window.setInterval(refresh, 250);
})();
