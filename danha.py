#!/usr/bin/env python3
"""
단아(Danha) — 게임 개발 특화 프로그래밍 언어

사용법:
    danha run <파일.dh>                    인터프리터로 실행
    danha run --watch <파일.dh>            파일 변경 감지 후 자동 재실행 (50단계)
    danha compile <파일.dh>               네이티브 컴파일 (LLVM)
    danha compile --watch [--run] <파일.dh> 네이티브 변경 감지 빌드/재시작
    danha compile --target wasm <파일.dh>  WebAssembly 컴파일 (54단계)
    danha compile --target ios <파일.dh>   iOS 프로젝트 생성 (57단계)
    danha compile --target android <파일.dh> Android 프로젝트 생성 (57단계)
    danha test <파일.dh>                  테스트 블록 실행 (48단계)
    danha doc <파일.dh>                   문서 생성 (49단계)
    danha profile <파일.dh>               프로파일링 실행 (51단계)
    danha debug <파일.dh>                 디버거로 실행 (53단계)
    danha shader <파일.dh>                GLSL 셰이더 변환 (56단계)
    danha editor [<씬파일.dhs>]           씬 에디터 실행 (58단계)
    danha pkg <init|add|list>            패키지 매니저 (47단계)
    danha repl                            대화형 모드
    danha check <파일.dh>                 구문 검사
    danha check --compile <파일.dh>       컴파일/타입 검사 (링크/실행 없음)
    danha check --standalone <파일.dh>    코어 독립성 검사
    danha version                         버전 정보
"""

import sys
import os
import re

DANHA_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DANHA_DIR)

VERSION = "0.88.0"
VERSION_NAME = "단아 (Danha)"


def print_banner():
    print(f"""
╔══════════════════════════════════════════╗
║   단아 (Danha) v{VERSION}                ║
║   게임 개발 특화 프로그래밍 언어          ║
║   Easy to Start, Hard to Master          ║
╚══════════════════════════════════════════╝
""")


# ===== 기본 명령 =====

def cmd_run(filepath, watch=False):
    """인터프리터로 .dh 파일 실행. --watch면 파일 변경 시 자동 재실행."""
    if not os.path.exists(filepath):
        print(f"❌ 파일을 찾을 수 없어: {filepath}")
        return 1

    if watch:
        return cmd_run_watch(filepath)

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()
    except Exception as e:
        print(f"❌ 파일 읽기 실패: {e}")
        return 1

    try:
        from danha_evaluator import run
        run(source, base_dir=os.path.dirname(os.path.abspath(filepath)))
        return 0
    except Exception as e:
        print(f"❌ {e}")
        return 1


def cmd_selfhost(args):
    """59단계: 셀프 호스팅 컴파일러 실행.
    danha selfhost <source.dh> [--runtime <mode>] [--out <name>]

    기본값은 이미 부트스트랩된 네이티브 danhac 실행 파일을 사용하는 경로다.
    --bootstrap-python을 넘기면 Python 인터프리터로 danhac.dh를 실행한다.
    """
    if not args:
        print("❌ 컴파일할 파일을 지정해줘: danha selfhost <파일.dh>")
        return 1

    force_python = '--bootstrap-python' in args
    args = [a for a in args if a != '--bootstrap-python']

    # clang이 PATH에 없으면 사용자 로컬 LLVM 경로 자동 추가
    import platform
    if platform.system() == 'Windows':
        llvm_bin = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'LLVM', 'bin')
        if os.path.isdir(llvm_bin) and llvm_bin not in os.environ.get('PATH', ''):
            os.environ['PATH'] = llvm_bin + os.pathsep + os.environ.get('PATH', '')

    if not force_python:
        native_names = (
            'danhac_dos.exe',
            'danhac_stage3.exe',
            'danhac_stage2.exe',
            'danhac.exe',
        ) if platform.system() == 'Windows' else (
            'danhac_dlx',
            'danhac',
        )
        for name in native_names:
            native_path = os.path.join(DANHA_DIR, name)
            if os.path.exists(native_path):
                import subprocess
                print(f"danha selfhost: native compiler → {name}")
                try:
                    return subprocess.call([native_path] + args, cwd=DANHA_DIR)
                except OSError as e:
                    print(f"⚠️  네이티브 selfhost 실행 실패, Python 부트스트랩으로 전환: {e}")
                    break

    danhac_path = os.path.join(DANHA_DIR, 'danhac.dh')
    if not os.path.exists(danhac_path):
        print(f"❌ danhac.dh 를 찾을 수 없어: {danhac_path}")
        return 1

    try:
        with open(danhac_path, 'r', encoding='utf-8') as f:
            source = f.read()
    except Exception as e:
        print(f"❌ danhac.dh 읽기 실패: {e}")
        return 1

    try:
        print("danha selfhost: Python bootstrap → danhac.dh")
        from danha_evaluator import run
        run(source,
            base_dir=DANHA_DIR,
            script_args=args)
        return 0
    except SystemExit as e:
        return e.code if e.code is not None else 0
    except Exception as e:
        print(f"❌ {e}")
        return 1


def cmd_run_watch(filepath):
    """50단계: 파일 변경 감지 후 자동 재실행."""
    import time

    abs_path = os.path.abspath(filepath)
    mtimes = {}

    print(f"👁  감시 중: {filepath}  (Ctrl+C로 종료)")

    def _snapshot():
        snap = {}
        for p in _scan_dh_dependencies(abs_path):
            try:
                snap[p] = os.path.getmtime(p)
            except OSError:
                snap[p] = 0
        return snap

    def _run_once():
        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                source = f.read()
            from danha_evaluator import run
            run(source, base_dir=os.path.dirname(abs_path))
        except Exception as e:
            print(f"❌ {e}")

    try:
        while True:
            new_mtimes = _snapshot()
            changed = [p for p, m in new_mtimes.items() if mtimes.get(p) != m]
            if changed:
                if mtimes:
                    names = ', '.join(os.path.basename(p) for p in changed[:4])
                    if len(changed) > 4:
                        names += f" 외 {len(changed) - 4}개"
                    print(f"\n🔄 변경 감지 — 재실행: {names}")
                mtimes = new_mtimes
                _run_once()

            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n👋 감시 종료")
        return 0


def cmd_compile(filepath, extra_args=None, target=None, runtime_mode='libc', backend='auto'):
    """LLVM 네이티브 컴파일. --target wasm 시 WebAssembly 출력.
    backend='clang'이면 llvmlite 바인딩 대신 텍스트 .ll + 외부 clang 경로."""
    if not os.path.exists(filepath):
        print(f"❌ 파일을 찾을 수 없어: {filepath}")
        return 1

    if target == 'wasm':
        try:
            from danha_compile import build_wasm
            out = build_wasm(filepath)
            print(f"\n✅ WASM 출력 완료: {out}")
            return 0
        except ImportError:
            print("❌ llvmlite가 설치되지 않았어. 'pip install llvmlite'로 설치해줘.")
            return 1
        except Exception as e:
            print(f"❌ {e}")
            return 1

    if target in ('ios', 'android'):
        try:
            from danha_mobile import build_ios, build_android
            if target == 'ios':
                out = build_ios(filepath)
            else:
                out = build_android(filepath)
            print(f"\n✅ {target.upper()} 프로젝트 생성 완료: {out}")
            return 0
        except Exception as e:
            print(f"❌ {e}")
            return 1

    try:
        from danha_compile import build
        output_path = build(filepath, extra_libs=extra_args, runtime_mode=runtime_mode,
                            backend=backend)
        print(f"\n✅ 실행 파일 생성 완료: {output_path}")
        print(f"   실행: ./{os.path.basename(output_path)}")
        return 0
    except ImportError:
        print("❌ llvmlite가 설치되지 않았어. 'pip install llvmlite'로 설치해줘.")
        return 1
    except Exception as e:
        print(f"❌ {e}")
        return 1


def _scan_dh_dependencies(filepath):
    """Entry .dh와 import/from import로 연결된 로컬 .dh 파일 목록."""
    import re

    seen = set()
    ordered = []
    root = os.path.abspath(filepath)
    base_dir = os.path.dirname(root)
    cwd = os.getcwd()
    parent_dir = os.path.dirname(base_dir)

    def _resolve(module_name, current_dir):
        rel = module_name.replace('.', os.sep) + '.dh'
        candidates = [
            os.path.join(current_dir, rel),
            os.path.join(base_dir, rel),
            os.path.join(parent_dir, rel),
            os.path.join(cwd, rel),
            os.path.join(DANHA_DIR, rel),
        ]
        for c in candidates:
            if os.path.exists(c):
                return os.path.abspath(c)
        return None

    def _visit(path):
        path = os.path.abspath(path)
        if path in seen:
            return
        seen.add(path)
        ordered.append(path)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read()
        except Exception:
            return
        current_dir = os.path.dirname(path)
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith('//'):
                continue
            m = re.match(r'^import\s+([A-Za-z_][A-Za-z0-9_.]*)\b', s)
            if not m:
                m = re.match(r'^from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import\b', s)
            if m:
                dep = _resolve(m.group(1), current_dir)
                if dep:
                    _visit(dep)
                continue
            for lib in re.findall(r'@clink\("([^"]+)"\)', s):
                c_path = os.path.join(current_dir, f"{lib}.c")
                if os.path.exists(c_path):
                    c_path = os.path.abspath(c_path)
                    if c_path not in seen:
                        seen.add(c_path)
                        ordered.append(c_path)
                h_path = os.path.join(current_dir, f"{lib}.h")
                if os.path.exists(h_path):
                    h_path = os.path.abspath(h_path)
                    if h_path not in seen:
                        seen.add(h_path)
                        ordered.append(h_path)

    _visit(root)
    return ordered


def cmd_compile_watch(filepath, extra_args=None, run_after=False, target=None):
    """파일 변경 감지 후 네이티브 재빌드. --run이면 실행 파일도 재시작."""
    import subprocess
    import time

    if target is not None:
        print("❌ compile --watch는 네이티브 타겟에서만 지원해.")
        return 1
    if not os.path.exists(filepath):
        print(f"❌ 파일을 찾을 수 없어: {filepath}")
        return 1

    proc = None
    mtimes = {}

    def _snapshot():
        deps = _scan_dh_dependencies(filepath)
        snap = {}
        for p in deps:
            try:
                snap[p] = os.path.getmtime(p)
            except OSError:
                snap[p] = 0
        return snap

    def _stop_proc():
        nonlocal proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc = None

    def _build_and_maybe_run(reason):
        nonlocal proc
        if reason:
            print(f"\n🔄 {reason}")
        try:
            from danha_compile import build
            output = build(filepath, extra_libs=extra_args)
            print(f"\n✅ 실행 파일 생성 완료: {output}")
        except Exception as e:
            print(f"❌ {e}")
            return 1
        if run_after:
            _stop_proc()
            print(f"▶ 실행: {output}")
            proc = subprocess.Popen([os.path.abspath(output)], cwd=os.path.dirname(os.path.abspath(filepath)) or None)
        return 0

    print(f"👁  네이티브 빌드 감시 중: {filepath}  (Ctrl+C로 종료)")
    _build_and_maybe_run(None)
    mtimes = _snapshot()

    try:
        while True:
            time.sleep(0.3)
            new_mtimes = _snapshot()
            changed = [p for p, m in new_mtimes.items() if mtimes.get(p) != m]
            if changed:
                mtimes = new_mtimes
                names = ', '.join(os.path.basename(p) for p in changed[:4])
                if len(changed) > 4:
                    names += f" 외 {len(changed) - 4}개"
                _build_and_maybe_run(f"변경 감지 — 재빌드: {names}")
    except KeyboardInterrupt:
        _stop_proc()
        print("\n👋 네이티브 빌드 감시 종료")
        return 0


ENGINE_MODULE_MARKERS = [
    '_mod_ari_',
    '_mod_danha_gl',
    '_mod_danha_gfx',
    '_mod_danha_audio',
    '_mod_danha_text',
    '_mod_danha_net',
]

ENGINE_SYMBOL_PREFIXES = (
    'ari_',
    'gl_',
    'SDL_',
)

LLVM_SYMBOL_RE = re.compile(r'@"([^"]+)"|@([A-Za-z_.$][A-Za-z0-9_.$]*)')


def _find_engine_symbol_leaks(llvm_ir):
    leaks = set()
    for match in LLVM_SYMBOL_RE.finditer(llvm_ir):
        symbol = match.group(1) or match.group(2)
        if symbol.startswith(ENGINE_SYMBOL_PREFIXES):
            leaks.add(symbol)
            continue
        if any(marker in symbol for marker in ENGINE_MODULE_MARKERS):
            leaks.add(symbol)
    return sorted(leaks)


def _format_symbol_leaks(leaks, limit=8):
    shown = leaks[:limit]
    if len(leaks) <= limit:
        return ', '.join(shown)
    return f"{', '.join(shown)} 외 {len(leaks) - limit}개"


def cmd_check(filepath, compile_check=False, standalone_check=False):
    """구문 검사, 선택적으로 컴파일/타입 검사까지 수행."""
    if not os.path.exists(filepath):
        print(f"❌ 파일을 찾을 수 없어: {filepath}")
        return 1

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()
    except Exception as e:
        print(f"❌ 파일 읽기 실패: {e}")
        return 1

    try:
        from lexer import lex
        from danha_parser import parse
        tokens = lex(source)
        ast = parse(tokens, source)
        if compile_check or standalone_check:
            from danha_compile import compile_module
            from llvmlite import binding as llvm
            module = compile_module(
                ast,
                base_dir=os.path.dirname(os.path.abspath(filepath)),
                source_code=source,
            )
            llvm_ir = str(module)
            mod_ref = llvm.parse_assembly(llvm_ir)
            mod_ref.verify()
            if standalone_check:
                leaks = _find_engine_symbol_leaks(llvm_ir)
                if leaks:
                    print(f"❌ {filepath} — 코어 독립성 검사 실패: 엔진 심볼/모듈 감지 ({_format_symbol_leaks(leaks)})")
                    return 1
                print(f"✅ {filepath} — 코어 독립성 검사 통과 ({len(ast[1])}개 문장)")
            else:
                print(f"✅ {filepath} — 컴파일 검사 통과 ({len(ast[1])}개 문장)")
        else:
            print(f"✅ {filepath} — 구문 검사 통과 ({len(ast[1])}개 문장)")
        return 0
    except Exception as e:
        print(f"❌ {e}")
        return 1


def cmd_repl():
    """대화형 모드 (Read-Eval-Print Loop)"""
    print_banner()
    print("  대화형 모드. 'exit' 또는 Ctrl+D로 종료.\n")

    from danha_evaluator import Scope, evaluate, format_value, BUILTINS
    from lexer import lex
    from danha_parser import parse

    global_scope = Scope()
    for name, builtin in BUILTINS.items():
        global_scope.declare(name, builtin)

    while True:
        try:
            line = input("단아> ")
        except (EOFError, KeyboardInterrupt):
            print("\n안녕! 👋")
            return 0

        line = line.strip()
        if not line:
            continue
        if line in ('exit', 'quit', '종료'):
            print("안녕! 👋")
            return 0

        full_input = line
        open_braces = line.count('{') - line.count('}')
        while open_braces > 0:
            try:
                cont = input("  ... ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            full_input += "\n" + cont
            open_braces += cont.count('{') - cont.count('}')

        try:
            tokens = lex(full_input)
            ast = parse(tokens, full_input)

            import danha_evaluator as _ev
            _ev._CURRENT_SOURCE = full_input

            result = None
            for stmt in ast[1]:
                result = evaluate(stmt, global_scope)

            if result is not None:
                formatted = format_value(result)
                print(f"→ {formatted}")

        except Exception as e:
            print(f"❌ {e}")


def cmd_version():
    print(f"{VERSION_NAME} v{VERSION}")
    print(f"호스트: Python {sys.version.split()[0]}")
    try:
        import llvmlite
        print(f"LLVM 백엔드: llvmlite {llvmlite.__version__}")
    except ImportError:
        print("LLVM 백엔드: 미설치 (인터프리터만 사용 가능)")


# ===== 48단계: danha test =====

def cmd_test(filepath):
    """test 블록을 찾아 실행하고 결과 보고."""
    if not os.path.exists(filepath):
        print(f"❌ 파일을 찾을 수 없어: {filepath}")
        return 1

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()
    except Exception as e:
        print(f"❌ 파일 읽기 실패: {e}")
        return 1

    import danha_evaluator as ev

    ev._TEST_MODE = True
    try:
        ev.run(source, base_dir=os.path.dirname(os.path.abspath(filepath)))
    except Exception as e:
        print(f"❌ 테스트 실행 중 오류: {e}")
        ev._TEST_MODE = False
        return 1
    finally:
        ev._TEST_MODE = False

    results = ev._TEST_RESULTS
    if not results:
        print(f"⚠  test 블록이 없어: {filepath}")
        return 0

    passed = sum(1 for r in results if r['passed'])
    failed = len(results) - passed

    print(f"\n테스트 결과: {filepath}")
    print("─" * 50)
    for r in results:
        mark = "✅" if r['passed'] else "❌"
        name = r['name'] or "(이름 없음)"
        print(f"  {mark}  {name}")
        if not r['passed'] and r['error']:
            print(f"       {r['error']}")
    print("─" * 50)
    print(f"  {passed}개 통과, {failed}개 실패 / 총 {len(results)}개")

    return 0 if failed == 0 else 1


# ===== 49단계: danha doc =====

def cmd_doc(filepath, markdown=False):
    """/// doc comment를 파싱해서 HTML 또는 Markdown 출력."""
    if not os.path.exists(filepath):
        print(f"❌ 파일을 찾을 수 없어: {filepath}")
        return 1

    try:
        from danha_doc import generate_html, generate_markdown
        if markdown:
            out = generate_markdown(filepath)
        else:
            out = generate_html(filepath)
        print(f"✅ 문서 생성 완료: {out}")
        return 0
    except Exception as e:
        print(f"❌ 문서 생성 실패: {e}")
        return 1


# ===== 51단계: danha profile =====

def cmd_profile(filepath):
    """함수별 실행 시간 프로파일링."""
    if not os.path.exists(filepath):
        print(f"❌ 파일을 찾을 수 없어: {filepath}")
        return 1

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()
    except Exception as e:
        print(f"❌ 파일 읽기 실패: {e}")
        return 1

    import danha_evaluator as ev

    ev._PROFILING = True
    try:
        ev.run(source, base_dir=os.path.dirname(os.path.abspath(filepath)))
    except Exception as e:
        print(f"❌ 실행 오류: {e}")
        ev._PROFILING = False
        return 1
    finally:
        ev._PROFILING = False

    stats = ev._PROFILE_STATS
    if not stats:
        print("프로파일링 결과: 사용자 함수 호출 없음")
        return 0

    print(f"\n프로파일링 결과: {filepath}")
    print(f"{'함수':30s} {'호출수':>8s} {'총 시간(ms)':>14s} {'평균(ms)':>12s}")
    print("─" * 70)

    sorted_stats = sorted(stats.items(), key=lambda x: x[1]['total'], reverse=True)
    for name, s in sorted_stats:
        calls = s['calls']
        total_ms = s['total'] * 1000
        avg_ms = total_ms / calls if calls else 0
        print(f"  {name:28s} {calls:>8d} {total_ms:>14.3f} {avg_ms:>12.3f}")

    print("─" * 70)
    grand_total = sum(s['total'] for s in stats.values()) * 1000
    print(f"  {'총합':28s} {sum(s['calls'] for s in stats.values()):>8d} {grand_total:>14.3f}")

    return 0


# ===== 53단계: danha debug =====

def cmd_debug(filepath):
    """디버거로 파일 실행."""
    try:
        from danha_debugger import run_debug
        return run_debug(filepath)
    except Exception as e:
        print(f"❌ 디버거 오류: {e}")
        return 1


# ===== 56단계: danha shader =====

def cmd_shader(args):
    """@vert/@frag 함수를 GLSL로 변환."""
    try:
        from danha_shader import main as shader_main
        return shader_main(args)
    except Exception as e:
        print(f"❌ 셰이더 변환 오류: {e}")
        return 1


# ===== 58단계: danha editor =====

def cmd_editor(args):
    """씬 에디터 실행."""
    try:
        from danha_editor import main as editor_main
        return editor_main(args)
    except Exception as e:
        print(f"❌ 에디터 오류: {e}")
        return 1


# ===== 47단계: danha pkg =====

def cmd_pkg(args):
    """패키지 매니저."""
    try:
        from danha_pkg import main as pkg_main
        return pkg_main(args)
    except Exception as e:
        print(f"❌ pkg 오류: {e}")
        return 1


# ===== 진입점 =====

def main():
    if len(sys.argv) < 2:
        print_banner()
        print("사용법:")
        print("  danha run <파일.dh>                      인터프리터 실행")
        print("  danha run --watch <파일.dh>              파일 변경 자동 재실행")
        print("  danha compile <파일.dh>                  네이티브 컴파일")
        print("  danha compile --watch [--run] <파일.dh>  네이티브 변경 감지 빌드/재시작")
        print("  danha compile --target wasm <파일.dh>    WebAssembly 컴파일")
        print("  danha compile --target ios <파일.dh>     iOS 프로젝트 생성")
        print("  danha compile --target android <파일.dh> Android 프로젝트 생성")
        print("  danha test <파일.dh>                     테스트 실행")
        print("  danha doc <파일.dh>                      문서 생성 (HTML)")
        print("  danha doc --md <파일.dh>                 문서 생성 (Markdown)")
        print("  danha profile <파일.dh>                  프로파일링")
        print("  danha debug <파일.dh>                    디버거")
        print("  danha shader <파일.dh>                   GLSL 셰이더 변환")
        print("  danha editor [<씬파일.dhs>]              씬 에디터")
        print("  danha pkg <init|add|list>               패키지 매니저")
        print("  danha repl                               대화형 모드")
        print("  danha check <파일.dh>                    구문 검사")
        print("  danha check --compile <파일.dh>          컴파일/타입 검사 (링크/실행 없음)")
        print("  danha check --standalone <파일.dh>       코어 독립성 검사")
        print("  danha selfhost <파일.dh>                  셀프 호스팅 컴파일러로 컴파일 (59단계)")
        print("  danha version                            버전 정보")
        return 0

    cmd = sys.argv[1].lower()

    if cmd == 'run':
        rest = sys.argv[2:]
        watch = '--watch' in rest
        files = [a for a in rest if not a.startswith('--')]
        if not files:
            print("❌ 실행할 파일을 지정해줘: danha run <파일.dh>")
            return 1
        return cmd_run(files[0], watch=watch)

    elif cmd == 'compile':
        rest = sys.argv[2:]
        target = None
        runtime_mode = 'libc'
        watch = '--watch' in rest
        run_after = '--run' in rest
        # --clang: llvmlite 바인딩 대신 외부 clang으로 오브젝트 생성 (.ll 텍스트 경로)
        use_clang = '--clang' in rest
        rest = [a for a in rest if a not in ('--watch', '--run', '--clang')]
        if '--target' in rest:
            idx = rest.index('--target')
            if idx + 1 < len(rest):
                target = rest[idx + 1].lower()
                rest = [a for i, a in enumerate(rest) if i not in (idx, idx + 1)]
        if '--runtime' in rest:
            idx = rest.index('--runtime')
            if idx + 1 < len(rest):
                runtime_mode = rest[idx + 1].lower()
                rest = [a for i, a in enumerate(rest) if i not in (idx, idx + 1)]
            else:
                print("❌ --runtime 값이 필요해: libc 또는 direct-os")
                return 1
        if runtime_mode not in ('libc', 'direct-os'):
            print("❌ 알 수 없는 런타임이야: libc 또는 direct-os")
            return 1
        files = [a for a in rest if not a.startswith('--')]
        extra = [a for a in rest if a.startswith('-') and a != '--target']
        if not files:
            print("❌ 컴파일할 파일을 지정해줘: danha compile <파일.dh>")
            return 1
        if watch:
            return cmd_compile_watch(files[0], extra_args=extra if extra else None,
                                     run_after=run_after, target=target)
        return cmd_compile(files[0], extra_args=extra if extra else None, target=target,
                           runtime_mode=runtime_mode,
                           backend='clang' if use_clang else 'auto')

    elif cmd == 'test':
        if len(sys.argv) < 3:
            print("❌ 테스트할 파일을 지정해줘: danha test <파일.dh>")
            return 1
        return cmd_test(sys.argv[2])

    elif cmd == 'doc':
        rest = sys.argv[2:]
        markdown = '--md' in rest
        files = [a for a in rest if not a.startswith('--')]
        if not files:
            print("❌ 문서화할 파일을 지정해줘: danha doc <파일.dh>")
            return 1
        return cmd_doc(files[0], markdown=markdown)

    elif cmd == 'profile':
        if len(sys.argv) < 3:
            print("❌ 프로파일링할 파일을 지정해줘: danha profile <파일.dh>")
            return 1
        return cmd_profile(sys.argv[2])

    elif cmd == 'debug':
        if len(sys.argv) < 3:
            print("❌ 디버그할 파일을 지정해줘: danha debug <파일.dh>")
            return 1
        return cmd_debug(sys.argv[2])

    elif cmd == 'shader':
        return cmd_shader(sys.argv[2:])

    elif cmd == 'editor':
        return cmd_editor(sys.argv[2:])

    elif cmd == 'pkg':
        return cmd_pkg(sys.argv[2:])

    elif cmd == 'check':
        rest = sys.argv[2:]
        compile_check = '--compile' in rest
        standalone_check = '--standalone' in rest
        rest = [a for a in rest if a not in ('--compile', '--standalone', '--syntax')]
        files = [a for a in rest if not a.startswith('--')]
        unknown = [a for a in rest if a.startswith('--')]
        if unknown:
            print(f"❌ 모르는 check 옵션이야: {', '.join(unknown)}")
            return 1
        if not files:
            print("❌ 검사할 파일을 지정해줘: danha check [--compile|--standalone] <파일.dh>")
            return 1
        return cmd_check(files[0], compile_check=compile_check, standalone_check=standalone_check)

    elif cmd == 'repl':
        return cmd_repl()

    elif cmd == 'version':
        cmd_version()
        return 0

    elif cmd == 'selfhost':
        return cmd_selfhost(sys.argv[2:])

    elif cmd.endswith('.dh'):
        return cmd_run(cmd)

    else:
        print(f"❌ 모르는 명령이야: {cmd}")
        print("  사용 가능: run, compile, test, doc, profile, debug, shader, editor, pkg, repl, check, selfhost, version")
        return 1


if __name__ == '__main__':
    sys.exit(main())
