; Установщик StreamTTS для Windows (Inno Setup 6.5+).
; usage: packaging/windows/.venv/Scripts/python.exe packaging/windows/build-exe.py --setup
;        (ISCC вручную — с теми же /D, что передаёт build-exe.py: iscc_defines, из streamtts/downloads.json)
; Входы: dist/StreamTTS/ (build-exe.py, без runtime/) и build/runtime/core/ (build-runtime.py).
; Результат: dist/StreamTTS-Setup-<версия>.exe.
; Ставится для текущего пользователя без прав администратора (по умолчанию %LOCALAPPDATA%\Programs\StreamTTS, папку
; можно выбрать любую, куда можно писать), и всё живёт в папке программы: данные — {app}\data, модели — {app}\models,
; голоса — {app}\voices (paths.py узнаёт такую установку по файлу {app}\portable). Рантайм VC++ лежит рядом с
; cosyvoice-server (build-runtime.py), поэтому vc_redist не нужен.
; Установщик ничего не качает: модель CosyVoice, CUDA и ESpeech программа качает по кнопкам на вкладке «Сервер»
; (install.py, engines/espeech/download.py), при первом запуске без модели админка открывается на ней. В
; {app}\runtime\espeech — только код espeech-server и DLL-заглушки (build-runtime.py). Язык установщика — в
; {app}\setup-lang.txt: на нём админка открывается в первый раз (i18n.py). Тексты — в [CustomMessages] на двух
; языках; сторонние лицензии — страница «Информация» (THIRD-PARTY*.txt).
; Установка поверх: {app}\runtime очищается целиком (GGML грузит все ggml-*.dll из папки, старые DLL несовместимы),
; data, models, voices и espeech-runtime не трогаются; CUDA, скачанная в программе, переносится через {app}\.cuda-keep,
; если её версия (runtime\cuda-version.txt) совпадает с downloads.json, иначе удаляется — новую предложит вкладка
; «Сервер». Удаление спрашивает, удалить ли также модели (с espeech-runtime), голоса и настройки.

#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#ifndef CudaVersion
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
WizardStyle=modern
UninstallDisplayIcon={app}\StreamTTS.exe
UninstallDisplayName=StreamTTS

[Languages]
; сторонние лицензии только перечисляются, принимать их не нужно: страница «Информация», а не соглашение
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"; InfoBeforeFile: "THIRD-PARTY.txt"
Name: "en"; MessagesFile: "compiler:Default.isl"; InfoBeforeFile: "THIRD-PARTY.en.txt"

[Messages]
; в Russian.isl «Кб», «Мб», «Гб», в остальных текстах — КБ, МБ, ГБ
ru.DiskSpaceGBLabel=Требуется как минимум [gb] ГБ свободного дискового пространства (без модели синтеза и CUDA: их программа скачает сама).
ru.DiskSpaceMBLabel=Требуется как минимум [mb] МБ свободного дискового пространства (без модели синтеза и CUDA: их программа скачает сама).
ru.DiskSpaceWarning=Установка требует не менее %1 КБ свободного места, а на выбранном вами диске доступно только %2 КБ.%n%nВы желаете тем не менее продолжить установку?
en.DiskSpaceGBLabel=At least [gb] GB of free disk space is required (not counting the speech model and CUDA, which the program downloads itself).
en.DiskSpaceMBLabel=At least [mb] MB of free disk space is required (not counting the speech model and CUDA, which the program downloads itself).

[CustomMessages]
ru.DirNotWritable=В папку %1 нельзя записывать без прав администратора (так устроены Program Files и другие системные папки). StreamTTS хранит в своей папке настройки, модели и голоса, поэтому выберите другую, например %2.
ru.DirNotEmpty=В папке %1 уже есть другие файлы. StreamTTS положит туда свои папки data, models, voices и runtime.%n%nВсё равно ставить сюда?
ru.ReadyLater=Скачивается потом, в программе (вкладка «Сервер»):
ru.ReadyModel=модель синтеза — %1–%2 МБ на выбор, с примерами звука
ru.ReadyCuda=CUDA для видеокарт NVIDIA — %1 МБ (распакованная — %2 МБ)
ru.ReadyCudaKeep=CUDA %1 уже установлена и сохранится
ru.ReadyCudaOld=CUDA прежней сборки удалится: новую (%1 МБ) программа предложит скачать
ru.UninstallData=Удалить также модели, голоса и настройки?%n%n%1%n%nОни лежат в папке программы. Если оставить, повторная установка в ту же папку их подхватит.
en.DirNotWritable=The folder %1 cannot be written without administrator rights (Program Files and other system folders work this way). StreamTTS keeps its settings, models and voices in its own folder, so choose another one, for example %2.
en.DirNotEmpty=The folder %1 already contains other files. StreamTTS will put its data, models, voices and runtime folders there.%n%nInstall here anyway?
en.ReadyLater=Downloaded later, in the program («Сервер» (Server) tab):
en.ReadyModel=speech model — %1–%2 MB of your choice, with audio samples
en.ReadyCuda=CUDA for NVIDIA graphics cards — %1 MB (%2 MB unpacked)
en.ReadyCudaKeep=CUDA %1 is already installed and will be kept
en.ReadyCudaOld=CUDA of the previous build will be removed: the program will offer to download the new one (%1 MB)
en.UninstallData=Also delete models, voices and settings?%n%n%1%n%nThey are in the program folder. If you keep them, a reinstall into the same folder will pick them up.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; runtime/ в dist кладёт только build-exe.py --runtime для проверки без установщика: здесь он берётся из build/
Source: "dist\StreamTTS\*"; DestDir: "{app}"; Excludes: "\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "build\runtime\core\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; рантайм — только из этой версии; CUDA той же версии переносится через {app}\.cuda-keep (CurStepChanged)
Type: filesandordirs; Name: "{app}\runtime"
; выбор модели в установщике до 0.2.0: модель теперь выбирают в программе
Type: files; Name: "{app}\setup-model.txt"

[UninstallDelete]
; CUDA могла прийти из .cuda-keep или из админки, cuda-version.txt и portable пишет [Code]
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\.cuda-keep"
Type: files; Name: "{app}\setup-lang.txt"
Type: files; Name: "{app}\portable"

[Icons]
Name: "{autoprograms}\StreamTTS"; Filename: "{app}\StreamTTS.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\StreamTTS"; Filename: "{app}\StreamTTS.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\StreamTTS.exe"; Description: "{cm:LaunchProgram,StreamTTS}"; Flags: nowait postinstall skipifsilent

[Code]
const
  CudaVersion = '{#CudaVersion}';
  CudaFiles = '{#CudaFiles}';

var
  DeleteData: Boolean;      // при удалении: удалить data, models, voices

// --- что уже есть в папке установки ---
function ModelInstalled: Boolean;
var
  F: TFindRec;
begin
  Result := FindFirst(ExpandConstant('{app}\models\*.gguf'), F);
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

// CUDA той же версии уже стоит: перенести через очистку runtime
function CudaKeep: Boolean;
begin
  Result := InstalledCudaVersion = CudaVersion;
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

// --- мастер ---
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
  end;
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

// Стандартная сводка плюс то, что программа скачает потом: модель (если её ещё нет) и CUDA (если есть драйвер NVIDIA)
function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
  MemoGroupInfo, MemoTasksInfo: String): String;
var
  Later, Cuda: String;
begin
  Result := '';
  AddMemo(Result, MemoUserInfoInfo, NewLine);
  AddMemo(Result, MemoDirInfo, NewLine);
  AddMemo(Result, MemoGroupInfo, NewLine);
  AddMemo(Result, MemoTasksInfo, NewLine);

  Later := '';
  if not ModelInstalled then
    Later := Later + NewLine + Space + FmtMessage(CustomMessage('ReadyModel'), ['{#ModelMinMB}', '{#ModelMaxMB}']);
  Cuda := InstalledCudaVersion;
  if CudaKeep then
    AddMemo(Result, FmtMessage(CustomMessage('ReadyCudaKeep'), [CudaVersion]), NewLine)
  else if Cuda <> '' then
    Later := Later + NewLine + Space + FmtMessage(CustomMessage('ReadyCudaOld'), ['{#CudaMB}'])
  // без драйвера NVIDIA (nvcuda.dll) CUDA бесполезна, и программа её не предлагает (install.py, cuda_state)
  else if FileExists(ExpandConstant('{sys}\nvcuda.dll')) then
    Later := Later + NewLine + Space + FmtMessage(CustomMessage('ReadyCuda'), ['{#CudaMB}', '{#CudaUnpackedMB}']);
  if Later <> '' then
    AddMemo(Result, CustomMessage('ReadyLater') + Later, NewLine);
end;

// Файлы CUDA (CudaFiles) из FromDir в ToDir (тот же диск: переименование)
procedure MoveCuda(FromDir, ToDir: String);
var
  Files, Name: String;
  P: Integer;
begin
  ForceDirectories(ToDir);
  Files := CudaFiles + ',';
  while Files <> '' do
  begin
    P := Pos(',', Files);
    Name := Copy(Files, 1, P - 1);
    Delete(Files, 1, P);
    DeleteFile(AddBackslash(ToDir) + Name);
    if not RenameFile(AddBackslash(FromDir) + Name, AddBackslash(ToDir) + Name) then
      Log('не перенесён ' + Name);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Keep: String;
begin
  Keep := ExpandConstant('{app}\.cuda-keep');
  // до [InstallDelete], который очищает runtime
  if CurStep = ssInstall then
  begin
    DelTree(Keep, True, True, True);
    if CudaKeep then
      MoveCuda(ExpandConstant('{app}\runtime'), Keep);
  end;
  if CurStep = ssPostInstall then
  begin
    if DirExists(Keep) then
    begin
      MoveCuda(Keep, ExpandConstant('{app}\runtime'));
      DelTree(Keep, True, True, True);
    end;
    if FileExists(ExpandConstant('{app}\runtime\ggml-cuda.dll')) then
      SaveStringToFile(ExpandConstant('{app}\runtime\cuda-version.txt'), CudaVersion, False);
    SaveStringToFile(ExpandConstant('{app}\portable'), 'StreamTTS: data, models, voices — в этой папке (paths.py)', False);
    SaveStringToFile(ExpandConstant('{app}\setup-lang.txt'), ActiveLanguage, False);
  end;
end;

// --- удаление ---
function DataDirs: String;
var
  Name: String;
  I: Integer;
begin
  Result := '';
  for I := 0 to 3 do
  begin
    case I of
      0: Name := 'models';
      1: Name := 'espeech-runtime';
      2: Name := 'voices';
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
    DelTree(ExpandConstant('{app}\espeech-runtime'), True, True, True);
    DelTree(ExpandConstant('{app}\voices'), True, True, True);
    DelTree(ExpandConstant('{app}\data'), True, True, True);
    RemoveDir(ExpandConstant('{app}'));
  end;
end;
