# Danha Syntax Guide

This document describes the practical Danha syntax used by the current examples and smoke tests. Danha is experimental, so this guide should be treated as a living document.

## Comments

```danha
// This is a line comment.
print("hello")
```

## Values And Variables

Variables can be assigned directly:

```danha
name = "Danha"
count = 3
ratio = 0.5
enabled = true
```

Constants use `const`:

```danha
const BASE = 10
const ANSWER = 42
```

Type annotations can be added when needed:

```danha
x: i64 = 10
y: f64 = 2.5
flag: bool = true
```

Common scalar types include:

- `i8`
- `i16`
- `i32`
- `i64`
- `f64`
- `bool`
- `str`

## Printing And String Interpolation

```danha
name = "developer"
print("Hello, {name}!")
print("1 + 2 = {1 + 2}")
```

## Operators

```danha
print(1 + 2)
print(5 - 3)
print(4 * 7)
print(10 / 2)

print(3 < 5)
print(3 <= 3)
print(5 > 2)
print(5 >= 5)
print(1 == 1)
print(1 != 2)
```

Boolean expressions use `and`, `or`, and `not` where supported by the active compiler path.

## If / Else

```danha
hp = 30

if hp <= 0 {
    print("down")
} else {
    print("alive")
}
```

Single-line blocks are also common in examples:

```danha
if hp > 0 { print("alive") }
```

## While

```danha
i = 0
while i < 3 {
    print(i)
    i = i + 1
}
```

## For Ranges

```danha
for frame in 0..3 {
    print(frame)
}
```

## Functions

```danha
fn add(a: i64, b: i64) -> i64 {
    return a + b
}

print(add(20, 22))
```

Functions without a return value can omit `-> Type`:

```danha
fn greet(name: str) {
    print("Hello, {name}")
}
```

## Casts

Use `as` for explicit casts:

```danha
x = 3
y = x as f64
print(y + 0.5)
```

## Lists

```danha
nums = [1, 2, 3]
print(nums[0])
nums.push(4)
print(nums.len())
```

Several higher-order list helpers are available in current examples:

```danha
nums = [1, 5, 15, 20]

big = nums.filter(fn(x: i64) -> bool {
    return x >= 10
})

shifted = nums.map(fn(x: i64) -> i64 {
    return x + 100
})

total = nums.reduce(0, fn(acc: i64, x: i64) -> i64 {
    return acc + x
})

print(big.len())
print(shifted[0])
print(total)
```

## Fixed-Size Arrays

Fixed arrays use `[T; N]`:

```danha
xs: [i32; 4] = [0; 4]
xs[0] = 10
xs[3] = 42
print(xs[0])
print(xs[3])
```

Array literals can also list values:

```danha
weights: [f64; 4] = [0.5, 1.5, 2.5, 3.5]
```

## Structs

```danha
struct Point {
    x: f64
    y: f64
}

p = Point { x: 1.0, y: 2.0 }
print(p.x)
p.y = 3.5
print(p.y)
```

Structs can be passed by reference:

```danha
fn length2(p: &Point) -> f64 {
    return p.x * p.x + p.y * p.y
}
```

## Unions

```danha
union Bits {
    i: i64
    f: f64
}

u = Bits { i: 77 }
print(u.i)
u.f = 2.5
print(u.f)
```

## Defer

`defer` runs cleanup code when the function exits:

```danha
fn work() -> i64 {
    defer { print("cleanup") }
    print("body")
    return 42
}
```

Multiple defers run in reverse order:

```danha
fn work() {
    defer { print("first defer") }
    defer { print("second defer") }
    print("body")
}
```

## Lambdas And Closures

```danha
add = fn(a: i64, b: i64) -> i64 {
    return a + b
}

print(add(2, 3))
```

Closures can read surrounding values in the supported paths:

```danha
offset = 100
nums = [1, 2, 3]
shifted = nums.map(fn(x: i64) -> i64 {
    return x + offset
})
```

## Generics

```danha
fn gmax[T](a: T, b: T) -> T {
    if a > b { return a }
    return b
}

print(gmax(3, 9))
print(gmax(2.5, 1.5))
```

Multiple generic parameters are supported in current smoke tests:

```danha
fn pair_sum[A, B](a: A, b: B) -> f64 {
    return (a as f64) + (b as f64)
}
```

## Macros

Macros are invoked with `!`:

```danha
macro square(x: i64) {
    x * x
}

print(square!(7))
```

Statement-style macros are also used:

```danha
macro log_twice(msg: str) {
    print(msg)
    print(msg)
}

log_twice!("hello")
```

## Comptime

`comptime` evaluates expressions during compile time in supported paths:

```danha
const BASE = 10
const CT = comptime { BASE * 4 + 2 }
print(CT)

x = comptime { 100 / 4 }
print(x)
```

## Unsafe

```danha
unsafe fn raw_add(a: i64, b: i64) -> i64 {
    return a + b
}

unsafe {
    print(raw_add(20, 22))
}
```

## Vectors

Vector helpers are available for math-heavy code:

```danha
v = vec3(1.0, 2.0, 3.0)
w = vec3(4.0, 5.0, 6.0)

print(v.x)
print(v.z)

s = v + w
print(s.y)

d = v * 2.0
print(d.z)

print(dot(v, w))
```

## HashMap

```danha
m = HashMap.new()
m.set("one", 1)
m.set("two", 2)

print(m.len())
print(m.get("two"))

if m.has("one") {
    print("has one")
}

m.remove("two")
```

Keys can be collected:

```danha
ks = m.keys()
i = 0
while i < len(ks) {
    print(ks[i])
    i = i + 1
}
```

## String Builder

```danha
sb = string_builder()
string_builder_append(sb, "Hello")
string_builder_append(sb, ", ")
string_builder_append(sb, "Danha!")

print(string_builder_to_string(sb))
print(string_builder_len(sb))
```

## Modules

Import a module:

```danha
import mathx
print(mathx.double(21))
```

Import a symbol from a module:

```danha
from mathx import triple
print(triple(7))
```

Import from a subfolder:

```danha
import geo.vecx
print(vecx.vlen2(3, 4))
```

## Components And Systems

Danha has ECS-style syntax for game code.

```danha
component Position {
    x: f64
    y: f64
}

component Velocity {
    dx: f64
    dy: f64
}

system move_entities(dt: f64) {
    for each (p: &mut Position, v: &Velocity) {
        p.x = p.x + v.dx * dt
        p.y = p.y + v.dy * dt
    }
}
```

Create entities and attach components:

```danha
player = spawn()
add(player, Position { x: 0.0, y: 0.0 })
add(player, Velocity { dx: 1.5, dy: 0.5 })
```

Run systems:

```danha
schedule(1.0)
```

Parallel systems are available in current smoke tests:

```danha
parallel system double_all() {
    for each (n: Num) {
        n.val = n.val * 2
    }
}
```

## Reflection Helpers

Current struct reflection helpers are generated with names such as:

```danha
struct Pt {
    x: f64
    y: f64
    tag: i64
}

p = Pt { x: 1.5, y: 2.5, tag: 7 }

print(_reflect_Pt_field_count())
print(_reflect_Pt_field_name(0))
print(_reflect_Pt_field_type(0))
print(_reflect_Pt_get_f64(p, 1))
_reflect_Pt_set_f64(p, 0, 9.5)
```

## Command-Line Basics

```powershell
python danha.py run examples/hello.dh
python danha.py check examples/hello.dh
python danha.py compile examples/hello.dh
python danha.py selfhost examples/hello.dh
```

Other CLI paths exist for testing, profiling, docs, packages, shaders, mobile targets, and editor experiments. Check `python danha.py` for the current command list.

