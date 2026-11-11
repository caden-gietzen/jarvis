r"""Jarvis entry point.

  .\jarvis devices         list microphones
  .\jarvis listen          wake word + transcription only (step 2 test)
  .\jarvis chat            type to Claude, no audio (step 3 test)
  .\jarvis run             voice -> Claude, replies spoken and shown in the terminal
  .\jarvis say "text"      test the voice (--voice NAME, or --all to hear each)
  .\jarvis voices          list installed voices
  python -m jarvis web-token [--rotate]   where the web page token lives / replace it
"""

import argparse
import asyncio
import os
import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from contextlib import nullcontext
from pathlib import Path

from . import commands, crashreport, restart, ui
from .dictation import dictation_command
from .config import load_config
from .events import bus
from .watchdog import Watchdog, loop_heartbeat
from .speaker import (conversation_command, for_speech, load_conversation_mode, save_conversation_mode,
                      voice_command)


HELPER_PORT = 47822  # the Chrome helper (chrome-ext/helper)
HELPER_SCRIPT = Path(__file__).resolve().parent.parent / "chrome-ext" / "helper" / "start_helper.ps1"
_helper_started = False


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def start_browser_helper(cfg, *, port_check=_port_listening, popen=subprocess.Popen, script=HELPER_SCRIPT) -> bool:
    """Launch the browser helper hidden if nothing listens on its port. Once per process, non-blocking,
    never raises. Returns True if a launch was attempted. Off with start_browser_helper: false."""
    global _helper_started
    try:
        if not cfg.get("start_browser_helper", True) or _helper_started:
            return False
        _helper_started = True
        if port_check(HELPER_PORT):
            ui.activity("Browser helper already running.")
            return False
        if not Path(script).exists():
            ui.activity("Browser helper script not found; not started.")
            return False
        ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), r"System32\WindowsPowerShell\v1.0\powershell.exe")
        # CREATE_NO_WINDOW and DETACHED_PROCESS are mutually exclusive on Windows (the launch silently failed
        # with both); a new process group keeps the helper alive independently of Jarvis' console.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        popen([ps, "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", str(script)],
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              creationflags=flags, close_fds=True)
        ui.activity("Started the browser helper (hidden).")
        return True
    except Exception as e:
        try:
            ui.activity(f"Couldn't start the browser helper ({type(e).__name__}).")
        except Exception:
            pass
        return False


# ---- open the web page at a cold start -------------------------------------------------------------
CHROME_PATHS = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
)


def find_chrome(which=shutil.which, exists=os.path.exists) -> str | None:
    """chrome.exe from PATH or the standard install folders; None if not found."""
    try:
        found = which("chrome") or which("chrome.exe")
        if found:
            return found
        for p in CHROME_PATHS:
            p = os.path.expandvars(p)
            if "%" not in p and exists(p):
                return p
    except Exception:
        pass
    return None


def is_cold_start(usage_pending: bool = False, marker=None) -> bool:
    """True unless this start follows an intentional restart (a fresh marker file, peeked at without
    consuming it) or a usage-limit restart. Never raises (on doubt: a cold start)."""
    try:
        if usage_pending:
            return False
        import json as _json
        marker = restart.MARKER_FILE if marker is None else marker
        if not Path(marker).exists():
            return True
        created = _json.loads(Path(marker).read_text(encoding="utf-8"))["created_at"]
        return not (0 <= restart.now_fn() - float(created) <= restart.MARKER_MAX_AGE_S)
    except Exception:
        return True


def open_web_ui_tab(url: str, *, chrome=None, popen=subprocess.Popen, opener=webbrowser.open_new_tab) -> str | None:
    """Open `url` in a NEW tab: chrome.exe --new-tab (an open Chrome gets a tab, nothing is replaced), else
    the default browser's new tab. Returns "chrome" / "browser", or None if both failed. Never raises and
    never logs the URL (it holds the token)."""
    try:
        exe = chrome or find_chrome()
        if exe:
            popen([exe, "--new-tab", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True)
            return "chrome"
    except Exception:
        pass
    try:
        return "browser" if opener(url) else None
    except Exception:
        return None


def open_web_ui_on_start(cfg, url, *, cold: bool, opener=open_web_ui_tab):
    """After the web server is up: open the page in a new Chrome tab on a cold start. Daemon thread, never
    blocks, never raises, one log line without the token. Off with open_web_ui_on_start: false, env
    JARVIS_OPEN_WEB_UI=0, no URL (web_ui off / server failed) or a restart start. Returns the thread or None."""
    try:
        if not url or not cold or not cfg.get("web_ui", True) or not cfg.get("open_web_ui_on_start", True):
            return None
        if os.environ.get("JARVIS_OPEN_WEB_UI", "").strip() == "0":
            return None

        def run():
            try:
                how = opener(url)
                ui.activity("Opened the web page in " + ("Chrome (new tab)." if how == "chrome"
                            else "the default browser (new tab)." if how else "no browser: couldn't open it."))
            except Exception as e:
                try:
                    ui.activity(f"Couldn't open the web page ({type(e).__name__}).")
                except Exception:
                    pass

        t = threading.Thread(target=run, daemon=True, name="open-web-ui")
        t.start()
        return t
    except Exception:
        return None


SPEAK_GRACE_S = 15.0  # past the speaker's own deadline before speak_with_barge_in gives up (tts.speak_grace_s)


def apply_speech_caps(cfg) -> None:
    """Read tts.speak_grace_s from the config (the speaker's own timing is read by Speaker itself)."""
    global SPEAK_GRACE_S
    from . import speaker as speaker_mod
    v = (cfg.get("tts") or {}).get("speak_grace_s")
    if v is not None:
        # One source of truth: brain's background say() path reads speaker.SPEAK_GRACE_S too.
        SPEAK_GRACE_S = speaker_mod.SPEAK_GRACE_S = speaker_mod.lenient_seconds(
            v, speaker_mod.SPEAK_GRACE_S, "speak_grace_s", speaker_mod.MIN_SPEAK_GRACE_S)
WAKE_STOP_WAIT_S = 2.0  # how long we wait for a wake watcher thread to notice its stop flag


def build_listener_and_stt(cfg):
    from .listener import Listener
    from .stt import make_stt, make_transcriber

    ui.status(f"Loading speech-to-text ({cfg['stt']['engine']})...")
    stt = make_stt(cfg)
    listener = Listener(cfg)

    transcribe = make_transcriber(stt)
    return listener, transcribe


def cmd_listen(cfg):
    from .speaker import Speaker

    listener, transcribe = build_listener_and_stt(cfg)
    speaker = Speaker(cfg)
    listener.start()
    ui.status(f"Say \"hey Jarvis\" (threshold {cfg['wake_word']['threshold']}). Ctrl+C to quit.")
    try:
        while True:
            listener.wait_for_wake()
            speaker.chime()
            ui.status("Listening...")
            audio = listener.record_utterance()
            if audio is None:
                ui.status("Heard nothing; back to waiting for the wake word.")
                continue
            text = transcribe(audio)
            ui.you(text or "[nothing recognisable]")
    finally:
        listener.stop()


async def cmd_chat(cfg, project, resume):
    from .brain import Brain

    crashreport.install_loop()
    crashreport.note(mode="chat")
    brain = Brain(project, cfg["claude"]["model"], resume, cfg.get("allowed_dirs"))
    await brain.start()
    show_dirs(brain)
    ui.status(f"Connected to Claude Code in {project}. Type a message; blank line or Ctrl+C to quit.")
    try:
        while True:
            text = (await asyncio.to_thread(input, "\nYou: ")).strip()
            if not text:
                break
            reply = await local_command(text, None, brain)
            if reply:
                ui.jarvis(reply, "local")
                continue
            reply = await brain.ask(text)
            if reply.strip():
                ui.jarvis(reply, brain.model)
    finally:
        await brain.close()


def show_dirs(brain):
    if brain.allowed_dirs:
        ui.activity("also allowed: " + ", ".join(brain.allowed_dirs))


_reply_stops: set = set()  # stop flags of reply speech in progress, so the web page can cut it off
MAX_TYPED_TURNS = 5  # typed messages from the web page that may be waiting on Claude at once


def stop_reply_speech() -> None:
    """Cut off the reply being spoken right now (the web page's "Stop speaking"). Thread-safe."""
    for stop in list(_reply_stops):
        stop.set()


_plain_active = 0  # speak_plain calls in progress (event-loop thread only)
PLAIN_STOP_WAIT_S = 3.0


def plain_speaking() -> bool:
    return _plain_active > 0


def stop_all_speech(brain) -> None:
    """"hey Jarvis" / Stop speaking: cut off plain speech (restart return line and reply) as well as
    the brain's background speech and narration."""
    stop_reply_speech()
    brain.stop_speech()


async def wait_plain_silent(timeout_s: float | None = None) -> None:
    """Wait until no speak_plain is playing (bounded), so a recording never captures Jarvis's own voice."""
    deadline = time.monotonic() + (PLAIN_STOP_WAIT_S if timeout_s is None else timeout_s)
    while _plain_active > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


# ---- dictation (jarvis/dictation.py): the controller lives here once cmd_run has built it --------------
_dictation = None  # the Dictation controller (voice mode only)
_dictation_pending = 0  # dictations whose text is still being handed over (keeps restart_safe False)
_dictation_pending_lock = threading.Lock()
DICT_START_NOTE = "Dictating."  # spoken only when started by voice; the page's pill/button show it otherwise
DICT_STOP_NOTE = "Dictation off. Sending it."  # printed only (never spoken: the text's reply follows)
DICT_MAX_NOTE = "Dictation reached its time limit. Sending what I have."
DICT_EMPTY_NOTE = "Dictation off. I didn't hear anything."
DICT_FAILED_NOTE = "Dictation failed. Nothing was sent."
DICT_ALREADY_REPLY = "Already dictating."
DICT_NOT_ON_REPLY = "Dictation isn't on."
DICT_NO_MIC_REPLY = "Dictation needs the microphone, so it only works in voice mode."


def dictation_active() -> bool:
    """True while a dictation is starting, recording or finishing (the mic is in use)."""
    d = _dictation
    return bool(d is not None and d.active)


def dictation_busy() -> bool:
    """True while a dictation is active OR its text/notes are still being handed over: nothing may listen
    (it would record Jarvis's own "Dictation off" note) and no voice prompt may start."""
    return dictation_active() or _dictation_pending > 0


def hand_over_dictated(idle: dict, text: str, wake_stop: threading.Event) -> None:
    """A dictation finished while the main loop is idle: park the text for it and wake it. The pending count
    stays up (restart_safe False) until take_dictated() has taken the text, so a restart can't beat it."""
    idle["dictated"] = text
    wake_stop.set()


def take_dictated(idle: dict) -> str | None:
    """The main loop takes the parked dictated text (None if there is none) and releases the pending count."""
    text, idle["dictated"] = idle.get("dictated"), None
    if text is not None:
        _pending_add(-1)
    return text


def _pending_add(n: int) -> None:
    global _dictation_pending
    with _dictation_pending_lock:
        _dictation_pending += n


def dictation_note_spoken(reason: str, text: str, from_page: bool) -> bool:
    """Whether the end-of-dictation note is spoken. A normal stop is silent (the reply to the sent text
    follows) and a stop from the page is always silent (the pill shows it); spoken only for the unasked
    time limit and a voice-stopped empty dictation."""
    return reason == "max" or (not text and not from_page)


async def run_dictation_start(speaker, brain=None, cut_speech: bool = False, announce: bool = True) -> None:
    """After a successful reserve(): say the short start note (silent when muted; skipped when announce is
    False, i.e. started from the page), then begin recording, so the note is never recorded. Never raises;
    a failure gives the reservation back."""
    d = _dictation
    launched = False
    try:
        if cut_speech and brain is not None:
            stop_all_speech(brain)
        await wait_plain_silent()  # nothing else may be mid-speech when the recording begins
        if announce:
            await speak_plain(speaker, DICT_START_NOTE)
        else:
            ui.status("Dictation on.")
        d.launch()
        launched = True
    except BaseException as e:  # incl. CancelledError: the reservation and the mic flag never get stuck
        if isinstance(e, Exception):
            ui.error(f"Couldn't start dictation ({type(e).__name__}: {e}).")
        if not launched:
            try:
                if d is not None:
                    d.abort()
            finally:
                if not dictation_active():
                    try:
                        setattr(speaker, "dictating", False)
                    except Exception:
                        pass
        if not isinstance(e, Exception):
            raise


async def dictation_local_command(cmd: str, speaker, brain) -> str:
    """"start dictation" / "stop dictation" typed or spoken in a normal turn."""
    d = _dictation
    if d is None:
        return DICT_NO_MIC_REPLY
    if cmd == "start":
        if not d.reserve():
            return DICT_ALREADY_REPLY
        await run_dictation_start(speaker, brain)
        return DICT_START_NOTE  # (the voice loop's own speak of this is silent: the mic is now in use)
    if not d.active:
        return DICT_NOT_ON_REPLY
    d.stop("command")
    return "Stopping dictation."


# ---- conversation mode: after a spoken reply, listen for the answer without the wake word ------------
_conversation_on = True  # the default; cmd_run loads the saved choice
CONV_ON_REPLY = "Conversation mode on."
CONV_OFF_REPLY = "Conversation mode off."


def conversation_mode() -> bool:
    return _conversation_on


def set_conversation_mode(on: bool, persist: bool = True) -> bool:
    """Conversation mode on/off. Persists (.jarvis_state.json) and tells every open page. One bool
    assignment, so any thread may call it; the loop reads it when a reply ends."""
    global _conversation_on
    _conversation_on = bool(on)
    if persist:
        save_conversation_mode(_conversation_on)
    bus.emit("conversation_mode", on=_conversation_on)
    return _conversation_on


def after_reply_mode(speaker) -> str:
    """The listening mode once Jarvis has spoken: "wake" if a dictation owns the mic, the voice is off, or
    conversation mode is off; else "followup" (answer without the wake word)."""
    if dictation_active() or is_muted(speaker) or not _conversation_on:
        return "wake"
    return "followup"


VOICE_OFF_REPLY = "Voice off."
VOICE_ON_REPLY = "Voice on."


def is_muted(speaker) -> bool:
    """True if the user turned Jarvis's voice off (text everywhere, no audio)."""
    return bool(getattr(speaker, "muted", False))


async def local_command(text: str, speaker, brain, speak_reply: bool = True) -> str | None:
    """Spoken/typed commands Jarvis handles itself: voice on/off first, then commands.handle (voice and
    model switches). Returns the reply text, or None if it was not a local command.

    Muting: the acknowledgement is spoken BEFORE the voice goes off (here, so the caller's own attempt
    to speak the returned text is then silent). Unmuting: the caller speaks the reply (speak_reply=True,
    the voice loop); if the caller will not speak it (a typed turn), it is spoken here."""
    dcmd = dictation_command(text)
    if dcmd is not None:
        return await dictation_local_command(dcmd, speaker, brain)
    ccmd = conversation_command(text)
    if ccmd is not None:  # needs no speaker; the acknowledgement is the returned text
        set_conversation_mode(ccmd == "on")
        return CONV_ON_REPLY if ccmd == "on" else CONV_OFF_REPLY
    cmd = voice_command(text)
    if cmd is None:
        return await commands.handle(text, speaker, brain)
    if speaker is None:
        return "There is no voice in this mode."
    if cmd == "mute":
        await speak_plain(speaker, VOICE_OFF_REPLY)  # silent if it is already off
        speaker.set_muted(True)
        return VOICE_OFF_REPLY
    speaker.set_muted(False)
    if not speak_reply:
        await speak_plain(speaker, VOICE_ON_REPLY)
    return VOICE_ON_REPLY


async def speak_plain(speaker, text: str) -> None:
    """Speak one piece of text outside the voice loop (restart return line and its reply). Can be
    cut off by the web page's "Stop speaking" or the wake word. Never raises. Silent while muted."""
    global _plain_active
    if is_muted(speaker):
        return
    stop = threading.Event()
    _reply_stops.add(stop)
    _plain_active += 1
    try:
        spoken = for_speech(text)
        await asyncio.wait_for(asyncio.to_thread(speaker.speak, spoken, stop),
                               speaker.deadline_for(spoken) + SPEAK_GRACE_S)
    except Exception as e:
        stop.set()
        ui.error(f"Couldn't speak ({type(e).__name__}: {e}).")
    finally:
        _reply_stops.discard(stop)
        _plain_active -= 1


async def typed_turn(text: str, speaker, brain, speak: bool = False, after=None):
    """One typed turn (web page, usage-limit or restart auto-continue): shown, local commands, then the
    brain. The reply is spoken only with speak=True (after awaiting `after`, e.g. the spoken return line)."""
    try:
        if after is not None:
            try:
                await after
            except Exception:
                pass
        ui.you(text)
        reply = await local_command(text, speaker, brain, speak_reply=speak)
        label = "local"
        if not reply:
            reply, label = await brain.ask(text), brain.model
        if reply and reply.strip():
            ui.jarvis(reply, label)
            if speak:
                await speak_plain(speaker, reply)
    except Exception as e:  # one bad typed turn must not touch the voice loop
        ui.error(f"A typed request failed ({type(e).__name__}: {e}).")


def start_web_ui(cfg, *, loop, brain, speaker):
    """Start the local web page unless web_ui is false in config. Returns the WebServer or None;
    never raises. Typed text takes the same path as a spoken turn (commands, then brain.ask), so
    the brain's FIFO turn handling is untouched; its reply is shown on the page and in the terminal
    but not spoken (the speaker belongs to the voice loop)."""
    if not cfg.get("web_ui", True):
        return None
    try:
        from . import cancel as cancel_mod
        from . import web as web_mod

        typed: set = set()

        def begin(text: str):
            task = asyncio.ensure_future(typed_turn(text, speaker, brain))
            typed.add(task)
            task.add_done_callback(typed.discard)

        def on_send(text: str):
            if len(typed) >= MAX_TYPED_TURNS:
                raise RuntimeError("too many typed messages are still waiting for answers")
            loop.call_soon_threadsafe(begin, text)

        def on_stop_speech():
            stop_reply_speech()
            loop.call_soon_threadsafe(brain.stop_speech)  # (stop_reply_speech above already cut plain speech)

        def on_voice(muted: bool):
            speaker.set_muted(muted)  # persists, cuts current speech, emits the "voice" event
            return speaker.muted

        def on_conversation(on: bool):
            return set_conversation_mode(on)  # persists and emits the event; no spoken note for the page

        def on_dictation(on: bool):
            d = _dictation
            if d is None:
                raise RuntimeError("dictation is not available")
            if on:
                if d.reserve():  # synchronous: a double click can't start two
                    def go():
                        try:
                            asyncio.ensure_future(run_dictation_start(speaker, brain, cut_speech=True, announce=False))
                        except BaseException:
                            d.abort()
                            raise
                    try:
                        loop.call_soon_threadsafe(go)
                    except BaseException:
                        d.abort()  # the loop is gone: give the reservation back
                        raise
            else:
                d.stop("page")
            return d.phase in ("starting", "recording")

        def on_permission(pid, decision):
            fn = getattr(brain, "resolve_permission", None)
            return fn(pid, decision) if fn else "unknown"

        bind = str(cfg.get("web_bind", "tailnet")).strip().lower()
        return web_mod.WebServer(on_send, web_mod.make_cancel_hook(loop, brain, cancel_mod), on_stop_speech,
                                 port=int(cfg.get("web_port") or web_mod.DEFAULT_PORT),
                                 token=web_mod.load_token(), on_permission=on_permission,
                                 on_voice=on_voice if speaker is not None else None,
                                 on_dictation=on_dictation if _dictation is not None else None,
                                 on_conversation=on_conversation,
                                 bind="tailnet" if bind == "tailnet" else "local")
    except Exception as e:
        ui.error(f"Web UI not started ({type(e).__name__}: {e}).")
        return None


async def speak_with_barge_in(speaker, listener, text: str) -> bool:
    """Speak while listening for the wake word. True if you cut in with "hey Jarvis". Voice off: nothing
    is spoken (the text is already shown) and there is nothing to cut in on."""
    if is_muted(speaker):
        return False
    stop, done = threading.Event(), threading.Event()
    _reply_stops.add(stop)

    async def speak():
        spoken = for_speech(text)
        try:
            # Outer cap in case speak() itself wedges; also ends the wake-word watcher.
            await asyncio.wait_for(asyncio.to_thread(speaker.speak, spoken, stop),
                                   speaker.deadline_for(spoken) + SPEAK_GRACE_S)
        except asyncio.TimeoutError:
            ui.error("Speaking the reply timed out; carrying on.")
            stop.set()
        except Exception as e:  # audio trouble must never end the run
            ui.error(f"Couldn't speak the reply ({type(e).__name__}: {e}).")
        finally:
            done.set()

    async def watch():
        heard = await asyncio.to_thread(listener.watch_for_wake, done)
        if heard:
            stop.set()
        return heard

    try:
        _, heard = await asyncio.gather(speak(), watch())
    finally:
        _reply_stops.discard(stop)
    return heard


async def race_wake(listener, work: asyncio.Future) -> bool:
    """Wait for `work` to finish while listening for "hey Jarvis". True if it was heard first;
    `work` is left running (we never cancel the user's background work for them). The watcher
    thread is always stopped before this returns, so no orphan keeps consuming frames."""
    stop = threading.Event()
    watcher = asyncio.ensure_future(asyncio.to_thread(listener.watch_for_wake, stop))
    try:
        await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if watcher.done() and watcher.exception() is not None:
            ui.error(f"Wake word watcher failed ({watcher.exception()}); finishing without it.")
            await asyncio.wait({work})
            return False
        return watcher.done() and bool(watcher.result())
    finally:
        stop.set()
        try:
            await asyncio.wait_for(watcher, WAKE_STOP_WAIT_S)
        except Exception:  # a stuck or failed watcher must not take the main loop down
            pass


async def flush_with_wake(brain, listener, wd=None) -> bool:
    """Speak queued background replies while still listening. True if "hey Jarvis" cut in
    (the interrupted reply stays queued)."""
    return (await _flush(brain, listener, wd))[0]


async def flush_mode(brain, listener, wd=None) -> str:
    """flush_with_wake, then pick the next listening mode: "command" if "hey Jarvis" cut in,
    "followup" if Jarvis spoke on its own and finished (you can answer without the wake word),
    else "wake" (nothing queued, or it was cut off some other way)."""
    heard, spoke = await _flush(brain, listener, wd)
    return "command" if heard else "followup" if spoke and _conversation_on else "wake"


IDLE_RESTART_POLL_S = 5.0  # how often idle wake-word listening looks for the restart flag


def restart_safe(brain) -> bool:
    """True if a restart cannot cut anything off: no reply/turn in progress, no outstanding ask, no
    permission prompt pending, no background speech being spoken, no plain speech (restart return line
    or its reply) playing. Unknown brains count as safe."""
    if plain_speaking() or dictation_active() or _dictation_pending > 0:
        return False
    try:
        can = getattr(brain, "_can_speak_background", None)
        if can is not None and not can():
            return False
        task = getattr(brain, "_flush_task", None)
        if task is not None and not task.done():
            return False
    except Exception:
        pass
    return True


async def wait_for_wake_or_restart(listener, wake_stop: threading.Event, brain,
                                   poll_s: float | None = None) -> tuple:
    """Idle wake-word wait that also notices the restart flag. Returns (heard, restart).

    The wake wait runs in a thread as before; a light poller (every poll_s, no audio work) checks
    restart.restart_pending() and, when it is safe (restart_safe), sets wake_stop to end the wait.
    restart=True means the caller should hand over to a fresh Jarvis (heard is then False).
    A wake word heard at the same moment wins; the flag stays and is acted on later."""
    poll_s = IDLE_RESTART_POLL_S if poll_s is None else poll_s
    found = {"restart": False}
    done = asyncio.Event()

    async def poll():
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), poll_s)
            except asyncio.TimeoutError:
                pass
            if done.is_set():
                return
            try:
                if restart.restart_pending() and restart_safe(brain):
                    found["restart"] = True
                    wake_stop.set()
                    return
            except Exception:
                pass

    poller = asyncio.ensure_future(poll())
    try:
        heard = await asyncio.to_thread(listener.wait_for_wake, wake_stop)
    finally:
        done.set()
        poller.cancel()
    if heard and found["restart"]:
        wake_stop.clear()  # the wake word won the race: the stop was ours, not a shutdown
    return bool(heard), bool(found["restart"]) and not heard


def start_restart():
    """Hand over to a fresh Jarvis (same path as after a conversation)."""
    reason = restart.flag_reason()
    restart.clear_restart()
    restart.write_marker(reason)  # the new instance sees this and knows the restart was on purpose
    ui.status("Restarting...")
    try:
        ok = restart.launch_restart_script()
    except Exception:
        ok = False
    if ok is False:  # nothing was started: a leftover marker would mislead a later manual start
        try:
            restart.MARKER_FILE.unlink(missing_ok=True)
        except Exception:
            pass


async def _flush(brain, listener, wd=None) -> tuple:
    """(heard wake word, background speech finished normally)."""
    brain.hold_background = False
    flush = asyncio.ensure_future(brain.flush_background())
    try:
        with wd.busy("speaking background reply", 300) if wd else nullcontext():
            heard = await race_wake(listener, flush)
        if heard:
            brain.hold_background = True  # the next queued reply must not start; the caller releases it
            stop_all_speech(brain)
        spoke = await flush
        return heard, bool(spoke) and not heard
    finally:
        if not flush.done():
            flush.cancel()


async def converse(asks: list, *, brain, speaker, listener, take_turn, submit, wd=None) -> str:
    """Wait for replies to outstanding turns and speak each, oldest first (brain._outstanding
    is FIFO too). "hey Jarvis" at any point (while thinking, mid-narration, mid-reply, or during
    background speech) stops speech and takes what you say as a new turn; work already running
    is NOT cancelled, the model decides that. Returns the next listening mode."""
    while True:
        heard = False
        while asks:
            crashreport.note(mode="thinking")
            with wd.busy("thinking") if wd else nullcontext():
                heard = await race_wake(listener, asks[0])
            if not heard:
                try:
                    reply, label = await asks.pop(0)
                except Exception as e:  # a failed turn must not end the run
                    ui.error(f"A request failed ({type(e).__name__}: {e}).")
                    continue
                if not reply.strip():
                    continue  # empty or interrupted result: nothing to say
                ui.jarvis(reply, label)
                crashreport.note(mode="speaking")
                heard = await speak_with_barge_in(speaker, listener, reply)
                if heard:
                    ui.activity("interrupted")
            if heard:
                stop_all_speech(brain)
                text, fallback = await take_turn("command")
                if text:
                    asks.append(submit(text))
                elif not asks:
                    return fallback
        # Everything answered. Speak what background agents said meanwhile, still listening.
        if await flush_with_wake(brain, listener, wd):
            text, fallback = await take_turn("command")
            if text:
                asks.append(submit(text))
                continue
            return fallback
        # no follow-up to offer if muted, the mic is in use for a dictation, or conversation mode is off
        return after_reply_mode(speaker)


async def cmd_run(cfg, project, resume):
    from .brain import Brain
    from .speaker import Speaker

    crashreport.install_loop()
    crashreport.note(mode="starting")
    try:  # decided before anything consumes the restart marker or the usage continue
        from . import usage_monitor as _um
        cold_start = is_cold_start(usage_pending=_um.continue_pending())
    except Exception:
        cold_start = is_cold_start()
    restart.save_window_identity()
    restart.clear_restart()  # drop any stale flag from a previous session
    try:  # after clear_restart: a re-armed usage-limit timer creates the flag later
        from . import usage_monitor
        usage_monitor.on_startup()
    except Exception as e:
        ui.activity(f"Usage monitor not started ({type(e).__name__}).")
    listener, transcribe = build_listener_and_stt(cfg)
    speaker = Speaker(cfg)
    set_conversation_mode(load_conversation_mode(), persist=False)  # saved choice; ON on the very first run
    speaker.set_muted(speaker.muted, persist=False)  # pages opened later see the saved state in the snapshot
    if speaker.muted:
        ui.status("Voice is off (saved setting). Say \"voice on\" or use the page's button to turn it back on.")
    voice_confirm = cfg.get("permissions", {}).get("confirm", "voice") == "voice"
    brain = Brain(project, cfg["claude"]["model"], resume, cfg.get("allowed_dirs"),
                  speaker=speaker if voice_confirm else None,
                  listener=listener if voice_confirm else None,
                  transcribe=transcribe if voice_confirm else None,
                  answer_window_s=cfg["endpointing"]["followup_window_s"])
    await brain.start()
    listener.start()
    # Hang detector. The listener's pump beats "mic" for every frame it reads, whoever is
    # listening, so the mic heartbeat now tracks the input stream itself; the busy scopes
    # (thinking, transcribing, background speech) stay as before and only excuse a dead mic.
    crashreport.register("run", lambda: {"voice": speaker.voice})
    wd = Watchdog()
    listener.on_frame = wd.beat
    wd.start()
    heartbeat = asyncio.create_task(loop_heartbeat(wd))
    followup = cfg["endpointing"]["followup_window_s"]
    show_dirs(brain)
    # wake: waiting for "hey Jarvis"; command: just woken, record now; followup: no wake word needed
    wake_stop = threading.Event()
    idle = {"waiting": False, "spoke": False, "dictated": None}  # idle: main loop is parked in wait_for_wake
    global _dictation
    from .dictation import Dictation
    from .listener import dictation_cap_s
    loop_ = asyncio.get_running_loop()
    dict_tasks: set = set()

    async def finish_dictation(text, reason, err):
        """Dictation ended: say so (silent when muted), then send the text as ONE message the way a spoken
        turn is sent: handed to the main loop when it is idle, else as a spoken typed turn."""
        handed = False  # True: the text waits for the idle loop, which then releases the pending count
        try:
            if reason == "error":
                ui.error(f"Dictation failed ({type(err).__name__ if err else 'error'}: {err}). Nothing was sent.")
                if text:
                    ui.status("Dictated so far (not sent): " + text)
                await speak_plain(speaker, DICT_FAILED_NOTE)
                return
            note = DICT_MAX_NOTE if reason == "max" else DICT_STOP_NOTE if text else DICT_EMPTY_NOTE
            ui.status(note)
            d_ = _dictation
            if dictation_note_spoken(reason, text, d_ is not None and d_.stop_why == "page"):
                await speak_plain(speaker, note)
            if not text:
                return
            if idle["waiting"]:
                ui.you(text)
                hand_over_dictated(idle, text, wake_stop)
                handed = True  # restart_safe stays False until the main loop has taken the text
            else:
                task = asyncio.ensure_future(typed_turn(text, speaker, brain, speak=True))
                dict_tasks.add(task)
                task.add_done_callback(dict_tasks.discard)
        except Exception as e:
            ui.error(f"Couldn't send the dictation ({type(e).__name__}: {e}).")
        finally:
            if not handed:
                _pending_add(-1)

    def on_dictation_done(text, reason, err):  # worker thread
        _pending_add(1)  # counted before the dictation reports idle: restart_safe stays False
        try:
            loop_.call_soon_threadsafe(lambda: asyncio.ensure_future(finish_dictation(text, reason, err)))
        except Exception:  # loop closed (shutting down)
            _pending_add(-1)

    _dictation = Dictation(listener.record_dictation, transcribe, on_dictation_done,
                           max_s=dictation_cap_s(cfg.get("dictation_max_s")),
                           on_recording=lambda on: setattr(speaker, "dictating", on))
    ui.status(f"Connected to Claude Code in {project}. Voice: {speaker.voice}. Say \"hey Jarvis\". Ctrl+C to quit.")
    web = start_web_ui(cfg, loop=asyncio.get_running_loop(), brain=brain, speaker=speaker)
    if web is not None:
        url = await asyncio.to_thread(web.start)
        if url:  # console only: the token must not reach the log or the event feed
            ui.console.print(f"[yellow]Web page: {url}[/]")
            ui.console.print(f"[dim]  (also saved to {web.url_file})[/]")
            if web.tailnet_ip:  # no token in this line: the phone link comes from make_phone_qr.py
                ui.console.print(f"[dim]Phone (Tailscale): http://{web.tailnet_ip}:{web.port}/  (token: jarvis-workspace/make_phone_qr.py)[/]")
        open_web_ui_on_start(cfg, url, cold=cold_start)  # daemon thread, never fatal
    if cfg.get("start_browser_helper", True):
        await asyncio.to_thread(start_browser_helper, cfg)
    try:  # after a usage-limit restart: continue the handoff by itself (non-blocking, never fatal)
        from . import usage_monitor
        auto_tasks: set = set()
        loop = asyncio.get_running_loop()

        def auto_begin(text: str):
            task = asyncio.ensure_future(typed_turn(text, speaker, brain))
            auto_tasks.add(task)
            task.add_done_callback(auto_tasks.discard)

        # An intentional restart (marker written by start_restart) continues too, spoken; a pending
        # usage-limit continue wins (checked before its thread clears it) and stays silent.
        ret = {"line": None}

        def return_speak(text: str):
            def go():
                ui.jarvis(text, "restart")
                ret["line"] = asyncio.ensure_future(speak_plain(speaker, text))
            loop.call_soon_threadsafe(go)

        def return_submit(text: str):
            def go():
                task = asyncio.ensure_future(typed_turn(text, speaker, brain, speak=True, after=ret["line"]))
                auto_tasks.add(task)
                task.add_done_callback(auto_tasks.discard)
            loop.call_soon_threadsafe(go)

        def return_log(msg: str):
            ui.activity(msg)
            if msg.startswith("Intentional restart"):
                ui.status(msg)  # visible on the web page and console, not just a plain initialization

        restart.schedule_return(return_submit, return_speak, return_log,
                                enabled=bool(cfg.get("continue_after_intentional_restart", True)),
                                usage_continue_pending=usage_monitor.continue_pending())
        usage_monitor.schedule_auto_continue(lambda text: loop.call_soon_threadsafe(auto_begin, text))
    except Exception as e:
        ui.activity(f"Auto-continue not started ({type(e).__name__}).")

    async def take_turn(kind: str):
        """Record and transcribe one utterance (kind: "command" right after the wake word,
        "followup" with no wake word). Returns (text, mode to fall back to if there is no text)."""
        crashreport.note(mode=kind)
        brain.hold_background = True  # user is talking / being answered: queue bg replies
        brain.mute_narration = True  # and narration must not talk over (or into) the recording
        try:
            async with brain.user_turn():  # a permission prompt must not talk over the recording
                await wait_plain_silent()  # restart return line / reply must be finished or cut off
                if dictation_busy():  # the mic is (or was just) a dictation's: its notes must not be recorded
                    return None, "wake"
                if kind == "command":
                    speaker.chime()
                    ui.status("Listening...")
                    audio = await asyncio.to_thread(listener.record_utterance)
                else:
                    ui.status(f"(listening for a follow-up for {followup}s...)")
                    audio = await asyncio.to_thread(listener.record_utterance, followup)
            if audio is not None and dictation_busy():
                audio = None  # a dictation started meanwhile: whatever was heard belongs to it
            if audio is None:
                ui.status("Waiting for \"hey Jarvis\".")
                return None, "wake"
            crashreport.note(mode="transcribing")
            with wd.busy("transcribing", 120):
                text = await asyncio.to_thread(transcribe, audio)
            if not text:
                ui.status("Didn't catch that.")
                return None, kind
            ui.you(text)
            return text, kind
        finally:
            brain.mute_narration = False

    def submit(text: str) -> asyncio.Future:
        """Start one turn; resolves to (reply, label). Local commands answer without Claude."""
        async def run():
            reply = await local_command(text, speaker, brain)
            if reply:
                return reply, "local"
            ui.status("Thinking...")
            return await brain.ask(text), brain.model

        return asyncio.ensure_future(run())

    mode = "wake"
    pending_text = None  # a finished dictation, sent as this turn's text (mode "dictated")

    def background_spoke():
        # Jarvis spoke on its own while idle and finished: end the wait and listen for a reply.
        # Ignored unless idle, so it can't hijack a turn that is already under way.
        if idle["waiting"] and _conversation_on:  # conversation mode off: no listening window opens
            idle["spoke"] = True
            wake_stop.set()

    brain.on_background_spoken = background_spoke
    brain.dictation_busy = dictation_busy
    try:
        while True:
            crashreport.note(mode=mode)
            if mode == "wake":
                idle["waiting"] = True
                try:
                    heard, restart_now = await wait_for_wake_or_restart(listener, wake_stop, brain)
                finally:
                    idle["waiting"] = False
                if restart_now and idle["dictated"] is None:  # dictated text is delivered before any restart
                    start_restart()  # flag appeared while idle: restart now, not at the next talk
                    return
                if not heard:
                    if idle["dictated"] is not None:  # a dictation finished while idle: send it like a spoken turn
                        pending_text = take_dictated(idle)  # (also releases restart_safe)
                        wake_stop.clear()
                        mode = "dictated"
                        continue
                    if idle["spoke"]:  # not a shutdown: follow-up after Jarvis's own speech
                        idle["spoke"] = False
                        wake_stop.clear()
                        if _conversation_on:  # (it may have been switched off since)
                            mode = "followup"
                        continue
                    return
                if dictation_active():  # the mic belongs to a dictation: "hey Jarvis" is just part of it
                    continue
                stop_all_speech(brain)  # "hey Jarvis" stops background, plain speech and narration at once
                mode = "command"
                continue
            if mode == "dictated":
                text, fallback = pending_text, "wake"
                pending_text = None
            else:
                text, fallback = await take_turn(mode)
            if not text:
                mode = fallback
                if mode == "followup" and not _conversation_on:  # switched off while listening
                    mode = "wake"
                if mode == "wake":
                    mode = await flush_mode(brain, listener, wd)  # woken or Jarvis spoke -> listen
                continue
            mode = await converse([submit(text)], brain=brain, speaker=speaker, listener=listener,
                                  take_turn=take_turn, submit=submit, wd=wd)
            brain.hold_background = False
            if mode == "wake":
                mode = await flush_mode(brain, listener, wd)
            if mode == "followup" and restart.restart_pending() and not plain_speaking() and not dictation_active():
                # Entering the follow-up listening state: hand over to a fresh Jarvis.
                start_restart()
                return
    finally:
        wake_stop.set()
        if _dictation is not None:
            _dictation.stop("shutdown")
            await asyncio.to_thread(_dictation.join, 3)
        _dictation = None
        if web is not None:
            await asyncio.to_thread(web.stop)
        heartbeat.cancel()
        listener.stop()
        await brain.close()


def cmd_web_token(rotate: bool):
    """`web-token`: show where the persistent web token lives; `--rotate` writes a new one."""
    from . import web as web_mod

    path = web_mod.token_file()
    if rotate:
        try:
            web_mod.rotate_token(path)
        except OSError as e:
            ui.error(f"Couldn't write the token file {path} ({type(e).__name__}).")
            return 1
        ui.status(f"New web token written to {path}. Restart Jarvis to use it and rescan the phone QR code.")
    else:
        ui.status(f"Web token file: {path} ({'exists' if path.exists() else 'created on first run'}). "
                  "`web-token --rotate` replaces it.")
    return 0


def cmd_voices(cfg):
    from .speaker import list_voices, nickname

    current = cfg["tts"]["piper"]["voice"]
    for v in list_voices():
        ui.console.print(f"{'*' if v == current else ' '} {v:40s} say \"switch to the {nickname(v)} voice\"")
    ui.status("\n* = default in config.yaml. Preview all: .\\jarvis say --all")
    ui.status("Models: say \"switch to Opus\" (or Sonnet, Haiku, Fable) while running.")


def cmd_say(cfg, text, voice=None, all_voices=False):
    from .speaker import Speaker, list_voices, nickname

    speaker = Speaker(cfg)
    speaker.muted = False  # an explicit voice test always speaks (the saved setting is left alone)
    for v in list_voices() if all_voices else [voice or speaker.voice]:
        if v and not speaker.set_voice(v):
            ui.error(f"No voice called {v}; see .\\jarvis voices")
            return
        if all_voices:
            ui.status(v)
        line = f"This is the {nickname(v)} voice. {text}" if all_voices else text
        speaker.speak(for_speech(line), threading.Event())


def main():
    p = argparse.ArgumentParser(prog="jarvis")
    p.add_argument("command", choices=["devices", "listen", "chat", "run", "say", "voices", "web-token"])
    p.add_argument("text", nargs="?", help="for say: the text to speak")
    p.add_argument("--project", help="directory Claude Code works in (default: config project_dir)")
    p.add_argument("--resume", action="store_true", help="continue the last Jarvis session")
    p.add_argument("--voice", help="for say: which voice (see `voices`)")
    p.add_argument("--all", action="store_true", help="for say: preview every voice")
    p.add_argument("--rotate", action="store_true", help="for web-token: write a new token")
    args = p.parse_args()
    crashreport.install()  # faulthandler -> logs/fatal.txt, plus the excepthooks
    crashreport.note(command=args.command)
    cfg = load_config()
    apply_speech_caps(cfg)
    project = args.project or cfg["project_dir"]

    try:
        if args.command == "devices":
            from .listener import list_devices
            list_devices()
        elif args.command == "listen":
            cmd_listen(cfg)
        elif args.command == "chat":
            asyncio.run(cmd_chat(cfg, project, args.resume))
        elif args.command == "say":
            cmd_say(cfg, args.text or "Good evening. All systems are online.", args.voice, args.all)
        elif args.command == "voices":
            cmd_voices(cfg)
        elif args.command == "web-token":
            raise SystemExit(cmd_web_token(args.rotate))
        else:
            asyncio.run(cmd_run(cfg, project, args.resume))
    except KeyboardInterrupt:
        ui.status("\nBye.")  # Ctrl+C is not a crash
    except Exception as e:
        crashreport.report(f"main-{args.command}", e, f"unhandled in command {args.command}")
        raise


if __name__ == "__main__":
    main()
