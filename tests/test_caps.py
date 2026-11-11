r"""Offline tests for the "no truncation" caps: spoken replies and recorded queries are not cut by short fixed
limits, yet nothing can hang forever.

Plain script, no pytest needed. Prints PASS/FAIL per test, exits non-zero on failure.
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # tests must never write the real token ledger
import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"  # tests must never send a real phone push
import asyncio
import site
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import torch  # noqa: E402

from jarvis import listener as listener_mod  # noqa: E402
from jarvis import speaker as speaker_mod  # noqa: E402
from jarvis import ui  # noqa: E402
from jarvis.config import load_config  # noqa: E402

ERRORS: list[str] = []
ui.error = lambda text: ERRORS.append(text)
ui.activity = lambda text: None
ui.status = lambda text: None

OLD_MARGIN_S, OLD_CHARS_PER_S = 30.0, 8.0  # the previous fixed formula: 30 + len / 8


def make_speaker(tts_extra=None):
    return speaker_mod.Speaker({"tts": {"chime": True, "engine": "none", **(tts_extra or {})}})


def test_long_reply_deadline_beats_old_cap():
    text = "x" * int(180 * OLD_CHARS_PER_S)  # ~3 minutes by the estimator
    old = OLD_MARGIN_S + len(text) / OLD_CHARS_PER_S
    d = make_speaker().deadline_for(text)
    assert d > old, (d, old)
    assert d >= 180 + 180, d  # at least the estimate plus a proportional margin
    # grows with length, no fixed ceiling
    assert make_speaker().deadline_for(text * 10) > 10 * d - 10 * 60


def test_speak_max_is_optional_ceiling():
    text = "x" * 4000
    assert make_speaker({"speak_max_s": 100}).deadline_for(text) == 100
    assert make_speaker({"speak_max_s": 0}).deadline_for(text) > 100
    assert make_speaker({"speak_margin_s": 25}).deadline_for("") == 25


class FakeWake:
    models = {"fake": None}

    def predict(self, frame):
        return {"fake": 0.0}

    def reset(self):
        pass


class FakeVad:
    def __call__(self, x, sr):
        return torch.tensor(1.0)  # always "talking"

    def reset_states(self):
        pass


def test_utterance_cap_zero_means_none_but_safety_stop():
    assert listener_mod.utterance_cap_s(0) == listener_mod.ABSOLUTE_MAX_UTTERANCE_S
    assert listener_mod.utterance_cap_s(None) == listener_mod.ABSOLUTE_MAX_UTTERANCE_S
    assert listener_mod.utterance_cap_s(-3) == listener_mod.ABSOLUTE_MAX_UTTERANCE_S
    assert listener_mod.utterance_cap_s(45) == 45.0  # explicit value still honoured
    assert listener_mod.utterance_cap_s(99999) == listener_mod.ABSOLUTE_MAX_UTTERANCE_S  # never above the safety stop
    assert listener_mod.ABSOLUTE_MAX_UTTERANCE_S > 45

    cfg = {"audio": {"device": None, "sample_rate": 16000}, "wake_word": {"model": "x", "threshold": 0.5},
           "endpointing": {"vad_threshold": 0.5, "end_silence_ms": 900, "no_speech_timeout_s": 5,
                           "max_utterance_s": 0, "followup_window_s": 1}}
    li = listener_mod.Listener(cfg, wake=FakeWake(), vad=FakeVad(), stream_factory=lambda *a, **k: None)
    assert li.max_utterance_s == listener_mod.ABSOLUTE_MAX_UTTERANCE_S


def test_safety_stop_constant_is_what_cap_zero_uses():
    """Cap 0 resolves to the (patchable) internal safety stop value."""
    old = listener_mod.ABSOLUTE_MAX_UTTERANCE_S
    try:
        listener_mod.ABSOLUTE_MAX_UTTERANCE_S = 0.6
        cfg = {"audio": {"device": None, "sample_rate": 16000}, "wake_word": {"model": "x", "threshold": 0.5},
               "endpointing": {"vad_threshold": 0.5, "end_silence_ms": 900, "no_speech_timeout_s": 5,
                               "max_utterance_s": 0, "followup_window_s": 1}}
        li = listener_mod.Listener(cfg, wake=FakeWake(), vad=FakeVad(), stream_factory=lambda *a, **k: None)
        assert li.max_utterance_s == 0.6
    finally:
        listener_mod.ABSOLUTE_MAX_UTTERANCE_S = old


class HungEngine:
    def chunks(self, text):
        time.sleep(30)
        yield np.zeros(10, dtype=np.float32), 16000


def test_hung_speaker_still_abandoned():
    old_min = speaker_mod.MIN_SPEAK_MARGIN_S
    speaker_mod.MIN_SPEAK_MARGIN_S = 0.0  # let this test use a tiny margin
    try:
        sp = make_speaker({"speak_margin_s": 0.5})
    finally:
        speaker_mod.MIN_SPEAK_MARGIN_S = old_min
    sp.muted = False  # the live app's state file may say muted; don't depend on it
    sp.engine = HungEngine()
    ERRORS.clear()
    t = time.monotonic()
    ok = sp.speak("hello there", threading.Event())
    took = time.monotonic() - t
    assert ok is False and took < 5, (ok, took)  # abandoned at ~margin + estimate, not after 30 s

    import jarvis.__main__ as main_mod

    class HangSpeaker:
        def deadline_for(self, text):
            return 0.2

        def speak(self, text, stop):
            time.sleep(3)

    class Li:
        def watch_for_wake(self, done):
            while not done.is_set():
                time.sleep(0.02)
            return False

    old = main_mod.SPEAK_GRACE_S
    main_mod.SPEAK_GRACE_S = 0.1
    try:
        async def run():  # timed inside: asyncio.run itself waits for the sleeping worker thread at exit
            t = time.monotonic()
            assert await main_mod.speak_with_barge_in(HangSpeaker(), Li(), "hello") is False
            assert time.monotonic() - t < 1.5
        asyncio.run(run())
    finally:
        main_mod.SPEAK_GRACE_S = old


def test_defaults_load_from_config_yaml():
    cfg = load_config()
    assert cfg["endpointing"]["max_utterance_s"] == 0, cfg["endpointing"]
    tts = cfg["tts"]
    assert tts["speak_margin_s"] >= 60 and tts["speak_max_s"] == 0 and tts["speak_grace_s"] >= 15, tts
    sp = speaker_mod.Speaker({"tts": {**tts, "engine": "none"}})
    assert sp.speak_margin_s == tts["speak_margin_s"] and sp.speak_max_s == 0

    import jarvis.__main__ as main_mod
    old = main_mod.SPEAK_GRACE_S
    try:
        main_mod.apply_speech_caps(cfg)
        assert main_mod.SPEAK_GRACE_S == float(tts["speak_grace_s"])
        main_mod.apply_speech_caps({})  # missing key leaves it alone
        assert main_mod.SPEAK_GRACE_S == float(tts["speak_grace_s"])
    finally:
        main_mod.SPEAK_GRACE_S = old
    assert old >= 15.0  # module default is also larger than the old 5 s


def test_utterance_cap_nan_and_inf_use_safety_stop():
    S = listener_mod.ABSOLUTE_MAX_UTTERANCE_S
    for bad in (float("nan"), float("inf"), float("-inf"), "nan", "x", [], {}):
        assert listener_mod.utterance_cap_s(bad) == S, bad


def test_invalid_speak_values_never_raise_and_fall_back():
    for bad in ("x", float("nan"), float("inf"), None, [], "inf"):
        ERRORS.clear()
        sp = make_speaker({"speak_margin_s": bad, "speak_max_s": bad})
        assert sp.speak_margin_s is None and sp.speak_max_s is None, (bad, sp.speak_margin_s)
        assert sp.deadline_for("") == speaker_mod.SPEAK_MARGIN_S, bad
    import jarvis.__main__ as main_mod
    old, old_sp = main_mod.SPEAK_GRACE_S, speaker_mod.SPEAK_GRACE_S
    lines = []
    old_act = ui.activity
    ui.activity = lines.append
    try:
        for bad in ("x", float("nan"), float("inf"), None):
            lines.clear()
            main_mod.SPEAK_GRACE_S = speaker_mod.SPEAK_GRACE_S = 15.0
            main_mod.apply_speech_caps({"tts": {"speak_grace_s": bad}})
            assert main_mod.SPEAK_GRACE_S == 15.0 and speaker_mod.SPEAK_GRACE_S == 15.0, bad
            assert len(lines) == (0 if bad is None else 1), (bad, lines)  # exactly one warning
    finally:
        ui.activity = old_act
        main_mod.SPEAK_GRACE_S, speaker_mod.SPEAK_GRACE_S = old, old_sp


def test_negative_margin_and_grace_are_clamped():
    for bad in (-5, 0, 3):
        sp = make_speaker({"speak_margin_s": bad})
        assert sp.speak_margin_s == speaker_mod.MIN_SPEAK_MARGIN_S and sp.deadline_for("") >= 10, bad
    assert make_speaker({"speak_margin_s": 90}).speak_margin_s == 90
    assert make_speaker({"speak_max_s": -4}).speak_max_s == 0  # negative ceiling = none
    import jarvis.__main__ as main_mod
    old, old_sp = main_mod.SPEAK_GRACE_S, speaker_mod.SPEAK_GRACE_S
    try:
        main_mod.apply_speech_caps({"tts": {"speak_grace_s": -7}})
        assert main_mod.SPEAK_GRACE_S == speaker_mod.MIN_SPEAK_GRACE_S == speaker_mod.SPEAK_GRACE_S
    finally:
        main_mod.SPEAK_GRACE_S, speaker_mod.SPEAK_GRACE_S = old, old_sp


def test_say_grace_follows_config():
    import jarvis.__main__ as main_mod
    from jarvis import brain
    assert brain.SAY_GRACE_S is None  # no hard-coded 5 s any more
    old, old_sp = main_mod.SPEAK_GRACE_S, speaker_mod.SPEAK_GRACE_S
    try:
        assert speaker_mod.SPEAK_GRACE_S >= 15.0
        main_mod.apply_speech_caps({"tts": {"speak_grace_s": 42}})
        assert brain._say_grace_s() == 42.0
        old_b = brain.SAY_GRACE_S
        brain.SAY_GRACE_S = 0.1  # explicit override (tests) still wins
        try:
            assert brain._say_grace_s() == 0.1
        finally:
            brain.SAY_GRACE_S = old_b
    finally:
        main_mod.SPEAK_GRACE_S, speaker_mod.SPEAK_GRACE_S = old, old_sp


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
