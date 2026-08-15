; Inno Setup script for GT7 AI Coach (Plan B newbie installer).
; Wraps the Nuitka --standalone "GT7Coach" folder into a familiar
; Next -> Next -> Install wizard. Per-user install so there is NO UAC
; admin prompt (one less scary popup for non-technical users).
;
; Built in CI as:
;   ISCC /DMyAppVersion=<x.y.z> /DMySrcDir=<abs path to dist\GT7Coach> gt7coach.iss
;
; NOTE: unsigned, like everything in Plan B. SmartScreen will still show
; "unknown publisher" once; that is a signing-only concern.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef MySrcDir
  #define MySrcDir "..\dist\GT7Coach"
#endif

#define MyAppName "GT7 AI Coach"
#define MyAppExeName "GT7Coach.exe"
#define MyAppPublisher "Szilva Solutions"
#define MyAppURL "https://github.com/szilvasolutions/GT7_AI_coach"

[Setup]
; Stable AppId so upgrades replace the previous install instead of stacking.
AppId={{7F3C1A2E-5B94-4E2D-9C1F-0A1B2C3D4E5F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={autopf}\GT7Coach
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
; Per-user install -> no admin/UAC prompt.
; Always per-user. Offering the admin choice would let someone install into
; Program Files, where the app cannot write config.yaml, .env or sessions\ and
; where the in-app updater cannot replace its own folder.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=GT7Coach-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[InstallDelete]
; Remove the previous build's binary payload before laying down the new one.
; ignoreversion overwrites files present in BOTH builds, but Inno never
; deletes files the new build no longer ships. A stale Qt plugin DLL left
; beside a newer Qt6Core is the classic "no Qt platform plugin could be
; initialized" crash, with nothing to suggest a clean reinstall would fix it.
;
; Deliberately NOT listed: config.yaml, .env and sessions\ — the driver's own
; files live in the install dir. Deliberately not *.exe either: unins000.exe
; sits here too, and deleting the uninstaller mid-install would be worse than
; any stale file.
Type: filesandordirs; Name: "{app}\PySide6"
Type: filesandordirs; Name: "{app}\qt-plugins"
Type: filesandordirs; Name: "{app}\platforms"
Type: filesandordirs; Name: "{app}\plugins"
Type: filesandordirs; Name: "{app}\gt7coach"
Type: files; Name: "{app}\*.pyd"
Type: files; Name: "{app}\*.dll"

[Files]
Source: "{#MySrcDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[UninstallDelete]
; After an in-app update the files on disk no longer match unins000.dat, so a
; plain uninstall left most of a ~300 MB bundle behind. Remove the binary
; payload explicitly, plus any backup folders the in-app updater parked beside
; the install.
;
; config.yaml, .env and sessions\ are intentionally left alone: they are the
; driver's own data, and silently deleting recorded sessions on uninstall is
; not a decision an installer should make. The folder is removed only if
; nothing remains in it.
Type: filesandordirs; Name: "{app}\PySide6"
Type: filesandordirs; Name: "{app}\qt-plugins"
Type: filesandordirs; Name: "{app}\platforms"
Type: filesandordirs; Name: "{app}\plugins"
Type: filesandordirs; Name: "{app}\gt7coach"
Type: filesandordirs; Name: "{app}\..\GT7Coach.bak.*"
Type: files; Name: "{app}\*.pyd"
Type: files; Name: "{app}\*.dll"
Type: files; Name: "{app}\*.exe"
Type: dirifempty; Name: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent
