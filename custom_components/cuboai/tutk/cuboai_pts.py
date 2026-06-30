#!/usr/bin/env python3
"""PTS clock (Part B) — turn per-AU TUTK FRAMEINFO into a clean presentation timeline for a
timestamped container (MPEG-TS, MSE/fMP4). Raw Annex-B carries no timestamps, so MSE invents
them from arrival timing and underruns; this maps the camera's real per-frame `timestamp_ms`
to monotonic PTS so MSE plays along currentTime without stalling.

Design:
  • RELATIVE base: base = first valid timestamp_ms; pts_ms = timestamp_ms - base (small values,
    no huge epoch numbers). The base is settable/shareable so a second track (audio) can be added
    later on the SAME epoch for A/V sync — no single-track assumption baked in.
  • STRICT monotonicity: PTS always increases; on a regression/duplicate, clamp to last+min_delta
    (MSE rejects non-monotonic DTS).
  • INVALID timestamps (~10% of AUs carry garbage ts_ms; flagged ts_valid=False): do NOT use the
    garbage — interpolate pts = last + nominal, nominal = running median of recent VALID inter-frame
    deltas (seed ~77 ms ≈ 13 fps), scaled by the frame_no gap when frame_no is trustworthy.
  • NO reordering (refs=1, no B-frames) → DTS = PTS. A PTS-only PES is correct.
  • Keyframe comes from the FRAMEINFO flag (authoritative); cross-checked against the NAL type.
  • 90 kHz container clock: pts_90k = pts_ms*90; a 64-bit internal counter keeps the relative
    timeline well-defined for a 24/7 monitor; the muxer masks to the 33-bit PES field (natural wrap).
"""
import collections


def _median(xs):
    s = sorted(xs); n = len(s)
    if n == 0:
        return None
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


class PTSClock:
    HZ = 90000                                  # MPEG-TS 90 kHz clock

    def __init__(self, nominal_ms=77.0, min_delta_ms=1.0, base_ms=None,
                 median_window=64, max_plausible_delta_ms=1000.0):
        self._base_ms = base_ms                 # shared epoch (None → auto-set on first valid ts)
        self._last_pts_ms = None                # last assigned RELATIVE pts (monotone, float ms, 64-bit-safe)
        self._nominal_seed = float(nominal_ms)
        self._min_delta_ms = float(min_delta_ms)
        self._max_delta_ms = float(max_plausible_delta_ms)
        self._deltas = collections.deque(maxlen=median_window)   # recent VALID inter-frame deltas
        self._prev_valid_ts = None              # previous AU's absolute valid timestamp_ms
        self._prev_frame_no = None
        # diagnostics
        self.n = 0; self.n_interp = 0; self.n_clamp = 0; self.n_kf_mismatch = 0

    # — base / nominal (shareable for A/V sync) —
    def set_base(self, base_ms):
        if self._base_ms is None:
            self._base_ms = base_ms
    @property
    def base_ms(self):
        return self._base_ms
    def nominal_ms(self):
        m = _median(self._deltas)
        return m if m is not None else self._nominal_seed

    def feed(self, *, timestamp_ms, ts_valid, is_keyframe, frame_no=None, nal_keyframe=None):
        """Assign timing to one AU. Returns dict: pts_ms, pts_90k, dts_90k, keyframe, interpolated."""
        self.n += 1
        valid = bool(ts_valid) and timestamp_ms is not None

        if valid:
            if self._base_ms is None:
                self._base_ms = int(timestamp_ms)        # relative base = first valid timestamp
            raw = float(timestamp_ms - self._base_ms)
            # fold this valid inter-frame delta into the running nominal (normalised by frame_no gap)
            if self._prev_valid_ts is not None:
                d = float(timestamp_ms - self._prev_valid_ts)
                if frame_no is not None and self._prev_frame_no is not None:
                    fg = frame_no - self._prev_frame_no
                    if 1 <= fg <= 8 and d > 0:
                        d = d / fg
                if 0 < d < self._max_delta_ms:
                    self._deltas.append(d)
            self._prev_valid_ts = int(timestamp_ms)
            self._prev_frame_no = frame_no
        else:
            # invalid/garbage timestamp → interpolate from the last PTS by the nominal step,
            # scaled by the frame_no gap when frame_no looks trustworthy.
            self.n_interp += 1
            step = self.nominal_ms()
            if frame_no is not None and self._prev_frame_no is not None:
                fg = frame_no - self._prev_frame_no
                if 1 <= fg <= 8:
                    step = self.nominal_ms() * fg
                    self._prev_frame_no = frame_no       # advance frame_no even when ts is garbage
            base_pts = self._last_pts_ms if self._last_pts_ms is not None else 0.0
            raw = base_pts + step

        # first AU whose ts is invalid → start the timeline at 0 (base sets on the first valid ts)
        if self._base_ms is None and self._last_pts_ms is None:
            raw = 0.0

        # strict monotonicity
        if self._last_pts_ms is None:
            pts_ms = max(0.0, raw)
        elif raw < self._last_pts_ms + self._min_delta_ms:
            pts_ms = self._last_pts_ms + self._min_delta_ms
            self.n_clamp += 1
        else:
            pts_ms = raw
        self._last_pts_ms = pts_ms

        kf = bool(is_keyframe)
        if nal_keyframe is not None and bool(nal_keyframe) != kf:
            self.n_kf_mismatch += 1

        pts_90k = int(round(pts_ms * 90.0))      # 64-bit internal; muxer masks to 33-bit PES field
        return {
            'pts_ms': pts_ms,
            'pts_90k': pts_90k,
            'dts_90k': pts_90k,                  # DTS = PTS (refs=1, no B-frames, no reordering)
            'keyframe': kf,
            'interpolated': not valid,
        }

    def stats(self):
        return dict(n=self.n, interpolated=self.n_interp, clamped=self.n_clamp,
                    kf_mismatch=self.n_kf_mismatch, base_ms=self._base_ms,
                    nominal_ms=round(self.nominal_ms(), 1))


class AudioTimeline:
    """Turn a per-AU audio FRAMEINFO into an ABSOLUTE monotonic ms timestamp on the camera's
    unix-epoch clock — to feed a PTSClock that SHARES video's base (A/V sync), NOT a separate
    free-running cadence.

    Why not just count frames? The camera's audio ts is second-resolution (`ts_sec`, the SAME epoch
    as video) with a garbage sub-second field. AAC-LC is cadence-regular (1024 samples / sample_rate
    ≈ 64 ms @16 kHz), so we add intra-second `frame_index × frame_ms` AND re-anchor to `ts_sec` on
    every new second. The re-anchor is the point: a lost audio AU becomes a bounded ≤1 s gap that
    self-corrects at the next second, never the CUMULATIVE drift a free-running `anchor + k·64ms`
    counter would accrue against video. (Within a second a loss compresses positions by ≤ one frame;
    the PTSClock's strict-monotonic clamp absorbs the occasional 16th-frame overshoot past 1000 ms.)
    """
    AAC_SAMPLES_PER_FRAME = 1024

    def __init__(self, sample_rate=16000, samples_per_frame=AAC_SAMPLES_PER_FRAME):
        self._spf = samples_per_frame
        self._frame_ms = 1000.0 * samples_per_frame / float(sample_rate)
        self._cur_sec = None
        self._sub = 0                           # frame index within the current second

    def timestamp_ms(self, ts_sec, sample_rate=None):
        """Absolute ms for an audio AU whose FRAMEINFO carries ts_sec (and optionally sample_rate)."""
        # ┌─ DO NOT "FIX" THE ±56 ms SUB-INDEX OSCILLATION — it is correct by design. ───────────────┐
        # │ `_sub * frame_ms` re-quantises 15.625 frame/s AAC-16k audio onto the 1 s ts_sec grid, so │
        # │ the audio PTS LEADS its true sample time by 0..56 ms (mean −27.5 ms). This is BOUNDED    │
        # │ and EXACTLY 8 s-PERIODIC (125-frame cycle) — NOT accumulating drift, NOT a constant      │
        # │ offset; it returns to 0 every cycle and never grows on a 24/7 stream. It is the          │
        # │ INTENTIONAL price of the per-second re-anchor below: the re-anchor turns a lost AU into  │
        # │ a self-correcting ≤1 s gap instead of the cumulative A/V drift a free-running            │
        # │ `anchor + k·64 ms` counter would accrue. The camera's sub-second ts is GARBAGE, so       │
        # │ ±56 ms is the FLOOR without 1 s of look-ahead. It is sub-perceptual (mean < the ~45 ms   │
        # │ ITU-R BT.1359 audio-ahead threshold; go2rtc AAC→Opus + the WebRTC jitter buffer re-time  │
        # │ it away). "Cap _sub < 1000 ms" / "space frames evenly" REINTRODUCES drift. Leave it.     │
        # └─────────────────────────────────────────────────────────────────────────────────────────┘
        if sample_rate:
            self._frame_ms = 1000.0 * self._spf / float(sample_rate)
        if ts_sec != self._cur_sec:             # new camera second → re-anchor, reset the sub-index
            self._cur_sec = ts_sec
            self._sub = 0
        ms = ts_sec * 1000.0 + self._sub * self._frame_ms
        self._sub += 1
        return ms


class AVTimeline:
    """Shared-base A/V PTS assignment — the single source of truth used by BOTH the live MPEG-TS
    streamer (cuboai_stream_video.mux_timed_stream) and the .mp4 recorder (cuboai_pure.record_video),
    so a captured clip holds the same A/V sync the live stream proved (~0.2 ms/min drift).

    video(fi): PTS from the camera FRAMEINFO timestamp_ms (interpolated when absent).
    audio(fi): PTS from the audio ts_sec via AudioTimeline (drift-free re-anchor).
    Both clocks SHARE one base, seeded ONLY from a VALID timestamp (the ts_valid gate) — never from a
    garbage ts, so it can't reproduce the base-shift bug. Separate PTSClock instances keep each track's
    monotonic timeline independent while the shared epoch preserves the inter-track offset.
    Returns the PTSClock dict (pts_ms, pts_90k, keyframe, interpolated) for each AU.
    """
    def __init__(self, audio_nominal_ms=64.0):
        self._v = PTSClock()
        self._a = PTSClock(nominal_ms=audio_nominal_ms)
        self._atl = AudioTimeline()
        self._base = None

    def _ensure_base(self, ts_ms):
        if self._base is None:
            self._base = int(ts_ms)
            self._v.set_base(self._base)
            self._a.set_base(self._base)

    def video(self, fi, nal_keyframe=None):
        if fi is not None:
            if fi.get('ts_valid'):                       # only a VALID ts seeds the shared base
                self._ensure_base(fi['timestamp_ms'])
            return self._v.feed(timestamp_ms=fi['timestamp_ms'], ts_valid=fi.get('ts_valid', False),
                                is_keyframe=fi.get('is_keyframe', False), frame_no=fi.get('frame_no'),
                                nal_keyframe=nal_keyframe)
        return self._v.feed(timestamp_ms=None, ts_valid=False, is_keyframe=bool(nal_keyframe),
                            nal_keyframe=nal_keyframe)

    def audio(self, fi):
        if fi is not None and fi.get('ts_valid'):
            self._ensure_base(fi['ts_sec'] * 1000)
            ms = self._atl.timestamp_ms(fi['ts_sec'], fi.get('sample_rate'))
            return self._a.feed(timestamp_ms=ms, ts_valid=True, is_keyframe=True)
        return self._a.feed(timestamp_ms=None, ts_valid=False, is_keyframe=True)

    def stats(self):
        return self._v.stats()


# ── self-test (unit tests for the edge cases) ──────────────────────────────────
if __name__ == '__main__':
    def feed_seq(clock, seq):
        """seq items: (timestamp_ms, ts_valid, is_keyframe[, frame_no])"""
        out = []
        for it in seq:
            ts, v, kf = it[0], it[1], it[2]
            fn = it[3] if len(it) > 3 else None
            out.append(clock.feed(timestamp_ms=ts, ts_valid=v, is_keyframe=kf, frame_no=fn))
        return out

    # 1) relative base + steady cadence
    c = PTSClock()
    r = feed_seq(c, [(1780922252778, True, True, 100),
                     (1780922252845, True, False, 101),
                     (1780922252912, True, False, 102),
                     (1780922252978, True, False, 103)])
    assert r[0]['pts_ms'] == 0.0, r[0]
    assert [round(x['pts_ms']) for x in r] == [0, 67, 134, 200], r
    assert all(x['dts_90k'] == x['pts_90k'] for x in r)
    assert r[0]['pts_90k'] == 0 and r[1]['pts_90k'] == round(67 * 90)
    assert r[0]['keyframe'] and not r[1]['keyframe']
    print("1 relative base + cadence + DTS=PTS + kf: OK", [round(x['pts_ms']) for x in r])

    # 2) regression / duplicate → clamp to last + min_delta (strict monotonic)
    c = PTSClock(min_delta_ms=1.0)
    r = feed_seq(c, [(1000000, True, True), (1000077, True, False),
                     (1000077, True, False),    # duplicate ts
                     (1000050, True, False)])   # backward ts
    pts = [x['pts_ms'] for x in r]
    assert all(pts[i] < pts[i+1] for i in range(len(pts)-1)), pts
    assert c.n_clamp == 2, c.stats()
    print("2 monotonic clamp on dup/regression: OK", pts, "clamped=", c.n_clamp)

    # 3) invalid ts (~10%) → interpolate by running-median nominal, stays monotonic
    c = PTSClock(nominal_ms=77.0)
    seq = [(1000000, True, True, 0)]
    for i in range(1, 30):
        if i % 7 == 0:        # ~14% garbage timestamps
            seq.append((999999999, False, False, i))      # garbage ts, ts_valid False
        else:
            seq.append((1000000 + i * 66, True, False, i))  # real ~66ms cadence
    r = feed_seq(c, seq)
    pts = [x['pts_ms'] for x in r]
    assert all(pts[i] < pts[i+1] for i in range(len(pts)-1)), ("non-monotonic", pts)
    interp = [i for i, x in enumerate(r) if x['interpolated']]
    assert interp == [7, 14, 21, 28], interp
    # interpolated steps should be ~nominal (≈66 once the median learns the real cadence)
    for i in interp:
        step = pts[i] - pts[i-1]
        assert 50 < step < 90, (i, step)
    assert 60 <= c.nominal_ms() <= 70, c.nominal_ms()   # learned ≈66ms from valid deltas
    print("3 invalid-ts interpolation (monotonic, nominal learned):", c.stats())

    # 4) frame_no-scaled interpolation: a gap of 2 frames → ~2× step
    c = PTSClock(nominal_ms=66.0)
    r = feed_seq(c, [(1000000, True, True, 0), (1000066, True, False, 1),
                     (0, False, False, 3)])   # invalid ts, frame_no jumped by 2
    step = r[2]['pts_ms'] - r[1]['pts_ms']
    assert 120 < step < 140, ("frame_no-scaled step", step)
    print("4 frame_no-scaled interpolation (~2x):", round(step), "ms OK")

    # 5) 90kHz + 33-bit-mask sanity (muxer masks; clock stays 64-bit)
    c = PTSClock(base_ms=0)
    x = c.feed(timestamp_ms=100000000, ts_valid=True, is_keyframe=True)   # 100000 s
    assert x['pts_90k'] == 100000000 * 90
    assert (x['pts_90k'] & 0x1FFFFFFFF) != x['pts_90k']   # exceeds 33 bits → muxer wraps
    print("5 90kHz + 33-bit wrap awareness: OK pts_90k=", x['pts_90k'])

    # 6) multi-track shared base (A/V sync groundwork)
    base = 1780922252778
    v = PTSClock(base_ms=base); a = PTSClock(base_ms=base)
    rv = v.feed(timestamp_ms=base + 0,   ts_valid=True, is_keyframe=True)
    ra = a.feed(timestamp_ms=base + 10,  ts_valid=True, is_keyframe=True)
    assert rv['pts_ms'] == 0.0 and ra['pts_ms'] == 10.0   # same epoch → comparable timelines
    print("6 multi-track shared base: OK (video=0, audio=+10ms on same epoch)")

    # 7) AudioTimeline: 64ms AAC cadence within a second, re-anchor on each new second
    at = AudioTimeline(sample_rate=16000)
    ms = [at.timestamp_ms(1000) for _ in range(5)]
    assert ms == [1000000.0, 1000064.0, 1000128.0, 1000192.0, 1000256.0], ms
    ms2 = [at.timestamp_ms(1001) for _ in range(2)]
    assert ms2 == [1001000.0, 1001064.0], ms2          # re-anchored to the new camera second
    print("7 AudioTimeline cadence + per-second re-anchor: OK", ms[:3], "→", ms2[:2])

    # 8) drift-free on loss: frames lost mid-second do NOT accumulate; the next second re-anchors
    at = AudioTimeline(sample_rate=16000)
    at.timestamp_ms(1000); at.timestamp_ms(1000)        # frames 0,1 then "lose" the rest of the second
    c = at.timestamp_ms(1001)                            # re-anchors to the camera second exactly
    assert c == 1001000.0, ("loss must not drift", c)    # a free-running counter would give 1000128
    print("8 AudioTimeline drift-free across loss: OK (re-anchored to 1001000, no cumulative skew)")

    # 9) audio through a SHARED-base PTSClock → monotonic PTS, A/V inter-track offset preserved
    base = 1780954281000                                 # audio's first ts (ms), shared epoch
    cv = PTSClock(); ca = PTSClock(nominal_ms=64.0)
    cv.set_base(base); ca.set_base(base)
    at = AudioTimeline(sample_rate=16000)
    ra0 = ca.feed(timestamp_ms=at.timestamp_ms(1780954281), ts_valid=True, is_keyframe=True)
    rv0 = cv.feed(timestamp_ms=1780954282000, ts_valid=True, is_keyframe=True)   # video +1s
    ra1 = ca.feed(timestamp_ms=at.timestamp_ms(1780954281), ts_valid=True, is_keyframe=True)
    assert ra0['pts_ms'] == 0.0 and rv0['pts_ms'] == 1000.0 and ra1['pts_ms'] == 64.0, (ra0, rv0, ra1)
    print("9 audio on shared base: OK (audio 0/64ms, video +1000ms — A/V offset preserved)")

    # 10) audio garbage-ts (lost trailer → ts_valid False) interpolates at the nominal cadence
    ca = PTSClock(base_ms=base, nominal_ms=64.0)
    r0 = ca.feed(timestamp_ms=base, ts_valid=True, is_keyframe=True)
    r1 = ca.feed(timestamp_ms=None, ts_valid=False, is_keyframe=True)            # no trailer → interp
    assert r1['interpolated'] and r1['pts_ms'] > r0['pts_ms'] and 50 < (r1['pts_ms'] - r0['pts_ms']) < 90
    print("10 audio garbage-ts interpolation: OK (interp step ~64ms, monotonic)")

    # 11) AVTimeline: ts_valid gate (a GARBAGE ts must NOT seed the base — the bug#1 guard) +
    #     shared-base A/V offset + audio re-anchor cadence
    av = AVTimeline()
    v0 = av.video({'timestamp_ms': 9_999_999_999, 'ts_valid': False, 'is_keyframe': True,
                   'frame_no': 0}, nal_keyframe=True)
    assert v0['pts_ms'] == 0.0 and av._base is None, ("garbage ts must not seed base", v0, av._base)
    a0 = av.audio({'ts_sec': 1780000000, 'ts_valid': True, 'sample_rate': 16000})   # seeds the base
    a1 = av.audio({'ts_sec': 1780000000, 'ts_valid': True, 'sample_rate': 16000})
    assert av._base == 1780000000000 and a0['pts_ms'] == 0.0 and a1['pts_ms'] == 64.0, (av._base, a0, a1)
    v1 = av.video({'timestamp_ms': 1780000001000, 'ts_valid': True, 'is_keyframe': True, 'frame_no': 5})
    assert v1['pts_ms'] == 1000.0, v1            # video +1s on the SAME epoch → A/V offset preserved
    print("11 AVTimeline: ts_valid base-gate (no garbage shift) + shared-base A/V offset: OK")

    print("\nALL PTSClock UNIT TESTS PASS")
