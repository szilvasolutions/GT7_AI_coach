@echo off
setlocal
title GT7 AI Coach - Windows installer

echo.
echo   ==========================================
echo      GT7 AI Coach  -  Windows installer
echo   ==========================================
echo.
echo   What you downloaded here is the SOURCE CODE.
echo   The ready-to-run app is a separate ~100 MB file.
echo.
echo   This downloads it and starts it for you.
echo   No Python, no setup. Takes about a minute.
echo.
pause

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set "rc=%errorlevel%"

echo.
if not "%rc%"=="0" (
  echo   Something went wrong ^(see the message above^).
  echo   You can always grab the zip by hand:
  echo     https://github.com/szilvasolutions/GT7_AI_coach/releases/latest
  echo   Unzip it and run  GT7Coach\GT7Coach.exe
  echo.
)
pause
exit /b %rc%
