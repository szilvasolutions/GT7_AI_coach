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
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
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

[Files]
Source: "{#MySrcDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent
