"""Dictation: keep recording the PC microphone until turned off, then send everything as ONE message.

Normal turns end at the first pause. Dictation ignores that: the listener (Listener.record_dictation)
hands over one audio segment per natural pause, a second thread transcribes the segments in order while
you keep talking, and when dictation is turned off the pieces are joined with spaces into a single text
that goes through the normal voice-turn path. Nothing is lost between pauses and the speech-to-text
model and language settings are untouched.

Turn it on/off with the page's button (POST /dictation), or by voice with the exact short phrases
"start dictation" / "stop dictation" (dictation_command). While dictating, a segment that is exactly the
stop phrase, or that ENDS with "stop dictation", ends the dictation; the phrase is not part of the text.

Phases (event "dictation", snapshot "dictation"): idle -> starting (reserved; the start note is being
spoken) -> recording -> finishing (stopped; the last segments are being transcribed) -> idle.
"""

import queue
import re
import threading
import time

from . import ui
from .events import emit as bus_emit

_START = r"(?:start|begin|turn on)\s+dictat(?:ion|ing)(?:\s+mode)?|dictation\s+(?:mode\s+)?on"
_STOP = r"(?:stop|end|finish|turn off)\s+dictat(?:ion|ing)(?:\s+mode)?|dictation\s+(?:mode\s+)?off"
_CMD = re.compile(r"^(?:jarvis\s+)?(?:please\s+)?(?:(?P<start>" + _START + r")|(?P<stop>" + _STOP + r"))(?:\s+please)?$")


def dictation_command(text: str) -> str | None:
    """"start" / "stop" if the WHOLE utterance is one of the exact short phrases (case and punctuation
    ignored), else None. A sentence that merely contains the words never matches."""
    t = re.sub(r"[^\w\s-]", " ", (text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    m = _CMD.match(t)
    if not m:
        return None
    return "start" if m.group("start") else "stop"


_STOP_TAIL = re.compile(r"(?:^|[^\w])stop\s+dictation[\s.,;:!?'\"]*$", re.IGNORECASE)


def split_stop_suffix(text: str) -> tuple[str, bool]:
    """(text without a trailing "stop dictation", True) if the text ENDS with exactly that phrase
    (case and trailing punctuation ignored); else (text, False). The phrase inside a sentence never matches."""
    m = _STOP_TAIL.search(text or "")
    if not m:
        return text, False
    return text[:m.start()].rstrip(" ,;:-"), True


class Dictation:
    """record(stop_event, on_segment, max_s) -> "stopped"|"max" (blocking; Listener.record_dictation),
    transcribe(audio) -> text, on_done(text, reason, error) called once per dictation from a worker thread
    (reason "stopped" | "max" | "error"; error is the exception or None). on_recording(bool) is told when
    the microphone recording begins / ends. emit(kind, **data) publishes events. All methods are thread-safe."""

    def __init__(self, record, transcribe, on_done, *, max_s: float, on_recording=None, emit=bus_emit,
                 clock=time.time):
        self._record, self._transcribe, self._on_done = record, transcribe, on_done
        self.max_s = max_s
        self._on_recording = on_recording
        self._emit = emit
        self._clock = clock
        self._lock = threading.Lock()
        self.phase = "idle"
        self.since: float | None = None
        self._stop = threading.Event()
        self._reason_stop = "stopped"
        self.stop_why: str | None = None  # who asked for the end: "page", "command", ... (None: not asked / phrase)
        self._segq: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []

    @property
    def active(self) -> bool:
        return self.phase != "idle"

    @property
    def recording(self) -> bool:
        return self.phase == "recording"

    def _publish(self) -> None:
        try:
            self._emit("dictation", on=self.phase in ("starting", "recording"), phase=self.phase,
                       since=self.since, max_s=self.max_s)
        except Exception:
            pass

    # ---- lifecycle ---------------------------------------------------------------------
    def reserve(self) -> bool:
        """Claim the dictation (False if one is already active). The state is "starting" until launch()."""
        with self._lock:
            if self.phase != "idle":
                return False
            self.phase = "starting"
            self.since = self._clock()
            self._stop = threading.Event()
            self._reason_stop = "stopped"
            self.stop_why = None
            self._segq = queue.Queue()
        self._publish()
        return True

    def abort(self) -> None:
        """Give the reservation back without recording (the start note failed or was cancelled). Does
        nothing once recording has begun (then stop() is the way out)."""
        with self._lock:
            if self.phase != "starting":
                return
            self.phase = "idle"
            self.since = None
        self._publish()

    def launch(self) -> None:
        """Begin recording (after reserve()). If stop() was already asked for, finish with nothing."""
        with self._lock:
            if self.phase != "starting":
                return
            stop, segq = self._stop, self._segq
            stopped_early = stop.is_set()
            self.phase = "finishing" if stopped_early else "recording"
        self._publish()
        if stopped_early:
            self._finish([], "stopped", None)
            return
        if self._on_recording:
            try:
                self._on_recording(True)
            except Exception:
                pass
        parts: list[str] = []
        rec = threading.Thread(target=self._rec_worker, args=(stop, segq), name="dictation-rec", daemon=True)
        trans = threading.Thread(target=self._trans_worker, args=(stop, segq, parts), name="dictation-stt", daemon=True)
        self._threads = [rec, trans]
        try:
            trans.start()
        except BaseException as e:  # no worker exists: finish (idle again, mic flag off) instead of sticking
            stop.set()
            self._finish([], "error", e)
            return
        try:
            rec.start()
        except BaseException as e:  # the transcriber is waiting: hand it the end marker
            stop.set()
            with self._lock:
                if self.phase == "recording":
                    self.phase = "finishing"
            self._publish()
            segq.put(("end", "error", e))

    def stop(self, why: str = "user") -> bool:
        """Ask the dictation to end. True if one was active. The text is sent once the last segments
        are transcribed."""
        with self._lock:
            if self.phase not in ("starting", "recording"):
                return self.phase == "finishing"
            if self.stop_why is None:
                self.stop_why = why
            self._stop.set()
            if self.phase == "recording":
                self.phase = "finishing"
        self._publish()
        return True

    # ---- workers -----------------------------------------------------------------------
    def _rec_worker(self, stop, segq) -> None:
        reason, err = "stopped", None
        try:
            reason = self._record(stop, lambda audio: segq.put(("seg", audio)), self.max_s)
        except Exception as e:
            reason, err = "error", e
        with self._lock:
            if self.phase == "recording":
                self.phase = "finishing"
        self._publish()
        segq.put(("end", reason, err))

    def _trans_worker(self, stop, segq, parts) -> None:
        phrase_seen = False
        failures = 0
        while True:
            item = segq.get()
            if item[0] == "end":
                _, reason, err = item
                break
            if phrase_seen:
                continue  # said after "stop dictation": not part of the text
            try:
                text = (self._transcribe(item[1]) or "").strip()
            except Exception as e:
                failures += 1
                ui.error(f"Dictation: couldn't transcribe one segment ({type(e).__name__}); carrying on.")
                continue
            if not text:
                continue
            text, tail = split_stop_suffix(text)  # "...that's all stop dictation": ends it, phrase not sent
            if tail or dictation_command(text) == "stop":
                if tail and text:
                    parts.append(text)
                phrase_seen = True
                stop.set()
                with self._lock:
                    if self.phase == "recording":
                        self.phase = "finishing"
                self._publish()
                continue
            parts.append(text)
        if reason == "stopped" and failures and not parts:
            reason, err = "error", RuntimeError("no segment could be transcribed")
        self._finish(parts, reason, err)

    def _finish(self, parts, reason, err) -> None:
        if self._on_recording:
            try:
                self._on_recording(False)  # before on_done: the stop note may be spoken
            except Exception:
                pass
        text = " ".join(p for p in parts if p).strip()
        try:
            self._on_done(text, reason, err)
        except Exception as e:
            ui.error(f"Dictation: finishing failed ({type(e).__name__}: {e}).")
        with self._lock:
            self.phase = "idle"
            self.since = None
        self._publish()

    def join(self, timeout: float = 5.0) -> None:
        for t in list(self._threads):
            t.join(timeout)
