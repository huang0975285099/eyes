const $ = (selector) => document.querySelector(selector);

const ui = {
  connectionPill: $("#connectionPill"),
  connectionText: $("#connectionText"),
  cameraFrame: $("#cameraFrame"),
  cameraPlaceholder: $("#cameraPlaceholder"),
  cameraError: $("#cameraError"),
  monitorToggle: $("#monitorToggle"),
  monitorToggleText: $("#monitorToggleText"),
  fpsValue: $("#fpsValue"),
  motionValue: $("#motionValue"),
  alertBanner: $("#alertBanner"),
  alertTime: $("#alertTime"),
  eventList: $("#eventList"),
  eventCount: $("#eventCount"),
  soundToggle: $("#soundToggle"),
  soundIcon: $("#soundIcon"),
  sensitivity: $("#sensitivity"),
  sensitivityValue: $("#sensitivityValue"),
  minArea: $("#minArea"),
  cameraIndex: $("#cameraIndex"),
  consecutiveFrames: $("#consecutiveFrames"),
  saveSnapshots: $("#saveSnapshots"),
  toast: $("#toast"),
};

let monitoring = false;
let soundEnabled = true;
let lastEventId = null;
let initialized = false;
let audioContext = null;
let polling = false;
let toastTimer = null;
let nativeNotifications = false;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function showToast(message) {
  ui.toast.textContent = message;
  ui.toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ui.toast.classList.remove("visible"), 2200);
}

function playAlert() {
  if (!soundEnabled) return;
  try {
    audioContext ||= new (window.AudioContext || window.webkitAudioContext)();
    const start = audioContext.currentTime;
    [0, 0.17, 0.34].forEach((delay, index) => {
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      oscillator.type = "sine";
      oscillator.frequency.value = index === 1 ? 720 : 880;
      gain.gain.setValueAtTime(0.0001, start + delay);
      gain.gain.exponentialRampToValueAtTime(0.18, start + delay + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + delay + 0.13);
      oscillator.connect(gain).connect(audioContext.destination);
      oscillator.start(start + delay);
      oscillator.stop(start + delay + 0.14);
    });
  } catch (_) {
    // Visual alert remains available if the browser blocks audio.
  }
}

function notify(event) {
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification("Visual change detected", { body: event.display_time, silent: true });
  }
}

function displayAlert(event) {
  ui.alertTime.textContent = `${event.display_time} · Change ${event.motion_score.toFixed(2)}%`;
  ui.alertBanner.classList.add("visible");
  ui.cameraFrame.classList.remove("alerting");
  void ui.cameraFrame.offsetWidth;
  ui.cameraFrame.classList.add("alerting");
  playAlert();
  if (!nativeNotifications) notify(event);
}

function renderEvents(events) {
  ui.eventCount.textContent = String(events.length);
  if (!events.length) {
    ui.eventList.innerHTML = `
      <div class="empty-state">
        <span class="empty-rings"></span>
        <strong>All quiet for now</strong>
        <small>Detected changes will appear here</small>
      </div>`;
    return;
  }
  ui.eventList.replaceChildren(...events.map((event) => {
    const item = document.createElement("div");
    item.className = "event-item";
    const visual = event.snapshot_url ? document.createElement("img") : document.createElement("div");
    visual.className = `event-thumb${event.snapshot_url ? "" : " blank"}`;
    if (event.snapshot_url) {
      visual.src = event.snapshot_url;
      visual.alt = "Alert snapshot";
    } else {
      visual.textContent = "!";
    }
    const copy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = event.display_time;
    const detail = document.createElement("small");
    detail.textContent = `Changed area ${Number(event.motion_score).toFixed(2)}%`;
    copy.append(title, detail);
    item.append(visual, copy);
    return item;
  }));
}

function setConnection(ok, error) {
  ui.connectionPill.classList.toggle("connected", ok);
  ui.connectionPill.classList.toggle("error", !ok);
  ui.connectionText.textContent = ok ? "Camera connected" : "Camera disconnected";
  ui.cameraPlaceholder.classList.toggle("hidden", ok);
  if (!ok) ui.cameraError.textContent = error || "Make sure the USB camera is connected";
}

function setMonitoring(enabled) {
  monitoring = enabled;
  ui.monitorToggle.classList.toggle("active", enabled);
  ui.cameraFrame.classList.toggle("monitoring", enabled);
  ui.monitorToggleText.textContent = enabled ? "Stop Change Detection" : "Start Change Detection";
}

function populateSettings(config) {
  ui.cameraIndex.value = config.camera_index;
  ui.sensitivity.value = config.sensitivity;
  ui.sensitivityValue.value = config.sensitivity;
  ui.sensitivityValue.textContent = config.sensitivity;
  ui.minArea.value = config.min_area_percent;
  ui.consecutiveFrames.value = config.consecutive_frames;
  ui.saveSnapshots.checked = config.save_snapshots;
}

async function pollStatus() {
  if (polling) return;
  polling = true;
  try {
    const status = await api("/api/status");
    nativeNotifications = Boolean(status.native_notifications);
    setConnection(status.camera_ok, status.error);
    setMonitoring(status.monitoring);
    ui.fpsValue.textContent = status.camera_ok ? status.fps.toFixed(1) : "--";
    ui.motionValue.textContent = `${status.motion_score.toFixed(2)}%`;
    renderEvents(status.events);
    if (!initialized) {
      populateSettings(status.config);
      lastEventId = status.latest_event?.id || null;
      initialized = true;
    } else if (status.latest_event && status.latest_event.id !== lastEventId) {
      lastEventId = status.latest_event.id;
      displayAlert(status.latest_event);
    }
  } catch (error) {
    setConnection(false, "Local service unavailable. Make sure the application is still running.");
  } finally {
    polling = false;
  }
}

ui.monitorToggle.addEventListener("click", async () => {
  try {
    if (!monitoring && !nativeNotifications && "Notification" in window && Notification.permission === "default") {
      Notification.requestPermission();
    }
    if (!monitoring && soundEnabled) {
      audioContext ||= new (window.AudioContext || window.webkitAudioContext)();
      await audioContext.resume();
    }
    const status = await api("/api/monitor", {
      method: "POST",
      body: JSON.stringify({ enabled: !monitoring }),
    });
    setMonitoring(status.monitoring);
    showToast(status.monitoring ? "Detection started. Learning the current scene." : "Detection stopped");
  } catch (error) {
    showToast(error.message);
  }
});

ui.soundToggle.addEventListener("click", () => {
  soundEnabled = !soundEnabled;
  ui.soundToggle.setAttribute("aria-pressed", String(soundEnabled));
  ui.soundIcon.textContent = soundEnabled ? "♪" : "×";
  showToast(soundEnabled ? "Sound alerts enabled" : "Sound alerts disabled");
});

$("#testNotification").addEventListener("click", async () => {
  const button = $("#testNotification");
  const originalText = "Test Notification";
  button.disabled = true;
  button.classList.remove("sent");
  button.textContent = "Sending...";
  try {
    const result = await api("/api/test-notification", { method: "POST", body: "{}" });
    if (!result.native) {
      if ("Notification" in window && Notification.permission === "default") {
        await Notification.requestPermission();
      }
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification("Sentinel test notification", { body: "Browser notifications are working." });
      } else {
        throw new Error("Notification permission was not granted");
      }
    }
    button.classList.add("sent");
    button.textContent = "Sent ✓";
    showToast("Test notification sent");
  } catch (error) {
    button.textContent = "Try Again";
    showToast(error.message);
  } finally {
    setTimeout(() => {
      button.disabled = false;
      button.classList.remove("sent");
      button.textContent = originalText;
    }, 1800);
  }
});

ui.sensitivity.addEventListener("input", () => {
  ui.sensitivityValue.value = ui.sensitivity.value;
  ui.sensitivityValue.textContent = ui.sensitivity.value;
});

$("#cameraMinus").addEventListener("click", () => {
  ui.cameraIndex.value = Math.max(0, Number(ui.cameraIndex.value) - 1);
});
$("#cameraPlus").addEventListener("click", () => {
  ui.cameraIndex.value = Math.min(9, Number(ui.cameraIndex.value) + 1);
});

$("#saveSettings").addEventListener("click", async () => {
  try {
    await api("/api/config", {
      method: "POST",
      body: JSON.stringify({
        camera_index: Number(ui.cameraIndex.value),
        sensitivity: Number(ui.sensitivity.value),
        min_area_percent: Number(ui.minArea.value),
        consecutive_frames: Number(ui.consecutiveFrames.value),
        save_snapshots: ui.saveSnapshots.checked,
      }),
    });
    showToast("Settings saved");
  } catch (error) {
    showToast(error.message);
  }
});

$("#dismissAlert").addEventListener("click", () => ui.alertBanner.classList.remove("visible"));

pollStatus();
setInterval(pollStatus, 700);
