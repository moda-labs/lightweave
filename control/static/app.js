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
let selectedGroup = 0;
let groupNameBaseline = null;
let powerBaseline = null;
let otaArtifact = null;
let otaArtifactUploading = false;
let otaInstall = null;
let otaInstallRefreshPromise = null;
let otaInstallPollTimer = null;
let savedPatterns = [];
let uploadedPatterns = [];
let customPatternSourceMode = "guided";
let calibrationFrames = [];
let calibrationProposal = null;
let calibrationCodePlan = null;
let calibrationSaveStatus = "";
let wifiStatus = null;
let releaseInfo = null;
let provisioning = null;
let powerHistory = { hours: 24, samples: [], count: 0, loading: true, error: null, loadedAt: 0 };
let powerHistoryRefreshPromise = null;
let powerHistoryPollTimer = null;
let audioState = null;
let audioRefreshPromise = null;
let audioPollTimer = null;
let audioMutationGeneration = 0;
let audioMutationPending = 0;
let audioTrackListSignature = null;
let fieldPreview = { nodes: [], frames: [], loading: true, error: null, loadedAt: 0 };
let fieldPreviewRefreshPromise = null;
let fieldPreviewPollTimer = null;
let fieldPreviewAnimationFrame = null;
let fieldPreviewAnimationStartedAt = 0;

const MAP_PADDING = 0.08;
const GROUP_COUNT = 8;
const LED_COUNTS = [16, 32, 64];
const MIN_ZOOM = 1;
const MAX_ZOOM = 3;
const DEFAULT_TIMEZONE = "America/Los_Angeles";
const TIMEZONE_STORAGE_KEY = "baskets.sleepTimezone";
const GROUP_BRIGHTNESS_STORAGE_KEY = "baskets.groupBrightnessRestore";
const POWER_DRAW_WINDOW_S = 15 * 60;
const POWER_HISTORY_POLL_MS = 60 * 1000;
const AUDIO_POLL_MS = 1000;
const FIELD_PREVIEW_DURATION_MS = 6000;
const FIELD_PREVIEW_FPS = 8;
const FIELD_PREVIEW_POLL_MS = 5000;
const POWER_SERIES_COLORS = ["#5eb7ff", "#f0b35a", "#54d67a", "#9f8cff", "#ff8a66", "#e4e9e3"];
const PATTERN_DEFAULTS = {
  Pulse: { hue: 40, saturation: 100, value: 255, period: 4000, wavelength: 300, spatial: 0, scatter: 100, angle: 45 },
  Glow: { hue: 40, saturation: 100, value: 255, period: 4000, wavelength: 300, spatial: 0, scatter: 100, angle: 45 },
  White: { hue: 40, saturation: 100, value: 255, period: 4000, wavelength: 300, spatial: 0, scatter: 100, angle: 45 },
  Solid: { hue: 40, saturation: 100, value: 255, period: 4000, wavelength: 300, spatial: 0, scatter: 100, angle: 45 },
  Sweep: { hue: 40, saturation: 100, value: 255, period: 4000, wavelength: 300, spatial: 0, scatter: 100, angle: 45 },
  Wavefront: { hue: 200, saturation: 90, value: 255, period: 6000, wavelength: 300, frontWidth: 28, spatial: 0, scatter: 100, angle: 0 },
  "Palette Drift": { hue: 40, saturation: 100, value: 255, period: 8000, wavelength: 300, spatial: 0, scatter: 100, angle: 45 },
  Firefly: { hue: 58, saturation: 85, value: 255, period: 7000, wavelength: 300, spatial: 0, scatter: 100, chorus: 36, angle: 45 },
  "Fire Flicker": { hue: 24, saturation: 95, value: 255, period: 1200, wavelength: 300, spatial: 0, scatter: 100, texture: 85, angle: 45 },
  Fire2012: { hue: 24, saturation: 100, value: 255, period: 1200, wavelength: 300, spatial: 0, scatter: 100, texture: 85, angle: 45, speed: 30, cooling: 55, sparking: 120 },
  "Ocean Wave": { hue: 205, saturation: 100, value: 255, period: 9000, wavelength: 100, spatial: 0, scatter: 100, angle: 45 },
  "Pond Ripple": { hue: 195, saturation: 80, value: 255, period: 6000, wavelength: 50, spatial: 0, scatter: 100, angle: 45, centerX: 500, centerY: 500 },
  "Uploaded Pattern": { hue: 202, saturation: 90, value: 255, period: 8000, wavelength: 100, spatial: 0, scatter: 100, angle: 45 },
};

const CUSTOM_BUILDER_DEFAULTS = Object.freeze({
  motion: "traveling-wave",
  hue: 202,
  saturation: 90,
  periodMs: 8000,
  wavelength: 1,
  direction: 45,
  centerX: 0.5,
  centerY: 0.5,
  minValue: 20,
  maxValue: 100,
});
const CUSTOM_BUILDER_MOTIONS = new Set([
  "traveling-wave",
  "center-ripple",
  "whole-field-pulse",
  "lantern-shimmer",
  "steady-glow",
]);

function clampCustomSetting(value, minimum, maximum, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(maximum, Math.max(minimum, number)) : fallback;
}

function displayPatternName(pattern) {
  return pattern === "Uploaded Pattern" ? "Custom Pattern" : pattern;
}

function customBuilderProgram(settings = {}) {
  const motion = CUSTOM_BUILDER_MOTIONS.has(settings.motion)
    ? settings.motion
    : CUSTOM_BUILDER_DEFAULTS.motion;
  const hue = clampCustomSetting(settings.hue, 0, 359, CUSTOM_BUILDER_DEFAULTS.hue);
  const saturation = clampCustomSetting(settings.saturation, 0, 100, CUSTOM_BUILDER_DEFAULTS.saturation);
  const periodMs = clampCustomSetting(settings.periodMs, 2000, 30000, CUSTOM_BUILDER_DEFAULTS.periodMs);
  const wavelength = clampCustomSetting(settings.wavelength, 0.25, 2, CUSTOM_BUILDER_DEFAULTS.wavelength);
  const direction = clampCustomSetting(settings.direction, 0, 355, CUSTOM_BUILDER_DEFAULTS.direction);
  const centerX = clampCustomSetting(settings.centerX, 0, 1, CUSTOM_BUILDER_DEFAULTS.centerX);
  const centerY = clampCustomSetting(settings.centerY, 0, 1, CUSTOM_BUILDER_DEFAULTS.centerY);
  const minValue = clampCustomSetting(settings.minValue, 0, 80, CUSTOM_BUILDER_DEFAULTS.minValue);
  const requestedMax = clampCustomSetting(settings.maxValue, 20, 100, CUSTOM_BUILDER_DEFAULTS.maxValue);
  const maxValue = Math.max(minValue, requestedMax);
  const radians = direction * Math.PI / 180;
  const timePhase = { op: "mul", args: ["time", 2 * Math.PI / (periodMs / 1000)] };
  let phase = timePhase;

  if (motion === "traveling-wave") {
    const spatialScale = 2 * Math.PI / wavelength;
    const spatialPhase = {
      op: "add",
      args: [
        { op: "mul", args: ["x", Math.cos(radians) * spatialScale] },
        { op: "mul", args: ["y", Math.sin(radians) * spatialScale] },
      ],
    };
    phase = { op: "add", args: [spatialPhase, timePhase] };
  } else if (motion === "center-ripple") {
    phase = {
      op: "sub",
      args: [
        {
          op: "mul",
          args: [
            { op: "distance", args: ["x", "y", centerX, centerY] },
            2 * Math.PI / wavelength,
          ],
        },
        timePhase,
      ],
    };
  } else if (motion === "lantern-shimmer") {
    const lanternPhase = {
      op: "mul",
      args: [
        {
          op: "hash",
          args: [{
            op: "add",
            args: [
              { op: "mul", args: ["x", 31] },
              { op: "mul", args: ["y", 17] },
            ],
          }],
        },
        2 * Math.PI,
      ],
    };
    phase = { op: "add", args: [timePhase, lanternPhase] };
  }

  const value = motion === "steady-glow"
    ? maxValue / 100
    : {
        op: "mix",
        args: [
          minValue / 100,
          maxValue / 100,
          { op: "wave", args: [phase] },
        ],
      };
  return {
    hue: hue / 360,
    saturation: saturation / 100,
    value,
    intensity: 1,
    _builder: {
      version: 1,
      motion,
      period_ms: periodMs,
      wavelength,
      direction_deg: direction,
      center_x: centerX,
      center_y: centerY,
      min_value: minValue,
      max_value: maxValue,
    },
  };
}

function customBuilderSettingsFromProgram(program) {
  const metadata = program?._builder;
  if (!metadata || Number(metadata.version) !== 1 || !CUSTOM_BUILDER_MOTIONS.has(metadata.motion)) return null;
  return {
    motion: metadata.motion,
    hue: clampCustomSetting(Number(program.hue) * 360, 0, 359, CUSTOM_BUILDER_DEFAULTS.hue),
    saturation: clampCustomSetting(Number(program.saturation) * 100, 0, 100, CUSTOM_BUILDER_DEFAULTS.saturation),
    periodMs: clampCustomSetting(metadata.period_ms, 2000, 30000, CUSTOM_BUILDER_DEFAULTS.periodMs),
    wavelength: clampCustomSetting(metadata.wavelength, 0.25, 2, CUSTOM_BUILDER_DEFAULTS.wavelength),
    direction: clampCustomSetting(metadata.direction_deg, 0, 355, CUSTOM_BUILDER_DEFAULTS.direction),
    centerX: clampCustomSetting(metadata.center_x, 0, 1, CUSTOM_BUILDER_DEFAULTS.centerX),
    centerY: clampCustomSetting(metadata.center_y, 0, 1, CUSTOM_BUILDER_DEFAULTS.centerY),
    minValue: clampCustomSetting(metadata.min_value, 0, 80, CUSTOM_BUILDER_DEFAULTS.minValue),
    maxValue: clampCustomSetting(metadata.max_value, 20, 100, CUSTOM_BUILDER_DEFAULTS.maxValue),
  };
}

const DEFAULT_UPLOADED_PROGRAM = customBuilderProgram(CUSTOM_BUILDER_DEFAULTS);

const COLOR_VALUE_MARKER = 0x8000;
const FIREFLY_SCATTER_MASK = 0x007f;
const FIREFLY_CHORUS_MARKER = 0x8000;
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

function colorWheelPosition(hue, saturation) {
  const angle = (((Number(hue) % 360) + 360) % 360) * Math.PI / 180;
  const radius = Math.min(100, Math.max(0, Number(saturation))) * 0.46;
  return {
    left: 50 + Math.cos(angle) * radius,
    top: 50 + Math.sin(angle) * radius,
  };
}

function colorWheelSelection(rect, clientX, clientY, fallbackHue = 0) {
  const centerX = rect.left + rect.width / 2;
  const centerY = rect.top + rect.height / 2;
  const dx = Number(clientX) - centerX;
  const dy = Number(clientY) - centerY;
  const maxRadius = Math.max(1, Math.min(rect.width, rect.height) * 0.46);
  const distance = Math.hypot(dx, dy);
  return {
    hue: distance < 1
      ? ((Math.round(Number(fallbackHue)) % 360) + 360) % 360
      : Math.round((Math.atan2(dy, dx) * 180 / Math.PI + 360) % 360) % 360,
    saturation: Math.round(Math.min(1, distance / maxRadius) * 100),
  };
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

function fireflyChorusPack(saturation, interval) {
  const safeSaturation = Math.min(100, Math.max(0, Math.round(Number(saturation))));
  const safeInterval = Math.min(255, Math.max(0, Math.round(Number(interval))));
  return FIREFLY_CHORUS_MARKER | (safeInterval << 7) | safeSaturation;
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
    const error = new Error(errorMessage(body.detail || response.statusText));
    error.status = response.status;
    if (response.status === 401 && path !== "/api/auth/session") {
      window.location.assign("/login");
    }
    throw error;
  }
  if (response.status === 204) return null;
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
    const error = new Error(errorMessage(body.detail || response.statusText));
    error.status = response.status;
    if (response.status === 401) window.location.assign("/login");
    throw error;
  }
  return response.json();
}

function performerId(item) {
  const direct = Number(item?.node_id);
  if (Number.isInteger(direct) && direct > 0) return direct;
  const match = String(item?.label || "").match(/^#(\d+)$/);
  return match ? Number(match[1]) : Number.POSITIVE_INFINITY;
}

function comparePerformers(a, b) {
  const idDifference = performerId(a) - performerId(b);
  if (Number.isFinite(idDifference) && idDifference !== 0) return idDifference;
  return String(a?.label || a?.mac || "").localeCompare(
    String(b?.label || b?.mac || ""),
    undefined,
    { numeric: true },
  );
}

function lanterns() {
  return [...(state?.lanterns || [])].sort(comparePerformers);
}

function patternForGroup(groupId) {
  const entry = (state?.patterns || []).find((item) => Number(item.group_id) === Number(groupId));
  return entry?.config || (Number(groupId) === 0 ? state?.pattern : null) || state?.pattern || {
    pattern: "Glow",
    brightness: 48,
    params: {},
  };
}

function activePatternState() {
  return patternForGroup(selectedGroup);
}

function storedGroupBrightness(groupId) {
  try {
    const saved = JSON.parse(localStorage.getItem(GROUP_BRIGHTNESS_STORAGE_KEY) || "{}");
    const brightness = Number(saved[String(groupId)]);
    return Number.isFinite(brightness) && brightness > 0 ? Math.min(192, brightness) : 48;
  } catch (_error) {
    return 48;
  }
}

function rememberGroupBrightness(groupId, brightness) {
  if (!Number.isFinite(Number(brightness)) || Number(brightness) <= 0) return;
  try {
    const saved = JSON.parse(localStorage.getItem(GROUP_BRIGHTNESS_STORAGE_KEY) || "{}");
    saved[String(groupId)] = Math.min(192, Number(brightness));
    localStorage.setItem(GROUP_BRIGHTNESS_STORAGE_KEY, JSON.stringify(saved));
  } catch (_error) {
    // Storage can be unavailable in private browsing. The turn-on fallback is 48.
  }
}

function applyOptimisticPattern(groupId, config) {
  state = {
    ...state,
    pattern: Number(groupId) === 0 ? config : state.pattern,
    patterns: (state.patterns || []).some((entry) => Number(entry.group_id) === Number(groupId))
      ? state.patterns.map((entry) => Number(entry.group_id) === Number(groupId)
        ? { ...entry, config }
        : entry)
      : [...(state.patterns || []), { group_id: Number(groupId), config }],
  };
}

function updateLanternState(mac, changes) {
  state = {
    ...state,
    lanterns: (state?.lanterns || []).map((lantern) => lantern.mac === mac
      ? { ...lantern, ...changes }
      : lantern),
  };
}

function updateLanternPosition(mac, position) {
  const current = (state?.lanterns || []).find((lantern) => lantern.mac === mac);
  updateLanternState(mac, {
    ...position,
    position: "Set",
    attention: current?.attention === "Needs position" ? "None" : current?.attention,
  });
}

function applyOptimisticBlackout(enabled) {
  const patterns = (state?.patterns || []).map((entry) => {
    const groupId = Number(entry.group_id);
    const config = entry.config || {};
    if (enabled) rememberGroupBrightness(groupId, config.brightness);
    return {
      ...entry,
      config: {
        ...config,
        brightness: enabled ? 0 : storedGroupBrightness(groupId),
      },
    };
  });
  let primary = patterns.find((entry) => Number(entry.group_id) === 0)?.config;
  if (!primary) {
    const current = state?.pattern || { pattern: "Glow", brightness: 48, params: {} };
    if (enabled) rememberGroupBrightness(0, current.brightness);
    primary = { ...current, brightness: enabled ? 0 : storedGroupBrightness(0) };
  }
  state = {
    ...state,
    pattern: primary,
    patterns,
    blackout: { ...(state?.blackout || {}), restore_available: enabled },
  };
}

function groupEntry(groupId) {
  return (state?.groups || []).find((item) => Number(item.group_id) === Number(groupId));
}

function groupName(groupId) {
  return String(groupEntry(groupId)?.name || "");
}

function groupLabel(groupId) {
  return String(groupEntry(groupId)?.label || `Group ${Number(groupId) + 1}`);
}

function groupPerformerCounts(items) {
  const counts = Array.from({ length: GROUP_COUNT }, () => ({ online: 0, total: 0 }));
  for (const item of Array.isArray(items) ? items : []) {
    const groupId = Number(item.group_id);
    if (item.status === "retired" || !Number.isInteger(groupId) || groupId < 0 || groupId >= GROUP_COUNT) continue;
    counts[groupId].total += 1;
    if (item.status === "alive") counts[groupId].online += 1;
  }
  return counts;
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
  return `https://github.com/moda-labs/lightweave/commit/${buildLabel}`;
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

function formatTrackTime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds || 0)));
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function onlinePerformerCount(items) {
  return (Array.isArray(items) ? items : []).filter((item) => item.status === "alive").length;
}

function powerDrawSeries(samples, windowSeconds = POWER_DRAW_WINDOW_S) {
  const byMeter = new Map();
  for (const sample of Array.isArray(samples) ? samples : []) {
    const mac = String(sample?.mac || "").trim().toUpperCase();
    if (sample?.received_at === null || sample?.received_at === undefined || sample?.wh === null || sample?.wh === undefined) continue;
    const receivedAt = Number(sample?.received_at);
    const wh = Number(sample?.wh);
    if (!mac || !Number.isFinite(receivedAt) || !Number.isFinite(wh) || wh < 0) continue;
    if (!byMeter.has(mac)) byMeter.set(mac, []);
    byMeter.get(mac).push({ ...sample, mac, received_at: receivedAt, wh });
  }

  return [...byMeter.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([mac, meterSamples]) => {
    const points = [];
    let anchors = [];
    let session = null;
    let breakBefore = true;
    for (const sample of meterSamples.sort((left, right) => left.received_at - right.received_at)) {
      const nextSession = Number.isFinite(Number(sample.energy_session)) ? Number(sample.energy_session) : 0;
      const previous = anchors[anchors.length - 1];
      const gap = previous ? sample.received_at - previous.received_at : 0;
      if (sample.plausible === false) {
        anchors = [];
        session = nextSession;
        breakBefore = true;
        continue;
      }
      if (session !== nextSession || gap <= 0 || gap > windowSeconds + 5 * 60) {
        anchors = [];
        breakBefore = true;
      }
      session = nextSession;
      anchors.push(sample);
      const cutoff = sample.received_at - windowSeconds;
      while (anchors.length > 2 && anchors[1].received_at <= cutoff) anchors.shift();

      let watts = null;
      let source = null;
      if (anchors.length >= 2) {
        const first = anchors[0];
        const elapsed = sample.received_at - first.received_at;
        const energy = sample.wh - first.wh;
        const recent = elapsed > 0 && energy >= 0 ? energy * 3600 / elapsed : null;
        if (Number.isFinite(recent) && recent >= 0 && recent <= 50) {
          watts = recent;
          source = "recent_average";
        }
      }
      if (watts === null) {
        if (sample.bus_v === null || sample.bus_v === undefined || sample.current_ma === null || sample.current_ma === undefined) continue;
        const busV = Number(sample.bus_v);
        const currentMa = Number(sample.current_ma);
        const instantaneous = busV * currentMa / 1000;
        if (Number.isFinite(instantaneous) && instantaneous >= 0 && instantaneous <= 50) {
          watts = instantaneous;
          source = "instantaneous";
        }
      }
      if (watts !== null) {
        points.push({
          received_at: sample.received_at,
          watts,
          source,
          energy_session: session,
          break_before: breakBefore,
        });
        breakBefore = false;
      }
    }
    return { mac, points };
  }).filter((series) => series.points.length > 0);
}

function mergePowerHistorySamples(existing, incoming, hours, nowSeconds = Date.now() / 1000) {
  const cutoff = nowSeconds - Number(hours) * 60 * 60;
  const unique = new Map();
  for (const sample of [...(Array.isArray(existing) ? existing : []), ...(Array.isArray(incoming) ? incoming : [])]) {
    const receivedAt = Number(sample?.received_at);
    if (!Number.isFinite(receivedAt) || receivedAt < cutoff) continue;
    const key = `${sample.mac}|${receivedAt}|${sample.energy_session}|${sample.wh}`;
    unique.set(key, sample);
  }
  return [...unique.values()].sort((left, right) => Number(left.received_at) - Number(right.received_at));
}

function overviewIssues(currentState) {
  if (!currentState) return [];
  const issues = [];
  const conductor = currentState.conductor || {};
  const lanternItems = Array.isArray(currentState.lanterns) ? currentState.lanterns : [];
  const recovery = currentState.recovery || {};
  const firmware = currentState.summary?.firmware || {};
  const monitor = currentState.power_monitor || {};
  const audio = currentState.audio || {};
  const missing = lanternItems.filter((item) => item.status === "missing" && item.position === "Set").length;
  const unpositioned = lanternItems.filter((item) => item.status === "alive" && item.position !== "Set").length;

  if (conductor.connected !== true) {
    issues.push({ severity: "bad", title: "Conductor disconnected", detail: "Commands and fresh field state are unavailable." });
  } else if (conductor.sync !== "locked") {
    issues.push({ severity: "warn", title: `Clock sync ${conductor.sync || "unknown"}`, detail: "Check conductor time sync before running coordinated patterns." });
  }
  if (missing) issues.push({ severity: "bad", title: `${missing} placed lantern${missing === 1 ? " is" : "s are"} missing`, detail: "Wake, power-cycle, or replace the missing lanterns." });
  if (unpositioned) issues.push({ severity: "warn", title: `${unpositioned} online lantern${unpositioned === 1 ? " needs" : "s need"} a position`, detail: "Assign locations before the next layout-dependent pattern." });
  if (firmware.consistent === false) issues.push({ severity: "bad", title: "Mixed field firmware", detail: "Reconcile firmware before show operation." });
  const failedOta = Array.isArray(recovery.failed_ota) ? recovery.failed_ota.length : 0;
  if (failedOta) issues.push({ severity: "bad", title: `${failedOta} firmware update failure${failedOta === 1 ? "" : "s"}`, detail: "Open Firmware for repair status." });
  const stale = Number(monitor.stale_count || 0);
  const implausible = Number(monitor.implausible_count || 0);
  if (stale) issues.push({ severity: "warn", title: `${stale} stale power meter${stale === 1 ? "" : "s"}`, detail: "Stale readings are excluded from battery estimates." });
  if (implausible) issues.push({ severity: "warn", title: `${implausible} implausible power reading${implausible === 1 ? "" : "s"}`, detail: "Check meter voltage and shunt wiring." });
  if (monitor.history?.error) issues.push({ severity: "warn", title: "Power history is not recording", detail: String(monitor.history.error) });
  if (audio.available === false) {
    issues.push({ severity: "warn", title: "Soundtrack is unavailable", detail: audio.error || "Check the Pi audio player and MP3 files." });
  }
  return issues;
}

function setActiveView(viewName) {
  const tab = $(`.tabs button[data-view="${viewName}"]`);
  const view = $(`#view-${viewName}`);
  if (!tab || !view) return;
  $$(".tabs button").forEach((item) => item.classList.toggle("active", item === tab));
  $$(".view").forEach((item) => item.classList.toggle("active", item === view));
  renderDetailVisibility();
}

function chartTimeLabel(timestamp, hours) {
  const date = new Date(timestamp * 1000);
  return hours >= 48
    ? date.toLocaleDateString([], { month: "short", day: "numeric" })
    : date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function renderPowerHistoryChart() {
  const chart = $("#power-history-chart");
  const legend = $("#power-history-legend");
  if (!chart || !legend) return;
  if (powerHistory.loading && !powerHistory.loadedAt) {
    chart.innerHTML = '<div class="empty-state">Loading measured power history.</div>';
    legend.innerHTML = "";
    return;
  }
  if (powerHistory.error) {
    chart.innerHTML = `<div class="empty-state bad">Power history unavailable: ${escapeHtml(powerHistory.error)}</div>`;
    legend.innerHTML = "";
    return;
  }

  const series = powerDrawSeries(powerHistory.samples);
  const end = Date.now() / 1000;
  const start = end - powerHistory.hours * 60 * 60;
  const visibleSeries = series.map((item) => ({
    ...item,
    points: item.points.filter((point) => point.received_at >= start && point.received_at <= end + 60),
  })).filter((item) => item.points.length > 0);
  if (!visibleSeries.length) {
    chart.innerHTML = '<div class="empty-state">No usable power samples in this time range.</div>';
    legend.innerHTML = "";
    return;
  }

  const width = 900;
  const height = 290;
  const plot = { left: 56, right: 18, top: 22, bottom: 38 };
  const plotWidth = width - plot.left - plot.right;
  const plotHeight = height - plot.top - plot.bottom;
  const peak = Math.max(...visibleSeries.flatMap((item) => item.points.map((point) => point.watts)));
  const yMax = peak <= 1.5 ? 1.5 : Math.ceil(peak * 2) / 2;
  const x = (timestamp) => plot.left + Math.max(0, Math.min(1, (timestamp - start) / (end - start))) * plotWidth;
  const y = (watts) => plot.top + plotHeight - Math.max(0, Math.min(1, watts / yMax)) * plotHeight;
  const grid = [];
  for (let index = 0; index <= 4; index += 1) {
    const watts = yMax * index / 4;
    const py = y(watts);
    grid.push(`<line class="power-chart-grid" x1="${plot.left}" y1="${py.toFixed(1)}" x2="${width - plot.right}" y2="${py.toFixed(1)}"></line>`);
    grid.push(`<text class="power-chart-axis" x="${plot.left - 9}" y="${(py + 4).toFixed(1)}" text-anchor="end">${watts.toFixed(watts < 1 ? 2 : 1)} W</text>`);
  }
  for (let index = 0; index <= 4; index += 1) {
    const timestamp = start + (end - start) * index / 4;
    const px = x(timestamp);
    grid.push(`<line class="power-chart-grid" x1="${px.toFixed(1)}" y1="${plot.top}" x2="${px.toFixed(1)}" y2="${plot.top + plotHeight}"></line>`);
    grid.push(`<text class="power-chart-axis" x="${px.toFixed(1)}" y="${height - 12}" text-anchor="middle">${escapeHtml(chartTimeLabel(timestamp, powerHistory.hours))}</text>`);
  }

  const labelByMac = new Map(lanterns().map((item) => [String(item.mac || "").toUpperCase(), item.label || item.mac]));
  const lines = [];
  visibleSeries.forEach((item, seriesIndex) => {
    const color = POWER_SERIES_COLORS[seriesIndex % POWER_SERIES_COLORS.length];
    let path = "";
    let previous = null;
    for (const point of item.points) {
      const command = point.break_before || !previous || point.received_at - previous.received_at > 5 * 60 ? "M" : "L";
      path += `${command}${x(point.received_at).toFixed(1)},${y(point.watts).toFixed(1)} `;
      previous = point;
    }
    lines.push(`<path class="power-chart-line" stroke="${color}" d="${path.trim()}"></path>`);
    const latest = item.points[item.points.length - 1];
    lines.push(`<circle class="power-chart-dot" fill="${color}" cx="${x(latest.received_at).toFixed(1)}" cy="${y(latest.watts).toFixed(1)}" r="3.5"><title>${escapeHtml(labelByMac.get(item.mac) || item.mac)}: ${latest.watts.toFixed(2)} W</title></circle>`);
  });
  chart.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Measured performer power draw over ${powerHistory.hours} hours">
    ${grid.join("")}
    ${lines.join("")}
  </svg>`;
  legend.innerHTML = visibleSeries.map((item, index) => {
    const color = POWER_SERIES_COLORS[index % POWER_SERIES_COLORS.length];
    const latest = item.points[item.points.length - 1];
    return `<span><i class="power-chart-key" style="background:${color}"></i>${escapeHtml(labelByMac.get(item.mac) || item.mac)} · ${latest.watts.toFixed(2)} W</span>`;
  }).join("");
}

function fieldPreviewFrameIndex(preview, elapsedMs) {
  const frames = Array.isArray(preview?.frames) ? preview.frames : [];
  if (!frames.length) return -1;
  const interval = Math.max(1, Number(preview.frame_interval_ms || Math.round(1000 / FIELD_PREVIEW_FPS)));
  return Math.floor(Math.max(0, Number(elapsedMs) || 0) / interval) % frames.length;
}

function fieldPreviewVisible() {
  return !document.hidden && $("#view-overview")?.classList.contains("active");
}

function fieldPreviewRgb(value) {
  const rgb = Array.isArray(value) ? value : [0, 0, 0];
  return rgb.slice(0, 3).map((channel) => Math.min(255, Math.max(0, Number(channel) || 0)));
}

function drawFieldPreviewLantern(context, node, color, x, y, radius) {
  const rgb = fieldPreviewRgb(color?.rgb);
  const peak = Math.max(...rgb);
  if (peak > 0) {
    const glow = context.createRadialGradient(x, y, radius * 0.2, x, y, radius * 3.2);
    glow.addColorStop(0, `rgba(${rgb.join(",")},${Math.min(0.72, 0.24 + peak / 510)})`);
    glow.addColorStop(0.32, `rgba(${rgb.join(",")},0.18)`);
    glow.addColorStop(1, `rgba(${rgb.join(",")},0)`);
    context.fillStyle = glow;
    context.beginPath();
    context.arc(x, y, radius * 3.2, 0, Math.PI * 2);
    context.fill();
  }

  const pixels = Array.isArray(color?.pixels) ? color.pixels : [];
  context.fillStyle = "#202821";
  context.beginPath();
  context.arc(x, y, radius + 2, 0, Math.PI * 2);
  context.fill();
  if (pixels.length) {
    const ringRadius = radius * 0.72;
    const ledRadius = Math.max(1.3, radius * 0.18);
    pixels.forEach((pixel, index) => {
      const angle = -Math.PI / 2 + Math.PI * 2 * index / pixels.length;
      const ledRgb = fieldPreviewRgb(pixel);
      context.fillStyle = `rgb(${ledRgb.join(",")})`;
      context.beginPath();
      context.arc(x + Math.cos(angle) * ringRadius, y + Math.sin(angle) * ringRadius, ledRadius, 0, Math.PI * 2);
      context.fill();
    });
  } else {
    context.fillStyle = `rgb(${rgb.join(",")})`;
    context.beginPath();
    context.arc(x, y, radius * 0.72, 0, Math.PI * 2);
    context.fill();
  }

  const attention = String(node.attention || "").toLowerCase();
  const missing = node.status !== "alive";
  const warning = !missing && attention && attention !== "none";
  context.strokeStyle = missing ? "#ff5d52" : warning ? "#f0b35a" : "rgba(228,233,227,.28)";
  context.lineWidth = missing || warning ? 2.5 : 1;
  context.setLineDash(missing ? [4, 3] : []);
  context.beginPath();
  context.arc(x, y, radius + 4, 0, Math.PI * 2);
  context.stroke();
  context.setLineDash([]);
}

function drawFieldPreview(elapsedMs = 0) {
  const canvas = $("#field-preview-canvas");
  const stage = $("#field-preview-stage");
  if (!canvas || !stage || !fieldPreview.nodes?.length || !fieldPreview.frames?.length) return;
  const rect = stage.getBoundingClientRect();
  if (rect.width < 1 || rect.height < 1) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const pixelWidth = Math.round(rect.width * dpr);
  const pixelHeight = Math.round(rect.height * dpr);
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }
  const context = canvas.getContext("2d");
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  context.clearRect(0, 0, rect.width, rect.height);

  const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
  const frameIndex = fieldPreviewFrameIndex(fieldPreview, reducedMotion ? 0 : elapsedMs);
  const frame = fieldPreview.frames[frameIndex];
  if (!frame) return;
  const paddingX = Math.max(28, rect.width * 0.07);
  const paddingY = Math.max(28, rect.height * 0.10);
  const availableWidth = Math.max(1, rect.width - paddingX * 2);
  const availableHeight = Math.max(1, rect.height - paddingY * 2);
  const radius = Math.max(6, Math.min(13, Math.min(rect.width, rect.height) / (Math.sqrt(fieldPreview.nodes.length) * 4.5)));
  const showLabels = fieldPreview.nodes.length <= 24;
  fieldPreview.nodes.forEach((node, index) => {
    const x = paddingX + Math.min(1, Math.max(0, Number(node.x) || 0)) * availableWidth;
    const y = paddingY + Math.min(1, Math.max(0, Number(node.y) || 0)) * availableHeight;
    drawFieldPreviewLantern(context, node, frame.colors?.[index], x, y, radius);
    if (showLabels) {
      context.fillStyle = node.status === "alive" ? "#a7b0aa" : "#ff8a82";
      context.font = '11px "JetBrains Mono", "IBM Plex Mono", monospace';
      context.textAlign = "center";
      context.fillText(node.label || "node", x, y + radius + 18);
    }
  });
}

function animateFieldPreview(timestamp) {
  if (!fieldPreviewAnimationStartedAt) fieldPreviewAnimationStartedAt = timestamp;
  if (fieldPreviewVisible()) drawFieldPreview(timestamp - fieldPreviewAnimationStartedAt);
  fieldPreviewAnimationFrame = window.requestAnimationFrame(animateFieldPreview);
}

function startFieldPreviewAnimation() {
  fieldPreviewAnimationStartedAt = 0;
  if (fieldPreviewAnimationFrame === null) {
    fieldPreviewAnimationFrame = window.requestAnimationFrame(animateFieldPreview);
  }
}

function renderFieldPreviewStatus() {
  const status = $("#field-preview-status");
  const empty = $("#field-preview-empty");
  const meta = $("#field-preview-meta");
  const canvas = $("#field-preview-canvas");
  if (!status || !empty || !meta || !canvas) return;

  const statePositioned = lanterns().filter(isPositioned).length;
  const stateUnpositioned = lanterns().filter((item) => item.status !== "retired" && !isPositioned(item)).length;
  const positioned = Number(fieldPreview.positioned_count ?? statePositioned);
  const unpositioned = Number(fieldPreview.unpositioned_count ?? stateUnpositioned);
  const hasFrames = positioned > 0 && fieldPreview.frames?.length > 0;
  empty.hidden = hasFrames;
  canvas.hidden = !hasFrames;

  if (fieldPreview.error) {
    status.textContent = "unavailable";
    status.className = "chip bad";
    empty.innerHTML = `<strong>Expected field unavailable</strong><span>${escapeHtml(fieldPreview.error)}</span>`;
  } else if (!positioned) {
    status.textContent = "awaiting positions";
    status.className = "chip active";
    empty.innerHTML = "<strong>No lantern positions assigned yet</strong><span>Place a couple of lanterns on the map and their live pattern frames will appear here automatically.</span>";
  } else if (!hasFrames || fieldPreview.loading) {
    status.textContent = "rendering";
    status.className = "chip sync";
    empty.innerHTML = "<strong>Rendering expected field</strong><span>Building frames from the latest conductor state.</span>";
  } else if (fieldPreview.leds_on === false) {
    status.textContent = "LEDs off";
    status.className = "chip active";
  } else if (fieldPreview.mode === "locator") {
    status.textContent = "locator view";
    status.className = "chip active";
  } else {
    status.textContent = "live expected";
    status.className = "chip sync";
  }

  const seq = fieldPreview.source_seq === null || fieldPreview.source_seq === undefined ? "" : ` · seq ${fieldPreview.source_seq}`;
  meta.textContent = `${positioned} positioned · ${unpositioned} awaiting positions${seq}`;
  canvas.setAttribute(
    "aria-label",
    `Expected field animation with ${positioned} positioned lantern${positioned === 1 ? "" : "s"} and ${unpositioned} awaiting positions`,
  );
  if (hasFrames) drawFieldPreview(0);
}

async function refreshFieldPreview() {
  if (fieldPreviewRefreshPromise) return fieldPreviewRefreshPromise;
  fieldPreview.loading = !fieldPreview.loadedAt;
  renderFieldPreviewStatus();
  fieldPreviewRefreshPromise = (async () => {
    try {
      const preview = await api(`/api/field-preview/frames.json?duration_ms=${FIELD_PREVIEW_DURATION_MS}&fps=${FIELD_PREVIEW_FPS}`);
      fieldPreview = { ...preview, loading: false, error: null, loadedAt: Date.now() };
      fieldPreviewAnimationStartedAt = 0;
      startFieldPreviewAnimation();
    } catch (error) {
      fieldPreview = { ...fieldPreview, loading: false, error: error.message, loadedAt: Date.now() };
    }
    renderFieldPreviewStatus();
    return fieldPreview;
  })();
  try {
    return await fieldPreviewRefreshPromise;
  } finally {
    fieldPreviewRefreshPromise = null;
  }
}

function startFieldPreviewPolling() {
  const poll = async () => {
    if (fieldPreviewVisible()) await refreshFieldPreview();
    fieldPreviewPollTimer = window.setTimeout(poll, FIELD_PREVIEW_POLL_MS);
  };
  if (fieldPreviewPollTimer === null) poll();
}

function renderOverview() {
  const monitor = state.power_monitor || {};
  const audio = audioState || state.audio || {};
  const summary = state.summary || {};
  const conductor = state.conductor || {};
  const firmware = summary.firmware || {};
  const issues = overviewIssues(state);
  const critical = issues.some((issue) => issue.severity === "bad");
  const warning = issues.some((issue) => issue.severity === "warn");
  const status = $("#overview-health-status");
  status.textContent = critical ? "action required" : warning ? "check field" : "field healthy";
  status.className = `chip ${critical ? "bad" : warning ? "active" : "sync"}`;

  const online = Number(summary.alive || 0);
  const total = Number(summary.total || 0);
  $("#overview-field-online").textContent = `${online} / ${total}`;
  $("#overview-field-online").className = `overview-value ${total > 0 && online === total ? "ok" : "bad"}`;
  $("#overview-field-note").textContent = `${onlinePerformerCount(lanterns())} performers reporting now`;
  $("#overview-sync").textContent = conductor.sync || "--";
  $("#overview-sync").className = `overview-value ${conductor.connected && conductor.sync === "locked" ? "sync" : "bad"}`;
  $("#overview-conductor-note").textContent = conductor.connected ? `Conductor online · seq ${conductor.seq ?? "--"}` : "Conductor disconnected";
  const expected = Number(firmware.expected ?? total);
  const matching = Number(firmware.matching || 0);
  $("#overview-firmware").textContent = `${matching} / ${expected}`;
  $("#overview-firmware").className = `overview-value ${firmware.consistent !== false ? "ok" : "bad"}`;
  $("#overview-firmware-note").textContent = firmware.consistent !== false ? "Field on one build" : "Mixed builds detected";

  const soc = monitor.soc_percent ?? monitor.estimated_node_soc_percent;
  const performerDraw = monitor.average_performer_draw_w ?? monitor.avg_node_w;
  $("#overview-battery").textContent = soc === null || soc === undefined ? "--" : `${Number(soc).toFixed(1)}%`;
  $("#overview-battery").className = `overview-value ${soc === null || soc === undefined ? "" : soc < 25 ? "bad" : soc < 50 ? "warn" : "ok"}`;
  $("#overview-power-note").textContent = Number(monitor.usable_sample_count || 0)
    ? `${monitor.usable_sample_count} usable meter${Number(monitor.usable_sample_count) === 1 ? "" : "s"}`
    : "No usable meter readings";
  $("#overview-power-draw").textContent = performerDraw === null || performerDraw === undefined ? "--" : `${Number(performerDraw).toFixed(2)} W`;
  $("#overview-meter-count").textContent = `${Number(monitor.usable_sample_count || 0)} / ${Number(monitor.sample_count || 0)}`;
  $("#overview-history-status").textContent = powerHistory.loading
    ? "updating"
    : powerHistory.error
      ? "error"
      : `${powerHistory.count} samples`;
  $("#overview-history-status").className = powerHistory.error ? "bad" : powerHistory.loading ? "sync" : "";
  $("#overview-audio-track").textContent = audio.track?.name || "No soundtrack";
  $("#overview-audio-position").textContent = audio.track ? formatTrackTime(audio.position_s) : "--:--";
  $("#overview-audio-status").textContent = audio.playing
    ? "Playing continuously · loop on"
    : audio.paused
      ? "Paused"
      : audio.error || "Audio player unavailable";
  $("#overview-audio-status").className = `soundtrack-status ${audio.playing ? "ok" : audio.paused ? "warn" : "bad"}`;

  const issueBox = $("#overview-issues");
  issueBox.innerHTML = issues.length
    ? issues.map((issue) => `<div class="overview-issue ${issue.severity}">
        <i class="overview-issue-dot" aria-hidden="true"></i>
        <div><strong>${escapeHtml(issue.title)}</strong><span>${escapeHtml(issue.detail)}</span></div>
      </div>`).join("")
    : '<div class="overview-issue ok"><i class="overview-issue-dot" aria-hidden="true"></i><div><strong>No field issues detected</strong><span>Conductor, performers, firmware, and power telemetry look healthy.</span></div></div>';

  const counts = groupPerformerCounts(lanterns());
  const activeGroups = (state.groups || []).filter((group) => counts[Number(group.group_id)]?.total > 0);
  $("#overview-groups").innerHTML = activeGroups.length
    ? activeGroups.map((group) => {
        const groupId = Number(group.group_id);
        const config = patternForGroup(groupId);
        const members = counts[groupId];
        const playing = Number(config.brightness || 0) > 0 ? displayPatternName(config.pattern) : "Off";
        return `<div class="overview-group-row">
          <i class="overview-group-swatch" aria-hidden="true"></i>
          <div><strong>${escapeHtml(group.label || groupLabel(groupId))}</strong><span>${escapeHtml(playing)} · brightness ${Number(config.brightness || 0)} / 192</span></div>
          <span>${members.online} / ${members.total} online</span>
        </div>`;
      }).join("")
    : '<div class="empty-state">No lantern groups have members yet.</div>';
  renderFieldPreviewStatus();
  renderPowerHistoryChart();
}

function renderAudio() {
  const audio = audioState || state?.audio || {};
  const track = audio.track || null;
  const status = $("#audio-status");
  status.textContent = audio.playing ? "playing · loop on" : audio.paused ? "paused" : "unavailable";
  status.className = `chip ${audio.playing ? "sync" : audio.paused ? "active" : "bad"}`;
  $("#audio-track-name").textContent = track?.name || "No soundtrack selected";
  $("#audio-position").textContent = track ? formatTrackTime(audio.position_s) : "--:--";
  $("#audio-duration").textContent = track?.duration_s ? `/ ${formatTrackTime(track.duration_s)}` : "/ --:--";
  const progress = track?.duration_s ? Math.min(100, Math.max(0, Number(audio.position_s || 0) / Number(track.duration_s) * 100)) : 0;
  $("#audio-progress-bar").style.width = `${progress}%`;
  const toggle = $('[data-action="toggle-audio"]');
  toggle.textContent = audio.paused || !audio.playing ? "Play" : "Pause";
  toggle.disabled = !audio.available && !audio.paused;
  $('[data-action="restart-audio"]').disabled = !track || !audio.available;
  const error = $("#audio-error");
  error.hidden = !audio.error;
  error.textContent = audio.error || "";
  const tracks = Array.isArray(audio.tracks) ? audio.tracks : [];
  const trackListSignature = JSON.stringify({
    playing: audio.playing === true,
    selected: audio.selected_track || null,
    tracks: tracks.map((item) => [item.id, item.name, item.duration_s]),
  });
  if (trackListSignature !== audioTrackListSignature) {
    const trackList = $("#audio-tracks");
    const focusedTrack = document.activeElement?.dataset?.audioTrack || null;
    audioTrackListSignature = trackListSignature;
    trackList.innerHTML = tracks.length
      ? tracks.map((item) => {
          const selected = item.id === audio.selected_track;
          return `<button type="button" class="audio-track ${selected ? "selected" : ""}" data-audio-track="${escapeHtml(item.id)}">
            <div><strong>${escapeHtml(item.name)}</strong><span>${item.duration_s ? formatTrackTime(item.duration_s) : "Duration unavailable"} · loops continuously</span></div>
            <span class="audio-track-state">${selected ? audio.playing ? "Playing" : "Selected" : "Play this"}</span>
          </button>`;
        }).join("")
      : '<div class="empty-state">No MP3 files were found in the sound folder.</div>';
    $$('[data-audio-track]').forEach((button) => {
      button.addEventListener("click", async () => {
        if (button.dataset.audioTrack === audio.selected_track) return;
        try {
          const selectedAudio = await mutateAudio("/api/audio/select", {
            method: "POST",
            body: JSON.stringify({ track_id: button.dataset.audioTrack }),
          });
          acceptAudioState(selectedAudio);
          renderAudio();
          renderOverview();
          const notice = audioActionNotice(
            audioState,
            audioState.paused ? "Soundtrack selected; player remains paused" : `Playing ${audioState.track?.name || "soundtrack"}`,
          );
          toast(notice.message, notice.error);
        } catch (error) {
          toast(error.message, true);
        }
      });
    });
    if (focusedTrack) {
      [...$$('[data-audio-track]')]
        .find((button) => button.dataset.audioTrack === focusedTrack)
        ?.focus();
    }
  }
}

async function mutateAudio(path, options) {
  audioMutationGeneration += 1;
  audioMutationPending += 1;
  try {
    return await api(path, options);
  } finally {
    audioMutationPending -= 1;
  }
}

function audioRevision(audio) {
  const revision = Number(audio?.revision);
  return Number.isFinite(revision) ? revision : -1;
}

function isCurrentAudioState(candidate, current) {
  return Boolean(candidate) && (!current || audioRevision(candidate) >= audioRevision(current));
}

function acceptAudioState(candidate) {
  const current = audioState || state?.audio || null;
  if (!isCurrentAudioState(candidate, current)) return false;
  audioState = candidate;
  if (state) state.audio = candidate;
  return true;
}

function audioActionNotice(audio, successMessage) {
  return audio?.error
    ? { message: audio.error, error: true }
    : { message: successMessage, error: false };
}

function render() {
  if (!state) return;
  if (!selectedMac && lanterns().length) selectedMac = lanterns()[0].mac;
  if (!patternDraft || !isPatternDirty()) patternDraft = patternDraftFromState();

  $("#connection-status").textContent = state.conductor.connected ? "connected" : "disconnected";
  $("#online-performer-count").textContent = `${onlinePerformerCount(lanterns())}`;
  $("#field-count").textContent = `${state.summary.alive} / ${state.summary.total}`;
  const activePattern = activePatternState();
  renderGroupControls();
  $("#show-name").textContent = `${groupLabel(selectedGroup)}: ${displayPatternName(activePattern.pattern)}`;
  $("#attention-count").textContent = `${state.summary.attention} lights`;
  $("#sync-status").textContent = `sync ${state.conductor.sync}`;
  $("#table-sync-status").textContent = `sync ${state.conductor.sync}`;
  $("#brightness").value = patternDraft.brightness;
  $("#brightness-value").textContent = patternDraft.brightness;

  renderOverview();
  renderAudio();
  renderPatternControls();
  renderSavedPatterns();
  renderUploadedPatterns();
  renderMap();
  renderUnpositionedTray();
  renderRows();
  renderDetail();
  renderFirmware();
  renderReleases();
  renderRecovery();
  renderWifi();
  renderPowerMonitor();
  renderCalibration();
  renderPowerPolicy();
  renderOta();
  renderProvisioning();
  renderEvents();
  renderDetailVisibility();
}

function provisioningStateLabel(stateName) {
  return String(stateName || "unknown").replaceAll("_", " ");
}

function provisioningUpdateLabel(job) {
  const labels = {
    current: "Current",
    update_needed: "Update needed",
    unsupported: "Not a performer",
    unknown: "Unknown",
  };
  return labels[job.update_status] || provisioningStateLabel(job.state);
}

function renderProvisioning() {
  if (!provisioning) return;
  const available = provisioning.available === true;
  const session = provisioning.session || {};
  const autoUpdate = session.auto_update_enabled === true || session.active === true;
  const artifact = provisioning.artifact;
  const jobs = Array.isArray(provisioning.jobs)
    ? provisioning.jobs.filter((job) => job.connected).sort(comparePerformers)
    : [];
  const status = $("#provisioning-status");
  status.textContent = !available ? "unavailable" : autoUpdate ? "auto-update on" : "manual";
  status.className = `chip ${available && autoUpdate ? "sync" : !available ? "active" : ""}`;
  $("#provisioning-release").textContent = artifact
    ? `${artifact.release} · ${artifact.build}`
    : "No approved artifact";
  $("#provisioning-connected").textContent = `${Number(provisioning.connected || 0)} boards`;
  $("#provisioning-running").textContent = String(Number(provisioning.running || 0));
  if (!autoUpdate) $("#provisioning-workers").value = String(session.max_workers || 5);

  const notice = $("#provisioning-notice");
  notice.className = `flash-notice ${!available || provisioning.artifact_error ? "warn" : ""}`;
  notice.textContent = !available
    ? (provisioning.artifact_error || "Install and start the local USB provisioner on this host.")
    : provisioning.artifact_error
      ? `Artifact warning: ${provisioning.artifact_error}`
      : autoUpdate
        ? `Auto-update is on. New and outdated boards install automatically; up to ${Number(session.max_workers || 5)} run at once.`
        : "Auto-update is off. Use Install firmware on an individual board, or enable auto-update for the station.";

  const enableAuto = $('[data-action="enable-provisioning-auto-update"]');
  const disableAuto = $('[data-action="disable-provisioning-auto-update"]');
  enableAuto.hidden = autoUpdate;
  disableAuto.hidden = !autoUpdate;
  enableAuto.disabled = !available || !artifact || autoUpdate;
  disableAuto.disabled = !autoUpdate;
  $("#provisioning-workers").disabled = autoUpdate;

  $("#provisioning-jobs").innerHTML = jobs.length ? jobs.map((job) => {
    const slot = job.slot ? `Hub slot ${escapeHtml(job.slot)}` : "USB board";
    const board = job.node_id
      ? `BOARD #${escapeHtml(job.node_id)}`
      : escapeHtml(String(job.role || "Unknown").toUpperCase());
    const firmware = job.firmware_version && job.firmware_build
      ? `v${escapeHtml(job.firmware_version)} · ${escapeHtml(job.firmware_build)}${job.firmware_proto !== null && job.firmware_proto !== undefined ? ` · p${escapeHtml(job.firmware_proto)}` : ""}${job.firmware_dirty ? " · dirty" : ""}`
      : "Unknown";
    const target = artifact
      ? `Target v${escapeHtml(artifact.version)} · ${escapeHtml(artifact.build)}`
      : "Target unavailable";
    const active = ["inspecting", "probing", "reserving_id", "preparing", "flashing", "erasing", "assigning_id", "verifying", "rebooting"].includes(job.state);
    const differentBuild = job.update_status === "update_needed"
      && job.firmware_version === artifact?.version
      && job.firmware_build
      && job.firmware_build !== artifact?.build;
    const badge = active
      ? provisioningStateLabel(job.state)
      : differentBuild
        ? `Different build of ${escapeHtml(job.firmware_version)}`
        : provisioningUpdateLabel(job);
    const installable = job.connected
      && !active
      && job.update_status !== "current"
      && job.update_status !== "unsupported";
    const install = installable
      ? `<div class="flash-job-actions"><button type="button" data-provision-action="install" data-job-id="${escapeHtml(job.id)}" ${autoUpdate ? "disabled" : ""}>Install firmware</button></div>`
      : "";
    return `<article class="flash-job" data-state="${escapeHtml(job.state)}" data-update-status="${escapeHtml(job.update_status || "unknown")}">
      <div class="flash-job-head">
        <div class="flash-slot">${slot}</div>
        <div class="chip">${escapeHtml(badge)}</div>
      </div>
      <div class="flash-board-id">${board}</div>
      <div class="flash-job-message">${escapeHtml(job.message || "")}</div>
      <div class="flash-firmware"><strong>${firmware}</strong><span>${target}</span></div>
      ${job.mac ? `<div class="mono">${escapeHtml(job.mac)}</div>` : ""}
      ${install}
    </article>`;
  }).join("") : '<div class="empty-state">No FireBeetles detected. Plug boards into the powered hub.</div>';
}

function patternHueFromState() {
  const live = activePatternState();
  const params = live.params || {};
  // Firefly is positional: hue lives in p1 (p0 is the period).
  if (live.pattern === "Firefly" || live.pattern === "Fire Flicker") {
    return params.p1 !== undefined ? Number(params.p1) : PATTERN_DEFAULTS[live.pattern].hue;
  }
  // Ocean Wave and Wavefront are positional: their base hue lives in p3.
  if (live.pattern === "Ocean Wave" || live.pattern === "Wavefront") {
    return params.p3 !== undefined ? Number(params.p3) : PATTERN_DEFAULTS[live.pattern].hue;
  }
  if (params.hue !== undefined) return Number(params.hue);
  if ((live.pattern === "Glow" || live.pattern === "Pulse") && params.p0 !== undefined) {
    return Number(params.p0);
  }
  return 40;
}

function patternSaturationFromState() {
  const live = activePatternState();
  const params = live.params || {};
  if (live.pattern === "Firefly" || live.pattern === "Fire Flicker") {
    if (live.pattern === "Firefly" && (Number(params.p3 || 0) & FIREFLY_CHORUS_MARKER)) {
      return Math.min(100, Number(params.p3) & 0x7f);
    }
    if (colorValuePresent(params.p2) && params.p3 !== undefined) return Math.min(100, Number(params.p3));
    return params.p3 !== undefined ? Number(params.p3) : PATTERN_DEFAULTS[live.pattern].saturation;
  }
  if ((live.pattern === "Ocean Wave" || live.pattern === "Wavefront") && colorValuePresent(params.p2)) {
    return Math.round(((Number(params.p1) >> 10) & 0x3f) * 100 / 63);
  }
  if ((live.pattern === "Glow" || live.pattern === "Pulse") &&
      colorValuePresent(params.p2) && params.p1 !== undefined) {
    return Math.min(100, Number(params.p1));
  }
  if (params.saturation !== undefined) return Number(params.saturation);
  if ((live.pattern === "Glow" || live.pattern === "Pulse") && params.p1 !== undefined) {
    return Number(params.p1);
  }
  return 100;
}

function patternValueFromState() {
  const live = activePatternState();
  const params = live.params || {};
  if ((live.pattern === "Firefly" || live.pattern === "Fire Flicker") && colorValuePresent(params.p2)) {
    return (Number(params.p2) >> 7) & 0xff;
  }
  if ((live.pattern === "Ocean Wave" || live.pattern === "Wavefront") && colorValuePresent(params.p2)) {
    return Math.round(((Number(params.p2) >> 9) & 0x3f) * 255 / 63);
  }
  if ((live.pattern === "Glow" || live.pattern === "Pulse") &&
      colorValuePresent(params.p2)) {
    return Number(params.p2) & 0xff;
  }
  return 255;
}

function patternPeriodFromState() {
  const live = activePatternState();
  const params = live.params || {};
  if (params.period !== undefined) return Number(params.period);
  if ((live.pattern === "Sweep" || live.pattern === "Wavefront" || live.pattern === "Palette Drift" || live.pattern === "Firefly" || live.pattern === "Fire Flicker" || live.pattern === "Ocean Wave" || live.pattern === "Pond Ripple") && params.p0 !== undefined) {
    return Number(params.p0);
  }
  return PATTERN_DEFAULTS[live.pattern]?.period || 4000;
}

function patternScatterFromState() {
  const live = activePatternState();
  const params = live.params || {};
  if (live.pattern === "Firefly" && params.p2 !== undefined) {
    if (colorValuePresent(params.p2)) return Number(params.p2) & FIREFLY_SCATTER_MASK;
    return Number(params.p2);
  }
  return PATTERN_DEFAULTS.Firefly.scatter;
}

function patternChorusFromState() {
  const live = activePatternState();
  const params = live.params || {};
  if (live.pattern === "Firefly") {
    if (params.chorus !== undefined) return Number(params.chorus);
    if (Number(params.p3 || 0) & FIREFLY_CHORUS_MARKER) {
      return (Number(params.p3) >> 7) & 0xff;
    }
  }
  return PATTERN_DEFAULTS.Firefly.chorus;
}

function patternTextureFromState() {
  const live = activePatternState();
  const params = live.params || {};
  if (live.pattern === "Fire Flicker" && params.p2 !== undefined) {
    if (colorValuePresent(params.p2)) return Number(params.p2) & FIREFLY_SCATTER_MASK;
    return Number(params.p2);
  }
  return PATTERN_DEFAULTS["Fire Flicker"].texture;
}

function patternFire2012ControlFromState(key, slot) {
  const live = activePatternState();
  const params = live.params || {};
  if (live.pattern === "Fire2012") {
    if (params[key] !== undefined) return Number(params[key]);
    if (params[`p${slot}`] !== undefined) return Number(params[`p${slot}`]);
  }
  return PATTERN_DEFAULTS.Fire2012[key];
}

function patternAngleFromState() {
  const live = activePatternState();
  const params = live.params || {};
  if ((live.pattern === "Ocean Wave" || live.pattern === "Wavefront") && params.p2 !== undefined) {
    return colorValuePresent(params.p2) ? Number(params.p2) & OCEAN_ANGLE_MASK : Number(params.p2);
  }
  return PATTERN_DEFAULTS[live.pattern]?.angle ?? PATTERN_DEFAULTS["Ocean Wave"].angle;
}

function patternFrontWidthFromState() {
  const live = activePatternState();
  const params = live.params || {};
  if (live.pattern === "Wavefront") {
    if (params.front_width !== undefined) return Number(params.front_width);
    if (params.p1 !== undefined) {
      return colorValuePresent(params.p2) ? Number(params.p1) & OCEAN_WAVELENGTH_MASK : Number(params.p1);
    }
  }
  return PATTERN_DEFAULTS.Wavefront.frontWidth;
}

function patternCenterFromState(axis) {
  const live = activePatternState();
  const params = live.params || {};
  if (live.pattern === "Pond Ripple") {
    const key = axis === "x" ? "p2" : "p3";
    if (params[key] !== undefined) return Number(params[key]);
  }
  return axis === "x"
    ? PATTERN_DEFAULTS["Pond Ripple"].centerX
    : PATTERN_DEFAULTS["Pond Ripple"].centerY;
}

function patternWavelengthFromState() {
  const live = activePatternState();
  const params = live.params || {};
  if (params.wavelength !== undefined) return Number(params.wavelength);
  if ((live.pattern === "Sweep" || live.pattern === "Ocean Wave" || live.pattern === "Pond Ripple") && params.p1 !== undefined) {
    if (live.pattern === "Ocean Wave" && colorValuePresent(params.p2)) {
      return Number(params.p1) & OCEAN_WAVELENGTH_MASK;
    }
    return Number(params.p1);
  }
  return PATTERN_DEFAULTS[live.pattern]?.wavelength ?? PATTERN_DEFAULTS.Sweep.wavelength;
}

function patternSpatialFromState() {
  const live = activePatternState();
  const params = live.params || {};
  if (params.spatial !== undefined) return Number(params.spatial);
  if (live.pattern === "Palette Drift" && params.p1 !== undefined) return Number(params.p1);
  return PATTERN_DEFAULTS["Palette Drift"].spatial;
}

function patternDraftFromState() {
  const live = activePatternState();
  const defaults = PATTERN_DEFAULTS[live.pattern] || PATTERN_DEFAULTS.Pulse;
  return {
    pattern: live.pattern,
    brightness: Number(live.brightness),
    hue: patternHueFromState(),
    saturation: patternSaturationFromState(),
    value: patternValueFromState(),
    period: patternPeriodFromState() || defaults.period,
    wavelength: patternWavelengthFromState() || defaults.wavelength,
    spatial: patternSpatialFromState(),
    scatter: patternScatterFromState(),
    chorus: patternChorusFromState(),
    texture: patternTextureFromState(),
    angle: patternAngleFromState(),
    frontWidth: patternFrontWidthFromState(),
    speed: patternFire2012ControlFromState("speed", 0),
    cooling: patternFire2012ControlFromState("cooling", 1),
    sparking: patternFire2012ControlFromState("sparking", 2),
    centerX: patternCenterFromState("x"),
    centerY: patternCenterFromState("y"),
  };
}

function patternDraftForSelection(pattern) {
  const defaults = PATTERN_DEFAULTS[pattern] || PATTERN_DEFAULTS.Pulse;
  return {
    pattern,
    brightness: Number(patternDraft?.brightness ?? activePatternState()?.brightness ?? 48),
    hue: Number(defaults.hue),
    saturation: Number(defaults.saturation),
    value: Number(defaults.value),
    period: Number(defaults.period),
    wavelength: Number(defaults.wavelength),
    spatial: Number(defaults.spatial),
    scatter: Number(defaults.scatter),
    chorus: Number(defaults.chorus ?? PATTERN_DEFAULTS.Firefly.chorus),
    texture: Number(defaults.texture ?? PATTERN_DEFAULTS["Fire Flicker"].texture),
    angle: Number(defaults.angle),
    frontWidth: Number(defaults.frontWidth ?? PATTERN_DEFAULTS.Wavefront.frontWidth),
    speed: Number(defaults.speed ?? PATTERN_DEFAULTS.Fire2012.speed),
    cooling: Number(defaults.cooling ?? PATTERN_DEFAULTS.Fire2012.cooling),
    sparking: Number(defaults.sparking ?? PATTERN_DEFAULTS.Fire2012.sparking),
    centerX: Number(defaults.centerX ?? PATTERN_DEFAULTS["Pond Ripple"].centerX),
    centerY: Number(defaults.centerY ?? PATTERN_DEFAULTS["Pond Ripple"].centerY),
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
      p3: fireflyChorusPack(draft.saturation ?? 85, draft.chorus ?? 36),
    };
  }
  if (draft.pattern === "Fire Flicker") {
    // Positional on the wire; p2 packs sRGB value with per-pixel texture depth.
    return {
      p0: Number(draft.period),
      p1: Number(draft.hue),
      p2: fireflyMetaPack(draft.texture ?? 85, draft.value ?? 255),
      p3: Number(draft.saturation ?? 95),
    };
  }
  if (draft.pattern === "Fire2012") {
    return {
      p0: Number(draft.speed ?? 30),
      p1: Number(draft.cooling ?? 55),
      p2: Number(draft.sparking ?? 120),
      p3: 0,
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
  if (draft.pattern === "Wavefront") {
    return {
      p0: Number(draft.period),
      p1: oceanWavelengthSaturationPack(draft.frontWidth ?? 28, draft.saturation ?? 90),
      p2: oceanAngleValuePack(draft.angle ?? 0, draft.value ?? 255),
      p3: Number(draft.hue),
    };
  }
  if (draft.pattern === "Pond Ripple") {
    return {
      p0: Number(draft.period),
      p1: Number(draft.wavelength),
      p2: Number(draft.centerX),
      p3: Number(draft.centerY),
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
  if (pattern === "Uploaded Pattern") return ["pattern", "brightness", "hue", "saturation", "value"];
  if (pattern === "Pulse" || pattern === "Glow") return ["pattern", "brightness", "hue", "saturation", "value"];
  if (pattern === "Sweep") return ["pattern", "brightness", "period", "wavelength"];
  if (pattern === "Palette Drift") return ["pattern", "brightness", "period", "spatial"];
  if (pattern === "Firefly") return ["pattern", "brightness", "period", "hue", "saturation", "value", "scatter", "chorus"];
  if (pattern === "Fire Flicker") return ["pattern", "brightness", "period", "hue", "saturation", "value", "texture"];
  if (pattern === "Fire2012") return ["pattern", "brightness", "speed", "cooling", "sparking"];
  if (pattern === "Ocean Wave") return ["pattern", "brightness", "period", "wavelength", "angle", "hue", "saturation", "value"];
  if (pattern === "Wavefront") return ["pattern", "brightness", "period", "frontWidth", "angle", "hue", "saturation", "value"];
  if (pattern === "Pond Ripple") return ["pattern", "brightness", "period", "wavelength", "centerX", "centerY"];
  return ["pattern", "brightness"];
}

function patternFirmwareReady(pattern) {
  if (pattern !== "Pond Ripple" && pattern !== "Uploaded Pattern") return true;
  const features = state?.conductor?.firmware?.features;
  const firmware = state?.summary?.firmware;
  return Number(firmware?.expected) > 0
    && firmware?.consistent === true
    && Number(firmware?.matching) === Number(firmware?.expected)
    && Number(firmware?.seen) === Number(firmware?.expected)
    && Array.isArray(features)
    && features.includes(pattern === "Pond Ripple" ? "pond_ripple" : "uploaded_patterns_v1");
}

function isPatternDirty() {
  if (!state || !patternDraft) return false;
  if (patternDraft.pattern === "Uploaded Pattern") return true;
  const live = patternDraftFromState();
  return relevantPatternFields(patternDraft.pattern).some((field) => {
    if (field === "pattern") return patternDraft.pattern !== live.pattern;
    return Number(patternDraft[field]) !== Number(live[field]);
  });
}

function customBuilderSettingsFromForm() {
  return {
    motion: $("#custom-pattern-motion").value,
    hue: Number(patternDraft?.hue ?? CUSTOM_BUILDER_DEFAULTS.hue),
    saturation: Number(patternDraft?.saturation ?? CUSTOM_BUILDER_DEFAULTS.saturation),
    periodMs: Number($("#custom-pattern-period").value),
    wavelength: Number($("#custom-pattern-wavelength").value) / 100,
    direction: Number($("#custom-pattern-direction").value),
    centerX: Number($("#custom-pattern-center-x").value) / 100,
    centerY: Number($("#custom-pattern-center-y").value) / 100,
    minValue: Number($("#custom-pattern-min-value").value),
    maxValue: Number($("#custom-pattern-max-value").value),
  };
}

function setCustomPatternSourceMode(mode) {
  customPatternSourceMode = mode === "advanced" ? "advanced" : "guided";
  const chip = $("#custom-pattern-source-mode");
  chip.textContent = customPatternSourceMode === "advanced" ? "Advanced source" : "Guided controls";
  chip.className = `chip ${customPatternSourceMode === "advanced" ? "active" : "sync"}`;
}

function syncCustomProgramFromBuilder({ resetStatus = false } = {}) {
  if (patternDraft?.pattern !== "Uploaded Pattern") return;
  setCustomPatternSourceMode("guided");
  $("#uploaded-pattern-json").value = JSON.stringify(
    customBuilderProgram(customBuilderSettingsFromForm()),
    null,
    2,
  );
  if (resetStatus) $("#uploaded-pattern-status").textContent = "Ready to validate";
}

function setCustomBuilderForm(settings) {
  $("#custom-pattern-motion").value = settings.motion;
  $("#custom-pattern-period").value = String(settings.periodMs);
  $("#custom-pattern-wavelength").value = String(Math.round(settings.wavelength * 100));
  $("#custom-pattern-direction").value = String(settings.direction);
  $("#custom-pattern-center-x").value = String(Math.round(settings.centerX * 100));
  $("#custom-pattern-center-y").value = String(Math.round(settings.centerY * 100));
  $("#custom-pattern-min-value").value = String(settings.minValue);
  $("#custom-pattern-max-value").value = String(settings.maxValue);
  if (patternDraft) {
    patternDraft.hue = settings.hue;
    patternDraft.saturation = settings.saturation;
    patternDraft.value = 255;
  }
}

function resetCustomPatternBuilder() {
  setCustomBuilderForm(CUSTOM_BUILDER_DEFAULTS);
  $("#uploaded-pattern-name").value = "Blue traveling wave";
  $("#custom-pattern-advanced").open = false;
  syncCustomProgramFromBuilder({ resetStatus: true });
}

function renderCustomPatternBuilder() {
  const motion = $("#custom-pattern-motion").value;
  const periodMs = Number($("#custom-pattern-period").value);
  const wavelength = Number($("#custom-pattern-wavelength").value);
  const direction = Number($("#custom-pattern-direction").value);
  const centerX = Number($("#custom-pattern-center-x").value);
  const centerY = Number($("#custom-pattern-center-y").value);
  const minValue = Number($("#custom-pattern-min-value").value);
  const maxValue = Math.max(minValue, Number($("#custom-pattern-max-value").value));
  $("#custom-pattern-max-value").value = String(maxValue);
  $("#custom-pattern-period-value").textContent = (periodMs / 1000).toFixed(1);
  $("#custom-pattern-wavelength-value").textContent = (wavelength / 100).toFixed(2);
  $("#custom-pattern-direction-value").textContent = String(direction);
  $("#custom-pattern-center-x-value").textContent = (centerX / 100).toFixed(2);
  $("#custom-pattern-center-y-value").textContent = (centerY / 100).toFixed(2);
  $("#custom-pattern-min-value-label").textContent = String(minValue);
  $("#custom-pattern-max-value-label").textContent = String(maxValue);
  $('[data-custom-param="period"]').hidden = motion === "steady-glow";
  $('[data-custom-param="wavelength"]').hidden = !(motion === "traveling-wave" || motion === "center-ripple");
  $('[data-custom-param="direction"]').hidden = motion !== "traveling-wave";
  $('[data-custom-param="center"]').hidden = motion !== "center-ripple";
  $('[data-custom-param="min-value"]').hidden = motion === "steady-glow";
  if (customPatternSourceMode === "guided") syncCustomProgramFromBuilder();
}

function loadCustomPatternEditor(item) {
  patternDraft = patternDraftForSelection("Uploaded Pattern");
  patternDraft.brightness = Number(item.brightness);
  $("#uploaded-pattern-name").value = item.name;
  const settings = customBuilderSettingsFromProgram(item.program);
  if (settings) {
    setCustomBuilderForm(settings);
    $("#custom-pattern-advanced").open = false;
    setCustomPatternSourceMode("guided");
    syncCustomProgramFromBuilder();
  } else {
    if (typeof item.program?.hue === "number") patternDraft.hue = (item.program.hue * 360 + 360) % 360;
    if (typeof item.program?.saturation === "number") patternDraft.saturation = item.program.saturation * 100;
    $("#uploaded-pattern-json").value = JSON.stringify(item.program, null, 2);
    $("#custom-pattern-advanced").open = true;
    setCustomPatternSourceMode("advanced");
  }
  $("#uploaded-pattern-status").textContent = `${item.compiled.instructions} instructions · ${item.compiled.bytes} bytes`;
  renderPatternControls();
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
  $("#pattern-chorus").value = patternDraft.chorus ?? 36;
  $("#chorus-value").textContent = String(Number(patternDraft.chorus ?? 36));
  $("#pattern-front-width").value = patternDraft.frontWidth ?? 28;
  $("#front-width-value").textContent = (Number(patternDraft.frontWidth ?? 28) / 100).toFixed(2);
  $("#pattern-texture").value = patternDraft.texture ?? 85;
  $("#texture-value").textContent = String(Number(patternDraft.texture ?? 85));
  $("#pattern-angle").value = patternDraft.angle ?? 45;
  $("#angle-value").textContent = String(Number(patternDraft.angle ?? 45));
  $("#pattern-fire-speed").value = patternDraft.speed ?? 30;
  $("#fire-speed-value").textContent = String(Number(patternDraft.speed ?? 30));
  $("#pattern-cooling").value = patternDraft.cooling ?? 55;
  $("#cooling-value").textContent = String(Number(patternDraft.cooling ?? 55));
  $("#pattern-sparking").value = patternDraft.sparking ?? 120;
  $("#sparking-value").textContent = String(Number(patternDraft.sparking ?? 120));
  $("#pattern-center-x").value = patternDraft.centerX ?? 500;
  $("#center-x-value").textContent = (Number(patternDraft.centerX ?? 500) / 1000).toFixed(2);
  $("#pattern-center-y").value = patternDraft.centerY ?? 500;
  $("#center-y-value").textContent = (Number(patternDraft.centerY ?? 500) / 1000).toFixed(2);
  const draftHex = hueSaturationValueToHex(
    patternDraft.hue,
    patternDraft.saturation ?? 100,
    patternDraft.value ?? 255,
  );
  $$("#color-presets button").forEach((button) => {
    button.classList.toggle("active", hexApproxEqual(button.dataset.hex, draftHex));
  });
  const isColorPattern = patternDraft.pattern === "Pulse" || patternDraft.pattern === "Glow" || patternDraft.pattern === "Firefly" || patternDraft.pattern === "Fire Flicker" || patternDraft.pattern === "Ocean Wave" || patternDraft.pattern === "Wavefront" || patternDraft.pattern === "Uploaded Pattern";
  $("#color-presets").hidden = !isColorPattern;
  $("#color-wheel-row").hidden = !isColorPattern;
  if (isColorPattern) {
    const hex = hueSaturationValueToHex(
      patternDraft.hue,
      patternDraft.saturation ?? 100,
      patternDraft.value ?? 255,
    );
    $("#pattern-color-picker").value = hex;
    const wheelPosition = colorWheelPosition(patternDraft.hue, patternDraft.saturation ?? 100);
    const wheel = $("#pattern-color-wheel");
    const thumb = $("#color-wheel-thumb");
    thumb.style.left = `${wheelPosition.left}%`;
    thumb.style.top = `${wheelPosition.top}%`;
    thumb.style.setProperty("--selected-color", hex);
    wheel.setAttribute("aria-valuenow", String(Math.round(Number(patternDraft.hue))));
    wheel.setAttribute("aria-valuetext", `Hue ${Math.round(Number(patternDraft.hue))} degrees, saturation ${Math.round(Number(patternDraft.saturation ?? 100))} percent`);
    $("#color-wheel-value").textContent = `Hue ${Math.round(Number(patternDraft.hue))}° · Saturation ${Math.round(Number(patternDraft.saturation ?? 100))}%`;
  }
  $('[data-param-group="period"]').hidden = !(patternDraft.pattern === "Sweep" || patternDraft.pattern === "Wavefront" || patternDraft.pattern === "Palette Drift" || patternDraft.pattern === "Firefly" || patternDraft.pattern === "Fire Flicker" || patternDraft.pattern === "Ocean Wave" || patternDraft.pattern === "Pond Ripple");
  $('[data-param-group="wavelength"]').hidden = !(patternDraft.pattern === "Sweep" || patternDraft.pattern === "Ocean Wave" || patternDraft.pattern === "Pond Ripple");
  $('[data-param-group="spatial"]').hidden = patternDraft.pattern !== "Palette Drift";
  $('[data-param-group="scatter"]').hidden = patternDraft.pattern !== "Firefly";
  $('[data-param-group="chorus"]').hidden = patternDraft.pattern !== "Firefly";
  $('[data-param-group="front-width"]').hidden = patternDraft.pattern !== "Wavefront";
  $('[data-param-group="texture"]').hidden = patternDraft.pattern !== "Fire Flicker";
  $('[data-param-group="angle"]').hidden = !(patternDraft.pattern === "Ocean Wave" || patternDraft.pattern === "Wavefront");
  $('[data-param-group="fire-speed"]').hidden = patternDraft.pattern !== "Fire2012";
  $('[data-param-group="cooling"]').hidden = patternDraft.pattern !== "Fire2012";
  $('[data-param-group="sparking"]').hidden = patternDraft.pattern !== "Fire2012";
  $('[data-param-group="ripple-center"]').hidden = patternDraft.pattern !== "Pond Ripple";
  const uploaded = patternDraft.pattern === "Uploaded Pattern";
  $("#uploaded-pattern-editor").hidden = !uploaded;
  if (uploaded && !$("#uploaded-pattern-json").value.trim()) {
    $("#uploaded-pattern-json").value = JSON.stringify(DEFAULT_UPLOADED_PROGRAM, null, 2);
  }
  if (uploaded) renderCustomPatternBuilder();
  const firmwareReady = patternFirmwareReady(patternDraft.pattern);
  $("#pattern-firmware-warning").textContent = patternDraft.pattern === "Uploaded Pattern"
    ? "Custom Pattern is paused until every placed lantern is online and running compatible firmware. Existing patterns will remain active."
    : "Ripple broadcast is paused until every placed lantern is online and running ripple-capable firmware. Finish reconciliation in Firmware first.";
  $("#pattern-firmware-warning").hidden = firmwareReady;
  const changeButton = $('[data-action="broadcast"]');
  changeButton.disabled = !isPatternDirty() || !firmwareReady;
  changeButton.textContent = uploaded ? "Verify & run custom pattern" : "Change pattern";
  changeButton.ariaDisabled = String(changeButton.disabled);
  const groupOffButton = $('[data-action="turn-off-group"]');
  const selectedGroupIsOff = Number(activePatternState()?.brightness || 0) === 0;
  groupOffButton.textContent = `${selectedGroupIsOff ? "Turn on" : "Turn off"} ${groupLabel(selectedGroup)}`;
  groupOffButton.disabled = false;
  groupOffButton.ariaDisabled = String(groupOffButton.disabled);
  const restoreButton = $('[data-action="restore-blackout"]');
  restoreButton.disabled = state?.blackout?.restore_available !== true;
  restoreButton.ariaDisabled = String(restoreButton.disabled);
}

function renderUploadedPatterns() {
  const list = $("#uploaded-pattern-list");
  const count = $("#uploaded-pattern-count");
  if (!list || !count) return;
  count.textContent = `${uploadedPatterns.length} saved`;
  if (!uploadedPatterns.length) {
    list.innerHTML = '<div class="empty-state">No custom patterns yet.</div>';
    return;
  }
  const firmwareReady = patternFirmwareReady("Uploaded Pattern");
  list.innerHTML = uploadedPatterns.map((item) => `
    <div class="saved-pattern-row uploaded-pattern-row">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <span>Custom Pattern · brightness ${escapeHtml(String(item.brightness))}</span>
        <small>${escapeHtml(item.compiled?.program_label || "uncompiled")} · ${escapeHtml(String(item.compiled?.instructions || 0))} instructions</small>
      </div>
      <button data-uploaded-action="load" data-pattern-id="${escapeHtml(item.id)}">Edit</button>
      <button class="primary" data-uploaded-action="broadcast" data-pattern-id="${escapeHtml(item.id)}" ${firmwareReady ? "" : "disabled"}>Verify &amp; run</button>
      <button class="danger" data-uploaded-action="delete" data-pattern-id="${escapeHtml(item.id)}">Delete</button>
    </div>
  `).join("");
}

function uploadedDraftPayload() {
  if (customPatternSourceMode === "guided") syncCustomProgramFromBuilder();
  let program;
  try {
    program = JSON.parse($("#uploaded-pattern-json").value);
  } catch (_error) {
    throw new Error("Custom pattern source is not valid JSON");
  }
  return {
    name: $("#uploaded-pattern-name").value.trim() || "Custom pattern",
    brightness: Number(patternDraft?.brightness ?? 48),
    program,
  };
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
    const details = `${escapeHtml(displayPatternName(item.pattern))} · bri ${escapeHtml(String(item.brightness))}`;
    const params = Object.entries(item.params || {})
      .map(([key, value]) => `${key}=${value}`)
      .join(" ");
    const id = escapeHtml(item.id);
    const broadcastDisabled = !patternFirmwareReady(item.pattern);
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
        <button class="primary" data-pattern-action="broadcast-saved" data-pattern-id="${id}" ${broadcastDisabled ? 'disabled title="Finish firmware reconciliation before broadcasting Ripple"' : ""}>Broadcast</button>
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

function groupOptions(selectedGroupId) {
  const counts = groupPerformerCounts(lanterns());
  return Array.from({ length: GROUP_COUNT }, (_, groupId) =>
    `<option value="${groupId}" ${groupId === selectedGroupId ? "selected" : ""}>${escapeHtml(`${groupLabel(groupId)} (${counts[groupId].online} online / ${counts[groupId].total})`)}</option>`
  ).join("");
}

function updateGroupNameDirtyState() {
  const input = $("#group-name");
  const saveButton = $('[data-action="save-group-name"]');
  if (!input || !saveButton) return;
  saveButton.disabled = groupNameBaseline === null || input.value.trim() === groupNameBaseline;
}

function renderGroupControls() {
  const patternSelect = $("#pattern-group");
  patternSelect.innerHTML = groupOptions(selectedGroup);
  patternSelect.value = String(selectedGroup);
  const input = $("#group-name");
  const liveName = groupName(selectedGroup);
  const dirty = groupNameBaseline !== null && input.value.trim() !== groupNameBaseline;
  if (groupNameBaseline === null || !dirty) {
    groupNameBaseline = liveName;
    input.value = liveName;
  }
  updateGroupNameDirtyState();
}

function ledCountSafe(value) {
  const count = Number(value || 16);
  return LED_COUNTS.includes(count) ? count : 16;
}

function ledCountOptions(selectedCount) {
  return LED_COUNTS.map((count) =>
    `<option value="${count}" ${count === selectedCount ? "selected" : ""}>${count}</option>`
  ).join("");
}

async function assignLanternGroup(mac, groupId, control = null) {
  if (!mac) return;
  if (control) control.disabled = true;
  try {
    const ack = await api(`/api/lanterns/${encodeURIComponent(mac)}/group`, {
      method: "POST",
      body: JSON.stringify({ group_id: groupId }),
    });
    updateLanternState(mac, { group_id: groupId, group: groupLabel(groupId) });
    render();
    toast(ack.message);
  } catch (error) {
    toast(error.message, true);
    render();
  }
}

async function assignLanternLedCount(mac, ledCount, control = null) {
  if (!mac) return;
  if (control) control.disabled = true;
  try {
    const ack = await api(`/api/lanterns/${encodeURIComponent(mac)}/led-count`, {
      method: "POST",
      body: JSON.stringify({ led_count: ledCount }),
    });
    updateLanternState(mac, { led_count: ledCount });
    render();
    toast(ack.message);
  } catch (error) {
    toast(error.message, true);
    render();
  }
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
    const groupId = Math.max(0, Math.min(GROUP_COUNT - 1, Number(lantern.group_id || 0)));
    const ledCount = ledCountSafe(lantern.led_count);
    return `<tr data-mac="${lantern.mac}" class="${lantern.mac === selectedMac ? "selected" : ""}">
      <td><strong>${escapeHtml(lantern.label)}</strong><br><span class="mono">${escapeHtml(lantern.mac)}</span></td>
      <td><select class="table-group-select" data-group-mac="${escapeHtml(lantern.mac)}" aria-label="Group for ${escapeHtml(lantern.label)}">${groupOptions(groupId)}</select></td>
      <td><select class="table-led-select" data-led-mac="${escapeHtml(lantern.mac)}" aria-label="LED count for ${escapeHtml(lantern.label)}">${ledCountOptions(ledCount)}</select></td>
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
  $$("#lantern-rows [data-group-mac]").forEach((select) => {
    select.addEventListener("click", (event) => event.stopPropagation());
    select.addEventListener("change", async (event) => {
      event.stopPropagation();
      const groupId = Math.max(0, Math.min(GROUP_COUNT - 1, Number(select.value || 0)));
      await assignLanternGroup(select.dataset.groupMac, groupId, select);
    });
  });
  $$("#lantern-rows [data-led-mac]").forEach((select) => {
    select.addEventListener("click", (event) => event.stopPropagation());
    select.addEventListener("change", async (event) => {
      event.stopPropagation();
      await assignLanternLedCount(select.dataset.ledMac, ledCountSafe(select.value), select);
    });
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
  const groupId = Number(lantern.group_id || 0);
  const ledCount = ledCountSafe(lantern.led_count);
  const groupPattern = patternForGroup(groupId);
  const moveLabel = isPositioned(lantern) ? "Move" : "Place";
  $("#detail-title").textContent = `${lantern.label} is ${isOk ? "healthy" : statusText(lantern)}`;
  $("#detail-title").className = isOk ? "" : "warn";
  $("#detail-summary").textContent = detailSummary(lantern);
  $("#lantern-group").innerHTML = groupOptions(groupId);
  $("#lantern-group").value = String(groupId);
  $("#lantern-led-count").value = String(ledCount);
  $("#detail-tech").innerHTML = [
    `MAC ${escapeHtml(lantern.mac)} · x=${fmt(lantern.x)} y=${fmt(lantern.y)} · status=${escapeHtml(statusText(lantern))}`,
    `firmware=${firmwareHtml(lantern.firmware)}`,
    `group=${escapeHtml(groupLabel(groupId))} · leds=${ledCount} · pattern=${escapeHtml(displayPatternName(groupPattern.pattern))} bri=${groupPattern.brightness} · seq=${state.conductor.seq}`,
    `power E=${fmt(lantern.power.wh)}Wh avg=${fmt(lantern.power.avg_w)}W · last report=${escapeHtml(lantern.power.last_report_label || "none")}`,
  ].join("<br>");
  $$('[data-action="move"]').forEach((button) => {
    button.innerHTML = `${moveLabel} <kbd>${isPositioned(lantern) ? "M" : "P"}</kbd>`;
  });
  document.body.classList.toggle("move-mode", movingLanternMac !== null);
  renderReplacePanel();
  renderSelectionRing();
  renderPlacementMarker();
}

function renderDetailVisibility() {
  const activeView = $(".tabs button.active")?.dataset.view;
  const sheet = $("#detail-sheet");
  const mapView = $("#view-map");
  if (activeView === "map" && sheet.parentElement !== mapView) {
    $(".map-locations-panel").before(sheet);
  } else if (activeView !== "map" && sheet.parentElement === mapView) {
    $("main").after(sheet);
  }
  sheet.hidden = !(activeView === "map" || activeView === "table");
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

function releaseCommitHtml(commit) {
  const value = String(commit || "");
  if (!value) return "commit unavailable";
  const label = shortHash(value);
  const url = commitUrl(value);
  return url
    ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`
    : escapeHtml(label);
}

function renderReleaseChanges(target, changes) {
  const items = Array.isArray(changes) ? changes : [];
  target.innerHTML = items.length
    ? items.map((change) => `<li>${escapeHtml(change)}</li>`).join("")
    : "<li>Release notes are not available for this version.</li>";
}

function renderReleases() {
  if (!releaseInfo) return;
  const control = releaseInfo.control || {};
  const firmware = releaseInfo.firmware || {};
  const controlRelease = control.release || {};
  const firmwareRelease = firmware.release || {};
  const desiredFirmwareRelease = firmware.desired_release || {};
  const controlOk = control.in_sync !== false;
  const firmwareConsistent = firmware.consistent !== false;
  const firmwareCoverageComplete = firmware.coverage_complete === true;
  const firmwareOk = firmware.in_sync !== false && firmwareConsistent && firmware.dirty !== true;

  $("#control-release-version").textContent = control.version ? `v${control.version}` : "unknown";
  $("#control-release-status").textContent = controlOk ? "deployed" : "drift";
  $("#control-release-status").className = `chip ${controlOk ? "sync" : "active"}`;
  $("#control-release-meta").innerHTML = `${releaseCommitHtml(control.commit)}${control.deployed_at ? ` · deployed ${escapeHtml(new Date(control.deployed_at).toLocaleString())}` : " · local/manual deployment"}`;
  $("#control-release-title").textContent = controlRelease.title || "Release notes unavailable";
  renderReleaseChanges($("#control-release-changes"), controlRelease.control_changes);

  $("#field-release-version").textContent = firmware.version ? `v${firmware.version}` : "no firmware online";
  const firmwareStatus = !firmware.version
    ? "offline"
    : firmware.dirty === true
      ? "dirty build"
      : !firmwareConsistent
        ? "mixed"
        : firmware.identity_in_sync === true && !firmwareCoverageComplete
          ? "deferred"
        : firmware.desired && !firmwareOk
          ? "update available"
          : "deployed";
  $("#field-release-status").textContent = firmwareStatus;
  $("#field-release-status").className = `chip ${firmwareOk ? "sync" : "active"}`;
  const matching = firmware.matching ?? 0;
  const expected = firmware.expected ?? state.summary.total;
  $("#field-release-meta").innerHTML = `${releaseCommitHtml(firmware.commit)} · ${escapeHtml(String(matching))} / ${escapeHtml(String(expected))} currently match`;
  $("#field-release-title").textContent = firmwareRelease.title || "Release notes unavailable";
  renderReleaseChanges($("#field-release-changes"), firmwareRelease.firmware_changes);
  const pendingFirmware = Boolean(firmware.desired_version && firmware.desired_version !== firmware.version);
  $("#field-release-pending").hidden = !pendingFirmware;
  if (pendingFirmware) {
    $("#field-release-pending-title").textContent = `Staged for v${firmware.desired_version}: ${desiredFirmwareRelease.title || "release notes unavailable"}`;
    renderReleaseChanges($("#field-release-pending-changes"), desiredFirmwareRelease.firmware_changes);
  }

  const allOk = controlOk && firmwareOk;
  $("#release-overall-status").textContent = allOk ? "in sync" : "attention";
  $("#release-overall-status").className = `chip ${allOk ? "sync" : "active"}`;
  const history = Array.isArray(releaseInfo.history) ? releaseInfo.history : [];
  const releaseSelect = $("#ota-release-select");
  if (releaseSelect) {
    const selected = releaseSelect.value || control.version || history[0]?.version || "";
    releaseSelect.innerHTML = history.map((release) => (
      `<option value="${escapeHtml(release.version)}">v${escapeHtml(release.version)} · ${escapeHtml(release.title)}</option>`
    )).join("");
    if ([...releaseSelect.options].some((option) => option.value === selected)) {
      releaseSelect.value = selected;
    }
  }
  $("#release-history-list").innerHTML = history.map((release) => `<details class="release-history-row">
    <summary><span><strong>v${escapeHtml(release.version)} · ${escapeHtml(release.title)}</strong><small>${escapeHtml(release.date)} · ${escapeHtml(String((release.control_changes || []).length))} control · ${escapeHtml(String((release.firmware_changes || []).length))} firmware</small></span></summary>
    <div class="release-history-detail">
      <div><strong>Web control plane</strong><ul class="release-changes">${(release.control_changes || []).map((change) => `<li>${escapeHtml(change)}</li>`).join("") || "<li>No control-plane changes.</li>"}</ul></div>
      <div><strong>Field firmware</strong><ul class="release-changes">${(release.firmware_changes || []).map((change) => `<li>${escapeHtml(change)}</li>`).join("") || "<li>No firmware changes.</li>"}</ul></div>
    </div>
  </details>`).join("") || '<div class="empty-state">No release history has been published.</div>';
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
  ].sort(comparePerformers);
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
  const allowChanges = wifi.allow_changes !== false;
  $("#wifi-ssid").disabled = !allowChanges;
  $("#wifi-password").disabled = !allowChanges;
  $('[data-action="join-wifi"]').disabled = !allowChanges;
  $('[data-action="start-hotspot"]').disabled = !allowChanges;
}

function renderPowerMonitor() {
  const monitor = state.power_monitor || {};
  const samples = Array.isArray(monitor.samples)
    ? [...monitor.samples].sort(comparePerformers)
    : [];
  const usable = Number(monitor.usable_sample_count || 0);
  const sampleCount = Number(monitor.sample_count || 0);
  const soc = monitor.soc_percent ?? monitor.estimated_node_soc_percent;
  const performerDraw = monitor.average_performer_draw_w ?? monitor.avg_node_w;
  const stale = Number(monitor.stale_count || 0);
  const bad = Number(monitor.implausible_count || 0);
  $("#power-monitor-status").textContent = sampleCount
    ? `${usable} / ${sampleCount} samples`
    : "no samples";
  $("#power-monitor-status").className = `ops-value ${usable ? "sync" : ""}`;
  $("#power-monitor-soc").textContent = soc === null || soc === undefined
    ? "--"
    : `${Number(soc).toFixed(1)}%`;
  $("#power-monitor-soc").className = `value ${soc === null || soc === undefined ? "" : soc < 25 ? "bad" : soc < 50 ? "warn" : "ok"}`;
  $("#power-monitor-draw").textContent = performerDraw === null || performerDraw === undefined
    ? "--"
    : `${Number(performerDraw).toFixed(2)} W`;
  $("#power-monitor-draw").className = `ops-value ${performerDraw === null || performerDraw === undefined ? "" : "sync"}`;
  $("#battery-capacity").value = monitor.battery_capacity_wh ?? 384;
  $("#battery-full-voltage").value = monitor.full_voltage ?? 14.4;

  const sampleBox = $("#power-samples");
  if (!samples.length) {
    sampleBox.innerHTML = `<div class="empty-state">No instrumented node has reported power yet.</div>`;
    return;
  }
  sampleBox.innerHTML = samples.map((sample) => {
    const classes = [sample.stale ? "warn" : "", sample.plausible === false ? "bad" : ""].filter(Boolean).join(" ");
    const socLabel = sample.soc_percent === null || sample.soc_percent === undefined ? "--" : `${Number(sample.soc_percent).toFixed(1)}%`;
    const voltage = sample.bus_v === null || sample.bus_v === undefined ? "--" : `${Number(sample.bus_v).toFixed(2)} V`;
    const drawLabel = sample.draw_source === "recent_average"
      ? `${fmt(sample.avg_w)} W recent avg`
      : `${fmt(sample.avg_w)} W now`;
    const detail = `${fmt(sample.used_since_full_wh)} Wh since full · ${drawLabel} · ${voltage}`;
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
      action: "Start the same staged firmware again. The update resumes from the verified image prefix; power-cycle only a performer that never checks back in.",
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
  return state?.recovery || {};
}

function renderOta() {
  const ota = state?.ota || {};
  const active = Boolean(ota.enabled);
  const ready = Boolean(ota.ready);
  const installing = Boolean(otaInstall?.running);
  const readyToActivate = otaInstall?.phase === "ready-to-activate";
  const expected = Number(ota.expected ?? state?.summary?.total ?? 0);
  const readyCount = Number(ota.ready_count ?? 0);
  const deferred = Number(ota.deferred ?? ota.missing ?? Math.max(0, expected - readyCount));
  const blockers = Array.isArray(ota.blocked) ? ota.blocked : [];
  const autoEnabled = otaInstall?.auto_update_enabled !== false;
  $("#ota-mode").textContent = installing ? "updating" : "field live";
  $("#ota-mode").className = `chip ${active ? "sync" : ""}`;
  $("#ota-readiness").textContent = `${readyCount} online · ${deferred} deferred`;
  $("#ota-readiness").className = `ops-value ${ready ? "ok" : "warn"}`;
  const retryTimeout = Number(otaInstall?.retry_timeout_s || 6 * 60 * 60);
  $("#ota-timeout").textContent = `${formatDuration(retryTimeout)} retry window`;
  $("#ota-timeout").className = "ops-value ok";
  $("#ota-blockers").textContent = !autoEnabled
    ? "Automatic updates are off. The field will not be interrupted unless you start an update manually."
    : blockers.length
    ? `Blocked: ${blockers.join(", ")}.`
    : deferred > 0
      ? "Ready. The show stays live; offline performers remain deferred until a later run."
      : "Ready. The show stays live while chunks are broadcast, repaired, and verified.";
  const controlVersion = releaseInfo?.control?.version;
  const companion = otaArtifact?.source === "release" && otaArtifact?.version === controlVersion;
  const targetLabel = !otaArtifact
    ? "Companion firmware unavailable"
    : companion
      ? `v${otaArtifact.version} companion firmware`
      : otaArtifact.source === "release"
        ? `v${otaArtifact.version} release override`
        : "Manual binary override";
  $("#ota-target").textContent = targetLabel;
  $("#ota-auto-note").textContent = autoEnabled
    ? "Old performers update automatically when they check in."
    : "Show lock is active; use Update field now for an explicit update.";
  const autoButton = $('[data-action="toggle-ota-auto-update"]');
  autoButton.textContent = `Automatic updates: ${autoEnabled ? "On" : "Off"}`;
  autoButton.classList.toggle("sync", autoEnabled);
  autoButton.classList.toggle("active", !autoEnabled);
  $("#ota-artifact").innerHTML = otaArtifact
    ? `${escapeHtml(otaArtifact.filename)} · ${formatBytes(otaArtifact.size)} · ${otaArtifact.chunks} chunks · sha256 <span class="mono">${escapeHtml(shortHash(otaArtifact.sha256))}</span>`
    : "No verified companion image is available on this control plane.";
  renderOtaProgress();
  renderOtaNodes();
  const fileInput = $("#ota-file");
  const protocolInput = $("#ota-file-protocol");
  const uploadButton = $('[data-action="upload-ota-artifact"]');
  fileInput.disabled = installing || readyToActivate || otaArtifactUploading;
  protocolInput.disabled = installing || readyToActivate || otaArtifactUploading;
  uploadButton.disabled = installing || readyToActivate || otaArtifactUploading || !fileInput.files?.length;
  const installButton = $('[data-action="install-ota"]');
  installButton.textContent = readyToActivate
    ? "Install firmware on field"
    : otaInstall?.phase === "paused"
      ? "Resume field update"
      : "Update field now";
  installButton.disabled = installing || otaArtifactUploading || (!readyToActivate && (!otaReadyForInstall() || !otaArtifact));
  $('[data-action="pause-ota"]').hidden = !installing;
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
  const targetCount = Number(otaInstall?.target_count || 0);
  const deferredCount = Number(otaInstall?.deferred_count || 0);
  const paused = otaInstall?.phase === "paused";
  const readyToActivate = otaInstall?.phase === "ready-to-activate";
  const show = running || complete || error || paused || readyToActivate;
  progress.hidden = !show;
  if (!show) return;

  const sent = Number(otaInstall?.chunks_sent || 0);
  const total = Math.max(0, Number(otaInstall?.chunks_total || 0));
  const bytesSent = Number(otaInstall?.bytes_sent || 0);
  const size = Number(otaInstall?.size || 0);
  const percent = total > 0 ? Math.min(100, Math.round((sent / total) * 100)) : 0;
  const phase = String(otaInstall?.phase || "starting");
  const repairChunks = Number(otaInstall?.repair_chunks || 0);
  const activated = Array.isArray(otaInstall?.activated_macs) ? otaInstall.activated_macs.length : 0;
  const staged = Array.isArray(otaInstall?.staged_macs) ? otaInstall.staged_macs.length : 0;
  const activeMac = otaInstall?.active_mac;
  const phaseLabels = {
    starting: "Preparing firmware update",
    waiting: "Retrying conductor connection",
    broadcasting: "Broadcasting firmware while the field stays live",
    repairing: "Repairing missed chunks",
    staging: "Verifying uploaded images",
    staged: "Firmware uploaded to performers",
    "ready-to-activate": "Firmware uploaded and ready to install",
    "preparing-activation": "Rechecking uploaded firmware",
    activating: "Restarting performers one at a time",
    "activating-conductor": "Restarting the conductor",
    paused: "Firmware update paused",
  };
  const label = error
    ? `Install failed: ${error}`
    : readyToActivate
      ? `${staged || targetCount} / ${targetCount} performers uploaded · ready to install`
      : complete
        ? `Firmware installed on ${targetCount || "online"} performer${targetCount === 1 ? "" : "s"}; ${deferredCount} deferred`
        : phaseLabels[phase] || `Installing ${otaInstall?.filename || "firmware"}`;
  $("#ota-progress-label").textContent = label;
  $("#ota-progress-count").textContent = total > 0
    ? `${sent} / ${total} chunks`
    : `${formatBytes(bytesSent)} / ${formatBytes(size)}`;
  const elapsed = Number(otaInstall?.elapsed_s || 0);
  const eta = Number(otaInstall?.eta_s || 0);
  const rate = Number(otaInstall?.bytes_per_s || 0);
  const rateLabel = rate > 0 ? `${formatBytes(rate)}/s` : "--";
  const activity = phase === "repairing"
    ? `${repairChunks} repair chunks sent`
    : phase === "activating"
      ? `${activated} / ${targetCount} restarted${activeMac ? ` · ${activeMac}` : ""}`
      : phase === "staging" || phase === "staged"
        ? `${staged} / ${targetCount} verified`
        : `ETA ${formatDuration(eta)} · ${rateLabel}`;
  $("#ota-progress-meta").textContent = running
    ? `Elapsed ${formatDuration(elapsed)} · ${activity}`
    : readyToActivate
      ? `Uploaded in ${formatDuration(elapsed)} · installation is waiting for the operator`
      : complete
        ? `Installed in ${formatDuration(elapsed)} · average ${rateLabel}`
        : error
          ? `Stopped after ${formatDuration(elapsed)}`
          : "";
  $("#ota-progress-fill").style.width = `${complete || readyToActivate ? 100 : percent}%`;
  progress.classList.toggle("bad", Boolean(error));
  progress.classList.toggle("ok", (complete || readyToActivate) && !error);
}

function renderOtaNodes() {
  const box = $("#ota-nodes");
  if (!box) return;
  const liveNodes = Array.isArray(state?.ota?.nodes) ? state.ota.nodes : [];
  const installNodes = Array.isArray(otaInstall?.nodes) ? otaInstall.nodes : [];
  const byMac = new Map();
  installNodes.forEach((node) => {
    if (node?.mac) byMac.set(node.mac, node);
  });
  liveNodes.forEach((node) => {
    if (node?.mac) byMac.set(node.mac, { ...(byMac.get(node.mac) || {}), ...node });
  });
  const targetMacs = Array.isArray(otaInstall?.target_macs) ? otaInstall.target_macs : [];
  const nodeOffsets = otaInstall?.node_offsets || {};
  targetMacs.forEach((mac) => {
    const existing = byMac.get(mac) || { mac, phase: otaInstall?.running ? "writing" : "idle" };
    byMac.set(mac, { ...existing, offset: nodeOffsets[mac] ?? existing.offset ?? 0 });
  });
  const nodes = [...byMac.values()].sort((a, b) => {
    const performerA = lanterns().find((item) => item.mac === a.mac) || a;
    const performerB = lanterns().find((item) => item.mac === b.mac) || b;
    return comparePerformers(performerA, performerB);
  });
  const installing = Boolean(otaInstall?.running);
  box.hidden = nodes.length === 0 && !installing;
  if (box.hidden) return;
  if (nodes.length === 0) {
    box.innerHTML = `<div class="ota-node-row"><span>Waiting for node reports</span><span class="muted-inline">--</span></div>`;
    return;
  }
  const totalSize = Number(otaInstall?.size || 0);
  const stagedMacs = new Set(Array.isArray(otaInstall?.staged_macs) ? otaInstall.staged_macs : []);
  const activatedMacs = new Set(
    Array.isArray(otaInstall?.activated_macs) ? otaInstall.activated_macs : [],
  );
  const alreadyInstalledMacs = new Set(
    Array.isArray(otaInstall?.already_installed_macs) ? otaInstall.already_installed_macs : [],
  );
  const phaseLabels = {
    idle: "Waiting",
    writing: "Uploading",
    staged: "Uploaded",
    activating: "Restarting",
    complete: "Installed",
    failed: "Failed",
  };
  box.innerHTML = nodes.map((node) => {
    const failed = node.phase === "failed";
    const alreadyInstalled = alreadyInstalledMacs.has(node.mac) || activatedMacs.has(node.mac);
    const uploaded = alreadyInstalled
      ? totalSize
      : Math.max(0, Math.min(totalSize, Number(node.offset || 0)));
    const fullImage = totalSize > 0 && uploaded === totalSize;
    const crcMatches = Number(node.crc32 || 0) === Number(otaInstall?.crc32 || 0);
    const verified = alreadyInstalled || (
      fullImage
      && crcMatches
      && (node.phase === "staged" || node.phase === "complete" || stagedMacs.has(node.mac))
    );
    const percent = totalSize > 0
      ? Math.max(0, Math.min(100, Math.floor(uploaded * 100 / totalSize)))
      : 0;
    const cls = failed ? "bad" : verified ? "ok" : "";
    const lantern = lanterns().find((item) => item.mac === node.mac);
    const label = lantern?.label ? `${lantern.label} ${node.mac}` : (node.mac || "node");
    const phase = failed
      ? "failed"
      : alreadyInstalled || (verified && node.phase === "complete")
        ? "complete"
        : verified
          ? "staged"
          : installing && targetMacs.includes(node.mac)
            ? "writing"
            : node.phase;
    const detail = failed
      ? node.error
      : `${formatBytes(uploaded)} / ${formatBytes(totalSize)}${node.last_seen_s !== undefined ? ` · ${node.last_seen_s}s ago` : ""}`;
    return `<div class="ota-node-row ${cls}">
      <span>${escapeHtml(label)}</span>
      <span>${escapeHtml(phaseLabels[phase] || phase || "Waiting")} · ${percent}%</span>
      <span class="mono">${escapeHtml(detail)}</span>
      <span class="ota-node-result" aria-label="${verified ? "verified" : "pending"}">${verified ? "✓" : ""}</span>
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
  const calibrationMode = state?.locator?.enabled
    ?? state?.pattern?.pattern === "Calibration";
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
  const calibrationMode = state?.locator?.enabled
    ?? state?.pattern?.pattern === "Calibration";
  const enabled = !calibrationMode;
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
    timezone: localStorage.getItem(TIMEZONE_STORAGE_KEY) || DEFAULT_TIMEZONE,
  };
}

function powerSnapshotFromForm() {
  return {
    light_sleep_check_s: Number($("#light-check").value || 4),
    deep_sleep_check_s: Number($("#deep-check").value || 900),
    led_on_start_min: timeToMinutes($("#led-on-start").value),
    led_on_end_min: timeToMinutes($("#led-on-end").value),
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

function fieldPowerMode(power = state?.power || {}) {
  if (Boolean(power.force_sleep)) return "off";
  if (Boolean(power.schedule_enabled) && !Boolean(power.force_awake)) return "scheduled";
  return "always-on";
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
    $("#schedule-timezone").value = nextBaseline.timezone;
  }
  const fieldMode = fieldPowerMode(power);
  $("#power-state").textContent = {
    off: "Off",
    scheduled: "Sleep on schedule",
    "always-on": "Always on",
  }[fieldMode];
  $('[data-action="sleep-field"]').disabled = fieldMode === "off";
  $('[data-action="wake-field"]').disabled = fieldMode === "always-on";
  $('[data-action="enable-sleep-schedule"]').disabled = fieldMode === "scheduled";
  $('[data-action="disable-sleep-schedule"]').disabled = fieldMode !== "scheduled";
  updateSleepScheduleDirtyState();
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
  const transferred = {
    x: oldLantern.x,
    y: oldLantern.y,
    position: "Set",
    group_id: oldLantern.group_id,
    group: oldLantern.group,
  };
  const ack = await api("/api/lanterns/replace", {
    method: "POST",
    body: JSON.stringify({ old_mac: oldMac, new_mac: newMac }),
  });
  selectedMac = ack.new_mac || newMac;
  updateLanternState(oldMac, { x: null, y: null, position: "Missing", attention: "Needs position" });
  updateLanternPosition(newMac, transferred);
  closeReplacePanel();
  render();
  toast(ack.message);
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
    updateLanternPosition(mac, position);
    render();
    toast(ack.message);
  } catch (error) {
    toast(error.message, true);
    render();
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
    updateLanternPosition(mac, position);
    render();
    toast(ack.message);
  } catch (error) {
    toast(error.message, true);
    render();
  }
}

async function refresh() {
  otaInstall = (await api("/api/operations/ota-install")).install;
  try {
    state = await api("/api/state?fresh=false");
  } catch (error) {
    if (error.status === 423) {
      if (otaInstall?.running) {
        toast("Firmware installation is running. Waiting for the conductor.");
        await pollOtaInstallUntilTerminal();
      } else {
        toast("Firmware installation is finishing. Waiting for the conductor.");
        await delay(250);
      }
      return refresh();
    }
    throw error;
  }
  savedPatterns = (await api("/api/patterns")).patterns;
  uploadedPatterns = (await api("/api/uploaded-patterns")).patterns;
  await refreshReleaseInfo();
  otaArtifact = (await api("/api/operations/ota-artifact")).artifact;
  calibrationFrames = (await api("/api/calibration/frames")).frames || [];
  await refreshWifiStatus({ quiet: true });
  try {
    provisioning = await api("/api/provisioning/status");
  } catch (error) {
    provisioning = {
      available: false,
      artifact_error: error.message,
      session: { active: false, auto_update_enabled: false, max_workers: 5 },
      jobs: [],
    };
  }
  render();
}

async function refreshReleaseInfo() {
  releaseInfo = await api("/api/releases");
  renderReleases();
  return releaseInfo;
}

async function applyLiveState(liveState) {
  const currentAudio = audioState || state?.audio || null;
  state = liveState;
  if (isCurrentAudioState(liveState.audio, currentAudio)) {
    audioState = liveState.audio;
  } else if (currentAudio) {
    audioState = currentAudio;
    state.audio = currentAudio;
  }
  render();
  try {
    await refreshReleaseInfo();
  } catch (_error) {
    // The live state is still useful if release metadata briefly fails to refresh.
  }
}

async function refreshAudio() {
  if (audioRefreshPromise) return audioRefreshPromise;
  if (audioMutationPending > 0) return audioState;
  const generation = audioMutationGeneration;
  audioRefreshPromise = (async () => {
    try {
      const refreshedAudio = await api("/api/audio");
      if (generation === audioMutationGeneration) {
        acceptAudioState(refreshedAudio);
        renderAudio();
        if (state) renderOverview();
      }
    } catch (_error) {
      // A later state/WebSocket update or poll will retry without disturbing
      // the rest of the control plane.
    }
    return audioState;
  })();
  try {
    return await audioRefreshPromise;
  } finally {
    audioRefreshPromise = null;
  }
}

function startAudioPolling() {
  const poll = async () => {
    await refreshAudio();
    audioPollTimer = window.setTimeout(poll, AUDIO_POLL_MS);
  };
  if (audioPollTimer === null) poll();
}

async function refreshSavedPatterns() {
  savedPatterns = (await api("/api/patterns")).patterns;
  renderSavedPatterns();
}

async function refreshUploadedPatterns() {
  uploadedPatterns = (await api("/api/uploaded-patterns")).patterns;
  renderUploadedPatterns();
}

async function refreshPowerHistory({ incremental = false } = {}) {
  if (powerHistoryRefreshPromise) return powerHistoryRefreshPromise;
  powerHistory.loading = true;
  if (state) renderOverview();
  powerHistoryRefreshPromise = (async () => {
    try {
      const requestedHours = incremental && powerHistory.loadedAt ? Math.min(1, powerHistory.hours) : powerHistory.hours;
      const history = await api(`/api/power/history?hours=${encodeURIComponent(requestedHours)}&limit=100000`);
      const previousSamples = incremental && powerHistory.loadedAt ? powerHistory.samples : [];
      const mergedSamples = mergePowerHistorySamples(previousSamples, history.samples, powerHistory.hours);
      powerHistory = {
        hours: powerHistory.hours,
        samples: mergedSamples,
        count: mergedSamples.length,
        loading: false,
        error: null,
        loadedAt: Date.now(),
      };
    } catch (error) {
      powerHistory = {
        ...powerHistory,
        loading: false,
        error: error.message,
        loadedAt: Date.now(),
      };
    }
    if (state) renderOverview();
    return powerHistory;
  })();
  try {
    return await powerHistoryRefreshPromise;
  } finally {
    powerHistoryRefreshPromise = null;
  }
}

function startPowerHistoryPolling() {
  const poll = async () => {
    await refreshPowerHistory({ incremental: powerHistory.loadedAt > 0 });
    powerHistoryPollTimer = window.setTimeout(poll, POWER_HISTORY_POLL_MS);
  };
  if (powerHistoryPollTimer === null) poll();
}

async function refreshOtaInstall() {
  if (!otaInstallRefreshPromise) {
    otaInstallRefreshPromise = (async () => {
      otaInstall = (await api("/api/operations/ota-install")).install;
      renderOta();
      return otaInstall;
    })();
  }
  const refreshPromise = otaInstallRefreshPromise;
  try {
    return await refreshPromise;
  } finally {
    if (otaInstallRefreshPromise === refreshPromise) otaInstallRefreshPromise = null;
  }
}

function scheduleOtaInstallPoll(delayMs) {
  if (otaInstallPollTimer !== null) return;
  otaInstallPollTimer = window.setTimeout(async () => {
    otaInstallPollTimer = null;
    try {
      await refreshOtaInstall();
    } catch (_error) {
      // The WebSocket connection indicator owns transient connectivity errors.
    } finally {
      scheduleOtaInstallPoll(otaInstall?.running ? 750 : 3000);
    }
  }, delayMs);
}

function startOtaInstallPolling() {
  scheduleOtaInstallPoll(otaInstall?.running ? 750 : 3000);
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

async function pollOtaInstallUntilTerminal() {
  do {
    await delay(750);
    await refreshOtaInstall();
  } while (otaInstall?.running);
  return otaInstall;
}

async function locateLantern(mac) {
  if (!mac) return;
  const ack = await api(`/api/lanterns/${encodeURIComponent(mac)}/identify`, { method: "POST" });
  toast(ack.message || "locator sent");
}

function lanternLocationHotkeyAction(key, lantern) {
  if (!lantern) return null;
  const normalized = String(key || "").toLowerCase();
  if (normalized === "m") return isPositioned(lantern) ? "move" : null;
  if (normalized === "p") return isPositioned(lantern) ? null : "move";
  return {
    l: "identify",
    r: "replace",
    d: "details",
    f: "forget",
  }[normalized] || null;
}

function triggerLanternLocationHotkey(key) {
  const lantern = selectedLantern();
  const normalized = String(key || "").toLowerCase();
  const action = lanternLocationHotkeyAction(normalized, lantern);
  if (action) {
    runAction(action);
    return true;
  }
  if (lantern && normalized === "m") {
    toast(`${lantern.label} needs a position; press P to place it`, true);
    return true;
  }
  if (lantern && normalized === "p") {
    toast(`${lantern.label} is already positioned; press M to move it`, true);
    return true;
  }
  return false;
}

async function runAction(action) {
  const lantern = selectedLantern();
  if (!lantern && ["identify", "move", "replace", "forget"].includes(action)) return;
  try {
    if (action === "logout") {
      await api("/api/auth/logout", { method: "POST" });
      window.location.assign("/login");
      return;
    }
    if (action === "toggle-audio") {
      const currentAudio = audioState || state?.audio;
      const updatedAudio = await mutateAudio(currentAudio?.playing ? "/api/audio/pause" : "/api/audio/play", { method: "POST" });
      acceptAudioState(updatedAudio);
      renderAudio();
      renderOverview();
      const notice = audioActionNotice(
        audioState,
        audioState.playing ? "Soundtrack playing" : "Soundtrack paused",
      );
      toast(notice.message, notice.error);
      return;
    }
    if (action === "restart-audio") {
      const updatedAudio = await mutateAudio("/api/audio/restart", { method: "POST" });
      acceptAudioState(updatedAudio);
      renderAudio();
      renderOverview();
      const notice = audioActionNotice(
        audioState,
        audioState.paused ? "Soundtrack reset to the beginning" : "Soundtrack restarted",
      );
      toast(notice.message, notice.error);
      return;
    }
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
      updateLanternState(lantern.mac, { x: null, y: null, position: "Missing", attention: "Needs position" });
      render();
      toast(ack.message);
      return;
    }
    if (action === "broadcast") {
      if (!state || !patternDraft) return;
      if (!isPatternDirty()) return;
      if (patternDraft.pattern === "Uploaded Pattern") {
        const payload = uploadedDraftPayload();
        const ack = await api(`/api/uploaded-patterns/broadcast?group_id=${selectedGroup}`, {
          method: "POST",
          body: JSON.stringify(payload),
        });
        const programId = Number(ack.compiled.program_id);
        const programTag = Number(ack.compiled.program_tag);
        applyOptimisticPattern(selectedGroup, {
          pattern: "Uploaded Pattern",
          brightness: Number(payload.brightness),
          params: {
            p0: programId & 0xffff,
            p1: (programId >>> 16) & 0xffff,
            p2: programTag & 0xffff,
            p3: (programTag >>> 16) & 0xffff,
          },
        });
        patternDraft = null;
        render();
        toast(ack.message);
        return;
      }
      const pattern = patternDraft.pattern;
      const brightness = Number(patternDraft.brightness);
      const params = patternParams(patternDraft);
      const ack = await api("/api/show/pattern", {
        method: "POST",
        body: JSON.stringify({ pattern, brightness, params, group_id: selectedGroup }),
      });
      const nextConfig = {
        pattern,
        brightness,
        params: patternStateParams(patternDraft),
      };
      applyOptimisticPattern(selectedGroup, nextConfig);
      render();
      toast(ack.message);
      return;
    }
    if (action === "save-pattern") {
      if (!state || !patternDraft) return;
      if (patternDraft.pattern === "Uploaded Pattern") {
        const ack = await api("/api/uploaded-patterns", {
          method: "POST",
          body: JSON.stringify(uploadedDraftPayload()),
        });
        uploadedPatterns = [...uploadedPatterns, ack.pattern]
          .sort((a, b) => a.name.localeCompare(b.name));
        renderUploadedPatterns();
        toast(`saved ${ack.pattern.name}`);
        return;
      }
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
    if (action === "preview-uploaded") {
      const preview = await api("/api/uploaded-patterns/preview", {
        method: "POST",
        body: JSON.stringify(uploadedDraftPayload()),
      });
      const compiled = preview.compiled;
      $("#uploaded-pattern-status").textContent = `${compiled.instructions} instructions · ${compiled.bytes} bytes · ${compiled.static ? "static" : "animated"}`;
      toast(`custom pattern ${compiled.program_label} is ready`);
      return;
    }
    if (action === "save-group-name") {
      const ack = await api(`/api/groups/${selectedGroup}`, {
        method: "PUT",
        body: JSON.stringify({ name: $("#group-name").value }),
      });
      state.groups = (state.groups || []).map((item) => Number(item.group_id) === selectedGroup ? ack.group : item);
      groupNameBaseline = ack.group.name;
      $("#group-name").value = ack.group.name;
      render();
      toast(ack.message);
      return;
    }
    if (action === "blackout") {
      if (!confirm("Black out all groups? You can restore their previous brightness afterward.")) return;
      const ack = await api("/api/show/blackout", { method: "POST" });
      applyOptimisticBlackout(true);
      patternDraft = null;
      render();
      toast(ack.message, true);
      return;
    }
    if (action === "restore-blackout") {
      const ack = await api("/api/show/restore", { method: "POST" });
      applyOptimisticBlackout(false);
      patternDraft = null;
      render();
      toast(ack.message);
      return;
    }
    if (action === "turn-off-group") {
      const live = activePatternState();
      const isOff = Number(live.brightness || 0) === 0;
      if (!isOff && !confirm(`Turn off ${groupLabel(selectedGroup)}?`)) return;
      const brightness = isOff ? storedGroupBrightness(selectedGroup) : 0;
      if (!isOff) rememberGroupBrightness(selectedGroup, live.brightness);
      const ack = await api("/api/show/pattern", {
        method: "POST",
        body: JSON.stringify({
          pattern: live.pattern,
          brightness,
          params: live.params || {},
          group_id: selectedGroup,
        }),
      });
      applyOptimisticPattern(selectedGroup, { ...live, brightness });
      patternDraft = null;
      render();
      toast(ack.message);
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
      const nextPower = { ...(state.power || {}), ...policy };
      nextPower.leds_on = powerLedsOn(nextPower);
      state = { ...state, power: nextPower };
      render();
      toast(ack.message);
      return;
    }
    if (["sleep-field", "wake-field", "enable-sleep-schedule", "disable-sleep-schedule"].includes(action)) {
      const mode = {
        "sleep-field": "sleep",
        "wake-field": "wake",
        "enable-sleep-schedule": "schedule",
        "disable-sleep-schedule": "wake",
      }[action];
      if (mode === "sleep" && otaInstall?.running) {
        if (!confirm("A firmware update is keeping the field awake. Pause it at the next safe boundary, then sleep the field?")) return;
        toast("Pausing firmware update before sleeping the field...");
        const paused = await api("/api/operations/ota-install", { method: "DELETE" });
        otaInstall = paused.install;
        renderOta();
      }
      const ack = await api("/api/operations/field-power", {
        method: "POST",
        body: JSON.stringify({ mode }),
      });
      const overrides = {
        sleep: { schedule_enabled: false, force_awake: false, force_sleep: true },
        wake: { schedule_enabled: false, force_awake: true, force_sleep: false },
        schedule: { schedule_enabled: true, force_awake: false, force_sleep: false },
      }[mode];
      const nextPower = { ...(state.power || {}), ...overrides };
      nextPower.leds_on = powerLedsOn(nextPower);
      state = { ...state, power: nextPower };
      render();
      toast(ack.message || `${mode} command sent`);
      return;
    }
    if (action === "save-power-monitor") {
      const ack = await api("/api/operations/power-monitor", {
        method: "POST",
        body: JSON.stringify({
          battery_capacity_wh: Number($("#battery-capacity").value || 384),
          full_voltage: Number($("#battery-full-voltage").value || 14.4),
        }),
      });
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
    if (action === "enable-provisioning-auto-update") {
      provisioning = await api("/api/provisioning/auto-update", {
        method: "PUT",
        body: JSON.stringify({
          max_workers: Number($("#provisioning-workers").value || 5),
        }),
      });
      renderProvisioning();
      toast("USB firmware auto-update enabled");
      return;
    }
    if (action === "disable-provisioning-auto-update") {
      provisioning = await api("/api/provisioning/auto-update", { method: "DELETE" });
      renderProvisioning();
      toast("USB firmware auto-update disabled");
      return;
    }
    if (action === "upload-ota-artifact") {
      const file = $("#ota-file")?.files?.[0];
      if (!file) return;
      const protocol = Number($("#ota-file-protocol")?.value);
      if (!Number.isInteger(protocol) || protocol < 1 || protocol > 255) {
        toast("enter the firmware wire protocol before uploading", true);
        return;
      }
      otaArtifactUploading = true;
      renderOta();
      toast("uploading firmware file to control plane");
      try {
        const ack = await apiBinary(`/api/operations/ota-artifact?filename=${encodeURIComponent(file.name)}&protocol=${protocol}`, file);
        otaArtifact = ack.artifact;
        toast(ack.message);
      } finally {
        otaArtifactUploading = false;
        renderOta();
      }
      return;
    }
    if (action === "toggle-ota-auto-update") {
      const enabled = otaInstall?.auto_update_enabled !== false;
      const ack = await api("/api/operations/ota-auto-update", {
        method: "PUT",
        body: JSON.stringify({ enabled: !enabled }),
      });
      otaInstall = ack.install;
      renderOta();
      toast(ack.message);
      return;
    }
    if (action === "select-ota-release") {
      const version = $("#ota-release-select")?.value;
      if (!version) return;
      if (!confirm(`Use firmware from release v${version} as the field target?`)) return;
      const ack = await api("/api/operations/ota-release", {
        method: "POST",
        body: JSON.stringify({ version }),
      });
      otaArtifact = ack.artifact;
      renderOta();
      toast(ack.message);
      return;
    }
    if (action === "install-ota") {
      if (otaInstall?.phase === "ready-to-activate") {
        if (!confirm("Install the verified firmware now? Staged performers and the conductor will restart.")) return;
        const ack = await api("/api/operations/ota-activate", { method: "POST" });
        otaInstall = ack.install;
        renderOta();
        await pollOtaInstallUntilTerminal();
        if (otaInstall.error) throw new Error(otaInstall.error);
        toast("Firmware installed on field");
        await refresh();
        return;
      }
      if (!otaArtifact || !otaReadyForInstall()) return;
      if (!confirm("Update every online performer? Firmware will upload and verify while the field stays live, then performers and the conductor will restart automatically.")) return;
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
      const ack = await api("/api/operations/ota-install", { method: "POST" });
      otaInstall = ack.install;
      renderOta();
      await pollOtaInstallUntilTerminal();
      if (otaInstall.error) throw new Error(otaInstall.error);
      toast("Firmware installed on field");
      await refresh();
      return;
    }
    if (action === "pause-ota") {
      const ack = await api("/api/operations/ota-install", { method: "DELETE" });
      otaInstall = ack.install;
      renderOta();
      toast(ack.message);
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
  ws.addEventListener("message", async (event) => {
    const data = JSON.parse(event.data);
    if (data.state) {
      await applyLiveState(data.state);
    }
    if (data.provisioning) {
      provisioning = data.provisioning;
      renderProvisioning();
    }
    if (data.audio) {
      acceptAudioState(data.audio);
      renderAudio();
      if (state) renderOverview();
    }
  });
  ws.addEventListener("close", (event) => {
    if (event.code === 4401) {
      window.location.assign("/login");
      return;
    }
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
    setActiveView(tab.dataset.view);
    if (tab.dataset.view === "overview" && Date.now() - powerHistory.loadedAt > POWER_HISTORY_POLL_MS) {
      refreshPowerHistory({ incremental: powerHistory.loadedAt > 0 });
    }
    if (tab.dataset.view === "overview" && Date.now() - fieldPreview.loadedAt > FIELD_PREVIEW_POLL_MS) {
      refreshFieldPreview();
    }
  });
});

$$('[data-overview-view]').forEach((button) => {
  button.addEventListener("click", () => setActiveView(button.dataset.overviewView));
});

$$('[data-power-range]').forEach((button) => {
  button.addEventListener("click", async () => {
    const hours = Number(button.dataset.powerRange);
    if (!Number.isFinite(hours) || hours <= 0 || hours === powerHistory.hours) return;
    powerHistory = { hours, samples: [], count: 0, loading: true, error: null, loadedAt: 0 };
    $$('[data-power-range]').forEach((item) => item.classList.toggle("active", item === button));
    renderOverview();
    await refreshPowerHistory();
  });
});

document.addEventListener("keydown", (event) => {
  if (event.repeat || event.metaKey || event.ctrlKey || event.altKey) return;
  if (!$("#view-map")?.classList.contains("active")) return;
  const target = event.target;
  if (target?.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(target?.tagName)) return;
  if (triggerLanternLocationHotkey(event.key)) event.preventDefault();
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
    api(`/api/patterns/${encodeURIComponent(id)}/broadcast?group_id=${selectedGroup}`, { method: "POST" })
      .then((ack) => {
        applyOptimisticPattern(selectedGroup, {
          pattern: ack.pattern.pattern,
          brightness: Number(ack.pattern.brightness),
          params: ack.pattern.params || {},
        });
        patternDraft = null;
        render();
        toast(ack.message);
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

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-uploaded-action]");
  if (!target) return;
  const id = target.dataset.patternId;
  const item = uploadedPatterns.find((pattern) => pattern.id === id);
  if (!item) return;
  const action = target.dataset.uploadedAction;
  if (action === "load") {
    loadCustomPatternEditor(item);
    $("#uploaded-pattern-editor").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  if (action === "broadcast") {
    target.disabled = true;
    api(`/api/uploaded-patterns/${encodeURIComponent(id)}/broadcast?group_id=${selectedGroup}`, { method: "POST" })
      .then((ack) => {
        const programId = Number(ack.compiled.program_id);
        const programTag = Number(ack.compiled.program_tag);
        applyOptimisticPattern(selectedGroup, {
          pattern: "Uploaded Pattern",
          brightness: Number(item.brightness),
          params: {
            p0: programId & 0xffff,
            p1: (programId >>> 16) & 0xffff,
            p2: programTag & 0xffff,
            p3: (programTag >>> 16) & 0xffff,
          },
        });
        patternDraft = null;
        render();
        toast(ack.message);
      })
      .catch((error) => toast(error.message, true))
      .finally(() => { target.disabled = false; });
  }
  if (action === "delete") {
    if (!confirm(`Delete ${item.name}?`)) return;
    api(`/api/uploaded-patterns/${encodeURIComponent(id)}`, { method: "DELETE" })
      .then(async () => {
        await refreshUploadedPatterns();
        toast("custom pattern deleted");
      })
      .catch((error) => toast(error.message, true));
  }
});

document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-provision-action]");
  if (!target) return;
  try {
    if (target.dataset.provisionAction === "install") {
      provisioning = await api(
        `/api/provisioning/jobs/${encodeURIComponent(target.dataset.jobId)}/install`,
        { method: "POST" },
      );
      renderProvisioning();
      toast("firmware installation queued");
      return;
    }
    if (target.dataset.provisionAction === "map-slot") {
      const input = $(`[data-slot-input="${target.dataset.jobId}"]`);
      const slot = Number(input?.value || 0);
      if (!Number.isInteger(slot) || slot < 1 || slot > 32) {
        toast("enter a physical slot from 1 to 32", true);
        return;
      }
      provisioning = await api("/api/provisioning/slots", {
        method: "PUT",
        body: JSON.stringify({ port_id: target.dataset.portId, slot }),
      });
      renderProvisioning();
      toast(`hub port assigned to slot ${slot}`);
    }
  } catch (error) {
    toast(error.message, true);
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

$("#pattern-group").addEventListener("change", (event) => {
  const next = Math.max(0, Math.min(GROUP_COUNT - 1, Number(event.target.value || 0)));
  if (next === selectedGroup) return;
  const groupNameDirty = groupNameBaseline !== null && $("#group-name").value.trim() !== groupNameBaseline;
  if ((isPatternDirty() || groupNameDirty) && !confirm("Discard the unsaved changes?")) {
    event.target.value = String(selectedGroup);
    return;
  }
  selectedGroup = next;
  groupNameBaseline = null;
  patternDraft = patternDraftFromState();
  render();
});

$("#lantern-group").addEventListener("change", async (event) => {
  const lantern = selectedLantern();
  if (!lantern) return;
  const groupId = Math.max(0, Math.min(GROUP_COUNT - 1, Number(event.target.value || 0)));
  await assignLanternGroup(lantern.mac, groupId, event.target);
});

$("#lantern-led-count").addEventListener("change", async (event) => {
  const lantern = selectedLantern();
  if (!lantern) return;
  await assignLanternLedCount(lantern.mac, ledCountSafe(event.target.value), event.target);
});

$("#pattern-picker").addEventListener("click", (event) => {
  if (event.target.dataset.pattern) {
    const previousPattern = patternDraft?.pattern;
    if (!patternDraft && state) patternDraft = patternDraftFromState();
    if (patternDraft) {
      patternDraft = patternDraftForSelection(event.target.dataset.pattern);
      if (patternDraft.pattern === "Uploaded Pattern" && previousPattern !== "Uploaded Pattern") {
        resetCustomPatternBuilder();
      }
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
  if (!rgb) return;
  if (!patternDraft && state) patternDraft = patternDraftFromState();
  if (!patternDraft) return;
  const { hue, saturation, value } = rgbToHueSaturationValue(rgb.r, rgb.g, rgb.b);
  patternDraft.hue = hue;
  patternDraft.saturation = saturation;
  patternDraft.value = value;
  if (patternDraft.pattern === "Uploaded Pattern") setCustomPatternSourceMode("guided");
  renderPatternControls();
}

$("#pattern-color-picker").addEventListener("input", (event) => {
  applyHexColor(event.target.value);
});

let colorWheelDragging = false;

function applyColorWheelPoint(clientX, clientY) {
  if (!patternDraft && state) patternDraft = patternDraftFromState();
  if (!patternDraft) return;
  const wheel = $("#pattern-color-wheel");
  const selected = colorWheelSelection(
    wheel.getBoundingClientRect(),
    clientX,
    clientY,
    patternDraft.hue,
  );
  patternDraft.hue = selected.hue;
  patternDraft.saturation = selected.saturation;
  patternDraft.value = 255;
  if (patternDraft.pattern === "Uploaded Pattern") setCustomPatternSourceMode("guided");
  renderPatternControls();
}

$("#pattern-color-wheel").addEventListener("pointerdown", (event) => {
  colorWheelDragging = true;
  event.currentTarget.focus({ preventScroll: true });
  event.currentTarget.setPointerCapture(event.pointerId);
  applyColorWheelPoint(event.clientX, event.clientY);
  event.preventDefault();
});

$("#pattern-color-wheel").addEventListener("pointermove", (event) => {
  if (!colorWheelDragging) return;
  applyColorWheelPoint(event.clientX, event.clientY);
});

$("#pattern-color-wheel").addEventListener("pointerup", (event) => {
  colorWheelDragging = false;
  if (event.currentTarget.hasPointerCapture(event.pointerId)) {
    event.currentTarget.releasePointerCapture(event.pointerId);
  }
});

$("#pattern-color-wheel").addEventListener("pointercancel", () => {
  colorWheelDragging = false;
});

$("#pattern-color-wheel").addEventListener("keydown", (event) => {
  if (!patternDraft && state) patternDraft = patternDraftFromState();
  if (!patternDraft) return;
  const step = event.shiftKey ? 10 : 2;
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    patternDraft.hue = (Number(patternDraft.hue) + (event.key === "ArrowRight" ? step : -step) + 360) % 360;
  } else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
    patternDraft.saturation = Math.min(100, Math.max(0, Number(patternDraft.saturation ?? 100) + (event.key === "ArrowUp" ? step : -step)));
  } else {
    return;
  }
  patternDraft.value = 255;
  if (patternDraft.pattern === "Uploaded Pattern") setCustomPatternSourceMode("guided");
  renderPatternControls();
  event.preventDefault();
});

[
  "#custom-pattern-motion",
  "#custom-pattern-period",
  "#custom-pattern-wavelength",
  "#custom-pattern-direction",
  "#custom-pattern-center-x",
  "#custom-pattern-center-y",
  "#custom-pattern-min-value",
  "#custom-pattern-max-value",
].forEach((selector) => {
  $(selector).addEventListener("input", () => {
    setCustomPatternSourceMode("guided");
    $("#uploaded-pattern-status").textContent = "Ready to validate";
    renderPatternControls();
  });
});

$("#uploaded-pattern-json").addEventListener("input", () => {
  setCustomPatternSourceMode("advanced");
  $("#uploaded-pattern-status").textContent = "Advanced source changed · validate before running";
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

$("#pattern-texture").addEventListener("input", (event) => {
  if (!patternDraft && state) patternDraft = patternDraftFromState();
  if (patternDraft) {
    patternDraft.texture = Number(event.target.value);
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

[["#pattern-front-width", "frontWidth"], ["#pattern-chorus", "chorus"]]
  .forEach(([selector, key]) => {
    $(selector).addEventListener("input", (event) => {
      if (!patternDraft && state) patternDraft = patternDraftFromState();
      if (patternDraft) {
        patternDraft[key] = Number(event.target.value);
        renderPatternControls();
      }
    });
  });

[["#pattern-fire-speed", "speed"], ["#pattern-cooling", "cooling"], ["#pattern-sparking", "sparking"]]
  .forEach(([selector, key]) => {
    $(selector).addEventListener("input", (event) => {
      if (!patternDraft && state) patternDraft = patternDraftFromState();
      if (patternDraft) {
        patternDraft[key] = Number(event.target.value);
        renderPatternControls();
      }
    });
  });

$("#pattern-center-x").addEventListener("input", (event) => {
  if (!patternDraft && state) patternDraft = patternDraftFromState();
  if (patternDraft) {
    patternDraft.centerX = Number(event.target.value);
    renderPatternControls();
  }
});

$("#pattern-center-y").addEventListener("input", (event) => {
  if (!patternDraft && state) patternDraft = patternDraftFromState();
  if (patternDraft) {
    patternDraft.centerY = Number(event.target.value);
    renderPatternControls();
  }
});

["#led-on-start", "#led-on-end", "#schedule-timezone", "#light-check", "#deep-check"].forEach((selector) => {
  const input = $(selector);
  input.addEventListener("input", updateSleepScheduleDirtyState);
  input.addEventListener("change", updateSleepScheduleDirtyState);
});

$("#group-name").addEventListener("input", updateGroupNameDirtyState);
$("#group-name").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !$('[data-action="save-group-name"]').disabled) {
    event.preventDefault();
    runAction("save-group-name").catch((error) => toast(error.message, true));
  }
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

window.addEventListener("resize", () => {
  if (fieldPreviewVisible()) drawFieldPreview(0);
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    fieldPreviewAnimationStartedAt = 0;
    if (fieldPreviewVisible()) drawFieldPreview(0);
  }
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

refresh().then(() => {
  connectWebSocket();
  startOtaInstallPolling();
  startPowerHistoryPolling();
  startFieldPreviewPolling();
  startAudioPolling();
}).catch((error) => toast(error.message, true));
