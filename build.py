#!/usr/bin/env python3
"""
단아(Danha) 빌드 스크립트 — PyInstaller로 설치 파일 생성

사용법:
    python build.py              # 기본 빌드 (onefile)
    python build.py --clean      # 빌드 폴더 정리 후 빌드

필요한 것:
    pip install pyinstaller llvmlite

결과물:
    dist/danha (Linux) 또는 dist/danha.exe (Windows)
"""

import subprocess
import sys
import os
import shutil

def clean():
    """빌드 폴더 정리"""
    for d in ['build', 'dist', '__pycache__']:
        if os.path.exists(d):
            shutil.rmtree(d)
    for f in os.listdir('.'):
        if f.endswith('.spec'):
            os.remove(f)
    print("🧹 빌드 폴더 정리 완료")

def build():
    """PyInstaller로 단아 빌드"""
    print("🔨 단아 빌드 시작...")
    
    sep = ';' if sys.platform == 'win32' else ':'
    py_modules = [
        'lexer.py', 'danha_parser.py', 'danha_evaluator.py',
        'danha_compile.py', 'danha_errors.py',
        'danha_pkg.py', 'danha_doc.py', 'danha_debugger.py',
        'danha_shader.py', 'danha_mobile.py', 'danha_editor.py',
    ]
    add_data = []
    for m in py_modules:
        if os.path.exists(m):
            add_data += ['--add-data', f'{m}{sep}.']

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',
        '--name', 'danha',
        *add_data,
        # 개나리꽃 로고 — danha.exe 자체 아이콘 (tools/make_logo_ico.py 생성물)
        *(['--icon', 'danha_logo.ico'] if os.path.exists('danha_logo.ico') else []),
        '--hidden-import=lexer',
        '--hidden-import=danha_parser',
        '--hidden-import=danha_evaluator',
        '--hidden-import=danha_compile',
        '--hidden-import=danha_errors',
        '--hidden-import=danha_pkg',
        '--hidden-import=danha_doc',
        '--hidden-import=danha_debugger',
        '--hidden-import=danha_shader',
        '--hidden-import=danha_mobile',
        '--hidden-import=danha_editor',
        '--hidden-import=llvmlite',
        '--hidden-import=llvmlite.ir',
        '--hidden-import=llvmlite.binding',
        'danha.py'
    ]
    
    result = subprocess.run(cmd, capture_output=False)
    
    if result.returncode == 0:
        exe = 'dist/danha.exe' if sys.platform == 'win32' else 'dist/danha'
        size_mb = os.path.getsize(exe) / (1024 * 1024)
        print(f"\n✅ 빌드 완료!")
        print(f"   📁 {exe} ({size_mb:.1f} MB)")
        print(f"\n사용법:")
        print(f"   {exe} run hello.dh       # 실행")
        print(f"   {exe} compile hello.dh   # 컴파일")
        print(f"   {exe} repl               # 대화형 모드")
    else:
        print("\n❌ 빌드 실패")
        sys.exit(1)

if __name__ == '__main__':
    if '--clean' in sys.argv:
        clean()
    build()
