; =====================================================================
; 단아(Danha) 윈도우 설치 프로그램 — Inno Setup 스크립트
; =====================================================================
;
; 이 파일이 하는 일:
;     "다음 → 다음 → 설치" 하면 danha.exe가 설치되고,
;     어디서든 터미널에 'danha'를 치면 바로 쓸 수 있게 해줌.
;
; 사용법:
;    1. Inno Setup 다운로드 & 설치: https://jrsoftware.org/isdl.php
;    2. 먼저 build.py로 danha.exe를 만들어놓기:
;       python build.py
;    3. 이 파일(.iss)을 Inno Setup으로 열기
;    4. "Compile" 버튼 클릭
;    5. Output 폴더에 DanhaSetup.exe가 생김!
;
; 폴더 구조 (빌드 전에 이렇게 준비):
;    단아/
;    ├── dist/
;    │   └── danha.exe              ← build.py가 만든 실행 파일 (Python 전체 포함)
;    ├── danhac_dos.exe             ← 셀프호스팅 네이티브 컴파일러 (v0.88)
;    ├── danha_logo.ico             ← 개나리꽃 로고 (tools/make_logo_ico.py가 SVG에서 생성)
;    ├── danha_win32.c              ← Win32 그래픽 C 백엔드 (@clink 시 자동 빌드)
;    ├── danha_sdl2.c               ← SDL2 그래픽 C 백엔드
;    ├── danha_gl.c                 ← OpenGL 2.1 + 배치 sprite + atlas + bloom (Stage 55, 79, 81, 84, 86, 87)
;    ├── danha_audio.c              ← SDL2 native audio (Stage 80)
;    ├── danha_text.c               ← stb_truetype 텍스트 렌더링 (Stage 85)
;    ├── stb_truetype.h             ← danha_text의 의존 헤더
;    ├── danha_gfx.dh               ← Win32 그래픽 모듈 (import danha_gfx)
;    ├── danha_net.dh               ← 네트워킹 모듈 (import danha_net)
;    ├── danha_gl.dh                ← OpenGL 모듈 (import danha_gl)
;    ├── danha_audio.dh             ← 오디오 모듈 (Stage 80)
;    ├── danha_text.dh              ← 텍스트 모듈 (Stage 85)
;    ├── ari_sprite.dh              ← Ari 스프라이트/atlas/batch/bloom wrapper (Stage 69, 79, 81, 84, 87)
;    ├── ari_audio.dh               ← Ari 오디오 wrapper (Stage 80)
;    ├── ari_text.dh                ← Ari 텍스트 wrapper (Stage 85)
;    ├── ari_particle.dh            ← Ari 입자 reference template (Stage 82)
;    ├── examples/
;    │   ├── hello.dh, fibonacci.dh, shapes.dh, ecs_demo.dh
;    │   ├── error_handling.dh, memory_demo.dh, file_demo.dh, hello_window.dh
;    │   ├── sprite_demo.dh         ← Stage 69 (스프라이트)
;    │   ├── sprite_batch_demo.dh   ← Stage 79 (배치 sprite — 30배 빠름)
;    │   ├── audio_demo.dh          ← Stage 80 (오디오)
;    │   ├── particle_demo.dh       ← Stage 82+84+87 (입자 + bloom glow)
;    │   └── text_demo.dh           ← Stage 85 (텍스트 + bloom)
;    ├── danha_installer.iss        ← 이 파일
;    └── build.py
; =====================================================================

[Setup]
; 앱 기본 정보
AppName=단아 (Danha)
AppVersion=0.88.0
AppPublisher=Danha Project
AppPublisherURL=https://github.com/danha-lang
DefaultDirName={autopf}\Danha
DefaultGroupName=단아 (Danha)

; 출력 파일 설정
OutputDir=installer_output
OutputBaseFilename=DanhaSetup_v0.88.0

; 설치 프로그램 아이콘 — 개나리꽃 로고
; (danha_logo_final.svg → tools/make_logo_ico.py 로 생성한 멀티사이즈 ico)
SetupIconFile=danha_logo.ico
UninstallDisplayIcon={app}\danha_logo.ico

; 기타 설정
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; 라이센스 파일 (없으면 주석 처리)
; LicenseFile=LICENSE

; 설치 시 PATH 수정을 위해 관리자 권한 필요
PrivilegesRequired=admin
ChangesEnvironment=yes

; 설치 마법사 페이지 설정
WizardStyle=modern

[Languages]
; 한국어가 기본, 영어도 지원
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; ── 핵심 실행 파일 ────────────────────────────────────────────────
Source: "dist\danha.exe"; DestDir: "{app}"; Flags: ignoreversion

; ── 셀프호스팅 네이티브 컴파일러 (v0.88: danha selfhost가 우선 사용) ─
Source: "danhac_dos.exe"; DestDir: "{app}"; Flags: ignoreversion

; ── 로고 아이콘 — .dh/.dhs 파일 연결 아이콘으로 사용 ─────────────
Source: "danha_logo.ico"; DestDir: "{app}"; Flags: ignoreversion

; ── C 백엔드 (컴파일 시 @clink로 자동 빌드됨) ───────────────────
Source: "danha_win32.c";   DestDir: "{app}"; Flags: ignoreversion
Source: "danha_sdl2.c";    DestDir: "{app}"; Flags: ignoreversion
Source: "danha_gl.c";      DestDir: "{app}"; Flags: ignoreversion
Source: "danha_audio.c";   DestDir: "{app}"; Flags: ignoreversion
Source: "danha_text.c";    DestDir: "{app}"; Flags: ignoreversion
Source: "stb_truetype.h";  DestDir: "{app}"; Flags: ignoreversion

; ── 단아 표준 모듈 (.dh) — import 시 {app} 디렉토리에서 탐색 ───
Source: "danha_gfx.dh";    DestDir: "{app}"; Flags: ignoreversion
Source: "danha_net.dh";    DestDir: "{app}"; Flags: ignoreversion
Source: "danha_gl.dh";     DestDir: "{app}"; Flags: ignoreversion
Source: "danha_audio.dh";  DestDir: "{app}"; Flags: ignoreversion
Source: "danha_text.dh";   DestDir: "{app}"; Flags: ignoreversion

; ── Ari 엔진 모듈 (게임 친화 wrapper) ────────────────────────────
Source: "ari_sprite.dh";   DestDir: "{app}"; Flags: ignoreversion
Source: "ari_audio.dh";    DestDir: "{app}"; Flags: ignoreversion
Source: "ari_text.dh";     DestDir: "{app}"; Flags: ignoreversion
Source: "ari_particle.dh"; DestDir: "{app}"; Flags: ignoreversion

; ── 예시 파일들 — 설치 후 바로 따라해볼 수 있도록 ───────────────
Source: "examples\hello.dh";              DestDir: "{app}\examples"; Flags: ignoreversion
Source: "examples\fibonacci.dh";          DestDir: "{app}\examples"; Flags: ignoreversion
Source: "examples\shapes.dh";             DestDir: "{app}\examples"; Flags: ignoreversion
Source: "examples\ecs_demo.dh";           DestDir: "{app}\examples"; Flags: ignoreversion
Source: "examples\error_handling.dh";     DestDir: "{app}\examples"; Flags: ignoreversion
Source: "examples\memory_demo.dh";        DestDir: "{app}\examples"; Flags: ignoreversion
Source: "examples\file_demo.dh";          DestDir: "{app}\examples"; Flags: ignoreversion
Source: "examples\hello_window.dh";       DestDir: "{app}\examples"; Flags: ignoreversion
Source: "examples\sprite_demo.dh";        DestDir: "{app}\examples"; Flags: ignoreversion
Source: "examples\sprite_batch_demo.dh";  DestDir: "{app}\examples"; Flags: ignoreversion
Source: "examples\audio_demo.dh";         DestDir: "{app}\examples"; Flags: ignoreversion
Source: "examples\particle_demo.dh";      DestDir: "{app}\examples"; Flags: ignoreversion
Source: "examples\text_demo.dh";          DestDir: "{app}\examples"; Flags: ignoreversion

[Dirs]
Name: "{app}\examples"

[Icons]
; 시작 메뉴에 REPL 바로가기
Name: "{group}\단아 REPL";   Filename: "{app}\danha.exe"; Parameters: "repl";   Comment: "단아 대화형 모드"
Name: "{group}\씬 에디터";   Filename: "{app}\danha.exe"; Parameters: "editor"; Comment: "단아 씬 에디터"
Name: "{group}\예시 파일";   Filename: "{app}\examples";  Comment: "단아 예시 코드"
Name: "{group}\단아 제거";   Filename: "{uninstallexe}";  Comment: "단아 제거"

[Registry]
; .dh 파일 연결 — 더블클릭하면 단아로 실행, 아이콘은 개나리꽃 로고
Root: HKCR; Subkey: ".dh";                           ValueType: string; ValueData: "DanhaFile";       Flags: uninsdeletevalue
Root: HKCR; Subkey: "DanhaFile";                     ValueType: string; ValueData: "단아 소스 파일"; Flags: uninsdeletekey
Root: HKCR; Subkey: "DanhaFile\DefaultIcon";         ValueType: string; ValueData: "{app}\danha_logo.ico"
Root: HKCR; Subkey: "DanhaFile\shell\open\command";  ValueType: string; ValueData: """{app}\danha.exe"" run ""%1"""
; .dhs 파일 연결 — 씬 파일은 에디터로 열기 (같은 로고)
Root: HKCR; Subkey: ".dhs";                          ValueType: string; ValueData: "DanhaSceneFile";  Flags: uninsdeletevalue
Root: HKCR; Subkey: "DanhaSceneFile";                ValueType: string; ValueData: "단아 씬 파일";   Flags: uninsdeletekey
Root: HKCR; Subkey: "DanhaSceneFile\DefaultIcon";    ValueType: string; ValueData: "{app}\danha_logo.ico"
Root: HKCR; Subkey: "DanhaSceneFile\shell\open\command"; ValueType: string; ValueData: """{app}\danha.exe"" editor ""%1"""
; 우클릭 메뉴: "단아로 실행"
Root: HKCR; Subkey: "DanhaFile\shell\run_danha";         ValueType: string; ValueData: "단아로 실행"
Root: HKCR; Subkey: "DanhaFile\shell\run_danha\command"; ValueType: string; ValueData: """{app}\danha.exe"" run ""%1"""

[Code]
// =====================================================================
// PATH 환경 변수에 단아를 추가/제거하는 파스칼 스크립트
// =====================================================================
//
// 이게 왜 필요한가:
//   PATH란 "이 이름의 프로그램이 어디 있는지" 컴퓨터에게 알려주는 목록이야.
//   여기에 C:\Program Files\Danha 를 추가해야
//   어디서든 'danha run hello.dh' 같은 명령이 작동해.
//   자바 설치하면 java 명령이 바로 되는 것도 같은 원리야.
//
// Inno Setup은 자체 스크립트 언어(Pascal Script)를 사용함.
// 아래 코드는 설치할 때 PATH에 추가하고, 제거할 때 빼주는 거야.

const
    EnvironmentKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';
    SHCNE_ASSOCCHANGED = $08000000;

// 파일 연결/아이콘 변경을 탐색기에 즉시 반영 (안 하면 재부팅 전까지 옛 아이콘 캐시가 보임)
procedure SHChangeNotify(wEventId: Integer; uFlags: Cardinal; dwItem1, dwItem2: Cardinal);
    external 'SHChangeNotify@shell32.dll stdcall';

procedure AddToPath(Dir: string);
var
    OldPath: string;
begin
    // 현재 PATH 값을 읽어옴
    if not RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', OldPath) then
        OldPath := '';

    // 이미 들어있으면 안 건드림
    if Pos(Uppercase(Dir), Uppercase(OldPath)) > 0 then
        Exit;

    // 끝에 세미콜론 + 우리 경로 추가
    if (OldPath <> '') and (OldPath[Length(OldPath)] <> ';') then
        OldPath := OldPath + ';';
    OldPath := OldPath + Dir;

    // 레지스트리에 저장
    RegWriteStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', OldPath);
end;

procedure RemoveFromPath(Dir: string);
var
    OldPath, NewPath: string;
    P: Integer;
begin
    if not RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', OldPath) then
        Exit;

    // 우리 경로 찾기
    P := Pos(Uppercase(Dir), Uppercase(OldPath));
    if P = 0 then
        Exit;

    // 경로 제거
    NewPath := Copy(OldPath, 1, P - 1) + Copy(OldPath, P + Length(Dir), MaxInt);

    // 남은 세미콜론 정리
    while (Length(NewPath) > 0) and (NewPath[1] = ';') do
        NewPath := Copy(NewPath, 2, MaxInt);
    while (Length(NewPath) > 0) and (NewPath[Length(NewPath)] = ';') do
        NewPath := Copy(NewPath, 1, Length(NewPath) - 1);
    StringChangeEx(NewPath, ';;', ';', True);

    RegWriteStringValue(HKEY_LOCAL_MACHINE, EnvironmentKey, 'Path', NewPath);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
    // 설치 완료 후 PATH에 추가 + 아이콘/파일연결 캐시 갱신
    if CurStep = ssPostInstall then
    begin
        AddToPath(ExpandConstant('{app}'));
        SHChangeNotify(SHCNE_ASSOCCHANGED, 0, 0, 0);
    end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
    // 제거 시 PATH에서 삭제
    if CurUninstallStep = usPostUninstall then
        RemoveFromPath(ExpandConstant('{app}'));
end;

[Run]
; 설치 완료 후 — 버전 확인
Filename: "{app}\danha.exe"; Parameters: "version"; \
    Description: "단아 버전 확인"; Flags: postinstall nowait skipifsilent runhidden

[UninstallDelete]
; 제거 시 examples 폴더 + 빌드 중간 파일(.o) 정리
Type: filesandordirs; Name: "{app}\examples"
Type: files; Name: "{app}\*.o"
