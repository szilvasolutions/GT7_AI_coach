$RepoPath = "C:\GT7"
$Branch = "main"
$Interval = 30

Set-Location $RepoPath
Write-Host "Watching $RepoPath for updates from origin/$Branch every $Interval seconds..."
Write-Host "Press Ctrl+C to stop."

while ($true) {
    $ts = Get-Date -Format "HH:mm:ss"
    git fetch --quiet origin $Branch 2>$null
    $local = git rev-parse HEAD 2>$null
    $remote = git rev-parse "origin/$Branch" 2>$null

    if ($local -ne $remote) {
        Write-Host "[$ts] Pulling updates..."
        git pull --ff-only origin $Branch
    } else {
        Write-Host "[$ts] up to date"
    }

    Start-Sleep -Seconds $Interval
}
