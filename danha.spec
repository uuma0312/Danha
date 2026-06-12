# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['danha.py'],
    pathex=[],
    binaries=[],
    datas=[('lexer.py', '.'), ('danha_parser.py', '.'), ('danha_evaluator.py', '.'), ('danha_compile.py', '.'), ('danha_errors.py', '.'), ('danha_pkg.py', '.'), ('danha_doc.py', '.'), ('danha_debugger.py', '.'), ('danha_shader.py', '.'), ('danha_mobile.py', '.'), ('danha_editor.py', '.')],
    hiddenimports=['lexer', 'danha_parser', 'danha_evaluator', 'danha_compile', 'danha_errors', 'danha_pkg', 'danha_doc', 'danha_debugger', 'danha_shader', 'danha_mobile', 'danha_editor', 'llvmlite', 'llvmlite.ir', 'llvmlite.binding'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='danha',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['danha_logo.ico'],
)
