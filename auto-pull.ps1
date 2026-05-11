# auto-pull.ps1 — Watch the GT7 coach repo and pull updates from the homelab
#
# Usage:
#   1. Edit $RepoPath below to your local clone path
#   2. Right-click → Run with PowerShell, OR
#   3. Schedule via Task Scheduler at logon (recommended)
#
# Stop with Ctrl+C.

$RepoPath = "C:\GT7"      # <-- EDIT THIS
$IntervalSeconds = 30
$Branch = "main"

if (-not (Test-Path $RepoPath)) {
    Write-Host "Repo path not found: $RepoPath" -ForegroundColor Red
    Write-Host "Edit the script and set `$RepoPath to your local clone."
    exit 1
}

Set-Location $RepoPath

Write-Host "Watching $RepoPath (branch: $Branch) every ${IntervalSeconds}s" -ForegroundColor Cyan
Write-Host "Press Ctrl+C to stop.`n"

while ($true) {
    $timestamp = Get-Date -Format "HH:mm:ss"
    try {
        # Capture current HEAD
        $before = git rev-parse HEAD 2>$null

        # Fetch + pull quietly
        git fetch --quiet origin $Branch 2>$null
        $remote = git rev-parse "origin/$Branch" 2>$null

        if ($before -ne $remote) {
            Write-Host "[$timestamp] New commits found, pulling..." -ForegroundColor Yellow
            git pull --quiet --ff-only origin $Branch
            if ($LASTEXITCODE -eq 0) {
                $after = git rev-parse HEAD
                $shortSha = $after.Substring(0, 7)
                $msg = git log -1 --pretty=%s
                Write-Host "[$timestamp] Updated to $shortSha — $msg" -ForegroundColor Green
            } else {
                Write-Host "[$timestamp] Pull failed (non-fast-forward?). Check repo state." -ForegroundColor Red
            }
        } else {
            Write-Host "[$timestamp] up to date" -ForegroundColor DarkGray
        }
    } catch {
        Write-Host "[$timestamp] Error: $_" -ForegroundColor Red
    }

    Start-Sleep -Seconds $IntervalSeconds
}
