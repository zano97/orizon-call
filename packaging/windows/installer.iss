; Orizon Call — Windows installer (Inno Setup 6).
;
; Built by packaging/build.py, which passes:
;   /DAppVersion=1.2.3 /DSourceDir=<PyInstaller output> /DOutputDir=<dir>
;   /DOutputBaseFilename=OrizonCall-1.2.3-windows-x64-setup
;
; Per-user install (no administrator rights, no UAC prompt) into
; %LOCALAPPDATA%\Programs\Orizon Call: the app can then update itself
; silently by running the next installer with /VERYSILENT. The AppId is the
; installed-app identity: never change it.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\Orizon Call"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist\release"
#endif
#ifndef OutputBaseFilename
  #define OutputBaseFilename "OrizonCall-setup"
#endif

#define AppName "Orizon Call"
#define AppExe "Orizon Call.exe"

[Setup]
AppId={{6F1C2B7E-4E0A-4C55-9D2B-0B8E5A7C3D41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Orizon
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DisableDirPage=yes
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
SetupIconFile=..\..\assets\icons\orizon-call.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no
ShowLanguageDialog=no
UsedUserAreasWarning=no

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"

[Tasks]
Name: "desktopicon"; Description: "Crea un'icona sul desktop"; GroupDescription: "Icone aggiuntive:"
Name: "autostart"; Description: "Avvia Orizon Call all'accesso a Windows"; GroupDescription: "Avvio:"; Flags: unchecked

[InstallDelete]
; Files of the previous version that the new one no longer ships must not
; linger next to the new ones (mixed library versions).
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "eu.orizon.call"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon; AppUserModelID: "eu.orizon.call"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Avvia Orizon Call"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM ""{#AppExe}"" /T /F"; Flags: runhidden; RunOnceId: "StopOrizonCall"
