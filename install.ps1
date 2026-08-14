# GT7 AI Coach - downloader / installer
# Fetches the latest ready-to-run Windows build from GitHub Releases and
# runs it. Prefers the one-click Setup wizard; falls back to the portable zip.
# Run via DOWNLOAD-THE-APP.bat (which sets ExecutionPolicy for you).

# -UseBasicParsing on every web call: without it PowerShell 5.1 hands the
# response to the Internet Explorer engine, which throws on any machine where
# IE's first-run setup never completed. That is most fresh Windows installs,
# and it silently broke the previous downloader.
$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repo    = 'szilvasolutions/GT7_AI_coach'
$dest    = $PSScriptRoot
$headers = @{ 'User-Agent' = 'gt7coach-installer' }

function Get-WantedHash($rel, $name) {
    $sumAsset = $rel.assets | Where-Object { $_.name -eq 'SHA256SUMS.txt' } | Select-Object -First 1
    if (-not $sumAsset) { return $null }
    $sums = (Invoke-WebRequest $sumAsset.browser_download_url -Headers $headers -UseBasicParsing).Content
    $line = ($sums -split "`n") | Where-Object { $_ -match [regex]::Escape($name) } | Select-Object -First 1
    if (-not $line) { return $null }
    return (($line -split '\s+')[0]).TrimStart('*').ToLower()
}

try {
    Write-Host ''
    Write-Host '  Fetching latest release info ...'
    $rel = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -Headers $headers -UseBasicParsing

    # Prefer the one-click installer wizard; fall back to the portable zip.
    $setup = $rel.assets | Where-Object { $_.name -like '*Setup*.exe' } | Select-Object -First 1
    $zipA  = $rel.assets | Where-Object { $_.name -like '*win64.zip' }  | Select-Object -First 1
    $asset = if ($setup) { $setup } elseif ($zipA) { $zipA } else { $null }
    if (-not $asset) { throw 'The latest release has no Setup.exe or win64.zip asset.' }

    $out = Join-Path $env:TEMP $asset.name
    $mb  = [math]::Round($asset.size / 1MB)
    Write-Host "  Downloading $($asset.name) ($mb MB) ..."
    Invoke-WebRequest $asset.browser_download_url -OutFile $out -Headers $headers -UseBasicParsing

    $want = Get-WantedHash $rel $asset.name
    if ($want) {
        $got = (Get-FileHash -Algorithm SHA256 -Path $out).Hash.ToLower()
        if ($want -ne $got) { throw "Checksum mismatch - expected $want but got $got" }
        Write-Host '  Checksum OK.'
    }

    if ($asset.name -like '*.exe') {
        # Installer wizard: just launch it.
        Write-Host ''
        Write-Host '  Starting the installer. If Windows shows a blue'
        Write-Host '  "unknown publisher" box, click "More info" then "Run anyway".'
        Start-Process -FilePath $out
    }
    else {
        # Portable zip: extract next to this script and launch.
        Write-Host '  Extracting ...'
        Expand-Archive -Path $out -DestinationPath $dest -Force
        Remove-Item $out -Force
        $exe = Join-Path $dest 'GT7Coach\GT7Coach.exe'
        if (-not (Test-Path $exe)) { throw 'GT7Coach.exe not found after extraction.' }
        Write-Host ''
        Write-Host "  Installed to: $exe"
        Write-Host '  Starting it now. If Windows warns the app is unsigned,'
        Write-Host '  click "More info" then "Run anyway".'
        Start-Process -FilePath $exe
    }
    exit 0
}
catch {
    Write-Host ''
    Write-Host "  Download failed: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  Grab it by hand instead:'
    Write-Host "    https://github.com/$repo/releases/latest"
    Write-Host '  Download GT7Coach-Setup-*.exe (or the -win64.zip), then run it.'
    exit 1
}
