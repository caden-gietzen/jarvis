"""Terminal display: everything you say and everything Jarvis says, plus activity."""

from collections import deque
from datetime import datetime

from rich.console import Console
from rich.markdown import Markdown

from .config import LOG_DIR
from .events import bus

console = Console(highlight=False)

LOG_DIR.mkdir(exist_ok=True)
_log_path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.log"


RECENT: deque[str] = deque(maxlen=300)  # in-memory copy of recent log lines, for crash reports
LAST_YOU = ""  # the user's latest utterance


def _log(tag: str, text: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {tag}: {text}"
    RECENT.append(line[:400])
    with open(_log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def you(text: str) -> None:
    global LAST_YOU
    LAST_YOU = text
    console.print(f"\n[bold cyan]You:[/] {text}")
    _log("YOU", text)
    bus.emit("conversation", role="you", text=text)
    bus.emit("mode", mode="thinking")


def jarvis(text: str, model: str | None = None) -> None:
    console.print("[bold green]Jarvis:[/]" + (f" [dim]({model})[/]" if model else ""))
    console.print(Markdown(text))
    _log("JARVIS", text)
    bus.emit("conversation", role="jarvis", text=text, model=model)


def activity(text: str) -> None:
    console.print(f"  [dim]· {text}[/]")
    _log("ACTIVITY", text)
    bus.emit("narration", text=text, level="activity")


def status(text: str) -> None:
    console.print(f"[yellow]{text}[/]")
    bus.emit("narration", text=text, level="status")


def error(text: str) -> None:
    console.print(f"[bold red]{text}[/]")
    _log("ERROR", text)
    bus.emit("error", text=text)
