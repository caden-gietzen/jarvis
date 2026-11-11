---
name: tile-windows
description: Tile two app windows side by side (left and right halves) on the extended monitor. Use when the user asks to arrange, split, snap or move windows, e.g. "put Chrome and VS Code side by side on the other monitor". Default is Chrome left, VS Code right.
---

**Windows only** (PowerShell + Win32 calls). On macOS or Linux this skill does not work as is: there is no tested equivalent. Suggestions (untested): macOS, use AppleScript (`osascript` with System Events to set a window's position and size) or a tool like Rectangle/yabai; Linux, `wmctrl -r :ACTIVE: -e 0,x,y,w,h` or `xdotool`. If you are not on Windows, tell the user and offer to write a small script for their window manager instead of running this one.

Run the bundled script; do not rewrite the Win32 code each time.

```
powershell -NoProfile -File "$HOME\.claude\skills\tile-windows\tile.ps1" [-Left chrome] [-Right code] [-Monitor extended|primary]
```

- Parameters are Windows process names (chrome, code, msedge, Spotify, ...). Defaults: Left=chrome, Right=code, Monitor=extended (the non-primary screen; falls back to primary if there is only one).
- It restores maximized/minimized windows first, then sizes each to half the monitor's work area.
- If a process has no visible window it prints "No window found" for it; open the app first (e.g. `Start-Process code <dev folder>`) and rerun.
- If the user asks for something else (top/bottom, thirds, a different monitor), copy the script's logic and adjust the x/y/width/height math rather than starting from scratch.
- Report briefly what was placed; the user checks the result by eye, since you cannot see the screen.

