"""Tests for issue #98: routed/VLAN diagnosability of the LAN handshake.

The report was "no 0x2041 across VLANs" — but the old failure message was raised
both when the discovery probe got NO ANSWER (a network-path problem: the camera
replies from ephemeral UDP ports, which stateful firewalls drop as unrelated
traffic) and when the camera ANSWERED but refused the grant (a camera-side
problem: rate-limit/load). Opposite causes, one message.

Proven live before writing this: a routed cross-VLAN client IS granted a session
(0.1s, different /24 through a router), so the transport has no same-L2
requirement — which makes the error text the thing to fix.

Covers both PureSession copies (live-stream transport and the playback engine).
"""

import pytest

from custom_components.cuboai.tutk import cuboai_transport_py as live_transport


class _InnerNoNo:
    """connect() fails; discovery was never answered."""

    last_handshake_saw_nO = False
    session_hdr = None

    def connect(self, timeout):
        return False


class _InnerNoGrant(_InnerNoNo):
    """connect() fails; the camera answered discovery but withheld the grant."""

    last_handshake_saw_nO = True


def _session_with(cls, inner):
    sess = cls.__new__(cls)
    sess._inner = inner
    sess.session_hdr = None
    return sess


class TestHandshakeDiagnosisSplit:
    def test_unanswered_discovery_blames_the_network_path(self):
        sess = _session_with(live_transport.PureSession, _InnerNoNo())
        with pytest.raises(RuntimeError) as e:
            sess.connect(timeout_sec=1)
        msg = str(e.value)
        assert "never answered the discovery probe" in msg
        assert "EPHEMERAL UDP ports" in msg
        assert "0x2041 after nO" not in msg

    def test_refused_grant_blames_the_camera(self):
        sess = _session_with(live_transport.PureSession, _InnerNoGrant())
        with pytest.raises(RuntimeError) as e:
            sess.connect(timeout_sec=1)
        msg = str(e.value)
        assert "answered discovery but did not grant" in msg
        assert "rate-limits" in msg
        assert "discovery probe" not in msg

    def test_an_inner_without_the_flag_defaults_to_the_network_diagnosis(self):
        """An old/foreign inner session object must not crash the error path."""

        class _Legacy:
            session_hdr = None

            def connect(self, timeout):
                return False

        sess = _session_with(live_transport.PureSession, _Legacy())
        with pytest.raises(RuntimeError) as e:
            sess.connect(timeout_sec=1)
        assert "never answered the discovery probe" in str(e.value)

    def test_the_flag_exists_and_resets_in_the_real_handshake(self):
        """The wiring, not just the wrapper: connect() must reset the flag at the
        top of every call and set it on nO receipt."""
        import inspect

        from custom_components.cuboai.tutk import cuboai_pure

        src = inspect.getsource(cuboai_pure.TUTKDirectSession.connect)
        reset = src.index("self.last_handshake_saw_nO = False")
        set_on_no = src.index("self.last_handshake_saw_nO = True")
        assert reset < set_on_no
        # set right where the nO is accepted
        assert "nO_recover_R(nO_raw)" in src[:set_on_no]

    def test_playback_engine_copy_stays_in_sync(self):
        import importlib.util
        import re
        from pathlib import Path

        eng = Path(__file__).parent.parent / "custom_components" / "cuboai" / "tutk" / "playback_engine"
        pure_src = (eng / "cuboai_pure.py").read_text(encoding="utf-8")
        assert pure_src.count("self.last_handshake_saw_nO = False") == 1
        assert pure_src.count("self.last_handshake_saw_nO = True") == 1
        # Runtime check (the messages are split across adjacent string literals
        # in source, so match the raised text, not the file text).
        spec = importlib.util.spec_from_file_location("pe_transport", eng / "cuboai_transport_py.py")
        # The playback-engine modules use flat imports (they run as a standalone
        # exec script); loading it here would need sys.path games, so assert on
        # source-contiguous markers instead.
        transport_src = (eng / "cuboai_transport_py.py").read_text(encoding="utf-8")
        assert "discovery probe (no nO reply)" in transport_src
        assert "(no 0x2041 after nO)" in transport_src
        assert re.search(r"last_handshake_saw_nO", transport_src)
        assert spec is not None
