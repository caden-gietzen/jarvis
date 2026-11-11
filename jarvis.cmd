@echo off
rem Jarvis launcher: .\jarvis <command> [args]   (same as "python -m jarvis <command> [args]")
rem Smart App Control blocks uv's unsigned .venv\Scripts\python.exe, so start the base
rem interpreter named by "home =" in the venv's pyvenv.cfg and let bootstrap.py add the venv packages.
setlocal
set "PYHOME="
for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"home" "%~dp0.venv\pyvenv.cfg" 2^>nul') do for /f "tokens=*" %%c in ("%%b") do set "PYHOME=%%c"
if not defined PYHOME (
    echo jarvis.cmd: no "home =" line in %~dp0.venv\pyvenv.cfg. Run "uv sync" first. 1>&2
    exit /b 1
)
if not exist "%PYHOME%\python.exe" (
    echo jarvis.cmd: base Python not found: %PYHOME%\python.exe 1>&2
    exit /b 1
)
pushd "%~dp0"
"%PYHOME%\python.exe" bootstrap.py %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
