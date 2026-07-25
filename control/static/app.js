let state = null;
let selectedMac = null;
let filter = "all";
let mapZoom = 1;
let mapPanX = 0;
let mapPanY = 0;
let pinchStartDistance = null;
let pinchStartZoom = 1;
let dragStart = null;
let movingLanternMac = null;
let movingDrag = null;
let replaceMode = false;
let replacementMac = null;
let patternDraft = null;
let powerBaseline = null;
let keepaliveBaseline = null;
let otaArtifact = null;
let otaInstall = null;
let savedPatterns = [];
let calibrationFrames = [];
let calibrationProposal = null;
let calibrationCodePlan = null;
let calibrationSaveStatus = "";
let wifiStatus = null;

const MAP_PADDING = 0.08;
const MIN_ZOOM = 1;
const MAX_ZOOM = 3;
const DEFAULT_TIMEZONE = "America/Los_Angeles";
const TIMEZONE_STORAGE_KEY = "baskets.sleepTimezone";
const PATTERN_DEFAULTS = {
  Pulse: { hue: 40, saturation: 100, value: 255, period: 4000, wavelength: 300, spatial: 0, scatter: 100, angle: 45 },
  Glow: { hue: 40, saturation: 100, value: 255, period: 4000, wavelength: 300, spatial: 0, scatter: 100, angle: 45 },
  Sweep: { hue: 40, saturation: 100, value: 255, period: 4000, wavelength: 300, spatial: 0, scatter: 100, angle: 45 },
  "Palette Drift": { hue: 40, saturation: 100, value: 255, period: 8000, wavelength: 300, spatial: 0, scatter: 100, angle: 45 },
  Firefly: { hue: 58, saturation: 85, value: 255, period: 7000, wavelength: 300, spatial: 0, scatter: 100, angle: 45 },
  "Ocean Wave": { hue: 205, saturation: 100, value: 255, period: 9000, wavelength: 100, spatial: 0, scatter: 100, angle: 45 },
};

const COLOR_VALUE_MARKER = 0x8000;
const FIREFLY_SCATTER_MASK = 0x007f;
const OCEAN_WAVELENGTH_MASK = 0x03ff;
const OCEAN_ANGLE_MASK = 0x01ff;

// Two hex colors are "the same" preset if every channel is within a couple of
// counts — HSV<->hex rounding can drift a preset's recomputed hex by 1, which
// would otherwise stop its swatch from highlighting when it is the active color.
function hexApproxEqual(a, b) {
  const ca = parseHexColor(a);
  const cb = parseHexColor(b);
  if (!ca || !cb) return false;
  return Math.abs(ca.r - cb.r) <= 2 && Math.abs(ca.g - cb.g) <= 2 && Math.abs(ca.b - cb.b) <= 2;
}

function parseHexColor(input) {
  const trimmed = (input || "").trim().replace(/^#/, "");
  let expanded = trimmed;
  if (/^[0-9a-fA-F]{3}$/.test(trimmed)) {
    expanded = trimmed.split("").map((c) => c + c).join("");
  }
  if (!/^[0-9a-fA-F]{6}$/.test(expanded)) return null;
  return {
    r: parseInt(expanded.slice(0, 2), 16),
    g: parseInt(expanded.slice(2, 4), 16),
    b: parseInt(expanded.slice(4, 6), 16),
  };
}

function rgbToHueSaturationValue(r, g, b) {
  const rf = r / 255, gf = g / 255, bf = b / 255;
  const max = Math.max(rf, gf, bf);
  const min = Math.min(rf, gf, bf);
  const delta = max - min;
  let hue = 0;
  if (delta !== 0) {
    if (max === rf) hue = ((gf - bf) / delta) % 6;
    else if (max === gf) hue = (bf - rf) / delta + 2;
    else hue = (rf - gf) / delta + 4;
    hue *= 60;
    if (hue < 0) hue += 360;
  }
  const saturation = max === 0 ? 0 : (delta / max) * 100;
  return {
    hue: Math.round(hue) % 360,
    saturation: Math.round(saturation),
    value: Math.round(max * 255),
  };
}

function hueSaturationValueToHex(hue, saturation, value) {
  const h = ((Number(hue) % 360) + 360) % 360;
  const s = Math.min(100, Math.max(0, Number(saturation))) / 100;
  const v = Math.min(255, Math.max(0, Number(value))) / 255;
  const hf = h / 60;
  const c = v * s;
  const x = c * (1 - Math.abs((hf % 2) - 1));
  let r = 0, g = 0, b = 0;
  if (hf < 1) [r, g, b] = [c, x, 0];
  else if (hf < 2) [r, g, b] = [x, c, 0];
  else if (hf < 3) [r, g, b] = [0, c, x];
  else if (hf < 4) [r, g, b] = [0, x, c];
  else if (hf < 5) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];
  const m = v - c;
  const toHex = (v) => Math.round((v + m) * 255).toString(16).padStart(2, "0");
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}

function colorValuePack(value) {
  return COLOR_VALUE_MARKER | Math.min(255, Math.max(0, Math.round(Number(value))));
}

function colorValuePresent(packed) {
  return (Number(packed || 0) & COLOR_VALUE_MARKER) !== 0;
}

function fireflyMetaPack(scatter, value) {
  const safeScatter = Math.min(100, Math.max(0, Math.round(Number(scatter))));
  const safeValue = Math.min(255, Math.max(0, Math.round(Number(value))));
  return COLOR_VALUE_MARKER | (safeValue << 7) | safeScatter;
}

function oceanWavelengthSaturationPack(wavelength, saturation) {
  const safeWavelength = Math.min(OCEAN_WAVELENGTH_MASK, Math.max(0, Math.round(Number(wavelength))));
  const safeSaturation = Math.min(100, Math.max(0, Number(saturation)));
  const sat6 = Math.round(safeSaturation * 63 / 100);
  return safeWavelength | (sat6 << 10);
}

function oceanAngleValuePack(angle, value) {
  const safeAngle = Math.round(Number(angle)) & OCEAN_ANGLE_MASK;
  const safeValue = Math.min(255, Math.max(0, Number(value)));
  const value6 = Math.round(safeValue * 63 / 255);
  return COLOR_VALUE_MARKER | (value6 << 9) | safeAngle;
}

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(errorMessage(body.detail || response.statusText));
  }
  return response.json();
}

async function apiBinary(path, data) {
  const response = await fetch(path, {
    method: "PUT",
    headers: { "Content-Type": "application/octet-stream" },
    body: data,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(errorMessage(body.detail || response.statusText));
  }
  return response.json();
}

function lanterns() {
  return state?.lanterns || [];
}

function lanternDisplayName(mac) {
  const lantern = lanterns().find((item) => item.mac === mac);
  if (lantern?.label && lantern.label !== "Unknown") return lantern.label;
  return String(mac || "").split(":").slice(-2).join(":") || "node";
}

function selectedLantern() {
  return lanterns().find((lantern) => lantern.mac === selectedMac) || lanterns()[0] || null;
}

function isPositioned(lantern) {
  if (!lantern) return false;
  return lantern.x !== null && lantern.x !== undefined && lantern.y !== null && lantern.y !== undefined;
}

function replacementCandidates() {
  return lanterns().filter((lantern) => lantern.mac !== selectedMac && lantern.status === "alive" && !isPositioned(lantern));
}

function statusText(lantern) {
  if (lantern.status === "retired") return "retired";
  if (lantern.status === "missing") return "missing";
  if (lantern.position === "Missing") return "needs position";
  return "healthy";
}

function cssStatus(lantern) {
  if (lantern.status === "retired") return "retired";
  if (lantern.status === "missing") return "missing";
  if (lantern.attention === "Firmware mismatch") return "mismatch";
  if (lantern.position === "Missing") return "unpositioned";
  return "";
}

function firmwareLabel(firmware) {
  if (!firmware) return "unknown";
  const dirty = firmware.dirty ? " dirty" : "";
  const version = firmware.version ? `v${firmware.version}` : "version unknown";
  const build = firmware.build_label || String(firmware.build_id || "unknown");
  return `${version} (${build} / p${firmware.proto}${dirty})`;
}

function commitUrl(buildLabel) {
  if (!/^[0-9a-f]{7,40}$/i.test(buildLabel || "")) return null;
  return `https://github.com/underminedsk/lightweave/commit/${buildLabel}`;
}

function firmwareHtml(firmware) {
  if (!firmware) return "unknown";
  const dirty = firmware.dirty ? " dirty" : "";
  const version = firmware.version ? `v${escapeHtml(firmware.version)}` : "version unknown";
  const build = firmware.build_label || String(firmware.build_id || "unknown");
  const url = commitUrl(build);
  const hash = url
    ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${escapeHtml(build)}</a>`
    : escapeHtml(build);
  return `${version} <span class="firmware-hash">${hash}</span> <span class="muted-inline">p${escapeHtml(String(firmware.proto))}${dirty}</span>`;
}

function shortHash(hash) {
  return String(hash || "").slice(0, 12);
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(2)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds || 0)));
  const minutes = Math.floor(total / 60);
  const remaining = total % 60;
  if (minutes <= 0) return `${remaining}s`;
  return `${minutes}m ${remaining}s`;
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function render() {
  if (!state) return;
  if (!selectedMac && lanterns().length) selectedMac = lanterns()[0].mac;
  if (!patternDraft || !isPatternDirty()) patternDraft = patternDraftFromState();

  $("#connection-status").textContent = state.conductor.connected ? "connected" : "disconnected";
  $("#field-count").textContent = `${state.summary.alive} / ${state.summary.total}`;
  $("#show-name").textContent = state.pattern.pattern;
  $("#attention-count").textContent = `${state.summary.attention} lights`;
  $("#sync-status").textContent = `sync ${state.conductor.sync}`;
  $("#table-sync-status").textContent = `sync ${state.conductor.sync}`;
  $("#brightness").value = patternDraft.brightness;
  $("#brightness-value").textContent = patternDraft.brightness;

  renderPatternControls();
  renderSavedPatterns();
  renderMap();
  renderUnpositionedTray();
  renderRows();
  renderDetail();
  renderFirmware();
  renderRecovery();
  renderWifi();
  renderPowerMonitor();
  renderOta();
  renderCalibration();
  renderPowerPolicy();
  renderKeepalive();
  renderEvents();
  renderDetailVisibility();
}

function patternHueFromState() {
  const params = state.pattern.params || {};
  // Firefly is positional: hue lives in p1 (p0 is the period).
  if (state.pattern.pattern === "Firefly") {
    return params.p1 !== undefined ? Number(params.p1) : PATTERN_DEFAULTS.Firefly.hue;
  }
  // Ocean Wave is positional: base water hue lives in p3.
  if (state.pattern.pattern === "Ocean Wave") {
    return params.p3 !== undefined ? Number(params.p3) : PATTERN_DEFAULTS["Ocean Wave"].hue;
  }
  if (params.hue !== undefined) return Number(params.hue);
  if ((state.pattern.pattern === "Glow" || state.pattern.pattern === "Pulse") && params.p0 !== undefined) {
    return Number(params.p0);
  }
  return 40;
}

function patternSaturationFromState() {
  const params = state.pattern.params || {};
  if (state.pattern.pattern === "Firefly") {
    if (colorValuePresent(params.p2) && params.p3 !== undefined) return Math.min(100, Number(params.p3));
    return params.p3 !== undefined ? Number(params.p3) : PATTERN_DEFAULTS.Firefly.saturation;
  }
  if (state.pattern.pattern === "Ocean Wave" && colorValuePresent(params.p2)) {
    return Math.round(((Number(params.p1) >> 10) & 0x3f) * 100 / 63);
  }
  if ((state.pattern.pattern === "Glow" || state.pattern.pattern === "Pulse") &&
      colorValuePresent(params.p2) && params.p1 !== undefined) {
    return Math.min(100, Number(params.p1));
  }
  if (params.saturation !== undefined) return Number(params.saturation);
  if ((state.pattern.pattern === "Glow" || state.pattern.pattern === "Pulse") && params.p1 !== undefined) {
    return Number(params.p1);
  }
  return 100;
}

function patternValueFromState() {
  const params = state.pattern.params || {};
  if (state.pattern.pattern === "Firefly" && colorValuePresent(params.p2)) {
    return (Number(params.p2) >> 7) & 0xff;
  }
  if (state.pattern.pattern === "Ocean Wave" && colorValuePresent(params.p2)) {
    return Math.round(((Number(params.p2) >> 9) & 0x3f) * 255 / 63);
  }
  if ((state.pattern.pattern === "Glow" || state.pattern.pattern === "Pulse") &&
      colorValuePresent(params.p2)) {
    return Number(params.p2) & 0xff;
  }
  return 255;
}

function patternPeriodFromState() {
  const params = state.pattern.params || {};
  if (params.period !== undefined) return Number(params.period);
  if ((state.pattern.pattern === "Sweep" || state.pattern.pattern === "Palette Drift" || state.pattern.pattern === "Firefly" || state.pattern.pattern === "Ocean Wave") && params.p0 !== undefined) {
    return Number(params.p0);
  }
  return PATTERN_DEFAULTS[state.pattern.pattern]?.period || 4000;
}

function patternScatterFromState() {
  const params = state.pattern.params || {};
  if (state.pattern.pattern === "Firefly" && params.p2 !== undefined) {
    if (colorValuePresent(params.p2)) return Number(params.p2) & FIREFLY_SCATTER_MASK;
    return Number(params.p2);
  }
  return PATTERN_DEFAULTS.Firefly.scatter;
}

function patternAngleFromState() {
  const params = state.pattern.params || {};
  if (state.pattern.pattern === "Ocean Wave" && params.p2 !== undefined) {
    return colorValuePresent(params.p2) ? Number(params.p2) & OCEAN_ANGLE_MASK : Number(params.p2);
  }
  return PATTERN_DEFAULTS["Ocean Wave"].angle;
}

function patternWavelengthFromState() {
  const params = state.pattern.params || {};
  if (params.wavelength !== undefined) return Number(params.wavelength);
  if ((state.pattern.pattern === "Sweep" || state.pattern.pattern === "Ocean Wave") && params.p1 !== undefined) {
    if (state.pattern.pattern === "Ocean Wave" && colorValuePresent(params.p2)) {
      return Number(params.p1) & OCEAN_WAVELENGTH_MASK;
    }
    return Number(params.p1);
  }
  return PATTERN_DEFAULTS[state.pattern.pattern]?.wavelength ?? PATTERN_DEFAULTS.Sweep.wavelength;
}

function patternSpatialFromState() {
  const params = state.pattern.params || {};
  if (params.spatial !== undefined) return Number(params.spatial);
  if (state.pattern.pattern === "Palette Drift" && params.p1 !== undefined) return Number(params.p1);
  return PATTERN_DEFAULTS["Palette Drift"].spatial;
}

function patternDraftFromState() {
  const defaults = PATTERN_DEFAULTS[state.pattern.pattern] || PATTERN_DEFAULTS.Pulse;
  return {
    pattern: state.pattern.pattern,
    brightness: Number(state.pattern.brightness),
    hue: patternHueFromState(),
    saturation: patternSaturationFromState(),
    value: patternValueFromState(),
    period: patternPeriodFromState() || defaults.period,
    wavelength: patternWavelengthFromState() || defaults.wavelength,
    spatial: patternSpatialFromState(),
    scatter: patternScatterFromState(),
    angle: patternAngleFromState(),
  };
}

function patternDraftForSelection(pattern) {
  const defaults = PATTERN_DEFAULTS[pattern] || PATTERN_DEFAULTS.Pulse;
  return {
    pattern,
    brightness: Number(patternDraft?.brightness ?? state?.pattern?.brightness ?? 48),
    hue: Number(defaults.hue),
    saturation: Number(defaults.saturation),
    value: Number(defaults.value),
    period: Number(defaults.period),
    wavelength: Number(defaults.wavelength),
    spatial: Number(defaults.spatial),
    scatter: Number(defaults.scatter),
    angle: Number(defaults.angle),
  };
}

function patternParams(draft) {
  if (draft.pattern === "Pulse" || draft.pattern === "Glow") {
    return {
      p0: Number(draft.hue),
      p1: Number(draft.saturation ?? 100),
      p2: colorValuePack(draft.value ?? 255),
      p3: 0,
    };
  }
  if (draft.pattern === "Sweep") {
    return { period: Number(draft.period), spatial: Number(draft.wavelength) };
  }
  if (draft.pattern === "Palette Drift") {
    return { period: Number(draft.period), spatial: Number(draft.spatial) };
  }
  if (draft.pattern === "Firefly") {
    // Positional on the wire; p2 packs value with scatter to keep four params.
    // (The hue/period aliases would both land on params[0], so send indices.)
    return {
      p0: Number(draft.period),
      p1: Number(draft.hue),
      p2: fireflyMetaPack(draft.scatter ?? 100, draft.value ?? 255),
      p3: Number(draft.saturation ?? 85),
    };
  }
  if (draft.pattern === "Ocean Wave") {
    // p1/p2 pack saturation/value above wavelength/angle to keep four params.
    return {
      p0: Number(draft.period),
      p1: oceanWavelengthSaturationPack(draft.wavelength, draft.saturation ?? 100),
      p2: oceanAngleValuePack(draft.angle ?? 45, draft.value ?? 255),
      p3: Number(draft.hue),
    };
  }
  return {};
}

function patternStateParams(draft) {
  const params = patternParams(draft);
  return {
    p0: Number(params.hue ?? params.period ?? 0),
    p1: Number(params.saturation ?? params.spatial ?? 0),
    p2: 0,
    p3: 0,
    ...params,
  };
}

function relevantPatternFields(pattern) {
  if (pattern === "Pulse" || pattern === "Glow") return ["pattern", "brightness", "hue", "saturation", "value"];
  if (pattern === "Sweep") return ["pattern", "brightness", "period", "wavelength"];
  if (pattern === "Palette Drift") return ["pattern", "brightness", "period", "spatial"];
  if (pattern === "Firefly") return ["pattern", "brightness", "period", "hue", "saturation", "value", "scatter"];
  if (pattern === "Ocean Wave") return ["pattern", "brightness", "period", "wavelength", "angle", "hue", "saturation", "value"];
  return ["pattern", "brightness"];
}

function isPatternDirty() {
  if (!state || !patternDraft) return false;
  const live = patternDraftFromState();
  return relevantPatternFields(patternDraft.pattern).some((field) => {
    if (field === "pattern") return patternDraft.pattern !== live.pattern;
    return Number(patternDraft[field]) !== Number(live[field]);
  });
}

function renderPatternControls() {
  $$("#pattern-picker button").forEach((button) => {
    button.classList.toggle("active", button.dataset.pattern === patternDraft.pattern);
  });
  $("#pattern-period").value = patternDraft.period;
  $("#period-value").textContent = (Number(patternDraft.period) / 1000).toFixed(1);
  $("#pattern-wavelength").value = patternDraft.wavelength;
  $("#wavelength-value").textContent = (Number(patternDraft.wavelength) / 100).toFixed(1);
  $("#pattern-spatial").value = patternDraft.spatial;
  $("#spatial-value").textContent = (Number(patternDraft.spatial) / 100).toFixed(2);
  $("#pattern-scatter").value = patternDraft.scatter ?? 100;
  $("#scatter-value").textContent = String(Number(patternDraft.scatter ?? 100));
  $("#pattern-angle").value = patternDraft.angle ?? 45;
  $("#angle-value").textContent = String(Number(patternDraft.angle ?? 45));
  const draftHex = hueSaturationValueToHex(
    patternDraft.hue,
    patternDraft.saturation ?? 100,
    patternDraft.value ?? 255,
  );
  $$("#color-presets button").forEach((button) => {
    button.classList.toggle("active", hexApproxEqual(button.dataset.hex, draftHex));
  });
  const isColorPattern = patternDraft.pattern === "Pulse" || patternDraft.pattern === "Glow" || patternDraft.pattern === "Firefly" || patternDraft.pattern === "Ocean Wave";
  $("#color-presets").hidden = !isColorPattern;
  $("#hex-color-row").hidden = !isColorPattern;
  if (isColorPattern) {
    const hex = hueSaturationValueToHex(
      patternDraft.hue,
      patternDraft.saturation ?? 100,
      patternDraft.value ?? 255,
    );
    if (document.activeElement !== $("#pattern-hex")) $("#pattern-hex").value = hex;
    $("#pattern-hex").classList.remove("invalid");
    $("#pattern-color-picker").value = hex;
    $("#color-swatch").style.backgroundColor = hex;
    $("#hex-error").hidden = true;
  }
  $('[data-param-group="period"]').hidden = !(patternDraft.pattern === "Sweep" || patternDraft.pattern === "Palette Drift" || patternDraft.pattern === "Firefly" || patternDraft.pattern === "Ocean Wave");
  $('[data-param-group="wavelength"]').hidden = !(patternDraft.pattern === "Sweep" || patternDraft.pattern === "Ocean Wave");
  $('[data-param-group="spatial"]').hidden = patternDraft.pattern !== "Palette Drift";
  $('[data-param-group="scatter"]').hidden = patternDraft.pattern !== "Firefly";
  $('[data-param-group="angle"]').hidden = patternDraft.pattern !== "Ocean Wave";
  const changeButton = $('[data-action="broadcast"]');
  changeButton.disabled = !isPatternDirty();
  changeButton.ariaDisabled = String(changeButton.disabled);
}

function renderSavedPatterns() {
  const list = $("#saved-pattern-list");
  const count = $("#saved-pattern-count");
  if (!list || !count) return;
  count.textContent = `${savedPatterns.length} saved`;
  if (!savedPatterns.length) {
    list.innerHTML = '<div class="empty-state">No saved patterns yet.</div>';
    return;
  }
  list.innerHTML = savedPatterns.map((item) => {
    const details = `${escapeHtml(item.pattern)} · bri ${escapeHtml(String(item.brightness))}`;
    const params = Object.entries(item.params || {})
      .map(([key, value]) => `${key}=${value}`)
      .join(" ");
    const id = escapeHtml(item.id);
    return `
      <div class="saved-pattern-row">
        <div>
          <strong>${escapeHtml(item.name)}</strong>
          <span>${details}</span>
          <small>${escapeHtml(params || "default params")}</small>
        </div>
        <a class="button-link" href="/api/patterns/${encodeURIComponent(item.id)}/preview" target="_blank" rel="noopener noreferrer">Preview</a>
        <a class="button-link" href="/api/patterns/${encodeURIComponent(item.id)}/preview/frames.json" target="_blank" rel="noopener noreferrer">Frames</a>
        <a class="button-link" href="/api/patterns/${encodeURIComponent(item.id)}/review" target="_blank" rel="noopener noreferrer">Review</a>
        <button class="primary" data-pattern-action="broadcast-saved" data-pattern-id="${id}">Broadcast</button>
        <button class="danger" data-pattern-action="delete-saved" data-pattern-id="${id}">Delete</button>
      </div>
    `;
  }).join("");
}

function renderMap() {
  const map = $("#map-content");
  $$(".node").forEach((node) => node.remove());
  $$(".selection-ring").forEach((ring) => ring.remove());
  lanterns().filter(isPositioned).forEach((lantern) => {
    const button = document.createElement("button");
    button.className = `node ${cssStatus(lantern)}`;
    if (movingLanternMac === lantern.mac) button.classList.add("move-target");
    button.dataset.mac = lantern.mac;
    button.type = "button";
    button.ariaLabel = lantern.label;
    button.style.left = `${mapCoord(lantern.x) * 100}%`;
    button.style.top = `${mapCoord(lantern.y) * 100}%`;
    button.addEventListener("click", () => selectLantern(lantern.mac));
    button.addEventListener("pointerdown", startLanternMove);
    button.addEventListener("mousedown", startLanternMove);
    map.appendChild(button);
  });
  ensureSelectionRing();
  renderMapZoom();
}

function renderUnpositionedTray() {
  const tray = $("#unpositioned-tray");
  const unpositioned = lanterns().filter((lantern) => !isPositioned(lantern));
  if (!unpositioned.length) {
    tray.hidden = true;
    tray.innerHTML = "";
    return;
  }
  tray.hidden = false;
  tray.innerHTML = [
    `<span class="tray-label">Unpositioned</span>`,
    ...unpositioned.map((lantern) => `<button type="button" class="tray-node ${lantern.mac === selectedMac ? "selected" : ""}" data-mac="${escapeHtml(lantern.mac)}">
      <span class="dot ${lantern.status === "missing" || lantern.status === "retired" ? "bad" : "warn"}"></span>
      <span>${escapeHtml(lantern.label)}</span>
    </button>`),
  ].join("");
  $$("#unpositioned-tray .tray-node").forEach((button) => {
    button.addEventListener("click", () => selectLantern(button.dataset.mac));
  });
}

function renderRows() {
  const rows = lanterns().filter((lantern) => {
    if (filter === "attention") return lantern.attention !== "None";
    if (filter === "missing") return lantern.status === "missing";
    if (filter === "unpositioned") return lantern.position === "Missing";
    return true;
  });

  $("#lantern-rows").innerHTML = rows.map((lantern) => {
    const isBad = lantern.status === "missing" || lantern.status === "retired";
    const isFirmwareBad = lantern.attention === "Firmware mismatch";
    const dotClass = isBad || isFirmwareBad ? "bad" : lantern.position === "Missing" ? "warn" : "";
    const attentionClass = lantern.attention === "None" ? "" : isBad || isFirmwareBad ? "bad" : "warn";
    return `<tr data-mac="${lantern.mac}" class="${lantern.mac === selectedMac ? "selected" : ""}">
      <td><strong>${escapeHtml(lantern.label)}</strong><br><span class="mono">${escapeHtml(lantern.mac)}</span></td>
      <td><span class="status"><span class="dot ${dotClass}"></span>${statusText(lantern)}</span></td>
      <td class="${isBad ? "bad" : "ok"}">${escapeHtml(lantern.last_seen_label)}</td>
      <td class="${lantern.position === "Missing" ? "warn" : ""}">${escapeHtml(lantern.position)}</td>
      <td class="${attentionClass}">${escapeHtml(lantern.attention)}</td>
      <td><button type="button" class="table-action" data-locate-mac="${escapeHtml(lantern.mac)}">Locate</button></td>
    </tr>`;
  }).join("");

  $$("#lantern-rows tr").forEach((row) => {
    row.addEventListener("click", () => selectLantern(row.dataset.mac));
  });
  $$("#lantern-rows [data-locate-mac]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      selectLantern(button.dataset.locateMac);
      await locateLantern(button.dataset.locateMac);
    });
  });
}

function renderDetail() {
  const lantern = selectedLantern();
  if (!lantern) return;
  const isOk = lantern.status === "alive" && lantern.position !== "Missing";
  const moveLabel = isPositioned(lantern) ? "Move" : "Place";
  $("#detail-title").textContent = `${lantern.label} is ${isOk ? "healthy" : statusText(lantern)}`;
  $("#detail-title").className = isOk ? "" : "warn";
  $("#detail-summary").textContent = detailSummary(lantern);
  $("#detail-tech").innerHTML = [
    `MAC ${escapeHtml(lantern.mac)} · x=${fmt(lantern.x)} y=${fmt(lantern.y)} · status=${escapeHtml(statusText(lantern))}`,
    `firmware=${firmwareHtml(lantern.firmware)}`,
    `pattern=${escapeHtml(state.pattern.pattern)} bri=${state.pattern.brightness} · seq=${state.conductor.seq}`,
    `power E=${fmt(lantern.power.wh)}Wh avg=${fmt(lantern.power.avg_w)}W · last report=${escapeHtml(lantern.power.last_report_label || "none")}`,
  ].join("<br>");
  $$('[data-action="move"]').forEach((button) => {
    button.textContent = moveLabel;
  });
  document.body.classList.toggle("move-mode", movingLanternMac !== null);
  renderReplacePanel();
  renderSelectionRing();
  renderPlacementMarker();
}

function renderDetailVisibility() {
  const activeView = $(".tabs button.active")?.dataset.view;
  $("#detail-sheet").hidden = !(activeView === "map" || activeView === "table");
}

function renderFirmware() {
  const summary = state.summary.firmware || {};
  const conductorFirmware = state.conductor.firmware || {};
  const firmware = {
    version: summary.version || conductorFirmware.version,
    build_label: summary.build_label || conductorFirmware.build_label,
    build_id: conductorFirmware.build_id,
    proto: conductorFirmware.proto,
    dirty: summary.dirty ?? conductorFirmware.dirty,
  };
  const dirty = summary.dirty || conductorFirmware.dirty ? " dirty" : "";
  $("#firmware-build").innerHTML = firmwareHtml(firmware);
  $("#firmware-build").className = `ops-value ${dirty ? "warn" : ""}`;
  const expected = summary.expected ?? state.summary.total;
  const matching = summary.matching ?? 0;
  const seen = summary.seen ?? 0;
  const consistent = summary.consistent !== false;
  $("#firmware-consistency").textContent = consistent
    ? `${matching} / ${expected} on this build`
    : `${matching} / ${seen} match`;
  $("#firmware-consistency").className = `ops-value ${consistent ? "ok" : "bad"}`;
}

function renderRecovery() {
  const recovery = effectiveRecovery();
  const status = recovery.status || "ready";
  const ready = recovery.ready !== false && status === "ready";
  $("#recovery-status").textContent = ready ? "ready" : "action needed";
  $("#recovery-status").className = `chip ${ready ? "sync" : "active"}`;
  $("#recovery-title").textContent = recovery.title || "No recovery needed";
  $("#recovery-title").className = `recovery-title ${ready ? "ok" : "warn"}`;
  $("#recovery-action").textContent = recovery.action || "Field firmware is consistent and all placed lanterns are healthy.";

  const rows = [
    ...(recovery.failed_ota || []).map((item) => ({ ...item, kind: "OTA" })),
    ...(recovery.mismatched || []).map((item) => ({ ...item, kind: "Firmware" })),
    ...(recovery.missing || []).map((item) => ({ ...item, kind: "Missing" })),
  ];
  const list = $("#recovery-list");
  list.hidden = rows.length === 0;
  if (list.hidden) {
    list.innerHTML = "";
    return;
  }
  list.innerHTML = rows.map((item) => `<div class="recovery-row">
    <span>${escapeHtml(item.kind)}</span>
    <strong>${escapeHtml(item.label || item.mac || "node")}</strong>
    <span class="mono">${escapeHtml(item.mac || "")}</span>
    <span>${escapeHtml(item.reason || "")}</span>
  </div>`).join("");
}

function renderWifi() {
  const wifi = wifiStatus || {};
  const status = $("#wifi-status");
  if (!status) return;
  const available = wifi.available !== false;
  const connected = wifi.state === "connected";
  status.textContent = available ? (connected ? "connected" : wifi.state || "idle") : "unavailable";
  status.className = `chip ${connected ? "sync" : available ? "warn" : ""}`;
  $("#wifi-connection").textContent = wifi.connection || wifi.error || "--";
  $("#wifi-address").textContent = (wifi.addresses || []).join(", ") || "--";
}

function renderPowerMonitor() {
  const monitor = state.power_monitor || {};
  const samples = Array.isArray(monitor.samples) ? monitor.samples : [];
  const usable = Number(monitor.usable_sample_count || 0);
  const sampleCount = Number(monitor.sample_count || 0);
  const soc = monitor.estimated_node_soc_percent;
  const fieldDraw = monitor.estimated_field_avg_w;
  const stale = Number(monitor.stale_count || 0);
  const bad = Number(monitor.implausible_count || 0);
  $("#power-monitor-status").textContent = sampleCount
    ? `${usable} / ${sampleCount} samples`
    : "no samples";
  $("#power-monitor-status").className = `chip ${usable ? "sync" : ""}`;
  $("#power-monitor-soc").textContent = soc === null || soc === undefined
    ? "--"
    : `${Number(soc).toFixed(1)}%`;
  $("#power-monitor-soc").className = `ops-value ${soc === null || soc === undefined ? "" : soc < 25 ? "bad" : soc < 50 ? "warn" : "ok"}`;
  $("#power-monitor-draw").textContent = fieldDraw === null || fieldDraw === undefined
    ? "--"
    : `${Number(fieldDraw).toFixed(1)} W`;
  $("#power-monitor-draw").className = `ops-value ${fieldDraw === null || fieldDraw === undefined ? "" : "sync"}`;
  $("#battery-capacity").value = monitor.battery_capacity_wh ?? 153.6;
  $("#battery-full-voltage").value = monitor.full_voltage ?? 14.6;

  const sampleBox = $("#power-samples");
  if (!samples.length) {
    sampleBox.innerHTML = `<div class="empty-state">No instrumented node has reported power yet.</div>`;
    return;
  }
  sampleBox.innerHTML = samples.map((sample) => {
    const classes = [sample.stale ? "warn" : "", sample.plausible === false ? "bad" : ""].filter(Boolean).join(" ");
    const socLabel = sample.soc_percent === null || sample.soc_percent === undefined ? "--" : `${Number(sample.soc_percent).toFixed(1)}%`;
    const voltage = sample.bus_v === null || sample.bus_v === undefined ? "--" : `${Number(sample.bus_v).toFixed(2)} V`;
    const detail = `${fmt(sample.used_since_full_wh)} Wh used · ${fmt(sample.avg_w)} W avg · ${voltage}`;
    return `<div class="power-sample-row ${classes}">
      <span><strong>${escapeHtml(sample.label || sample.mac || "node")}</strong><small class="mono">${escapeHtml(sample.mac || "")}</small></span>
      <span>${escapeHtml(socLabel)}</span>
      <span>${escapeHtml(detail)}</span>
      <span>${escapeHtml(sample.last_report_label || "no report age")}</span>
      <button type="button" data-power-sync="${escapeHtml(sample.mac || "")}">Sync to 100%</button>
    </div>`;
  }).join("");
  if (stale || bad) {
    sampleBox.insertAdjacentHTML("afterbegin", `<div class="power-warning">${stale ? `${stale} stale ` : ""}${bad ? `${bad} implausible ` : ""}sample${stale + bad === 1 ? "" : "s"} excluded from estimates.</div>`);
  }
  $$("[data-power-sync]").forEach((button) => {
    button.addEventListener("click", async () => {
      const mac = button.dataset.powerSync;
      if (!mac || !confirm("Sync this meter to 100% at its current reading?")) return;
      const ack = await api(`/api/lanterns/${encodeURIComponent(mac)}/power-sync-full`, { method: "POST" });
      toast(ack.message);
      await refresh();
    });
  });
}

function effectiveRecovery() {
  if (otaInstall?.error) {
    const failed = Array.isArray(otaInstall.nodes)
      ? otaInstall.nodes.filter((node) => node.phase === "failed")
      : [];
    return {
      status: "ota_failed",
      ready: false,
      title: "Firmware update needs recovery",
      action: "Exit maintenance mode, enter it again, wait for readiness, then rerun the same staged firmware. Power-cycle any listed lantern that does not check back in.",
      missing: [],
      mismatched: [],
      failed_ota: failed.length
        ? failed.map((node) => ({
          mac: node.mac,
          label: lanterns().find((item) => item.mac === node.mac)?.label || node.mac || "node",
          reason: node.error || otaInstall.error,
          phase: node.phase,
        }))
        : [{ mac: "", label: "Field update", reason: otaInstall.error, phase: "failed" }],
    };
  }
  return state.recovery || {};
}

function renderOta() {
  const ota = state.ota || {};
  const active = Boolean(ota.enabled);
  const ready = Boolean(ota.ready);
  const installing = Boolean(otaInstall?.running);
  const expected = Number(ota.expected ?? state.summary.total ?? 0);
  const readyCount = Number(ota.ready_count ?? 0);
  const timeout = Number(ota.timeout_s ?? 0);
  const blockers = Array.isArray(ota.blocked) ? ota.blocked : [];
  $("#ota-mode").textContent = active ? "maintenance" : "idle";
  $("#ota-mode").className = `chip ${active ? "sync" : ""}`;
  $("#ota-readiness").textContent = ready ? `${readyCount} / ${expected} ready` : `${readyCount} / ${expected} ready`;
  $("#ota-readiness").className = `ops-value ${ready ? "ok" : "warn"}`;
  $("#ota-timeout").textContent = active ? `${Math.max(0, Math.floor(timeout / 60))}m ${timeout % 60}s` : "closed";
  $("#ota-timeout").className = `ops-value ${active ? "ok" : ""}`;
  $("#ota-blockers").textContent = blockers.length
    ? `Blocked: ${blockers.join(", ")}.`
    : "Ready for the next firmware upload step.";
  $("#ota-artifact").innerHTML = otaArtifact
    ? `Staged ${escapeHtml(otaArtifact.filename)} · ${formatBytes(otaArtifact.size)} · ${otaArtifact.chunks} chunks · sha256 <span class="mono">${escapeHtml(shortHash(otaArtifact.sha256))}</span>`
    : "No firmware staged.";
  renderOtaProgress();
  renderOtaNodes();
  const fileInput = $("#ota-file");
  $('[data-action="stage-ota-artifact"]').disabled = installing || !fileInput?.files?.length;
  $('[data-action="enter-ota"]').disabled = installing || active;
  $('[data-action="install-ota"]').disabled = installing || !otaReadyForInstall() || !otaArtifact;
  $('[data-action="exit-ota"]').disabled = installing || !active;
}

function otaReadyForInstall() {
  const ota = state?.ota || {};
  if (ota.ready) return true;
  const recovery = effectiveRecovery();
  return (
    ota.enabled === true
    && Number(ota.expected || 0) > 0
    && Number(ota.missing || 0) === 0
    && (recovery.status === "mixed_firmware" || recovery.status === "ota_failed")
  );
}

function renderOtaProgress() {
  const progress = $("#ota-progress");
  if (!progress) return;
  const running = Boolean(otaInstall?.running);
  const complete = Boolean(otaInstall?.complete);
  const error = otaInstall?.error;
  const show = running || complete || error;
  progress.hidden = !show;
  if (!show) return;

  const sent = Number(otaInstall?.chunks_sent || 0);
  const total = Math.max(0, Number(otaInstall?.chunks_total || 0));
  const bytesSent = Number(otaInstall?.bytes_sent || 0);
  const size = Number(otaInstall?.size || 0);
  const percent = total > 0 ? Math.min(100, Math.round((sent / total) * 100)) : 0;
  const label = error
    ? `Install failed: ${error}`
    : complete
      ? "Install complete; boards rebooting"
      : `Installing ${otaInstall?.filename || "firmware"}`;
  $("#ota-progress-label").textContent = label;
  $("#ota-progress-count").textContent = total > 0
    ? `${sent} / ${total} chunks`
    : `${formatBytes(bytesSent)} / ${formatBytes(size)}`;
  const elapsed = Number(otaInstall?.elapsed_s || 0);
  const eta = Number(otaInstall?.eta_s || 0);
  const rate = Number(otaInstall?.bytes_per_s || 0);
  const rateLabel = rate > 0 ? `${formatBytes(rate)}/s` : "--";
  $("#ota-progress-meta").textContent = running
    ? `Elapsed ${formatDuration(elapsed)} · ETA ${formatDuration(eta)} · ${rateLabel}`
    : complete
      ? `Completed in ${formatDuration(elapsed)} · average ${rateLabel}`
      : error
        ? `Stopped after ${formatDuration(elapsed)}`
        : "";
  $("#ota-progress-fill").style.width = `${complete ? 100 : percent}%`;
  progress.classList.toggle("bad", Boolean(error));
  progress.classList.toggle("ok", complete && !error);
}

function renderOtaNodes() {
  const box = $("#ota-nodes");
  if (!box) return;
  const liveNodes = Array.isArray(state?.ota?.nodes) ? state.ota.nodes : [];
  const installNodes = Array.isArray(otaInstall?.nodes) ? otaInstall.nodes : [];
  const nodes = liveNodes.length ? liveNodes : installNodes;
  const installing = Boolean(otaInstall?.running);
  box.hidden = nodes.length === 0 && !installing;
  if (box.hidden) return;
  if (nodes.length === 0) {
    box.innerHTML = `<div class="ota-node-row"><span>Waiting for node reports</span><span class="muted-inline">--</span></div>`;
    return;
  }
  box.innerHTML = nodes.map((node) => {
    const failed = node.phase === "failed";
    const complete = node.phase === "complete";
    const cls = failed ? "bad" : complete ? "ok" : "";
    const lantern = lanterns().find((item) => item.mac === node.mac);
    const label = lantern?.label ? `${lantern.label} ${node.mac}` : (node.mac || "node");
    const detail = failed
      ? node.error
      : `${formatBytes(node.offset || 0)}${node.last_seen_s !== undefined ? ` · ${node.last_seen_s}s ago` : ""}`;
    return `<div class="ota-node-row ${cls}">
      <span>${escapeHtml(label)}</span>
      <span>${escapeHtml(node.phase || "idle")}</span>
      <span class="mono">${escapeHtml(detail)}</span>
    </div>`;
  }).join("");
}

function calibrationSettings() {
  return {
    threshold: Number($("#calibration-threshold")?.value || 180),
    min_area: Number($("#calibration-min-area")?.value || 4),
    max_distance: 0.035,
    first_code: Number($("#calibration-first-code")?.value || 1),
  };
}

function calibrationMissingFrames() {
  const value = String($("#calibration-missing-frames")?.value || "").trim();
  if (!value) return [];
  return value.split(",")
    .map((item) => Number(item.trim()))
    .filter((item) => Number.isInteger(item) && item >= 0);
}

function syntheticCalibrationSettings() {
  return {
    ...calibrationSettings(),
    led_value: Number($("#calibration-led-value")?.value || 255),
    jitter_px: Number($("#calibration-jitter")?.value || 0),
    glare_count: Number($("#calibration-glare-count")?.value || 0),
    glare_value: 230,
    missing_frames: calibrationMissingFrames(),
    perspective: Number($("#calibration-perspective")?.value || 0),
    min_hamming_distance: 3,
  };
}

function selectedCalibrationFrameIds() {
  return calibrationFrames.map((frame) => frame.frame_id);
}

function currentCalibrationProposal() {
  return calibrationProposal?.proposal || calibrationProposal;
}

function renderCalibration() {
  const frameCount = calibrationFrames.length;
  const proposal = currentCalibrationProposal();
  const assigned = Number(proposal?.metrics?.assigned || 0);
  const expected = Number(proposal?.metrics?.expected || 0);
  const problems = Number(proposal?.metrics?.missing || 0)
    + Number(proposal?.metrics?.ambiguous || 0)
    + Number(proposal?.metrics?.extra || 0);
  $("#calibration-status").textContent = problems > 0 ? "review" : assigned > 0 ? "proposal" : "idle";
  $("#calibration-status").className = `chip ${problems > 0 ? "warn" : assigned > 0 ? "sync" : ""}`;
  $("#calibration-frame-count").textContent = `${frameCount} uploaded`;
  $("#calibration-assignment-count").textContent = expected > 0 ? `${assigned} / ${expected}` : "--";
  $("#calibration-code-plan").textContent = calibrationCodePlan
    ? `${calibrationCodePlan.codes.length} nodes / ${calibrationCodePlan.bit_count} frames`
    : "--";
  const calibrationMode = state?.pattern?.pattern === "Calibration";
  const toggle = $('[data-action="toggle-calibration-mode"]');
  if (toggle) {
    toggle.textContent = calibrationMode ? "Stop lantern locator pattern" : "Play lantern locator pattern";
    toggle.classList.toggle("danger", calibrationMode);
  }
  $('[data-action="analyze-calibration-video"]').disabled = !$("#calibration-video")?.files?.length;
  $('[data-action="upload-calibration-frames"]').disabled = !$("#calibration-files")?.files?.length;
  $('[data-action="extract-calibration-video"]').disabled = !$("#calibration-video")?.files?.length;
  $('[data-action="propose-calibration"]').disabled = frameCount === 0;
  $$('[data-action="save-calibration-proposal"]').forEach((button) => {
    button.disabled = assigned === 0;
  });

  renderCalibrationResults(proposal);
}

function renderCalibrationResults(proposal) {
  renderLocationPreview(proposal);
  renderLocationSummary(proposal);
  const box = $("#calibration-results");
  if (!box) return;
  box.hidden = !proposal;
  if (!proposal) return;
  const assignments = proposal.assignments || [];
  const missing = proposal.missing || [];
  const ambiguous = proposal.ambiguous || [];
  const rows = [
    ...missing.map((item) => ({
      kind: "missing",
      label: item.mac,
      detail: `code ${item.code}`,
      position: item.reason,
      mac: item.mac,
    })),
    ...ambiguous.map((item) => ({
      kind: "ambiguous",
      label: item.mac,
      detail: `code ${item.code}`,
      position: item.reason,
      mac: item.mac,
    })),
  ];
  box.hidden = rows.length === 0;
  if (box.hidden) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = rows.length
    ? rows.map((row) => `<div class="calibration-result-row ${escapeHtml(row.kind)}">
      <span>${escapeHtml(row.kind)}</span>
      <strong>${escapeHtml(row.label)}</strong>
      <span>${escapeHtml(row.detail)}</span>
      <span class="mono">${escapeHtml(row.position)}</span>
      <button type="button" class="table-action" data-calibration-locate-mac="${escapeHtml(row.mac)}">Locate</button>
    </div>`).join("")
    : "";
  $$("[data-calibration-locate-mac]").forEach((button) => {
    button.addEventListener("click", async () => {
      selectLantern(button.dataset.calibrationLocateMac);
      await locateLantern(button.dataset.calibrationLocateMac);
    });
  });
}

function renderLocationSummary(proposal) {
  const box = $("#location-summary");
  if (!box) return;
  box.hidden = !proposal;
  if (!proposal) {
    box.innerHTML = "";
    return;
  }
  const metrics = proposal.metrics || {};
  const assigned = Number(metrics.assigned || 0);
  const expected = Number(metrics.expected || 0);
  const missing = Number(metrics.missing || 0);
  const ambiguous = Number(metrics.ambiguous || 0);
  const extra = Number(metrics.extra || 0);
  box.innerHTML = `
    <span class="summary-chip ok">${escapeHtml(String(assigned))}${expected ? ` / ${escapeHtml(String(expected))}` : ""} assigned</span>
    <span class="summary-chip ${missing ? "warn" : ""}">${escapeHtml(String(missing))} missing</span>
    <span class="summary-chip ${ambiguous ? "warn" : ""}">${escapeHtml(String(ambiguous))} ambiguous</span>
    <span class="summary-chip">${escapeHtml(String(extra))} ignored</span>
  `;
}

function renderLocationPreview(proposal) {
  const box = $("#location-preview");
  const saveRow = $("#location-save-row");
  const saveStatus = $("#location-save-status");
  if (!box) return;
  const frame = calibrationFrames[0];
  box.hidden = !proposal || !frame;
  if (saveRow) saveRow.hidden = box.hidden;
  if (saveStatus) saveStatus.textContent = calibrationSaveStatus;
  if (box.hidden) {
    box.innerHTML = "";
    return;
  }
  const assignments = proposal.assignments || [];
  const markers = assignments
    .map((item) => locationMarker("assign", lanternDisplayName(item.mac), item.x, item.y, item.bits))
    .join("");
  const offset = proposal.alignment_offset ? ` · aligned +${proposal.alignment_offset}` : "";
  const extra = Number(proposal.metrics?.extra || 0);
  const extraLabel = extra ? ` · ${extra} ignored` : "";
  box.innerHTML = `
    <div class="location-preview-head">
      <strong>Location proposal</strong>
      <span>${escapeHtml(String(assignments.length))} assigned${escapeHtml(extraLabel)}${escapeHtml(offset)}</span>
    </div>
    <div class="location-image-wrap">
      <img src="/api/calibration/frames/${encodeURIComponent(frame.frame_id)}/image" alt="">
      <div class="location-overlay">${markers}</div>
    </div>
  `;
}

function locationMarker(kind, label, x, y, detail) {
  const px = Math.max(0, Math.min(100, Number(x || 0) * 100));
  const py = Math.max(0, Math.min(100, Number(y || 0) * 100));
  return `<div class="location-marker ${escapeHtml(kind)}" style="left:${px}%;top:${py}%">
    <span class="location-pin"></span>
    <span class="location-label">${escapeHtml(label)}${detail ? ` · ${escapeHtml(detail)}` : ""}</span>
  </div>`;
}

async function refreshCalibrationFrames() {
  calibrationFrames = (await api("/api/calibration/frames")).frames || [];
  calibrationSaveStatus = "";
  renderCalibration();
}

async function uploadCalibrationFrames() {
  const input = $("#calibration-files");
  const files = Array.from(input?.files || []);
  if (!files.length) return;
  const uploaded = [];
  for (const file of files) {
    const ack = await apiBinary(`/api/calibration/frames?filename=${encodeURIComponent(file.name)}`, file);
    uploaded.push(ack.frame);
  }
  input.value = "";
  calibrationFrames = uploaded;
  calibrationProposal = null;
  calibrationSaveStatus = "";
  renderCalibration();
  toast(`uploaded ${files.length} calibration frame${files.length === 1 ? "" : "s"}`);
}

async function proposeCalibrationLayout() {
  const frameIds = selectedCalibrationFrameIds();
  if (!frameIds.length) return;
  if (!calibrationCodePlan) {
    await planCalibrationCodes();
  }
  const ack = await api("/api/calibration/propose-layout", {
    method: "POST",
    body: JSON.stringify({
      frame_ids: frameIds,
      code_map: calibrationCodePlan?.codes,
      ...calibrationSettings(),
    }),
  });
  calibrationProposal = ack.proposal;
  calibrationSaveStatus = "";
  renderCalibration();
  const metrics = ack.proposal.metrics || {};
  toast(`proposal: ${metrics.assigned || 0} assigned, ${metrics.missing || 0} missing`);
}

async function saveCalibrationProposal() {
  const proposal = currentCalibrationProposal();
  const assignments = proposal?.assignments || [];
  if (!assignments.length) return;
  const ack = await api("/api/calibration/apply-proposal", {
    method: "POST",
    body: JSON.stringify({
      assignments: assignments.map((item) => ({
        mac: item.mac,
        x: item.x,
        y: item.y,
        code: item.code,
        bits: item.bits,
      })),
      missing: proposal.missing || [],
      ambiguous: proposal.ambiguous || [],
    }),
  });
  state = await api("/api/state");
  calibrationSaveStatus = ack.message;
  render();
  toast(ack.message, !ack.ok);
}

async function planCalibrationCodes() {
  const ack = await api("/api/calibration/code-plan", {
    method: "POST",
    body: JSON.stringify({
      first_code: calibrationSettings().first_code,
      min_hamming_distance: 3,
    }),
  });
  calibrationCodePlan = ack.plan;
  renderCalibration();
  toast(`code plan: ${calibrationCodePlan.codes.length} nodes, ${calibrationCodePlan.bit_count} frames`);
  return calibrationCodePlan;
}

async function simulateCalibrationLayout() {
  const ack = await api("/api/calibration/simulate", {
    method: "POST",
    body: JSON.stringify({
      width: 960,
      height: 720,
      blob_radius: 5,
      ...syntheticCalibrationSettings(),
    }),
  });
  calibrationFrames = ack.simulation.frames || [];
  calibrationCodePlan = ack.simulation.plan || null;
  calibrationProposal = ack.simulation.proposal;
  calibrationSaveStatus = "";
  renderCalibration();
  const metrics = calibrationProposal.metrics || {};
  toast(`simulation: ${metrics.assigned || 0} assigned, ${metrics.missing || 0} missing`);
}

async function extractCalibrationVideoFrames() {
  const file = $("#calibration-video")?.files?.[0];
  if (!file) return;
  const plan = calibrationCodePlan || await planCalibrationCodes();
  const start = Number($("#calibration-video-start")?.value || 0);
  const interval = Number($("#calibration-video-interval")?.value || 1);
  const count = Number(plan?.bit_count || 0);
  if (!count) return;
  const frames = await extractVideoFrames(file, start, interval, count);
  const uploaded = [];
  for (const frame of frames) {
    const ack = await apiBinary(`/api/calibration/frames?filename=${encodeURIComponent(frame.filename)}`, frame.blob);
    uploaded.push(ack.frame);
  }
  calibrationFrames = uploaded;
  calibrationProposal = null;
  calibrationSaveStatus = "";
  renderCalibration();
  toast(`extracted ${frames.length} video frame${frames.length === 1 ? "" : "s"}`);
}

async function analyzeCalibrationVideo() {
  if (!$("#calibration-video")?.files?.length) return;
  await planCalibrationCodes();
  await extractCalibrationVideoFrames();
  await proposeCalibrationLayout();
}

async function toggleCalibrationMode() {
  const enabled = state?.pattern?.pattern !== "Calibration";
  const ack = await api("/api/operations/calibration-mode", {
    method: "POST",
    body: JSON.stringify({ enabled }),
  });
  calibrationCodePlan = ack.plan || calibrationCodePlan;
  toast(ack.message || (enabled ? "location mode started" : "location mode stopped"));
  await refresh();
}

function seekVideo(video, time) {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      video.removeEventListener("seeked", onSeeked);
      video.removeEventListener("error", onError);
    };
    const onSeeked = () => {
      cleanup();
      resolve();
    };
    const onError = () => {
      cleanup();
      reject(new Error("video frame could not be decoded"));
    };
    video.addEventListener("seeked", onSeeked, { once: true });
    video.addEventListener("error", onError, { once: true });
    video.currentTime = time;
  });
}

function loadVideoMetadata(video) {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      video.removeEventListener("loadedmetadata", onLoaded);
      video.removeEventListener("error", onError);
    };
    const onLoaded = () => {
      cleanup();
      resolve();
    };
    const onError = () => {
      cleanup();
      reject(new Error("video metadata could not be read"));
    };
    video.addEventListener("loadedmetadata", onLoaded, { once: true });
    video.addEventListener("error", onError, { once: true });
  });
}

function canvasBlob(canvas) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("video frame could not be encoded"));
    }, "image/png");
  });
}

async function extractVideoFrames(file, start, interval, count) {
  const url = URL.createObjectURL(file);
  const video = document.createElement("video");
  video.muted = true;
  video.preload = "metadata";
  video.src = url;
  try {
    await loadVideoMetadata(video);
    const width = video.videoWidth;
    const height = video.videoHeight;
    if (!width || !height) throw new Error("video has no usable dimensions");
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    const frames = [];
    for (let index = 0; index < count; index += 1) {
      const at = Math.min(Math.max(0, start + index * interval), Math.max(0, video.duration - 0.05));
      await seekVideo(video, at);
      context.drawImage(video, 0, 0, width, height);
      frames.push({
        filename: `video-calibration-${String(index + 1).padStart(2, "0")}.png`,
        blob: await canvasBlob(canvas),
      });
    }
    return frames;
  } finally {
    URL.revokeObjectURL(url);
  }
}


function minutesToTime(minutes) {
  const value = Number(minutes || 0) % 1440;
  const hh = String(Math.floor(value / 60)).padStart(2, "0");
  const mm = String(value % 60).padStart(2, "0");
  return `${hh}:${mm}`;
}

function timeToMinutes(value) {
  const [hh, mm] = String(value || "00:00").split(":").map(Number);
  return Math.min(1439, Math.max(0, (hh || 0) * 60 + (mm || 0)));
}

function selectedTimezone() {
  return $("#schedule-timezone")?.value || localStorage.getItem(TIMEZONE_STORAGE_KEY) || DEFAULT_TIMEZONE;
}

function currentMinuteInTimezone(timeZone = selectedTimezone()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(new Date());
  const hour = Number(parts.find((part) => part.type === "hour")?.value || 0) % 24;
  const minute = Number(parts.find((part) => part.type === "minute")?.value || 0);
  return hour * 60 + minute;
}

function powerSnapshotFromState(power = state?.power || {}) {
  return {
    light_sleep_check_s: Number(power.light_sleep_check_s ?? 4),
    deep_sleep_check_s: Number(power.deep_sleep_check_min ?? 15) * 60,
    led_on_start_min: Number(power.led_on_start_min ?? 20 * 60),
    led_on_end_min: Number(power.led_on_end_min ?? 6 * 60),
    schedule_enabled: Boolean(power.schedule_enabled),
    timezone: localStorage.getItem(TIMEZONE_STORAGE_KEY) || DEFAULT_TIMEZONE,
  };
}

function powerSnapshotFromForm() {
  return {
    light_sleep_check_s: Number($("#light-check").value || 4),
    deep_sleep_check_s: Number($("#deep-check").value || 900),
    led_on_start_min: timeToMinutes($("#led-on-start").value),
    led_on_end_min: timeToMinutes($("#led-on-end").value),
    schedule_enabled: $("#schedule-enabled").checked,
    timezone: selectedTimezone(),
  };
}

function powerSnapshotKey(snapshot) {
  return JSON.stringify(snapshot);
}

function isPowerDirty() {
  if (!powerBaseline) return false;
  return powerSnapshotKey(powerSnapshotFromForm()) !== powerSnapshotKey(powerBaseline);
}

function updateSleepScheduleDirtyState() {
  const saveButton = $('[data-action="save-power-policy"]');
  if (saveButton) saveButton.disabled = !isPowerDirty();
}

function renderPowerPolicy() {
  const power = state.power || {};
  const nextBaseline = powerSnapshotFromState(power);
  if (!powerBaseline || !isPowerDirty()) {
    powerBaseline = nextBaseline;
    $("#light-check").value = nextBaseline.light_sleep_check_s;
    $("#deep-check").value = nextBaseline.deep_sleep_check_s;
    $("#led-on-start").value = minutesToTime(nextBaseline.led_on_start_min);
    $("#led-on-end").value = minutesToTime(nextBaseline.led_on_end_min);
    $("#schedule-enabled").checked = nextBaseline.schedule_enabled;
    $("#schedule-timezone").value = nextBaseline.timezone;
  }
  const fieldMode = power.force_awake ? "wake" : (power.force_sleep ? "sleep" : "schedule");
  $("#field-power-state").textContent = fieldMode === "sleep"
    ? "sleep override"
    : (fieldMode === "wake" ? "awake override" : "following schedule");
  $('[data-action="sleep-field"]').disabled = fieldMode === "sleep";
  $('[data-action="wake-field"]').disabled = fieldMode === "wake";
  $('[data-action="follow-schedule"]').disabled = fieldMode === "schedule";
  $("#power-state").textContent = power.force_sleep
    ? "forced asleep"
    : (Boolean(power.schedule_enabled) ? (power.leds_on ? "LEDs on" : "asleep") : "boards on");
  updateSleepScheduleDirtyState();
}

function keepaliveSnapshotFromState(keepalive = state?.keepalive || {}) {
  return {
    enabled: Boolean(keepalive.enabled),
    interval_ms: Number(keepalive.interval_ms ?? 10000),
    pulse_ms: Number(keepalive.pulse_ms ?? 100),
    brightness: Number(keepalive.brightness ?? 64),
  };
}

function keepaliveSnapshotFromForm() {
  return {
    enabled: $("#keepalive-enabled").checked,
    interval_ms: Number($("#keepalive-interval").value || 10000),
    pulse_ms: Number($("#keepalive-pulse").value || 100),
    brightness: Number($("#keepalive-brightness").value || 64),
  };
}

function keepaliveSnapshotKey(snapshot) {
  return JSON.stringify(snapshot);
}

function isKeepaliveDirty() {
  if (!keepaliveBaseline) return false;
  return keepaliveSnapshotKey(keepaliveSnapshotFromForm()) !== keepaliveSnapshotKey(keepaliveBaseline);
}

function updateKeepaliveDirtyState() {
  const saveButton = $('[data-action="save-keepalive"]');
  if (saveButton) saveButton.disabled = !isKeepaliveDirty();
}

function renderKeepalive() {
  const keepalive = state.keepalive || {};
  const nextBaseline = keepaliveSnapshotFromState(keepalive);
  if (!keepaliveBaseline || !isKeepaliveDirty()) {
    keepaliveBaseline = nextBaseline;
    $("#keepalive-enabled").checked = nextBaseline.enabled;
    $("#keepalive-interval").value = nextBaseline.interval_ms;
    $("#keepalive-pulse").value = nextBaseline.pulse_ms;
    $("#keepalive-brightness").value = nextBaseline.brightness;
  }
  $("#keepalive-state").textContent = nextBaseline.enabled
    ? `${nextBaseline.pulse_ms}ms / ${nextBaseline.interval_ms}ms`
    : "off";
  updateKeepaliveDirtyState();
}

function powerWindowActive(power) {
  const minute = Number(power.current_min ?? currentMinuteInTimezone()) % 1440;
  const start = Number(power.led_on_start_min ?? 20 * 60) % 1440;
  const end = Number(power.led_on_end_min ?? 6 * 60) % 1440;
  if (start === end) return true;
  if (start < end) return minute >= start && minute < end;
  return minute >= start || minute < end;
}

function powerLedsOn(power) {
  return Boolean(power.force_awake)
    || (!Boolean(power.force_sleep) && (!Boolean(power.schedule_enabled) || powerWindowActive(power)));
}

function powerPolicyFromForm() {
  const deepSleepSeconds = Math.max(60, Number($("#deep-check").value || 900));
  return {
    light_sleep_check_s: Number($("#light-check").value || 4),
    deep_sleep_check_min: Math.max(1, Math.round(deepSleepSeconds / 60)),
    led_on_start_min: timeToMinutes($("#led-on-start").value),
    led_on_end_min: timeToMinutes($("#led-on-end").value),
    schedule_enabled: $("#schedule-enabled").checked,
    force_awake: false,
    force_sleep: false,
    current_min: currentMinuteInTimezone(),
    current_epoch_s: Math.floor(Date.now() / 1000),
  };
}

function detailSummary(lantern) {
  if (lantern.status === "retired") {
    return `This MAC was replaced and should not be used as a spare.`;
  }
  if (lantern.status === "missing") {
    return `Last seen ${lantern.last_seen_label}. Use Identify after it returns, or Replace if this lantern is physically gone.`;
  }
  if (lantern.position === "Missing") {
    return `Last seen ${lantern.last_seen_label}. It is healthy but has no table position yet.`;
  }
  return `Last seen ${lantern.last_seen_label}. Position is set. No action needed.`;
}

function renderEvents() {
  const log = $("#event-log");
  const events = state.events || [];
  log.hidden = events.length === 0;
  log.innerHTML = events.map((event) => {
    const time = new Date(event.ts * 1000).toLocaleTimeString();
    return `<div><span class="mono">${time}</span> ${escapeHtml(event.message)}</div>`;
  }).join("");
}

function selectLantern(mac) {
  if (mac !== selectedMac) {
    movingLanternMac = null;
    movingDrag = null;
    document.body.classList.remove("move-mode");
    document.body.classList.remove("place-mode");
    renderPlacementMarker();
  }
  selectedMac = mac;
  closeReplacePanel();
  ensureSelectionRing();
  renderUnpositionedTray();
  renderRows();
  renderDetail();
}

function toast(message, danger = false) {
  const node = $("#toast");
  node.textContent = message;
  node.style.borderColor = danger ? "rgba(255,93,82,.55)" : "rgba(84,214,122,.42)";
  node.style.color = danger ? "var(--alert)" : "var(--live)";
  node.classList.add("show");
  window.setTimeout(() => node.classList.remove("show"), 1800);
}

function mapCoord(value) {
  return MAP_PADDING + value * (1 - MAP_PADDING * 2);
}

function unmapCoord(value) {
  return Math.min(1, Math.max(0, (value - MAP_PADDING) / (1 - MAP_PADDING * 2)));
}

function setMapZoom(nextZoom) {
  mapZoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, nextZoom));
  setMapPan(mapPanX, mapPanY);
  renderMapZoom();
}

function renderMapZoom() {
  const content = $("#map-content");
  if (!content) return;
  content.style.transform = `translate(${mapPanX}px, ${mapPanY}px) scale(${mapZoom})`;
  const reset = $('[data-zoom="reset"]');
  if (reset) reset.textContent = `${mapZoom.toFixed(mapZoom === 1 ? 0 : 1)}x`;
}

function setMapPan(x, y) {
  const map = $("#map");
  const basePan = 0.16;
  const maxX = map.clientWidth * (basePan + (mapZoom - 1) * 0.5);
  const maxY = map.clientHeight * (basePan + (mapZoom - 1) * 0.5);
  mapPanX = Math.min(maxX, Math.max(-maxX, x));
  mapPanY = Math.min(maxY, Math.max(-maxY, y));
  renderMapZoom();
}

function touchDistance(touches) {
  const dx = touches[0].clientX - touches[1].clientX;
  const dy = touches[0].clientY - touches[1].clientY;
  return Math.hypot(dx, dy);
}

function pointToField(clientX, clientY) {
  const rect = $("#map").getBoundingClientRect();
  const normalizedX = ((clientX - rect.left - mapPanX) / mapZoom) / rect.width;
  const normalizedY = ((clientY - rect.top - mapPanY) / mapZoom) / rect.height;
  return { x: unmapCoord(normalizedX), y: unmapCoord(normalizedY) };
}

function setLanternPreview(mac, x, y) {
  const node = $$(".node").find((item) => item.dataset.mac === mac);
  if (!node) return;
  node.style.left = `${mapCoord(x) * 100}%`;
  node.style.top = `${mapCoord(y) * 100}%`;
  if (mac === selectedMac) {
    const ring = $(".selection-ring");
    if (ring) {
      ring.style.left = node.style.left;
      ring.style.top = node.style.top;
    }
  }
}

function isPlacingUnpositioned() {
  return movingLanternMac && !movingDrag && selectedLantern()?.mac === movingLanternMac && !isPositioned(selectedLantern());
}

function renderPlacementMarker(position = null) {
  let marker = $(".placement-marker");
  if (!isPlacingUnpositioned()) {
    marker?.remove();
    return;
  }
  if (!marker) {
    marker = document.createElement("div");
    marker.className = "placement-marker";
    $("#map-content").appendChild(marker);
  }
  if (!position) {
    position = { x: 0.5, y: 0.5 };
  }
  marker.style.left = `${mapCoord(position.x) * 100}%`;
  marker.style.top = `${mapCoord(position.y) * 100}%`;
}

function renderSelectionRing() {
  const lantern = selectedLantern();
  if (!isPositioned(lantern)) {
    $(".selection-ring")?.remove();
    return;
  }
  const ring = ensureSelectionRing();
  if (!ring) return;
  ring.style.left = `${mapCoord(lantern.x) * 100}%`;
  ring.style.top = `${mapCoord(lantern.y) * 100}%`;
}

function ensureSelectionRing() {
  const lantern = selectedLantern();
  if (!lantern || !isPositioned(lantern)) return null;
  let ring = $(".selection-ring");
  if (!ring) {
    ring = document.createElement("div");
    ring.className = "selection-ring";
    $("#map-content").prepend(ring);
  }
  return ring;
}

function openReplacePanel() {
  const lantern = selectedLantern();
  if (!lantern) return;
  if (!isPositioned(lantern)) {
    toast(`${lantern.label} has no position to replace`, true);
    return;
  }
  replaceMode = true;
  replacementMac = replacementCandidates()[0]?.mac || null;
  renderReplacePanel();
}

function closeReplacePanel() {
  replaceMode = false;
  replacementMac = null;
  renderReplacePanel();
}

function renderReplacePanel() {
  const panel = $("#replace-panel");
  if (!panel) return;
  const oldLantern = selectedLantern();
  panel.hidden = !replaceMode || !oldLantern;
  if (panel.hidden) return;

  const candidates = replacementCandidates();
  $("#replace-summary").textContent = candidates.length
    ? `Move ${oldLantern.label}'s position to an awake unpositioned spare.`
    : `No awake unpositioned spare is available. Turn on a spare lantern and wait for it to register.`;
  $("#replace-candidates").innerHTML = candidates.map((lantern) => `<button type="button" class="candidate ${lantern.mac === replacementMac ? "selected" : ""}" data-mac="${escapeHtml(lantern.mac)}">
    <strong>${escapeHtml(lantern.label)}</strong>
    <span class="mono">${escapeHtml(lantern.mac)}</span>
    <span>${escapeHtml(lantern.last_seen_label)}</span>
  </button>`).join("");
  $$("#replace-candidates .candidate").forEach((button) => {
    button.addEventListener("click", () => {
      replacementMac = button.dataset.mac;
      renderReplacePanel();
    });
  });
  $("#replace-confirm").disabled = !replacementMac;
}

async function confirmReplace() {
  const oldLantern = selectedLantern();
  if (!oldLantern || !replacementMac) return;
  const oldMac = oldLantern.mac;
  const newMac = replacementMac;
  const ack = await api("/api/lanterns/replace", {
    method: "POST",
    body: JSON.stringify({ old_mac: oldMac, new_mac: newMac }),
  });
  selectedMac = ack.new_mac || newMac;
  closeReplacePanel();
  toast(ack.message);
  await refresh();
}

function startMoveMode() {
  const lantern = selectedLantern();
  if (!lantern) return;
  movingLanternMac = lantern.mac;
  updateMoveTargetClass();
  document.body.classList.add("move-mode");
  document.body.classList.toggle("place-mode", !isPositioned(lantern));
  if (isPositioned(lantern)) {
    toast(`Drag ${lantern.label} to its new position`);
  } else {
    renderPlacementMarker();
    toast(`Click the map to place ${lantern.label}`);
  }
}

function updateMoveTargetClass() {
  $$(".node").forEach((node) => node.classList.toggle("move-target", node.dataset.mac === movingLanternMac));
}

function startLanternMove(event) {
  if (event.pointerType === "touch" || event.button !== 0 || movingLanternMac !== event.currentTarget.dataset.mac) return;
  event.stopPropagation();
  event.preventDefault();
  movingDrag = { pointerId: event.pointerId ?? null };
  if (event.pointerId !== undefined && event.currentTarget.setPointerCapture) {
    event.currentTarget.setPointerCapture(event.pointerId);
  }
}

async function finishLanternMove(clientX, clientY) {
  if (!movingLanternMac || !movingDrag) return;
  const mac = movingLanternMac;
  const position = pointToField(clientX, clientY);
  movingLanternMac = null;
  movingDrag = null;
  document.body.classList.remove("move-mode");
  document.body.classList.remove("place-mode");
  updateMoveTargetClass();
  renderPlacementMarker();
  setLanternPreview(mac, position.x, position.y);
  try {
    const ack = await api(`/api/lanterns/${encodeURIComponent(mac)}/assign`, {
      method: "POST",
      body: JSON.stringify(position),
    });
    toast(ack.message);
    await refresh();
  } catch (error) {
    toast(error.message, true);
    await refresh();
  }
}

async function placeSelectedLantern(clientX, clientY) {
  if (!isPlacingUnpositioned()) return;
  const mac = movingLanternMac;
  const position = pointToField(clientX, clientY);
  movingLanternMac = null;
  document.body.classList.remove("move-mode");
  document.body.classList.remove("place-mode");
  renderPlacementMarker();
  try {
    const ack = await api(`/api/lanterns/${encodeURIComponent(mac)}/assign`, {
      method: "POST",
      body: JSON.stringify(position),
    });
    toast(ack.message);
    await refresh();
  } catch (error) {
    toast(error.message, true);
    await refresh();
  }
}

async function refresh() {
  state = await api("/api/state");
  savedPatterns = (await api("/api/patterns")).patterns;
  otaArtifact = (await api("/api/operations/ota-artifact")).artifact;
  otaInstall = (await api("/api/operations/ota-install")).install;
  calibrationFrames = (await api("/api/calibration/frames")).frames || [];
  await refreshWifiStatus({ quiet: true });
  render();
}

async function refreshSavedPatterns() {
  savedPatterns = (await api("/api/patterns")).patterns;
  renderSavedPatterns();
}

async function refreshOtaInstall() {
  otaInstall = (await api("/api/operations/ota-install")).install;
  if (state) renderOta();
}

async function refreshWifiStatus({ quiet = false } = {}) {
  try {
    wifiStatus = (await api("/api/network/wifi")).wifi;
  } catch (error) {
    wifiStatus = { available: false, error: error.message, state: "unavailable", addresses: [] };
    if (!quiet) toast(error.message, true);
  }
  if (state) renderWifi();
  return wifiStatus;
}

async function pollOtaInstallWhile(installPromise) {
  let finished = false;
  installPromise.then(
    () => { finished = true; },
    () => { finished = true; },
  );
  while (!finished) {
    await delay(750);
    await refreshOtaInstall();
  }
  await refreshOtaInstall();
}

async function locateLantern(mac) {
  if (!mac) return;
  const ack = await api(`/api/lanterns/${encodeURIComponent(mac)}/identify`, { method: "POST" });
  toast(ack.message || "locator sent");
}

async function runAction(action) {
  const lantern = selectedLantern();
  if (!lantern && ["identify", "move", "replace", "forget"].includes(action)) return;
  try {
    if (action === "details") {
      const sheet = $("#detail-sheet");
      sheet.classList.toggle("show-details");
      if (sheet.classList.contains("show-details")) {
        sheet.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
      return;
    }
    if (action === "identify") {
      await locateLantern(lantern.mac);
      return;
    }
    if (action === "move") {
      startMoveMode();
      return;
    }
    if (action === "replace") {
      openReplacePanel();
      return;
    }
    if (action === "forget") {
      if (!confirm(`Forget position for ${lantern.label}?`)) return;
      const ack = await api(`/api/lanterns/${encodeURIComponent(lantern.mac)}/forget`, { method: "POST" });
      toast(ack.message);
      await refresh();
      return;
    }
    if (action === "broadcast") {
      if (!state || !patternDraft) return;
      if (!isPatternDirty()) return;
      const pattern = patternDraft.pattern;
      const brightness = Number(patternDraft.brightness);
      const params = patternParams(patternDraft);
      const ack = await api("/api/show/pattern", {
        method: "POST",
        body: JSON.stringify({ pattern, brightness, params }),
      });
      state = {
        ...state,
        pattern: {
          pattern,
          brightness,
          params: patternStateParams(patternDraft),
        },
      };
      render();
      toast(ack.message);
      await refresh();
      return;
    }
    if (action === "save-pattern") {
      if (!state || !patternDraft) return;
      const fallback = `${patternDraft.pattern} ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
      const name = prompt("Pattern name", fallback);
      if (!name) return;
      const ack = await api("/api/patterns", {
        method: "POST",
        body: JSON.stringify({
          name,
          pattern: patternDraft.pattern,
          brightness: Number(patternDraft.brightness),
          params: patternParams(patternDraft),
        }),
      });
      savedPatterns = [...savedPatterns, ack.pattern].sort((a, b) => a.name.localeCompare(b.name));
      renderSavedPatterns();
      toast(`saved ${ack.pattern.name}`);
      return;
    }
    if (action === "blackout") {
      if (!confirm("Broadcast blackout brightness 0?")) return;
      const ack = await api("/api/show/blackout", { method: "POST" });
      toast(ack.message, true);
      await refresh();
      return;
    }
    if (action === "save-power-policy") {
      if (!isPowerDirty()) return;
      const policy = powerPolicyFromForm();
      const ack = await api("/api/operations/power-policy", {
        method: "POST",
        body: JSON.stringify(policy),
      });
      localStorage.setItem(TIMEZONE_STORAGE_KEY, selectedTimezone());
      powerBaseline = powerSnapshotFromForm();
      updateSleepScheduleDirtyState();
      toast(ack.message);
      await refresh();
      return;
    }
    if (["sleep-field", "wake-field", "follow-schedule"].includes(action)) {
      const mode = {
        "sleep-field": "sleep",
        "wake-field": "wake",
        "follow-schedule": "schedule",
      }[action];
      const ack = await api("/api/operations/field-power", {
        method: "POST",
        body: JSON.stringify({ mode }),
      });
      toast(ack.message || `${mode} command sent`);
      await refresh();
      return;
    }
    if (action === "save-power-monitor") {
      const ack = await api("/api/operations/power-monitor", {
        method: "POST",
        body: JSON.stringify({
          battery_capacity_wh: Number($("#battery-capacity").value || 153.6),
          full_voltage: Number($("#battery-full-voltage").value || 14.6),
        }),
      });
      toast(ack.message);
      await refresh();
      return;
    }
    if (action === "save-keepalive") {
      if (!isKeepaliveDirty()) return;
      const config = keepaliveSnapshotFromForm();
      const ack = await api("/api/operations/keepalive", {
        method: "POST",
        body: JSON.stringify(config),
      });
      keepaliveBaseline = config;
      updateKeepaliveDirtyState();
      toast(ack.message);
      await refresh();
      return;
    }
    if (action === "refresh-wifi") {
      await refreshWifiStatus();
      toast("Wi-Fi refreshed");
      return;
    }
    if (action === "join-wifi") {
      const ssid = $("#wifi-ssid")?.value.trim();
      const password = $("#wifi-password")?.value || "";
      if (!ssid) {
        toast("network name is required", true);
        return;
      }
      if (!confirm(`Join Wi-Fi network "${ssid}"? The Pi may leave this network and the browser may disconnect.`)) return;
      const ack = await api("/api/network/wifi", {
        method: "POST",
        body: JSON.stringify({ ssid, password }),
      });
      toast(ack.message);
      return;
    }
    if (action === "start-hotspot") {
      if (!confirm("Start Basketnet? The Pi will leave its current Wi-Fi and the browser may disconnect.")) return;
      const ack = await api("/api/network/hotspot", { method: "POST" });
      toast(ack.message);
      return;
    }
    if (action === "enter-ota" || action === "exit-ota") {
      const enabled = action === "enter-ota";
      if (enabled && !confirm("Enter maintenance mode for a field-wide firmware update?")) return;
      const ack = await api("/api/operations/ota-mode", {
        method: "POST",
        body: JSON.stringify({ enabled }),
      });
      toast(ack.message);
      await refresh();
      return;
    }
    if (action === "stage-ota-artifact") {
      const file = $("#ota-file")?.files?.[0];
      if (!file) return;
      const ack = await apiBinary(`/api/operations/ota-artifact?filename=${encodeURIComponent(file.name)}`, file);
      otaArtifact = ack.artifact;
      renderOta();
      toast(ack.message);
      return;
    }
    if (action === "install-ota") {
      if (!otaArtifact || !otaReadyForInstall()) return;
      if (!confirm("Install staged firmware across the field? Boards will reboot.")) return;
      otaInstall = {
        running: true,
        complete: false,
        error: null,
        filename: otaArtifact.filename,
        size: otaArtifact.size,
        bytes_sent: 0,
        chunks_sent: 0,
        chunks_total: otaArtifact.chunks,
      };
      renderOta();
      const installPromise = api("/api/operations/ota-install", { method: "POST" });
      await pollOtaInstallWhile(installPromise);
      const ack = await installPromise;
      toast(ack.message);
      await refresh();
      return;
    }
    if (action === "upload-calibration-frames") {
      await uploadCalibrationFrames();
      return;
    }
    if (action === "refresh-calibration") {
      await refreshCalibrationFrames();
      toast("calibration frames refreshed");
      return;
    }
    if (action === "plan-calibration") {
      await planCalibrationCodes();
      return;
    }
    if (action === "propose-calibration") {
      await proposeCalibrationLayout();
      return;
    }
    if (action === "simulate-calibration") {
      await simulateCalibrationLayout();
      return;
    }
    if (action === "extract-calibration-video") {
      await extractCalibrationVideoFrames();
      return;
    }
    if (action === "analyze-calibration-video") {
      await analyzeCalibrationVideo();
      return;
    }
    if (action === "save-calibration-proposal") {
      await saveCalibrationProposal();
      return;
    }
    if (action === "toggle-calibration-mode") {
      await toggleCalibrationMode();
      return;
    }
  } catch (error) {
    toast(error.message, true);
  }
}

function connectWebSocket() {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${window.location.host}/ws`);
  ws.addEventListener("open", () => {
    $("#connection-status").textContent = "connected";
  });
  ws.addEventListener("message", (event) => {
    const data = JSON.parse(event.data);
    if (data.state) {
      state = data.state;
      render();
    }
  });
  ws.addEventListener("close", () => {
    $("#connection-status").textContent = "reconnecting";
    window.setTimeout(connectWebSocket, 1500);
  });
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

function errorMessage(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      const location = Array.isArray(item.loc) ? item.loc.join(".") : "request";
      return `${location}: ${item.msg || "invalid value"}`;
    }).join("; ");
  }
  if (detail && typeof detail === "object") {
    return detail.message || detail.error || JSON.stringify(detail);
  }
  return String(detail);
}

function fmt(value) {
  return value === null || value === undefined ? "-" : Number(value).toFixed(2);
}

$$(".tabs button").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tabs button").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    $$(".view").forEach((view) => view.classList.remove("active"));
    $(`#view-${tab.dataset.view}`).classList.add("active");
    renderDetailVisibility();
  });
});

$$(".filters .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    $$(".filters .chip").forEach((item) => item.classList.remove("active"));
    chip.classList.add("active");
    filter = chip.dataset.filter;
    renderRows();
  });
});

document.addEventListener("click", (event) => {
  const zoomTarget = event.target.closest("[data-zoom]");
  const zoom = zoomTarget?.dataset.zoom;
  if (zoom === "in") setMapZoom(mapZoom + 0.25);
  if (zoom === "out") setMapZoom(mapZoom - 0.25);
  if (zoom === "reset") {
    mapPanX = 0;
    mapPanY = 0;
    setMapZoom(1);
  }
  const patternTarget = event.target.closest("[data-pattern-action]");
  if (!patternTarget) return;
  const id = patternTarget.dataset.patternId;
  const action = patternTarget.dataset.patternAction;
  if (!id || !action) return;
  if (action === "broadcast-saved") {
    api(`/api/patterns/${encodeURIComponent(id)}/broadcast`, { method: "POST" })
      .then(async (ack) => {
        toast(ack.message);
        await refresh();
      })
      .catch((error) => toast(error.message, true));
  }
  if (action === "delete-saved") {
    const item = savedPatterns.find((pattern) => pattern.id === id);
    if (!confirm(`Delete ${item?.name || id}?`)) return;
    api(`/api/patterns/${encodeURIComponent(id)}`, { method: "DELETE" })
      .then(async () => {
        await refreshSavedPatterns();
        toast("pattern deleted");
      })
      .catch((error) => toast(error.message, true));
  }
});

$$("[data-action]").forEach((button) => {
  button.addEventListener("click", () => runAction(button.dataset.action));
});

$("#ota-file")?.addEventListener("change", renderOta);
$("#calibration-files")?.addEventListener("change", () => {
  calibrationProposal = null;
  calibrationSaveStatus = "";
  renderCalibration();
});
$("#calibration-video")?.addEventListener("change", () => {
  calibrationProposal = null;
  calibrationSaveStatus = "";
  renderCalibration();
});
[
  "#calibration-threshold",
  "#calibration-min-area",
  "#calibration-first-code",
  "#calibration-video-start",
  "#calibration-video-interval",
  "#calibration-jitter",
  "#calibration-led-value",
  "#calibration-glare-count",
  "#calibration-perspective",
  "#calibration-missing-frames",
].forEach((selector) => {
  const input = $(selector);
  input?.addEventListener("input", () => {
    if (selector === "#calibration-first-code") calibrationCodePlan = null;
    calibrationProposal = null;
    calibrationSaveStatus = "";
    renderCalibration();
  });
});

$("[data-replace-cancel]").addEventListener("click", closeReplacePanel);
$("#replace-confirm").addEventListener("click", () => {
  confirmReplace().catch((error) => toast(error.message, true));
});

$("#brightness").addEventListener("input", (event) => {
  if (!patternDraft && state) patternDraft = patternDraftFromState();
  if (patternDraft) patternDraft.brightness = Number(event.target.value);
  $("#brightness-value").textContent = event.target.value;
  renderPatternControls();
});

$("#pattern-picker").addEventListener("click", (event) => {
  if (event.target.dataset.pattern) {
    if (!patternDraft && state) patternDraft = patternDraftFromState();
    if (patternDraft) {
      patternDraft = patternDraftForSelection(event.target.dataset.pattern);
      renderPatternControls();
    }
  }
});

$("#color-presets").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-hex]");
  if (!button) return;
  if (!patternDraft && state) patternDraft = patternDraftFromState();
  if (patternDraft) applyHexColor(button.dataset.hex);
});

function applyHexColor(hex) {
  const rgb = parseHexColor(hex);
  if (!rgb) {
    $("#pattern-hex").classList.add("invalid");
    $("#hex-error").textContent = "Enter a hex color like #ff8800 or #f80.";
    $("#hex-error").hidden = false;
    return;
  }
  if (!patternDraft && state) patternDraft = patternDraftFromState();
  if (!patternDraft) return;
  const { hue, saturation, value } = rgbToHueSaturationValue(rgb.r, rgb.g, rgb.b);
  patternDraft.hue = hue;
  patternDraft.saturation = saturation;
  patternDraft.value = value;
  renderPatternControls();
}

$("#pattern-hex").addEventListener("input", (event) => {
  applyHexColor(event.target.value);
});

$("#pattern-color-picker").addEventListener("input", (event) => {
  applyHexColor(event.target.value);
});

$("#pattern-period").addEventListener("input", (event) => {
  if (!patternDraft && state) patternDraft = patternDraftFromState();
  if (patternDraft) {
    patternDraft.period = Number(event.target.value);
    renderPatternControls();
  }
});

$("#pattern-wavelength").addEventListener("input", (event) => {
  if (!patternDraft && state) patternDraft = patternDraftFromState();
  if (patternDraft) {
    patternDraft.wavelength = Number(event.target.value);
    renderPatternControls();
  }
});

$("#pattern-spatial").addEventListener("input", (event) => {
  if (!patternDraft && state) patternDraft = patternDraftFromState();
  if (patternDraft) {
    patternDraft.spatial = Number(event.target.value);
    renderPatternControls();
  }
});

$("#pattern-scatter").addEventListener("input", (event) => {
  if (!patternDraft && state) patternDraft = patternDraftFromState();
  if (patternDraft) {
    patternDraft.scatter = Number(event.target.value);
    renderPatternControls();
  }
});

$("#pattern-angle").addEventListener("input", (event) => {
  if (!patternDraft && state) patternDraft = patternDraftFromState();
  if (patternDraft) {
    patternDraft.angle = Number(event.target.value);
    renderPatternControls();
  }
});

["#schedule-enabled", "#led-on-start", "#led-on-end", "#schedule-timezone", "#light-check", "#deep-check"].forEach((selector) => {
  const input = $(selector);
  input.addEventListener("input", updateSleepScheduleDirtyState);
  input.addEventListener("change", updateSleepScheduleDirtyState);
});

["#keepalive-enabled", "#keepalive-interval", "#keepalive-pulse", "#keepalive-brightness"].forEach((selector) => {
  const input = $(selector);
  input.addEventListener("input", updateKeepaliveDirtyState);
  input.addEventListener("change", updateKeepaliveDirtyState);
});

$("#map").addEventListener("wheel", (event) => {
  if (!event.ctrlKey && !event.metaKey) return;
  event.preventDefault();
  setMapZoom(mapZoom + (event.deltaY < 0 ? 0.12 : -0.12));
}, { passive: false });

$("#map").addEventListener("touchstart", (event) => {
  if (event.touches.length === 2) {
    dragStart = null;
    movingDrag = null;
    pinchStartDistance = touchDistance(event.touches);
    pinchStartZoom = mapZoom;
    return;
  }
  if (event.touches.length === 1 && movingLanternMac && event.target.classList.contains("node") && event.target.dataset.mac === movingLanternMac) {
    const touch = event.touches[0];
    movingDrag = { touchId: touch.identifier };
    event.preventDefault();
    return;
  }
  if (event.touches.length === 1 && isPlacingUnpositioned() && !event.target.closest("button")) {
    const touch = event.touches[0];
    renderPlacementMarker(pointToField(touch.clientX, touch.clientY));
    event.preventDefault();
    return;
  }
  if (event.touches.length === 1 && !event.target.classList.contains("node") && !event.target.closest("button")) {
    const touch = event.touches[0];
    dragStart = { x: touch.clientX, y: touch.clientY, panX: mapPanX, panY: mapPanY };
  }
}, { passive: false });

$("#map").addEventListener("touchmove", (event) => {
  if (event.touches.length === 1 && movingLanternMac && movingDrag) {
    event.preventDefault();
    const touch = event.touches[0];
    const position = pointToField(touch.clientX, touch.clientY);
    setLanternPreview(movingLanternMac, position.x, position.y);
    return;
  }
  if (event.touches.length === 1 && isPlacingUnpositioned()) {
    event.preventDefault();
    const touch = event.touches[0];
    renderPlacementMarker(pointToField(touch.clientX, touch.clientY));
    return;
  }
  if (event.touches.length === 2 && pinchStartDistance) {
    event.preventDefault();
    setMapZoom(pinchStartZoom * (touchDistance(event.touches) / pinchStartDistance));
    return;
  }
  if (event.touches.length === 1 && dragStart) {
    event.preventDefault();
    const touch = event.touches[0];
    setMapPan(dragStart.panX + touch.clientX - dragStart.x, dragStart.panY + touch.clientY - dragStart.y);
  }
}, { passive: false });

$("#map").addEventListener("touchend", (event) => {
  if (movingLanternMac && movingDrag && event.changedTouches.length) {
    const touch = event.changedTouches[0];
    finishLanternMove(touch.clientX, touch.clientY);
    return;
  }
  if (isPlacingUnpositioned() && event.changedTouches.length) {
    const touch = event.changedTouches[0];
    placeSelectedLantern(touch.clientX, touch.clientY);
    return;
  }
  if (event.touches.length < 2) pinchStartDistance = null;
  if (event.touches.length === 0) dragStart = null;
}, { passive: true });

$("#map").addEventListener("pointerdown", (event) => {
  if (event.pointerType === "touch" || event.button !== 0 || event.target.classList.contains("node") || event.target.closest("button")) return;
  if (isPlacingUnpositioned()) {
    event.preventDefault();
    renderPlacementMarker(pointToField(event.clientX, event.clientY));
    return;
  }
  if (movingLanternMac) return;
  dragStart = { x: event.clientX, y: event.clientY, panX: mapPanX, panY: mapPanY };
  $("#map").setPointerCapture(event.pointerId);
});

$("#map").addEventListener("pointermove", (event) => {
  if (isPlacingUnpositioned() && event.pointerType !== "touch") {
    renderPlacementMarker(pointToField(event.clientX, event.clientY));
    return;
  }
  if (!dragStart || event.pointerType === "touch") return;
  setMapPan(dragStart.panX + event.clientX - dragStart.x, dragStart.panY + event.clientY - dragStart.y);
});

$("#map").addEventListener("pointerup", (event) => {
  if (isPlacingUnpositioned() && event.pointerType !== "touch") {
    placeSelectedLantern(event.clientX, event.clientY);
    return;
  }
  if (event.pointerType !== "touch") dragStart = null;
});

$("#map").addEventListener("pointercancel", () => {
  dragStart = null;
});

window.addEventListener("pointermove", (event) => {
  if (!movingLanternMac || !movingDrag || event.pointerType === "touch") return;
  const position = pointToField(event.clientX, event.clientY);
  setLanternPreview(movingLanternMac, position.x, position.y);
});

window.addEventListener("pointerup", (event) => {
  if (!movingLanternMac || !movingDrag || event.pointerType === "touch") return;
  finishLanternMove(event.clientX, event.clientY);
});

window.addEventListener("pointercancel", () => {
  movingDrag = null;
  if (isPlacingUnpositioned()) renderPlacementMarker();
});

window.addEventListener("mousemove", (event) => {
  if (!movingLanternMac || !movingDrag) return;
  const position = pointToField(event.clientX, event.clientY);
  setLanternPreview(movingLanternMac, position.x, position.y);
});

window.addEventListener("mouseup", (event) => {
  if (!movingLanternMac || !movingDrag) return;
  finishLanternMove(event.clientX, event.clientY);
});

refresh().then(connectWebSocket).catch((error) => toast(error.message, true));
