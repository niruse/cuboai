"""Two-way audio: the mic button, and the rules its code must keep.

The button was created `display: none !important` and nothing ever showed it,
so no install had a mic button; a docs-image stub whose ha-icon-button set
`display` itself hid the bug from the rendered screenshots. The mic now runs
over Home Assistant's own websocket (the `cuboai/talk` command); the earlier
WebRTC mic (its own RTCPeerConnection to go2rtc's speaker stream) is gone.

These are source checks of ordering and guard rules -- what must come before
what, and what must never appear. The behaviour itself runs in Node in
tests/test_mic_capture.py. Every test names the mutation it kills.
"""

import re
from pathlib import Path

CARD = Path(__file__).resolve().parent.parent / "custom_components" / "cuboai" / "www" / "cuboai-card.js"


def _src() -> str:
    return CARD.read_text(encoding="utf-8")


def _code() -> str:
    """The card without comment lines: comments legitimately name the things
    the guards forbid while explaining why."""
    return "\n".join(line for line in _src().splitlines() if not line.strip().startswith("//"))


def _mic_css() -> str:
    m = re.search(r"this\.micButton\.style\.cssText = '([^']*)'", _src())
    assert m, "mic button style not found"
    return m.group(1)


def _click_handler() -> str:
    src = _src()
    start = src.index("this.micButton.addEventListener('click'")
    end = src.index("\n        });", start)
    return src[start:end]


def _method(name: str) -> str:
    src = _src()
    m = re.search(rf"^  (?:async )?{name}\(", src, re.M)
    assert m, f"{name} not found"
    return src[m.start() : src.index("\n  }\n", m.start())]


def _mic_methods() -> str:
    """Everything the card does for the mic, from the notice helper to the
    last mic method (the shared-settings helpers follow them)."""
    src = _src()
    return src[src.index("  _cuboNotice(text") : src.index("  // ── Shared per-camera settings")]


# =============================================================================
# The button
# =============================================================================


def test_the_mic_button_is_not_hidden():
    """Kill: `display: none` back on the button."""
    display = re.findall(r"display:\s*([a-z-]+)", _mic_css())
    assert display and "none" not in display, display


def test_the_live_ring_can_animate():
    """The live state animates box-shadow; an !important inline box-shadow
    outranks animations. Kill: `!important` back on it."""
    assert re.search(r"box-shadow:[^;]*;", _mic_css())
    assert not re.search(r"box-shadow:[^;]*!important", _mic_css())


def test_the_notice_helper_exists():
    """Kill: the helper deleted (every mic notice would throw)."""
    assert re.search(r"^\s+_cuboNotice\(text, ms = \d+\) \{", _src(), re.M)


def test_a_tap_toggles_and_is_ignored_while_stopping():
    """Toggle: idle starts, connecting or live stops. Kill: a tap while
    stopping starting a second talk, or the tap no longer able to cancel a
    connecting one."""
    h = _click_handler()
    assert "if (st === 'idle') this._cuboStartMic();" in h
    assert "else if (st === 'connecting' || st === 'live') this._cuboStopMic('client_end');" in h
    assert h.count("_cuboStartMic(") == 1 and h.count("_cuboStopMic(") == 1
    # Nothing else in the handler: no unconditional start or stop.
    assert "else this._cubo" not in h


def test_the_button_paints_every_state():
    """Kill: a state without its look (the button would throw on destructure),
    or the button left enabled while stopping."""
    paint = _method("_cuboMicPaint")
    for state in ("idle", "connecting", "live", "stopping"):
        assert re.search(rf"\b{state}: \['mdi:microphone", paint), state
    assert "b.disabled = st === 'stopping';" in paint


# =============================================================================
# The video is never reconfigured for the mic
# =============================================================================
#
# Seen on an iPhone: adding the mic to the VIDEO connection reconnected it on
# every tap (black picture, speaker back to its default), and the session that
# survived came back WITHOUT the mic.


def test_a_mic_tap_never_reconnects_the_video():
    """Kill: the tap, or any mic method, reconfiguring or reconnecting the
    video player."""
    for body in (_click_handler(), _mic_methods()):
        for call in ("setConfig(", "ondisconnect(", "onconnect(", "nextStream("):
            assert call not in body, call
    assert "isMuted =" not in _click_handler()


def test_the_video_config_never_carries_the_microphone():
    """Kill: 'microphone' back in the video player's media, or the old
    micEnabled switch back anywhere."""
    src = _src()
    start = src.index("function cuboaiWebrtcConfig(")
    body = src[start : src.index("\n}\n", start)]
    assert "microphone" not in body
    assert "micEnabled" not in _code()
    assert "function cuboaiWebrtcConfig(found, isMuted)" in src


def test_no_webrtc_mic_is_left():
    """The WebRTC mic sent to go2rtc's speaker stream, which the camera-talk
    arbiter cannot see. Kill: any of it coming back."""
    code = _code()
    for gone in ("RTCPeerConnection", "sign_path", "cuboai_speaker_", "stun:"):
        assert gone not in code, gone


# =============================================================================
# Starting: order matters
# =============================================================================


def test_without_https_the_card_says_why_before_touching_audio():
    """Browsers give no microphone outside a secure context. Kill: the guard
    or its notice removed, or the early return dropped (an AudioContext would
    be made on a page that can never capture)."""
    start = _method("async _cuboStartMic")
    guard = start.index("if (!cuboaiMicCanCapture(window))")
    notice = start.index("this._cuboNotice(", guard)
    insecure = start.index("'insecure'", guard)
    ret = start.index("return;", notice)
    ctx = start.index("new (window.AudioContext")
    assert guard < notice < ret < ctx and insecure < ret


def test_the_audio_context_is_made_inside_the_tap():
    """iOS starts audio only inside the user's tap: once anything is awaited
    the gesture is gone. Kill: the context (or its resume) moved after
    getUserMedia, or a sampleRate forced on it (Firefox refuses the mic on a
    context of another rate)."""
    start = _method("async _cuboStartMic")
    first_await = start.index("await ")
    ctx = start.index("new (window.AudioContext || window.webkitAudioContext)()")
    resume = start.index("mic.ctx.resume()")
    assert ctx < resume < first_await
    assert "sampleRate" not in start[ctx : ctx + 80]


def test_the_talk_opens_only_after_the_microphone_is_captured():
    """The talk makes the camera's speaker click (SPEAKERSTART). Kill: the
    command sent before getUserMedia and the capture graph -- a denied
    permission would then click in the nursery for nothing."""
    start = _method("async _cuboStartMic")
    gum = start.index("await this._cuboMicGetStream()")
    graph = start.index("await this._cuboMicCapture(mic)")
    sub = start.index("conn.subscribeMessage(")
    assert gum < graph < sub


def test_a_reconnect_never_reopens_a_talk():
    """home-assistant-js-websocket re-sends a subscription after a reconnect
    by default, and an unsubscribe made while offline is never sent. Kill:
    `resubscribe: false` removed."""
    start = _method("async _cuboStartMic")
    call = start[start.index("conn.subscribeMessage(") :]
    call = call[: call.index(";")]
    assert "{ resubscribe: false }" in call
    assert "type: 'cuboai/talk'" in start and "sample_rate: CUBOAI_MIC_RATE" in start


def test_the_request_has_a_timeout():
    """Kill: the 10 s race removed (the button would stay amber forever when
    Home Assistant never answers)."""
    start = _method("async _cuboStartMic")
    assert "Promise.race([pending" in start
    assert "CUBOAI_MIC_SUBSCRIBE_MS" in start
    assert re.search(r"const CUBOAI_MIC_SUBSCRIBE_MS = 10000;", _src())


def test_test_mode_is_yaml_only_and_says_so():
    """Kill: the dry-run switches removed, the TEST MODE banner gone, or the
    switch exposed in the visual editor (it must take a deliberate YAML edit)."""
    src = _src()
    start = _method("async _cuboStartMic")
    # Both switches, each read fail-safe (tests/test_mic_capture.py::test_test_mode_fails_safe).
    assert "cuboaiMicDryRunAsked(window.__cuboaiMicDryRun)" in start
    assert "cuboaiMicDryRunAsked((this._config || {}).talk_dry_run)" in start
    assert "TEST MODE — nothing plays at the camera" in src
    editor = src[src.index("class CuboAICameraCardEditor") : src.index("class CuboAICameraCard extends")]
    assert "talk_dry_run" not in editor and "dry" not in editor.lower()


# =============================================================================
# Sending
# =============================================================================


def test_the_socket_is_never_kept():
    """The frontend replaces its socket on every reconnect, and handler ids
    belong to one connection: a kept socket (or one read once per talk) would
    send into a dead one, or route voice to another handler on the new one.
    Kill: the socket stored in a field or a variable outside the send."""
    code = _code()
    assert not re.search(r"(?:this|mic)\.\w+\s*=\s*[^;\n]*\.socket\b", code)
    getter = _method("_cuboMicSocket")
    assert "return conn ? conn.socket : null;" in getter
    send = _method("_cuboMicSend")
    loop = send[send.index("for (const chunk of") :]
    assert loop.index("const sock = this._cuboMicSocket();") < loop.index("sock.send(")
    stop = _method("async _cuboStopMic")
    assert "const sock = this._cuboMicSocket();" in stop


def test_frames_go_out_only_for_a_live_handler_and_through_the_gate():
    """Kill: frames sent before `started` gave a handler id, after a stop, or
    past the send gate (a backlog or a closed socket)."""
    send = _method("_cuboMicSend")
    guard = send.index("mic.handlerId == null")
    assert "mic.closed" in send[: guard + 40] and "st !== 'connecting' && st !== 'live'" in send
    assert "cuboaiMicSendGate(sock) !== 'ok'" in send
    assert send.index("cuboaiMicSendGate(sock)") < send.index("sock.send(cuboaiMicFrame(")


def test_the_frame_constants():
    """What the server is built for: 16 kHz s16le mono, 40 ms messages.
    Kill: a rate, chunk or backlog limit drifting from the contract."""
    src = _src()
    assert re.search(r"const CUBOAI_MIC_RATE = 16000;", src)
    assert re.search(r"const CUBOAI_MIC_CHUNK = 640;", src)
    assert re.search(r"const CUBOAI_MIC_MAX_BUFFERED = 32768;", src)


# =============================================================================
# Stopping
# =============================================================================


def test_stopping_releases_the_microphone_completely():
    """HA's own recorder only suspends the context and disables the track,
    which keeps the iOS play-and-record session and the orange dot. Kill: the
    tracks left running, the context not closed, or suspend() instead."""
    down = _method("_cuboMicTeardown")
    assert "mic.stream.getTracks().forEach((t) => { t.onended = null; t.stop(); })" in down
    assert "mic.ctx.close()" in down
    assert "port.onmessage = null" in down
    assert "suspend(" not in _mic_methods() and "enabled = false" not in _mic_methods()


def test_the_stop_order():
    """Capture first, then the end frame, then up to 4 s for `ended`, then the
    unsubscribe. Kill: the end frame before the capture is down (more frames
    after it), unsubscribing first (late frames hit a dropped handler), or no
    wait (the camera's tail cut)."""
    stop = _method("async _cuboStopMic")
    assert stop.index("if (!mic || mic.closed) return;") < stop.index("mic.closed = true;")
    teardown = stop.index("this._cuboMicTeardown(mic);")
    end = stop.index("sock.send(cuboaiMicEndFrame(mic.handlerId))")
    wait = stop.index("CUBOAI_MIC_ENDED_WAIT_MS")
    release = stop.index("await this._cuboMicRelease(mic);")
    assert teardown < end < wait < release
    assert "reason !== 'disconnected'" in stop[:end]
    assert re.search(r"const CUBOAI_MIC_ENDED_WAIT_MS = 4000;", _src())


def test_every_stop_path_is_armed():
    """Kill: any of the ways a talk must end without the button unarmed --
    the OS taking the mic, the app going to the background, the websocket
    dropping, the audio being interrupted, the camera never starting, the
    server's cap never arriving."""
    arm = _method("_cuboMicArm")
    assert "track.onended = end('track_ended')" in arm
    assert "document.addEventListener('visibilitychange'" in arm and "'hidden'" in arm
    assert "mic.conn.addEventListener('disconnected'" in arm
    assert "mic.ctx.onstatechange" in arm and "'interrupted'" in arm and "'closed'" in arm
    start = _method("async _cuboStartMic")
    assert "CUBOAI_MIC_NO_LIVE_MS" in start and "CUBOAI_MIC_BACKUP_MS" in start
    src = _src()
    # The server stops at 35 s without `live` and caps a talk at 120 s: the
    # card's own timers are the backup, so they must come after those.
    assert re.search(r"const CUBOAI_MIC_NO_LIVE_MS = 40000;", src)
    assert re.search(r"const CUBOAI_MIC_BACKUP_MS = 125000;", src)
    disarm = _method("_cuboMicDisarm")
    assert "removeEventListener('visibilitychange'" in disarm and "removeEventListener('disconnected'" in disarm


def test_leaving_the_dashboard_releases_the_mic():
    """Kill: the disconnect hook not stopping the mic."""
    src = _src()
    hook = src[src.index("  disconnectedCallback() {") : src.index("super.disconnectedCallback();")]
    assert "this._cuboStopMic('card_removed')" in hook


# =============================================================================
# The speaker and the mic
# =============================================================================
#
# Reported 2026-09-28: talking flipped the speaker (mute <-> unmute). By default the
# mic now never touches the speaker. Muting the room while talking is an option
# (talk_mutes_speaker, off by default); it touches only the video element (the
# player's icon follows it), is never saved, and its restore runs in the
# teardown every end path goes through -- inside the stop tap when there is one.


def _talk_mute_helpers() -> str:
    return _method("_cuboTalkMuteOn") + _method("_cuboTalkMuteOff")


def test_without_the_option_the_mic_never_touches_the_speaker():
    """Kill: the talk mute applied without the option, or any mute, unmute or
    speaker icon written from the rest of the mic code."""
    on = _method("_cuboTalkMuteOn")
    assert "this._config.talk_mutes_speaker !== true" in on.split("\n")[1]
    body = _mic_methods()
    for name in ("_cuboTalkMuteOn", "_cuboTalkMuteOff"):
        body = body.replace(_method(name), "")
    for forbidden in (".muted =", "isMuted =", "mdi:volume", "localStorage", "_setSharedSetting", "save_settings"):
        assert forbidden not in body, forbidden


def test_the_talk_mute_is_never_saved_and_never_sets_the_icon():
    """Kill: the talk's mute written to localStorage or the shared setting
    (every device would open muted), or the icon set by hand (icon and sound
    could disagree -- the 'opposite' bug)."""
    body = _talk_mute_helpers()
    for forbidden in ("localStorage", "_setSharedSetting", "save_settings", "icon ="):
        assert forbidden not in body, forbidden


def test_every_end_of_a_talk_restores_the_room():
    """Kill: the restore missing from the teardown (a failed or server-ended
    talk would leave the room muted), or the mute applied before the
    microphone was captured."""
    teardown = _method("_cuboMicTeardown")
    assert teardown.split("\n")[1].strip() == "this._cuboTalkMuteOff(mic);"
    start = _method("async _cuboStartMic")
    arm = start.index("this._cuboMicArm(mic);")
    assert arm < start.index("this._cuboTalkMuteOn(mic);") < start.index("conn.subscribeMessage(")


def test_the_card_stands_down_while_a_talk_mutes_the_room():
    """Kill: any of the card's own unmute paths left unguarded -- the first-tap
    unmute, a rebuilt video, another device's setting, a recording swap -- or
    the speaker tap not marked before the watchdog could act."""
    src = _src()
    auto = src[src.index("const doAutoUnmute = (e) => {") :]
    assert auto.index("if (this._mic && this._mic.talkMute) return;") < auto.index(
        "window.removeEventListener('pointerdown'"
    )
    init = src[src.index("if (!isAppleAudio && this._wantUnmuted") :]
    assert "!(this._mic && this._mic.talkMute)" in init[: init.index("{")]
    sync = src[src.index("const wantMuted = !!shared.muted;") :]
    assert sync.index("if (talkMute) {") < sync.index("if (v) v.muted = wantMuted;")
    assert "showEntity(rec.entityId, !!(this._mic && this._mic.talkMute));" in src
    hook = src[src.index("volumeIcon.addEventListener('click', () => {") :]
    assert hook.index("this._mic.talkMute.touched = true;") < hook.index("setTimeout(")


def test_the_option_is_in_the_card_editor():
    """Kill: the toggle missing, not wired, not reflecting the config, or
    writing `false` (the key must be removed: off is the default)."""
    src = _src()
    assert 'id="talk-mute-toggle"' in src
    assert "['#talk-mute-toggle', 'talk_mutes_speaker', false]," in src
    assert "check('#talk-mute-toggle', c.talk_mutes_speaker === true);" in src
    assert "if (target.checked) newConfig.talk_mutes_speaker = true; else delete newConfig.talk_mutes_speaker;" in src
