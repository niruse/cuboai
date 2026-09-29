// NOTE: This card must NEVER globally override customElements.define.
// A previous version wrapped it to swallow "duplicate registration" errors,
// but that intercepted EVERY custom element on the page — including Home
// Assistant's own core UI shell (home-assistant-main, ha-panel-config,
// ha-init-page, …). It raced HA's frontend bootstrap and blocked those core
// registrations as "duplicates", producing an intermittent blank white screen
// that needed many refreshes to load (issue #86). Our own two elements are
// each guarded individually with customElements.get() at their define() sites
// below, which is all that is needed.

// Find the CuboAI camera entity by the device_id ATTRIBUTE that camera.py
// publishes — never by entity-id string surgery.
//
// The previous filter required the entity id to both start with
// `camera.cuboai_` and end with `_local_camera`, and then to contain a
// "babyName" token derived by string-slicing the speaker's entity id. None of
// that is guaranteed. camera.py sets only _attr_name ("<baby> Local Camera")
// and never _attr_has_entity_name, so the object id is whatever Home Assistant
// composes from the device and entity names — `camera.cuboai_baby_baby_local_camera`
// on one install, `camera.baby_local_camera` on another, depending on HA version,
// device naming and any rename the user has made. HA's "_2" duplicate suffix
// breaks the endsWith test outright, and the babyName token (the last
// underscore-separated part of the speaker id) need not appear in the camera id
// at all.
//
// When the match failed the card silently fell through to a hardcoded
// rtsp://127.0.0.1:8555/... URL. On HA OS that port belongs to HA's own go2rtc
// WebRTC listener, which accepts the connection then tears it down:
// "connection reset by peer" (issue #89, same collision as #80).
//
// The device_id attribute is immune to all of it. Note the speaker matcher
// below already worked this way — the camera was the odd one out.
function cuboaiFindCameraState(hass, deviceId) {
  if (!hass || !hass.states) return null;
  const shaped = [];
  for (const entityId in hass.states) {
    if (!entityId.startsWith('camera.')) continue;
    const state = hass.states[entityId];
    const attrs = (state && state.attributes) || {};
    // The attribute pair IS the CuboAI camera signature.
    if (attrs.device_id === undefined || attrs.rtsp_port === undefined) continue;
    if (deviceId && (attrs.device_id === deviceId || attrs.uid === deviceId)) {
      return { entityId, state };
    }
    shaped.push({ entityId, state });
  }
  // Unpinned card, or a device_id matching nothing: only safe when there is
  // exactly one CuboAI camera. The old code took the first arbitrary match,
  // which shows the wrong baby on multi-camera setups.
  return shaped.length === 1 ? shaped[0] : null;
}

// The device_id of a camera cuboaiFindCameraState returned, else null.
function cuboaiCameraDeviceId(found) {
  const attrs = (found && found.state && found.state.attributes) || {};
  return attrs.device_id || null;
}

// The playback entity that belongs to the same camera as the live one.
//
// Matched on attributes, never on the entity id: `dvr` marks it and
// `device_id` pairs it. Issue #89 was this exact mistake made the other way
// round -- a card that tested `entityId.startsWith('camera.cuboai_')` matched
// nothing on any install, because the backend never sets
// `_attr_has_entity_name`, so the id is `camera.<baby>_recording`.
function cuboaiFindRecordingState(hass, deviceId) {
  if (!hass || !hass.states) return null;
  const shaped = [];
  for (const entityId in hass.states) {
    if (!entityId.startsWith('camera.')) continue;
    const state = hass.states[entityId];
    const attrs = (state && state.attributes) || {};
    if (attrs.dvr !== true) continue;
    if (deviceId && attrs.device_id === deviceId) return { entityId, state };
    shaped.push({ entityId, state });
  }
  // Same rule as the live camera: an unpinned card is only safe to guess for
  // when there is exactly one, or it shows the wrong baby.
  return shaped.length === 1 ? shaped[0] : null;
}

// What the card's audio setting asks for: sound (true) or silence (false).
// 'unmuted' / 'muted' are fixed; 'remember' takes the shared (cross-device)
// choice first, then this browser's saved one, then silence.
function cuboaiWantUnmuted(mode, sharedMuted, savedMuted) {
  if (mode === 'unmuted') return true;
  if (mode === 'muted') return false;
  if (sharedMuted !== undefined) return !sharedMuted;
  return savedMuted ? savedMuted !== 'true' : false;
}

// The mute state the player starts in, given what the audio setting wants.
// Browsers block UNMUTED autoplay, and a video that can't autoplay leaves
// webrtc-camera with no rendered controls (the speaker button vanished on
// "Always Unmute") — so the card starts muted and brings the sound up on the
// first interaction (the video init). Apple WebKit is the exception: there
// the native speaker owns mute and the card never unmutes by script, so a
// muted start stayed muted whatever the setting said (since v2.4.0). On Apple
// the setting is therefore the STARTING state, as in v2.3.x; if the phone
// refuses sound without a tap, video-rtc falls back to muted by itself.
function cuboaiStartMuted(wantUnmuted, vendor) {
  return String(vendor || '').includes('Apple') ? !wantUnmuted : true;
}

// Picture bridge. When the player swaps its source -- Wi-Fi to mobile data
// (WebRTC dies, MSE starts), a reconnect -- the <video> is black until the new
// stream's first picture: up to the camera's 4 s keyframe interval plus the
// reconnect, which video-rtc may delay by up to 15 s. The card shows the last
// good picture on a canvas over the video instead, with "Reconnecting…" once
// the gap outlasts a blink, until the new source's picture is really on
// screen.
// - Copied about once a second while the video really plays, not at the swap:
//   by then an iPhone may have nothing left to copy. Three triggers, because an
//   iPhone playing WebRTC may not fire timeupdate: presented frames
//   (requestVideoFrameCallback), timeupdate, and a 1 s timer that only copies
//   while currentTime moves. A copy that came out empty (transparent or pure
//   black) never replaces a good one.
// - The ending of a WebRTC stream starts the bridge at once.
// - Held up to CUBOAI_BRIDGE_WAIT_MS while the player has no new source yet,
//   then CUBOAI_BRIDGE_MAX_MS once it has; a copy older than
//   CUBOAI_BRIDGE_STALE_MS is never shown, and a copy let go is not shown again
//   -- a frozen picture cannot pass for live. With no picture, the label alone
//   says what the black is.
// - On an iPhone a new WebRTC stream reports its first frame before it is on
//   screen, so the hide waits for the picture to settle.
// - The card's own switches to and from a recording are not bridged (quiet).
// Hooks only this element's own src/srcObject; the WebRTC Camera files are
// untouched.
const CUBOAI_BRIDGE_MAX_MS = 8000;         // after the new source is attached
const CUBOAI_BRIDGE_WAIT_MS = 25000;       // until the player attaches one
const CUBOAI_BRIDGE_STALE_MS = 60000;      // an older copy is not shown
const CUBOAI_BRIDGE_LABEL_MS = 600;        // a swap faster than this shows no label
const CUBOAI_BRIDGE_SNAP_MS = 1000;        // how often the last good picture is copied
const CUBOAI_BRIDGE_PARK_MS = 60000;       // a detached player's timer stops after this
const CUBOAI_BRIDGE_SETTLE_RTC_MS = 800;   // WebRTC: decoded is not yet on screen (iOS)
const CUBOAI_BRIDGE_SETTLE_MSE_MS = 200;
const CUBOAI_BRIDGE_COPY_W = 960;          // the copy is at most this wide
// The last good picture of each camera, kept for the page rather than inside
// one card: after a network change Home Assistant rebuilds a YAML dashboard's
// cards (seen at a Wi-Fi -> 5G switch: a new card, a new empty <video>, black
// while its stream starts). A new card for the same camera starts from the old
// card's picture. Keyed by device id: { canvas, at }.
const cuboaiLastPictures = new Map();

function cuboaiBridgeGap(video, deviceId) {
  if (!video || video.__cuboBridge) return;
  const host = video.parentNode;
  const doc = video.ownerDocument;
  if (!host || !doc || typeof host.insertBefore !== 'function') return;
  const canvas = doc.createElement('canvas');
  canvas.style.cssText = 'position: absolute; left: 0; top: 0; width: 100%; height: 100%; object-fit: contain; pointer-events: none; display: none;';
  const work = doc.createElement('canvas');   // copies land here first, and are checked
  const label = doc.createElement('div');
  label.textContent = 'Reconnecting…';
  label.style.cssText = 'position: absolute; left: 50%; bottom: 14px; transform: translateX(-50%); z-index: 1; padding: 3px 10px; border-radius: 10px; background: rgba(0,0,0,0.55); color: #fff; font: 12px/1.4 sans-serif; pointer-events: none; display: none;';
  host.insertBefore(canvas, video.nextSibling);
  // The canvas pans and zooms with the video (it sits in the digital-zoom
  // layer); the label sits above that layer so zoom never moves it off screen.
  const outer = host.parentNode;
  if (outer && typeof outer.appendChild === 'function') outer.appendChild(label);
  else host.insertBefore(label, canvas.nextSibling);
  const st = {
    canvas, label, gap: false, showing: false, cap: 0, labelTimer: 0, armed: false, settle: 0,
    snapAt: -Infinity, hasPicture: false, played: false, lastCt: null, poll: 0, idleMs: 0,
    loopPending: false, loopGen: 0, quietUntil: 0, firstAt: 0, newFrames: 0, hideGen: 0, gapAt: 0, blackSeen: false,
    snaps: 0, empty: 0, tu: 0, frames: 0, polls: 0, drawErr: null, onDiag: null,
  };
  video.__cuboBridge = st;
  const now = () => (typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now());
  const diag = (ev, extra) => {
    if (typeof st.onDiag !== 'function') return;
    try { st.onDiag(Object.assign({ ev }, extra || {})); } catch (e) { /* never break playback */ }
  };
  const isStream = (o) => !!o && typeof o.getVideoTracks === 'function';

  const hide = (why) => {
    clearTimeout(st.cap);
    clearTimeout(st.labelTimer);
    clearTimeout(st.settle);
    st.cap = 0;
    st.labelTimer = 0;
    st.settle = 0;
    st.armed = false;
    if (!st.gap) return;
    diag('hide', { why: why || 'frame', held: st.showing, rs: video.readyState, paused: !!video.paused,
      nf: st.newFrames, ms: Math.round(now() - st.gapAt) });
    // A picture let go by the cap is not shown again: only a fresh copy is.
    if (why === 'cap') st.hasPicture = false;
    st.gap = false;
    st.showing = false;
    canvas.style.display = 'none';
    label.style.display = 'none';
  };
  st.hide = hide;
  const capIt = (ms) => {
    clearTimeout(st.cap);
    st.cap = setTimeout(() => hide('cap'), ms || CUBOAI_BRIDGE_MAX_MS);
  };
  // A WebRTC stream whose video track has ended or stopped delivering shows
  // black on an iPhone: never copy it.
  const streamLive = () => {
    const so = video.srcObject;
    if (!isStream(so)) return true;
    const tracks = so.getVideoTracks();
    return tracks.length > 0 && tracks.every((t) => t.readyState === 'live' && !t.muted);
  };
  // True unless a small patch of the copy is all transparent or all pure black
  // (what a player with nothing to show hands back). A night-vision picture
  // always has noise. A copy that cannot be read is trusted.
  const looksLikeAPicture = (ctx, w, h) => {
    try {
      const n = Math.min(8, w, h);
      const d = ctx.getImageData((w - n) >> 1, (h - n) >> 1, n, n).data;
      for (let i = 0; i < d.length; i += 4) if (d[i + 3] !== 0 && (d[i] || d[i + 1] || d[i + 2])) return true;
      return false;
    } catch (e) {
      return true;
    }
  };
  // Copy the picture on screen: into `work`, checked, then onto the canvas.
  const snap = () => {
    if (st.gap || !(video.readyState >= 2) || !video.videoWidth || !video.videoHeight || !streamLive()) return false;
    if (doc.hidden) return false;
    const w = Math.min(CUBOAI_BRIDGE_COPY_W, video.videoWidth);
    const h = Math.round((video.videoHeight * w) / video.videoWidth);
    try {
      if (work.width !== w) work.width = w;
      if (work.height !== h) work.height = h;
      const wctx = work.getContext('2d');
      wctx.drawImage(video, 0, 0, w, h);
      if (!looksLikeAPicture(wctx, w, h)) {
        st.empty += 1;
        return false;
      }
      if (canvas.width !== w) canvas.width = w;
      if (canvas.height !== h) canvas.height = h;
      canvas.getContext('2d').drawImage(work, 0, 0);
    } catch (e) {
      st.drawErr = String((e && e.name) || e);
      return false;
    }
    st.snapAt = now();
    st.hasPicture = true;
    st.played = true;
    st.snaps += 1;
    if (deviceId) cuboaiLastPictures.set(deviceId, { canvas, at: st.snapAt });
    return true;
  };
  const maybeSnap = () => { if (now() - st.snapAt >= CUBOAI_BRIDGE_SNAP_MS) snap(); };
  // Trigger 1: each presented frame. A pending callback may be dropped with
  // its source, so a swap re-arms it.
  // A generation per registration: a swap starts a fresh loop, and a callback
  // of an older one that did survive the swap ends itself (never two loops).
  const armLoop = (fresh) => {
    if (typeof video.requestVideoFrameCallback !== 'function') return;
    if (fresh) st.loopGen += 1;
    else if (st.loopPending) return;
    const gen = st.loopGen;
    const frameLoop = () => {
      if (gen !== st.loopGen) return;
      st.loopPending = false;
      st.frames += 1;
      maybeSnap();
      startPoll();
      armLoop();
    };
    try { video.requestVideoFrameCallback(frameLoop); st.loopPending = true; } catch (e) { /* not for this source */ }
  };
  // Trigger 3: a timer, copying only when currentTime moves (really playing).
  // Parked while the player is off the page; any sign of playback restarts it.
  const startPoll = () => {
    if (st.poll) return;
    st.idleMs = 0;
    st.poll = setInterval(() => {
      if (!video.isConnected) {
        st.idleMs += CUBOAI_BRIDGE_SNAP_MS;
        if (st.idleMs >= CUBOAI_BRIDGE_PARK_MS) { clearInterval(st.poll); st.poll = 0; }
        return;
      }
      st.idleMs = 0;
      const ct = video.currentTime;
      if (ct === st.lastCt) return;
      st.lastCt = ct;
      st.polls += 1;
      if (video.readyState >= 2) st.played = true;
      maybeSnap();
    }, CUBOAI_BRIDGE_SNAP_MS);
  };
  armLoop();
  // Trigger 2: timeupdate while the video plays.
  video.addEventListener('timeupdate', () => { st.tu += 1; maybeSnap(); startPoll(); });
  video.addEventListener('play', () => startPoll());
  startPoll();
  // Start bridging a gap: the last picture if there is a fresh one, and the
  // label if the gap outlasts a blink. Only once something has played -- the
  // first load of the card is not a reconnect -- and never for the card's own
  // switch to or from a recording.
  const startGap = () => {
    if (st.gap) return true;
    if (now() < st.quietUntil) return false;
    if (!st.hasPicture) snap();
    if (!st.played) return false;
    st.gap = true;
    st.gapAt = now();
    st.blackSeen = false;
    st.firstAt = 0;
    if (st.hasPicture && now() - st.snapAt <= CUBOAI_BRIDGE_STALE_MS) {
      st.showing = true;
      canvas.style.display = 'block';
    }
    // Only while no new picture has arrived: a new one that is just settling
    // (WebRTC at home, within a second) is no reconnect to announce.
    st.labelTimer = setTimeout(() => { if (st.gap && !st.firstAt) label.style.display = 'block'; }, CUBOAI_BRIDGE_LABEL_MS);
    return true;
  };
  // The card's own switch to or from a recording: no bridge until the next
  // picture (or 15 s).
  st.quiet = () => {
    st.quietUntil = now() + 15000;
    hide('quiet');
  };
  const brightness = () => {
    if (!st.hasPicture) return null;
    try {
      const n = Math.min(8, canvas.width, canvas.height);
      const d = canvas.getContext('2d').getImageData((canvas.width - n) >> 1, (canvas.height - n) >> 1, n, n).data;
      let sum = 0;
      for (let i = 0; i < d.length; i += 4) sum += d[i] + d[i + 1] + d[i + 2];
      return Math.round(sum / (3 * (d.length / 4)));
    } catch (e) {
      return 'err';
    }
  };
  const state = (extra) => Object.assign({
    rs: video.readyState, vw: video.videoWidth, paused: !!video.paused, live: streamLive(), has: st.hasPicture,
    played: st.played, age: st.hasPicture ? Math.round(now() - st.snapAt) : null, snaps: st.snaps, empty: st.empty,
    tu: st.tu, frames: st.frames, polls: st.polls, rvfc: typeof video.requestVideoFrameCallback === 'function',
    err: st.drawErr, lum: typeof st.onDiag === 'function' ? brightness() : null,
  }, extra || {});
  // Hide once the new source's picture is really on screen: playing, a frame
  // decoded, and settled -- longer for WebRTC, whose first decoded frames an
  // iPhone reports before its display layer shows them.
  // The new source's picture, looked at: a tiny draw of the frame it shows.
  // An iPhone reports an MSE stream "playing" as soon as its sound arrives,
  // while the picture waits for the camera's next keyframe (black), and a new
  // WebRTC stream reports frames before they are on screen.
  const probe = doc.createElement('canvas');
  const onScreen = () => {
    try {
      probe.width = 32;
      probe.height = 18;
      const pctx = probe.getContext('2d');
      pctx.drawImage(video, 0, 0, 32, 18);
      return looksLikeAPicture(pctx, 32, 18);
    } catch (e) {
      return true;
    }
  };
  // The new source has a picture but sits paused: an iPhone does not always
  // start a stream attached without a tap (recorded: the MSE stream after a
  // Wi-Fi -> 5G switch had frames, readyState 4, and stayed paused). Press play
  // for it, at most every 2 s; if the phone refuses sound without a tap, play
  // muted -- what the player itself does on such a refusal -- so the picture
  // moves and the speaker button brings the sound back.
  const kickPlay = () => {
    if (st.kickAt && now() - st.kickAt < 2000) return;
    st.kickAt = now();
    let p;
    try {
      p = video.play();
    } catch (e) {
      p = Promise.reject(e);
    }
    Promise.resolve(p).then(
      () => diag('kick', { ok: true, muted: !!video.muted }),
      (e) => {
        const err = (e && e.name) || String(e);
        diag('kick', { ok: false, err, muted: !!video.muted });
        if (err === 'NotAllowedError' && !video.muted) {
          video.muted = true;
          try { Promise.resolve(video.play()).catch(() => {}); } catch (e2) { /* nothing more to try */ }
        }
      },
    );
  };
  // Hide once the new source's picture is really on screen: playing, settled,
  // frames presented (where this browser reports them), and a drawn frame that
  // is a picture, not black.
  const tryHide = (why) => {
    if (!st.armed) return true;
    if (video.paused && video.readyState >= 2 && video.videoWidth) kickPlay();
    if (video.paused || !(video.readyState >= 2) || !video.videoWidth) return false;
    if (!st.firstAt) st.firstAt = now();
    const rtc = isStream(video.srcObject);
    const settle = rtc ? CUBOAI_BRIDGE_SETTLE_RTC_MS : CUBOAI_BRIDGE_SETTLE_MSE_MS;
    const needFrames = st.frames > 0 ? (rtc ? 3 : 2) : 0;   // frame callbacks work on this browser
    if (now() - st.firstAt < settle || st.newFrames < needFrames) return false;
    if (!onScreen()) {
      if (!st.blackSeen) {
        st.blackSeen = true;
        diag('black', { rs: video.readyState, nf: st.newFrames, ms: Math.round(now() - st.gapAt), rtc });
      }
      return false;
    }
    hide(why);
    return true;
  };
  // Frames presented by the NEW source. Re-armed at every swap: a callback
  // pending from before the source changed may be dropped with it.
  const armFrames = () => {
    const gen = ++st.hideGen;
    st.newFrames = 0;
    const onFrame = () => {
      if (!st.armed || gen !== st.hideGen) return;
      st.newFrames += 1;
      if (!tryHide('frame')) {
        try { video.requestVideoFrameCallback(onFrame); } catch (e) { /* not for this source */ }
      }
    };
    if (typeof video.requestVideoFrameCallback === 'function') {
      try { video.requestVideoFrameCallback(onFrame); } catch (e) { /* not for this source */ }
    }
  };
  const armHide = (capMs) => {
    capIt(capMs);
    st.armed = true;
    armFrames();
    const poll = () => {
      clearTimeout(st.settle);
      if (st.armed && !tryHide('playing')) st.settle = setTimeout(poll, 100);
    };
    video.addEventListener('loadeddata', poll, { once: true });
    video.addEventListener('playing', poll, { once: true });
  };
  // A WebRTC stream that ends (the player closes it on a network change) goes
  // black or freezes before the next stream is even requested: bridge from that
  // moment, for as long as the player may take to attach the next one.
  const watchStream = (so) => {
    if (!isStream(so)) return;
    for (const t of so.getVideoTracks()) {
      if (typeof t.addEventListener !== 'function') continue;
      t.addEventListener('ended', () => {
        if (video.srcObject !== so) return;
        diag('ended', state());
        if (startGap()) armHide(CUBOAI_BRIDGE_WAIT_MS);
      });
    }
  };
  // The card usually hooks in while the player is already playing.
  watchStream(video.srcObject);
  // A new card whose player has no picture yet, for a camera another card of
  // this page showed a moment ago: start from that picture (the handover after
  // Home Assistant rebuilt the dashboard), until this player's own is shown.
  const prior = deviceId ? cuboaiLastPictures.get(deviceId) : null;
  if (prior && prior.canvas !== canvas && !(video.readyState >= 2) && now() - prior.at <= CUBOAI_BRIDGE_STALE_MS) {
    try {
      canvas.width = prior.canvas.width;
      canvas.height = prior.canvas.height;
      canvas.getContext('2d').drawImage(prior.canvas, 0, 0);
      st.hasPicture = true;
      st.played = true;
      st.snapAt = prior.at;
      diag('handover', { age: Math.round(now() - prior.at) });
      if (startGap()) armHide(CUBOAI_BRIDGE_WAIT_MS);
    } catch (e) {
      /* no picture to hand over: this card starts like a fresh one */
    }
  }

  for (const prop of ['srcObject', 'src']) {
    let proto = Object.getPrototypeOf(video);
    let desc = null;
    while (proto && !desc) {
      desc = Object.getOwnPropertyDescriptor(proto, prop);
      proto = Object.getPrototypeOf(proto);
    }
    if (!desc || !desc.set || !desc.get) continue;
    Object.defineProperty(video, prop, {
      configurable: true,
      enumerable: true,
      get() { return desc.get.call(this); },
      set(v) {
        // The freshest picture if the one on screen is still good, else the
        // last copy; then the swap.
        if (!st.gap && now() - st.snapAt >= 250) snap();
        const to = v ? ((v.constructor && v.constructor.name) || typeof v) : String(v);
        diag('swap', state({ prop, to, gap: st.gap }));
        const held = startGap();
        desc.set.call(this, v);
        if (prop === 'srcObject') watchStream(v);
        armLoop(true);
        startPoll();
        if (!held) return;
        // A new source: hide once its picture is on screen, within the cap.
        // Cleared (a disconnect): hold for the source that follows.
        if (v) armHide(CUBOAI_BRIDGE_MAX_MS); else capIt(CUBOAI_BRIDGE_WAIT_MS);
      },
    });
  }
}

// Revive a player left stuck by a network change. Two ways it gets stuck:
// 1. webrtc-camera asks Home Assistant to sign a new stream URL while Home
//    Assistant's own connection is down; the request fails ("error") or never
//    answers ("Loading.."), and it never tries again (seen on iOS 26: the WebRTC
//    stream ended, no new source for 25 s);
// 2. a stream connection being set up over the old network hangs without ever
//    closing, so the player waits for it for good (seen: a card Home Assistant
//    re-created 2 s before the switch never showed a picture).
// Fast path: a player that has played and has had neither a stream nor a
// connection attempt for CUBOAI_REVIVE_AFTER_MS is dialled again. Watchdog: a
// player with no moving picture for CUBOAI_STUCK_MS is restarted from scratch
// (disconnect, then dial once the old connection is gone). Both wait for Home
// Assistant's connection and leave alone a card that is not on screen, a page
// in the background, the card's own recording playback and a video that is
// paused with a picture (the user's pause). Returns what it did (for
// diagnostics), else null.
const CUBOAI_REVIVE_AFTER_MS = 3000;
const CUBOAI_REVIVE_EVERY_MS = 3000;
const CUBOAI_STUCK_MS = 10000;

function cuboaiPlayerMode(p) {
  const modeEl = p && typeof p.querySelector === 'function' ? p.querySelector('.mode') : null;
  const mode = modeEl ? String(modeEl.innerText || '') : '';
  // A signing that never answered leaves "Loading.." up, and webrtc-camera
  // refuses every retry while it says so.
  if (modeEl && mode === 'Loading..') modeEl.innerText = '';
  return mode || 'blank';
}

function cuboaiRevivePlayer(card, t) {
  const p = card && card.content;
  const video = card && typeof card._cuboMedia === 'function' ? card._cuboMedia().video : null;
  const doc = video && video.ownerDocument;
  if (!p || typeof p.onconnect !== 'function' || !p.isConnected || card._dvrRedial || card._dvrPlaying || (doc && doc.hidden)) {
    card._deadSince = null;
    card._progressAt = null;
    card._restartAt = null;
    return null;
  }
  // Progress: the picture moving, or paused with a picture (the user's pause).
  const ct = video ? video.currentTime : null;
  if (card._progressAt == null || ct !== card._progressCt || (video && video.paused && video.readyState >= 3)) {
    card._progressAt = t;
    card._progressCt = ct;
  }
  // A restart under way: dial as soon as the old connection is gone.
  if (card._restartAt != null) {
    if (!p.ws && !p.pc) {
      card._restartAt = null;
      const mode = cuboaiPlayerMode(p);
      try { p.onconnect(); } catch (e) { return 'threw'; }
      return 'restarted:' + mode;
    }
    if (t - card._restartAt >= CUBOAI_REVIVE_EVERY_MS) card._restartAt = null;
    return null;
  }
  const played = !!(video && video.__cuboBridge && video.__cuboBridge.played);
  if (played && !p.ws && !p.pc) {
    if (card._deadSince == null) card._deadSince = t;
  } else {
    card._deadSince = null;
  }
  const conn = card._hass && card._hass.connection;
  if (conn && conn.connected === false) return null;
  if (card._deadSince != null && t - card._deadSince >= CUBOAI_REVIVE_AFTER_MS
      && (card._reviveAt == null || t - card._reviveAt >= CUBOAI_REVIVE_EVERY_MS)) {
    card._reviveAt = t;
    const mode = cuboaiPlayerMode(p);
    try { p.onconnect(); } catch (e) { return 'threw'; }
    return mode;
  }
  if (t - card._progressAt >= CUBOAI_STUCK_MS && typeof p.ondisconnect === 'function') {
    card._progressAt = t;
    card._restartAt = t;
    const mode = cuboaiPlayerMode(p);
    try { p.ondisconnect(); } catch (e) { /* the dial below still runs */ }
    return 'stuck:' + mode;
  }
  return null;
}

// Home Assistant sometimes replaces a dashboard's cards with new ones (seen on
// the iPhone app about 8 s after a page load). With `background: true` the old
// card's player keeps its stream up although nobody can see it: a second
// stream on mobile data, and a second consumer at the camera. A card off the
// page for CUBOAI_REPLACED_MS while another card shows the same camera stops
// its player; put back on the page, webrtc-camera dials again by itself. A
// card merely off screen (another dashboard view) keeps playing, as before.
const CUBOAI_REPLACED_MS = 10000;
const cuboaiCardRefs = [];

function cuboaiRegisterCard(card) {
  if (card.__cuboRegistered || typeof WeakRef !== 'function') return;
  card.__cuboRegistered = true;
  cuboaiCardRefs.push(new WeakRef(card));
}

function cuboaiStopReplaced(card, t) {
  if (card.isConnected) {
    card._detachedAt = null;
    card._stoppedAsReplaced = false;
    return false;
  }
  if (card._detachedAt == null) {
    card._detachedAt = t;
    return false;
  }
  if (card._stoppedAsReplaced || t - card._detachedAt < CUBOAI_REPLACED_MS) return false;
  const id = card._liveDeviceId;
  const p = card.content;
  if (!id || !p || typeof p.ondisconnect !== 'function') return false;
  for (let i = cuboaiCardRefs.length - 1; i >= 0; i -= 1) {
    const other = cuboaiCardRefs[i].deref();
    if (!other) {
      cuboaiCardRefs.splice(i, 1);
      continue;
    }
    if (other !== card && other.isConnected && other._liveDeviceId === id) {
      card._stoppedAsReplaced = true;
      try { p.ondisconnect(); } catch (e) { /* already stopped */ }
      return true;
    }
  }
  return false;
}

// Single source of truth for the webrtc-camera child config. This object was
// duplicated at three call sites, so flags added to one copy silently missed
// the others. There is deliberately NO `url:` fallback — resolving through the
// entity makes camera.stream_source() supply the healed RTSP port, the NVR
// credentials, and the producer pre-warm, none of which a hand-built URL has.
// The microphone is NOT part of it: two-way audio has its own path over Home
// Assistant's websocket (_cuboStartMic), so a mic tap never reconnects the
// picture. Adding the mic to the video connection reconnected it on every tap
// (black picture, and on iOS the session came back without the mic).
function cuboaiWebrtcConfig(found, isMuted) {
  const attrs = (found && found.state && found.state.attributes) || {};
  return {
    type: 'custom:webrtc-camera',
    entity: found.entityId,
    mode: 'webrtc,mse',
    ui: true,
    muted: isMuted,
    poster: attrs.entity_picture || undefined,
    // Baby monitor: keep the stream (and its audio) running when the window is
    // minimized or the tab is hidden. Without this, video-rtc disconnects ~5s
    // after the page hides and the sound stops.
    background: true,
    media: 'video,audio',
  };
}

// ── cuboai mic helpers ──
// Two-way audio over Home Assistant's own websocket. The card captures the
// microphone, resamples it to 16 kHz 16-bit mono and sends it as binary
// messages -- [handler id byte] + PCM -- to the `cuboai/talk` command, which
// feeds a camera talk session running in a child process on the HA box.
// Pure helpers only (no DOM), so tests/test_mic_capture.py runs them in Node.
const CUBOAI_MIC_RATE = 16000;            // what the card sends: s16le mono
const CUBOAI_MIC_CHUNK = 640;             // samples per message: 40 ms, 1281 bytes, 25 a second
const CUBOAI_MIC_MAX_BUFFERED = 32768;    // bytes already queued in the socket (~1 s): drop, don't lag
const CUBOAI_MIC_LOWPASS_ABOVE = 17600;   // context rates above this get the anti-alias filter
const CUBOAI_MIC_SUBSCRIBE_MS = 10000;    // Home Assistant must accept the talk within this
const CUBOAI_MIC_NO_LIVE_MS = 40000;      // ...and the camera start pulling audio within this (server: 35 s)
const CUBOAI_MIC_BACKUP_MS = 125000;      // hard stop even if the server's 120 s cap never arrives
const CUBOAI_MIC_ENDED_WAIT_MS = 4000;    // after the end message, wait this long for `ended`

// Test mode (a dry run: everything but the camera, nothing plays). Fails safe:
// ANY value given for a switch turns it on, except the literal `false`. YAML
// hands `talk_dry_run: "true"`, `yes`, `on` or `1` over as strings or numbers,
// and each of those quietly becoming a real, audible talk in the nursery is the
// one mistake this must never allow. Only a missing switch or `false` is off.
function cuboaiMicDryRunAsked(value) {
  return value !== undefined && value !== false;
}

// Browsers expose the microphone only in a secure context (https, or
// localhost). On a plain http:// LAN address `navigator.mediaDevices` does not
// exist at all, whatever the transport.
function cuboaiMicCanCapture(win) {
  const md = win && win.navigator && win.navigator.mediaDevices;
  return !!(win && win.isSecureContext && md && typeof md.getUserMedia === 'function');
}

// Stateful linear interpolation, float [-1, 1] in, Int16 out, continuous
// across pieces of any size: it keeps the fractional read position and the
// previous piece's last sample. Anti-aliasing happens before it, in the audio
// graph (two native low-pass biquads). Out-of-range input is clamped, never
// wrapped round to the opposite sign.
function cuboaiMicResampler(inRate, outRate = CUBOAI_MIC_RATE) {
  const step = inRate / outRate;
  let pos = 1, last = 0;                  // virtual input: v[0] = last, v[i] = input[i - 1]
  return (input) => {
    const n = input ? input.length : 0;
    if (!n) return new Int16Array(0);
    const out = new Int16Array(Math.max(0, Math.ceil((n - pos) / step)) + 1);
    let k = 0;
    while (pos < n) {
      const i = pos | 0, f = pos - i;
      const a = i === 0 ? last : input[i - 1], b = input[i];
      let s = a + (b - a) * f;
      s = s < -1 ? -1 : s > 1 ? 1 : s;
      out[k++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      pos += step;
    }
    pos -= n;
    last = input[n - 1];
    return out.subarray(0, k);
  };
}

// Fixed-size frames out of pieces of any size. The remainder waits for the
// next piece; nothing is padded and nothing is lost.
function cuboaiMicChunker(size = CUBOAI_MIC_CHUNK) {
  let buf = new Int16Array(size), len = 0;
  return (pcm) => {
    const out = [];
    for (let i = 0; pcm && i < pcm.length;) {
      const n = Math.min(size - len, pcm.length - i);
      buf.set(pcm.subarray(i, i + n), len);
      len += n;
      i += n;
      if (len === size) { out.push(buf); buf = new Int16Array(size); len = 0; }
    }
    return out;
  };
}

function cuboaiMicCheckId(id) {
  if (!Number.isInteger(id) || id < 1 || id > 255) throw new RangeError(`cuboai mic: bad handler id ${id}`);
}

// One binary websocket message: byte 0 is the binary handler id Home Assistant
// gave this talk (1..255), the PCM follows. The Int16Array's own bytes are
// little-endian on every browser platform, which is what s16le means -- HA's
// Assist sends its microphone the same way.
function cuboaiMicFrame(id, pcm) {
  cuboaiMicCheckId(id);
  const frame = new Uint8Array(1 + pcm.byteLength);
  frame[0] = id;
  frame.set(new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength), 1);
  return frame;
}

// End of the talk: the handler id alone, exactly one byte. A 0-byte binary
// message makes Home Assistant drop the WHOLE websocket connection.
function cuboaiMicEndFrame(id) {
  cuboaiMicCheckId(id);
  return new Uint8Array([id]);
}

// May a frame go out on this socket now? 'closed' (not open: never queue into
// a dead socket), 'backlog' (over ~1 s already waiting, e.g. a slow mobile
// uplink: drop this frame rather than let the voice fall ever further
// behind), or 'ok'.
function cuboaiMicSendGate(sock) {
  if (!sock || sock.readyState !== 1) return 'closed';
  if (sock.bufferedAmount > CUBOAI_MIC_MAX_BUFFERED) return 'backlog';
  return 'ok';
}

// The button's life: idle -tap-> connecting -live-> live; a tap or any stop
// while connecting or live -> stopping -stopped-> idle. Anything else is
// ignored: a late `live` after a cancel must not light the button up again,
// and a tap while stopping must not start a second talk.
function cuboaiMicNextState(state, ev) {
  switch (state) {
    case 'idle': return ev === 'tap' ? 'connecting' : 'idle';
    case 'connecting':
      if (ev === 'live') return 'live';
      return ev === 'tap' || ev === 'stop' ? 'stopping' : 'connecting';
    case 'live': return ev === 'tap' || ev === 'stop' ? 'stopping' : 'live';
    case 'stopping': return ev === 'stopped' ? 'idle' : 'stopping';
    default: return 'idle';
  }
}

const CUBOAI_MIC_TEXT = {
  no_capture: 'This browser does not give web pages a microphone.',
  no_camera: 'Two-way audio: no CuboAI camera found for this card.',
  NotAllowedError: 'Microphone access was not allowed (iPhone: Settings › Home Assistant › Microphone).',
  NotFoundError: 'No microphone found.',
  NotReadableError: 'Microphone busy (a call or another app is using it).',
  // Refusals from Home Assistant (the talk never started).
  not_found: 'Two-way audio: this camera is not set up in Home Assistant.',
  unauthorized: "Your Home Assistant user may not use this camera's speaker.",
  speaker_busy: 'The camera speaker is playing. Stop the music first, then talk.',
  busy: 'Someone is already talking through this camera.',
  spawn_failed: 'Two-way audio could not start on Home Assistant (see its log).',
  unknown_command: 'Two-way audio needs the CuboAI integration updated and Home Assistant restarted.',
  timeout: 'Home Assistant did not answer the talk request in time.',
  // The camera side.
  camera_unreachable: "Couldn't reach the camera.",
  camera_refused: "The camera didn't accept the talk (it may be busy).",
  camera_lost: 'Lost the connection to the camera.',
  camera_timeout: "The camera didn't start the talk in time.",
  internal: 'Two-way audio failed (see the Home Assistant log).',
  // Why a talk ended.
  idle: 'Talk stopped: no audio arrived from this device.',
  max_duration: 'Talk stopped at the time limit.',
  disconnected: 'Talk stopped: the connection to Home Assistant dropped.',
  hidden: 'Talk stopped: the app went to the background.',
  track_ended: 'Talk stopped: another app took the microphone.',
  audio_interrupted: 'Talk stopped: the phone interrupted the audio (a call or Siri).',
  dry_run_refused: 'TEST MODE was not confirmed by Home Assistant, so nothing was sent.',
  shutdown: 'Talk stopped: Home Assistant is restarting.',
  unloaded: 'Talk stopped: the camera was reloaded in Home Assistant.',
  child_exit: 'Talk stopped on the Home Assistant side (see its log).',
  client_end: '',
  card_removed: '',
};
const CUBOAI_MIC_ALIAS = {
  SecurityError: 'NotAllowedError',
  OverconstrainedError: 'NotFoundError',
  AbortError: 'NotReadableError',
};

// Text for anything that stops or refuses a talk: a code or reason string
// ('speaker_busy', 'idle', ...), a Home Assistant error {code, message}, or a
// getUserMedia exception (by its name). `where` is the page's origin, shown
// when the page is not secure. '' means "say nothing" (the user's own stop).
function cuboaiMicErrorText(errOrCode, where) {
  if (errOrCode === 'insecure') {
    return 'The microphone needs Home Assistant opened over HTTPS. This page is ' + (where || 'http://…') +
      ', which browsers block. Open Home Assistant through its https:// address (in the HA app, the ' +
      'home/internal URL too).';
  }
  const e = errOrCode || {};
  // A DOMException also has a numeric legacy `code`; only a string code is
  // Home Assistant's, otherwise the exception's name decides.
  const key = typeof e === 'string' ? e : (typeof e.code === 'string' ? e.code : e.name);
  const k = CUBOAI_MIC_ALIAS[key] || key;
  if (typeof k === 'string' && Object.prototype.hasOwnProperty.call(CUBOAI_MIC_TEXT, k)) return CUBOAI_MIC_TEXT[k];
  if (typeof e === 'string') return `Talk ended (${e}).`;
  return 'Two-way audio failed: ' + (e.message || key || String(errOrCode));
}

// The capture tap, loaded into the AudioContext from a Blob URL: copies
// channel 0 into 1024-frame batches and posts each batch to the main thread
// (transferred, not copied). 'stop' ends processing.
const CUBOAI_MIC_WORKLET_SRC = `
class CuboaiMicTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.n = 1024; this.buf = new Float32Array(this.n); this.len = 0; this.on = true;
    this.port.onmessage = (e) => { if (e.data === 'stop') this.on = false; };
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (this.on && ch) {
      for (let i = 0; i < ch.length; i++) {
        this.buf[this.len++] = ch[i];
        if (this.len === this.n) {
          this.port.postMessage(this.buf, [this.buf.buffer]);
          this.buf = new Float32Array(this.n); this.len = 0;
        }
      }
    }
    return this.on;
  }
}
registerProcessor('cuboai-mic-tap', CuboaiMicTap);
`;
// ── end mic helpers ──

// MSE playback away from home. On Wi-Fi a phone plays WebRTC (Opus audio).
// On mobile data through the Cloudflare tunnel WebRTC can't connect, so
// video-rtc falls back to MSE (H.264 + the camera's AAC), and two things in
// its MSE `updateend` handler (WebRTC Camera 3.6.1) broke the sound there:
//  1. It sets `playbackRate = seconds buffered ahead` (floor 0.1) on every
//     fragment, so over bursty delivery the speed swings 0.1x-3x many times
//     a second. The stream carried a song continuously while the iPhone
//     played one-second fragments of it.
//  2. It trims the SourceBuffer to its last 5 s, and MSE removes video up to
//     the NEXT keyframe — this camera sends one every 4 s — so a trim can
//     delete the very frames being played whenever the playhead keeps any
//     cushion. The picture then freezes and the sound with it (why a plain
//     speed clamp, which kept a cushion, froze every few seconds).
// So the card (a) never lets a trim come closer than two keyframe intervals
// behind the playhead and (b) owns the speed: normal while 2-4.2 s are
// buffered, a little slower below that, a little faster above, switching
// rarely (hysteresis). The ~3 s cushion absorbs the bursts; video-rtc still
// jumps ahead past 5 s, so the delay stays bounded. All hooks are per
// element — the WebRTC Camera files are never edited.
const CUBOAI_TRIM_KEEP_S = 8;          // never trim closer than this behind the playhead
const CUBOAI_RATE_SLOW = 0.8;
const CUBOAI_RATE_FAST = 1.25;
const CUBOAI_AHEAD_LOW = 2.0;          // less buffered than this: slow down...
const CUBOAI_AHEAD_LOW_EXIT = 2.6;     // ...until this much is buffered
const CUBOAI_AHEAD_HIGH = 4.2;         // more buffered than this: speed up...
const CUBOAI_AHEAD_HIGH_EXIT = 3.6;    // ...until back under this
const CUBOAI_RANGE_GAP_S = 0.3;        // buffered ranges this close play as one
const CUBOAI_MSE_TICK_MS = 250;
const CUBOAI_MSE_PARK_MS = 60000;      // stop the timer of a detached, paused player

// Seconds of media ahead of the playhead; null when nothing is buffered
// (WebRTC, or before the first fragment). A playhead outside every range has
// nothing ahead of it.
function cuboaiBufferedAhead(video) {
  const r = video && video.buffered;
  if (!r || !r.length) return null;
  const t = Number(video.currentTime);
  if (!Number.isFinite(t)) return null;
  let end = null;
  for (let i = 0; i < r.length; i++) {
    const s = r.start(i), e = r.end(i);
    if (end === null) {
      if (t >= s - CUBOAI_RANGE_GAP_S && t < e) end = e;
    } else if (s - end <= CUBOAI_RANGE_GAP_S) {
      end = Math.max(end, e);
    } else {
      break;
    }
  }
  return end === null ? 0 : Math.max(0, end - t);
}

// The next speed from the current one and the seconds buffered ahead. Only
// 1, SLOW or FAST ever come out, and each is held until its exit threshold.
function cuboaiNextRate(rate, ahead) {
  if (ahead === null || ahead === undefined) return 1;
  const b = Number(ahead);
  if (!Number.isFinite(b)) return 1;
  if (rate === CUBOAI_RATE_SLOW) return b > CUBOAI_AHEAD_LOW_EXIT ? 1 : CUBOAI_RATE_SLOW;
  if (rate === CUBOAI_RATE_FAST) return b < CUBOAI_AHEAD_HIGH_EXIT ? 1 : CUBOAI_RATE_FAST;
  if (b < CUBOAI_AHEAD_LOW) return CUBOAI_RATE_SLOW;
  if (b > CUBOAI_AHEAD_HIGH) return CUBOAI_RATE_FAST;
  return 1;
}

// Give one SourceBuffer a remove() that stops short of the playhead: the end
// is pulled back to CUBOAI_TRIM_KEEP_S behind it, and a trim that would free
// less than a second is skipped. Instance-level, so no other player changes.
function cuboaiGuardSourceBuffer(video, sb) {
  if (!sb || sb.__cuboTrimGuard) return;
  const remove = sb.remove;
  if (typeof remove !== 'function') return;
  Object.defineProperty(sb, 'remove', {
    configurable: true,
    writable: true,
    value(start, end) {
      const e = Math.min(Number(end), Number(video.currentTime) - CUBOAI_TRIM_KEEP_S);
      if (!(e - Number(start) >= 1)) return undefined;
      return remove.call(this, start, e);
    },
  });
  sb.__cuboTrimGuard = true;
}

// ManagedMediaSource (iPhone, Safari 17+) is reachable as video.srcObject;
// every reconnect makes a new one, so this runs on every tick. (Chrome's
// classic MediaSource hides behind a blob URL; Chrome also keeps playing
// through a trimmed range, so it doesn't need the guard.)
function cuboaiGuardMseTrim(video) {
  const list = video && video.srcObject && video.srcObject.sourceBuffers;
  if (!list) return;
  for (let i = 0; i < list.length; i++) cuboaiGuardSourceBuffer(video, list[i]);
}

// Take over this <video>'s playback speed and guard its MSE trims. video-rtc
// keeps writing playbackRate; an own accessor on the element shadows
// HTMLMediaElement's and ignores those writes, while a timer sets the speed
// from the real buffer.
function cuboaiSteerMsePlayback(video) {
  if (!video || video.__cuboMse) return;
  const proto = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'playbackRate');
  if (!proto || !proto.set || !proto.get) return;
  const st = { rate: 1, timer: 0, idleMs: 0 };
  video.__cuboMse = st;
  const apply = (rate) => { st.rate = rate; proto.set.call(video, rate); };
  const tick = () => {
    // With `background: true` a detached player keeps playing (the sound
    // must go on), so only a detached AND paused one parks its timer.
    if (!video.isConnected && video.paused) {
      st.idleMs += CUBOAI_MSE_TICK_MS;
      if (st.idleMs >= CUBOAI_MSE_PARK_MS) { clearInterval(st.timer); st.timer = 0; }
      return;
    }
    st.idleMs = 0;
    cuboaiGuardMseTrim(video);
    // WebRTC (a live MediaStream) has no buffer to steer: always normal speed.
    const live = typeof MediaStream !== 'undefined' && video.srcObject instanceof MediaStream;
    const next = live ? 1 : cuboaiNextRate(st.rate, cuboaiBufferedAhead(video));
    if (next !== st.rate || proto.get.call(video) !== next) apply(next);
  };
  st.start = () => { if (!st.timer) st.timer = setInterval(tick, CUBOAI_MSE_TICK_MS); };
  Object.defineProperty(video, 'playbackRate', {
    configurable: true,
    enumerable: true,
    get() { return proto.get.call(this); },
    // The value (video-rtc's "seconds buffered") is ignored; a write only
    // shows the player is live again, so a parked timer restarts.
    set() { if (this.isConnected || !this.paused) st.start(); },
  });
  apply(1);   // whatever speed was set before we hooked in: start from normal
  st.start();
}

class CuboAICameraCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    // HA may deliver `hass` (which triggers the one-time render) BEFORE
    // setConfig, or update the config after the first render. Since render()
    // only runs once, re-sync the form controls here — otherwise the dropdowns
    // show stale defaults (e.g. "Remember Last State" while the saved config is
    // actually "unmuted").
    if (this._rendered) this._applyConfigToForm();
  }

  _applyConfigToForm() {
    const c = this._config || {};
    const set = (sel, val) => {
      const el = this.querySelector(sel);
      if (el) el.value = val;
    };
    set('#camera-select', c.device_id || '');
    set('#mute-select', c.default_mute_state || 'remember');
    set('#song-filter-select', c.default_song_filter || 'all');
    set('#playlist-filter-select', c.default_playlist_filter || 'all');
    const check = (sel, on) => {
      const el = this.querySelector(sel);
      if (el) el.checked = on;
    };
    check('#show-env-toggle', c.show_env_overlay !== false);
    check('#show-mat-toggle', c.show_mat_overlay !== false);
    check('#show-music-toggle', c.show_music !== false);
    check('#show-timestamp-toggle', c.show_timestamp === true);
    check('#talk-mute-toggle', c.talk_mutes_speaker === true);
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._rendered) {
      this._rendered = true;
      this.render();
    }
  }

  render() {
    if (!this._hass) return;

    const cameras = [];
    for (const entity_id in this._hass.states) {
      if (entity_id.startsWith('media_player.') && entity_id.endsWith('_speaker')) {
        const state = this._hass.states[entity_id];
        if (state.attributes && state.attributes.device_id) {
          const name = state.attributes.friendly_name || entity_id;
          cameras.push({ entity_id, name: name.replace(' Speaker', ''), deviceId: state.attributes.device_id });
        }
      }
    }

    let optionsHtml = '<option value="">Auto-Detect (First Camera)</option>';
    optionsHtml += cameras.map(cam => 
      `<option value="${cam.deviceId}">${cam.name}</option>`
    ).join('');

    if (cameras.length === 0) {
      optionsHtml = `<option value="" disabled>No CuboAI cameras found</option>`;
    }

    this.innerHTML = `
      <div class="card-config">
        <label for="camera-select" style="display: block; font-weight: 500; margin-bottom: 8px;">Select Camera:</label>
        <select id="camera-select" style="width: 100%; padding: 8px; border-radius: 4px; border: 1px solid var(--divider-color, #ccc); background: var(--card-background-color, #fff); color: var(--primary-text-color, #000); margin-bottom: 16px;">
          ${optionsHtml}
        </select>
        
        <label for="mute-select" style="display: block; font-weight: 500; margin-bottom: 8px;">Initial Audio State:</label>
        <select id="mute-select" style="width: 100%; padding: 8px; border-radius: 4px; border: 1px solid var(--divider-color, #ccc); background: var(--card-background-color, #fff); color: var(--primary-text-color, #000); margin-bottom: 16px;">
          <option value="remember">Remember Last State (Default)</option>
          <option value="muted">Always Start Muted</option>
          <option value="unmuted">Always Start Unmuted</option>
        </select>

        <label for="song-filter-select" style="display: block; font-weight: 500; margin-bottom: 8px;">Default Song Filter:</label>
        <select id="song-filter-select" style="width: 100%; padding: 8px; border-radius: 4px; border: 1px solid var(--divider-color, #ccc); background: var(--card-background-color, #fff); color: var(--primary-text-color, #000); margin-bottom: 16px;">
          <option value="all">All Users</option>
          <option value="me">My Songs</option>
        </select>
        
        <label for="playlist-filter-select" style="display: block; font-weight: 500; margin-bottom: 8px;">Default Playlist Filter:</label>
        <select id="playlist-filter-select" style="width: 100%; padding: 8px; border-radius: 4px; border: 1px solid var(--divider-color, #ccc); background: var(--card-background-color, #fff); color: var(--primary-text-color, #000);">
          <option value="all">All Users</option>
          <option value="me">My Playlists</option>
        </select>

        <div style="margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--divider-color, #eee);">
          <label style="display: block; font-weight: 500; margin-bottom: 8px;">Card Sections:</label>
          <label style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px; cursor: pointer;">
            <input type="checkbox" id="show-env-toggle">
            <span>Temperature / humidity badge on the video</span>
          </label>
          <label style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px; cursor: pointer;">
            <input type="checkbox" id="show-mat-toggle">
            <span>Sleep-mat BPM badge on the video</span>
          </label>
          <label style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px; cursor: pointer;">
            <input type="checkbox" id="show-music-toggle">
            <span>Lullabies &amp; music section below the video</span>
          </label>
          <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
            <input type="checkbox" id="show-timestamp-toggle">
            <span>Timestamp badge (freezes and turns red when the stream stalls)</span>
          </label>
          <p style="color: var(--secondary-text-color); font-size: 12px; margin-top: 8px;">
            Badges auto-hide when their sensor has no data (mat / thermometer are optional accessories).
          </p>
        </div>

        <div style="margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--divider-color, #eee);">
          <label style="display: block; font-weight: 500; margin-bottom: 8px;">Microphone:</label>
          <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
            <input type="checkbox" id="talk-mute-toggle">
            <span>Mute the room's sound on this device while talking</span>
          </label>
          <p style="color: var(--secondary-text-color); font-size: 12px; margin-top: 8px;">
            Off: talking leaves the speaker as it is. On: the sound comes back when you stop.
          </p>
        </div>

        <div style="margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--divider-color, #eee);">
          <label style="display: block; font-weight: 500; margin-bottom: 8px;">Video Compatibility:</label>
          <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
            <input type="checkbox" id="h264-toggle">
            <span>Transcode this camera to H.264 (needed for HomeKit / HLS with H.265 cameras like Cubo 3)</span>
          </label>
          <p id="h264-help" style="color: var(--secondary-text-color); font-size: 12px; margin-top: 8px;">
            Only enable for H.265/HEVC cameras that fail in HomeKit or the HA stream view. Uses extra CPU; leave off for native-H.264 cameras (Cubo 2). Applies to the whole integration, not just this card.
          </p>
        </div>

        <div style="margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--divider-color, #eee);">
          <label style="display: block; font-weight: 500; margin-bottom: 8px;">Song Cache:</label>
          <label style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px; cursor: pointer;">
            <input type="checkbox" id="cache-toggle">
            <span>Save YouTube/Spotify songs to local cache</span>
          </label>
          <div style="display: flex; align-items: center; gap: 8px;">
            <button id="clear-cache-btn" style="padding: 8px 14px; border-radius: 4px; border: none; background: var(--error-color, #f44336); color: white; font-weight: 500; cursor: pointer;">Clear Song Cache</button>
            <span id="clear-cache-status" style="font-size: 12px; color: var(--secondary-text-color);"></span>
          </div>
          <p style="color: var(--secondary-text-color); font-size: 12px; margin-top: 8px;">
            These are global settings — they apply to all cameras and all CuboAI cards.
          </p>
        </div>

        <p style="color: var(--secondary-text-color); font-size: 12px; margin-top: 12px;">
          Note: By default, the card will automatically find and display the first camera on your account.
          Use the dropdown above if you have multiple cameras and want to pin this card to a specific one.
        </p>
      </div>
    `;
    
    const selectEl = this.querySelector('#camera-select');
    if (selectEl) {
      selectEl.value = this._config ? (this._config.device_id || '') : '';
      selectEl.addEventListener('change', (e) => this._valueChanged(e));
    }
    
    const muteSelect = this.querySelector('#mute-select');
    if (muteSelect) {
      muteSelect.value = this._config ? (this._config.default_mute_state || 'remember') : 'remember';
      muteSelect.addEventListener('change', (e) => this._valueChanged(e));
    }
    
    const songFilter = this.querySelector('#song-filter-select');
    if (songFilter) {
      songFilter.value = this._config ? (this._config.default_song_filter || 'all') : 'all';
      songFilter.addEventListener('change', (e) => this._valueChanged(e));
    }
    
    const plFilter = this.querySelector('#playlist-filter-select');
    if (plFilter) {
      plFilter.value = this._config ? (this._config.default_playlist_filter || 'all') : 'all';
      plFilter.addEventListener('change', (e) => this._valueChanged(e));
    }

    // Section toggles (issues #99/#100). Defaults: everything on except the
    // timestamp badge, which is opt-in.
    for (const [id, key, dflt] of [
      ['#show-env-toggle', 'show_env_overlay', true],
      ['#show-mat-toggle', 'show_mat_overlay', true],
      ['#show-music-toggle', 'show_music', true],
      ['#show-timestamp-toggle', 'show_timestamp', false],
      ['#talk-mute-toggle', 'talk_mutes_speaker', false],
    ]) {
      const el = this.querySelector(id);
      if (el) {
        const v = this._config ? this._config[key] : undefined;
        el.checked = v === undefined ? dflt : v !== false && v !== 'false';
        el.addEventListener('change', (e) => this._valueChanged(e));
      }
    }

    // Global song-cache controls (drive the shared switch entity / service,
    // not the per-card config)
    const findCacheSwitch = () => {
      if (!this._hass) return null;
      return Object.keys(this._hass.states).find(
        id => id.startsWith('switch.') && id.includes('cache_youtube')
      ) || null;
    };

    const cacheToggle = this.querySelector('#cache-toggle');
    if (cacheToggle) {
      const cacheEnt = findCacheSwitch();
      if (cacheEnt) {
        cacheToggle.checked = this._hass.states[cacheEnt].state === 'on';
        cacheToggle.addEventListener('change', () => {
          this._hass.callService('switch', cacheToggle.checked ? 'turn_on' : 'turn_off', { entity_id: cacheEnt });
        });
      } else {
        cacheToggle.disabled = true;
      }
    }

    // Per-camera H.264 transcode. This is an INTEGRATION option (it fixes
    // HomeKit/HLS, which use the camera entity, not the card) — the toggle
    // reflects the camera entity's h264_transcode attribute and writes it via
    // the cuboai.set_h264_transcode service, which reloads the integration.
    // Was a `camera.cuboai_*` entity-id filter that never matched, so camEnt was
    // always null and the H.264 toggle below was permanently disabled (#89).
    const findCameraEntity = () => {
      const found = cuboaiFindCameraState(
        this._hass, this._config && this._config.device_id);
      return found ? found.state : null;
    };
    const h264Toggle = this.querySelector('#h264-toggle');
    if (h264Toggle) {
      const camEnt = findCameraEntity();
      const camDevId = camEnt && camEnt.attributes && camEnt.attributes.device_id;
      if (camDevId) {
        h264Toggle.checked = !!camEnt.attributes.h264_transcode;
        h264Toggle.addEventListener('change', () => {
          this._hass.callService('cuboai', 'set_h264_transcode', {
            device_id: camDevId,
            enabled: h264Toggle.checked,
          });
          const help = this.querySelector('#h264-help');
          if (help) help.textContent = 'Saved — reloading the integration to apply the stream change…';
        });
      } else {
        h264Toggle.disabled = true;
      }
    }

    const clearCacheBtn = this.querySelector('#clear-cache-btn');
    const clearCacheStatus = this.querySelector('#clear-cache-status');
    if (clearCacheBtn) {
      clearCacheBtn.addEventListener('click', () => {
        if (this._hass && window.confirm('Delete all locally cached YouTube/Spotify songs?')) {
          this._hass.callService('cuboai', 'clear_youtube_cache', {});
          if (clearCacheStatus) {
            clearCacheStatus.textContent = 'Cache cleared ✓';
            setTimeout(() => { clearCacheStatus.textContent = ''; }, 4000);
          }
        }
      });
    }
  }

  _valueChanged(ev) {
    if (!this._config || !this._hass) return;
    const target = ev.target;
    
    let newConfig = { ...this._config };
    if (target.id === "camera-select") {
      newConfig.device_id = target.value;
    } else if (target.id === "mute-select") {
      newConfig.default_mute_state = target.value;
    } else if (target.id === "song-filter-select") {
      newConfig.default_song_filter = target.value;
    } else if (target.id === "playlist-filter-select") {
      newConfig.default_playlist_filter = target.value;
    } else if (target.id === "show-env-toggle") {
      if (target.checked) delete newConfig.show_env_overlay; else newConfig.show_env_overlay = false;
    } else if (target.id === "show-mat-toggle") {
      if (target.checked) delete newConfig.show_mat_overlay; else newConfig.show_mat_overlay = false;
    } else if (target.id === "show-music-toggle") {
      if (target.checked) delete newConfig.show_music; else newConfig.show_music = false;
    } else if (target.id === "show-timestamp-toggle") {
      if (target.checked) newConfig.show_timestamp = true; else delete newConfig.show_timestamp;
    } else if (target.id === "talk-mute-toggle") {
      if (target.checked) newConfig.talk_mutes_speaker = true; else delete newConfig.talk_mutes_speaker;
    }

    // Use CustomEvent so `detail` is delivered reliably (a plain Event with a
    // manually-attached detail is ignored by newer HA editors, leaving Save
    // greyed out because HA never sees the config change).
    this.dispatchEvent(new CustomEvent("config-changed", {
      detail: { config: newConfig },
      bubbles: true,
      composed: true,
    }));
  }
}

if (!customElements.get('cuboai-camera-card-editor')) {
  customElements.define("cuboai-camera-card-editor", CuboAICameraCardEditor);
}


class CuboAICameraCard extends HTMLElement {
  disconnectedCallback() {
    // The DVR timeline keeps a repaint interval and a ResizeObserver alive;
    // drop both when the card leaves the DOM so a dashboard that is opened and
    // closed repeatedly does not accumulate timers.
    if (this._dvrTick) { clearInterval(this._dvrTick); this._dvrTick = null; }
    if (this._dvrWait) { clearInterval(this._dvrWait); this._dvrWait = null; }
    if (this._dvrNudge) { clearTimeout(this._dvrNudge); this._dvrNudge = null; }
    if (this._dvrResize) { this._dvrResize.disconnect(); this._dvrResize = null; }
    if (this._tsClock) { clearInterval(this._tsClock); this._tsClock = null; }
    // Never leave the microphone open on a card nobody is looking at. Checked a
    // moment later, not now: a dashboard that re-flows its columns (masonry, on
    // rotating the phone) takes the card out and puts it straight back, which
    // must not end a talk. A card really gone is still stopped, one task later.
    if (this._mic) setTimeout(() => { if (!this.isConnected && this._mic) this._cuboStopMic('card_removed'); }, 0);
    if (super.disconnectedCallback) super.disconnectedCallback();
  }

  
  static getConfigElement() {
    return document.createElement("cuboai-camera-card-editor");
  }

  static getStubConfig() {
    return { type: "custom:cuboai-camera-card", device_id: "" };
  }

  // A short message over the video (why the mic cannot start, that it is
  // live, why it stopped). ms = 0 keeps it until the next one.
  _cuboNotice(text, ms = 5000) {
    const host = (this.micButton && this.micButton.parentNode) || this;
    if (!this._noticeEl) {
      this._noticeEl = document.createElement('div');
      this._noticeEl.style.cssText = 'position: absolute !important; top: 64px !important; left: 16px !important; right: 16px !important; z-index: 2147483647 !important; background: rgba(0,0,0,0.75) !important; color: #fff !important; padding: 8px 12px !important; border-radius: 8px !important; font-size: 13px !important; line-height: 1.4 !important; pointer-events: none !important;';
    }
    if (this._noticeEl.parentNode !== host) host.appendChild(this._noticeEl);
    this._noticeEl.textContent = text;
    this._noticeEl.style.display = 'block';
    clearTimeout(this._noticeTimer);
    this._noticeTimer = ms > 0 ? setTimeout(() => this._cuboNoticeHide(), ms) : null;
  }

  _cuboNoticeHide() {
    clearTimeout(this._noticeTimer);
    this._noticeTimer = null;
    if (this._noticeEl) this._noticeEl.style.display = 'none';
  }

  // ── Two-way audio ────────────────────────────────────────────────────
  // Tap to talk, tap again to stop. The microphone is captured here as 16 kHz
  // 16-bit mono and streamed over Home Assistant's own websocket to the
  // `cuboai/talk` command, which runs the camera talk in a child process (see
  // the helpers at the top). The video player is never reconfigured for it.
  // The speaker is never touched: talking leaves the room sound exactly as the
  // user and the card's audio setting have it (the phone's echo cancellation
  // keeps its own speaker out of the mic).

  _cuboMicSet(ev) {
    this._micState = cuboaiMicNextState(this._micState || 'idle', ev);
    this._cuboMicPaint();
  }

  _cuboMicPaint() {
    const b = this.micButton;
    if (!b) return;
    const st = this._micState || 'idle';
    const [icon, bg, label] = {
      idle: ['mdi:microphone-off', 'rgba(0, 0, 0, 0.5)', 'Talk'],
      connecting: ['mdi:microphone', 'rgba(255, 160, 0, 0.9)', 'Connecting… tap to cancel'],
      live: ['mdi:microphone', 'rgba(220, 53, 69, 0.9)', 'Live: tap to stop'],
      stopping: ['mdi:microphone-off', 'rgba(0, 0, 0, 0.35)', 'Stopping…'],
    }[st];
    b.innerHTML = `<ha-icon icon="${icon}"></ha-icon>`;
    b.style.backgroundColor = bg;
    b.setAttribute('title', label);
    b.setAttribute('aria-label', label);
    // Disabled while stopping: that also spaces out the camera handshakes,
    // which the camera rate-limits.
    b.disabled = st === 'stopping';
    b.style.pointerEvents = st === 'stopping' ? 'none' : '';
    b.style.opacity = st === 'stopping' ? '0.6' : '';
    // Animated through the element itself: the button lives inside the
    // player's shadow root, where the page's @keyframes do not reach.
    if (this._micAnim) { try { this._micAnim.cancel(); } catch (e) { /* gone */ } this._micAnim = null; }
    if (typeof b.animate === 'function') {
      if (st === 'connecting') {
        this._micAnim = b.animate([{ opacity: 1 }, { opacity: 0.45 }, { opacity: 1 }],
          { duration: 1000, iterations: Infinity });
      } else if (st === 'live') {
        this._micAnim = b.animate([
          { boxShadow: '0 0 0 0 rgba(220, 53, 69, 0.75)' },
          { boxShadow: '0 0 0 14px rgba(220, 53, 69, 0)' },
        ], { duration: 1200, iterations: Infinity });
      }
    }
  }

  // The camera the talk is for: the pinned device, else the camera the live
  // picture was built for (_liveDeviceId). Never re-resolved per tap: an
  // unpinned card finds its speaker as the FIRST match in hass.states, an order
  // that changes when an entity is re-added (an entry reload), and the voice
  // must go to the nursery on screen, not to whichever camera sorts first.
  _cuboMicDeviceId() {
    const pinned = (this._config || {}).device_id;
    if (pinned) return pinned;
    return this._liveDeviceId || null;
  }

  // Read fresh on every send and never kept: the frontend replaces the socket
  // on every reconnect, and a handler id belongs to one connection only.
  _cuboMicSocket() {
    const conn = this._hass && this._hass.connection;
    return conn ? conn.socket : null;
  }

  _cuboMicLabel(mic, text) {
    return mic.dryRun ? `TEST MODE — nothing plays at the camera. ${text}` : text;
  }

  _cuboMicSay(mic, what) {
    const text = cuboaiMicErrorText(what);
    if (!text) return;
    mic.noticed = true;
    this._cuboNotice(text, 8000);
  }

  _cuboMedia() {
    const root = this.content ? (this.content.shadowRoot || this.content) : null;
    if (!root || typeof root.querySelector !== 'function') return {};
    return { video: root.querySelector('video'), audio: root.querySelector('audio'), icon: root.querySelector('.volume') };
  }

  async _cuboStartMic() {
    if ((this._micState || 'idle') !== 'idle' || this._mic) return;
    if (!cuboaiMicCanCapture(window)) {
      const where = (window.location && window.location.origin) || '';
      this._cuboNotice(cuboaiMicErrorText(window.isSecureContext ? 'no_capture' : 'insecure', where), 12000);
      return;
    }
    const deviceId = this._cuboMicDeviceId();
    const conn = this._hass && this._hass.connection;
    if (!deviceId || !conn || typeof conn.subscribeMessage !== 'function') {
      this._cuboNotice(cuboaiMicErrorText('no_camera'), 6000);
      return;
    }
    const dryRun = cuboaiMicDryRunAsked(window.__cuboaiMicDryRun) ||
      cuboaiMicDryRunAsked((this._config || {}).talk_dry_run);
    const mic = {
      deviceId, dryRun, conn, handlerId: null, timers: [], sent: 0, dropped: 0,
      closed: false, chunk: cuboaiMicChunker(CUBOAI_MIC_CHUNK),
    };
    mic.endedP = new Promise((resolve) => { mic.endedResolve = resolve; });
    this._mic = mic;
    this._cuboMicSet('tap');
    this._cuboNotice(this._cuboMicLabel(mic, 'Connecting to the camera…'), 0);
    // iOS starts audio only inside the tap itself: create and resume the
    // context synchronously, BEFORE anything is awaited. No sampleRate option:
    // Firefox refuses a microphone on a context of another rate, and a
    // Bluetooth headset on iOS changes the hardware rate.
    try {
      mic.ctx = new (window.AudioContext || window.webkitAudioContext)();
      Promise.resolve(mic.ctx.resume()).catch(() => {});
    } catch (e) {
      return this._cuboMicFail(mic, e);
    }
    try {
      mic.stream = await this._cuboMicGetStream();
      if (mic.closed) return this._cuboMicTeardown(mic);    // cancelled at the permission prompt
      await this._cuboMicCapture(mic);
      if (mic.closed) return this._cuboMicTeardown(mic);
    } catch (e) {
      return this._cuboMicFail(mic, e);
    }
    this._cuboMicArm(mic);
    if (mic.closed) return;                               // ended by the state the arming found
    this._cuboTalkMuteOn(mic);
    // Only now is the talk opened: a refused or missing microphone never
    // makes the camera's speaker click. `resubscribe: false` -- after a
    // reconnect the frontend would otherwise quietly re-open a talk that
    // nobody is holding.
    const msg = { type: 'cuboai/talk', device_id: deviceId, sample_rate: CUBOAI_MIC_RATE, dry_run: dryRun };
    mic.timers.push(setTimeout(() => {
      if (this._micState === 'connecting') this._cuboMicEnd(mic, 'camera_timeout');
    }, CUBOAI_MIC_NO_LIVE_MS));
    mic.timers.push(setTimeout(() => this._cuboMicEnd(mic, 'max_duration'), CUBOAI_MIC_BACKUP_MS));
    let timer = null;
    try {
      const pending = conn.subscribeMessage((ev) => this._cuboMicEvent(mic, ev), msg, { resubscribe: false });
      // Registered first, so it runs before the race below resumes. A talk
      // that is accepted after a stop or a timeout is released at once.
      pending.then((unsub) => {
        mic.unsub = unsub;
        if (mic.closed) this._cuboMicRelease(mic);
      }, () => {});
      await Promise.race([pending, new Promise((resolve, reject) => {
        timer = setTimeout(() => reject({ code: 'timeout' }), CUBOAI_MIC_SUBSCRIBE_MS);
      })]);
    } catch (e) {
      return this._cuboMicFail(mic, e);
    } finally {
      clearTimeout(timer);
    }
  }

  async _cuboMicGetStream() {
    const md = navigator.mediaDevices;
    try {
      return await md.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (e) {
      // Constraints some devices cannot meet: take whatever microphone there is.
      if (e && (e.name === 'OverconstrainedError' || e.name === 'TypeError')) return md.getUserMedia({ audio: true });
      throw e;
    }
  }

  // source -> 2x low-pass 7.2 kHz (only above 17.6 kHz) -> tap -> Gain(0) ->
  // destination. The silent gain keeps the graph pulled without playing the
  // microphone back. The tap is an AudioWorklet from a Blob URL, or a
  // ScriptProcessor where worklets are missing or refuse to load.
  async _cuboMicCapture(mic) {
    const ctx = mic.ctx;
    const src = ctx.createMediaStreamSource(mic.stream);
    mic.nodes = [src];
    let head = src;
    if (ctx.sampleRate > CUBOAI_MIC_LOWPASS_ABOVE) {
      for (let i = 0; i < 2; i++) {
        const lp = ctx.createBiquadFilter();
        lp.type = 'lowpass';
        lp.frequency.value = 7200;
        lp.Q.value = 0.707;
        head.connect(lp);
        head = lp;
        mic.nodes.push(lp);
      }
    }
    const sink = ctx.createGain();
    sink.gain.value = 0;
    sink.connect(ctx.destination);
    mic.nodes.push(sink);
    const resample = cuboaiMicResampler(ctx.sampleRate, CUBOAI_MIC_RATE);   // the REAL rate
    const onPcm = (f32) => this._cuboMicSend(mic, resample(f32));
    let tap = null;
    if (ctx.audioWorklet && typeof AudioWorkletNode !== 'undefined') {
      const url = URL.createObjectURL(new Blob([CUBOAI_MIC_WORKLET_SRC], { type: 'application/javascript' }));
      try {
        await ctx.audioWorklet.addModule(url);
        tap = new AudioWorkletNode(ctx, 'cuboai-mic-tap', {
          numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
          channelCount: 1, channelCountMode: 'explicit',
        });
        tap.port.onmessage = (ev) => onPcm(ev.data);
        mic.path = 'worklet';
      } catch (e) {
        tap = null;
        console.warn('CuboAI mic: AudioWorklet unavailable, using ScriptProcessor', e);
      } finally {
        URL.revokeObjectURL(url);
      }
    }
    if (!tap) {
      tap = ctx.createScriptProcessor(2048, 1, 1);
      tap.onaudioprocess = (ev) => {
        onPcm(ev.inputBuffer.getChannelData(0));
        ev.outputBuffer.getChannelData(0).fill(0);
      };
      mic.path = 'script';
    }
    mic.tap = tap;
    mic.nodes.push(tap);
    if (mic.closed) return;
    head.connect(tap);
    tap.connect(sink);
  }

  // Every way a talk can end without a tap on the button.
  _cuboMicArm(mic) {
    const end = (reason) => () => this._cuboMicEnd(mic, reason);
    const track = mic.stream.getAudioTracks ? mic.stream.getAudioTracks()[0] : null;
    if (track) track.onended = end('track_ended');     // the OS or another app took the mic
    mic.onVis = () => { if (document.visibilityState === 'hidden') this._cuboMicEnd(mic, 'hidden'); };
    document.addEventListener('visibilitychange', mic.onVis);
    // Handler ids die with the socket; there is no automatic restart after a
    // reconnect -- a talk nobody asked for again must not resume. Armed until
    // the talk is fully released: a drop while _cuboStopMic waits for `ended`
    // ends that wait too (`ended` can no longer arrive), and nothing is
    // unsubscribed after it (_cuboMicRelease).
    mic.onDisc = () => {
      mic.connLost = true;
      mic.endedResolve();
      this._cuboMicEnd(mic, 'disconnected');
    };
    try { mic.conn.addEventListener('disconnected', mic.onDisc); } catch (e) { /* old frontend */ }
    const onState = () => {
      const s = mic.ctx.state;
      if (s === 'interrupted' || s === 'closed') this._cuboMicEnd(mic, 'audio_interrupted');
      else if (s === 'suspended') Promise.resolve(mic.ctx.resume()).catch(() => {});
    };
    mic.ctx.onstatechange = onState;
    // What changed while the microphone and the worklet were loading has
    // already fired (or never will): look now, before the talk is opened. A
    // phone locked during the permission prompt must not start a talk that
    // only the server's idle timer would end.
    if (document.visibilityState === 'hidden') this._cuboMicEnd(mic, 'hidden');
    else if (track && track.readyState === 'ended') this._cuboMicEnd(mic, 'track_ended');
    else onState();
  }

  _cuboMicDisarm(mic) {
    mic.timers.forEach((t) => clearTimeout(t));
    mic.timers = [];
    if (mic.onVis) document.removeEventListener('visibilitychange', mic.onVis);
    if (mic.onDisc) { try { mic.conn.removeEventListener('disconnected', mic.onDisc); } catch (e) { /* old frontend */ } }
    mic.onVis = mic.onDisc = null;
  }

  // Release the microphone completely: tracks stopped and the context CLOSED.
  // HA's own recorder only suspends and disables the track, which keeps the
  // iOS play-and-record audio session (and the orange dot) alive. Safe to
  // call again, e.g. for a stream that arrived after a cancel.
  // Option `talk_mutes_speaker` (off by default): mute the room's sound on
  // this device while talking, so the phone's speaker is not heard back
  // through the camera. Only the video element is touched; the player's own
  // speaker icon follows it (volumechange), so icon and sound never disagree.
  // Nothing is saved: not in this browser, not in the shared setting. Setting
  // `isMuted` makes the card's own unmute paths (the watchdog, a recording
  // swap, a rebuilt video) stand down for the talk.
  _cuboTalkMuteOn(mic) {
    if (!this._config || this._config.talk_mutes_speaker !== true || mic.talkMute) return;
    const { video, audio } = this._cuboMedia();
    mic.talkMute = { wasMuted: video ? video.muted : this.isMuted, wasIsMuted: this.isMuted, touched: false };
    this.isMuted = true;
    if (video) video.muted = true;
    if (audio) audio.muted = true;
  }

  // Back to what the speaker was before the talk -- unless the user tapped the
  // speaker during it: that choice stands. The unmute runs inside the stop tap
  // when there is one (iOS unmutes a playing video only in a gesture); when the
  // talk ended by itself and the browser pauses the video for it, the picture
  // goes on muted and the next tap brings the sound back.
  _cuboTalkMuteOff(mic) {
    const tm = mic && mic.talkMute;
    if (!tm) return;
    mic.talkMute = null;
    const { video, audio } = this._cuboMedia();
    if (tm.touched) {
      if (video) this.isMuted = video.muted;
      return;
    }
    this.isMuted = tm.wasIsMuted;
    if (tm.wasMuted || !video) return;
    const inTap = !!(navigator.userActivation && navigator.userActivation.isActive);
    video.muted = false;
    if (audio) audio.muted = false;
    if (video.paused) Promise.resolve(video.play()).catch(() => {});
    if (inTap) return;
    setTimeout(() => {
      if (!video.paused || (this._mic && this._mic.talkMute)) return;
      this._soundNeedsGesture = true;
      this.isMuted = true;
      video.muted = true;
      if (audio) audio.muted = true;
      Promise.resolve(video.play()).catch(() => {});
      if (this._armGestureUnmute) this._armGestureUnmute();
      // Added to a notice that is up (why the talk ended), never in its place.
      const hint = 'Tap 🔈 to hear the room.';
      const up = this._noticeEl && this._noticeEl.style.display !== 'none' ? this._noticeEl.textContent : '';
      this._cuboNotice(up ? `${up} ${hint}` : hint, up ? 8000 : 6000);
    }, 250);
  }

  _cuboMicTeardown(mic) {
    this._cuboTalkMuteOff(mic);
    const tap = mic.tap;
    if (tap) {
      try { if (tap.port) { tap.port.onmessage = null; tap.port.postMessage('stop'); } } catch (e) { /* closed */ }
      tap.onaudioprocess = null;
    }
    (mic.nodes || []).forEach((n) => { try { n.disconnect(); } catch (e) { /* never connected */ } });
    mic.nodes = [];
    if (mic.stream) {
      try { mic.stream.getTracks().forEach((t) => { t.onended = null; t.stop(); }); } catch (e) { /* gone */ }
    }
    if (mic.ctx) {
      mic.ctx.onstatechange = null;
      try { if (mic.ctx.state !== 'closed') Promise.resolve(mic.ctx.close()).catch(() => {}); } catch (e) { /* gone */ }
    }
  }

  _cuboMicSend(mic, pcm) {
    const st = this._micState;
    if (mic.closed || this._mic !== mic || mic.handlerId == null || (st !== 'connecting' && st !== 'live')) return;
    for (const chunk of mic.chunk(pcm)) {
      const sock = this._cuboMicSocket();
      if (cuboaiMicSendGate(sock) !== 'ok') { mic.dropped += 1; continue; }
      try {
        sock.send(cuboaiMicFrame(mic.handlerId, chunk));
        mic.sent += 1;
      } catch (e) {
        mic.dropped += 1;
      }
    }
  }

  _cuboMicEvent(mic, ev) {
    if (!ev) return;
    if (ev.type === 'ended') {
      // Also while stopping: _cuboStopMic is waiting for exactly this.
      mic.ended = ev;
      mic.endedResolve();
      // Already stopping: whatever was said about it (an `error`) stands.
      if (mic.closed) return;
      if (ev.reason !== 'client_end') this._cuboMicSay(mic, ev.reason || 'internal');
      this._cuboStopMic(ev.reason || 'ended', mic);
      return;
    }
    if (mic.closed || this._mic !== mic) return;
    if (ev.type === 'started') {
      if (mic.dryRun && ev.dry_run !== true) {
        // Asked for a test and got a real talk: send it nothing, end it now.
        this._cuboMicSay(mic, 'dry_run_refused');
        this._cuboStopMic('dry_run_refused', mic);
        return;
      }
      const id = Number(ev.handler_id);
      if (!Number.isInteger(id) || id < 1 || id > 255) {
        this._cuboMicSay(mic, 'internal');
        this._cuboStopMic('internal', mic);
        return;
      }
      mic.handlerId = id;
      // End our side a second before the server's own cap, so no frame is
      // ever sent to a handler it has already dropped.
      const cap = Number(ev.max_secs);
      if (cap > 0) {
        mic.timers.push(setTimeout(() => this._cuboMicEnd(mic, 'max_duration'), Math.max(1, cap - 1) * 1000));
      }
    } else if (ev.type === 'live') {
      this._cuboMicSet('live');
      const how = mic.dryRun ? ` (${mic.path}, ${mic.ctx.sampleRate} Hz)` : '';
      this._cuboNotice(this._cuboMicLabel(mic, `Live — speak now. Tap to stop${how}`), mic.dryRun ? 0 : 5000);
    } else if (ev.type === 'status') {
      // Debug only. In test mode it shows the audio really flows.
      mic.status = ev;
      if (mic.dryRun && this._micState === 'live') {
        this._cuboNotice(this._cuboMicLabel(mic, `Live (${mic.path}, ${mic.ctx.sampleRate} Hz): ` +
          `${mic.sent} sent, ${mic.dropped} dropped here; camera frames ${ev.speech}.`), 0);
      }
    } else if (ev.type === 'error') {
      // The server ends this talk itself; `ended` follows.
      mic.serverError = ev.code || 'internal';
      this._cuboMicSay(mic, ev.code ? ev : 'internal');
      this._cuboStopMic(mic.serverError, mic);
    }
  }

  // A stop that did not come from the button: say why, then stop.
  _cuboMicEnd(mic, reason) {
    if (mic.closed || this._mic !== mic) return;
    this._cuboMicSay(mic, reason);
    this._cuboStopMic(reason, mic);
  }

  _cuboMicFail(mic, e) {
    if (!mic.closed) this._cuboMicSay(mic, e);
    this._cuboMicTeardown(mic);
    return this._cuboStopMic('error', mic);
  }

  // The unsubscribe, once -- and never after the connection dropped. Home
  // Assistant ended the talk when the socket closed, and the frontend has
  // already forgotten this command: after its reconnect the command ids start
  // again, so the old id may now be another card's subscription, which this
  // unsubscribe would cancel.
  _cuboMicRelease(mic) {
    if (!mic.unsub || mic.released || mic.connLost) return Promise.resolve();
    mic.released = true;
    return Promise.resolve().then(() => mic.unsub()).catch(() => { /* already gone server-side */ });
  }

  // Idempotent. Capture first and completely, then the talk: a 1-byte end
  // message, up to 4 s for the server's `ended`, then unsubscribe. After a
  // dropped connection there is nobody to tell: Home Assistant ends the talk
  // itself when the socket closes.
  async _cuboStopMic(reason = 'client_end', mic = this._mic) {
    if (!mic || mic.closed) return;
    mic.closed = true;
    if (this._mic === mic) this._cuboMicSet('stop');
    if (!mic.noticed) this._cuboNoticeHide();
    // The stop paths stay armed until the end (every one is a no-op once
    // `closed`): a dropped connection must still cut the wait below short.
    this._cuboMicTeardown(mic);
    let endSent = false;
    if (reason !== 'disconnected' && mic.handlerId != null && !mic.ended && !mic.serverError) {
      const sock = this._cuboMicSocket();
      if (cuboaiMicSendGate(sock) !== 'closed') {
        try { sock.send(cuboaiMicEndFrame(mic.handlerId)); endSent = true; } catch (e) { /* socket closing */ }
      }
    }
    mic.handlerId = null;
    if (reason !== 'disconnected' && !mic.ended && (endSent || mic.serverError)) {
      let wait = null;
      await Promise.race([mic.endedP, new Promise((resolve) => { wait = setTimeout(resolve, CUBOAI_MIC_ENDED_WAIT_MS); })]);
      clearTimeout(wait);
    }
    await this._cuboMicRelease(mic);
    this._cuboMicDisarm(mic);
    if (this._mic === mic) {
      this._mic = null;
      this._cuboMicSet('stopped');
    }
  }

  // ── Shared per-camera settings (synced across all devices via the media
  //    library sensor + cuboai.save_settings service) ─────────────────────
  _findLibrarySensor() {
    if (!this._hass || !this._hass.states) return null;
    for (const k in this._hass.states) {
      if (k.startsWith('sensor.cuboai_media_library')) return this._hass.states[k];
    }
    return null;
  }
  _getSharedSetting(deviceId, key) {
    const lib = this._findLibrarySensor();
    const s = lib && lib.attributes && lib.attributes.settings && lib.attributes.settings[deviceId];
    return s ? s[key] : undefined;
  }
  _setSharedSetting(deviceId, patch) {
    try {
      const lib = this._findLibrarySensor();
      const all = (lib && lib.attributes && lib.attributes.settings)
        ? JSON.parse(JSON.stringify(lib.attributes.settings)) : {};
      all[deviceId] = Object.assign({}, all[deviceId] || {}, patch);
      this._settingsWriteTs = Date.now();
      if (this._hass) this._hass.callService('cuboai', 'save_settings', { settings: all });
    } catch (e) {}
  }

  set hass(hass) {
    if (this._error) {
      this.innerHTML = `<div style="background: #fee; border: 1px solid #fcc; color: #c00; padding: 15px; border-radius: 8px;"><h3>CuboAI Card Configuration Error</h3><p>${this._error.message}</p><pre>${this._error.stack}</pre></div>`;
      return;
    }
    try {
      if (!hass || !hass.states) return;
      this._hass = hass;
      
      let deviceId = this._config?.device_id;
      this._speakerEntityId = null;
      this._lullabyEntityId = null;
      
      for (const entity_id in hass.states) {
        if (entity_id.startsWith('media_player.') && entity_id.endsWith('_speaker')) {
          const state = hass.states[entity_id];
          if (state.attributes && state.attributes.device_id) {
            if (!deviceId || deviceId === state.attributes.device_id) {
              deviceId = state.attributes.device_id;
              this._speakerEntityId = entity_id;
              break;
            }
          }
        }
      }

      if (this._speakerEntityId) {
        const candidate = this._speakerEntityId.replace('_speaker', '_lullaby');
        if (hass.states[candidate]) {
          this._lullabyEntityId = candidate;
        } else {
          // Fallback search
          for (const entity_id in hass.states) {
            if (entity_id.startsWith('media_player.') && entity_id.endsWith('_lullaby')) {
              this._lullabyEntityId = entity_id;
              break;
            }
          }
        }
      }

      if (!this.content) {
        this.style.display = 'block';
        this.style.position = 'relative';
        if (!deviceId || !this._speakerEntityId) return;

      const savedMuted = localStorage.getItem(`cuboai_muted_${deviceId}`);
      const defaultMuteState = this._config?.default_mute_state || 'remember';
      
      // Decide whether the user ultimately wants SOUND (unmuted) or silence.
      const wantUnmuted = cuboaiWantUnmuted(defaultMuteState, this._getSharedSetting(deviceId, 'muted'), savedMuted);
      // The state the player starts in (cuboaiStartMuted): muted everywhere
      // but Apple, where the setting itself is the starting state.
      this.isMuted = cuboaiStartMuted(wantUnmuted, navigator.vendor);
      this._wantUnmuted = wantUnmuted;

      // Resolve the camera by attribute (#89). Bail out visibly rather than
      // streaming from a fabricated URL: if no CuboAI camera entity exists the
      // camera platform did not load, so nothing is listening on any port.
      // `set hass` re-runs on every state update, so this self-heals as soon as
      // the entity appears (HA startup race).
      const found = cuboaiFindCameraState(hass, deviceId);
      if (!found) {
        if (!this._noCamEl) {
          this._noCamEl = document.createElement('div');
          this._noCamEl.style.cssText = 'background: #fee; border: 1px solid #fcc; color: #c00; padding: 15px; border-radius: 8px;';
          this.appendChild(this._noCamEl);
        }
        this._noCamEl.innerHTML =
          `<h3>CuboAI camera unavailable</h3>` +
          `<p>No camera entity was found${deviceId ? ` for device <code>${deviceId}</code>` : ''}.</p>` +
          `<p>Check Settings &rarr; Devices &amp; Services &rarr; CuboAI (the camera platform may have ` +
          `failed to load), and the Home Assistant log for <code>CuboAI go2rtc is not running</code>.</p>`;
        return;
      }
      if (this._noCamEl) {
        this._noCamEl.remove();
        this._noCamEl = null;
      }

      // The poster (camera's last snapshot, shown while (re)connecting instead
      // of a black frame) comes from the entity's entity_picture — an
      // access-token URL served by HA, so it works from any device.
      const webrtcConfig = cuboaiWebrtcConfig(found, this.isMuted);

      // Two-way audio button: tap to talk, tap again to stop (_cuboStartMic).
      if (!this.micButton) {
        this.micButton = document.createElement('ha-icon-button');
        // Visible. It was created `display: none !important` and nothing ever
        // revealed it, so no install had a mic button. box-shadow is not
        // !important: the live state animates it (_cuboMicPaint).
        this.micButton.style.cssText = 'position: absolute !important; top: 16px !important; left: 16px !important; z-index: 2147483647 !important; border-radius: 50% !important; color: white !important; box-shadow: 0 4px 6px rgba(0,0,0,0.3); transition: all 0.2s !important; display: inline-flex !important; align-items: center; justify-content: center; cursor: pointer;';
        // Never touches the video player: no reconfigure, no reconnect.
        this.micButton.addEventListener('click', () => {
          const st = this._micState || 'idle';
          if (st === 'idle') this._cuboStartMic();
          else if (st === 'connecting' || st === 'live') this._cuboStopMic('client_end');
          // 'stopping': ignored until the talk has really ended.
        });
        this._cuboMicPaint();
      }
      
      if (!this.bpmOverlay) {
        this.bpmOverlay = document.createElement('div');
        this.bpmOverlay.style.cssText = 'position: absolute !important; top: 16px !important; left: 50% !important; transform: translateX(-50%) !important; z-index: 2147483647 !important; color: white !important; text-shadow: 1px 1px 3px black !important; font-weight: bold !important; font-size: 14px !important; pointer-events: none !important; background: rgba(0,0,0,0.3) !important; padding: 4px 10px !important; border-radius: 12px !important; align-items: center; justify-content: center;';
        this.appendChild(this.bpmOverlay);
      }
      
      // ── DVR timeline ────────────────────────────────────────────────
      // A scrub bar over the camera's own recording: hour ticks, a draggable
      // playhead, and the moment under it shown above. Releasing the playhead
      // asks cuboai.play_recording for that moment, and the Recording camera
      // entity plays it. Hidden unless the integration exposes that entity, so
      // older installs are unaffected.
      if (!this.dvrBar && this._config && this._config.show_timeline !== false) {
        // Measured against a real camera, twice, with opposite results: on
        // 2026-08-07 (light recording) 48h back returned frames; on 2026-08-10
        // (baby-presence detection on, heavy recording) everything before
        // roughly LOCAL MIDNIGHT was gone, and the boundary held still all
        // day — the camera stores whole per-day files and drops the oldest
        // day under space pressure. Retention is therefore "some whole days,
        // possibly just today", so the default span is one day; raise
        // timeline_hours if your SD card demonstrably holds more. The bar
        // also LEARNS its dead zone (see covNote below) and dims it.
        const HOURS = Number(this._config.timeline_hours) || 18;   // span shown
        // A minute of footage then silence looked like playback had failed.
        // Fifteen minutes is long enough to watch something; the bar restarts
        // it wherever you drop the playhead next.
        const PLAY_SECONDS = Number(this._config.timeline_play_seconds) || 900;
        // The freshest minute is still being written and will not play, so the
        // right-hand edge stops short of "now" rather than offering a moment
        // the camera will refuse.
        const EDGE_LAG_S = 120;
        // Painted faintly on the ruler so a phone screenshot settles "which
        // card build is this client actually running" — hours of cache-forensics
        // this session were exactly that question. Keep in sync with manifest.
        const CARD_VERSION = 'v2.6.46';

        const bar = document.createElement('div');
        bar.className = 'cuboai-dvr';
        bar.style.cssText = 'position:relative;width:100%;background:#000;color:#fff;' +
          'padding:6px 0 10px;font-family:inherit;user-select:none;touch-action:none;';

        const head0 = document.createElement('div');
        head0.style.cssText = 'display:flex;align-items:center;gap:10px;padding:0 12px 6px;';
        const stamp = document.createElement('div');
        stamp.style.cssText = 'font-size:13px;opacity:.9;flex:1 1 auto;';
        // Returning to live needs its own control. Dragging the playhead back
        // to the right-hand edge is fiddly on a phone and impossible to hit
        // exactly, so there was no reliable way out of playback.
        const liveBtn = document.createElement('button');
        liveBtn.textContent = '● LIVE';
        liveBtn.style.cssText = 'flex:0 0 auto;background:none;border:1px solid #03dac6;' +
          'color:#03dac6;border-radius:12px;padding:3px 10px;font:inherit;font-size:12px;' +
          'cursor:pointer;';
        head0.appendChild(stamp);
        head0.appendChild(liveBtn);
        bar.appendChild(head0);

        const track = document.createElement('div');
        track.style.cssText = 'position:relative;height:46px;margin:0 12px;cursor:ew-resize;';
        bar.appendChild(track);

        const ruler = document.createElement('canvas');
        ruler.style.cssText = 'width:100%;height:46px;display:block;';
        track.appendChild(ruler);

        const head = document.createElement('div');
        head.style.cssText = 'position:absolute;top:0;bottom:0;width:2px;background:#03dac6;' +
          'pointer-events:none;';
        const knob = document.createElement('div');
        knob.style.cssText = 'position:absolute;top:-6px;left:50%;transform:translateX(-50%);' +
          'width:14px;height:14px;border-radius:50%;background:#03dac6;';
        head.appendChild(knob);
        track.appendChild(head);

        // frac 0..1 across the visible span; 1 = the recent edge.
        let frac = 1;
        const FULL_SPAN_MS = HOURS * 3600 * 1000;
        const edgeAt = () => Date.now() - EDGE_LAG_S * 1000;
        // The bar CLAMPS itself to what the camera actually still holds: once
        // a seek has come back empty (emptyMax learned, see covNote below),
        // the left edge moves to that moment — the same window the official
        // app's own bar shows — and keeps tracking it as the camera prunes
        // oldest hours (observed sliding ~18-20h under heavy recording).
        // timeline_hours is the UPPER bound, not a promise.
        const spanNow = () => {
          const cov = covGet(this._dvrDev || (this._config || {}).device_id);
          if (cov && cov.emptyMax != null) {
            const s = edgeAt() - cov.emptyMax;
            if (s > 1800 * 1000 && s < FULL_SPAN_MS) return s;
          }
          return FULL_SPAN_MS;
        };
        const timeAt = (f) => new Date(edgeAt() - (1 - f) * spanNow());

        // Learned DVR coverage. The camera drops whole day-files under space
        // pressure, so part of the bar can be dead space that only reveals
        // itself when a seek comes back empty. Remember, per camera, the
        // newest moment known EMPTY (emptyMax) and the oldest known to PLAY
        // (okMin); the ruler dims everything at or before emptyMax. A later
        // success older than emptyMax contradicts it (the gap was privacy
        // mode, not rotation) and clears the mark.
        const covKey = (dev) => `cuboai_dvr_gap_${dev}`;
        const covGet = (dev) => {
          try { return JSON.parse(localStorage.getItem(covKey(dev)) || 'null'); } catch (e) { return null; }
        };
        const covNote = (dev, kind, tMs) => {
          if (!dev) return;
          const c = covGet(dev) || { emptyMax: null, okMin: null };
          if (kind === 'ok') {
            c.okMin = c.okMin == null ? tMs : Math.min(c.okMin, tMs);
            if (c.emptyMax != null && c.emptyMax >= tMs) c.emptyMax = null;
          } else if (c.okMin == null || tMs < c.okMin) {
            c.emptyMax = c.emptyMax == null ? tMs : Math.max(c.emptyMax, tMs);
          }
          try { localStorage.setItem(covKey(dev), JSON.stringify(c)); } catch (e) { /* private mode */ }
        };

        const drawRuler = () => {
          const w = track.clientWidth || 300;
          const dpr = window.devicePixelRatio || 1;
          ruler.width = w * dpr; ruler.height = 46 * dpr;
          const g = ruler.getContext('2d');
          g.setTransform(dpr, 0, 0, dpr, 0, 0);
          g.clearRect(0, 0, w, 46);
          const end = edgeAt(), start = end - spanNow();
          g.fillStyle = 'rgba(255,255,255,0.28)';
          g.font = '9px sans-serif';
          g.textAlign = 'right';
          g.fillText(CARD_VERSION, w - 3, 44);
          g.textAlign = 'left';
          // Dim the learned dead zone (rotated-away footage) before drawing
          // ticks over it, so scrubbing there is visibly a long shot.
          const cov = covGet(this._dvrDev || (this._config || {}).device_id);
          if (cov && cov.emptyMax != null && cov.emptyMax > start) {
            const x = Math.min(w, ((cov.emptyMax - start) / spanNow()) * w);
            g.fillStyle = 'rgba(255, 99, 99, 0.12)';
            g.fillRect(0, 0, x, 46);
          }
          // Tick spacing has to follow the span. Fifteen minutes was right for
          // a 12-hour bar and is 288 unreadable ticks across three days, so
          // pick the finest step that still leaves ticks ~8px apart, and label
          // roughly every sixth one.
          const MIN_PX = 8;
          const STEPS = [5, 15, 30, 60, 120, 180, 360, 720, 1440].map((m) => m * 60000);
          const step = STEPS.find((s) => (s / spanNow()) * w >= MIN_PX) || STEPS[STEPS.length - 1];
          const labelEvery = Math.max(1, Math.round((w / 6) / ((step / spanNow()) * w)));
          const overADay = spanNow() > 26 * 3600 * 1000;

          const first = Math.ceil(start / step) * step;
          g.textAlign = 'center';
          g.font = '10px system-ui, sans-serif';
          let i = 0;
          for (let t = first; t <= end; t += step, i++) {
            const x = ((t - start) / spanNow()) * w;
            const at = new Date(t);
            const major = i % labelEvery === 0;
            g.strokeStyle = major ? 'rgba(255,255,255,.85)' : 'rgba(255,255,255,.35)';
            g.beginPath();
            g.moveTo(x, major ? 22 : 30); g.lineTo(x, 44); g.stroke();
            if (major) {
              g.fillStyle = 'rgba(255,255,255,.85)';
              // Centred text runs off both ends of the canvas -- the first
              // label was rendering as "d, 15" instead of "Wed, 15". Pull the
              // edge labels inside instead of letting them clip.
              g.textAlign = x < 28 ? 'left' : (x > w - 28 ? 'right' : 'center');
              // Across more than a day, an hour alone is ambiguous: 03:00 today
              // and 03:00 yesterday look identical on the same bar.
              g.fillText(
                overADay
                  ? at.toLocaleString([], { weekday: 'short', hour: '2-digit' })
                  : at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
                x, 14
              );
            }
          }
        };

        const paint = () => {
          const w = track.clientWidth || 300;
          head.style.left = (frac * w) + 'px';
          const at = timeAt(frac);
          const live = frac > 0.999;
          // Bound the picker to what the camera still holds, and keep it in
          // step with the playhead so the two controls never disagree.
          when.min = asLocalInput(new Date(edgeAt() - spanNow()));
          when.max = asLocalInput(new Date(edgeAt()));
          // Filled even while live: an empty box gives you nothing to adjust
          // from and no hint of the format it wants. Never while it has focus
          // though -- paint() also runs on a 30s tick and on resize, and would
          // overwrite a time you were halfway through typing.
          if (document.activeElement !== when) when.value = asLocalInput(at);
          // While playback runs the 1s clock owns the stamp, so the 30s repaint
          // must not overwrite the running timecode with a static date label.
          // BUT a DRAG is the exception: the label must PREVIEW the moment under
          // the playhead as you move it, otherwise it keeps showing the playing
          // position and a reverse-drag looks like "the time isn't changing"
          // (the small picker box tracked the drag, the big label did not).
          if (!this._dvrPlaying || this._dvrDragging) {
            stamp.textContent = live
              ? 'Live'
              : at.toLocaleString([], { month: '2-digit', day: '2-digit',
                                        hour: '2-digit', minute: '2-digit', second: '2-digit' });
          }
        };

        const seek = (clientX) => {
          const r = track.getBoundingClientRect();
          frac = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
          paint();
        };

        // Point the picture that is already on screen at an entity. Swapping
        // the child's config is what keeps playback inside THIS card: the
        // alternative -- and what this did before -- was to leave the recording
        // playing on a separate entity you had to add a second card for.
        const showEntity = (entityId, muted) => {
          if (!this.content || !this.content.setConfig) return false;
          const found = { entityId, state: (this._hass.states || {})[entityId] };
          const cfg = cuboaiWebrtcConfig(found, muted);
          this._dvrEntity = entityId;
          // The card's own switch to or from a recording: not a reconnect, so
          // no frozen live picture labelled "Reconnecting…" over it.
          const bridged = this._cuboMedia().video;
          if (bridged && bridged.__cuboBridge && bridged.__cuboBridge.quiet) bridged.__cuboBridge.quiet();
          this.content.setConfig(cfg);
          this.content.hass = this._hass;
          // setConfig alone never re-dials: the player's onconnect() refuses
          // while a ws/pc is active, and with `background: true` (which keeps
          // sound alive minimized) there are no visibility disconnects left to
          // apply the new entity by accident — the picture silently stayed
          // live while the bar said "Playing". Force the same reload cycle the
          // player's own stream-switch button uses (disconnect, then reconnect
          // with the new config). One fixed 150ms delay was NOT enough: over a
          // remote connection (4G / Nabu Casa) the old WebSocket takes longer
          // than that to actually close, onconnect() still saw it and refused,
          // and the picture froze on the last live frame. Poll until the old
          // connection is really gone (up to ~5s), then dial.
          if (this.content.ondisconnect && this.content.onconnect) {
            this.content.ondisconnect();
            let tries = 0;
            const content = this.content;
            clearInterval(this._dvrRedial);
            this._dvrRedial = setInterval(() => {
              tries += 1;
              const busy = content.ws || content.pc;
              if (!busy || tries >= 25) {
                clearInterval(this._dvrRedial);
                this._dvrRedial = null;
                try { content.onconnect(); } catch (e) { /* best-effort */ }
                // The swapped stream arrives into a video element whose
                // autoplay moment was consumed by the live view, and the
                // card's unmute logic deliberately skips play() when the user
                // muted (or on Apple). Nothing else starts the new stream —
                // MSE buffers fill while the element sits PAUSED on its last
                // live frame (verified live: buffers full, paused: true; one
                // play() un-froze it). Kick play() once the new source has
                // data, keeping whatever mute state the user chose.
                setTimeout(() => {
                  const v = content.video;
                  if (!v) return;
                  // The reused video element also keeps the PREVIOUS stream's
                  // mute state — a live view muted by the user stayed muted
                  // through the swap, so recorded playback ran silent. Order
                  // matters and iOS dictates it: an UNMUTED play() outside a
                  // live tap is rejected there and can blank the picture
                  // entirely. So: always START muted (never black), then try
                  // lifting the mute; where the platform refuses, playback
                  // continues muted and the speaker icon (a real tap) brings
                  // the sound — same UX as the live view.
                  const go = () => {
                    v.muted = true;
                    const p = v.play();
                    Promise.resolve(p).then(() => {
                      if (muted) return;      // swap asked for silence — keep it
                      v.muted = false;
                      const q = v.play();
                      if (q && q.catch) q.catch(() => { v.muted = true; v.play().catch(() => {}); });
                    }).catch(() => {});
                  };
                  v.addEventListener('loadeddata', go, { once: true });
                  if (v.readyState >= 2) go();
                }, 300);
              }
            }, 200);
          }
          return true;
        };

        const goLive = () => {
          frac = 1;
          this._dvrPlaying = false;
          this._dvrShownMs = null;   // back to live: badge shows wall-clock again
          clearInterval(this._dvrWait); this._dvrWait = null;
          clearInterval(this._dvrClock); this._dvrClock = null;
          const live = cuboaiFindCameraState(this._hass, (this._config || {}).device_id);
          if (live) showEntity(live.entityId, this.isMuted);
          this._dvrEntity = null;
          liveBtn.style.opacity = '.45';
          paint();
        };
        liveBtn.addEventListener('click', goLive);
        liveBtn.style.opacity = '.45';

        const playFrom = (at) => {
          if (!this._hass) return;
          // An unpinned card still knows which camera it is showing -- it
          // resolved one to put a picture on screen. Taking the id from that
          // entity's attributes is what makes playback work without
          // `device_id:` in the card config; asking the service for device_id
          // "" just raises "No CuboAI camera with device_id ''".
          const pinned = (this._config || {}).device_id;
          const rec = cuboaiFindRecordingState(this._hass, pinned);
          const liveCam = cuboaiFindCameraState(this._hass, pinned);
          const attrsOf = (f) => (f && f.state && f.state.attributes) || {};
          const dev = pinned || attrsOf(rec).device_id || attrsOf(liveCam).device_id || '';
          if (!dev) {
            stamp.textContent = 'No CuboAI camera found';
            return;
          }
          this._dvrDev = dev;   // lets the ruler find this camera's learned coverage
          // A RE-SEEK (reverse, ±nudge, picker Go, chunk-continue) enters here
          // while the PREVIOUS request's running clock is still ticking. Kill it
          // now — it is otherwise cleared only inside the new wait's success
          // branch below, so until then it keeps repainting the OLD time over
          // this new "Loading" label. And null the per-seek currentTime baseline
          // (see the clock): the recording stream reuses the same deterministic
          // RTSP URL, so the <video> element is reused and its currentTime does
          // NOT restart from the new target — the clock must re-baseline, or the
          // label reads new-target + all the seconds the previous moment played.
          clearInterval(this._dvrClock); this._dvrClock = null;
          this._dvrBaseCT = null;
          this._dvrOkNoted = false;
          const seekTarget = at.getTime();
          // The service takes an absolute time as well as "10m"-style offsets.
          this._hass.callService('cuboai', 'play_recording', {
            device_id: dev,
            start_time: at.toISOString(),
            duration: PLAY_SECONDS,
          });
          this._dvrPlaying = true;
          liveBtn.style.opacity = '1';
          stamp.textContent = 'Loading ' +
            at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) + '…';

          // The producer has to connect to the camera and seek before the
          // entity has a stream, so the swap waits for it to become available
          // rather than showing an unavailable picture for several seconds.
          if (!rec) {
            stamp.textContent = 'No recording entity — reload the integration';
            return;
          }
          let waited = 0;
          clearInterval(this._dvrWait);
          this._dvrWait = setInterval(() => {
            waited += 500;
            const st = (this._hass.states || {})[rec.entityId];
            // Readiness is `playing_from`, not the entity state. The entity is
            // always available -- it has to be, or its attributes never reach
            // the frontend and this card cannot find it at all -- so its state
            // says nothing about whether the producer has seeked yet.
            //
            // It must MATCH THIS seek's target, not merely be truthy: on a
            // re-seek `playing_from` still holds the PREVIOUS request's time
            // (the backend flips it old-value -> new-value with no null gap), so
            // a truthiness test passed on the stale value and swapped onto the
            // old footage before the new target was live. The service stores the
            // exact epoch we sent, so this compares equal within a slack window.
            const pf = st && st.attributes && st.attributes.playing_from;
            const ready = pf && Math.abs(new Date(pf).getTime() - seekTarget) < 2000;
            if (ready) {
              clearInterval(this._dvrWait);
              this._dvrWait = null;
              // Recorded audio on -- unless a talk is holding the room's sound off.
              showEntity(rec.entityId, !!(this._mic && this._mic.talkMute));
              // Success is NOT noted here: the engine reports "started" even
              // for a moment it then finds empty. The playback clock notes
              // 'ok' only once >5s of footage has really played.
              this._dvrOkNoted = false;
              stamp.textContent = 'Playing ' +
                at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
              // Official-app-style running timecode: the label and the
              // playhead track the FOOTAGE moment (seek time + seconds the
              // video element actually played), instead of freezing at the
              // request time while minutes of footage roll by — which read
              // as "the time bar is wrong".
              const playedFrom = at.getTime();
              // Seconds of footage actually played SINCE THIS seek. The <video>
              // element is reused across seeks (same RTSP URL), so currentTime
              // is an absolute, ever-growing counter, not "seconds since the
              // target". Baseline it on first read and re-baseline if it ever
              // drops (a re-dial that did reset the element) so elapsed always
              // starts at 0 for this moment.
              const elapsedOf = (vid) => {
                if (!vid) return 0;
                if (this._dvrBaseCT == null || vid.currentTime < this._dvrBaseCT) this._dvrBaseCT = vid.currentTime;
                return Math.max(0, vid.currentTime - this._dvrBaseCT);
              };
              clearInterval(this._dvrClock);
              this._dvrClock = setInterval(() => {
                if (!this._dvrPlaying) { clearInterval(this._dvrClock); this._dvrClock = null; return; }
                // Hold the running timecode while the user is scrubbing — paint()
                // is showing the moment under the playhead, and this 1s tick must
                // not fight it back to the playing position.
                if (this._dvrDragging) return;
                // The camera's SD card does not hold every minute (retention
                // and coverage vary). A moment with no data makes the DVR
                // producer exit empty and the player shows a raw error
                // overlay and retries forever. Say what happened instead,
                // and go back to live.
                const modeEl = this.content && this.content.querySelector && this.content.querySelector('.mode');
                if (modeEl && /error/i.test(modeEl.innerText || '')) {
                  clearInterval(this._dvrClock); this._dvrClock = null;
                  // Two very different reasons land here. The service plays at
                  // most 900 s of footage per request — when a chunk ends, the
                  // producer exits and the player errors exactly like an empty
                  // moment does. If real footage played, CONTINUE from where it
                  // stopped (the next chunk keeps the session rolling past the
                  // 15-minute cap); only an error with nothing played means the
                  // SD card holds nothing there.
                  const vv = this.content && this.content.video;
                  const playedS = elapsedOf(vv);
                  if (playedS > 5) {
                    playFrom(new Date(playedFrom + playedS * 1000));
                  } else {
                    stamp.textContent = 'Nothing recorded at that moment — try another time';
                    covNote(dev, 'empty', at.getTime());
                    drawRuler();
                    setTimeout(() => { if (this._dvrPlaying) goLive(); }, 2500);
                  }
                  return;
                }
                const v = this.content && this.content.video;
                if (!v || v.readyState < 2) return;
                const elapsed = elapsedOf(v);
                if (!this._dvrOkNoted && elapsed > 5) {
                  this._dvrOkNoted = true;
                  covNote(dev, 'ok', playedFrom);
                  drawRuler();
                }
                const shownMs = playedFrom + elapsed * 1000;
                // Publish the footage moment so the on-video timestamp badge can
                // show it during playback (the badge shows wall-clock only while
                // live). Cleared in goLive().
                this._dvrShownMs = shownMs;
                const f = 1 - (edgeAt() - shownMs) / spanNow();
                if (f >= 0 && f <= 1) {
                  frac = f;
                  head.style.left = (frac * (track.clientWidth || 300)) + 'px';
                }
                stamp.textContent = 'Playing ' + new Date(shownMs)
                  .toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
              }, 1000);
            } else if (waited >= 30000) {
              clearInterval(this._dvrWait);
              this._dvrWait = null;
              stamp.textContent = 'Nothing recorded at that moment';
              covNote(dev, 'empty', at.getTime());
              drawRuler();
            }
          }, 500);
        };

        const commit = () => {
          if (frac > 0.999) return goLive();      // dropped back on "live"
          playFrom(timeAt(frac));
        };

        // Pick a moment instead of aiming at it. At phone width the bar is
        // ~366px across two days, so one pixel is about eight minutes -- fine
        // for "last night", useless for "02:14".
        const picker = document.createElement('div');
        picker.style.cssText = 'display:flex;align-items:center;gap:8px;padding:8px 12px 0;';
        const when = document.createElement('input');
        when.type = 'datetime-local';
        when.step = '1';
        when.style.cssText = 'flex:1 1 auto;min-width:0;background:#1c1c1e;color:#fff;' +
          'border:1px solid #444;border-radius:6px;padding:6px 8px;font:inherit;font-size:13px;' +
          'color-scheme:dark;';
        const go = document.createElement('button');
        go.textContent = 'Go';
        go.style.cssText = 'flex:0 0 auto;background:#03dac6;color:#000;border:none;' +
          'border-radius:6px;padding:6px 14px;font:inherit;font-size:13px;cursor:pointer;';
        picker.appendChild(when);
        picker.appendChild(go);
        bar.appendChild(picker);

        // Local time, no timezone suffix -- what datetime-local speaks.
        const asLocalInput = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000)
          .toISOString().slice(0, 19);

        const goToPicked = () => {
          if (!when.value) return;
          const at = new Date(when.value);        // parsed as local time
          if (isNaN(at)) return;
          const end = edgeAt(), start = end - spanNow();
          if (at.getTime() >= end) return goLive();
          if (at.getTime() < start) {
            // Say so rather than seek into nothing and time out after 30s.
            stamp.textContent = 'Older than the camera still holds';
            return;
          }
          frac = 1 - (end - at.getTime()) / spanNow();
          paint();
          playFrom(at);
        };
        // iOS renders datetime-local as a native wheel with hours and minutes
        // and no seconds, whatever `step` says. These work on every platform
        // and are easier than a wheel when you are hunting for a moment.
        const fine = document.createElement('div');
        fine.style.cssText = 'display:flex;gap:6px;padding:6px 12px 0;';
        const nudge = (secs) => {
          const end = edgeAt(), start = end - spanNow();
          const from = when.value ? new Date(when.value).getTime() : timeAt(frac).getTime();
          const t = Math.min(Math.max(from + secs * 1000, start), end);
          frac = 1 - (end - t) / spanNow();
          paint();
          stamp.textContent = 'Loading ' + new Date(t).toLocaleTimeString([],
            { hour: '2-digit', minute: '2-digit', second: '2-digit' }) + '…';
          // Tapping -10s four times should be one seek, not four: each call
          // restarts a producer that takes seconds to connect and seek.
          clearTimeout(this._dvrNudge);
          this._dvrNudge = setTimeout(() => playFrom(new Date(t)), 700);
        };
        for (const [label, secs] of [['-1m', -60], ['-10s', -10], ['+10s', 10], ['+1m', 60]]) {
          const b = document.createElement('button');
          b.textContent = label;
          b.style.cssText = 'flex:1 1 0;background:none;border:1px solid #444;color:#ddd;' +
            'border-radius:6px;padding:6px 0;font:inherit;font-size:13px;cursor:pointer;';
          b.addEventListener('click', () => nudge(secs));
          fine.appendChild(b);
        }
        bar.appendChild(fine);

        go.addEventListener('click', goToPicked);
        when.addEventListener('keydown', (e) => { if (e.key === 'Enter') goToPicked(); });

        // Shared on the instance (not a local) so paint() and the 1s playback
        // clock can see it and let the drag preview the seek target.
        this._dvrDragging = false;
        track.addEventListener('pointerdown', (e) => {
          this._dvrDragging = true; track.setPointerCapture(e.pointerId); seek(e.clientX);
        });
        track.addEventListener('pointermove', (e) => { if (this._dvrDragging) seek(e.clientX); });
        const endDrag = (e) => {
          if (!this._dvrDragging) return;
          this._dvrDragging = false;
          try { track.releasePointerCapture(e.pointerId); } catch (_) {}
          commit();
        };
        track.addEventListener('pointerup', endDrag);
        track.addEventListener('pointercancel', endDrag);

        // Keep the ruler honest as time passes and on resize; while the user is
        // dragging, leave the playhead alone.
        this._dvrTick = setInterval(() => { if (!this._dvrDragging) { drawRuler(); paint(); } }, 30000);
        if (window.ResizeObserver) {
          this._dvrResize = new ResizeObserver(() => { drawRuler(); paint(); });
          this._dvrResize.observe(track);
        }
        requestAnimationFrame(() => { drawRuler(); paint(); });

        this.dvrBar = bar;
        this.appendChild(bar);
      }

      if (!this.envOverlay) {
        this.envOverlay = document.createElement('div');
        this.envOverlay.style.cssText = 'position: absolute !important; bottom: 60px !important; left: 16px !important; z-index: 2147483647 !important; color: white !important; text-shadow: 1px 1px 3px black !important; font-weight: bold !important; font-size: 14px !important; pointer-events: none !important; background: rgba(0,0,0,0.3) !important; padding: 4px 10px !important; border-radius: 12px !important; display: flex !important; gap: 10px !important; align-items: center;';
        this.appendChild(this.envOverlay);
      }

      // ── Opt-in timestamp badge (issue #99, show_timestamp: true) ──────────
      // Driven by FRAME PROGRESS, not a wall clock: a plain clock keeps ticking
      // over a frozen frame, which is exactly the failure the badge exists to
      // expose. While video.currentTime advances it shows the current time;
      // when frames stop for >4s it freezes at the last-advance moment and
      // turns red — a stale image now looks stale.
      if (this._config && this._config.show_timestamp === true) {
        if (!this.tsOverlay) {
          this.tsOverlay = document.createElement('div');
          this.tsOverlay.style.cssText = 'position: absolute !important; bottom: 60px !important; right: 16px !important; z-index: 2147483647 !important; color: white !important; text-shadow: 1px 1px 3px black !important; font-weight: bold !important; font-size: 14px !important; pointer-events: none !important; background: rgba(0,0,0,0.3) !important; padding: 4px 10px !important; border-radius: 12px !important; display: none; align-items: center;';
          this.appendChild(this.tsOverlay);
          this._tsLastMediaTime = -1;
          this._tsLastAdvance = 0;
        }
        // (Re)arm separately from element creation: disconnectedCallback clears
        // the interval but the element property survives a re-mount, so a
        // create-only guard would leave the badge permanently frozen.
        if (!this._tsClock) {
        const STALL_MS = 4000;
        const fmt = (d) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        this._tsClock = setInterval(() => {
          const o = this.tsOverlay;
          if (!o) return;
          const v = this.content && this.content.video;
          // Honor a live config change (the card editor preview re-runs
          // setConfig on the same element): unchecking the option must stop
          // the badge, not leave the interval painting it forever.
          if (!(this._config && this._config.show_timestamp === true)) { o.style.display = 'none'; return; }
          if (!v) { o.style.display = 'none'; return; }
          // During DVR playback show the FOOTAGE moment (published by the scrub
          // clock), not wall-clock — the badge stays visible when you reverse
          // into a recording instead of vanishing. Plain style: the stall/red
          // logic is a "is the LIVE image frozen" signal and does not apply to
          // recorded playback.
          if (this._dvrPlaying) {
            if (this._dvrShownMs) {
              o.textContent = fmt(new Date(this._dvrShownMs));
              o.style.background = 'rgba(0,0,0,0.3)';
              o.style.display = 'flex';
            } else {
              o.style.display = 'none';   // playback starting, no footage moment yet
            }
            return;
          }
          const now = Date.now();
          // A reconnect swaps in a fresh MediaStream and currentTime restarts
          // near 0 — far BELOW the remembered high-water mark. Without this
          // re-anchor the badge would stay red forever after a recovery.
          if (v.currentTime < this._tsLastMediaTime - 1) this._tsLastMediaTime = v.currentTime;
          if (v.currentTime > this._tsLastMediaTime + 0.05) {
            this._tsLastMediaTime = v.currentTime;
            this._tsLastAdvance = now;
          }
          if (!this._tsLastAdvance) { o.style.display = 'none'; return; }  // nothing played yet
          const stalled = now - this._tsLastAdvance > STALL_MS;
          if (stalled) {
            o.textContent = '⚠ ' + fmt(new Date(this._tsLastAdvance));
            o.style.background = 'rgba(198,40,40,0.75)';
          } else {
            o.textContent = fmt(new Date(now));
            o.style.background = 'rgba(0,0,0,0.3)';
          }
          o.style.display = 'flex';
        }, 1000);
        }
      }


      customElements.whenDefined('webrtc-camera').then(() => {
        if (!this.content) {
          this.content = document.createElement('webrtc-camera');
          // The camera this picture shows: an unpinned card's mic talks to it
          // and to nothing else (_cuboMicDeviceId).
          this._liveDeviceId = cuboaiCameraDeviceId(found);
          if (this.content.setConfig) {
            // Rebuilt picture (dashboard navigation, a re-render that dropped
            // the child) while a recording is playing: restore what the scrub
            // bar says is on screen, not live.
            if (this._dvrPlaying && this._dvrEntity) webrtcConfig.entity = this._dvrEntity;
            this.content.setConfig(webrtcConfig);
          }
          this.content.hass = this._hass;
          this.appendChild(this.content);
          // Note: mobile browsers block UNMUTED autoplay by policy, so a card
          // configured to open with sound still starts muted on mobile until the
          // user taps webrtc-camera's own volume button. We intentionally do NOT
          // intercept taps here — doing so fought the volume button and prevented
          // unmuting ("can't hear"). The initial muted state is passed via the
          // webrtc-camera `muted` config above.
          if (this.bpmOverlay) this.appendChild(this.bpmOverlay);
          if (this.envOverlay) this.appendChild(this.envOverlay);
          // Re-appending moves it, so the scrub bar sits below the picture.
          if (this.dvrBar) this.appendChild(this.dvrBar);
          
          
          // Add Music Player Bar & Song Library
          const defaultSongs = [];

          const loadCustomSongs = () => {
            try {
              let libraryState = null;
              if (this._hass && this._hass.states) {
                for (const key in this._hass.states) {
                  if (key.startsWith('sensor.cuboai_media_library')) {
                    libraryState = this._hass.states[key];
                    break;
                  }
                }
              }
              const libSongs = (libraryState && libraryState.attributes && libraryState.attributes.custom_songs) || [];

              // Restore from this browser's local storage whenever the server
              // library is EMPTY but local storage still has songs — recovers
              // from accidental wipes and covers first-time migration. Only
              // runs when the library sensor is actually present (so a slow
              // startup doesn't false-trigger a restore).
              let stored = localStorage.getItem(`cuboai_custom_songs_${deviceId}`);
              if (stored && libraryState && libSongs.length === 0) {
                const parsed = JSON.parse(stored);
                if (parsed && parsed.length > 0) {
                  localStorage.setItem(`cuboai_custom_songs_migrated_${deviceId}`, 'true');
                  setTimeout(() => saveCustomSongs(parsed), 500);
                  return Array.isArray(parsed) ? parsed.filter(s => s) : [];
                }
              }

              if (libSongs.length > 0) {
                return JSON.parse(JSON.stringify(libSongs));
              }
            } catch(e) {}
            return JSON.parse(JSON.stringify(defaultSongs));
          };

          const saveCustomSongs = (songs) => {
            localStorage.setItem(`cuboai_custom_songs_${deviceId}`, JSON.stringify(songs));
            if (this._hass) {
              this._hass.callService('cuboai', 'save_custom_songs', { songs: songs });
            }
          };

          const loadPlaylists = () => {
            try {
              let libraryState = null;
              if (this._hass && this._hass.states) {
                for (const key in this._hass.states) {
                  if (key.startsWith('sensor.cuboai_media_library')) {
                    libraryState = this._hass.states[key];
                    break;
                  }
                }
              }
              const libPlaylists = (libraryState && libraryState.attributes && libraryState.attributes.playlists) || [];

              // Restore from local storage only when the server library is empty
              let stored = localStorage.getItem(`cuboai_playlists_${deviceId}`);
              if (stored && libraryState && libPlaylists.length === 0) {
                const parsed = JSON.parse(stored);
                if (parsed && parsed.length > 0) {
                  localStorage.setItem(`cuboai_playlists_migrated_${deviceId}`, 'true');
                  setTimeout(() => savePlaylists(parsed), 500);
                  return Array.isArray(parsed) ? parsed : [];
                }
              }

              return JSON.parse(JSON.stringify(libPlaylists));
            } catch (e) {
              return [];
            }
          };

          const savePlaylists = (playlists) => {
            localStorage.setItem(`cuboai_playlists_${deviceId}`, JSON.stringify(playlists));
            if (this._hass) {
              this._hass.callService('cuboai', 'save_playlists', { playlists: playlists });
            }
          };

          // Shared per-camera card settings (shuffle, ...) stored server-side in the
          // media library so they sync across all devices/browsers.
          const findLibraryState = () => {
            if (!this._hass || !this._hass.states) return null;
            for (const key in this._hass.states) {
              if (key.startsWith('sensor.cuboai_media_library')) return this._hass.states[key];
            }
            return null;
          };
          const loadSharedSettings = () => {
            try {
              const lib = findLibraryState();
              if (lib && lib.attributes && lib.attributes.settings) {
                return lib.attributes.settings[deviceId] || {};
              }
            } catch (e) {}
            return {};
          };
          const saveSharedSettings = (patch) => {
            try {
              this._settingsWriteTs = Date.now();
              const lib = findLibraryState();
              const all = (lib && lib.attributes && lib.attributes.settings)
                ? JSON.parse(JSON.stringify(lib.attributes.settings)) : {};
              all[deviceId] = Object.assign({}, all[deviceId] || {}, patch);
              if (this._hass) this._hass.callService('cuboai', 'save_settings', { settings: all });
            } catch (e) {}
          };

          if (this._playlistPage === undefined) {
             this._playlistPage = 1;
             this._songPage = 1;
             this._deviceId = deviceId;
             const sharedSettings = loadSharedSettings();
             // Server-side value wins; localStorage is only the pre-sync fallback
             this._shuffleMode = sharedSettings.shuffle !== undefined
               ? !!sharedSettings.shuffle
               : localStorage.getItem(`cuboai_shuffle_${deviceId}`) === 'true';
             this._repeatMode = localStorage.getItem(`cuboai_repeat_${deviceId}`) || 'off';
             
             // Only clear inPlaylist if it's the first render to prevent checking jumping
             const cs = loadCustomSongs();
             let changed = false;
             cs.forEach(s => { if (s.inPlaylist) { s.inPlaylist = false; changed = true; } });
             if (changed) saveCustomSongs(cs);
          }
          this.musicBar = document.createElement('div');
          this.musicBar.style.cssText = 'display: flex; flex-direction: column; margin-top: 8px; padding: 12px; border-top: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12)); background: var(--card-background-color, #fff); border-radius: 0 0 12px 12px; font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); color: var(--primary-text-color, #212121);';
          // show_music:false hides the whole lullaby/music area (issue #100).
          // The element is still BUILT (the build below is one long straight-line
          // block; hiding beats restructuring it) — it just never displays.
          if (this._config && this._config.show_music === false) this.musicBar.style.display = 'none';

          this.musicBar.innerHTML = `
            <style>
              .cubo-row { display: flex; align-items: center; margin-bottom: 8px; gap: 8px; }
              .cubo-input { flex-grow: 1; padding: 8px 12px; border-radius: 6px; border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.2)); background: var(--card-background-color, #fff); color: var(--primary-text-color, #000); font-size: 14px; outline: none; transition: border-color 0.2s; }
              .cubo-input:focus { border-color: var(--primary-color, #03a9f4); }
              .cubo-btn { padding: 8px 16px; border-radius: 6px; border: none; background: var(--primary-color, #03a9f4); color: white; font-weight: bold; cursor: pointer; font-size: 14px; transition: opacity 0.2s; }
              .cubo-btn:hover { opacity: 0.9; }
              .cubo-btn-red { background: var(--error-color, #f44336); }
              .cubo-btn-sec { background: var(--secondary-background-color, #e0e0e0); color: var(--primary-text-color, #212121); }
              
              .library-header { display: flex; align-items: center; justify-content: space-between; margin-top: 12px; padding-top: 8px; border-top: 1px dashed var(--divider-color, rgba(0, 0, 0, 0.1)); }
              .library-title { font-size: 14px; font-weight: bold; color: var(--secondary-text-color, #727272); }
              
              .filter-bar { display: flex; gap: 8px; margin-top: 8px; margin-bottom: 8px; }
              .cubo-select { padding: 6px; border-radius: 6px; border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.2)); background: var(--card-background-color, #fff); color: var(--primary-text-color, #000); font-size: 12px; outline: none; }
              
              .song-list { max-height: 150px; overflow-y: auto; margin-top: 8px; display: flex; flex-direction: column; gap: 4px; padding-right: 4px; }
              .song-item { display: flex; align-items: center; justify-content: space-between; padding: 6px 10px; border-radius: 6px; background: var(--secondary-background-color, #f5f5f5); border: 1px solid var(--divider-color, rgba(0,0,0,0.05)); font-size: 13px; }
              .song-info { display: flex; align-items: center; gap: 8px; flex-grow: 1; min-width: 0; }
              .song-badge { font-size: 10px; padding: 2px 6px; border-radius: 4px; background: var(--primary-color, #03a9f4); color: white; text-transform: uppercase; font-weight: bold; }
              .song-badge.spotify { background: #1ed760; }
              .song-badge.youtube { background: #ff0000; }
              .song-badge.lullabies { background: #9c27b0; }
              .song-badge.custom { background: #607d8b; }
              .song-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }
              
              .song-actions { display: flex; gap: 6px; align-items: center; }
              .icon-btn { border: none; background: none; color: var(--primary-text-color, #212121); cursor: pointer; display: flex; align-items: center; justify-content: center; padding: 4px; border-radius: 4px; transition: background 0.2s; }
              .icon-btn:hover { background: rgba(0,0,0,0.08); }
              .icon-btn.delete { color: var(--error-color, #f44336); }
              
              .add-form { display: none; flex-direction: column; gap: 8px; margin-top: 10px; padding: 10px; border-radius: 8px; background: var(--secondary-background-color, #fafafa); border: 1px solid var(--divider-color, rgba(0,0,0,0.08)); }
            </style>

            <div id="nowPlaying" class="cubo-row" style="display: none; background: rgba(3, 169, 244, 0.1); padding: 8px 12px; border-radius: 6px; border: 1px solid rgba(3, 169, 244, 0.2); margin-bottom: 12px; align-items: center;">
              <ha-icon icon="mdi:volume-high" style="color: var(--primary-color, #03a9f4); margin-right: 8px;"></ha-icon>
              <span id="nowPlayingText" style="font-weight: bold; font-size: 13px; color: var(--primary-color, #03a9f4);">Now Playing: ...</span>
            </div>

            <div class="cubo-row">
              <ha-icon icon="mdi:music" style="color: var(--secondary-text-color);"></ha-icon>
              <input type="text" id="musicUrl" class="cubo-input" placeholder="Paste YouTube or Spotify link...">
              <button id="playMusicBtn" class="cubo-btn">Play</button>
              <button id="stopMusicBtn" class="cubo-btn cubo-btn-red">Stop</button>
            </div>

            <div class="library-header" style="justify-content: space-between; flex-wrap: wrap; gap: 6px;">
              <div class="library-title">Saved Playlists</div>
              <div style="display: flex; gap: 6px; align-items: center; flex-wrap: wrap; justify-content: flex-end; max-width: 100%;">
                <select id="playlistUserFilter" class="cubo-select" style="padding: 2px; font-size: 11px; min-height: unset; height: auto;">
                  <option value="all" ${this._playlistUserFilter === 'me' ? '' : 'selected'}>All Users</option>
                  <option value="me" ${this._playlistUserFilter === 'me' ? 'selected' : ''}>My Playlists</option>
                </select>
                <button id="toggleShuffleBtn" class="cubo-btn cubo-btn-sec" style="padding: 2px 6px; font-size: 11px; display: flex; align-items: center; gap: 4px;">
                  <ha-icon icon="mdi:shuffle" style="--mdc-icon-size: 14px;"></ha-icon> <span>Shuffle: OFF</span>
                </button>
                <button id="toggleRepeatBtn" class="cubo-btn cubo-btn-sec" style="padding: 2px 6px; font-size: 11px; display: flex; align-items: center; gap: 4px;">
                  <ha-icon icon="mdi:repeat" style="--mdc-icon-size: 14px;"></ha-icon> <span>Repeat: OFF</span>
                </button>
                <button id="toggleCacheBtn" class="cubo-btn cubo-btn-sec" style="padding: 2px 6px; font-size: 11px; display: none; align-items: center; gap: 4px;" title="Save YouTube/Spotify songs to local cache">
                  <ha-icon icon="mdi:download" style="--mdc-icon-size: 14px;"></ha-icon> <span>Cache: OFF</span>
                </button>
                <button id="clearCacheBtn" class="cubo-btn cubo-btn-sec" style="padding: 2px 6px; font-size: 11px; display: none; align-items: center; gap: 4px;" title="Delete all locally cached songs">
                  <ha-icon icon="mdi:delete-sweep" style="--mdc-icon-size: 14px;"></ha-icon>
                </button>
                <select id="playTimeSelect" class="cubo-select" style="padding: 2px; font-size: 11px; min-height: unset; height: auto; max-width: 130px;" title="Speaker Play Time">
                  <option value="0">Play Time: Infinite</option>
                  <option value="10">10 mins</option>
                  <option value="20">20 mins</option>
                  <option value="30">30 mins</option>
                  <option value="60">1 hour</option>
                  <option value="90">1.5 hours</option>
                  <option value="120">2 hours</option>
                </select>
              </div>
            </div>
            
            <div id="playlistsContainer" style="display: flex; flex-direction: column; gap: 4px; margin-top: 8px;"></div>
            
            <div class="library-header" style="margin-top: 16px;">
              <div class="library-title">Song Library</div>
              <div style="display: flex; gap: 6px;">
                <button id="toggleAddFormBtn" class="cubo-btn cubo-btn-sec" style="padding: 4px 8px; font-size: 12px;">+ Add Song</button>
              </div>
            </div>



            <div id="addForm" class="add-form">
              <div id="addFormTitle" style="font-weight: bold; font-size: 12px; margin-bottom: 4px;">Add New Song to Library</div>
              <div id="addFormError" style="color: var(--error-color, #f44336); font-size: 11px; margin-bottom: 4px; display: none;"></div>
              <input type="text" id="newSongName" class="cubo-input" placeholder="Song Name" style="margin-bottom: 4px;">
              <input type="text" id="newSongUrl" class="cubo-input" placeholder="YouTube or Spotify Link" style="margin-bottom: 4px;">
              <div class="cubo-row" style="margin-bottom: 0;">
                <select id="newSongCat" class="cubo-select" style="flex-grow: 1;">
                  <option value="youtube">YouTube</option>
                  <option value="spotify">Spotify</option>
                </select>
                <button id="saveSongBtn" class="cubo-btn" style="padding: 6px 12px; font-size: 12px;">Save</button>
              </div>
            </div>

            <div class="filter-bar" style="display: flex; gap: 8px; flex-wrap: wrap;">
              <input type="text" id="searchBar" class="cubo-input" placeholder="Search song name..." style="padding: 4px 8px; font-size: 12px; flex-grow: 1; min-width: 0;">
              <select id="categoryFilter" class="cubo-select" style="max-width: 100px;">
                <option value="all">All Categories</option>
                <option value="youtube">YouTube</option>
                <option value="spotify">Spotify</option>
                <option value="lullabies">Lullabies</option>
                <option value="user_added">User Added</option>
              </select>
              <select id="userFilter" class="cubo-select" style="max-width: 100px;">
                <option value="all">All Users</option>
              </select>
              <select id="sortFilter" class="cubo-select" style="max-width: 100px;">
                <option value="newest">Newest First</option>
                <option value="oldest">Oldest First</option>
                <option value="name_asc">Name (A-Z)</option>
                <option value="name_desc">Name (Z-A)</option>
              </select>
            </div>

            <div id="songListContainer" class="song-list"></div>

            <div id="quickAddModal" style="display: none; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 1000; align-items: center; justify-content: center; backdrop-filter: blur(2px); border-radius: 0 0 12px 12px;">
              <div style="background: var(--card-background-color, #fff); padding: 16px; border-radius: 8px; width: 85%; max-width: 300px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); display: flex; flex-direction: column;">
                <div style="font-weight: bold; margin-bottom: 12px; color: var(--primary-text-color, #212121);">Add to Playlist</div>
                <div id="quickAddPlaylistList" style="max-height: 150px; overflow-y: auto; margin-bottom: 12px; border: 1px solid var(--divider-color, rgba(0,0,0,0.1)); border-radius: 4px; display: flex; flex-direction: column;">
                </div>
                <div style="display: flex; gap: 8px; margin-bottom: 12px;">
                  <input type="text" id="quickAddNewName" class="cubo-input" placeholder="New Playlist..." style="flex-grow: 1; padding: 6px 8px; font-size: 12px;">
                  <button id="quickAddNewBtn" class="cubo-btn" style="padding: 6px 12px; font-size: 12px;">Create</button>
                </div>
                <div style="text-align: right;">
                  <button id="quickAddCancelBtn" class="cubo-btn cubo-btn-sec" style="padding: 6px 12px; font-size: 12px;">Cancel</button>
                </div>
              </div>
            </div>
          `;

          this.appendChild(this.musicBar);
          
          const playBtn = this.musicBar.querySelector('#playMusicBtn');
          const stopBtn = this.musicBar.querySelector('#stopMusicBtn');
          const inputUrl = this.musicBar.querySelector('#musicUrl');
          const searchBar = this.musicBar.querySelector('#searchBar');
          const categoryFilter = this.musicBar.querySelector('#categoryFilter');
          const userFilter = this.musicBar.querySelector('#userFilter');
          const songListContainer = this.musicBar.querySelector('#songListContainer');
          
          const toggleAddFormBtn = this.musicBar.querySelector('#toggleAddFormBtn');
          const addForm = this.musicBar.querySelector('#addForm');
          const saveSongBtn = this.musicBar.querySelector('#saveSongBtn');
          
          const playlistsContainer = this.musicBar.querySelector('#playlistsContainer');
          const savePlaylistBtn = this.musicBar.querySelector('#savePlaylistBtn');
          const savePlaylistForm = this.musicBar.querySelector('#savePlaylistForm');
          const newPlaylistName = this.musicBar.querySelector('#newPlaylistName');
          const existingPlaylistSelect = this.musicBar.querySelector('#existingPlaylistSelect');
          const confirmSavePlaylistBtn = this.musicBar.querySelector('#confirmSavePlaylistBtn');
          const cancelSavePlaylistBtn = this.musicBar.querySelector('#cancelSavePlaylistBtn');
          const toggleShuffleBtn = this.musicBar.querySelector('#toggleShuffleBtn');
          const toggleRepeatBtn = this.musicBar.querySelector('#toggleRepeatBtn');
          const toggleCacheBtn = this.musicBar.querySelector('#toggleCacheBtn');
          const clearCacheBtn = this.musicBar.querySelector('#clearCacheBtn');
          const playTimeSelect = this.musicBar.querySelector('#playTimeSelect');

          // The global "Cache YouTube/Spotify Songs" switch entity (entity_id
          // differs between old installs and new ones: cache_youtube_songs vs
          // cache_youtube_spotify_songs — match the common prefix).
          const findCacheSwitch = () => {
            if (!this._hass) return null;
            return Object.keys(this._hass.states).find(
              id => id.startsWith('switch.') && id.includes('cache_youtube')
            ) || null;
          };
          
          const newSongName = this.musicBar.querySelector('#newSongName');
          const newSongUrl = this.musicBar.querySelector('#newSongUrl');
          const newSongCat = this.musicBar.querySelector('#newSongCat');
          const addFormError = this.musicBar.querySelector('#addFormError');

          // Render list of songs based on search and filters
          const renderSongs = () => {
            this._renderSongsFn = renderSongs;
            try {
              const pItemsPerPage = 5;
              const sItemsPerPage = 10;
              
              const query = searchBar.value.toLowerCase();
              const filter = categoryFilter.value;
              const customSongs = loadCustomSongs().filter(s => s);
              const playlists = loadPlaylists();
              
              let filteredPlaylists = playlists;
              const selectedUserFilter = userFilter ? userFilter.value : 'all';
              
              filteredPlaylists = filteredPlaylists.filter(p => {
                const matchesSearch = p.name.toLowerCase().includes(query);
                const matchesUser = selectedUserFilter === 'all' || (p.addedBy || 'System') === selectedUserFilter;
                return matchesSearch && matchesUser;
              });
              
              const totalPlaylistPages = Math.ceil(filteredPlaylists.length / pItemsPerPage) || 1;
              if (this._playlistPage > totalPlaylistPages) this._playlistPage = totalPlaylistPages;
              
              const pStart = (this._playlistPage - 1) * pItemsPerPage;
              const paginatedPlaylists = filteredPlaylists.slice(pStart, pStart + pItemsPerPage);

              let plHtml = filteredPlaylists.length === 0 
                ? `<div style="font-size: 12px; color: var(--secondary-text-color); font-style: italic; padding: 4px;">No saved playlists yet.</div>`
                : paginatedPlaylists.map(pl => `
                  <div class="song-item" style="flex-direction: column; align-items: stretch; padding: 0; background: var(--card-background-color, #fff);">
                    <div style="display: flex; justify-content: space-between; align-items: center; padding: 10px;">
                      <div class="song-info">
                        <div class="song-name" style="font-weight: bold;">${pl.name}</div>
                        <div style="font-size: 11px; color: var(--secondary-text-color);">${pl.songs.length} songs</div>
                        <div style="font-size: 10px; color: var(--secondary-text-color, #727272); font-style: italic; margin-top: 2px;">Added by: ${pl.addedBy || 'System'}</div>
                      </div>
                      <div class="song-actions">
                        <button class="icon-btn play-playlist-btn" data-id="${pl.id}" title="Play playlist">
                          <ha-icon icon="mdi:play" style="--mdc-icon-size: 20px; color: #4caf50;"></ha-icon>
                        </button>
                        <button class="icon-btn edit-playlist-btn" data-id="${pl.id}" title="Edit playlist">
                          <ha-icon icon="mdi:pencil" style="--mdc-icon-size: 20px; color: var(--secondary-text-color, #727272);"></ha-icon>
                        </button>
                        <button class="icon-btn delete-playlist-btn" data-id="${pl.id}" title="Delete playlist">
                          <ha-icon icon="mdi:delete" style="--mdc-icon-size: 20px; color: var(--error-color, #f44336);"></ha-icon>
                        </button>
                      </div>
                    </div>
                    <div id="playlist-edit-${pl.id}" style="display: ${this._expandedPlaylist === pl.id ? 'flex' : 'none'}; flex-direction: column; gap: 4px; padding: 0 10px 10px 10px; border-top: 1px dashed var(--divider-color, rgba(0,0,0,0.05));">
                      ${pl.songs.map((sUrl, idx) => {
                        const sObj = customSongs.find(cs => cs.url === sUrl);
                        const sName = sObj ? sObj.name : 'Unknown Song';
                        return `
                          <div style="display: flex; justify-content: space-between; align-items: center; padding: 4px; font-size: 12px; border-bottom: 1px solid var(--divider-color, rgba(0,0,0,0.03));">
                            <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${sName}</span>
                            <button class="icon-btn remove-from-playlist-btn" data-plid="${pl.id}" data-idx="${idx}" style="padding: 2px;">
                              <ha-icon icon="mdi:close" style="--mdc-icon-size: 16px; color: var(--error-color, #f44336);"></ha-icon>
                            </button>
                          </div>
                        `;
                      }).join('')}
                    </div>
                  </div>
                `).join('');
                
              if (toggleShuffleBtn) {
                toggleShuffleBtn.innerHTML = `<ha-icon icon="mdi:shuffle" style="--mdc-icon-size: 14px; color: ${this._shuffleMode ? '#4caf50' : 'inherit'};"></ha-icon> <span>Shuffle: ${this._shuffleMode ? 'ON' : 'OFF'}</span>`;
              }
              if (toggleRepeatBtn) {
                let rColor = this._repeatMode === 'off' ? 'inherit' : '#4caf50';
                let rIcon = this._repeatMode === 'one' ? 'mdi:repeat-once' : 'mdi:repeat';
                toggleRepeatBtn.innerHTML = `<ha-icon icon="${rIcon}" style="--mdc-icon-size: 14px; color: ${rColor};"></ha-icon> <span>Repeat: ${this._repeatMode.toUpperCase()}</span>`;
              }
              if (toggleCacheBtn) {
                const cacheEnt = findCacheSwitch();
                const cacheOn = cacheEnt && this._hass.states[cacheEnt].state === 'on';
                toggleCacheBtn.style.display = cacheEnt ? 'flex' : 'none';
                toggleCacheBtn.innerHTML = `<ha-icon icon="mdi:download" style="--mdc-icon-size: 14px; color: ${cacheOn ? '#4caf50' : 'inherit'};"></ha-icon> <span>Cache: ${cacheOn ? 'ON' : 'OFF'}</span>`;
                if (clearCacheBtn) clearCacheBtn.style.display = cacheEnt ? 'flex' : 'none';
              }
              
              if (existingPlaylistSelect) {
                const currentVal = existingPlaylistSelect.value;
                existingPlaylistSelect.innerHTML = '<option value="">-- Create New Playlist --</option>' + playlists.map(p => `<option value="${p.id}">${p.name}</option>`).join('');
                if (playlists.some(p => p.id === currentVal)) existingPlaylistSelect.value = currentVal;
              }

              if (playlists.length > pItemsPerPage) {
                plHtml += `
                  <div style="display: flex; justify-content: space-between; align-items: center; padding: 4px; font-size: 12px;">
                    <button class="cubo-btn cubo-btn-sec playlist-prev-btn" style="padding: 2px 6px; ${this._playlistPage === 1 ? 'opacity: 0.5; pointer-events: none;' : ''}">Prev</button>
                    <span>Page ${this._playlistPage} of ${totalPlaylistPages}</span>
                    <button class="cubo-btn cubo-btn-sec playlist-next-btn" style="padding: 2px 6px; ${this._playlistPage === totalPlaylistPages ? 'opacity: 0.5; pointer-events: none;' : ''}">Next</button>
                  </div>
                `;
              }
              playlistsContainer.innerHTML = plHtml;
            
            // Populate User Filter select dropdown dynamically
            const prevSelectedUser = userFilter.value;
            const songUsers = customSongs.map(s => s.addedBy || 'System');
            const playlistUsers = playlists.map(p => p.addedBy || 'System');
            const users = [...new Set([...songUsers, ...playlistUsers])];
            userFilter.innerHTML = '<option value="all">All Users</option>' + users.map(u => `<option value="${u}">${u}</option>`).join('');
            if (users.includes(prevSelectedUser)) {
              userFilter.value = prevSelectedUser;
            } else {
              userFilter.value = 'all';
            }

            // Get dynamic sources from the lullaby media player
            const lullabyState = this._hass && this._lullabyEntityId ? this._hass.states[this._lullabyEntityId] : null;
            const sources = (lullabyState && lullabyState.attributes && lullabyState.attributes.source_list) || [];
            const lullabySongs = sources.map(sourceName => ({
              name: sourceName,
              url: sourceName,
              category: "Lullabies",
              custom: false,
              isLullaby: true
            }));

            // Fallback to default if offline/not loaded
            const actualLullabies = lullabySongs.length > 0 ? lullabySongs : [
              { name: "Camera Lullaby", url: "CuboAI_Lullaby", category: "Lullabies", custom: false, isLullaby: true }
            ];

            // Filter custom songs based on search
            const filteredCustom = customSongs.map((song, index) => ({...song, _originalIndex: index})).filter(song => {
              const matchesSearch = song.name.toLowerCase().includes(query) || song.url.toLowerCase().includes(query);
              let matchesCategory = true;
              if (filter === "youtube") matchesCategory = song.category.toLowerCase() === "youtube";
              else if (filter === "spotify") matchesCategory = song.category.toLowerCase() === "spotify";
              else if (filter === "lullabies") matchesCategory = false; // Lullabies are handled separately below
              else if (filter === "user_added") matchesCategory = song.custom === true;
              
              const selectedUserFilter = userFilter.value;
              const matchesUser = selectedUserFilter === 'all' || (song.addedBy || 'System') === selectedUserFilter;
              
              return matchesSearch && matchesCategory && matchesUser;
            });
            
            const sortFilter = this.musicBar.querySelector('#sortFilter');
            const sortVal = sortFilter ? sortFilter.value : 'newest';
            if (sortVal === 'name_asc') {
              filteredCustom.sort((a, b) => a.name.localeCompare(b.name));
            } else if (sortVal === 'name_desc') {
              filteredCustom.sort((a, b) => b.name.localeCompare(a.name));
            } else if (sortVal === 'oldest') {
              filteredCustom.sort((a, b) => a._originalIndex - b._originalIndex);
            } else {
              // newest
              filteredCustom.sort((a, b) => b._originalIndex - a._originalIndex);
            }

            this._filteredSongs = filteredCustom;

            // Generate HTML for Expandable Lullabies
            let lullabiesHtml = '';
            if (filter === "all" || filter === "lullabies") {
              const matchesLullabySearch = "camera lullabies".includes(query) || "lullabies".includes(query) || actualLullabies.some(s => s.name.toLowerCase().includes(query));
              if (matchesLullabySearch) {
                lullabiesHtml = `
                  <div class="song-item" style="flex-direction: column; align-items: stretch; gap: 0; padding: 0;">
                    <div id="toggleLullabiesBtn" class="song-info" style="cursor: pointer; padding: 10px; display: flex; align-items: center; justify-content: space-between; user-select: none;">
                      <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="song-badge lullabies">Lullabies</span>
                        <div class="song-name" style="font-weight: bold;">Camera Lullabies</div>
                      </div>
                      <ha-icon id="lullabyChevron" icon="${this._lullabiesExpanded ? 'mdi:chevron-up' : 'mdi:chevron-down'}" style="color: var(--secondary-text-color);"></ha-icon>
                    </div>
                    <div id="lullabiesSublist" style="display: ${this._lullabiesExpanded ? 'flex' : 'none'}; flex-direction: column; gap: 4px; padding: 0 10px 10px 10px; border-top: 1px dashed var(--divider-color, rgba(0,0,0,0.05));">
                      ${actualLullabies.map((song) => `
                        <div class="song-item" style="background: var(--card-background-color, #fff); margin-top: 4px; padding: 6px 10px; border: 1px solid var(--divider-color, rgba(0,0,0,0.03));">
                          <div class="song-info">
                            <div class="song-name" style="font-size: 13px;">${song.name}</div>
                          </div>
                          <div class="song-actions">
                            ${this._expandedPlaylist ? `
                              <button class="cubo-btn add-to-active-btn" data-url="${song.url}" style="padding: 2px 8px; font-size: 11px; background: var(--success-color, #28a745);">Add</button>
                            ` : `
                              <button class="icon-btn quick-add-btn" data-url="${song.url}" title="Add to Playlist">
                                <ha-icon icon="mdi:playlist-plus" style="--mdc-icon-size: 20px; color: var(--secondary-text-color, #727272);"></ha-icon>
                              </button>
                            `}
                            <button class="icon-btn play-song-btn" data-url="${song.url}" data-lullaby="true" title="Play song">
                              <ha-icon icon="mdi:play" style="--mdc-icon-size: 20px; color: var(--primary-color, #03a9f4);"></ha-icon>
                            </button>
                          </div>
                        </div>
                      `).join('')}
                    </div>
                  </div>
                `;
              }
            }

            // Render Custom/Added list of songs
            const totalSongPages = Math.ceil(filteredCustom.length / sItemsPerPage) || 1;
            if (this._songPage > totalSongPages) this._songPage = totalSongPages;
            const sStart = (this._songPage - 1) * sItemsPerPage;
            const paginatedCustom = filteredCustom.slice(sStart, sStart + sItemsPerPage);

            let customSongsHtml = paginatedCustom.map((song) => {
              const originalIdx = customSongs.findIndex(s => s.name === song.name && s.url === song.url);
              return `
                <div class="song-item">
                  <div class="song-info" style="align-items: center; gap: 8px;">
                    
                    <span class="song-badge ${song.category.toLowerCase()}">${song.category}</span>
                    <div style="display: flex; flex-direction: column; min-width: 0;">
                      <div class="song-name" title="${song.name}" style="font-weight: 500;">${song.name}</div>
                      <div class="song-url" title="${song.url}" style="font-size: 11px; color: var(--secondary-text-color, #727272); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 250px;">${song.url}</div>
                      <div class="song-added-by" style="font-size: 10px; color: var(--secondary-text-color, #727272); font-style: italic;">Added by: ${song.addedBy || 'System'}</div>
                    </div>
                  </div>
                  <div class="song-actions">
                    ${this._expandedPlaylist ? `
                      <button class="cubo-btn add-to-active-btn" data-url="${song.url}" style="padding: 2px 8px; font-size: 11px; margin-right: 4px; background: var(--success-color, #28a745);">Add</button>
                    ` : `
                      <button class="icon-btn quick-add-btn" data-url="${song.url}" title="Add to Playlist">
                        <ha-icon icon="mdi:playlist-plus" style="--mdc-icon-size: 20px; color: var(--secondary-text-color, #727272);"></ha-icon>
                      </button>
                    `}
                    <button class="icon-btn play-song-btn" data-url="${song.url}" data-lullaby="false" title="Play song">
                      <ha-icon icon="mdi:play" style="--mdc-icon-size: 20px; color: var(--primary-color, #03a9f4);"></ha-icon>
                    </button>
                    <button class="icon-btn edit-song-btn" data-index="${song._originalIndex}" title="Edit song">
                      <ha-icon icon="mdi:pencil" style="--mdc-icon-size: 20px; color: var(--secondary-text-color, #727272);"></ha-icon>
                    </button>
                    <button class="icon-btn delete-song-btn" data-index="${song._originalIndex}" title="Delete song">
                      <ha-icon icon="mdi:delete" style="--mdc-icon-size: 20px; color: var(--error-color, #f44336);"></ha-icon>
                    </button>
                  </div>
                </div>
              `;
            }).join('');

            if (filteredCustom.length > sItemsPerPage) {
              customSongsHtml += `
                <div style="display: flex; justify-content: space-between; align-items: center; padding: 8px 4px; font-size: 12px;">
                  <button class="cubo-btn cubo-btn-sec song-prev-btn" style="padding: 2px 8px; ${this._songPage === 1 ? 'opacity: 0.5; pointer-events: none;' : ''}">Prev</button>
                  <span>Page ${this._songPage} of ${totalSongPages}</span>
                  <button class="cubo-btn cubo-btn-sec song-next-btn" style="padding: 2px 8px; ${this._songPage === totalSongPages ? 'opacity: 0.5; pointer-events: none;' : ''}">Next</button>
                </div>
              `;
            }

            songListContainer.innerHTML = lullabiesHtml + customSongsHtml;

            // Bind Lullabies accordion click
            const toggleLullabiesBtn = songListContainer.querySelector('#toggleLullabiesBtn');
            if (toggleLullabiesBtn) {
              toggleLullabiesBtn.addEventListener('click', () => {
                this._lullabiesExpanded = !this._lullabiesExpanded;
                renderSongs();
              });
            }

            // Bind Quick Add and Add to Active
            songListContainer.querySelectorAll('.add-to-active-btn').forEach(btn => {
              btn.addEventListener('click', () => {
                const url = btn.getAttribute('data-url');
                let playlists = loadPlaylists();
                const pl = playlists.find(p => p.id === this._expandedPlaylist);
                if (pl) {
                  pl.songs.push(url);
                  savePlaylists(playlists);
                }
              });
            });

            songListContainer.querySelectorAll('.quick-add-btn').forEach(btn => {
              btn.addEventListener('click', () => {
                const url = btn.getAttribute('data-url');
                this._quickAddUrl = url;
                
                const playlists = loadPlaylists();
                const listContainer = this.musicBar.querySelector('#quickAddPlaylistList');
                if (listContainer) {
                  listContainer.innerHTML = playlists.length === 0 
                    ? '<div style="padding: 8px; font-size: 11px; font-style: italic; color: #999; text-align: center;">No playlists created yet.</div>'
                    : playlists.map(p => `<div class="quick-add-pl-option" data-id="${p.id}" style="padding: 8px 12px; font-size: 13px; cursor: pointer; border-bottom: 1px solid var(--divider-color, rgba(0,0,0,0.05));">${p.name}</div>`).join('');
                    
                  // Bind clicks to options
                  listContainer.querySelectorAll('.quick-add-pl-option').forEach(opt => {
                    opt.addEventListener('click', () => {
                      const plId = opt.getAttribute('data-id');
                      let pls = loadPlaylists();
                      const pl = pls.find(p => p.id === plId);
                      if (pl) {
                        pl.songs.push(this._quickAddUrl);
                        savePlaylists(pls);
                      }
                      this.musicBar.querySelector('#quickAddModal').style.display = 'none';
                    });
                  });
                }
                
                this.musicBar.querySelector('#quickAddModal').style.display = 'flex';
                this.musicBar.querySelector('#quickAddNewName').value = '';
              });
            });

            const quickAddModal = this.musicBar.querySelector('#quickAddModal');
            if (quickAddModal && !quickAddModal.dataset.bound) {
              quickAddModal.dataset.bound = "true";
              
              this.musicBar.querySelector('#quickAddCancelBtn').addEventListener('click', () => {
                quickAddModal.style.display = 'none';
              });
              
              this.musicBar.querySelector('#quickAddNewBtn').addEventListener('click', () => {
                const name = this.musicBar.querySelector('#quickAddNewName').value.trim();
                if (name && this._quickAddUrl) {
                  let pls = loadPlaylists();
                  pls.push({ id: Date.now().toString(), name: name, songs: [this._quickAddUrl], addedBy: this._hass && this._hass.user ? this._hass.user.name : "System" });
                  savePlaylists(pls);
                  quickAddModal.style.display = 'none';
                }
              });
            }

            // Bind play button listeners
            songListContainer.querySelectorAll('.play-song-btn').forEach(btn => {
              btn.addEventListener('click', (e) => {
                const url = btn.getAttribute('data-url');
                const type = btn.getAttribute('data-type');
                const name = btn.getAttribute('data-name');
                
                if (this._hass) {
                    if (type === 'lullaby') {
                        if (this._lullabyEntityId) {
                            this._hass.callService('media_player', 'select_source', {
                                entity_id: this._lullabyEntityId,
                                source: name
                            });
                        }
                    } else {
                        this._hass.callService('media_player', 'play_media', {
                          entity_id: this._speakerEntityId,
                          media_content_id: url,
                          media_content_type: 'music',
                          enqueue: 'replace'
                        });
                    }
                }
              });
            });



            // Bind edit button listeners
            songListContainer.querySelectorAll('.edit-song-btn').forEach(btn => {
              btn.addEventListener('click', (e) => {
                const idx = parseInt(btn.getAttribute('data-index'));
                const customSongs = loadCustomSongs();
                const song = customSongs[idx];
                if (song) {
                  this._editingIndex = idx;
                  addForm.style.display = 'flex';
                  toggleAddFormBtn.textContent = 'Close';
                  
                  newSongName.value = song.name;
                  newSongUrl.value = song.url;
                  
                  const reverseCatMap = {
                    "YouTube": "youtube",
                    "Spotify": "spotify",
                    "Lullabies": "lullabies"
                  };
                  newSongCat.value = reverseCatMap[song.category] || "youtube";
                  
                  const addFormTitle = this.musicBar.querySelector('#addFormTitle');
                  if (addFormTitle) addFormTitle.textContent = "Edit Song in Library";
                  saveSongBtn.textContent = "Save Changes";
                }
              });
            });

            // Bind delete button listeners
            songListContainer.querySelectorAll('.delete-song-btn').forEach(btn => {
              btn.addEventListener('click', (e) => {
                const idx = parseInt(btn.getAttribute('data-index'));
                let customSongs = loadCustomSongs();
                if (idx >= 0 && idx < customSongs.length) {
                  customSongs.splice(idx, 1);
                  saveCustomSongs(customSongs);
                }
              });
            });

            // Bind playlist action buttons
            playlistsContainer.querySelectorAll('.play-playlist-btn').forEach(btn => {
              btn.addEventListener('click', () => {
                const id = btn.getAttribute('data-id');
                const playlists = loadPlaylists();
                const pl = playlists.find(p => p.id === id);
                if (pl && pl.songs.length > 0) {
                  let songs = [...pl.songs];
                  if (this._shuffleMode) {
                    songs = songs.sort(() => Math.random() - 0.5);
                  }
                  
                  if (this._hass) {
                    songs.forEach((url, index) => {
                      this._hass.callService('media_player', 'play_media', {
                        entity_id: this._speakerEntityId,
                        media_content_id: url,
                        media_content_type: 'music',
                        enqueue: index === 0 ? 'replace' : 'add'
                      });
                    });
                  }
                }
              });
            });

            playlistsContainer.querySelectorAll('.delete-playlist-btn').forEach(btn => {
              btn.addEventListener('click', () => {
                const id = btn.getAttribute('data-id');
                let playlists = loadPlaylists();
                playlists = playlists.filter(p => p.id !== id);
                savePlaylists(playlists);
              });
            });

            playlistsContainer.querySelectorAll('.edit-playlist-btn').forEach(btn => {
              btn.addEventListener('click', () => {
                const id = btn.getAttribute('data-id');
                this._expandedPlaylist = this._expandedPlaylist === id ? null : id;
                renderSongs();
              });
            });

            playlistsContainer.querySelectorAll('.remove-from-playlist-btn').forEach(btn => {
              btn.addEventListener('click', () => {
                const id = btn.getAttribute('data-plid');
                const idx = parseInt(btn.getAttribute('data-idx'));
                let playlists = loadPlaylists();
                const pl = playlists.find(p => p.id === id);
                if (pl) {
                  pl.songs.splice(idx, 1);
                  savePlaylists(playlists);
                }
              });
            });

            const pPrevBtn = playlistsContainer.querySelector('.playlist-prev-btn');
            if (pPrevBtn) pPrevBtn.addEventListener('click', () => { this._playlistPage = Math.max(1, this._playlistPage - 1); renderSongs(); });
            
            const pNextBtn = playlistsContainer.querySelector('.playlist-next-btn');
            if (pNextBtn) pNextBtn.addEventListener('click', () => { this._playlistPage++; renderSongs(); });

            const sPrevBtn = songListContainer.querySelector('.song-prev-btn');
            if (sPrevBtn) sPrevBtn.addEventListener('click', () => { this._songPage = Math.max(1, this._songPage - 1); renderSongs(); });
            
            const sNextBtn = songListContainer.querySelector('.song-next-btn');
            if (sNextBtn) sNextBtn.addEventListener('click', () => { this._songPage++; renderSongs(); });
            } catch (renderErr) {
              console.error(renderErr);
              songListContainer.innerHTML = `<div style="color: red; padding: 10px;">Error rendering songs: ${renderErr.message}</div>`;
            }
          };

          playBtn.addEventListener('click', () => {
             const url = inputUrl.value;
             if (url && this._hass) {
                this._hass.callService('media_player', 'play_media', {
                    entity_id: this._speakerEntityId,
                    media_content_id: url,
                    media_content_type: 'music'
                });
                inputUrl.value = '';
             }
          });
          
          stopBtn.addEventListener('click', () => {
             if (this._hass) {
                 this._hass.callService('media_player', 'media_stop', {
                     entity_id: this._speakerEntityId
                 });
                 if (this._lullabyEntityId) {
                     this._hass.callService('media_player', 'media_stop', {
                         entity_id: this._lullabyEntityId
                     });
                 }
             }
          });

          toggleAddFormBtn.addEventListener('click', () => {
            const isVisible = addForm.style.display === 'flex';
            addForm.style.display = isVisible ? 'none' : 'flex';
            toggleAddFormBtn.textContent = isVisible ? '+ Add Song' : 'Close';
            if (isVisible) {
              this._editingIndex = null;
              newSongName.value = '';
              newSongUrl.value = '';
              const addFormTitle = this.musicBar.querySelector('#addFormTitle');
              if (addFormTitle) addFormTitle.textContent = "Add Song to Library";
              saveSongBtn.textContent = "Add Song";
            }
            if (addFormError) addFormError.style.display = 'none';
          });

          // Removed playAllBtn listener

          saveSongBtn.addEventListener('click', () => {
            const name = newSongName.value.trim();
            const url = newSongUrl.value.trim();
            const cat = newSongCat.value;
            
            if (!name || !url) return;

            if (cat === "youtube" && !url.includes("youtube.com") && !url.includes("youtu.be")) {
              if (addFormError) {
                addFormError.textContent = "Please enter a valid YouTube URL (e.g. containing youtube.com or youtu.be).";
                addFormError.style.display = 'block';
              }
              return;
            }
            
            if (cat === "spotify" && !url.includes("spotify.com")) {
              if (addFormError) {
                addFormError.textContent = "Please enter a valid Spotify URL (e.g. containing spotify.com).";
                addFormError.style.display = 'block';
              }
              return;
            }

            if (addFormError) addFormError.style.display = 'none';

            const categoryMap = {
              youtube: "YouTube",
              spotify: "Spotify",
              lullabies: "Lullabies"
            };

            let customSongs = loadCustomSongs();

            if (this._editingIndex !== undefined && this._editingIndex !== null) {
              if (customSongs[this._editingIndex]) {
                customSongs[this._editingIndex].name = name;
                customSongs[this._editingIndex].url = url;
                customSongs[this._editingIndex].category = categoryMap[cat] || "YouTube";
              }
              this._editingIndex = null;
            } else {
              customSongs.push({
                name: name,
                url: url,
                category: categoryMap[cat] || "YouTube",
                custom: true,
                inPlaylist: false,
                addedBy: this._hass && this._hass.user ? this._hass.user.name : "System"
              });
            }
            
            saveCustomSongs(customSongs);

            newSongName.value = '';
            newSongUrl.value = '';
            addForm.style.display = 'none';
            toggleAddFormBtn.textContent = '+ Add Song';
            
            const addFormTitle = this.musicBar.querySelector('#addFormTitle');
            if (addFormTitle) addFormTitle.textContent = "Add New Song to Library";
            saveSongBtn.textContent = "Save";

            renderSongs();
          });
          // Removed savePlaylistBtn, cancelSavePlaylistBtn, confirmSavePlaylistBtn listeners
          
          if (toggleShuffleBtn) {
            toggleShuffleBtn.addEventListener('click', () => {
              this._shuffleMode = !this._shuffleMode;
              localStorage.setItem(`cuboai_shuffle_${deviceId}`, this._shuffleMode);
              saveSharedSettings({ shuffle: this._shuffleMode });
              renderSongs();
            });
          }

          if (toggleCacheBtn) {
            toggleCacheBtn.addEventListener('click', () => {
              const cacheEnt = findCacheSwitch();
              if (cacheEnt && this._hass) {
                const cacheOn = this._hass.states[cacheEnt].state === 'on';
                this._hass.callService('switch', cacheOn ? 'turn_off' : 'turn_on', { entity_id: cacheEnt });
                // Give HA a moment to process, then refresh the label
                setTimeout(() => { try { renderSongs(); } catch (e) {} }, 600);
              }
            });
          }

          if (clearCacheBtn) {
            clearCacheBtn.addEventListener('click', () => {
              if (this._hass && window.confirm('Delete all locally cached YouTube/Spotify songs?')) {
                this._hass.callService('cuboai', 'clear_youtube_cache', {});
              }
            });
          }
          
          if (toggleRepeatBtn) {
            toggleRepeatBtn.addEventListener('click', () => {
              let newMode = 'off';
              if (this._repeatMode === 'off') newMode = 'all';
              else if (this._repeatMode === 'all') newMode = 'one';
              else newMode = 'off';

              // Optimistic update so the chip flips instantly; the entity
              // round-trip (repeat_set → state → updateMusicStatus) confirms.
              // The entity is the live cross-device authority for repeat and
              // now survives restarts (RestoreEntity on the speaker), so
              // localStorage is only the pre-connect fallback.
              this._repeatMode = newMode;
              localStorage.setItem(`cuboai_repeat_${deviceId}`, newMode);
              try { renderSongs(); } catch (e) {}

              if (this._hass && this._speakerEntityId) {
                this._hass.callService('media_player', 'repeat_set', {
                  entity_id: this._speakerEntityId,
                  repeat: newMode
                });
              }
            });
          }
          
          if (playTimeSelect) {
            playTimeSelect.addEventListener('change', (e) => {
              if (this._hass && this._speakerEntityId) {
                // Entity ids derive from the entity NAME ("{baby} Speaker Play Time"),
                // so build it from the speaker's object id — the old hardcoded
                // "number.cuboai_speaker_timer_<device>" guess never existed.
                const base = this._speakerEntityId.split('.')[1].replace(/_speaker$/, '');
                const minutes = parseInt(e.target.value);
                // Play Time governs card playback (streams AND lullabies played
                // from the card — HA sends the lullaby stop). The separate
                // Lullaby Timer entity stays camera-native for plays from the
                // entity controls / CuboAI app.
                this._hass.callService('number', 'set_value', {
                  entity_id: `number.${base}_speaker_play_time`,
                  value: minutes
                });
              }
            });
          }
          

          const onFilterChange = () => {
            this._songPage = 1;
            renderSongs();
          };
          const sortFilter = this.musicBar.querySelector('#sortFilter');
          searchBar.addEventListener('input', onFilterChange);
          categoryFilter.addEventListener('change', onFilterChange);
          sortFilter.addEventListener('change', onFilterChange);
          userFilter.addEventListener('change', (e) => {
            this._userFilter = e.target.value;
            onFilterChange();
          });
          
          const playlistUserFilter = this.musicBar.querySelector('#playlistUserFilter');
          if (playlistUserFilter) {
            playlistUserFilter.addEventListener('change', (e) => {
              this._playlistUserFilter = e.target.value;
              this._playlistPage = 1;
              renderSongs();
            });
          }

          // Initial Render
          renderSongs();
        }
        
        // Use an interval to ensure the button stays attached even if the child re-renders
        if (!this.attachInterval) {
          this.attachInterval = setInterval(() => {
            cuboaiStopReplaced(this, Date.now());
            cuboaiRevivePlayer(this, Date.now());
            // Penetrate Shadow DOM if it exists
            const root = this.content.shadowRoot || this.content;
            const player = root.querySelector('.player') || root.querySelector('.card') || root;
            const video = root.querySelector('video');
            const audio = root.querySelector('audio');
            const volumeIcon = root.querySelector('.volume');

            // Speaker button visibility. webrtc-camera creates it display:none
            // and only reveals it after audio is detected on each (re)connect —
            // that's the "button blinks out / missing for a second" on MSE, so
            // on desktop/Android we pin it ALWAYS visible. Apple WebKit (iOS —
            // every iOS browser) keeps STOCK webrtc-camera behaviour (as in
            // v2.3.x): the icon appears once audio is detected and the user
            // taps it to unmute — forcing it visible there covered the native
            // player controls, and hiding it removed the unmute button.
            if (!this._cuboVolumeStyleDone && root.querySelector('.controls')
                && !(navigator.vendor && navigator.vendor.includes('Apple'))) {
              this._cuboVolumeStyleDone = true;
              const st = document.createElement('style');
              st.textContent = '.controls .volume { display: block !important; }';
              (root.querySelector('.card') || this.content).appendChild(st);
            }
            // video-rtc force-mutes on ANY play() rejection (its autoplay
            // fallback) — including harmless AbortErrors from MSE source
            // reloads. That's the "starts unmuted, flips to mute after a few
            // seconds" bug. If the video got muted and neither we nor the
            // user asked for it, undo it. Retries are capped: when the
            // browser GENUINELY blocks sound, unmuting pauses playback, so
            // after 3 failed attempts we accept muted and wait for the first
            // user interaction instead of fighting a losing battle.
            if (video && video.muted && !this.isMuted && !this._userMutedThisSession
                && !this._soundNeedsGesture
                && !(navigator.vendor && navigator.vendor.includes('Apple'))) {
              if (!video.paused && (this._reUnmuteAttempts || 0) < 3) {
                this._reUnmuteAttempts = (this._reUnmuteAttempts || 0) + 1;
                video.muted = false;
                // Chrome punishes a gesture-less unmute by PAUSING the video
                // ("Unmuting failed and the element was paused instead").
                // Detect that right away, revert to muted playback so the
                // stream doesn't freeze, and stop attempting until a gesture.
                setTimeout(() => {
                  if (video.paused) {
                    this._soundNeedsGesture = true;
                    this.isMuted = true;
                    video.muted = true;
                    video.play().catch(() => {});
                    if (this._armGestureUnmute) this._armGestureUnmute();
                  }
                }, 80);
              } else {
                this.isMuted = true; // browser insists — wait for a gesture
                if (this._armGestureUnmute) this._armGestureUnmute();
              }
            } else if (video && !video.muted) {
              this._reUnmuteAttempts = 0;
            }
            // Keep the icon truthful (mute state can change before the
            // player's own volumechange listener is wired up).
            if (video && volumeIcon) {
              const wantIcon = video.muted ? 'mdi:volume-mute' : 'mdi:volume-high';
              if (volumeIcon.icon !== wantIcon) volumeIcon.icon = wantIcon;
            }

            if (!this.micButton.isConnected || (player && !player.contains(this.micButton))) {
              if (player) player.appendChild(this.micButton);
              else root.appendChild(this.micButton);
            }
            
            if (this.bpmOverlay && (!this.bpmOverlay.isConnected || (player && !player.contains(this.bpmOverlay)))) {
              if (player) player.appendChild(this.bpmOverlay);
              else root.appendChild(this.bpmOverlay);
            }
            
            if (this.envOverlay && (!this.envOverlay.isConnected || (player && !player.contains(this.envOverlay)))) {
              if (player) player.appendChild(this.envOverlay);
              else root.appendChild(this.envOverlay);
            }

            if (this.tsOverlay && (!this.tsOverlay.isConnected || (player && !player.contains(this.tsOverlay)))) {
              if (player) player.appendChild(this.tsOverlay);
              else root.appendChild(this.tsOverlay);
            }
            
            if ((video || audio) && volumeIcon) {
              // Ensure the media matches our memory when it first loads
              if (video && !video.dataset.cuboInit) {
                video.dataset.cuboInit = "true";

                // Keep sound and picture going on MSE, the path a phone takes
                // away from home (mobile data through the tunnel): the card
                // owns the playback speed and stops video-rtc's 5 s trim from
                // deleting the frames being played. See cuboaiSteerMsePlayback.
                cuboaiSteerMsePlayback(video);
                // Keep the last picture over the black gap while the player
                // changes stream (Wi-Fi <-> mobile data), and hand it over to a
                // card Home Assistant rebuilt for the same camera.
                cuboaiBridgeGap(video, this._liveDeviceId);

                // Show the full camera frame instead of cropping/zooming in.
                // The inner <video> otherwise crops the (near-square) CuboAI
                // frame to the card's aspect ratio and looks "zoomed in".
                video.style.setProperty('object-fit', 'contain', 'important');

                // Apply the desired audio state to this (possibly re-created)
                // video. When the setting wants sound we TRY unmuted playback
                // first — browsers allow unmuted autoplay on frequently-visited
                // sites — and only fall back to muted + unmute-on-first-
                // interaction when the browser blocks it. An explicit mute by
                // the user always wins (_userMutedThisSession).
                // Apple WebKit (iOS) is excluded: the video plays with NATIVE
                // player controls there, mute/unmute belongs to the native
                // speaker button, and scripted mute changes fight the user.
                const isAppleAudio = navigator.vendor && navigator.vendor.includes('Apple');
                const armGestureUnmute = () => {
                  if (this._autoUnmuteArmed) return;
                  this._autoUnmuteArmed = true;
                  const doAutoUnmute = (e) => {
                    // Not while a talk holds the sound off; stays armed for
                    // the first tap after it.
                    if (this._mic && this._mic.talkMute) return;
                    window.removeEventListener('pointerdown', doAutoUnmute, true);
                    this._autoUnmuteArmed = false;
                    if (this._userMutedThisSession) return;
                    // Let a click on the player's own volume button be handled
                    // by it alone — no double-toggle.
                    const path = (e && e.composedPath) ? e.composedPath() : [];
                    if (path.some(el => el && el.classList && el.classList.contains('volume'))) return;
                    const r = this.content?.shadowRoot || this.content;
                    const vv = r?.querySelector('video');
                    const aa = r?.querySelector('audio');
                    this.isMuted = false;
                    this._reUnmuteAttempts = 0; // gesture given — watchdog may guard again
                    this._soundNeedsGesture = false;
                    if (vv) {
                      vv.muted = false;
                      // Chrome may have PAUSED the video when a gesture-less
                      // unmute was attempted — resume it now that we have one.
                      if (vv.paused) vv.play().catch(() => {});
                    }
                    if (aa) aa.muted = false;
                  };
                  window.addEventListener('pointerdown', doAutoUnmute, true);
                };
                // Expose to the watchdog interval (it gives up re-unmuting
                // after repeated browser blocks and needs to arm this).
                this._armGestureUnmute = armGestureUnmute;

                // Only attempt an immediate unmute if the user has ALREADY
                // interacted with the page (userActivation). A gesture-less
                // unmute makes Chrome pause the video and log "Unmuting failed
                // and the element was paused" — so when there's been no
                // interaction yet we simply stay muted and unmute on the first
                // tap (armGestureUnmute), which is silent and clean.
                const hasInteracted = !!(navigator.userActivation && navigator.userActivation.hasBeenActive);
                if (!isAppleAudio && this._wantUnmuted && !this._userMutedThisSession && !this._soundNeedsGesture
                    && !(this._mic && this._mic.talkMute)) {
                  if (hasInteracted) {
                    video.muted = false;
                    this.isMuted = false;
                    const p = video.play();
                    if (p && p.catch) {
                      p.catch((err) => {
                        // Only a real autoplay-policy block means "the browser
                        // refuses sound". MSE reloads reject pending play()
                        // calls with AbortError ("interrupted by a new load
                        // request") — muting on those would silence the card a
                        // few seconds after it correctly started unmuted.
                        if (err && err.name !== 'NotAllowedError') return;
                        this._soundNeedsGesture = true;
                        this.isMuted = true;
                        video.muted = true;
                        video.play().catch(() => {});
                        armGestureUnmute();
                      });
                    }
                  } else {
                    // No interaction yet — stay muted (clean autoplay) and wait
                    // for the first gesture to bring the sound up.
                    video.muted = true;
                    this.isMuted = true;
                    this._soundNeedsGesture = true;
                    armGestureUnmute();
                  }
                } else if (!isAppleAudio) {
                  video.muted = this.isMuted;
                }
                this._reUnmuteAttempts = 0;

                // Apple devices (iOS/Safari) use strict native media players and break if patched.
                // We reliably detect Apple engines by checking the vendor string.
                const isAppleWebKit = navigator.vendor && navigator.vendor.includes('Apple');

                // Android Chrome hardware-decodes the WebRTC/MSE video, and hardware
                // frames can't be read back into a <canvas> — so the canvas-overlay PiP
                // technique below produces a black/empty picture and requestPictureInPicture
                // rejects ("open video minimized not working", issue #87). On Android we
                // therefore skip the canvas patch and let the browser's NATIVE PiP run on
                // the real video element (it works; it just lacks the drawn BPM/temp overlays).
                const isAndroid = /android/i.test(navigator.userAgent || '');

                // Fullscreen Patch: redirect video fullscreen to the player container
                if (!isAppleWebKit) {
                    const originalFs = video.requestFullscreen || video.webkitRequestFullscreen;
                    if (originalFs) {
                       video.requestFullscreen = function(options) {
                          if (player && player.requestFullscreen) return player.requestFullscreen(options);
                          if (player && player.webkitRequestFullscreen) return player.webkitRequestFullscreen(options);
                          return originalFs.call(video, options);
                       };
                       if (video.webkitRequestFullscreen) video.webkitRequestFullscreen = video.requestFullscreen;
                    }
                }


                // PiP Patch: Canvas stream overlay technique (desktop Chrome only —
                // Apple uses native PiP, and Android can't read HW-decoded frames into
                // a canvas so it also uses native PiP; see isAndroid note above, #87).
                if (!isAppleWebKit && !isAndroid) {
                    const originalPip = video.requestPictureInPicture;
                    if (originalPip) {
                       video.crossOrigin = "anonymous";
                   const self = this;
                   
                   const setupPip = () => {
                       if (video._pipVideo) return;
                       
                       const cvs = document.createElement('canvas');
                       cvs.width = video.videoWidth || 1920;
                       cvs.height = video.videoHeight || 1080;
                       const ctx = cvs.getContext('2d');
                       
                       const pipVideo = document.createElement('video');
                       pipVideo.muted = true;
                       pipVideo.autoplay = true;
                       
                       const stream = cvs.captureStream(30);
                       pipVideo.srcObject = stream;
                       pipVideo.style.position = 'absolute';
                       pipVideo.style.width = '1px';
                       pipVideo.style.height = '1px';
                       pipVideo.style.opacity = '0.01';
                       pipVideo.style.pointerEvents = 'none';
                       self.appendChild(pipVideo);
                       
                       pipVideo.addEventListener('volumechange', () => {
                           if (audio) {
                               audio.muted = pipVideo.muted;
                               self.isMuted = audio.muted;
                               if (volumeIcon) {
                                   volumeIcon.icon = self.isMuted ? 'mdi:volume-mute' : 'mdi:volume-high';
                               }
                           }
                       });
                       
                       video._pipVideo = pipVideo;
                       video._pipCanvas = cvs;
                       video._pipCtx = ctx;
                       
                       // Keep the canvas stream alive in the background at 1fps
                       // This ensures pipVideo successfully loads its metadata
                       // so requestPictureInPicture doesn't reject synchronously.
                       setInterval(() => {
                           if (!video._pipActive && video.videoWidth > 0) {
                               if (cvs.width !== video.videoWidth || cvs.height !== video.videoHeight) {
                                   cvs.width = video.videoWidth;
                                   cvs.height = video.videoHeight;
                               }
                               ctx.drawImage(video, 0, 0, cvs.width, cvs.height);
                           }
                       }, 1000);
                       
                       pipVideo.addEventListener('leavepictureinpicture', () => {
                          video._pipActive = false;
                       });
                       
                       pipVideo.play().catch(e => console.error("PiP background play failed", e));
                   };
                   
                   setupPip();
                   video.addEventListener('playing', setupPip);
                   if (audio) audio.addEventListener('playing', setupPip);
                   
                   video.requestPictureInPicture = function() {
                      if (!video._pipVideo) {
                          setupPip();
                      }
                      
                      // Ensure audio track is attached dynamically
                      if (audio && audio.srcObject && video._pipVideo.srcObject) {
                          const audioTracks = audio.srcObject.getAudioTracks();
                          const existingTracks = video._pipVideo.srcObject.getAudioTracks();
                          if (audioTracks.length > 0 && existingTracks.length === 0) {
                              video._pipVideo.srcObject.addTrack(audioTracks[0]);
                          }
                      }
                      
                      video._pipActive = true;
                      
                      const drawFrame = () => {
                          if (!video._pipActive) return;
                          const cvs = video._pipCanvas;
                          const ctx = video._pipCtx;
                          if (video.videoWidth > 0 && video.videoHeight > 0) {
                              if (cvs.width !== video.videoWidth || cvs.height !== video.videoHeight) {
                                  cvs.width = video.videoWidth;
                                  cvs.height = video.videoHeight;
                              }
                              ctx.drawImage(video, 0, 0, cvs.width, cvs.height);
                              
                              // Draw Overlays
                              const drawIcon = (pathData, color, x, y, size) => {
                                  ctx.save();
                                  ctx.translate(x, y);
                                  const scale = size / 24;
                                  ctx.scale(scale, scale);
                                  ctx.fillStyle = color;
                                  ctx.fill(new Path2D(pathData));
                                  ctx.restore();
                              };

                              const drawPill = (text, iconPath, iconColor, x, y, size) => {
                                  ctx.font = `bold ${size}px Arial`;
                                  const textWidth = ctx.measureText(text).width;
                                  const padding = size * 0.5;
                                  const iconSize = size * 1.2;
                                  const gap = size * 0.3;
                                  const width = padding + iconSize + gap + textWidth + padding;
                                  const height = size * 1.8;
                                  const radius = height / 2;
                                  
                                  // Draw background
                                  ctx.fillStyle = "rgba(0, 0, 0, 0.6)";
                                  ctx.beginPath();
                                  ctx.roundRect(x, y - height/2, width, height, radius);
                                  ctx.fill();
                                  
                                  // Draw icon
                                  drawIcon(iconPath, iconColor, x + padding, y - iconSize/2, iconSize);
                                  
                                  // Draw text
                                  ctx.fillStyle = "white";
                                  ctx.textAlign = "left";
                                  ctx.textBaseline = "middle";
                                  ctx.fillText(text, x + padding + iconSize + gap, y);
                                  
                                  return width;
                              };
                              
                              const pathHeartPulse = "M7.5,4A5.5,5.5 0 0,0 2,9.5C2,10 2.09,10.5 2.22,11H6.3L7.57,7.63C7.87,6.83 9.05,6.75 9.43,7.63L11.5,13L12.09,11.58C12.22,11.25 12.57,11 13,11H21.78C21.91,10.5 22,10 22,9.5A5.5,5.5 0 0,0 16.5,4C14.64,4 13,4.93 12,6.34C11,4.93 9.36,4 7.5,4V4M3,12.5A1,1 0 0,0 2,13.5A1,1 0 0,0 3,14.5H5.44L11,20C12,20.9 12,20.9 13,20L18.56,14.5H21A1,1 0 0,0 22,13.5A1,1 0 0,0 21,12.5H13.4L12.47,14.8C12.07,15.81 10.92,15.67 10.55,14.83L8.5,9.5L7.54,11.83C7.39,12.21 7.05,12.5 6.6,12.5H3Z";
                              const pathThermometer = "M15 13V5A3 3 0 0 0 9 5V13A5 5 0 1 0 15 13M12 4A1 1 0 0 1 13 5V8H11V5A1 1 0 0 1 12 4Z";
                              const pathWaterPercent = "M12,3.25C12,3.25 6,10 6,14C6,17.32 8.69,20 12,20A6,6 0 0,0 18,14C18,10 12,3.25 12,3.25M14.47,9.97L15.53,11.03L9.53,17.03L8.47,15.97M9.75,10A1.25,1.25 0 0,1 11,11.25A1.25,1.25 0 0,1 9.75,12.5A1.25,1.25 0 0,1 8.5,11.25A1.25,1.25 0 0,1 9.75,10M14.25,14.5A1.25,1.25 0 0,1 15.5,15.75A1.25,1.25 0 0,1 14.25,17A1.25,1.25 0 0,1 13,15.75A1.25,1.25 0 0,1 14.25,14.5Z";
                              
                              const bpm = self._currentBpmText;
                              if (bpm) {
                                  const textWidth = ctx.measureText(bpm).width;
                                  const size = 48;
                                  const totalWidth = size * 0.5 + size * 1.2 + size * 0.3 + textWidth + size * 0.5;
                                  drawPill(bpm, pathHeartPulse, "#f44336", cvs.width / 2 - totalWidth / 2, 80, size);
                              }
                              
                              const temp = self._currentTempText;
                              const humi = self._currentHumiText;
                              if (temp || humi) {
                                  let currentX = 40;
                                  const ey = cvs.height - 60;
                                  const gap = 20;
                                  
                                  if (temp) {
                                      currentX += drawPill(temp, pathThermometer, "#ff9800", currentX, ey, 48) + gap;
                                  }
                                  if (humi) {
                                      drawPill(humi, pathWaterPercent, "#03a9f4", currentX, ey, 48);
                                  }
                              }
                          }
                          
                          requestAnimationFrame(drawFrame);
                      };
                      
                      drawFrame();
                      
                      // Synchronously request PiP to preserve user gesture
                      return video._pipVideo.requestPictureInPicture().catch(e => {
                          video._pipActive = false;
                          console.error("Custom PiP failed, falling back to standard PiP:", e);
                          return originalPip.call(video);
                      });
                   };
                }

              }
            } // ADDED MISSING BRACKET FOR if (video && !video.dataset.cuboInit)
            
            if (audio && !audio.dataset.cuboInit) {
              audio.dataset.cuboInit = "true";
              }
              if (!volumeIcon.dataset.cuboInit) {
                volumeIcon.dataset.cuboInit = "true";
                volumeIcon.icon = this.isMuted ? 'mdi:volume-mute' : 'mdi:volume-high';
              }
              
              if (!volumeIcon.dataset.cuboHooked) {
                volumeIcon.dataset.cuboHooked = "true";
                volumeIcon.addEventListener('click', () => {
                  // A speaker tap during a talk that muted the room is the
                  // user's own choice: the end of the talk leaves it alone.
                  if (this._mic && this._mic.talkMute) this._mic.talkMute.touched = true;
                  setTimeout(() => {
                    this.isMuted = video ? video.muted : (audio ? audio.muted : false);
                    // The user's explicit choice wins: once they mute, stop
                    // auto-unmuting for the rest of this page view (and resume
                    // if they unmute again themselves).
                    this._userMutedThisSession = this.isMuted;
                    if (audio) audio.muted = this.isMuted;
                    localStorage.setItem(`cuboai_muted_${deviceId}`, this.isMuted ? 'true' : 'false');
                    // Sync mute across devices (respected only in 'remember' mode)
                    if ((this._config?.default_mute_state || 'remember') === 'remember') {
                      this._setSharedSetting(deviceId, { muted: this.isMuted });
                    }
                  }, 100);
                });
              }
            }
          }, 500);
        }
      });
    } else {
      // If content already exists, just update hass
      if (this.content.setConfig && this._hass) {
        this.content.hass = this._hass;
      }
      // Built once, so a re-render that detached the bar would lose it
      // permanently; put it back instead.
      if (this.dvrBar && !this.dvrBar.isConnected) this.appendChild(this.dvrBar);
    }

      let tempState = null;
      let humiState = null;
      let bpmState = null;
      let babyName = null;
      
      if (this._speakerEntityId) {
          const nameParts = this._speakerEntityId.replace('media_player.', '').replace('_speaker', '').split('_');
          babyName = nameParts[nameParts.length - 1]; // e.g. "nursery"
      }

      for (const entity_id in hass.states) {
          if (entity_id.startsWith('sensor.cuboai_') && !entity_id.includes('alert')) {
              if (babyName && !entity_id.includes(babyName)) continue;
              if (entity_id.includes('temperature') && !entity_id.includes('thermometer')) tempState = hass.states[entity_id];
              else if (entity_id.includes('humidity')) humiState = hass.states[entity_id];
              else if (entity_id.includes('mat_bpm')) bpmState = hass.states[entity_id];
          }
      }

      // A sensor that is missing/unknown/unavailable renders NOTHING, not "??":
      // the mat and thermometer are sold separately, so on many cameras these
      // entities simply do not exist and a permanent "?? BPM" floating on the
      // video is noise (issue #100). The config flags are a hard off switch on
      // top of that auto-hide (show_mat_overlay / show_env_overlay, default on).
      const _live = (sens) => sens && sens.state !== 'unknown' && sens.state !== 'unavailable';

      if (this.bpmOverlay) {
        const wanted = !this._config || this._config.show_mat_overlay !== false;
        if (wanted && _live(bpmState)) {
          const parsed = parseFloat(bpmState.state);
          const bpmText = !isNaN(parsed) ? Math.round(parsed) : bpmState.state;
          this.bpmOverlay.innerHTML = `<ha-icon icon="mdi:heart-pulse" style="margin-right: 4px; color: #ff4a4a; --mdc-icon-size: 18px;"></ha-icon>${bpmText} BPM`;
          this.bpmOverlay.style.display = 'flex';
          this._currentBpmText = bpmText + " BPM";
        } else {
          this.bpmOverlay.style.display = 'none';
          this._currentBpmText = "";
        }
      }

      if (this.envOverlay) {
        const wanted = !this._config || this._config.show_env_overlay !== false;
        let envHtml = '';
        this._currentTempText = "";
        this._currentHumiText = "";
        if (wanted && _live(tempState)) {
            const parsed = parseFloat(tempState.state);
            const tempText = !isNaN(parsed) ? Math.round(parsed) : tempState.state;
            let tempUnit = "°C";
            if (tempState.attributes.unit_of_measurement) {
                tempUnit = tempState.attributes.unit_of_measurement.replace(/[^A-Za-z0-9°CF]/g, '');
            }
            envHtml += `<span style="display:flex;align-items:center;"><ha-icon icon="mdi:thermometer" style="margin-right: 2px; color: #ff9800; --mdc-icon-size: 18px;"></ha-icon>${tempText}${tempUnit}</span>`;
            this._currentTempText = tempText + tempUnit;
        }
        if (wanted && _live(humiState)) {
            const parsed = parseFloat(humiState.state);
            const humiText = !isNaN(parsed) ? Math.round(parsed) : humiState.state;
            const humiUnit = (humiState.attributes.unit_of_measurement) || "%";
            envHtml += `<span style="display:flex;align-items:center;"><ha-icon icon="mdi:water-percent" style="margin-right: 2px; color: #03a9f4; --mdc-icon-size: 18px;"></ha-icon>${humiText}${humiUnit}</span>`;
            this._currentHumiText = humiText + humiUnit;
        }
        if (envHtml) {
          this.envOverlay.innerHTML = envHtml;
          this.envOverlay.style.display = 'flex';
        } else {
          this.envOverlay.style.display = 'none';
        }
      }

      if (!this._initialized) {
        this._initialized = true;
        this.updateMusicStatus(hass, this._speakerEntityId, this._lullabyEntityId);
      }

      // Check if media library updated to automatically refresh the song list
      let currentLibraryStateObj = null;
      for (const key in hass.states) {
        if (key.startsWith('sensor.cuboai_media_library')) {
          currentLibraryStateObj = hass.states[key];
          break;
        }
      }
      const newLibraryStateStr = currentLibraryStateObj ? JSON.stringify(currentLibraryStateObj.attributes) : null;
      if (this._lastLibraryStateStr !== newLibraryStateStr) {
        this._lastLibraryStateStr = newLibraryStateStr;
        // Adopt shuffle changes made on other devices (skipped for a few
        // seconds after a local toggle so our own optimistic state wins)
        try {
          if (currentLibraryStateObj && this._deviceId &&
              (!this._settingsWriteTs || Date.now() - this._settingsWriteTs > 3000)) {
            const shared = (currentLibraryStateObj.attributes.settings || {})[this._deviceId] || {};
            if (shared.shuffle !== undefined) {
              this._shuffleMode = !!shared.shuffle;
              // Keep the pre-sync fallback fresh so a cold page load (before
              // the library sensor arrives) starts from the synced value.
              try { localStorage.setItem(`cuboai_shuffle_${this._deviceId}`, this._shuffleMode); } catch (e) {}
            }
            // Adopt a mute change from another device (only in 'remember' mode).
            if (shared.muted !== undefined && (this._config?.default_mute_state || 'remember') === 'remember') {
              const wantMuted = !!shared.muted;
              const talkMute = this._mic && this._mic.talkMute;
              if (talkMute) {
                // Muted for a talk (talk_mutes_speaker): the other device's
                // choice is what the end of the talk brings back.
                talkMute.wasMuted = wantMuted;
                talkMute.wasIsMuted = wantMuted;
              } else if (wantMuted !== this.isMuted) {
                this.isMuted = wantMuted;
                const root = this.content?.shadowRoot || this.content;
                if (root) {
                  const v = root.querySelector('video');
                  const a = root.querySelector('audio');
                  const vi = root.querySelector('.volume');
                  if (v) v.muted = wantMuted;
                  if (a) a.muted = wantMuted;
                  if (vi) vi.icon = wantMuted ? 'mdi:volume-mute' : 'mdi:volume-high';
                }
              }
            }
          }
        } catch (e) {}
        if (this.musicBar && typeof this._renderSongsFn === 'function') {
           this._renderSongsFn();
        }
      }

      if (this.musicBar && this._speakerEntityId) {
        this.updateMusicStatus(hass, this._speakerEntityId, this._lullabyEntityId);
      }
    } catch (err) {
      console.error(err);
      this.innerHTML = `<div style="background: #fee; border: 1px solid #fcc; color: #c00; padding: 15px; border-radius: 8px;"><h3>CuboAI Card Error</h3><p>${err.message}</p><pre style="overflow: auto; max-height: 150px;">${err.stack}</pre></div>`;
    }
  }

  setConfig(config) {
    cuboaiRegisterCard(this);
    try {
      if (!config) {
        throw new Error("Invalid configuration (config is undefined)");
      }
      this._config = config;
      if (this._userFilter === undefined) {
        this._userFilter = config.default_song_filter || 'all';
      }
      if (this._playlistUserFilter === undefined) {
        this._playlistUserFilter = config.default_playlist_filter || 'all';
      }
      if (this.config && this.config.device_id !== config.device_id) {
         // Config changed via editor, update child
         if (this.content && config.device_id) {
           const found = cuboaiFindCameraState(this._hass, config.device_id);
           if (found) {
             const webrtcConfig = cuboaiWebrtcConfig(found, this.isMuted);
             customElements.whenDefined('webrtc-camera').then(() => {
               // Not while a recording is playing: this fires on any config
               // touch and would drop the user back to live mid-scrub.
               if (!this._dvrPlaying) this.content.setConfig(webrtcConfig);
             });
           }
         } else if (this.content && !config.device_id) {
             // Fallback to auto-detect. The old scan looked for
             // `media_player.cuboai_speaker_<id>`, but media_player.py names the
             // entity "<baby> Speaker" so the id is `media_player.<baby>_speaker`
             // — deviceId was therefore always null and this branch was dead
             // code (#89). Use the same attribute scan the render path uses.
             let deviceId = null;
             if (this._hass && this._hass.states) {
               for (const entity_id in this._hass.states) {
                 if (entity_id.startsWith('media_player.') && entity_id.endsWith('_speaker')) {
                   const attrs = this._hass.states[entity_id].attributes || {};
                   if (attrs.device_id) {
                     deviceId = attrs.device_id;
                     break;
                   }
                 }
               }
             }
             // A null deviceId is fine — the matcher falls back to the sole
             // CuboAI camera when there is exactly one.
             const found = cuboaiFindCameraState(this._hass, deviceId);
             if (found) {
               const webrtcConfig = cuboaiWebrtcConfig(found, this.isMuted);
               customElements.whenDefined('webrtc-camera').then(() => {
                 // Not while a recording is playing: this fires on any config
                 // touch and would drop the user back to live mid-scrub.
                 if (!this._dvrPlaying) {
                   this.content.setConfig(webrtcConfig);
                   this._liveDeviceId = cuboaiCameraDeviceId(found);   // the mic follows the picture
                 }
               });
             }
         }
      }
      this.config = config;
    } catch (err) {
      console.error("CuboAI Card setConfig Error:", err);
      this._error = err;
    }
  }

  updateMusicStatus(hass, speakerEntityId, lullabyEntityId) {
    if (!this.musicBar) return;
    if (this._config && this._config.show_music === false) return;  // hidden section (issue #100)
    const speakerState = hass.states[speakerEntityId];
    const lullabyState = lullabyEntityId ? hass.states[lullabyEntityId] : null;
    
    if (speakerState && speakerState.attributes) {
      const haRepeat = speakerState.attributes.repeat;
      if (haRepeat !== undefined && this._repeatMode !== haRepeat) {
        this._repeatMode = haRepeat;
        const toggleRepeatBtn = this.musicBar ? this.musicBar.querySelector('#toggleRepeatBtn') : null;
        if (toggleRepeatBtn) {
          let rColor = this._repeatMode === 'off' ? 'inherit' : '#4caf50';
          let rIcon = this._repeatMode === 'one' ? 'mdi:repeat-once' : 'mdi:repeat';
          toggleRepeatBtn.innerHTML = `<ha-icon icon="${rIcon}" style="--mdc-icon-size: 14px; color: ${rColor};"></ha-icon> <span>Repeat: ${this._repeatMode.toUpperCase()}</span>`;
        }
      }
    }
    
    if (speakerEntityId) {
      // Derive from the speaker's object id — entity ids come from entity names
      const base = speakerEntityId.split('.')[1].replace(/_speaker$/, '');
      const timerState = hass.states[`number.${base}_speaker_play_time`];
      if (timerState && this.musicBar) {
        const playTimeSelect = this.musicBar.querySelector('#playTimeSelect');
        if (playTimeSelect && playTimeSelect.value !== timerState.state) {
          playTimeSelect.value = timerState.state;
        }
      }
    }
    if (!speakerState) return;

    let activeState = speakerState.state;
    let activeAttributes = speakerState.attributes || {};
    let isLullabyPlaying = false;

    if (lullabyState && lullabyState.state === 'playing') {
      activeState = 'playing';
      activeAttributes = lullabyState.attributes || {};
      isLullabyPlaying = true;
    }
    
    const nowPlayingDiv = this.musicBar.querySelector('#nowPlaying');
    const nowPlayingText = this.musicBar.querySelector('#nowPlayingText');
    
    if (nowPlayingDiv && nowPlayingText) {
      if (activeState === 'playing') {
        let title = 'Unknown Song';
        if (isLullabyPlaying) {
          title = lullabyState.attributes.source || 'Lullaby';
        } else {
          const activeUrl = activeAttributes.media_content_id || activeAttributes.media_title;
          if (activeUrl) {
            let customSongs = [];
            try {
              let libraryState = null;
              if (hass.states) {
                for (const key in hass.states) {
                  if (key.startsWith('sensor.cuboai_media_library')) {
                    libraryState = hass.states[key];
                    break;
                  }
                }
              }
              if (libraryState && libraryState.attributes && libraryState.attributes.custom_songs) {
                customSongs = libraryState.attributes.custom_songs;
              }
              if (customSongs.length === 0) {
                // device_id comes from the speaker's attributes — parsing the
                // entity_id ("...".split('_')[2]) just returns "speaker".
                const deviceId = (speakerState.attributes || {}).device_id;
                if (deviceId) {
                  customSongs = JSON.parse(localStorage.getItem(`cuboai_custom_songs_${deviceId}`)) || [];
                }
              }
            } catch(e) {}
            const song = customSongs.find(s => {
              if (s.url === activeUrl) return true;
              
              const getVid = (u) => {
                try {
                  const m = u.match(/(?:v=|\/)([0-9A-Za-z_-]{11})(?:&|\?|$)/);
                  return m ? m[1] : u;
                } catch(e) { return u; }
              };
              
              const vidS = getVid(s.url);
              const vidA = getVid(activeUrl);
              
              if (vidS.length === 11 && vidS === vidA) return true;
              
              return activeUrl.includes(s.url) || s.url.includes(activeUrl);
            });
            if (song) title = song.name;
            else if (activeAttributes.media_title && activeAttributes.media_title !== activeAttributes.media_content_id) title = activeAttributes.media_title;
            else title = activeUrl;
          } else {
            title = activeAttributes.media_title || 'Unknown Song';
          }
        }
        const artist = isLullabyPlaying ? 'CuboAI Lullaby' : (activeAttributes.media_artist || '');
        nowPlayingDiv.style.display = 'flex';
        nowPlayingText.textContent = `Now Playing: ${title}${artist ? ` - ${artist}` : ''}`;
      } else {
        nowPlayingDiv.style.display = 'none';
        
        // Reset playlist button if not playing anymore
        if (this._playlistActive && !this._isAdvancing) {
          this._playlistActive = false;
          this._currentPlaylist = [];
          this._queueIndex = -1;
          const playAllBtn = this.musicBar.querySelector('#playAllBtn');
          if (playAllBtn) {
            playAllBtn.textContent = 'Play';
            playAllBtn.style.background = '#4caf50';
          }
        }
      }
    }

    const previousState = this._lastSpeakerState;
    this._lastSpeakerState = activeState;

    if (this._playlistActive && previousState === 'playing' && activeState !== 'playing' && !this._isAdvancing) {
      this._isAdvancing = true;
      setTimeout(() => {
        const checkSpeaker = this._hass.states[speakerEntityId];
        const checkLullaby = lullabyEntityId ? this._hass.states[lullabyEntityId] : null;
        const stillPlaying = (checkSpeaker && checkSpeaker.state === 'playing') || (checkLullaby && checkLullaby.state === 'playing');
        
        if (!stillPlaying) {
          this.playNextQueueSong(speakerEntityId, lullabyEntityId);
        }
        this._isAdvancing = false;
      }, 2000);
    }
  }

  getCardSize() {
    return 3;
  }
}

if (!customElements.get('cuboai-camera-card')) {
  customElements.define('cuboai-camera-card', CuboAICameraCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some(c => c.type === 'cuboai-camera-card')) {
  window.customCards.push({
    type: 'cuboai-camera-card',
    name: 'CuboAI Camera',
    description: 'Zero-config CuboAI Live View with Two-Way Audio',
    preview: true
  });
}



// ─────────────────────────────────────────────────────────────────────────────
// CuboAI timeline card — a swimlane chart, one row per thing being watched.
//
// Home Assistant's history-graph gives each entity its own strip and its own
// axis, which is unreadable for "what happened last night": you cannot see that
// the noise spike and the baby leaving the crib were the same moment. This puts
// every row on ONE shared axis with hour gridlines, the way the CuboAI app's
// own sleep chart does — except the app's is behind its paid tier, and this is
// drawn from readings Home Assistant already records for free.
//
// It lives in this file rather than its own because the integration registers
// exactly one dashboard resource. A second file would need new plumbing and
// would silently 404 on every install that has not had it added.
// ─────────────────────────────────────────────────────────────────────────────

class CuboAITimelineCard extends HTMLElement {
  setConfig(config) {
    if (!config || !Array.isArray(config.rows) || !config.rows.length) {
      throw new Error("cuboai-timeline-card: 'rows' is required");
    }
    // A row that names no entity is unreadable for an events row -- there is
    // no history reply to come back empty, so it would draw a silent blank
    // lane forever. Only events rows are checked: history rows have shipped
    // without validation and a throw here would break existing dashboards.
    for (const row of config.rows) {
      if (this._eventsAttr(row) && !(row && row.entity)) {
        throw new Error("cuboai-timeline-card: an 'events' row needs an 'entity'");
      }
    }
    this._config = config;
    this._hours = Number(config.hours) || 14;
    this._rows = config.rows;
  }

  // Which attribute, if any, holds this row's point events.
  //
  //   events: alerts     the attribute name
  //   events: true       shorthand for the only list either alert sensor
  //                      publishes
  //
  // Anything else is a history row, unchanged.
  _eventsAttr(row) {
    const a = row && row.events;
    if (a === true) return "alerts";
    return typeof a === "string" && a ? a : null;
  }

  getCardSize() {
    return 2 + this._rows.length;
  }

  set hass(hass) {
    this._hass = hass;
    // Hours of history is a websocket round trip against the recorder.
    // Refetching on every state tick would hammer it and make the card blink.
    const now = Date.now();
    if (this._fetchedAt && now - this._fetchedAt < 60000) {
      // Point events are already in memory -- they arrive as an attribute on a
      // state, not from the recorder -- but nothing on screen changes outside
      // _render, so throttling the FETCH must not also throttle the REPAINT or
      // an alert sits invisible for up to a minute after it fires. Repaint on
      // a cheap signature of the lists, not on every tick.
      const sig = this._eventSignature();
      if (sig !== this._eventSig) {
        this._eventSig = sig;
        this._repaint();
      }
      return;
    }
    this._fetchedAt = now;
    this._eventSig = this._eventSignature();
    this._load();
  }

  // Enough of the event lists to notice a new, removed or replaced alert
  // without walking them on every state tick.
  _eventSignature() {
    let sig = "";
    for (const row of this._rows || []) {
      if (!this._eventsAttr(row)) continue;
      const list = this._eventList(row);
      const newest = list.length ? list[0] : null;
      sig += `${row.entity}=${list.length}@${(newest && (newest.id || newest.ts || newest.time)) || ""};`;
    }
    return sig;
  }

  // Draw the last window again with the current states. No recorder traffic:
  // the history half of the picture is whatever the last fetch returned.
  _repaint() {
    const r = this._lastRender;
    if (!r) return;
    this._render(r.history, r.start, r.end, r.error, r.dataEnd);
  }

  disconnectedCallback() {
    this._fetchedAt = 0;
  }

  // The window this card covers.
  //
  // `hours` alone means "the last N hours ending now", which is why a Night
  // card and a Day card built that way showed almost the same stretch of time:
  // both ended at this moment and merely differed in length. A tab that says
  // 19:00-07:00 has to plot 19:00-07:00.
  //
  //   from/to  a clock window, e.g. 19:00 -> 07:00. `to` at or before `from`
  //            spans midnight. Always the most recently STARTED window, so
  //            Night keeps showing last night all through the following day.
  //   days     a multi-day span ending now, for the Summary.
  //   hours    the original behaviour, kept for anything already using it.
  _window() {
    const now = new Date();
    if (this._config.days) {
      const start = new Date(now.getTime() - Number(this._config.days) * 86400000);
      return { start, axisEnd: now, fetchEnd: now };
    }
    const { from, to } = this._config;
    if (from && to) {
      const parse = (v) => String(v).split(":").map(Number);
      const [fh, fm] = parse(from);
      const [th, tm] = parse(to);
      const start = new Date(now); start.setHours(fh, fm || 0, 0, 0);
      const end = new Date(now); end.setHours(th, tm || 0, 0, 0);
      if (end <= start) start.setDate(start.getDate() - 1);   // spans midnight
      if (start > now) {                                      // not begun today
        start.setDate(start.getDate() - 1);
        end.setDate(end.getDate() - 1);
      }
      // The axis keeps the window's full width even when it has not finished,
      // so an afternoon glance at Daytime shows how much of the day is left
      // rather than silently rescaling to a shorter chart.
      return { start, axisEnd: end, fetchEnd: end > now ? now : end };
    }
    const start = new Date(now.getTime() - this._hours * 3600 * 1000);
    return { start, axisEnd: now, fetchEnd: now };
  }

  async _load() {
    const { start, axisEnd, fetchEnd } = this._window();
    const end = fetchEnd;
    // Events rows are deliberately absent from this list. Their data is a
    // state ATTRIBUTE, which the recorder drops above 16 KiB and which
    // history_during_period is asked not to return anyway (no_attributes
    // below), so querying them would cost a round trip and return nothing.
    const ids = [...new Set(this._rows.filter((r) => !this._eventsAttr(r)).map((r) => r.entity))];
    if (!ids.length) {
      this._render({}, start, axisEnd, null, end);
      return;
    }
    try {
      const history = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: start.toISOString(),
        end_time: end.toISOString(),
        entity_ids: ids,
        minimal_response: true,
        no_attributes: true,
      });
      this._render(history, start, axisEnd, null, end);
    } catch (err) {
      this._render(null, start, axisEnd, (err && err.message) || "History unavailable", end);
    }
  }

  // Turn one row's history into the spans where its test passes.
  //
  // Recorder points carry the moment a state BEGAN, so a span runs from a
  // matching point to the NEXT point of any kind. Running it to the next
  // *matching* point instead would swallow every gap between them and draw one
  // continuous block across a night the baby spent half out of the crib.
  _spans(row, points, start, end) {
    const spans = [];
    const at = (p) => (p.lu !== undefined ? p.lu * 1000 : Date.parse(p.last_changed));
    const value = (p) => (p.s !== undefined ? p.s : p.state);
    const matches = (v) => {
      if (v === undefined || v === null) return false;
      // A gap in the data is not a negative reading, and must not be drawn as
      // one — that is the difference between "not in the crib" and "no idea".
      if (v === "unavailable" || v === "unknown") return false;
      if (row.above !== undefined) return Number(v) >= Number(row.above);
      const want = Array.isArray(row.match) ? row.match : [row.match];
      return want.some((w) => String(w) === String(v));
    };

    let open = null;
    for (const p of points) {
      const raw = at(p);
      // Points outside the window are discarded, not clamped. Clamping one
      // that begins AFTER the end closed its span at the end instead -- a
      // negative width, which surfaced as a lane reporting -35% coverage.
      // Home Assistant bounds its own reply, but the card must not depend on
      // that to produce a sane number.
      if (raw >= end.getTime()) break;
      const t = Math.max(raw, start.getTime());
      if (matches(value(p))) {
        if (open === null) open = t;
      } else if (open !== null) {
        spans.push({ from: open, to: t });
        open = null;
      }
    }
    if (open !== null) spans.push({ from: open, to: end.getTime() });
    return spans.filter((sp) => sp.to > sp.from);
  }

  // ── Point events ────────────────────────────────────────────────────────
  //
  // Alerts are not states. They have a moment, not a duration, and they never
  // reach the recorder as history -- they arrive as a LIST on an attribute of
  // sensor.cuboai_..._last_alert_..., newest first. So they cannot go through
  // _spans (which filters out every zero-width interval at its last line) and
  // they cannot come from _load. They are read straight off hass.states and
  // drawn as ticks.

  // This row's raw list, defended against every way it can be missing.
  //
  // The attribute is normally present and list-valued, but before the first
  // refresh -- and after a refresh whose alert fetch threw, which resets the
  // list to empty rather than keeping the last one -- it is []. That is
  // "nothing to draw", never an error.
  _eventList(row) {
    const attr = this._eventsAttr(row);
    if (!attr) return [];
    const st = this._hass && this._hass.states && this._hass.states[row.entity];
    if (!st || st.state === "unavailable" || st.state === "unknown") return [];
    const list = st.attributes && st.attributes[attr];
    return Array.isArray(list) ? list : [];
  }

  // When one alert happened, in epoch milliseconds, or NaN if it cannot be
  // known.
  //
  // `ts` is unix SECONDS and is preferred because it needs no zone reasoning
  // at all. The string forms do: Date.parse("2024-01-01") is specified as UTC
  // midnight while Date.parse("2024-01-01T00:00") is LOCAL, so a bare date
  // silently lands hours off on a card that is local-time throughout. A
  // date-only string is therefore read as local midnight, matching the rest of
  // this card rather than the spec's split personality.
  _eventTime(a) {
    if (!a || typeof a !== "object") return NaN;
    if (a.ts !== undefined && a.ts !== null && a.ts !== "") {
      const n = Number(a.ts);
      // `ts` has no default upstream: the key exists holding null whenever the
      // API omits it. Plotted, that is a mark at the epoch or left: NaN%.
      if (!Number.isFinite(n) || n <= 0) return NaN;
      return n * 1000;
    }
    const s = a.time || a.created;
    if (typeof s !== "string" || !s) return NaN;
    const t = Date.parse(/^\d{4}-\d{2}-\d{2}$/.test(s) ? s + "T00:00:00" : s);
    return Number.isFinite(t) ? t : NaN;
  }

  // One row's list into the marks inside the window. Pure -- no DOM, no hass.
  //
  // `match_type` narrows the lane to one alert type (or a list of them) so a
  // Cry lane and a Cough lane can be separate rows over the same entity. With
  // none, every type that arrives is drawn: the types are free-form strings
  // from the API and nothing in the pipeline enumerates them, so a hardcoded
  // list would silently hide any type this camera has not shown yet.
  _events(row, list, start, end) {
    if (!Array.isArray(list)) return [];
    const raw = row.match_type;
    const want =
      raw === undefined || raw === null ? null : (Array.isArray(raw) ? raw : [raw]).map(String);
    const from = start.getTime();
    const to = end.getTime();
    const marks = [];
    const seen = new Set();
    for (const a of list) {
      const t = this._eventTime(a);
      if (!Number.isFinite(t)) continue;
      if (t < from || t >= to) continue;
      const type = String((a && a.type) || "unknown");
      if (want && !want.includes(type)) continue;
      // The same alert is re-published on every poll and both alert sensors
      // are built from one list, so a lane can be handed the same event twice.
      // Two marks at one moment stack invisibly and count double in the legend.
      const key = a && a.id !== undefined && a.id !== null ? "id:" + a.id : `${type}@${t}`;
      if (seen.has(key)) continue;
      seen.add(key);
      // `image` is null whenever download_images is off, and the session
      // history sensor's `image_url` is null rather than "" for the same case
      // -- so this is a truthiness test, never a comparison against "".
      marks.push({ t, type, image: (a && (a.image || a.image_url)) || null, id: a && a.id });
    }
    // The list arrives newest first; the axis runs the other way.
    marks.sort((x, y) => x.t - y.t);
    return marks;
  }

  // CUBO_ALERT_TEMPERATURE -> Temperature. The prefix is the same on every
  // type and spends a third of a phone's line width saying nothing.
  _eventLabel(type) {
    const t = String(type || "unknown")
      .replace(/^CUBO_ALERT_/, "")
      .replace(/_/g, " ")
      .toLowerCase()
      .trim();
    if (!t) return "Alert";
    return t.charAt(0).toUpperCase() + t.slice(1);
  }

  _shell() {
    if (this._card) return this._body;
    this._card = document.createElement("ha-card");
    const style = document.createElement("style");
    style.textContent = `
      .tl-wrap { padding: 4px 12px 14px; }
      .tl-row { display: flex; align-items: center; gap: 10px; height: 30px; }
      .tl-ico { flex: 0 0 26px; width: 26px; height: 26px; border-radius: 50%;
                display: flex; align-items: center; justify-content: center;
                --mdc-icon-size: 16px; color: #fff; }
      .tl-track { position: relative; flex: 1 1 auto; height: 22px;
                  border-radius: 4px; background: rgba(127,127,127,.10);
                  overflow: hidden; min-width: 0; }
      .tl-seg { position: absolute; top: 3px; bottom: 3px; border-radius: 3px;
                min-width: 2px; }
      /* A point event has no duration to scale, so its mark is a fixed width
         in PIXELS -- a percentage would be a hairline on a phone and a slab
         on a tablet. 9px stays tappable at any window length. */
      .tl-mark { position: absolute; top: 1px; bottom: 1px; width: 9px;
                 margin-left: -4px; border-radius: 3px; cursor: pointer;
                 box-shadow: 0 0 0 1px rgba(0,0,0,.35); }
      /* The track is overflow:hidden, so a thumbnail cannot live in it. The
         detail line is below the legend and full width -- phone-safe. */
      .tl-shot { display: block; margin-top: 6px; max-width: 100%;
                 width: 180px; border-radius: 6px; }
      /* Gridlines are drawn inside every track at the same offsets. That
         alignment down the column is the entire point of the card. */
      .tl-grid { position: absolute; top: 0; bottom: 0; width: 1px;
                 background: rgba(127,127,127,.25); }
      .tl-axis { display: flex; align-items: center; gap: 10px; margin-top: 2px; }
      .tl-axis .tl-ico { visibility: hidden; }
      .tl-ticks { position: relative; flex: 1 1 auto; height: 16px; min-width: 0; }
      .tl-tick { position: absolute; top: 0; font-size: 11px;
                 color: var(--secondary-text-color); transform: translateX(-50%);
                 white-space: nowrap; }
      .tl-note { font-size: 12px; color: var(--secondary-text-color);
                 padding: 6px 0 0 36px; }
      .tl-seg { cursor: pointer; }
      .tl-ico { cursor: pointer; }
      /* Tooltips need a mouse. On a phone the labels and the timespans were
         unreachable, so both are shown outright. */
      /* Which window this is. Two tabs drawing genuinely different periods
         still read as the same chart when nothing on either says what it
         covers -- especially when several lanes are empty in both. */
      .tl-when { font-size: 12px; color: var(--secondary-text-color);
                 padding: 0 0 6px 36px; }
      .tl-legend { display: flex; flex-wrap: wrap; gap: 4px 12px;
                   padding: 8px 0 0 36px; }
      .tl-pc { color: var(--primary-text-color); font-variant-numeric: tabular-nums; }
      .tl-key { display: flex; align-items: center; gap: 5px; font-size: 12px;
                color: var(--secondary-text-color); }
      .tl-dot { width: 9px; height: 9px; border-radius: 2px; flex: 0 0 auto; }
      .tl-detail { min-height: 18px; padding: 8px 0 0 36px; font-size: 13px;
                   color: var(--primary-text-color); }
      .tl-detail.empty { color: var(--secondary-text-color); font-size: 12px; }
    `;
    this._body = document.createElement("div");
    this._body.className = "tl-wrap";
    this._card.appendChild(style);
    this._card.appendChild(this._body);
    this.appendChild(this._card);
    return this._body;
  }

  _render(history, start, end, error, dataEnd) {
    const body = this._shell();
    this._card.header = this._config.title || undefined;
    body.textContent = "";
    // Kept so a new alert can be repainted onto the same window without going
    // back to the recorder for history that has not changed.
    this._lastRender = { history, start, end, error, dataEnd };

    const anyEvents = this._rows.some((r) => this._eventsAttr(r));
    if (error) {
      const p = document.createElement("div");
      p.className = "tl-note";
      p.textContent = error;
      body.appendChild(p);
      // A recorder hiccup says nothing about the alert lanes -- they never
      // asked it anything -- so they still draw. With no events row the card
      // behaves exactly as it did: the error and nothing else.
      if (!anyEvents) return;
    }
    // Events rows read hass.states, not the recorder, so they are unaffected;
    // history rows have nothing and must say so rather than show a number.
    const historyFailed = Boolean(error);

    const span = end.getTime() - start.getTime();
    const pct = (t) => ((t - start.getTime()) / span) * 100;

    const stamp = (d) =>
      d.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
    const when = document.createElement("div");
    when.className = "tl-when";
    when.textContent = `${stamp(start)} – ${stamp(end)} · ${Math.round(span / 3600e3)}h`;
    body.appendChild(when);

    // Marks, at whatever spacing keeps them from colliding. A week at six
    // hours is twenty-eight labels in the width of a phone, all overlapping,
    // so past two days it switches to one per day and labels the weekday.
    const daily = span > 48 * 3600e3;
    const step = daily ? 24 : span > 20 * 3600e3 ? 6 : span > 10 * 3600e3 ? 3 : 1;
    const marks = [];
    const first = new Date(start);
    first.setMinutes(0, 0, 0);
    for (let t = first.getTime(); t <= end.getTime(); t += 3600e3) {
      if (t < start.getTime()) continue;
      if (new Date(t).getHours() % step) continue;
      marks.push(t);
    }

    let drew = 0;
    const covered = new Map();
    const counted = new Map();
    for (const row of this._rows) {
      let rowCover = 0;
      const line = document.createElement("div");
      line.className = "tl-row";

      const ico = document.createElement("div");
      ico.className = "tl-ico";
      ico.style.background = row.color || "#5e5ce6";
      const icon = document.createElement("ha-icon");
      icon.setAttribute("icon", row.icon || "mdi:circle-small");
      ico.appendChild(icon);
      ico.title = row.label || row.entity;
      ico.addEventListener("click", () => {
        this.dispatchEvent(new CustomEvent("hass-more-info", {
          detail: { entityId: row.entity }, bubbles: true, composed: true,
        }));
      });
      line.appendChild(ico);

      const track = document.createElement("div");
      track.className = "tl-track";
      for (const t of marks) {
        const g = document.createElement("div");
        g.className = "tl-grid";
        g.style.left = pct(t) + "%";
        track.appendChild(g);
      }

      if (this._eventsAttr(row)) {
        const marks = this._events(row, this._eventList(row), start, end);
        for (const m of marks) {
          const mk = document.createElement("div");
          mk.className = "tl-mark";
          mk.style.left = pct(m.t) + "%";
          mk.style.background = row.color || "#ff453a";
          const at = new Date(m.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
          const text =
            `${row.label || row.entity} · ${this._eventLabel(m.type)} · ${at}` +
            (m.image ? " · photo" : "");
          mk.title = text;
          mk.addEventListener("click", (ev) => {
            ev.stopPropagation();
            this._detail.textContent = text;
            this._detail.classList.remove("empty");
            if (m.image) {
              const img = document.createElement("img");
              img.className = "tl-shot";
              img.src = m.image;
              img.alt = "";
              // Photos are pruned to max_saved_photos while alerts_count can
              // be larger, so an older alert holds the path of a jpg that has
              // been deleted -- and /local 404s outright on an install with no
              // www folder. Either way, no broken-image icon.
              img.addEventListener("error", () => img.remove());
              this._detail.appendChild(img);
            }
          });
          track.appendChild(mk);
          drew++;
        }
        counted.set(row, marks.length);
        line.appendChild(track);
        body.appendChild(line);
        continue;
      }

      const points = (history && history[row.entity]) || [];
      for (const s of this._spans(row, points, start, dataEnd || end)) {
        const seg = document.createElement("div");
        seg.className = "tl-seg";
        seg.style.left = pct(s.from) + "%";
        // A one-minute event across fourteen hours is 0.1% wide and invisible,
        // so every span gets a floor it can actually be seen and tapped at.
        seg.style.width = Math.max(pct(s.to) - pct(s.from), 0.6) + "%";
        seg.style.background = row.color || "#5e5ce6";
        const fmt = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        const mins = Math.round((s.to - s.from) / 60000);
        const dur = mins >= 60 ? `${Math.floor(mins / 60)}h ${mins % 60}m` : `${mins}m`;
        const text = `${row.label || row.entity} · ${fmt(new Date(s.from))}–${fmt(new Date(s.to))} · ${dur}`;
        seg.title = text;
        seg.addEventListener("click", (ev) => {
          ev.stopPropagation();
          this._detail.textContent = text;
          this._detail.classList.remove("empty");
        });
        track.appendChild(seg);
        rowCover += s.to - s.from;
        drew++;
      }
      covered.set(row, rowCover);
      line.appendChild(track);
      body.appendChild(line);
    }

    const axis = document.createElement("div");
    axis.className = "tl-axis";
    const spacer = document.createElement("div");
    spacer.className = "tl-ico";
    axis.appendChild(spacer);
    const ticks = document.createElement("div");
    ticks.className = "tl-ticks";
    for (const t of marks) {
      const lab = document.createElement("div");
      lab.className = "tl-tick";
      lab.style.left = pct(t) + "%";
      lab.textContent = daily
        ? new Date(t).toLocaleDateString([], { weekday: "short" })
        : String(new Date(t).getHours()).padStart(2, "0");
      ticks.appendChild(lab);
    }
    axis.appendChild(ticks);
    body.appendChild(axis);

    const dataSpan = (dataEnd ? dataEnd.getTime() : end.getTime()) - start.getTime();
    const legend = document.createElement("div");
    legend.className = "tl-legend";
    for (const row of this._rows) {
      const key = document.createElement("div");
      key.className = "tl-key";
      const dot = document.createElement("span");
      dot.className = "tl-dot";
      dot.style.background = row.color || "#5e5ce6";
      key.appendChild(dot);
      const t = document.createElement("span");
      t.textContent = row.label || row.entity;
      key.appendChild(t);
      // The share of the window each lane covers. This is what actually
      // distinguishes one period from another at a glance -- noise at 52%
      // versus 84% is the difference between two nights, and a wall of
      // identical-looking stipple hides it completely.
      const pc = document.createElement("span");
      pc.className = "tl-pc";
      if (this._eventsAttr(row)) {
        // A share of the window is meaningless for events that have no
        // duration -- it is 0% however many fired. The count says "recent"
        // because it is not the night's total: the integration keeps only its
        // last `alerts_count` alerts (5 by default) from the last `hours_back`
        // hours (12), so the start of a 14-hour window cannot be covered.
        const n = counted.get(row) || 0;
        // An entity id that does not exist returns [] exactly like a quiet
        // night does. Silence about a typo is the worst of both.
        if (this._hass && this._hass.states && !this._hass.states[row.entity]) {
          pc.textContent = "sensor not found";
          pc.title = `No entity named ${row.entity}`;
        } else {
          pc.textContent = n === 1 ? "1 recent alert" : `${n} recent alerts`;
        }
        pc.title = "Only the most recent alerts the integration keeps are available.";
      } else if (historyFailed) {
        // The recorder did not answer, so this lane has no data -- and 0% is a
        // claim about the night, not an admission of not knowing. `_spans`
        // already refuses that conflation for `unavailable` readings; the
        // legend must not undo it one line later.
        pc.textContent = "—";
        pc.title = "No history returned for this window.";
      } else {
        pc.textContent = `${Math.round((100 * (covered.get(row) || 0)) / (dataSpan || span))}%`;
      }
      key.appendChild(pc);
      legend.appendChild(key);
    }
    body.appendChild(legend);

    this._detail = document.createElement("div");
    this._detail.className = "tl-detail empty";
    this._detail.textContent = anyEvents
      ? "Tap a bar or a marker for detail, or an icon for the sensor."
      : "Tap a bar for its times, or an icon for the sensor.";
    body.appendChild(this._detail);

    // An empty chart and a broken one look identical otherwise.
    if (!drew) {
      const p = document.createElement("div");
      p.className = "tl-note";
      p.textContent = "Nothing recorded in this window.";
      body.appendChild(p);
    }
  }
}

if (!customElements.get("cuboai-timeline-card")) {
  customElements.define("cuboai-timeline-card", CuboAITimelineCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "cuboai-timeline-card")) {
  window.customCards.push({
    type: "cuboai-timeline-card",
    name: "CuboAI Timeline",
    description: "One row per sensor, all on a single shared time axis.",
  });
}
