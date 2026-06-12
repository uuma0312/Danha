import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_compile import CASES, capture_interp, normalize_floats  # noqa: E402


def normalize_danhac(text):
    text = normalize_floats(text)
    out = []
    for line in text.splitlines():
        if "." in line:
            try:
                fv = float(line)
                if fv.is_integer():
                    out.append(str(int(fv)))
                else:
                    out.append(("%f" % fv).rstrip("0").rstrip("."))
                continue
            except ValueError:
                pass
        out.append(line)
    return "\n".join(out)


def run_cmd(cmd, *, cwd=ROOT, timeout=60, input_text=None):
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def compile_and_run(case_index, source, out_dir, compiler, runtime, linker):
    src_path = out_dir / f"case_{case_index:04d}.dh"
    out_base = out_dir / f"case_{case_index:04d}"
    src_path.write_text(source, encoding="utf-8")

    cmd = [str(compiler), str(src_path.relative_to(ROOT)), "--runtime", runtime, "--out", str(out_base.relative_to(ROOT))]
    if linker:
        cmd.extend(["--linker", linker])
    try:
        proc = run_cmd(cmd, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "compile-timeout", ""
    if proc.returncode != 0:
        return False, "compile", proc.stdout + proc.stderr

    exe = out_base.with_suffix(".exe")
    if not exe.exists():
        return False, "compile-missing-exe", proc.stdout + proc.stderr
    try:
        proc = run_cmd([str(exe)], timeout=30)
    except subprocess.TimeoutExpired:
        return False, "run-timeout", ""
    if proc.returncode != 0:
        return False, "run", proc.stdout + proc.stderr
    return True, "ok", proc.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--runtime", default="direct-os")
    ap.add_argument("--linker", default="")
    ap.add_argument("--compiler", default=str(ROOT / "danhac_dos.exe"))
    args = ap.parse_args()

    out_dir = ROOT / "tests" / ".danhac_parity"
    if out_dir.exists() and not args.keep:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    compiler = Path(args.compiler)
    if not compiler.is_absolute():
        compiler = ROOT / compiler

    end = min(len(CASES), args.start + args.limit)
    passed = 0
    failed = 0
    first_failure = -1

    for idx in range(args.start, end):
        source = CASES[idx]
        label = source.strip().splitlines()[0][:70]
        try:
            expected = capture_interp(source)
        except Exception as exc:
            print(f"SKIP {idx:04d} {label!r} expected-error {exc}")
            continue

        ok, phase, actual = compile_and_run(idx, source, out_dir, compiler, args.runtime, args.linker)
        if ok and normalize_danhac(expected).strip() == normalize_danhac(actual).strip():
            passed += 1
            print(f"OK   {idx:04d} {label!r}")
        else:
            failed += 1
            if first_failure == -1:
                first_failure = idx
            print(f"FAIL {idx:04d} {phase} {label!r}")
            print("  expected:", repr(expected.strip()))
            print("  actual:  ", repr(actual.strip()[:2000]))
            if not args.keep:
                break

    print(f"SUMMARY passed={passed} failed={failed} range={args.start}:{end} first_failure={first_failure}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
