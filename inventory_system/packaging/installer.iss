; Inno Setup script for Inventory Management System.
;
; Chosen over WiX/NSIS because the payload is a single PyInstaller onedir
; output: this needs a directory copy, shortcuts and an uninstaller, which is
; exactly Inno's sweet spot and about forty lines of it.
;
; Built by .github/workflows/windows-build.yml as:
;
;   iscc /DAppVersion=1.0.0 packaging\installer.iss
;
; Expects PyInstaller's dist\InventoryManagementSystem\ to exist already.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Inventory Management System"
#define AppPublisher "Inventory Management System"
#define AppExeName "InventoryManagementSystem.exe"
#define AppDirName "InventoryManagementSystem"

[Setup]
; A fixed GUID is what makes an upgrade an upgrade. Changing it would make
; every future release install alongside this one instead of replacing it,
; leaving two entries in Apps & Features and two Start Menu shortcuts.
AppId={{8F3D2A17-6C4B-4E59-9A1D-3B7E0C5F82D4}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppDirName}
DefaultGroupName={#AppName}
; The user gets to choose where it goes.
DisableDirPage=no
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=InventoryManagementSystemSetup-{#AppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 64-bit only, Windows 10 and later — matches what the app is tested on.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Lets a user without administrator rights install into their own profile
; rather than being stopped at the UAC prompt. Common in small businesses
; where the shop PC's day-to-day account is not an admin.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; The whole PyInstaller output directory, recursively.
Source: "..\dist\InventoryManagementSystem\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; \
    Description: "Launch {#AppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only things the *application* generated inside its own install directory.
; Deliberately absent: {userappdata}\InventoryManagementSystem and
; {localappdata}\InventoryManagementSystem. Those hold the database
; connection settings and the logs, and an upgrade uninstalls before it
; reinstalls — removing them would silently reset every machine to the
; first-run setup wizard on every update.
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    { Nothing to do: the application creates its own per-user directories on
      first launch (app.core.paths.ensure_user_dirs), which is also what
      makes it work for a second Windows user on the same machine. }
  end;
end;
