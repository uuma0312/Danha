# Danha

Danha, written as `단아`, is an experimental programming language for building game engines, game tools, and native runtime experiments.

The project started from a simple idea: a language should be pleasant enough for small gameplay scripts, but low-level enough to grow toward a real engine runtime. Danha currently includes an interpreter, an LLVM-based native compile path, a growing self-hosted compiler written in Danha itself, ECS-style game primitives, native Windows/direct-OS experiments, and small graphics/audio/runtime bindings.

> Danha is still experimental. APIs, syntax details, and compiler behavior may change quickly.

## What Danha Tries To Be

- Easy to start with simple scripts.
- Useful for gameplay and engine-style code.
- Capable of native compilation through LLVM.
- Friendly to ECS patterns such as `component`, `system`, `spawn`, and `for each`.
- A path toward self-hosting, where Danha can compile more of itself over time.

## Quick Example

```danha
print("Hello, Danha!")

name = "developer"
print("Welcome, {name}!")

fn add(a: i64, b: i64) -> i64 {
    return a + b
}

print(add(20, 22))
```

## ECS Example

```danha
component Position {
    x: f64
    y: f64
}

component Velocity {
    dx: f64
    dy: f64
}

system move(dt: f64) {
    for each (p: &mut Position, v: &Velocity) {
        p.x = p.x + v.dx * dt
        p.y = p.y + v.dy * dt
    }
}

entity = spawn()
add(entity, Position { x: 0.0, y: 0.0 })
add(entity, Velocity { dx: 1.0, dy: 0.5 })
schedule(1.0)
```

## Running

From the Danha directory:

```powershell
python danha.py run examples/hello.dh
```

Check syntax:

```powershell
python danha.py check examples/hello.dh
```

Compile through the native path:

```powershell
python danha.py compile examples/hello.dh
```

Run the self-hosting compiler route:

```powershell
python danha.py selfhost examples/hello.dh
```

## Project Layout

```text
danha.py              CLI entry point
danha_compile.py      native compiler path
danha_evaluator.py    interpreter
danha_parser.py       parser
danhac.dh             self-hosted compiler source
danha_pe_linker.py    experimental PE linker
examples/             sample Danha programs
tests/                smoke tests and regression cases
bench/                benchmark harness and workloads
tools/                helper scripts
docs/                 documentation
```

## Syntax Guide

See [`docs/SYNTAX.md`](docs/SYNTAX.md) for the current practical syntax guide.

## Current Status

Danha is not a finished production language yet. The core language, interpreter, native compiler, ECS features, binary I/O, direct-OS runtime work, and self-hosting path are all moving together. That makes the project interesting, but also means the repository should be treated as a research and engine-language prototype.

The most useful way to evaluate Danha today is to run the examples, run the smoke tests, and read `danhac.dh` to see how far the self-hosting path has progressed.

## License

Danha is released under the MIT License. See [`LICENSE`](LICENSE).
