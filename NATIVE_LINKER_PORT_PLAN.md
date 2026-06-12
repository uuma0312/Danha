# Danha Native Linker Port Plan

## Goal

Move the PE/COFF linker knowledge from `danha_pe_linker.py` into native Danha code in small, verifiable slices.

## Slice 1: Native COFF Probe in `danhac.dh`

Status: complete and promoted into `danhac_dos.exe` on 2026-06-11.

Add a development command:

- `danhac_dos.exe --probe-obj <input.obj>`

It should read the COFF object through Danha's byte runtime and print linker-relevant facts:

- file size and byte count
- machine, section count, symbol table pointer/count, section header pointer
- total relocation count
- discardable section count
- code section count
- initialized data section count
- BSS-like section count
- undefined symbol count

This does not link yet. It proves `danhac.dh` can inspect the same object metadata that the Python PE linker uses before layout/import/relocation work.

## Slice 2: Native Layout Model

Status: section merge/output layout/RVA computation complete and promoted into `danhac_dos.exe` on 2026-06-11. Relocation application and final PE image writing are still pending.

Add Danha functions that compute merged output section counts and aligned RVA/raw offsets using the same constants as `danha_pe_linker.py`:

- image base `0x140000000`
- section alignment `0x1000`
- file alignment `0x200`

Added development command:

- `danhac_dos.exe --probe-layout <input.obj>`

It decodes COFF section names, skips discardable sections, normalizes alignment characteristics, merges sections by `(name, normalized characteristics)`, tracks source-section offsets inside merged sections, appends `.dthnk` and `.idata`, and computes section RVAs with 4096-byte alignment.

## Slice 3: Native Import/Thunk Model

Status: import/helper/unsupported classification, import/thunk byte generation, and RVA finalization complete and promoted into `danhac_dos.exe` on 2026-06-11. PE image integration is still pending.

Classify undefined COFF symbols into:

- supported KERNEL32 imports
- helper symbols such as `__chkstk` / `___chkstk_ms`
- unsupported externals

Added development command:

- `danhac_dos.exe --probe-imports <input.obj>`
- `danhac_dos.exe --probe-import-bytes <input.obj> <out-prefix>`
- `danhac_dos.exe --probe-finalized-import-bytes <input.obj> <out-prefix> <idata-rva> <thunks-rva>`

It decodes COFF symbol names from both short 8-byte names and the COFF string table, then prints each undefined symbol followed by:

- `undefined=<n>`
- `helpers=<n>`
- `supported_imports=<n>`
- `unsupported=<n>`

The byte probe writes:

- `<out-prefix>.idata.bin`
- `<out-prefix>.thunks.bin`

These match Python `danha_pe_linker.py`'s `_build_imports()` and `_build_thunks()` output before final RVA patching.

The finalized byte probe writes:

- `<out-prefix>.idata.final.bin`
- `<out-prefix>.thunks.final.bin`

These match Python `danha_pe_linker.py`'s `_finalize_imports()` and `_patch_thunks()` output for the supplied section RVAs.

## Slice 4: Native PE Writer

Status: section layout, import/thunk generation/finalization, relocation target resolution, merged section byte assembly, and relocation byte patching are in `danhac.dh`. Final PE image writing is still pending.

Generate a minimal PE image from one direct-os COFF object, then compare:

- executable output
- `llvm-readobj --coff-imports`
- smoke execution

Added development command:

- `danhac_dos.exe --probe-relocs <input.obj> [limit]`
- `danhac_dos.exe --probe-patched-sections <input.obj> <out-prefix>`

It walks COFF relocation records, resolves merged-section patch offsets, computes `place_rva`, resolves target RVAs for both section-local symbols and import thunks, and prints relocation facts before byte patching.

The patched section probe writes:

- `<out-prefix>.text.bin`
- `<out-prefix>.xdata.bin`
- `<out-prefix>.rdata.bin`
- `<out-prefix>.pdata.bin`
- `<out-prefix>.dthnk.bin`
- `<out-prefix>.idata.bin`

## Current Completion Bar

Slice 1 is complete when the native `--probe-obj` output matches a Python reference probe for `tests/probe_subject.obj`.

Verified on 2026-06-11:

- `danhac_dos.exe --probe-obj tests\probe_subject.obj`
- Python reference probe for `tests/probe_subject.obj`
- Both printed:
  - `20069` file size
  - `20069` byte count
  - `34404` machine
  - `9` section count
  - `17035` symbol table pointer
  - `111` symbol count
  - `20` section header pointer
  - `362` relocation count
  - `1` discardable/remove section count
  - `1` code section count
  - `6` initialized-data section count
  - `1` BSS-like section count
  - `19` undefined symbol count
- Promoted compiler smoke:
  - `danhac_dos.exe tests\direct_os_smoke.dh --runtime direct-os --out tests\promoted_probe_smoke`
  - `tests\promoted_probe_smoke.exe` builds through clang object emission plus `danha_pe_linker.py`.

Slice 2 layout verification on 2026-06-11:

- `danhac_dos.exe --probe-layout tests\probe_subject.obj`
- Python reference using `danha_pe_linker.py` section merge/layout logic
- SHA256 matched line-for-line:
  - layout summary: `6C6EA1ACDE495C8BE405C17C8F7291AE980C8257998D0651EB5A9EA442CE1817`
- Both printed:
  - `0 .text 11753 11753 1610612768 4096`
  - `1 .data 0 1 3221225536 16384`
  - `2 .bss 0 40 3221225600 20480`
  - `3 .xdata 572 572 1073741888 24576`
  - `4 .rdata 232 232 1073741888 28672`
  - `5 .pdata 492 492 1073741888 32768`
  - `6 .dthnk 114 114 1615855648 36864`
  - `7 .idata 699 699 3224371264 40960`
  - source offsets: `1 0`, `2 0`, `3 0`, `4 0`, `5 0`, `6 208`, `7 224`, `8 0`
  - `count 8`
- Promoted compiler smoke:
  - `danhac_dos.exe tests\direct_os_smoke.dh --runtime direct-os --out tests\layout_promoted_smoke`
  - `tests\layout_promoted_smoke.exe` prints `direct-os`, `12345`, `true`.
  - `llvm-readobj --coff-imports` shows only `KERNEL32.dll`.

Slice 3 import classification verification on 2026-06-11:

- `danhac_dos.exe --probe-imports tests\probe_subject.obj`
- Python reference using `danha_pe_linker.py` internals
- Both printed the same 19 undefined symbols:
  - `GetProcessHeap`
  - `HeapAlloc`
  - `HeapReAlloc`
  - `HeapFree`
  - `GetStdHandle`
  - `WriteFile`
  - `ExitProcess`
  - `CreateFileA`
  - `GetFileSize`
  - `ReadFile`
  - `CloseHandle`
  - `SetFilePointer`
  - `GetFileAttributesA`
  - `CreateProcessA`
  - `WaitForSingleObject`
  - `GetExitCodeProcess`
  - `QueryPerformanceCounter`
  - `QueryPerformanceFrequency`
  - `GetCommandLineA`
- Both printed `undefined=19`, `helpers=0`, `supported_imports=19`, `unsupported=0`.
- Promoted compiler smoke:
  - `danhac_dos.exe tests\direct_os_smoke.dh --runtime direct-os --out tests\import_probe_promoted_smoke`
  - `tests\import_probe_promoted_smoke.exe` prints `direct-os`, `12345`, `true`.
  - `llvm-readobj --coff-imports` shows only `KERNEL32.dll`.

Slice 3 byte generation verification on 2026-06-11:

- Native:
  - `danhac_dos.exe --probe-import-bytes tests\probe_subject.obj tests\promoted_native_ref`
  - printed `imports=19`, `idata_len=699`, `thunks_len=114`
- Python reference:
  - `danha_pe_linker.py` internals `_build_imports()` and `_build_thunks()`
  - printed `imports=19`, `idata_len=699`, `thunks_len=114`
- SHA256 matched byte-for-byte:
  - idata: `0D080B5015F73D7266240E2481CD6FC20AA5A30B346CAD7AEE5D7A301EB0D69C`
  - thunks: `B84491BC977EA50C26372AD52E4C96C2C7BD4399D281F54695CC1F9AB9A9382D`
- Promoted compiler smoke:
  - `danhac_dos.exe tests\direct_os_smoke.dh --runtime direct-os --out tests\import_bytes_promoted_smoke`
  - `tests\import_bytes_promoted_smoke.exe` prints `direct-os`, `12345`, `true`.
  - `llvm-readobj --coff-imports` shows only `KERNEL32.dll`.

Slice 3 RVA finalization verification on 2026-06-11:

- Native:
  - `danhac_dos.exe --probe-finalized-import-bytes tests\probe_subject.obj tests\promoted_final 36864 32768`
  - printed `imports=19`, `idata_len=699`, `thunks_len=114`, `idata_rva=36864`, `thunks_rva=32768`
- Python reference:
  - `danha_pe_linker.py` internals `_finalize_imports()` and `_patch_thunks()` with the same RVAs
  - printed the same counts and RVAs.
- SHA256 matched byte-for-byte:
  - finalized idata: `1D9A80155980C6FE591086DBDDE07A27AF96942428FFC6C588F01232B9FD9641`
  - finalized thunks: `0B22FEF072FF6ED4DDDDC581E06C6274CF5CA0573A9B1B61422AA8A6FCBE35E6`
- Promoted compiler smoke:
  - `danhac_dos.exe tests\direct_os_smoke.dh --runtime direct-os --out tests\finalized_imports_promoted_smoke`
  - `tests\finalized_imports_promoted_smoke.exe` prints `direct-os`, `12345`, `true`.
  - `llvm-readobj --coff-imports` shows only `KERNEL32.dll`.

Slice 4 relocation resolution verification on 2026-06-11:

- `danhac_dos.exe --probe-relocs tests\probe_subject.obj 80`
- Python reference using `danha_pe_linker.py` relocation walk and symbol resolution
- SHA256 matched line-for-line for the first 80 relocation records plus total count:
  - relocation summary: `DDA0B931A9F280BFAC959E8A97B740974BFCE35EA51A35164080928C65B7B2BA`
- Both reported `reloc_count 362`.
- First resolved records matched, for example:
  - `0 sec=1 off=9 sym=22 name=GetProcessHeap kind=4 patch=9 place=4105 target=36912`
  - `1 sec=1 off=30 sym=23 name=HeapAlloc kind=4 patch=30 place=4126 target=36924`
  - `2 sec=1 off=61 sym=22 name=GetProcessHeap kind=4 patch=61 place=4157 target=36912`
- Promoted compiler smoke:
  - `danhac_dos.exe tests\direct_os_smoke.dh --runtime direct-os --out tests\reloc_probe_promoted_smoke`
  - `tests\reloc_probe_promoted_smoke.exe` prints `direct-os`, `12345`, `true`.
  - `llvm-readobj --coff-imports` shows only `KERNEL32.dll`.

Slice 4 relocation patch verification on 2026-06-11:

- `danhac_dos.exe --probe-patched-sections tests\probe_subject.obj tests\promoted_patched`
- Python reference built from `danha_pe_linker.py` after `_apply_relocations()`
- Section lengths matched:
  - `.text 11753`
  - `.xdata 572`
  - `.rdata 232`
  - `.pdata 492`
  - `.dthnk 114`
  - `.idata 699`
- SHA256 matched byte-for-byte:
  - `.text`: `B445C08A6E0A1259FBACD6E28AA145780F40BCE5170ED03CBE6F7EACA33B9552`
  - `.xdata`: `F35AC05B3EC498C3F169C3DF29738E5C88D39E2DFE11AA96522D74A5D0AD4C2D`
  - `.rdata`: `EC2B051023010EDF43B2E535FEC80C72FD0CE4CF6A567A633B96204248C866D6`
  - `.pdata`: `0F6FFA12DA5CA867F0999B09D1DA5F66776330ADAB0FE7A9234A981002BA8264`
  - `.dthnk`: `0B22FEF072FF6ED4DDDDC581E06C6274CF5CA0573A9B1B61422AA8A6FCBE35E6`
  - `.idata`: `2679702D5BB10D84CAB5ABC6B453EA32ED6828106EC02A11818DCD648081E232`
- Promoted compiler smoke:
  - `danhac_dos.exe tests\direct_os_smoke.dh --runtime direct-os --out tests\reloc_patch_promoted_smoke`
  - `tests\reloc_patch_promoted_smoke.exe` prints `direct-os`, `12345`, `true`.
  - `llvm-readobj --coff-imports` shows only `KERNEL32.dll`.
