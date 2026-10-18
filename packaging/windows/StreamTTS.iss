; Установщик StreamTTS для Windows (Inno Setup 6).
; usage: packaging/windows/.venv/Scripts/python.exe packaging/windows/build-exe.py --setup
;        или ISCC packaging/windows/StreamTTS.iss /DAppVersion=0.1.0 после build-runtime.py и build-exe.py
; Входы: dist/StreamTTS/ (build-exe.py, без runtime/), build/runtime/{core,cuda}/ и build/runtime/vc_redist.x64.exe
; (build-runtime.py). Результат: dist/StreamTTS-Setup-<версия>.exe.
; Программа ставится в Program Files (нужен админ: vc_redist), данные, модель и голоса живут в
; %LOCALAPPDATA%\StreamTTS (paths.py) и при удалении не трогаются. Модель в установщик не входит: вариант
; квантования выбирается на странице «Модель синтеза» (с примерами звука), пишется в {app}\setup-model.txt, и
; программа сама качает его при первом запуске (install.py, Installer.first_run); потом — вкладка «Сервер».
; Язык установщика пишется в {app}\setup-lang.txt: на нём админка открывается в первый раз (i18n.py).
; Тексты — в [CustomMessages] на двух языках; сторонние лицензии — страница «Информация» (THIRD-PARTY*.txt).

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

[Languages]
; сторонние лицензии только перечисляются, принимать их не нужно: страница «Информация», а не соглашение
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"; InfoBeforeFile: "THIRD-PARTY.txt"
Name: "en"; MessagesFile: "compiler:Default.isl"; InfoBeforeFile: "THIRD-PARTY.en.txt"

[Messages]
; в Russian.isl «Кб», «Мб», «Гб», в остальных текстах — КБ, МБ, ГБ
ru.DiskSpaceGBLabel=Требуется как минимум [gb] ГБ свободного дискового пространства.
ru.DiskSpaceMBLabel=Требуется как минимум [mb] МБ свободного дискового пространства.
ru.DiskSpaceWarning=Установка требует не менее %1 КБ свободного места, а на выбранном вами диске доступно только %2 КБ.%n%nВы желаете тем не менее продолжить установку?
ru.ComponentSize1=%1 КБ
ru.ComponentSize2=%1 МБ
ru.ComponentsDiskSpaceGBLabel=Текущий выбор требует не менее [gb] ГБ на диске.
ru.ComponentsDiskSpaceMBLabel=Текущий выбор требует не менее [mb] МБ на диске.

[CustomMessages]
ru.TypeFull=Полная (с CUDA)
ru.TypeCompact=Без CUDA (видеокарты AMD, Intel или только процессор)
ru.TypeCustom=Выборочная
ru.ComponentMain=StreamTTS и cosyvoice-server (процессор и Vulkan)
ru.ComponentCuda=Ускорение на видеокартах NVIDIA (CUDA)
ru.VCRedistStatus=Установка Microsoft Visual C++ Redistributable…
ru.ModelCaption=Модель синтеза
ru.ModelDescription=Какой вариант модели скачать при первом запуске?
ru.ModelSubCaption=Модель CosyVoice3 качается с Hugging Face при первом запуске программы, другой вариант можно скачать потом во вкладке «Сервер». Чем меньше файл, тем меньше видеопамяти; на слух разница едва заметна — послушайте примеры. Видеопамять указана для видеокарт NVIDIA, на Vulkan — примерно на 0,5 ГБ больше.
ru.ModelQ8=Q8_0 — 900 МБ, видеопамять ≈1,5 ГБ (до 1,9) — рекомендуется
ru.ModelQ6=Q6_K — 801 МБ, видеопамять ≈1,4 ГБ (до 1,8)
ru.ModelQ5=Q5_K_M — 651 МБ, видеопамять ≈1,3 ГБ (до 1,7) — для видеокарт с 4 ГБ
ru.ModelListen=Послушать выбранный вариант:
ru.ModelPlay=Пример
ru.ModelStop=Стоп
ru.ReadyModel=Модель синтеза (скачается при первом запуске):
en.TypeFull=Full (with CUDA)
en.TypeCompact=Without CUDA (AMD or Intel graphics, or CPU only)
en.TypeCustom=Custom
en.ComponentMain=StreamTTS and cosyvoice-server (CPU and Vulkan)
en.ComponentCuda=NVIDIA GPU acceleration (CUDA)
en.VCRedistStatus=Installing Microsoft Visual C++ Redistributable…
en.ModelCaption=Speech Model
en.ModelDescription=Which model variant should be downloaded on first launch?
en.ModelSubCaption=The CosyVoice3 model is downloaded from Hugging Face when the program first starts; another variant can be downloaded later on the «Сервер» (Server) tab. A smaller file needs less video memory, and the difference is barely audible — listen to the samples. Video memory is given for NVIDIA cards; with Vulkan it is about 0.5 GB more.
en.ModelQ8=Q8_0 — 900 MB, video memory ≈1.5 GB (up to 1.9) — recommended
en.ModelQ6=Q6_K — 801 MB, video memory ≈1.4 GB (up to 1.8)
en.ModelQ5=Q5_K_M — 651 MB, video memory ≈1.3 GB (up to 1.7) — for 4 GB graphics cards
en.ModelListen=Listen to the selected variant:
en.ModelPlay=Sample
en.ModelStop=Stop
en.ReadyModel=Speech model (downloaded on first launch):

[Types]
Name: "full"; Description: "{cm:TypeFull}"
Name: "compact"; Description: "{cm:TypeCompact}"
Name: "custom"; Description: "{cm:TypeCustom}"; Flags: iscustom

[Components]
Name: "main"; Description: "{cm:ComponentMain}"; Types: full compact custom; Flags: fixed
; размер (~660 МБ) Inno показывает в колонке справа
Name: "cuda"; Description: "{cm:ComponentCuda}"; Types: full

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
Type: files; Name: "{app}\setup-lang.txt"

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
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "{cm:VCRedistStatus}"; Flags: waituntilterminated; Check: VCRedistNeeded
; от имени пользователя, а не админа: данные — в его %LOCALAPPDATA%
Filename: "{app}\StreamTTS.exe"; Description: "{cm:LaunchProgram,StreamTTS}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
const
  SampleAlias = 'streamtts_sample';
  // голос примеров — SAMPLE_VOICE в install.py
  SampleVoice = 'ohf-denis';

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

procedure SamplePlayClick(Sender: TObject);
begin
  PlaySample(SampleVoice);
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
  ModelPage := CreateInputOptionPage(wpSelectComponents, CustomMessage('ModelCaption'),
    CustomMessage('ModelDescription'), CustomMessage('ModelSubCaption'), True, False);
  ModelPage.Add(CustomMessage('ModelQ8'));
  ModelPage.Add(CustomMessage('ModelQ6'));
  ModelPage.Add(CustomMessage('ModelQ5'));
  ModelPage.SelectedValueIndex := StrToIntDef(GetPreviousData('ModelQuant', '0'), 0);
  ModelPage.CheckListBox.Height := ScaleY(66);

  Top := ModelPage.CheckListBox.Top + ModelPage.CheckListBox.Height + ScaleY(12);
  Hint := TNewStaticText.Create(ModelPage);
  Hint.Parent := ModelPage.Surface;
  Hint.Top := Top;
  Hint.Caption := CustomMessage('ModelListen');
  Top := Hint.Top + Hint.Height + ScaleY(6);
  AddButton(CustomMessage('ModelPlay'), 0, Top, @SamplePlayClick);
  Stop := AddButton(CustomMessage('ModelStop'), ScaleX(138), Top, @SampleStopClick);
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

procedure AddMemo(var Memo: String; Info, NewLine: String);
begin
  if Info <> '' then
  begin
    // разделы — через пустую строку, как в стандартной сводке
    if Memo <> '' then
      Memo := Memo + NewLine + NewLine;
    Memo := Memo + Info;
  end;
end;

// Стандартная сводка плюс выбранная модель (страницу модели пропустили — модель уже скачана, писать нечего)
function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
  MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := '';
  AddMemo(Result, MemoUserInfoInfo, NewLine);
  AddMemo(Result, MemoDirInfo, NewLine);
  AddMemo(Result, MemoTypeInfo, NewLine);
  AddMemo(Result, MemoComponentsInfo, NewLine);
  if not ModelInstalled then
    AddMemo(Result, CustomMessage('ReadyModel') + NewLine + Space +
      ModelPage.CheckListBox.ItemCaption[ModelPage.SelectedValueIndex], NewLine);
  AddMemo(Result, MemoGroupInfo, NewLine);
  AddMemo(Result, MemoTasksInfo, NewLine);
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'ModelQuant', IntToStr(ModelPage.SelectedValueIndex));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    SaveStringToFile(ExpandConstant('{app}\setup-model.txt'), ModelQuant(ModelPage.SelectedValueIndex), False);
    SaveStringToFile(ExpandConstant('{app}\setup-lang.txt'), ActiveLanguage, False);
  end;
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
