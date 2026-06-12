# Danha Self-Hosting Gap Fix Plan

> **STATUS (2026-06-11): COMPLETE for this pass.**
>
> - `danha selfhost` prefers `danhac_dos.exe` when present and keeps Python bootstrap as fallback.
> - Native selfhost outputs write `.selfhost.txt` metadata with compiler/source/runtime/target/IR.
> - Windows direct-os selfhost builds use native `danhac_dos.exe`, emit COFF with clang, then link through `danha_pe_linker.py`.
> - Direct-os import checks confirmed generated smoke/self-rebuild outputs import only `KERNEL32.dll`.
> - Direct-linux args and `os_exec` smokes build from native `danhac_dos.exe` and run under WSL.
> - Arena instance allocation now exits non-zero on capacity overflow instead of silently advancing past capacity.

## Goal

Close the practical gaps found in the 2026-06-11 self-hosting review.

## Scope

1. Make `danha selfhost` prefer a native `danhac*.exe` when one is available, while keeping Python bootstrap available.
2. Add reproducible self-host output metadata so stage artifacts can be traced.
3. Improve direct runtime behavior for output paths that currently use silent stubs.
4. Add a real guard/test for arena overflow expectations.
5. Verify Windows direct-os self-hosting, direct-linux smoke execution, and import tables.

## Fixes Landed

- `danha_compile.py` now checks `Arena.new(cap).alloc(size)` against the arena instance capacity before updating the offset.
- On overflow, compiled programs print `danha arena: allocation exceeds capacity` and exit with code 1.
- This brings Python AOT arena instance behavior in line with the self-hosted `danhac.dh` runtime guard.

## Non-goal For This Pass

Removing the external `clang`/`lld` dependency entirely is a separate linker/codegen project. This pass should make that dependency explicit instead of pretending full toolchain independence.

## Verification

- `python danha.py selfhost tests/direct_os_smoke.dh --runtime direct-os --out tests/plan_selfhost_smoke`
- native `danhac*.exe` self-compile of `danhac.dh`
- `llvm-readobj --coff-imports` confirms direct-os outputs import only `KERNEL32.dll`
- WSL direct-linux smoke for args and `os_exec`
- arena overflow test exits non-zero when it is meant to exceed the supported limit

Verified on 2026-06-11:

- `python -m py_compile danha.py danha_compile.py danha_pe_linker.py danha_evaluator.py`
- `python danha.py selfhost tests/direct_os_smoke.dh --runtime direct-os --out tests/plan_selfhost_smoke2`
- `tests\plan_selfhost_smoke2.exe` output: `direct-os`, `12345`, `true`
- `danhac_dos.exe danhac.dh --runtime direct-os --out tests\plan_danhac_dos_rebuild`
- `danhac_dos.exe tests\binary_writer_smoke.dh --runtime direct-os --out tests\binary_writer_selfhost_check`
- `tests\binary_writer_selfhost_check.exe` output matches byte-writer expectations.
- `llvm-readobj --coff-imports` on direct-os smoke/self-rebuild outputs shows only `KERNEL32.dll`.
- `tests\arena_alloc_overflow.exe` exits with code 1 after printing the overflow message.
- `danhac_dos.exe tests\dl_args.dh --runtime direct-linux --out tests\dl_args_check2`, then WSL run prints `2`, `aa`, `bb`.
- `danhac_dos.exe tests\dl_exec.dh --runtime direct-linux --out tests\dl_exec_check`, then WSL run prints `syscall-exec-ok`, `0`, `7`.
