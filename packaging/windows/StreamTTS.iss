; Установщик StreamTTS для Windows (Inno Setup 6).
; usage: packaging/windows/.venv/Scripts/python.exe packaging/windows/build-exe.py --setup
;        или ISCC packaging/windows/StreamTTS.iss /DAppVersion=0.1.0 после build-runtime.py и build-exe.py
; Входы: dist/StreamTTS/ (build-exe.py, без runtime/), build/runtime/{core,cuda}/ и build/runtime/vc_redist.x64.exe
; (build-runtime.py). Результат: dist/StreamTTS-Setup-<версия>.exe.
; Программа ставится в Program Files (нужен админ: vc_redist), данные, модель и голоса живут в
; %LOCALAPPDATA%\StreamTTS (paths.py) и при удалении не трогаются. Модель в установщик не входит: она качается при
; первом запуске во вкладке «Сервер».

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6C1B7E52-3F0A-4D4B-9E61-5A2E8C0D7F31}
AppName=StreamTTS
AppVersion={#AppVersion}
AppVerName=StreamTTS {#AppVersion}
AppPublisher=StreamTTS
DefaultDirName={autopf}\StreamTTS
DefaultGroupName=StreamTTS
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; desktop.py держит мьютекс StreamTTS, пока программа запущена: установщик просит её закрыть
AppMutex=StreamTTS
OutputDir=dist
OutputBaseFilename=StreamTTS-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
UninstallDisplayIcon={app}\StreamTTS.exe
UninstallDisplayName=StreamTTS
LicenseFile=THIRD-PARTY.txt

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Types]
Name: "full"; Description: "Полная (с CUDA)"
Name: "compact"; Description: "Без CUDA (видеокарты AMD, Intel или только процессор)"
Name: "custom"; Description: "Выборочная"; Flags: iscustom

[Components]
Name: "main"; Description: "StreamTTS и cosyvoice-server (процессор и Vulkan)"; Types: full compact custom; Flags: fixed
Name: "cuda"; Description: "Ускорение на видеокартах NVIDIA (CUDA), ~660 МБ"; Types: full

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; runtime/ в dist кладёт только build-exe.py --runtime для проверки без установщика: здесь он берётся из build/
Source: "dist\StreamTTS\*"; DestDir: "{app}"; Excludes: "\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: main
Source: "build\runtime\core\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: main
Source: "build\runtime\cuda\*"; DestDir: "{app}\runtime"; Flags: ignoreversion; Components: cuda
Source: "build\runtime\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: VCRedistNeeded

[InstallDelete]
; CUDA сняли при обновлении: без ggml-cuda.dll runtime.py её и не предложит
Type: files; Name: "{app}\runtime\ggml-cuda.dll"; Components: not cuda
Type: files; Name: "{app}\runtime\cudart64_13.dll"; Components: not cuda
Type: files; Name: "{app}\runtime\cublas64_13.dll"; Components: not cuda
Type: files; Name: "{app}\runtime\cublasLt64_13.dll"; Components: not cuda

[Icons]
Name: "{autoprograms}\StreamTTS"; Filename: "{app}\StreamTTS.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\StreamTTS"; Filename: "{app}\StreamTTS.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Установка Microsoft Visual C++ Redistributable…"; Flags: waituntilterminated; Check: VCRedistNeeded
; от имени пользователя, а не админа: данные — в его %LOCALAPPDATA%
Filename: "{app}\StreamTTS.exe"; Description: "Запустить StreamTTS"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
// cosyvoice собран тулсетом VC++ 14.51: с рантаймом старше он не запустится (build-runtime.py)
function VCRedistNeeded: Boolean;
var
  Key: String;
  Installed, Major, Minor: Cardinal;
begin
  Key := 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';
  Result := True;
  if RegQueryDWordValue(HKLM64, Key, 'Installed', Installed) and (Installed = 1) and
     RegQueryDWordValue(HKLM64, Key, 'Major', Major) and RegQueryDWordValue(HKLM64, Key, 'Minor', Minor) then
    Result := (Major < 14) or ((Major = 14) and (Minor < 51));
end;

// Без драйвера NVIDIA (nvcuda.dll) CUDA бесполезна: по умолчанию не ставить
procedure InitializeWizard;
begin
  if not FileExists(ExpandConstant('{sys}\nvcuda.dll')) then
    WizardSelectComponents('!cuda');
end;
