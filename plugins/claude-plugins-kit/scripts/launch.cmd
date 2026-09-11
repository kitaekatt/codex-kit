@echo off
setlocal
if defined CODEX_KIT_PYTHON (
  "%CODEX_KIT_PYTHON%" "%~dp0bridge.py" %*
  goto :done
)
if exist "%USERPROFILE%\.local\share\python-standalone\python\python.exe" (
  "%USERPROFILE%\.local\share\python-standalone\python\python.exe" "%~dp0bridge.py" %*
  goto :done
)
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3 "%~dp0bridge.py" %*
  goto :done
)
where python3 >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  python3 "%~dp0bridge.py" %*
  goto :done
)
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  python "%~dp0bridge.py" %*
  goto :done
)
echo Claude Plugins Kit needs Python 3.10 or newer. Set CODEX_KIT_PYTHON. 1>&2
exit /b 2

:done
exit /b %ERRORLEVEL%
