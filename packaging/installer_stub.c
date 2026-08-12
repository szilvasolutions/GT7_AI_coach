/* GT7 AI Coach — tiny installer stub (INSTALL-GT7-COACH.exe).
 *
 * Committed to the repo root as a real .exe so that someone who clicks
 * GitHub's green "Code > Download ZIP" button finds something they can
 * double-click. The app itself is ~95 MB and lives in GitHub Releases;
 * git would keep every version of it forever, so this ~30 KB stub goes
 * in the repo instead and fetches the real thing on demand.
 *
 * All it does: confirm with the user, drop a PowerShell script in %TEMP%,
 * and run it. The script is the same one !CLICK-ME-TO-INSTALL.bat runs —
 * resolve the latest release through the GitHub API, download the win64
 * bundle, check it against SHA256SUMS.txt, unpack it next to this exe and
 * launch it. Keep the two in sync.
 *
 * Build (from the repo root, on Linux with mingw-w64):
 *   x86_64-w64-mingw32-gcc -O2 -s -mwindows -municode \
 *       -o INSTALL-GT7-COACH.exe packaging/installer_stub.c -lshell32
 *
 * CI rebuilds and verifies this on every release; see release.yml.
 */

/* windows.h first — shellapi.h depends on its typedefs. */
#include <windows.h>

#include <shellapi.h>
#include <stdio.h>

#define APP_TITLE L"GT7 AI Coach installer"

/* Written to %TEMP% and handed to powershell -File. Kept as one string so
 * it stays diffable against the .bat. */
static const wchar_t *PS_SCRIPT =
    L"$ErrorActionPreference='Stop'\n"
    L"$ProgressPreference='SilentlyContinue'\n"
    L"[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12\n"
    L"$dest='%s'\n"
    L"Write-Host ''\n"
    L"Write-Host '  GT7 AI Coach - downloading the app' -ForegroundColor Cyan\n"
    L"Write-Host ''\n"
    L"try {\n"
    L"  $h=@{'User-Agent'='gt7coach-installer'}\n"
    L"  $r=Invoke-RestMethod "
    L"'https://api.github.com/repos/szilvasolutions/GT7_AI_coach/releases/latest' -Headers $h\n"
    L"  $a=$r.assets|Where-Object{$_.name -like '*win64.zip'}|Select-Object -First 1\n"
    L"  if(-not $a){throw 'the latest release has no win64 zip'}\n"
    L"  $zip=Join-Path $env:TEMP $a.name\n"
    L"  Write-Host ('  Downloading ' + $a.name + ' (' + [math]::Round($a.size/1MB) + ' MB) ...')\n"
    L"  Invoke-WebRequest $a.browser_download_url -OutFile $zip -Headers $h\n"
    L"  $s=$r.assets|Where-Object{$_.name -eq 'SHA256SUMS.txt'}|Select-Object -First 1\n"
    L"  if($s){\n"
    L"    $sums=(Invoke-WebRequest $s.browser_download_url -Headers $h).Content\n"
    L"    $want=($sums -split \"`n\"|Where-Object{$_ -match [regex]::Escape($a.name)}|"
    L"Select-Object -First 1) -split '\\s+'|Select-Object -First 1\n"
    L"    $got=(Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()\n"
    L"    if($want -and $want -ne $got){throw ('checksum mismatch - expected ' + $want + "
    L"' but got ' + $got)}\n"
    L"    Write-Host '  Checksum OK' -ForegroundColor Green\n"
    L"  }\n"
    L"  Write-Host '  Extracting ...'\n"
    L"  Expand-Archive -Path $zip -DestinationPath $dest -Force\n"
    L"  Remove-Item $zip -Force\n"
    L"  $exe=Join-Path $dest 'GT7Coach\\GT7Coach.exe'\n"
    L"  if(-not (Test-Path $exe)){throw 'the zip did not contain GT7Coach.exe'}\n"
    L"  Write-Host ''\n"
    L"  Write-Host ('  Installed: ' + $exe) -ForegroundColor Green\n"
    L"  Write-Host '  Starting it now. Windows will warn that the app is not signed -'\n"
    L"  Write-Host '  click \"More info\", then \"Run anyway\".'\n"
    L"  Start-Process $exe\n"
    L"  Start-Sleep -Seconds 6\n"
    L"} catch {\n"
    L"  Write-Host ''\n"
    L"  Write-Host ('  Install failed: ' + $_.Exception.Message) -ForegroundColor Red\n"
    L"  Write-Host ''\n"
    L"  Write-Host '  Download it by hand instead:'\n"
    L"  Write-Host '  https://github.com/szilvasolutions/GT7_AI_coach/releases/latest'\n"
    L"  Write-Host ''\n"
    L"  Read-Host '  Press Enter to close'\n"
    L"  exit 1\n"
    L"}\n";

/* Directory this exe lives in, with the trailing backslash removed. */
static BOOL exe_dir(wchar_t *out, DWORD cch) {
    DWORD n = GetModuleFileNameW(NULL, out, cch);
    if (n == 0 || n >= cch) return FALSE;
    wchar_t *slash = wcsrchr(out, L'\\');
    if (!slash) return FALSE;
    *slash = L'\0';
    return TRUE;
}

/* /S skips the confirmation dialog so CI can run the whole thing headless. */
static BOOL silent(PWSTR args) {
    if (!args) return FALSE;
    for (const wchar_t *p = args; *p; p++) {
        if ((*p == L'/' || *p == L'-') && (p[1] == L'S' || p[1] == L's')) return TRUE;
    }
    return FALSE;
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, PWSTR args, int show) {
    (void)inst; (void)prev; (void)show;

    if (!silent(args) &&
        MessageBoxW(NULL,
                    L"This downloads GT7 AI Coach (about 95 MB) from its official "
                    L"GitHub release page, unpacks it into this folder and starts it.\n\n"
                    L"Nothing is written anywhere else and nothing is added to startup.\n\n"
                    L"Continue?",
                    APP_TITLE, MB_ICONQUESTION | MB_OKCANCEL | MB_DEFBUTTON1) != IDOK) {
        return 0;
    }

    wchar_t dir[MAX_PATH];
    if (!exe_dir(dir, MAX_PATH)) {
        MessageBoxW(NULL, L"Could not work out where this program is running from.",
                    APP_TITLE, MB_ICONERROR | MB_OK);
        return 1;
    }

    wchar_t tmp[MAX_PATH];
    if (GetTempPathW(MAX_PATH, tmp) == 0) {
        MessageBoxW(NULL, L"Could not find the temp folder.", APP_TITLE, MB_ICONERROR | MB_OK);
        return 1;
    }
    /* _snwprintf leaves the buffer unterminated when it truncates, so bail
     * on a negative return and terminate by hand otherwise. */
    wchar_t script[MAX_PATH];
    if (_snwprintf(script, MAX_PATH, L"%sgt7coach-install.ps1", tmp) < 0) {
        MessageBoxW(NULL, L"Temp path too long.", APP_TITLE, MB_ICONERROR | MB_OK);
        return 1;
    }
    script[MAX_PATH - 1] = L'\0';

    FILE *f = _wfopen(script, L"w, ccs=UTF-8");
    if (!f) {
        MessageBoxW(NULL, L"Could not write the install script to the temp folder.",
                    APP_TITLE, MB_ICONERROR | MB_OK);
        return 1;
    }
    fwprintf(f, PS_SCRIPT, dir);
    fclose(f);

    wchar_t params[MAX_PATH + 64];
    if (_snwprintf(params, MAX_PATH + 64, L"-NoProfile -ExecutionPolicy Bypass -File \"%s\"",
                   script) < 0) {
        MessageBoxW(NULL, L"Temp path too long.", APP_TITLE, MB_ICONERROR | MB_OK);
        return 1;
    }
    params[MAX_PATH + 63] = L'\0';

    HINSTANCE rc = ShellExecuteW(NULL, L"open", L"powershell.exe", params, dir, SW_SHOWNORMAL);
    if ((INT_PTR)rc <= 32) {
        MessageBoxW(NULL,
                    L"Could not start PowerShell.\n\n"
                    L"Download the app by hand instead:\n"
                    L"https://github.com/szilvasolutions/GT7_AI_coach/releases/latest",
                    APP_TITLE, MB_ICONERROR | MB_OK);
        return 1;
    }
    return 0;
}
