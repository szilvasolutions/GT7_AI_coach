# GT7 AI Coach - downloader / installer
# Fetches the latest ready-to-run Windows build from GitHub Releases,
# verifies its checksum, unpacks it next to this script, and launches it.
# Run via DOWNLOAD-THE-APP.bat (which sets ExecutionPolicy for you).

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repo    = 'szilvasolutions/GT7_AI_coach'
$dest    = $PSScriptRoot
$headers = @{ 'User-Agent' = 'gt7coach-installer' }

try {
    Write-Host ''
    Write-Host '  Fetching latest release info ...'
    $rel = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -Headers $headers

    $asset = $rel.assets | Where-Object { $_.name -like '*win64.zip' } | Select-Object -First 1
    if (-not $asset) { throw 'The latest release has no *win64.zip asset.' }

    $zip = Join-Path $env:TEMP $asset.name
    $mb  = [math]::Round($asset.size / 1MB)
    Write-Host "  Downloading $($asset.name) ($mb MB) ..."
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip -Headers $headers

    # Optional integrity check against SHA256SUMS.txt
    $sumAsset = $rel.assets | Where-Object { $_.name -eq 'SHA256SUMS.txt' } | Select-Object -First 1
    if ($sumAsset) {
        $sums = (Invoke-WebRequest $sumAsset.browser_download_url -Headers $headers).Content
        $line = ($sums -split "`n") | Where-Object { $_ -match [regex]::Escape($asset.name) } | Select-Object -First 1
        if ($line) {
            $want = (($line -split '\s+')[0]).TrimStart('*').ToLower()
            $got  = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash.ToLower()
            if ($want -and $want -ne $got) {
                throw "Checksum mismatch - expected $want but got $got"
            }
            Write-Host '  Checksum OK.'
        }
    }

    Write-Host '  Extracting ...'
    Expand-Archive -Path $zip -DestinationPath $dest -Force
    Remove-Item $zip -Force

    $exe = Join-Path $dest 'GT7Coach\GT7Coach.exe'
    Write-Host ''
    Write-Host "  Installed to: $exe"
    if (Test-Path $exe) {
        Write-Host '  Starting it now. If Windows warns the app is unsigned,'
        Write-Host '  click "More info" then "Run anyway".'
        Start-Process -FilePath $exe
    } else {
        throw "Expected GT7Coach.exe was not found after extraction."
    }
    exit 0
}
catch {
    Write-Host ''
    Write-Host "  Download failed: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  Grab the zip by hand instead:'
    Write-Host "    https://github.com/$repo/releases/latest"
    Write-Host '  Download GT7Coach-vX.Y.Z-win64.zip, unzip it, and run GT7Coach\GT7Coach.exe'
    exit 1
}
