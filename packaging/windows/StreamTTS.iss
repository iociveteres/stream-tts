; Установщик StreamTTS для Windows (Inno Setup 6).
; usage: packaging/windows/.venv/Scripts/python.exe packaging/windows/build-exe.py --setup
;        или ISCC packaging/windows/StreamTTS.iss /DAppVersion=0.1.0 после build-runtime.py и build-exe.py
; Входы: dist/StreamTTS/ (build-exe.py, без runtime/), build/runtime/{core,cuda}/ и build/runtime/vc_redist.x64.exe
; (build-runtime.py). Результат: dist/StreamTTS-Setup-<версия>.exe.
; Программа ставится в Program Files (нужен админ: vc_redist), данные, модель и голоса живут в
; %LOCALAPPDATA%\StreamTTS (paths.py) и при удалении не трогаются. Модель в установщик не входит: вариант
; квантования выбирается на странице «Модель синтеза» (с примерами звука), пишется в {app}\setup-model.txt, и
; программа сама качает его при первом запуске (install.py, Installer.first_run); потом — вкладка «Сервер».

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
; примеры звука для страницы «Модель синтеза» (в программу они попадают через dist, это те же файлы)
Source: "..\..\stream-tts\streamtts\static\quants\*.mp3"; Flags: dontcopy

[UninstallDelete]
Type: files; Name: "{app}\setup-model.txt"

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
const
  SampleAlias = 'streamtts_sample';

var
  ModelPage: TInputOptionWizardPage;

// MCI проигрывает mp3 без сторонних библиотек (PlaySound умеет только wav)
function mciSendString(Command: String; ReturnString: String; ReturnLength: Cardinal; Callback: HWND): Cardinal;
  external 'mciSendStringW@winmm.dll stdcall';

// Порядок — как в QUANTS install.py; размеры и видеопамять — оттуда же
function ModelQuant(Index: Integer): String;
begin
  case Index of
    1: Result := 'Q6_K';
    2: Result := 'Q5_K_M';
  else
    Result := 'Q8_0';
  end;
end;

procedure StopSample;
begin
  mciSendString('close ' + SampleAlias, '', 0, 0);
end;

procedure PlaySample(Voice: String);
var
  FileName: String;
begin
  StopSample;
  FileName := Voice + '-' + ModelQuant(ModelPage.SelectedValueIndex) + '.mp3';
  ExtractTemporaryFile(FileName);
  mciSendString('open "' + ExpandConstant('{tmp}\') + FileName + '" type mpegvideo alias ' + SampleAlias, '', 0, 0);
  mciSendString('play ' + SampleAlias, '', 0, 0);
end;

procedure SampleFemaleClick(Sender: TObject);
begin
  PlaySample('irina');
end;

procedure SampleMaleClick(Sender: TObject);
begin
  PlaySample('wc3-footman');
end;

procedure SampleStopClick(Sender: TObject);
begin
  StopSample;
end;

function AddButton(Caption: String; Left, Top: Integer; OnClick: TNotifyEvent): TNewButton;
begin
  Result := TNewButton.Create(WizardForm);
  Result.Parent := ModelPage.Surface;
  Result.Caption := Caption;
  Result.Left := Left;
  Result.Top := Top;
  Result.Width := ScaleX(130);
  Result.Height := WizardForm.NextButton.Height;
  Result.OnClick := OnClick;
end;

procedure CreateModelPage;
var
  Hint: TNewStaticText;
  Stop: TNewButton;
  Top: Integer;
begin
  ModelPage := CreateInputOptionPage(wpSelectComponents, 'Модель синтеза',
    'Какой вариант модели скачать при первом запуске?',
    'Модель CosyVoice3 качается с Hugging Face при первом запуске программы, другой вариант можно скачать ' +
    'потом во вкладке «Сервер». Чем меньше файл, тем меньше видеопамяти; на слух разница едва заметна — ' +
    'послушайте примеры. Видеопамять указана для видеокарт NVIDIA, на Vulkan — примерно на 0,5 ГБ больше.',
    True, False);
  ModelPage.Add('Q8_0 — 900 МБ, видеопамять ≈1,5 ГБ (до 1,9) — рекомендуется');
  ModelPage.Add('Q6_K — 801 МБ, видеопамять ≈1,4 ГБ (до 1,8)');
  ModelPage.Add('Q5_K_M — 651 МБ, видеопамять ≈1,3 ГБ (до 1,7) — для видеокарт с 4 ГБ');
  ModelPage.SelectedValueIndex := StrToIntDef(GetPreviousData('ModelQuant', '0'), 0);
  ModelPage.CheckListBox.Height := ScaleY(66);

  Top := ModelPage.CheckListBox.Top + ModelPage.CheckListBox.Height + ScaleY(12);
  Hint := TNewStaticText.Create(ModelPage);
  Hint.Parent := ModelPage.Surface;
  Hint.Top := Top;
  Hint.Caption := 'Послушать выбранный вариант:';
  Top := Hint.Top + Hint.Height + ScaleY(6);
  AddButton('Женский голос', 0, Top, @SampleFemaleClick);
  AddButton('Мужской голос', ScaleX(138), Top, @SampleMaleClick);
  Stop := AddButton('Стоп', ScaleX(276), Top, @SampleStopClick);
  Stop.Width := ScaleX(80);
end;

// Модель уже скачана (обновление программы): выбирать нечего
function ModelInstalled: Boolean;
var
  F: TFindRec;
begin
  Result := FindFirst(ExpandConstant('{localappdata}\StreamTTS\models\*.gguf'), F);
  if Result then
    FindClose(F);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = ModelPage.ID) and ModelInstalled;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID <> ModelPage.ID then
    StopSample;
end;

procedure DeinitializeSetup;
begin
  StopSample;
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'ModelQuant', IntToStr(ModelPage.SelectedValueIndex));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SaveStringToFile(ExpandConstant('{app}\setup-model.txt'), ModelQuant(ModelPage.SelectedValueIndex), False);
end;

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
  CreateModelPage;
end;
