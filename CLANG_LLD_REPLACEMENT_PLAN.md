# Danha Clang/Lld Replacement Plan

## Goal

Remove the external `clang`/`lld` dependency in stages, without pretending that the whole LLVM toolchain has been replaced at once.

## Stage 1: Python AOT Direct-OS Path

Use the existing llvmlite target machine to emit a Windows COFF object, then link that object with a Danha-owned minimal PE/COFF linker for the direct-os runtime.

Success criteria:

- `danha compile <file>.dh --runtime direct-os` can produce a working `.exe` without invoking `clang`, `lld`, `gcc`, or `link.exe`.
- The resulting executable imports only `KERNEL32.dll`.
- The old external linker remains available through an explicit fallback option.

Status: implemented for the single-object direct-os path with KERNEL32 imports and x64 `REL32`/`ADDR32NB` relocations. Set `DANHA_EXTERNAL_LINKER=1` to force the previous external linker path, or `DANHA_LINKER_STRICT=1` to fail instead of falling back.

Known limit: this replaces the linker, not LLVM IR to object code. Python AOT still uses llvmlite for object emission. Native `danhac.dh` still emits LLVM IR and invokes external `clang` for now.

## Stage 2: Self-Hosted Linker Surface

Expose the same linker contract to `danhac.dh`:

- `--emit-ir`
- `--emit-obj`
- `--linker danha|external`

This makes the remaining external boundary explicit for the native self-host compiler.

Status: implemented for Windows direct-os. `danhac.dh` now defaults to `--linker danha` for direct-os builds with no extra C link libraries. It still invokes `clang -c` for LLVM IR to COFF object emission, then calls `danha_pe_linker.py` for PE linking. The promoted `danhac_dos.exe` is itself produced by this Danha linker path.

## Stage 2.5: Binary Runtime Primitives

Before the PE/COFF linker can move from Python into `danhac.dh`, the native compiler needs byte-exact file access. String-based `file_read`/`file_write` cannot carry embedded NUL bytes from COFF objects or PE images.

Added direct-os primitives:

- `file_size(path) -> i64`
- `file_read_bytes(path) -> [i64]`
- `file_write_bytes(path, bytes)`
- `u16le(bytes, off) -> i64`
- `i16le(bytes, off) -> i64`
- `u32le(bytes, off) -> i64`
- `put_u8(bytes, value)`
- `put_u16le(bytes, value)`
- `put_u32le(bytes, value)`

Status: implemented and promoted into `danhac_dos.exe`. `tests/binary_io_smoke.dh` verifies that `77, 0, 90` survives a write/read round trip. `tests/coff_probe.dh` verifies that Danha can parse the COFF header, section table, symbol table pointer/count, and relocation count from a real direct-os object. `tests/binary_writer_smoke.dh` verifies little-endian byte emission for PE/COFF writer work. This is the foundation for porting `danha_pe_linker.py` into native Danha code.

## Stage 3: Native IR/Object Codegen Replacement

Replace the last `clang` role for self-hosted builds: LLVM IR text to machine code/object. This is the large compiler-backend project, likely starting with a restricted x64 backend for the direct-os subset before general LLVM IR support.
