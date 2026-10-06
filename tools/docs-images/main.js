// Renders one docs image per ?shot=<name>, with made-up sample data only.
// The camera card and the timeline card are the REAL cuboai-card.js; the
// setup/Configure dialogs are drawn from forms.json, which dump_forms.py builds
// from the real config flow and translations.

import { FIXED_NOW, mdiPath } from "./stubs.js";

const DEV = "CB02XXXXXXXX0001";
const DEV2 = "SW05XXXXXXXX0002";
const HOUR = 3600 * 1000;
const shotName = new URLSearchParams(location.search).get("shot") || "card-live";
const root = document.getElementById("shot");

// ── Sample Home Assistant state ──────────────────────────────────────────────

function st(entity_id, state, attributes = {}) {
  return { entity_id, state: String(state), attributes, last_changed: new Date(FIXED_NOW).toISOString() };
}

const SONGS = [
  { name: "Brahms' Lullaby (piano)", url: "https://www.youtube.com/watch?v=sample01", category: "YouTube", addedBy: "Parent" },
  { name: "Rain on a tin roof – 1 hour", url: "https://www.youtube.com/watch?v=sample02", category: "YouTube", addedBy: "Parent" },
  { name: "Gentle ocean waves", url: "https://open.spotify.com/track/sample03", category: "Spotify", addedBy: "Grandma" },
  { name: "Twinkle Twinkle (music box)", url: "https://www.youtube.com/watch?v=sample04", category: "YouTube", addedBy: "Parent" },
];
const PLAYLISTS = [
  { name: "Bedtime", songs: [SONGS[0].url, SONGS[3].url, SONGS[1].url], addedBy: "Parent" },
  { name: "Nap time", songs: [SONGS[2].url, SONGS[1].url], addedBy: "Grandma" },
];

function alerts() {
  const at = (h, m) => new Date(new Date(FIXED_NOW).setHours(h, m, 0, 0)).getTime() / 1000;
  return [
    { type: "CUBO_ALERT_CRY", ts: at(5, 42), time: "05:42", image_url: "" },
    { type: "CUBO_ALERT_CRY", ts: at(3, 12), time: "03:12", image_url: "" },
    { type: "CUBO_ALERT_COVERED_FACE", ts: at(1, 5), time: "01:05", image_url: "" },
    { type: "CUBO_ALERT_TEMPERATURE", ts: at(23, 30) - 86400, time: "23:30", image_url: "" },
  ];
}

export const STATES = Object.fromEntries(
  [
    st("camera.baby_local_camera", "streaming", {
      device_id: DEV, uid: "SAMPLEUID", rtsp_port: 8557, h264_transcode: false, friendly_name: "Baby Local Camera",
    }),
    st("camera.baby_recording", "idle", { device_id: DEV, dvr: true, playing_from: null, friendly_name: "Baby Recording" }),
    st("media_player.baby_speaker", "idle", { device_id: DEV, friendly_name: "Baby Speaker", volume_level: 0.5 }),
    st("media_player.baby_lullaby", "playing", {
      device_id: DEV, friendly_name: "Baby Lullaby", source: "Brahms' Lullaby", media_title: "Brahms' Lullaby",
      source_list: ["Brahms' Lullaby", "Twinkle Twinkle Little Star", "White Noise", "Rain", "Ocean"], volume_level: 0.4,
    }),
    st("number.baby_speaker_play_time", "30", { min: 0, max: 120, step: 10, friendly_name: "Baby Speaker Play Time" }),
    st("number.baby_lullaby_timer", "30", { min: 0, max: 60, step: 30, friendly_name: "Baby Lullaby Timer" }),
    st("sensor.cuboai_temperature_baby", "22.5", { unit_of_measurement: "°C", friendly_name: "CuboAI Temperature Baby" }),
    st("sensor.cuboai_humidity_baby", "48", { unit_of_measurement: "%", friendly_name: "CuboAI Humidity Baby" }),
    st("sensor.cuboai_mat_bpm_baby", "118", { unit_of_measurement: "bpm", friendly_name: "CuboAI Mat BPM Baby" }),
    st("sensor.cuboai_media_library", "active", {
      custom_songs: SONGS, playlists: PLAYLISTS, settings: { [DEV]: { shuffle: false, repeat: "all", muted: true } },
    }),
    st("switch.cache_youtube_spotify_songs", "on", { friendly_name: "Cache YouTube/Spotify Songs" }),
    st("sensor.cuboai_last_alert_baby", "Cry detected", { alerts: alerts(), friendly_name: "CuboAI Last Alert Baby" }),
    st("sensor.cuboai_baby_present_baby", "in crib", {}),
    st("sensor.cuboai_motion_baby", "still", {}),
    st("sensor.cuboai_noise_level_baby", "24", {}),
    st("sensor.cuboai_camera_state_baby", "online", {}),
  ].map((s) => [s.entity_id, s]),
);

// A plausible night: in the crib from 19:40 with two wake-ups, noise and
// motion around them, the camera online throughout.
function nightHistory(start) {
  const t = (h, m) => {
    const d = new Date(start);
    d.setHours(h, m, 0, 0);
    if (d.getTime() < start) d.setDate(d.getDate() + 1);
    return d.getTime() / 1000;
  };
  const series = (pairs) => pairs.map(([h, m, s]) => ({ s: String(s), lu: t(h, m) }));
  return {
    "sensor.cuboai_baby_present_baby": series([
      [19, 0, "not in crib"], [19, 40, "in crib"], [23, 10, "not in crib"], [23, 35, "in crib"],
      [3, 5, "not in crib"], [3, 30, "in crib"], [6, 50, "not in crib"],
    ]),
    "sensor.cuboai_motion_baby": series([
      [19, 0, "moving"], [19, 55, "still"], [23, 5, "moving"], [23, 40, "still"], [2, 58, "strong (2)"],
      [3, 35, "still"], [6, 40, "moving"],
    ]),
    "sensor.cuboai_noise_level_baby": series([
      [19, 0, 25], [19, 50, 22], [23, 0, 29], [23, 40, 21], [3, 0, 31], [3, 40, 22], [6, 45, 27],
    ]),
    "sensor.cuboai_camera_state_baby": series([[19, 0, "online"]]),
  };
}

export const hass = {
  states: STATES,
  user: { name: "Parent", id: "sample-user" },
  language: "en",
  locale: { language: "en", number_format: "language", time_format: "24" },
  themes: { darkMode: false },
  callService: async (domain, service, data) => {
    console.log("callService", domain, service, JSON.stringify(data || {}).slice(0, 120));
    // What the backend does: the Recording camera reports the moment it seeked to.
    if (domain === "cuboai" && service === "play_recording") {
      STATES["camera.baby_recording"].attributes.playing_from = data.start_time;
    }
  },
  callWS: async (msg) => {
    if (msg.type === "history/history_during_period") return nightHistory(Date.parse(msg.start_time));
    return {};
  },
  connection: { subscribeMessage: async () => () => {}, subscribeEvents: async () => () => {} },
};

// ── Home Assistant-style form (data entry flow dialog) ──────────────────────

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const icon = (name, size = 20, color = "currentColor") =>
  `<svg viewBox="0 0 24 24" width="${size}" height="${size}" style="fill:${color};flex:none"><path d="${mdiPath(name)}"/></svg>`;

const FORM_CSS = `
  .dlg { width: 560px; background:#fff; border-radius: 28px; box-shadow: 0 10px 30px rgba(0,0,0,.18); overflow:hidden; }
  .dlg .hd { display:flex; align-items:center; gap:14px; padding: 22px 24px 6px; }
  .dlg .hd h2 { font: 400 22px/1.3 Roboto, sans-serif; margin:0; flex:1 }
  .dlg .desc { padding: 4px 24px 8px; color:#444; font-size:14px; line-height:1.5 }
  .dlg .bd { padding: 6px 24px 8px; }
  .fld { margin: 14px 0; }
  .tf { position:relative; background:#f5f5f5; border-radius:4px 4px 0 0; border-bottom:1px solid #8a8a8a; padding: 22px 12px 7px; font-size:16px; min-height: 22px }
  .tf .lb { position:absolute; left:12px; top:6px; font-size:12px; color:#6b6b6b; }
  .tf .val { color:#212121 } .tf .ph { color:#9e9e9e }
  .tf .sfx { position:absolute; right:12px; top: 18px; color:#6b6b6b }
  .help { color:#6b6b6b; font-size:12.5px; line-height:1.45; margin: 5px 12px 0; }
  .bool { display:flex; align-items:flex-start; gap:14px; font-size:15px; padding: 4px 0 }
  .sw { width:36px; height:14px; border-radius:7px; background:#bdbdbd; position:relative; margin-top:4px; flex:none }
  .sw::after { content:""; position:absolute; top:-3px; left:-2px; width:20px; height:20px; border-radius:50%; background:#fafafa; box-shadow:0 1px 3px rgba(0,0,0,.4) }
  .sw.on { background: rgba(3,169,244,.5) } .sw.on::after { left: 18px; background: #03a9f4 }
  .multi .ttl { font-size:15px; margin: 2px 0 6px }
  .chk { display:flex; align-items:center; gap:12px; padding: 5px 2px; font-size:15px }
  .box { width:18px; height:18px; border:2px solid #5f5f5f; border-radius:2px; display:flex; align-items:center; justify-content:center; flex:none }
  .box.on { background:#03a9f4; border-color:#03a9f4 }
  .ft { display:flex; justify-content:flex-end; padding: 10px 24px 22px }
  .btn { color:#03a9f4; font: 500 14px Roboto, sans-serif; letter-spacing:.3px; padding: 10px 14px }
  .more { text-align:center; color:#9e9e9e; font-size:13px; padding: 4px 0 2px }
`;

// Helper text as Home Assistant shows it: placeholders filled, `code` and
// [links](url) rendered.
const PLACEHOLDERS = { docs_url: "https://github.com/niruse/cuboai#debug-logs" };
function md(text) {
  const filled = String(text).replace(/\{(\w+)\}/g, (m, k) => PLACEHOLDERS[k] ?? m);
  return esc(filled)
    .replace(/`([^`]+)`/g, '<code style="background:#eee;border-radius:3px;padding:0 3px;font-size:11.5px">$1</code>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<span style="color:#03a9f4">$1</span>');
}

function field(f, value) {
  const help = f.help ? `<div class="help">${md(f.help)}</div>` : "";
  if (f.type === "bool") {
    return `<div class="fld"><div class="bool"><div class="sw ${value ? "on" : ""}"></div><div>${esc(f.label)}</div></div>${help}</div>`;
  }
  if (f.type === "multi") {
    const chosen = new Set(value || []);
    const rows = Object.entries(f.choices || {})
      .map(([k, lbl]) => `<div class="chk"><div class="box ${chosen.has(k) ? "on" : ""}">${chosen.has(k) ? icon("mdi:check", 16, "#fff") : ""}</div>${esc(lbl)}</div>`)
      .join("");
    return `<div class="fld multi"><div class="ttl">${esc(f.label)}</div>${rows}${help}</div>`;
  }
  let shown;
  if (f.type === "password") shown = value ? `<span class="val">${"•".repeat(10)}</span>` : "";
  else if (value === "" || value === null || value === undefined) shown = "";
  else shown = `<span class="val">${esc(value)}</span>`;
  const req = f.required ? "*" : "";
  const eye =
    f.type === "password"
      ? `<span class="sfx">${icon("mdi:eye", 20, "#6b6b6b")}</span>`
      : f.type === "select"
        ? `<span class="sfx">${icon("mdi:menu-down", 22, "#6b6b6b")}</span>`
        : "";
  return `<div class="fld"><div class="tf"><span class="lb">${esc(f.label)}${req}</span>${shown}${eye}</div>${help}</div>`;
}

function dialog(form, { keys = null, values = {}, closeIcon = true, footer = "Submit", more = "" } = {}) {
  const fields = form.fields.filter((f) => !keys || keys.includes(f.key));
  const order = keys ? keys.map((k) => fields.find((f) => f.key === k)).filter(Boolean) : fields;
  const body = order.map((f) => field(f, f.key in values ? values[f.key] : f.default)).join("");
  return `<style>${FORM_CSS}</style>
    <div class="dlg">
      <div class="hd">${closeIcon ? icon("mdi:close", 22, "#444") : ""}<h2>${esc(form.title)}</h2>${icon("mdi:help-circle-outline", 22, "#444")}</div>
      ${form.description ? `<div class="desc">${esc(form.description)}</div>` : ""}
      <div class="bd">${more ? `<div class="more">${esc(more)}</div>` : ""}${body}</div>
      <div class="ft"><span class="btn">${esc(footer)}</span></div>
    </div>`;
}

// ── Entities card / more-info / notification ────────────────────────────────

const ENT_CSS = `
  .ec { width: 420px; background:#fff; border-radius:12px; border:1px solid #e0e0e0; padding: 12px 0 8px }
  .ec h3 { font: 400 22px Roboto, sans-serif; margin: 4px 16px 10px }
  .row { display:flex; align-items:center; gap:16px; padding: 7px 16px; font-size:14.5px }
  .row .ic { width:40px; display:flex; justify-content:center; color:#44739e }
  .row .nm { flex:1 } .row .vl { color:#212121; text-align:right }
  .tg { width:34px; height:14px; border-radius:7px; background:#bdbdbd; position:relative }
  .tg::after { content:""; position:absolute; top:-3px; left:-2px; width:20px; height:20px; border-radius:50%; background:#fafafa; box-shadow:0 1px 3px rgba(0,0,0,.4) }
  .tg.on { background: rgba(3,169,244,.5) } .tg.on::after { left:16px; background:#03a9f4 }
  .sel { border-bottom:1px solid #8a8a8a; padding: 2px 4px; min-width: 70px; display:inline-flex; justify-content:space-between; gap:8px }
  .mi { width: 560px; background:#fff; border-radius:28px; box-shadow: 0 10px 30px rgba(0,0,0,.18); padding: 8px 0 18px }
  .mi .hd { display:flex; align-items:center; gap:14px; padding: 16px 24px 4px; font-size: 20px }
  .mi .big { padding: 6px 24px 10px; font-size: 15px; color:#212121; word-break: break-all }
  .attr { display:flex; justify-content:space-between; gap: 18px; padding: 8px 24px; border-top:1px solid #eee; font-size: 13.5px }
  .attr .k { color:#555; white-space:nowrap } .attr .v { text-align:right; word-break: break-all; font-family: "Roboto Mono", Consolas, monospace; font-size: 12.5px }
  .nt { width: 460px; background:#fff; border-radius:12px; border:1px solid #e0e0e0; padding: 16px 18px }
  .nt .t { font: 500 16px Roboto, sans-serif; margin-bottom: 8px } .nt .m { font-size: 14px; line-height: 1.5; color:#333 }
  .nt .when { color:#8a8a8a; font-size: 12px; margin-top: 10px; display:flex; justify-content:space-between }
  .nt .dismiss { color:#03a9f4; font-weight:500; letter-spacing:.3px }
`;

function entities(title, rows) {
  const html = rows
    .map(([ic, name, val]) => {
      let v = esc(val);
      if (val === true || val === false) v = `<div class="tg ${val ? "on" : ""}"></div>`;
      else if (typeof val === "object" && val && val.select) v = `<span class="sel">${esc(val.select)}${icon("mdi:menu-down", 18, "#555")}</span>`;
      return `<div class="row"><div class="ic">${icon(ic, 24, "#44739e")}</div><div class="nm">${esc(name)}</div><div class="vl">${v}</div></div>`;
    })
    .join("");
  return `<style>${ENT_CSS}</style><div class="ec"><h3>${esc(title)}</h3>${html}</div>`;
}

// ── Scenes ───────────────────────────────────────────────────────────────────

async function loadCards() {
  await import("../../custom_components/cuboai/www/cuboai-card.js");
}

async function cameraCard(config = {}) {
  await loadCards();
  const card = document.createElement("cuboai-camera-card");
  card.setConfig({ type: "custom:cuboai-camera-card", device_id: DEV, show_timestamp: true, ...config });
  const wrap = document.createElement("div");
  wrap.style.width = "520px";
  wrap.appendChild(card);
  root.appendChild(wrap);
  card.hass = hass;
  await settle(900);
  card.hass = hass;
  await settle(600);
  return card;
}

const settle = (ms) => new Promise((r) => setTimeout(r, ms));

async function forms() {
  const r = await fetch("./forms.json", { cache: "no-store" });
  return r.json();
}

const SAMPLE_IPS = { [`camera_ip_${DEV}`]: "192.168.1.31", [`camera_ip_${DEV2}`]: "192.168.1.32" };

const SCENES = {
  async "card-live"() {
    await cameraCard({ show_music: false });
  },
  async "card-music"() {
    const card = await cameraCard({ show_timeline: false });
    // Only the music part below the video.
    return card;
  },
  async "card-playback"() {
    const card = await cameraCard({ show_music: false });
    // Jump to 03:12:40 the way a user does: the date field, then Go.
    const input = card.querySelector('input[type="datetime-local"]');
    const go = [...card.querySelectorAll("button")].find((b) => b.textContent.trim() === "Go");
    if (!input || !go) throw new Error("DVR controls not found");
    input.value = "2026-09-20T03:12:40";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    go.click();
    await settle(2500);
  },
  async "card-editor"() {
    await loadCards();
    const ed = document.createElement("cuboai-camera-card-editor");
    ed.setConfig({ type: "custom:cuboai-camera-card", device_id: "", show_timestamp: true });
    ed.hass = hass;
    const wrap = document.createElement("div");
    wrap.style.cssText = "width:520px;background:#fff;border-radius:12px;padding:16px;border:1px solid #e0e0e0";
    wrap.appendChild(ed);
    root.appendChild(wrap);
    ed.hass = hass;
    await settle(500);
  },
  async "timeline-card"() {
    await loadCards();
    const card = document.createElement("cuboai-timeline-card");
    card.setConfig({
      type: "custom:cuboai-timeline-card",
      title: "Last night",
      from: "19:00",
      to: "07:00",
      rows: [
        { entity: "sensor.cuboai_baby_present_baby", label: "In crib", icon: "mdi:sleep", match: "in crib", color: "#2a9d8f" },
        { entity: "sensor.cuboai_baby_present_baby", label: "Not in crib", icon: "mdi:baby-carriage", match: ["not in crib", "0"], color: "#e9c46a" },
        { entity: "sensor.cuboai_motion_baby", label: "Moving", icon: "mdi:motion-sensor", match: ["moving", "strong (2)", "strong (3)"], color: "#f4a261" },
        { entity: "sensor.cuboai_noise_level_baby", label: "Noise over 26", icon: "mdi:volume-high", above: 26, color: "#5e5ce6" },
        { entity: "sensor.cuboai_camera_state_baby", label: "Camera online", icon: "mdi:cctv", match: "online", color: "#30d158" },
        { entity: "sensor.cuboai_last_alert_baby", label: "Alerts", icon: "mdi:bell-ring", events: "alerts", color: "#ff453a" },
      ],
    });
    const wrap = document.createElement("div");
    wrap.style.width = "560px";
    wrap.appendChild(card);
    root.appendChild(wrap);
    card.hass = hass;
    await settle(900);
  },
  async "setup-login"() {
    const f = await forms();
    root.innerHTML = dialog(f["setup-login"], { values: { username: "parent@example.com", password: "sample-pass" } });
  },
  async "setup-cameras"() {
    const f = await forms();
    root.innerHTML = dialog(f["setup-cameras"]);
  },
  async "setup-options"() {
    const f = await forms();
    root.innerHTML = dialog(f["setup-options"], { values: { rtsp_port: 8557, ...SAMPLE_IPS } });
  },
  async "configure-general"() {
    const f = await forms();
    root.innerHTML = dialog(f["configure"], {
      keys: ["cameras", "download_images", "cache_youtube_songs", "enable_debug_logs", "history_sensors", "notify_on_engine_restart", "alerts_count", "max_saved_photos", "hours_back", "update_interval"],
      values: { cache_youtube_songs: true, history_sensors: true },
    });
  },
  async "configure-streaming"() {
    const f = await forms();
    root.innerHTML = dialog(f["configure"], {
      keys: ["rtsp_port", "nvr_enabled", "nvr_username", "nvr_password", `camera_ip_${DEV}`, `camera_ip_${DEV2}`, "h264_cameras", "h264_resolution", "rtsp_timestamp_cameras"],
      values: { rtsp_port: 8557, nvr_enabled: true, nvr_password: "", h264_cameras: [DEV2], rtsp_timestamp_cameras: [DEV], ...SAMPLE_IPS },
      more: "… general options above …",
    });
  },
  async "configure-protect"() {
    const f = await forms();
    root.innerHTML = dialog(f["configure"], {
      keys: ["unifi_protect_enabled", "unifi_protect_cameras", "unifi_protect_username", "unifi_protect_password", "unifi_protect_port"],
      values: { unifi_protect_enabled: true, unifi_protect_cameras: [DEV, DEV2], unifi_protect_password: "x" },
      more: "… other options above …",
    });
  },
  async "entities-sensors"() {
    root.innerHTML = entities("Baby – sensors", [
      ["mdi:cctv", "Camera State", "online"],
      ["mdi:thermometer", "Temperature", "22.5 °C"],
      ["mdi:water-percent", "Humidity", "48 %"],
      ["mdi:heart-pulse", "Mat BPM", "118 bpm"],
      ["mdi:emoticon-cry-outline", "Cry Detection Status", "On"],
      ["mdi:shield-check", "Sleep Safety Status", "On"],
      ["mdi:bell-ring", "Last Alert", "Cry detected · 05:42"],
      ["mdi:sleep", "Baby Present", "in crib"],
      ["mdi:volume-high", "Noise Level", "24"],
      ["mdi:wifi", "WiFi Quality", "82 %"],
      ["mdi:chip", "Firmware", "2.0.2273"],
    ]);
  },
  async "entities-controls"() {
    root.innerHTML = entities("Baby – controls", [
      ["mdi:lightbulb-night", "Night Light", true],
      ["mdi:brightness-6", "Night Light Brightness", "40 %"],
      ["mdi:theme-light-dark", "Night Vision", { select: "Auto" }],
      ["mdi:music", "Lullaby", "Brahms' Lullaby"],
      ["mdi:timer-music", "Lullaby Timer", "30 min"],
      ["mdi:timer-sand", "Speaker Play Time", "30 min"],
      ["mdi:sleep", "Sleep Mode", false],
      ["mdi:led-on", "Status LED", true],
      ["mdi:flip-vertical", "Flip Screen", false],
      ["mdi:baby-face-outline", "Baby Presence", true],
    ]);
  },
  async "stream-sensor"() {
    const attrs = [
      ["Stream id", `cuboai_combined_${DEV}`],
      ["Nvr rtsp url", `rtsp://192.168.1.20:8557/cuboai_combined_${DEV}`],
      ["Nvr rtsp url video only", `rtsp://192.168.1.20:8557/cuboai_combined_${DEV}?video`],
      ["Nvr auth", "none (open stream)"],
      ["Unifi protect address", "192.168.1.20:8899"],
      ["Unifi protect username", "cuboai"],
      ["Unifi protect status", "ready"],
      ["Web player url", `http://127.0.0.1:1985/stream.html?src=cuboai_combined_${DEV}`],
    ];
    root.innerHTML = `<style>${ENT_CSS}</style><div class="mi">
      <div class="hd">${icon("mdi:close", 22, "#444")}<span style="flex:1">CuboAI WebRTC Stream Baby</span>${icon("mdi:cog-outline", 22, "#444")}</div>
      <div class="big">${icon("mdi:cctv", 22, "#44739e")} cuboai_combined_${DEV}</div>
      ${attrs.map(([k, v]) => `<div class="attr"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join("")}
    </div>`;
  },
  async "notification-restart"() {
    root.innerHTML = `<style>${ENT_CSS}</style><div class="nt">
      <div class="t">CuboAI streaming engine restarted</div>
      <div class="m">The streaming engine stopped unexpectedly (exit code 139) after 3.2 hours and CuboAI restarted it
      automatically. Your cameras were unavailable for a few seconds. No action is needed — this notice exists so a
      repeating crash does not go unnoticed.<br><br>You can turn these notifications off in
      Settings → Devices &amp; Services → CuboAI → Configure.</div>
      <div class="when"><span>5 minutes ago</span><span class="dismiss">DISMISS</span></div>
    </div>`;
  },
};

(async () => {
  try {
    const scene = SCENES[shotName];
    if (!scene) throw new Error(`unknown shot ${shotName}`);
    await scene();
  } catch (e) {
    root.innerHTML = `<pre style="color:#c00">${esc(e.stack || e)}</pre>`;
    console.error(e);
  }
  document.body.dataset.ready = "1";
})();
