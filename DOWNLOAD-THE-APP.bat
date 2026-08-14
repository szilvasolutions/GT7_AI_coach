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
echo   This script downloads it and starts it for you.
echo   No Python, no setup. Takes about a minute.
echo.
pause

set "GT7DEST=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $env:PSModulePath=\"$env:SystemRoot\system32\WindowsPowerShell\v1.0\Modules;$env:ProgramFiles\WindowsPowerShell\Modules\"; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $h=@{'User-Agent'='gt7coach-installer'}; $r=Invoke-RestMethod 'https://api.github.com/repos/szilvasolutions/GT7_AI_coach/releases/latest' -Headers $h -UseBasicParsing; $a=$r.assets|Where-Object{$_.name -like '*win64.zip'}|Select-Object -First 1; if(-not $a){throw 'the latest release has no win64 zip'}; $zip=Join-Path $env:TEMP $a.name; Write-Host ('Downloading ' + $a.name + ' (' + [math]::Round($a.size/1MB) + ' MB) ...'); Invoke-WebRequest $a.browser_download_url -OutFile $zip -Headers $h -UseBasicParsing; $s=$r.assets|Where-Object{$_.name -eq 'SHA256SUMS.txt'}|Select-Object -First 1; if($s){ $sums=(Invoke-WebRequest $s.browser_download_url -Headers $h -UseBasicParsing).Content; $want=(($sums -split "`n"|Where-Object{$_ -match [regex]::Escape($a.name)}|Select-Object -First 1) -split '\s+')[0]; $fs=[System.IO.File]::OpenRead($zip); try{$got=[BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash($fs)).Replace('-','').ToLower()}finally{$fs.Dispose()}; if($want -and $want -ne $got){throw ('checksum mismatch - expected ' + $want + ' but got ' + $got)}; Write-Host 'Checksum OK'; }; Write-Host 'Extracting ...'; Expand-Archive -Path $zip -DestinationPath $env:GT7DEST -Force; Remove-Item $zip -Force; Write-Host 'Done.'"

if errorlevel 1 goto fail

echo.
echo   Installed to:
echo     %GT7DEST%GT7Coach\GT7Coach.exe
echo.
echo   Starting it now. Windows will warn that the app is not
echo   signed - click "More info", then "Run anyway".
echo.
start "" "%GT7DEST%GT7Coach\GT7Coach.exe"
timeout /t 8 >nul
exit /b 0

:fail
echo.
echo   Download failed - no internet, or GitHub is blocked here.
echo   Grab the zip by hand instead:
echo.
echo     https://github.com/szilvasolutions/GT7_AI_coach/releases/latest
echo.
echo   Download GT7Coach-vX.Y.Z-win64.zip, unzip it, and run
echo   GT7Coach\GT7Coach.exe
echo.
pause
exit /b 1
