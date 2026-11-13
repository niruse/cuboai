"""After a network outage longer than the fragment window, the reader must re-anchor, not go deaf.

The reader accepts a fragment only if its frag-seq is at most 256 ahead of the high-water mark.
After an outage of more than 256 fragments (~2.2 s at 0.8 Mbps) EVERY later fragment was rejected
as out-of-band for the rest of the session: no output while fragments kept arriving, so the
silence gate could not see it (here the output-stall gate then ended the session and the wrapper
reconnected; possibly the "session goes silent" of #105). Reproduced: a 245-fragment outage
recovers, 280 never did.

A run of 24 consecutive frag-seqs ahead of the window now means the stream moved on: the reader
drops its stale window and the existing start-of-stream seeding re-anchors on the next AU start.
Isolated wild frag-seqs (stray frames) never trigger it. CUBOAI_RESYNC=0 restores the old state.
"""

from tests._stream_rig import Reader, au_parts, load_pure, plain_frag

cp = load_pure("live_pure_resync")


def _run(monkeypatch, outage_frags, resync="1", strays=False):
    r = Reader(cp, {"CUBOAI_RESYNC": resync}, monkeypatch)
    f = i = 0
    for phase in (0, 1):
        if phase == 1:
            f += outage_frags  # the outage: these fragments never arrive
            i += outage_frags // 7
        for _ in range(40):
            for p in au_parts(i):
                r.send(plain_frag(i, f, p))
                f += 1
            if strays and i % 10 == 5:  # an isolated far-ahead frag-seq now and then
                r.send(plain_frag(i, f + 5000 + i, b"\x55" * 50))
            i += 1
    aus, st = r.finish()
    first_after = (40 + outage_frags // 7) % 256
    resumed = sum(1 for a in aus if len(a) > 6 and a[6] >= first_after % 256) if outage_frags else len(aus)
    return aus, st, resumed


def test_short_outage_inside_the_window_recovers_without_resync(monkeypatch):
    aus, st, _ = _run(monkeypatch, 245)
    assert st["resyncs"] == 0 and len(aus) > 60


def test_long_outage_resyncs_and_the_stream_resumes(monkeypatch):
    for gap in (280, 700, 3500):
        aus, st, resumed = _run(monkeypatch, gap)
        assert st["resyncs"] == 1, gap
        assert resumed > 25, (gap, resumed)


def test_resync_off_stays_deaf(monkeypatch):
    aus, st, _ = _run(monkeypatch, 700, resync="0")
    assert st["resyncs"] == 0
    assert len(aus) <= 40  # nothing after the outage


def test_isolated_stray_frag_seqs_do_not_resync(monkeypatch):
    aus, st, _ = _run(monkeypatch, 0, strays=True)
    assert st["resyncs"] == 0 and len(aus) > 60
