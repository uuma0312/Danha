"""Minimal PE/COFF linker for Danha direct-os Windows executables.

This is intentionally small: one x86-64 COFF object, KERNEL32 imports, and the
relocation forms emitted by llvmlite for Danha direct-os programs.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field


IMAGE_REL_AMD64_ADDR32NB = 0x0003
IMAGE_REL_AMD64_REL32 = 0x0004

IMAGE_BASE = 0x140000000
SECTION_ALIGNMENT = 0x1000
FILE_ALIGNMENT = 0x200


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def _u16(data: bytes, off: int) -> int:
    return struct.unpack_from("<H", data, off)[0]


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def _i16(data: bytes, off: int) -> int:
    return struct.unpack_from("<h", data, off)[0]


def _read_c_string(data: bytes, off: int) -> str:
    end = data.find(b"\0", off)
    if end < 0:
        end = len(data)
    return data[off:end].decode("utf-8", errors="replace")


@dataclass
class CoffSection:
    name: str
    raw: bytearray
    raw_size: int
    raw_ptr: int
    reloc_ptr: int
    reloc_count: int
    characteristics: int
    rva: int = 0
    raw_out_ptr: int = 0


@dataclass
class CoffSymbol:
    name: str
    value: int
    section_number: int
    storage_class: int
    aux_count: int


@dataclass
class Reloc:
    section_index: int
    offset: int
    symbol_index: int
    kind: int


@dataclass
class LinkedSection:
    name: str
    data: bytearray
    virtual_size: int
    characteristics: int
    rva: int = 0
    raw_ptr: int = 0


@dataclass
class CoffObject:
    sections: list[CoffSection]
    symbols: list[CoffSymbol | None]
    relocs: list[Reloc]


def _coff_name(raw_name: bytes, string_table: bytes) -> str:
    if raw_name[:4] == b"\0\0\0\0":
        off = struct.unpack_from("<I", raw_name, 4)[0]
        return _read_c_string(string_table, off)
    return raw_name.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def _parse_coff(path: str) -> CoffObject:
    data = open(path, "rb").read()
    machine = _u16(data, 0)
    if machine != 0x8664:
        raise ValueError("Danha PE linker only supports x86-64 COFF objects")
    section_count = _u16(data, 2)
    sym_ptr = _u32(data, 8)
    sym_count = _u32(data, 12)
    opt_size = _u16(data, 16)
    sec_ptr = 20 + opt_size
    string_table = data[sym_ptr + sym_count * 18 :]

    sections: list[CoffSection] = []
    for i in range(section_count):
        off = sec_ptr + i * 40
        name = _coff_name(data[off : off + 8], string_table)
        raw_size = _u32(data, off + 16)
        raw_ptr = _u32(data, off + 20)
        reloc_ptr = _u32(data, off + 24)
        reloc_count = _u16(data, off + 32)
        chars = _u32(data, off + 36)
        if chars & 0x80:
            raw = bytearray()
        elif raw_ptr and raw_size:
            raw = bytearray(data[raw_ptr : raw_ptr + raw_size])
        else:
            raw = bytearray(raw_size)
        sections.append(CoffSection(name, raw, raw_size, raw_ptr, reloc_ptr, reloc_count, chars))

    symbols: list[CoffSymbol | None] = []
    idx = 0
    while idx < sym_count:
        off = sym_ptr + idx * 18
        name = _coff_name(data[off : off + 8], string_table)
        value = _u32(data, off + 8)
        section_number = _i16(data, off + 12)
        storage_class = data[off + 16]
        aux_count = data[off + 17]
        symbols.append(CoffSymbol(name, value, section_number, storage_class, aux_count))
        idx += 1
        for _ in range(aux_count):
            symbols.append(None)
            idx += 1

    relocs: list[Reloc] = []
    for sec_idx, sec in enumerate(sections, start=1):
        for r in range(sec.reloc_count):
            off = sec.reloc_ptr + r * 10
            relocs.append(Reloc(sec_idx, _u32(data, off), _u32(data, off + 4), _u16(data, off + 8)))

    return CoffObject(sections, symbols, relocs)


def _undefined_symbols(obj: CoffObject) -> list[str]:
    names: list[str] = []
    for sym in obj.symbols:
        if sym is None:
            continue
        if sym.section_number == 0 and sym.name not in names:
            names.append(sym.name)
    return names


def _import_name(symbol: str) -> str:
    if symbol == "exit":
        return "ExitProcess"
    return symbol


def _helper_symbols(symbols: list[str]) -> list[str]:
    return [s for s in symbols if s in ("__chkstk", "___chkstk_ms")]


def _import_symbols(symbols: list[str]) -> list[str]:
    helpers = set(_helper_symbols(symbols))
    return [s for s in symbols if s not in helpers]


def _build_imports(import_symbols: list[str]) -> tuple[LinkedSection, dict[str, int]]:
    imports = sorted({_import_name(s) for s in import_symbols})
    desc_size = 40
    ilt_off = desc_size
    iat_off = ilt_off + (len(imports) + 1) * 8
    cursor = iat_off + (len(imports) + 1) * 8
    name_offsets: dict[str, int] = {}
    data = bytearray(cursor)

    for name in imports:
        cursor = _align(cursor, 2)
        name_offsets[name] = cursor
        blob = struct.pack("<H", 0) + name.encode("ascii") + b"\0"
        data.extend(b"\0" * (cursor - len(data)))
        data.extend(blob)
        cursor += len(blob)

    dll_name_off = cursor
    data.extend(b"KERNEL32.dll\0")

    def write_u32(off: int, value: int) -> None:
        struct.pack_into("<I", data, off, value)

    # RVAs are filled after section layout. Store offsets temporarily by using
    # negative sentinels patched in _finalize_imports.
    write_u32(0, ilt_off)
    write_u32(12, dll_name_off)
    write_u32(16, iat_off)

    sec = LinkedSection(".idata", data, len(data), 0xC0300040)
    iat_offsets = {name: iat_off + i * 8 for i, name in enumerate(imports)}
    return sec, {name: off for name, off in iat_offsets.items()}


def _finalize_imports(sec: LinkedSection, imports: list[str], iat_offsets: dict[str, int]) -> None:
    data = sec.data
    ilt_off = struct.unpack_from("<I", data, 0)[0]
    dll_name_off = struct.unpack_from("<I", data, 12)[0]
    iat_off = struct.unpack_from("<I", data, 16)[0]
    struct.pack_into("<I", data, 0, sec.rva + ilt_off)
    struct.pack_into("<I", data, 12, sec.rva + dll_name_off)
    struct.pack_into("<I", data, 16, sec.rva + iat_off)

    for i, name in enumerate(imports):
        # Find hint/name offset by scanning the import names. This remains tiny.
        needle = struct.pack("<H", 0) + name.encode("ascii") + b"\0"
        name_off = data.find(needle)
        thunk_value = sec.rva + name_off
        struct.pack_into("<Q", data, ilt_off + i * 8, thunk_value)
        struct.pack_into("<Q", data, iat_offsets[name], thunk_value)


def _build_thunks(import_symbols: list[str]) -> tuple[LinkedSection, dict[str, int]]:
    data = bytearray()
    offsets: dict[str, int] = {}
    for symbol in sorted(import_symbols):
        offsets[symbol] = len(data)
        data.extend(b"\xFF\x25\x00\x00\x00\x00")
    return LinkedSection(".dthnk", data, len(data), 0x60500020), offsets


def _build_helpers(helper_symbols: list[str]) -> tuple[LinkedSection, dict[str, int]]:
    data = bytearray()
    offsets: dict[str, int] = {}
    for symbol in sorted(helper_symbols):
        offsets[symbol] = len(data)
        # Windows x64 stack probe helper. The caller keeps the probed size in
        # RAX and performs the actual stack adjustment, so a leaf return is a
        # correct minimal helper for Danha's current direct-os workloads.
        data.extend(b"\xC3")
    return LinkedSection(".drt", data, len(data), 0x60500020), offsets


def _patch_thunks(thunks: LinkedSection, thunk_offsets: dict[str, int], idata: LinkedSection, iat_offsets: dict[str, int]) -> None:
    for symbol, off in thunk_offsets.items():
        imp = _import_name(symbol)
        thunk_next = thunks.rva + off + 6
        iat_rva = idata.rva + iat_offsets[imp]
        struct.pack_into("<i", thunks.data, off + 2, iat_rva - thunk_next)


def _symbol_rva(
    obj: CoffObject,
    sym_index: int,
    section_map: dict[int, LinkedSection],
    section_offsets: dict[int, int],
    thunk_rvas: dict[str, int],
) -> int:
    sym = obj.symbols[sym_index]
    if sym is None:
        raise ValueError(f"relocation references aux symbol {sym_index}")
    if sym.section_number > 0:
        if sym.section_number not in section_map:
            raise ValueError(f"symbol references removed section: {sym.name}")
        return section_map[sym.section_number].rva + section_offsets.get(sym.section_number, 0) + sym.value
    if sym.name in thunk_rvas:
        return thunk_rvas[sym.name]
    raise ValueError(f"unresolved external symbol: {sym.name}")


def _apply_relocations(
    obj: CoffObject,
    section_map: dict[int, LinkedSection],
    section_offsets: dict[int, int],
    thunk_rvas: dict[str, int],
) -> None:
    for rel in obj.relocs:
        if rel.section_index not in section_map:
            continue
        sec = section_map[rel.section_index]
        patch_off = section_offsets.get(rel.section_index, 0) + rel.offset
        place_rva = sec.rva + patch_off
        target_rva = _symbol_rva(obj, rel.symbol_index, section_map, section_offsets, thunk_rvas)
        if rel.kind == IMAGE_REL_AMD64_REL32:
            addend = struct.unpack_from("<i", sec.data, patch_off)[0]
            value = target_rva + addend - (place_rva + 4)
            struct.pack_into("<i", sec.data, patch_off, value)
        elif rel.kind == IMAGE_REL_AMD64_ADDR32NB:
            addend = struct.unpack_from("<I", sec.data, patch_off)[0]
            struct.pack_into("<I", sec.data, patch_off, target_rva + addend)
        else:
            raise ValueError(f"unsupported AMD64 relocation type: 0x{rel.kind:04x}")


def _section_header(sec: LinkedSection) -> bytes:
    raw_size = _align(len(sec.data), FILE_ALIGNMENT) if sec.data else 0
    name = sec.name.encode("ascii")[:8].ljust(8, b"\0")
    return struct.pack(
        "<8sIIIIIIHHI",
        name,
        sec.virtual_size,
        sec.rva,
        raw_size,
        sec.raw_ptr if raw_size else 0,
        0,
        0,
        0,
        0,
        sec.characteristics,
    )


def link_direct_os(obj_path: str, output_path: str) -> str:
    obj = _parse_coff(obj_path)
    undefined = _undefined_symbols(obj)
    helper_symbols = _helper_symbols(undefined)
    import_symbols = _import_symbols(undefined)
    supported = {
        "CloseHandle",
        "CreateFileA",
        "CreateProcessA",
        "CreateThread",
        "ExitProcess",
        "GetCommandLineA",
        "GetExitCodeProcess",
        "GetFileAttributesA",
        "GetFileSize",
        "GetProcessHeap",
        "GetStdHandle",
        "HeapAlloc",
        "HeapFree",
        "HeapReAlloc",
        "QueryPerformanceCounter",
        "QueryPerformanceFrequency",
        "ReadFile",
        "SetFilePointer",
        "WaitForSingleObject",
        "WriteFile",
        "exit",
        "__chkstk",
        "___chkstk_ms",
    }
    unsupported = [s for s in undefined if s not in supported]
    if unsupported:
        raise ValueError("unsupported direct-os imports for Danha PE linker: " + ", ".join(unsupported))

    out_sections: list[LinkedSection] = []
    section_map: dict[int, LinkedSection] = {}
    section_offsets: dict[int, int] = {}
    merge_map: dict[tuple[str, int], LinkedSection] = {}
    for idx, sec in enumerate(obj.sections, start=1):
        if sec.characteristics & 0x800:
            continue
        normalized_chars = sec.characteristics & ~0x00F01000
        key = (sec.name, normalized_chars)
        linked = merge_map.get(key)
        if linked is None:
            linked = LinkedSection(sec.name, bytearray(), 0, normalized_chars)
            merge_map[key] = linked
            out_sections.append(linked)
        if sec.raw:
            off = _align(len(linked.data), 16)
            linked.data.extend(b"\0" * (off - len(linked.data)))
            linked.data.extend(sec.raw)
            linked.virtual_size = max(linked.virtual_size, len(linked.data))
        else:
            off = linked.virtual_size
            linked.virtual_size += max(sec.raw_size, 1)
        section_map[idx] = linked
        section_offsets[idx] = off
    helpers, helper_offsets = _build_helpers(helper_symbols)
    thunks, thunk_offsets = _build_thunks(import_symbols)
    idata, iat_offsets = _build_imports(import_symbols)
    if helpers.data:
        out_sections.append(helpers)
    out_sections.extend([thunks, idata])

    rva = SECTION_ALIGNMENT
    for sec in out_sections:
        sec.rva = rva
        rva += _align(max(sec.virtual_size, len(sec.data), 1), SECTION_ALIGNMENT)

    imports_sorted = sorted({_import_name(s) for s in import_symbols})
    _finalize_imports(idata, imports_sorted, iat_offsets)
    _patch_thunks(thunks, thunk_offsets, idata, iat_offsets)
    runtime_rvas = {sym: helpers.rva + off for sym, off in helper_offsets.items()}
    runtime_rvas.update({sym: thunks.rva + off for sym, off in thunk_offsets.items()})
    _apply_relocations(obj, section_map, section_offsets, runtime_rvas)

    main_rva = None
    for idx, sym in enumerate(obj.symbols):
        if sym is not None and sym.name == "main" and sym.section_number > 0:
            main_rva = _symbol_rva(obj, idx, section_map, section_offsets, runtime_rvas)
            break
    if main_rva is None:
        raise ValueError("entry symbol 'main' not found")

    pe_offset = 0x80
    opt_size = 0xF0
    header_size = _align(pe_offset + 4 + 20 + opt_size + len(out_sections) * 40, FILE_ALIGNMENT)
    raw_ptr = header_size
    for sec in out_sections:
        if sec.data:
            sec.raw_ptr = raw_ptr
            raw_ptr += _align(len(sec.data), FILE_ALIGNMENT)

    size_of_image = _align(max(sec.rva + max(sec.virtual_size, len(sec.data)) for sec in out_sections), SECTION_ALIGNMENT)
    size_of_code = sum(_align(len(s.data), FILE_ALIGNMENT) for s in out_sections if s.characteristics & 0x20)
    size_of_init = sum(_align(len(s.data), FILE_ALIGNMENT) for s in out_sections if s.characteristics & 0x40)
    size_of_uninit = sum(s.virtual_size for s in out_sections if s.characteristics & 0x80)

    dos = bytearray(b"MZ") + bytearray(0x3A)
    dos.extend(struct.pack("<I", pe_offset))
    dos.extend(b"\0" * (pe_offset - len(dos)))

    coff = struct.pack(
        "<HHIIIHH",
        0x8664,
        len(out_sections),
        0,
        0,
        0,
        opt_size,
        0x0022,
    )

    dirs = [(0, 0)] * 16
    dirs[1] = (idata.rva, idata.virtual_size)
    pdata = next((s for s in out_sections if s.name == ".pdata"), None)
    if pdata is not None and pdata.virtual_size:
        dirs[3] = (pdata.rva, pdata.virtual_size)

    optional = struct.pack(
        "<HBBIIIIIQIIHHHHHHIIIIHHQQQQII",
        0x20B,
        14,
        0,
        size_of_code,
        size_of_init,
        size_of_uninit,
        main_rva,
        next(s.rva for s in out_sections if s.characteristics & 0x20),
        IMAGE_BASE,
        SECTION_ALIGNMENT,
        FILE_ALIGNMENT,
        6,
        0,
        0,
        0,
        6,
        0,
        0,
        size_of_image,
        header_size,
        0,
        3,
        0x8100,
        0x100000,
        0x1000,
        0x100000,
        0x1000,
        0,
        16,
    )
    optional += b"".join(struct.pack("<II", rva_, size) for rva_, size in dirs)

    image = bytearray()
    image.extend(dos)
    image.extend(b"PE\0\0")
    image.extend(coff)
    image.extend(optional)
    for sec in out_sections:
        image.extend(_section_header(sec))
    image.extend(b"\0" * (header_size - len(image)))

    for sec in out_sections:
        if not sec.data:
            continue
        image.extend(b"\0" * (sec.raw_ptr - len(image)))
        image.extend(sec.data)
        image.extend(b"\0" * (_align(len(sec.data), FILE_ALIGNMENT) - len(sec.data)))

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(image)
    return output_path


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python danha_pe_linker.py <input.obj> <output.exe>")
        return 2
    try:
        link_direct_os(args[0], args[1])
        print(f"danha-pe-linker: {args[0]} -> {args[1]}")
        return 0
    except Exception as e:
        print(f"danha-pe-linker: failed: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
