"""Microphone -> wake word -> one utterance of audio.

Audio arrives in 80 ms frames (1280 samples at 16 kHz), the size openWakeWord wants.
Silero VAD runs on 512-sample windows carved out of the same stream to decide
when you've started and stopped talking.

One pump thread reads every frame from the mic exactly once and fans it out to
subscribers (the wake-word watcher, the VAD recorder), so both can listen at the
same time without stealing frames from each other.
"""

import math
import queue
import threading
import time
import warnings
from collections import deque
from contextlib import contextmanager

import numpy as np
import openwakeword
import sounddevice as sd
import torch
from openwakeword.model import Model as WakeModel
from silero_vad import load_silero_vad

from . import ui

FRAME = 1280
VAD_WINDOW = 512
FRAME_MS = 80
PREROLL_FRAMES = 6  # ~0.5 s kept so follow-ups don't lose their first syllable
SETTLE_QUIET_FRAMES = 2  # 160 ms of quiet after the wake word = end of "Jarvis"
WAKE_FLUSH_FRAMES = 25  # 2 s of silence covers the wake model's input window
SETTLE_MAX_FRAMES = 10  # stop waiting for that quiet after 0.8 s; you're talking through
STALL_S = 3.0  # no mic frame for this long = the input stream has died; reopen it
MAX_STALL_S = 30.0  # reopen backoff ceiling
READ_TIMEOUT_S = 0.2  # every queue read gives up after this, so deadlines are always checked
ABSOLUTE_MAX_UTTERANCE_S = 600.0  # internal safety stop: a stuck mic / noisy room can't record forever


def utterance_cap_s(configured) -> float:
    """Seconds one recording may last. Configured 0 / None / negative = no user-facing cap, which still
    means the absolute safety stop. A configured value is never allowed above the safety stop."""
    try:
        v = float(configured or 0)
    except (TypeError, ValueError):
        v = 0.0
    if not math.isfinite(v) or v <= 0:  # NaN / inf / zero / negative: the safety stop
        return ABSOLUTE_MAX_UTTERANCE_S
    return min(v, ABSOLUTE_MAX_UTTERANCE_S)
DEFAULT_DICTATION_MAX_S = 1800.0
ABSOLUTE_MAX_DICTATION_S = 7200.0  # internal hard stop for dictation, whatever the config says
MAX_DICTATION_SEGMENT_FRAMES = int(60 * 1000 / FRAME_MS)  # one unbroken stretch of speech is cut here


def dictation_cap_s(configured) -> float:
    """Seconds one dictation may last. Missing / invalid / zero / negative -> the default (1800); a
    configured value is never allowed above the internal hard stop."""
    try:
        v = float(configured)
    except (TypeError, ValueError):
        return DEFAULT_DICTATION_MAX_S
    if not math.isfinite(v) or v <= 0:
        return DEFAULT_DICTATION_MAX_S
    return min(v, ABSOLUTE_MAX_DICTATION_S)


class Listener:
    def __init__(self, cfg: dict, wake=None, vad=None, stream_factory=None):
        """wake / vad / stream_factory are injectable so tests can run without models or a mic."""
        self.rate = cfg["audio"]["sample_rate"]
        self.device = cfg["audio"]["device"]
        ww = cfg["wake_word"]
        self.wake_threshold = ww["threshold"]
        ep = cfg["endpointing"]
        self.vad_threshold = ep["vad_threshold"]
        self.end_silence_frames = ep["end_silence_ms"] // FRAME_MS
        self.end_silence_s = ep["end_silence_ms"] / 1000
        self.no_speech_s = float(ep["no_speech_timeout_s"])
        self.max_utterance_s = utterance_cap_s(ep.get("max_utterance_s", 0))

        if wake is None:
            with warnings.catch_warnings():  # it asks for CUDA first; CPU is expected here
                warnings.simplefilter("ignore", UserWarning)
                wake = WakeModel(wakeword_model_paths=[openwakeword.models[ww["model"]]["model_path"]])
        self.wake = wake
        self.wake_name = next(iter(self.wake.models))  # keyed by file stem, e.g. hey_jarvis_v0.1
        if vad is None:
            torch.set_num_threads(1)
            with warnings.catch_warnings():  # torch.jit.load deprecation; harmless, just noise
                warnings.simplefilter("ignore", FutureWarning)
                vad = load_silero_vad()
        self.vad = vad
        self._vad_carry = np.zeros(0, dtype=np.float32)

        self._q: queue.Queue[np.ndarray] = queue.Queue()  # raw frames from the audio callback
        # The pump thread copies each frame to every subscriber queue. Only one recorder may
        # run at a time (it owns the VAD state); wake watchers just read their own copy.
        self._subs: list[queue.Queue] = []
        self._subs_lock = threading.Lock()
        self._pump_thread: threading.Thread | None = None
        self._closed = threading.Event()
        self._rec_lock = threading.Lock()
        self._wake_lock = threading.Lock()  # the wake model is not thread-safe
        self._priority = 0  # a permission prompt is in progress: the wake word steps aside
        self._recording = 0  # recorders active or waiting: the wake word steps aside
        self._dictations = 0  # dictations recording or waiting for the mic: record_utterance gives way
        self.on_frame = None  # optional heartbeat callback, called per pumped frame
        self._make_stream = stream_factory or self._default_stream
        self._stream = self._make_stream()
        self._last_frame = time.monotonic()
        self._stalls = 0  # consecutive reopens without a frame in between

    def _default_stream(self):
        return sd.InputStream(
            samplerate=self.rate, channels=1, dtype="int16", blocksize=FRAME,
            device=self.device, callback=self._on_audio,
        )

    def _on_audio(self, indata, frames, time_info, status):
        self._last_frame = time.monotonic()
        self._stalls = 0
        self._q.put(indata[:, 0].copy())

    def start(self):
        self._last_frame = time.monotonic()
        self._stream.start()
        self._ensure_pump()

    def stop(self):
        self._closed.set()
        self._stream.stop()
        self._stream.close()

    # ---- frame pump ------------------------------------------------------------------

    def _ensure_pump(self):
        with self._subs_lock:
            if self._pump_thread and self._pump_thread.is_alive():
                return
            self._pump_thread = threading.Thread(target=self._pump, daemon=True, name="mic-pump")
            self._pump_thread.start()

    def _pump(self):
        """Read each mic frame once and give a copy to every subscriber. Also notices a dead
        stream and beats the watchdog. With nobody subscribed the frames are just dropped."""
        while not self._closed.is_set():
            try:
                frame = self._q.get(timeout=READ_TIMEOUT_S)
            except queue.Empty:
                self._check_stall()
                continue
            if self.on_frame:
                try:
                    self.on_frame()
                except Exception:
                    pass
            with self._subs_lock:
                subs = list(self._subs)
            for q in subs:
                q.put(frame)

    @contextmanager
    def _subscribe(self):
        """A queue that receives every frame pumped from now until the block ends."""
        self._ensure_pump()
        q: queue.Queue = queue.Queue()
        with self._subs_lock:
            self._subs.append(q)
        try:
            yield q
        finally:
            with self._subs_lock:
                self._subs.remove(q)

    @staticmethod
    def _get_frame(q: queue.Queue, timeout: float = READ_TIMEOUT_S) -> np.ndarray | None:
        """Next frame for this subscriber, or None after timeout (so deadlines get checked)."""
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None

    @contextmanager
    def priority(self):
        """A voice permission prompt is running: the wake word must not fire or consume."""
        self._priority += 1
        try:
            yield
        finally:
            self._priority -= 1

    def wake_muted(self) -> bool:
        return bool(self._priority or self._recording)

    # ---- stall handling --------------------------------------------------------------

    def _check_stall(self):
        now = time.monotonic()
        limit = min(STALL_S * 2 ** self._stalls, MAX_STALL_S)  # backoff while it stays dead
        if now - self._last_frame < limit:
            return
        self._stalls += 1
        self._last_frame = now
        ui.error(f"No microphone audio for {limit:.0f}s; reopening the input stream (attempt {self._stalls}).")
        self._reopen()

    def _reopen(self):
        def work():
            old = self._stream
            try:
                old.stop()
                old.close()
            except Exception as e:
                ui.error(f"Closing the old input stream failed ({e}).")
            try:
                self._stream = self._make_stream()
                self._stream.start()
            except Exception as e:
                ui.error(f"Reopening the input stream failed ({e}); will retry.")

        # PortAudio calls can hang on a wedged driver, so don't let that hang the caller too.
        t = threading.Thread(target=work, daemon=True, name="mic-reopen")
        t.start()
        t.join(5)
        if t.is_alive():
            ui.error("Reopening the input stream is taking over 5 s; carrying on.")

    def _speech_prob(self, frame: np.ndarray) -> float:
        x = np.concatenate([self._vad_carry, frame.astype(np.float32) / 32768.0])
        best = 0.0
        while len(x) >= VAD_WINDOW:
            p = self.vad(torch.from_numpy(x[:VAD_WINDOW]), self.rate).item()
            best = max(best, p)
            x = x[VAD_WINDOW:]
        self._vad_carry = x
        return best

    def _reset_vad(self):
        self.vad.reset_states()
        self._vad_carry = np.zeros(0, dtype=np.float32)

    def reset_wake(self):
        # Model.reset() only clears the score history; the ~10 s of audio features it
        # has already seen stay buffered, so the last "hey Jarvis" would fire it again.
        # Push that out with silence.
        self.wake.predict(np.zeros(FRAME * WAKE_FLUSH_FRAMES, dtype=np.int16))
        self.wake.reset()

    def _reset_wake_locked(self):
        with self._wake_lock:
            self.reset_wake()

    # ---- wake word -------------------------------------------------------------------

    def wait_for_wake(self, stop: threading.Event | None = None) -> bool:
        """Block until the wake word is heard (True), or until stop is set (False)."""
        return self.watch_for_wake(stop or threading.Event())

    def watch_for_wake(self, done: threading.Event) -> bool:
        """Listen for the wake word until done is set. True if it was heard.

        Safe to run alongside a recorder or a permission prompt: while either is active
        it reads and discards frames instead of scoring
        them, then flushes the model's memory before it listens again."""
        with self._subscribe() as q:
            self._reset_wake_locked()
            muted = False
            while not done.is_set():
                frame = self._get_frame(q)
                if frame is None:
                    continue
                if self.wake_muted():
                    muted = True
                    continue
                if muted:
                    muted = False
                    self._reset_wake_locked()  # audio from the muted stretch must not fire it
                    continue
                with self._wake_lock:
                    if self.wake.predict(frame)[self.wake_name] >= self.wake_threshold:
                        return True
        return False

    # ---- recording -------------------------------------------------------------------

    def record_utterance(self, followup_s: float = 0.0, priority: bool = False) -> np.ndarray | None:
        """Record one utterance and return float32 audio, or None if nothing was said.

        followup_s == 0: called right after the wake word; you have no_speech_timeout_s to start.
        followup_s > 0: listening without the wake word; you have that many seconds to start.
        priority: a voice permission prompt (the wake word is muted for any recorder).

        All windows are wall-clock deadlines, so they end even if the mic delivers nothing. While a
        dictation owns (or is waiting for) the mic this returns None at once, and a call already waiting
        for the recorder lock gives up when a dictation starts: nobody blocks behind a dictation.
        """
        if self._dictations:
            return None
        self._recording += 1  # counted before waiting for the recorder lock: the wake word steps aside now
        if priority:
            self._priority += 1
        try:
            while not self._rec_lock.acquire(timeout=READ_TIMEOUT_S):
                if self._dictations:
                    return None
            try:
                if self._dictations:  # a dictation asked for the mic while we were getting the lock
                    return None
                return self._record(followup_s)
            finally:
                self._rec_lock.release()
        finally:
            self._recording -= 1
            if priority:
                self._priority -= 1

    def _record(self, followup_s: float) -> np.ndarray | None:
        with self._subscribe() as q:  # a fresh queue: audio from before now is not wanted
            return self._record_from(q, followup_s)

    def _record_from(self, q: queue.Queue, followup_s: float) -> np.ndarray | None:
        self._reset_vad()
        wait_s = followup_s or self.no_speech_s
        preroll: deque[np.ndarray] = deque(maxlen=PREROLL_FRAMES)
        frames: list[np.ndarray] = []
        started = False
        silent = 0

        if not followup_s:
            # The wake word fires before you finish saying "Jarvis". Let that tail die
            # out so it isn't taken as the whole request. If you keep talking straight
            # through, the preroll already holds the start of the command.
            quiet = 0
            settle: list[np.ndarray] = []
            settle_end = time.monotonic() + SETTLE_MAX_FRAMES * FRAME_MS / 1000
            settled = False
            while time.monotonic() < settle_end:
                frame = self._get_frame(q)
                if frame is None:
                    continue
                talking = self._speech_prob(frame) >= self.vad_threshold
                settle.append(frame)
                quiet = 0 if talking else quiet + 1
                if quiet >= SETTLE_QUIET_FRAMES:
                    settled = True
                    break
            if not settled and settle:
                started = True
                frames.extend(settle)

        wait_end = time.monotonic() + wait_s
        speech_end = time.monotonic() + self.max_utterance_s if started else 0.0
        last_voice = time.monotonic()
        while True:
            now = time.monotonic()
            if started:
                if now >= speech_end:
                    break
                # Frame counts end it normally; this covers a mic that went quiet mid-utterance.
                if now - last_voice >= 2 * self.end_silence_s:
                    break
            elif now >= wait_end:
                return None

            frame = self._get_frame(q)
            if frame is None:
                continue
            talking = self._speech_prob(frame) >= self.vad_threshold
            if talking:
                last_voice = time.monotonic()

            if not started:
                preroll.append(frame)
                if talking:
                    started = True
                    frames.extend(preroll)
                    speech_end = time.monotonic() + self.max_utterance_s
                    last_voice = time.monotonic()
                continue

            frames.append(frame)
            silent = 0 if talking else silent + 1
            if silent >= self.end_silence_frames:
                break

        return np.concatenate(frames).astype(np.float32) / 32768.0

    # ---- dictation -------------------------------------------------------------------

    def record_dictation(self, stop: threading.Event, on_segment, max_s: float | None = None) -> str:
        """Keep recording until `stop` is set or max_s passes; no end-of-speech cut ends it. Speech is
        split into segments at each normal pause (end_silence_ms of quiet) and each finished segment
        (float32 audio) goes to on_segment(audio), which must not block (hand it to a queue). A pause
        however long just waits for the next speech; nothing is dropped between segments (the preroll
        keeps the start of the next one). A stretch of speech with no pause is cut at 60 s. When stop is
        set, frames already queued are drained and the partial segment is delivered too.
        Returns "stopped" or "max". Waits for the recorder lock (stop ends that wait). Exceptions from
        on_segment / the audio path propagate to the caller."""
        cap = dictation_cap_s(max_s)
        self._recording += 1  # the wake word steps aside for the whole dictation
        self._dictations += 1  # waiting utterance recorders give up; new ones return None
        try:
            while not self._rec_lock.acquire(timeout=READ_TIMEOUT_S):
                if stop.is_set():
                    return "stopped"
            try:
                with self._subscribe() as q:
                    return self._dictate(q, stop, on_segment, time.monotonic() + cap)
            finally:
                self._rec_lock.release()
        finally:
            self._dictations -= 1
            self._recording -= 1

    def _dictate(self, q: queue.Queue, stop: threading.Event, on_segment, deadline: float) -> str:
        self._reset_vad()
        preroll: deque[np.ndarray] = deque(maxlen=PREROLL_FRAMES)
        st = {"frames": [], "started": False, "silent": 0}

        def flush():
            if st["frames"]:
                audio = np.concatenate(st["frames"]).astype(np.float32) / 32768.0
                st["frames"] = []
                on_segment(audio)
            st["started"] = False
            st["silent"] = 0

        def feed(frame):
            talking = self._speech_prob(frame) >= self.vad_threshold
            if not st["started"]:
                preroll.append(frame)
                if talking:
                    st["started"] = True
                    st["frames"] = list(preroll)
                    preroll.clear()
                    st["silent"] = 0
                return
            st["frames"].append(frame)
            st["silent"] = 0 if talking else st["silent"] + 1
            if st["silent"] >= self.end_silence_frames or len(st["frames"]) >= MAX_DICTATION_SEGMENT_FRAMES:
                flush()

        reason = "stopped"
        while not stop.is_set():
            if time.monotonic() >= deadline:
                reason = "max"
                break
            frame = self._get_frame(q)
            if frame is not None:
                feed(frame)
        while True:  # frames that arrived before the stop: still part of the dictation
            try:
                feed(q.get_nowait())
            except queue.Empty:
                break
        flush()
        return reason


def list_devices() -> None:
    print(sd.query_devices())
    print(f"\nDefault input: {sd.query_devices(kind='input')['name']}")
