; Установщик StreamTTS для Windows (Inno Setup 6.5+: ExtractArchive, загрузки с проверкой sha256).
; usage: packaging/windows/.venv/Scripts/python.exe packaging/windows/build-exe.py --setup
;        (ISCC вручную — с теми же /D, что передаёт build-exe.py: iscc_defines, из streamtts/downloads.json)
; Входы: dist/StreamTTS/ (build-exe.py, без runtime/) и build/runtime/core/ (build-runtime.py).
; Результат: dist/StreamTTS-Setup-<версия>.exe.
; Ставится для текущего пользователя без прав администратора (по умолчанию %LOCALAPPDATA%\Programs\StreamTTS, папку
; можно выбрать любую, куда можно писать), и всё живёт в папке программы: данные — {app}\data, модели — {app}\models,
; голоса — {app}\voices (paths.py узнаёт такую установку по файлу {app}\portable). Рантайм VC++ лежит рядом с
; cosyvoice-server (build-runtime.py), поэтому vc_redist не нужен.
; CUDA (~550 МБ, распакованная ~660 МБ) и модель в установщик не входят: что отмечено в компонентах, качается на
; странице загрузки после «Готово к установке» (адреса и sha256 — из downloads.json). CUDA — два архива из релиза
; llama.cpp той же сборки, что и ядро GGML: они распаковываются во временную папку, а в {app}\runtime ставятся
; только нужные DLL (CudaFiles). Модель кладётся в {app}\models. Квант выбирается на странице «Модель синтеза» (с примерами звука) и
; пишется в {app}\setup-model.txt: если загрузку пропустили или она не удалась, программа скачает его сама при
; первом запуске (install.py, Installer.first_run). Язык установщика — в {app}\setup-lang.txt: на нём админка
; открывается в первый раз (i18n.py). Тексты — в [CustomMessages] на двух языках; сторонние лицензии — страница
; «Информация» (THIRD-PARTY*.txt).
; Установка поверх: {app}\runtime очищается целиком (GGML грузит все ggml-*.dll из папки, старые DLL несовместимы),
; data, models и voices не трогаются; CUDA той же версии (runtime\cuda-version.txt) не качается заново, а
; переносится через {app}\.cuda-keep. Удаление спрашивает, удалить ли также модели, голоса и настройки.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef Cuda0Url
  #error Нужны /D из downloads.json: собирайте через build-exe.py --setup
#endif

[Setup]
AppId={{6C1B7E52-3F0A-4D4B-9E61-5A2E8C0D7F31}
AppName=StreamTTS
AppVersion={#AppVersion}
AppVerName=StreamTTS {#AppVersion}
AppPublisher=StreamTTS
DefaultDirName={userpf}\StreamTTS
DefaultGroupName=StreamTTS
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; desktop.py держит мьютекс StreamTTS, пока программа запущена: установщик и деинсталлятор просят её закрыть
AppMutex=StreamTTS
OutputDir=dist
OutputBaseFilename=StreamTTS-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
LZMANumBlockThreads=4
; архивы CUDA — zip
ArchiveExtraction=full
WizardStyle=modern
UninstallDisplayIcon={app}\StreamTTS.exe
UninstallDisplayName=StreamTTS

[Languages]
; сторонние лицензии только перечисляются, принимать их не нужно: страница «Информация», а не соглашение
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"; InfoBeforeFile: "THIRD-PARTY.txt"
Name: "en"; MessagesFile: "compiler:Default.isl"; InfoBeforeFile: "THIRD-PARTY.en.txt"

[Messages]
; в Russian.isl «Кб», «Мб», «Гб», в остальных текстах — КБ, МБ, ГБ
ru.DiskSpaceGBLabel=Требуется как минимум [gb] ГБ свободного дискового пространства (вместе со скачиваемыми CUDA и моделью).
ru.DiskSpaceMBLabel=Требуется как минимум [mb] МБ свободного дискового пространства (вместе со скачиваемыми CUDA и моделью).
ru.DiskSpaceWarning=Установка требует не менее %1 КБ свободного места, а на выбранном вами диске доступно только %2 КБ.%n%nВы желаете тем не менее продолжить установку?
ru.ComponentSize1=%1 КБ
ru.ComponentSize2=%1 МБ
ru.ComponentsDiskSpaceGBLabel=Текущий выбор требует не менее [gb] ГБ на диске.
ru.ComponentsDiskSpaceMBLabel=Текущий выбор требует не менее [mb] МБ на диске.
en.DiskSpaceGBLabel=At least [gb] GB of free disk space is required (including the downloaded CUDA and model).
en.DiskSpaceMBLabel=At least [mb] MB of free disk space is required (including the downloaded CUDA and model).

[CustomMessages]
ru.TypeFull=Полная (с CUDA)
ru.TypeCompact=Без CUDA (видеокарты AMD, Intel или только процессор)
ru.TypeCustom=Выборочная
ru.ComponentMain=StreamTTS и cosyvoice-server (процессор и Vulkan)
ru.ComponentCuda=Ускорение на видеокартах NVIDIA (CUDA) — скачать %1 МБ
ru.ComponentModel=Модель синтеза — скачать (вариант на следующей странице)
ru.ModelCaption=Модель синтеза
ru.ModelDescription=Какой вариант модели скачать?
ru.ModelSubCaption=Модель CosyVoice3 скачается с Hugging Face во время установки (если загрузка не удастся, программа скачает её сама при первом запуске), другой вариант можно скачать потом во вкладке «Сервер». Чем меньше файл, тем меньше видеопамяти; на слух разница едва заметна — послушайте примеры. Видеопамять указана для видеокарт NVIDIA, на Vulkan — примерно на 0,5 ГБ больше.
ru.ModelQ8=Q8_0 — 900 МБ, видеопамять ≈1,5 ГБ (до 1,9) — рекомендуется
ru.ModelQ6=Q6_K — 801 МБ, видеопамять ≈1,4 ГБ (до 1,8)
ru.ModelQ5=Q5_K_M — 651 МБ, видеопамять ≈1,3 ГБ (до 1,7) — для видеокарт с 4 ГБ
ru.ModelListen=Послушать выбранный вариант:
ru.ModelPlay=Пример
ru.ModelStop=Стоп
ru.DirNotWritable=В папку %1 нельзя записывать без прав администратора (так устроены Program Files и другие системные папки). StreamTTS хранит в своей папке настройки, модели и голоса, поэтому выберите другую, например %2.
ru.DirNotEmpty=В папке %1 уже есть другие файлы. StreamTTS положит туда свои папки data, models, voices и runtime.%n%nВсё равно ставить сюда?
ru.ReadyModel=Модель синтеза:
ru.ReadyDownloads=Будет скачано:
ru.ReadyCudaKeep=CUDA %1 уже установлена: заново не качается
ru.ReadyCudaItem=CUDA %1 — %2 МБ (распакованная — %3 МБ)
ru.ReadyModelItem=модель %1 — %2 МБ
ru.ReadyModelHave=модель %1 уже скачана
ru.ReadySpace=Место на диске:
ru.ReadySpaceApp=в папке программы — около %1 МБ
ru.ReadySpaceTmp=во временной папке на время установки — ещё %1 МБ
ru.SpaceWarning=Для загрузки нужно %1 МБ во временной папке (%2), а свободно только %3 МБ.%n%nВсё равно продолжить?
ru.DownloadFailed=Не удалось скачать: %1
ru.DownloadAborted=Загрузка прервана
ru.DownloadCuda=CUDA
ru.DownloadModel=модель %1
ru.DownloadRetry=Повторить
ru.DownloadSkip=Продолжить без этого
ru.DownloadSkipCuda=Без CUDA программа работает через Vulkan или на процессоре; скачать CUDA можно потом во вкладке «Сервер».
ru.CudaExtractFailed=Не удалось распаковать CUDA: программа будет работать через Vulkan или на процессоре, скачать CUDA можно потом во вкладке «Сервер».
ru.DownloadSkipModel=Программа скачает модель сама при первом запуске.
ru.UninstallData=Удалить также модели, голоса и настройки?%n%n%1%n%nОни лежат в папке программы. Если оставить, повторная установка в ту же папку их подхватит.
en.TypeFull=Full (with CUDA)
en.TypeCompact=Without CUDA (AMD or Intel graphics, or CPU only)
en.TypeCustom=Custom
en.ComponentMain=StreamTTS and cosyvoice-server (CPU and Vulkan)
en.ComponentCuda=NVIDIA GPU acceleration (CUDA) — download %1 MB
en.ComponentModel=Speech model — download (variant on the next page)
en.ModelCaption=Speech Model
en.ModelDescription=Which model variant should be downloaded?
en.ModelSubCaption=The CosyVoice3 model is downloaded from Hugging Face during installation (if the download fails, the program downloads it on first launch); another variant can be downloaded later on the «Сервер» (Server) tab. A smaller file needs less video memory, and the difference is barely audible — listen to the samples. Video memory is given for NVIDIA cards; with Vulkan it is about 0.5 GB more.
en.ModelQ8=Q8_0 — 900 MB, video memory ≈1.5 GB (up to 1.9) — recommended
en.ModelQ6=Q6_K — 801 MB, video memory ≈1.4 GB (up to 1.8)
en.ModelQ5=Q5_K_M — 651 MB, video memory ≈1.3 GB (up to 1.7) — for 4 GB graphics cards
en.ModelListen=Listen to the selected variant:
en.ModelPlay=Sample
en.ModelStop=Stop
en.DirNotWritable=The folder %1 cannot be written without administrator rights (Program Files and other system folders work this way). StreamTTS keeps its settings, models and voices in its own folder, so choose another one, for example %2.
en.DirNotEmpty=The folder %1 already contains other files. StreamTTS will put its data, models, voices and runtime folders there.%n%nInstall here anyway?
en.ReadyModel=Speech model:
en.ReadyDownloads=To be downloaded:
en.ReadyCudaKeep=CUDA %1 is already installed and will not be downloaded again
en.ReadyCudaItem=CUDA %1 — %2 MB (%3 MB unpacked)
en.ReadyModelItem=model %1 — %2 MB
en.ReadyModelHave=model %1 is already downloaded
en.ReadySpace=Disk space:
en.ReadySpaceApp=in the program folder — about %1 MB
en.ReadySpaceTmp=in the temporary folder during installation — another %1 MB
en.SpaceWarning=The download needs %1 MB in the temporary folder (%2), but only %3 MB is free.%n%nContinue anyway?
en.DownloadFailed=Could not download: %1
en.DownloadAborted=Download aborted
en.DownloadCuda=CUDA
en.DownloadModel=model %1
en.DownloadRetry=Retry
en.DownloadSkip=Continue without it
en.DownloadSkipCuda=Without CUDA the program uses Vulkan or the CPU; CUDA can be downloaded later on the «Сервер» (Server) tab.
en.CudaExtractFailed=Could not unpack CUDA: the program will use Vulkan or the CPU; CUDA can be downloaded later on the «Сервер» (Server) tab.
en.DownloadSkipModel=The program will download the model itself on first launch.
en.UninstallData=Also delete models, voices and settings?%n%n%1%n%nThey are in the program folder. If you keep them, a reinstall into the same folder will pick them up.

[Types]
Name: "full"; Description: "{cm:TypeFull}"
Name: "compact"; Description: "{cm:TypeCompact}"
Name: "custom"; Description: "{cm:TypeCustom}"; Flags: iscustom

[Components]
Name: "main"; Description: "{cm:ComponentMain}"; Types: full compact custom; Flags: fixed
; размер в колонке справа — на диске после распаковки (ExternalSize в [Files])
Name: "cuda"; Description: "{cm:ComponentCuda,{#CudaSizeMB}}"; Types: full
Name: "model"; Description: "{cm:ComponentModel}"; Types: full compact

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; runtime/ в dist кладёт только build-exe.py --runtime для проверки без установщика: здесь он берётся из build/
Source: "dist\StreamTTS\*"; DestDir: "{app}"; Excludes: "\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: main
Source: "build\runtime\core\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: main
; Скачанное на странице загрузки (NextButtonClick): нет файла — загрузку пропустили. ExternalSize — для расчёта
; места: CUDA в распакованном виде, модель — самый большой вариант (Q8_0). В {tmp}\cuda — только DLL CUDA (PrepareCuda)
Source: "{tmp}\cuda\*"; DestDir: "{app}\runtime"; ExternalSize: {#CudaUnpacked}; Flags: external ignoreversion skipifsourcedoesntexist; Components: cuda
Source: "{tmp}\{code:ModelFile}"; DestDir: "{app}\models"; ExternalSize: {#Model0Size}; Flags: external ignoreversion skipifsourcedoesntexist uninsneveruninstall; Components: model
; примеры звука для страницы «Модель синтеза» (в программу они попадают через dist, это те же файлы)
Source: "..\..\stream-tts\streamtts\static\quants\*.mp3"; Flags: dontcopy

[InstallDelete]
; рантайм — только из этой версии; CUDA той же версии переносится через {app}\.cuda-keep (CurStepChanged)
Type: filesandordirs; Name: "{app}\runtime"

[UninstallDelete]
; CUDA могла прийти из .cuda-keep или из админки, cuda-version.txt и portable пишет [Code]
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\.cuda-keep"
Type: files; Name: "{app}\setup-model.txt"
Type: files; Name: "{app}\setup-lang.txt"
Type: files; Name: "{app}\portable"

[Icons]
Name: "{autoprograms}\StreamTTS"; Filename: "{app}\StreamTTS.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\StreamTTS"; Filename: "{app}\StreamTTS.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\StreamTTS.exe"; Description: "{cm:LaunchProgram,StreamTTS}"; Flags: nowait postinstall skipifsilent

[Code]
const
  SampleAlias = 'streamtts_sample';
  // голос примеров — SAMPLE_VOICE в install.py
  SampleVoice = 'ohf-denis';
  CudaVersion = '{#CudaVersion}';
  CudaFiles = '{#CudaFiles}';
  MB = 1048576;

var
  ModelPage: TInputOptionWizardPage;
  DownloadPage: TDownloadWizardPage;
  ExtractionPage: TExtractionWizardPage;
  CheckedDir: String;       // папка, для которой выставлен выбор модели по умолчанию
  DeleteData: Boolean;      // при удалении: удалить data, models, voices

// MCI проигрывает mp3 без сторонних библиотек (PlaySound умеет только wav)
function mciSendString(Command: String; ReturnString: String; ReturnLength: Cardinal; Callback: HWND): Cardinal;
  external 'mciSendStringW@winmm.dll stdcall';

// Порядок — как в QUANTS install.py и downloads.json (build-exe.py передаёт Model0..2)
function ModelQuant(Index: Integer): String;
begin
  case Index of
    1: Result := '{#Model1}';
    2: Result := '{#Model2}';
  else
    Result := '{#Model0}';
  end;
end;

function ModelSize(Index: Integer): Int64;
begin
  case Index of
    1: Result := {#Model1Size};
    2: Result := {#Model2Size};
  else
    Result := {#Model0Size};
  end;
end;

function ModelSha256(Index: Integer): String;
begin
  case Index of
    1: Result := '{#Model1Sha256}';
    2: Result := '{#Model2Sha256}';
  else
    Result := '{#Model0Sha256}';
  end;
end;

// Имя файла модели — как model_name() в install.py
function ModelFile(Param: String): String;
begin
  Result := 'CosyVoice3-2512_' + ModelQuant(ModelPage.SelectedValueIndex) + '.gguf';
end;

function SizeMB(Bytes: Int64): String;
begin
  Result := IntToStr((Bytes + MB div 2) div MB);
end;

// --- страница «Модель синтеза» ---
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

// --- что уже есть в папке установки ---
// Модель уже скачана (обновление программы): по умолчанию не качать
function ModelInstalledIn(Dir: String): Boolean;
var
  F: TFindRec;
begin
  Result := FindFirst(AddBackslash(Dir) + 'models\*.gguf', F);
  if Result then
    FindClose(F);
end;

function InstalledCudaVersion: String;
var
  S: AnsiString;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\runtime\ggml-cuda.dll')) and
     LoadStringFromFile(ExpandConstant('{app}\runtime\cuda-version.txt'), S) then
    Result := Trim(String(S));
end;

// CUDA той же версии уже стоит: не качать, а перенести через очистку runtime
function CudaKeep: Boolean;
begin
  Result := WizardIsComponentSelected('cuda') and (InstalledCudaVersion = CudaVersion);
end;

function CudaNeedsDownload: Boolean;
begin
  Result := WizardIsComponentSelected('cuda') and not CudaKeep;
end;

function ModelNeedsDownload: Boolean;
begin
  Result := WizardIsComponentSelected('model') and not FileExists(ExpandConstant('{app}\models\') + ModelFile(''));
end;

// --- выбор папки: можно ли писать (Program Files — нельзя без админа) ---
function DirWritable(Dir: String): Boolean;
var
  Created: TArrayOfString;
  P, Parent, Test: String;
  I, N: Integer;
begin
  // папки, которых ещё нет, создаются для проверки и потом убираются: каталог создаст сама установка
  P := RemoveBackslashUnlessRoot(Dir);
  N := 0;
  while not DirExists(P) do
  begin
    SetArrayLength(Created, N + 1);
    Created[N] := P;
    N := N + 1;
    Parent := ExtractFileDir(P);
    if (Parent = P) or (Parent = '') then
      Break;
    P := Parent;
  end;
  Result := ForceDirectories(Dir);
  if Result then
  begin
    Test := AddBackslash(Dir) + '.streamtts-write-test';
    Result := SaveStringToFile(Test, 'test', False);
    if Result then
      DeleteFile(Test);
  end;
  for I := 0 to N - 1 do
    RemoveDir(Created[I]);
end;

// В папке что-то есть, но не StreamTTS: предупредить (удаление потом заберёт data, models и voices оттуда)
function DirForeign(Dir: String): Boolean;
var
  F: TFindRec;
begin
  Result := False;
  if not DirExists(Dir) or FileExists(AddBackslash(Dir) + 'StreamTTS.exe') then
    Exit;
  if FindFirst(AddBackslash(Dir) + '*', F) then
    try
      repeat
        if (F.Name <> '.') and (F.Name <> '..') then
        begin
          Result := True;
          Break;
        end;
      until not FindNext(F);
    finally
      FindClose(F);
    end;
end;

// --- загрузка ---
// Одна загрузка: при ошибке — «Повторить / Продолжить без этого / Отмена». False — пользователь отменил установку
function DownloadOne(Url, BaseName, Sha256, Name, SkipHint: String): Boolean;
var
  Error: String;
  Labels: TArrayOfString;
  Answer: Integer;
begin
  Result := True;
  if FileExists(ExpandConstant('{tmp}\') + BaseName) then
    Exit;  // уже скачан и проверен (вернулись назад и снова «Установить»)
  SetArrayLength(Labels, 2);
  Labels[0] := CustomMessage('DownloadRetry');
  Labels[1] := CustomMessage('DownloadSkip');
  repeat
    DownloadPage.Clear;
    DownloadPage.Add(Url, BaseName, Sha256);
    try
      DownloadPage.Download;
      Exit;
    except
      if DownloadPage.AbortedByUser then
        Error := CustomMessage('DownloadAborted')
      else
        Error := GetExceptionMessage;
      Log(Format('%s: %s', [BaseName, Error]));
    end;
    Answer := SuppressibleTaskDialogMsgBox(FmtMessage(CustomMessage('DownloadFailed'), [Name]),
      AddPeriod(Error) + #13#10#13#10 + SkipHint, mbError, MB_YESNOCANCEL, Labels, 0, IDNO);
  until Answer <> IDYES;
  Result := Answer = IDNO;
end;

function DriveFreeMB(Path: String): Int64;
var
  Free, Total: Int64;
begin
  if GetSpaceOnDisk64(ExtractFileDrive(Path), Free, Total) then
    Result := Free div MB
  else
    Result := -1;
end;

function DownloadSize: Int64;
begin
  Result := 0;
  if CudaNeedsDownload then
    Result := Result + {#CudaSize};
  if ModelNeedsDownload then
    Result := Result + ModelSize(ModelPage.SelectedValueIndex);
end;

// Сколько нужно во временной папке: загрузки плюс архивы CUDA в распакованном виде (до установки лежат и те, и те)
function TempSize: Int64;
begin
  Result := DownloadSize;
  if CudaNeedsDownload then
    Result := Result + {#Cuda0Unpacked} + {#Cuda1Unpacked};
end;

// Архивы CUDA распаковываются целиком (ExtractArchive иначе не умеет), в {tmp}\cuda переносятся только CudaFiles,
// остальное (программы llama.cpp) и сами архивы удаляются. False — не вышло, CUDA не ставится
function PrepareCuda: Boolean;
var
  Files, Name, Dir: String;
  P: Integer;
begin
  Dir := ExpandConstant('{tmp}\');
  ExtractionPage.Clear;
  ExtractionPage.Add(Dir + '{#Cuda0Name}', Dir + 'cuda0', False);
  ExtractionPage.Add(Dir + '{#Cuda1Name}', Dir + 'cuda1', False);
  ExtractionPage.Show;
  try
    try
      ExtractionPage.Extract;
      Result := True;
    except
      Log('распаковка CUDA: ' + GetExceptionMessage);
      Result := False;
    end;
  finally
    ExtractionPage.Hide;
  end;
  ForceDirectories(Dir + 'cuda');
  Files := CudaFiles + ',';
  while Result and (Files <> '') do
  begin
    P := Pos(',', Files);
    Name := Copy(Files, 1, P - 1);
    Delete(Files, 1, P);
    Result := RenameFile(Dir + 'cuda0\' + Name, Dir + 'cuda\' + Name) or
      RenameFile(Dir + 'cuda1\' + Name, Dir + 'cuda\' + Name);
    if not Result then
      Log('нет ' + Name + ' в архивах CUDA');
  end;
  DelTree(Dir + 'cuda0', True, True, True);
  DelTree(Dir + 'cuda1', True, True, True);
  DeleteFile(Dir + '{#Cuda0Name}');
  DeleteFile(Dir + '{#Cuda1Name}');
  if not Result then
    DelTree(Dir + 'cuda', True, True, True);
end;

// Обе части CUDA: без любой из них она бесполезна
function DownloadCuda: Boolean;
begin
  Result := True;
  if FileExists(ExpandConstant('{tmp}\cuda\ggml-cuda.dll')) then
    Exit;  // уже скачана и разложена (вернулись назад и снова «Установить»)
  if not DownloadOne('{#Cuda0Url}', '{#Cuda0Name}', '{#Cuda0Sha256}', CustomMessage('DownloadCuda'),
      CustomMessage('DownloadSkipCuda')) then
    Result := False
  else if FileExists(ExpandConstant('{tmp}\{#Cuda0Name}')) and not DownloadOne('{#Cuda1Url}', '{#Cuda1Name}',
      '{#Cuda1Sha256}', CustomMessage('DownloadCuda'), CustomMessage('DownloadSkipCuda')) then
    Result := False;
  // «Продолжить без этого» на любой части — без CUDA
  if Result and FileExists(ExpandConstant('{tmp}\{#Cuda0Name}')) and FileExists(ExpandConstant('{tmp}\{#Cuda1Name}')) then
  begin
    DownloadPage.Hide;
    if not PrepareCuda then
      SuppressibleMsgBox(CustomMessage('CudaExtractFailed'), mbError, MB_OK, IDOK);
    DownloadPage.Show;
  end
  else
  begin
    DeleteFile(ExpandConstant('{tmp}\{#Cuda0Name}'));
    DeleteFile(ExpandConstant('{tmp}\{#Cuda1Name}'));
  end;
end;

function DownloadAll: Boolean;
var
  Need, Free: Int64;
begin
  Result := True;
  if DownloadSize = 0 then
    Exit;
  Need := (TempSize + MB - 1) div MB;
  Free := DriveFreeMB(ExpandConstant('{tmp}'));
  // строка в [Code] не может начинаться с «[»: Inno принимает её за заголовок секции
  if (Free >= 0) and (Free < Need) and (SuppressibleMsgBox(FmtMessage(CustomMessage('SpaceWarning'), [IntToStr(Need),
      ExpandConstant('{tmp}'), IntToStr(Free)]), mbError, MB_YESNO, IDYES) <> IDYES) then
  begin
    Result := False;
    Exit;
  end;
  DownloadPage.Show;
  try
    if CudaNeedsDownload then
      Result := DownloadCuda;
    if Result and ModelNeedsDownload then
      Result := DownloadOne('{#ModelUrl}' + ModelFile(''), ModelFile(''), ModelSha256(ModelPage.SelectedValueIndex),
        FmtMessage(CustomMessage('DownloadModel'), [ModelQuant(ModelPage.SelectedValueIndex)]),
        CustomMessage('DownloadSkipModel'));
  finally
    DownloadPage.Hide;
  end;
end;

// --- мастер ---
// Компоненты заданы в командной строке (/COMPONENTS=…): выбор по умолчанию их не трогает
function ComponentsFromCmdLine: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if Pos('/COMPONENTS=', Uppercase(ParamStr(I))) = 1 then
      Result := True;
end;

// Модель по умолчанию качается, если её в выбранной папке ещё нет. Перед страницей компонентов, а не после
// выбора папки: при обновлении страницу папки мастер пропускает
procedure DefaultModel;
begin
  if ComponentsFromCmdLine or (CompareText(WizardDirValue, CheckedDir) = 0) then
    Exit;
  CheckedDir := WizardDirValue;
  if ModelInstalledIn(CheckedDir) then
    WizardSelectComponents('!model')
  else
    WizardSelectComponents('model');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  if PageID = wpSelectComponents then
    DefaultModel;
  Result := (PageID = ModelPage.ID) and not WizardIsComponentSelected('model');
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID <> ModelPage.ID then
    StopSample;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Dir := WizardDirValue;
    if not DirWritable(Dir) then
    begin
      SuppressibleMsgBox(FmtMessage(CustomMessage('DirNotWritable'), [Dir, 'D:\StreamTTS']), mbError, MB_OK, IDOK);
      Result := False;
    end
    else if DirForeign(Dir) and (SuppressibleMsgBox(FmtMessage(CustomMessage('DirNotEmpty'), [Dir]), mbConfirmation,
        MB_YESNO or MB_DEFBUTTON2, IDYES) <> IDYES) then
      Result := False;
  end
  else if CurPageID = wpReady then
    Result := DownloadAll;
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

// Стандартная сводка плюс модель, загрузки и место на диске
function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
  MemoGroupInfo, MemoTasksInfo: String): String;
var
  Downloads: String;
  AppBytes: Int64;
  Q: Integer;
begin
  Q := ModelPage.SelectedValueIndex;
  Result := '';
  AddMemo(Result, MemoUserInfoInfo, NewLine);
  AddMemo(Result, MemoDirInfo, NewLine);
  AddMemo(Result, MemoTypeInfo, NewLine);
  AddMemo(Result, MemoComponentsInfo, NewLine);
  if WizardIsComponentSelected('model') then
    AddMemo(Result, CustomMessage('ReadyModel') + NewLine + Space + ModelPage.CheckListBox.ItemCaption[Q], NewLine);

  Downloads := '';
  AppBytes := {#MainSize};
  if CudaKeep then
    Downloads := Downloads + NewLine + Space + FmtMessage(CustomMessage('ReadyCudaKeep'), [CudaVersion])
  else if CudaNeedsDownload then
    Downloads := Downloads + NewLine + Space + FmtMessage(CustomMessage('ReadyCudaItem'), [CudaVersion,
      SizeMB({#CudaSize}), SizeMB({#CudaUnpacked})]);
  if WizardIsComponentSelected('cuda') then
    AppBytes := AppBytes + {#CudaUnpacked};
  if ModelNeedsDownload then
  begin
    Downloads := Downloads + NewLine + Space + FmtMessage(CustomMessage('ReadyModelItem'), [ModelQuant(Q),
      SizeMB(ModelSize(Q))]);
    AppBytes := AppBytes + ModelSize(Q);
  end
  else if WizardIsComponentSelected('model') then
    Downloads := Downloads + NewLine + Space + FmtMessage(CustomMessage('ReadyModelHave'), [ModelQuant(Q)]);
  if Downloads <> '' then
    AddMemo(Result, CustomMessage('ReadyDownloads') + Downloads, NewLine);
  Downloads := CustomMessage('ReadySpace') + NewLine + Space +
    FmtMessage(CustomMessage('ReadySpaceApp'), [SizeMB(AppBytes)]);
  if TempSize > 0 then
    Downloads := Downloads + NewLine + Space + FmtMessage(CustomMessage('ReadySpaceTmp'), [SizeMB(TempSize)]);
  AddMemo(Result, Downloads, NewLine);

  AddMemo(Result, MemoGroupInfo, NewLine);
  AddMemo(Result, MemoTasksInfo, NewLine);
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'ModelQuant', IntToStr(ModelPage.SelectedValueIndex));
end;

// Файлы CUDA из runtime в Dir и обратно (тот же диск: переименование)
procedure MoveCuda(FromDir, ToDir: String);
var
  F: TFindRec;
begin
  ForceDirectories(ToDir);
  if FindFirst(AddBackslash(FromDir) + '*', F) then
    try
      repeat
        if (F.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0 then
        begin
          DeleteFile(AddBackslash(ToDir) + F.Name);
          if not RenameFile(AddBackslash(FromDir) + F.Name, AddBackslash(ToDir) + F.Name) then
            Log('не перенесён ' + F.Name);
        end;
      until not FindNext(F);
    finally
      FindClose(F);
    end;
end;

procedure KeepCuda;
var
  Keep: String;
begin
  Keep := ExpandConstant('{app}\.cuda-keep');
  DelTree(Keep, True, True, True);
  ForceDirectories(Keep);
  RenameFile(ExpandConstant('{app}\runtime\ggml-cuda.dll'), Keep + '\ggml-cuda.dll');
  RenameFile(ExpandConstant('{app}\runtime\cudart64_13.dll'), Keep + '\cudart64_13.dll');
  RenameFile(ExpandConstant('{app}\runtime\cublas64_13.dll'), Keep + '\cublas64_13.dll');
  RenameFile(ExpandConstant('{app}\runtime\cublasLt64_13.dll'), Keep + '\cublasLt64_13.dll');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Keep: String;
begin
  Keep := ExpandConstant('{app}\.cuda-keep');
  // до [InstallDelete], который очищает runtime
  if (CurStep = ssInstall) and CudaKeep then
    KeepCuda;
  if CurStep = ssPostInstall then
  begin
    if DirExists(Keep) then
    begin
      if WizardIsComponentSelected('cuda') then
        MoveCuda(Keep, ExpandConstant('{app}\runtime'));
      DelTree(Keep, True, True, True);
    end;
    if FileExists(ExpandConstant('{app}\runtime\ggml-cuda.dll')) then
      SaveStringToFile(ExpandConstant('{app}\runtime\cuda-version.txt'), CudaVersion, False);
    SaveStringToFile(ExpandConstant('{app}\portable'), 'StreamTTS: data, models, voices — в этой папке (paths.py)', False);
    if WizardIsComponentSelected('model') then
      SaveStringToFile(ExpandConstant('{app}\setup-model.txt'), ModelQuant(ModelPage.SelectedValueIndex), False);
    SaveStringToFile(ExpandConstant('{app}\setup-lang.txt'), ActiveLanguage, False);
  end;
end;

// Без драйвера NVIDIA (nvcuda.dll) CUDA бесполезна: по умолчанию не ставить
procedure InitializeWizard;
begin
  if not FileExists(ExpandConstant('{sys}\nvcuda.dll')) and not ComponentsFromCmdLine then
    WizardSelectComponents('!cuda');
  CreateModelPage;
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), nil);
  DownloadPage.ShowBaseNameInsteadOfUrl := True;
  ExtractionPage := CreateExtractionPage(SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), nil);
end;

// --- удаление ---
function DataDirs: String;
var
  Name: String;
  I: Integer;
begin
  Result := '';
  for I := 0 to 2 do
  begin
    case I of
      0: Name := 'models';
      1: Name := 'voices';
    else
      Name := 'data';
    end;
    if DirExists(ExpandConstant('{app}\') + Name) then
    begin
      if Result <> '' then
        Result := Result + #13#10;
      Result := Result + ExpandConstant('{app}\') + Name;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // спросить до удаления файлов; по умолчанию — оставить (тихое удаление тоже оставляет)
  if (CurUninstallStep = usUninstall) and (DataDirs <> '') then
    DeleteData := SuppressibleMsgBox(FmtMessage(CustomMessage('UninstallData'), [DataDirs]), mbConfirmation,
      MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES;
  if (CurUninstallStep = usPostUninstall) and DeleteData then
  begin
    DelTree(ExpandConstant('{app}\models'), True, True, True);
    DelTree(ExpandConstant('{app}\voices'), True, True, True);
    DelTree(ExpandConstant('{app}\data'), True, True, True);
    RemoveDir(ExpandConstant('{app}'));
  end;
end;
