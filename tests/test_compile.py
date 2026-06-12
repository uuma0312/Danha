# test_compile_6_1.py
# 6-1 컴파일러가 인터프리터와 같은 답을 내는지 검증.
# print 출력은 stdout으로 나가지만, ctypes 경로라 redirect_stdout이 안 잡힌다.
# 그래서 OS 레벨로 stdout fd를 가로채는 방식으로 캡처한다.

import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

# tests/ 하위에서 실행되어도 부모(저장소 루트)를 import 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from danha_evaluator import run as run_interp
from danha_compile import run_native


def jit_print_output(source: str, opt_level: int = 2) -> str:
    """JIT(printf) 표준 출력을 문자열로 돌려준다.

    Linux 등: fd 1을 임시 파일로 dup2.
    Windows: CRT printf가 dup2를 따르지 않는 경우가 있어 자식 프로세스+파이프로 캡처.
    """
    if sys.platform == "win32":
        # 저장소 루트(tests/의 부모) — danha_compile.py가 있는 곳
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        runner = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from danha_compile import run_native\n"
            "src = sys.stdin.read()\n"
            "opt = int(sys.argv[1])\n"
            "run_native(src, opt_level=opt)\n"
        ) % here
        proc = subprocess.run(
            [sys.executable, "-c", runner, str(opt_level)],
            input=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=here,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"자식 JIT 실패 (exit {proc.returncode}): {proc.stderr[:2000]!r}"
            )
        return proc.stdout
    saved_fd = os.dup(1)
    old_stdout = sys.stdout
    with tempfile.TemporaryFile(mode="w+b") as tmp:
        os.dup2(tmp.fileno(), 1)
        sys.stdout = io.StringIO()
        try:
            run_native(source, opt_level=opt_level)
            os.fsync(1)
        finally:
            os.dup2(saved_fd, 1)
            os.close(saved_fd)
            sys.stdout = old_stdout
        tmp.seek(0)
        return tmp.read().decode("utf-8")


def capture_interp(source):
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_interp(source)
    return buf.getvalue()


CASES = [
    # ----- 6-1: 산술 -----
    "print(1 + 2 * 3)",         # 7
    "print(42)",                 # 42
    "print(100 - 7)",            # 93
    "print(2 * 3 * 4)",          # 24
    "print((1 + 2) * (3 + 4))",  # 21
    "print(20 / 6)",             # 3 (정수 나눗셈)
    "print(20 % 6)",             # 2
    "print(-5)",                 # -5
    "print(-3 * 4)",             # -12
    "print(2 - -3)",             # 5
    "print(- -7)",               # 7
    "print(-7 / 2)",             # -3 (C 규칙: 0 방향 버림)
    "print(-10 / -3)",           # 3
    "print(1)\nprint(2)\nprint(3)",  # 여러 print
    "print(100 / 10 / 2)",       # 5 (좌결합)
    "print(1 + 2 - 3 + 4)",      # 4
    
    # ----- 6-2: 변수 -----
    "x = 5\nprint(x)",                          # 5
    "x = 5\nx = x + 1\nprint(x)",               # 6
    "x = 100\nx = x - 30\nprint(x)",            # 70
    "a = 10\nb = 20\nprint(a + b)",             # 30
    "a = 10\nb = 20\nc = a + b\nprint(c)",      # 30
    "x = 7\nprint(x * x)",                      # 49 (제곱)
    "x = 1\nx = x + x\nx = x + x\nprint(x)",    # 4 (더블링)
    "n = 100\nm = -n\nprint(m)",                # -100 (단항 - 변수에)
    "x = 10\ny = 3\nprint(x / y)\nprint(x % y)",  # 3, 1
    "a = 1\nb = 2\nc = 3\nd = a + b * c\nprint(d)",  # 7 (우선순위 with 변수)
    "x = 5\nx = x + 1\nx = x + 1\nx = x + 1\nprint(x)",  # 8 (반복 갱신)
    "first = 10\nsecond = first * 2\nthird = second + first\nprint(third)",  # 30
    
    # ----- 6-3a + 6-4: 불리언과 비교 (이제 컴파일러도 true/false로 찍음) -----
    "print(true)",
    "print(false)",
    "print(5 > 3)",
    "print(5 < 3)",
    "print(5 == 5)",
    "print(5 == 6)",
    "print(5 != 6)",
    "print(5 >= 5)",
    "print(5 <= 4)",
    "print(-5 < 3)",          # signed 비교 정상
    "print(-5 < -10)",
    "print(0 == 0)",
    "x = 10\nprint(x > 5)",
    "x = 10\ny = 3\nprint(x > y)",
    "a = 5\nb = 5\nprint(a == b)",
    "print(1 + 2 < 5)",        # 우선순위
    "print(2 * 3 == 6)",
    "print(10 - 5 > 3)",
    
    # ----- 6-4: if / else / else if -----
    "x = 10\nif x > 5 { print(1) }",
    "x = 3\nif x > 5 { print(1) }\nprint(99)",
    "x = 10\nif x > 5 { print(1) } else { print(2) }",
    "x = 3\nif x > 5 { print(1) } else { print(2) }",
    
    # else if 사슬 - 모든 가지 시연
    "x = 1\nif x == 1 { print(10) } else if x == 2 { print(20) } else { print(30) }",
    "x = 2\nif x == 1 { print(10) } else if x == 2 { print(20) } else { print(30) }",
    "x = 9\nif x == 1 { print(10) } else if x == 2 { print(20) } else { print(30) }",
    
    # if 안에서 변수 갱신
    "total = 100\nif total > 50 { total = total + 10 }\nprint(total)",
    
    # 절댓값
    "x = -7\nif x < 0 { x = -x }\nprint(x)",
    "x = 7\nif x < 0 { x = -x }\nprint(x)",
    
    # 중첩 if
    """
x = 10
y = 20
if x > 5 {
    if y > 15 { print(1) } else { print(2) }
} else {
    print(3)
}
""",
    
    # if 식 안에 비교가 두 번 (and/or 없이도 가능한 패턴)
    """
score = 75
if score >= 90 { print(1) }
if score >= 80 { print(2) }
if score >= 70 { print(3) }
if score >= 60 { print(4) }
""",
    
    # if-else 후 코드가 잘 이어지는지
    """
x = 5
if x > 0 { print(1) } else { print(0) }
print(x + 100)
""",
    
    # ----- 6-5: while -----
    
    # 0~4 카운트업
    """
i = 0
while i < 5 {
    print(i)
    i = i + 1
}
""",
    
    # 카운트다운
    """
n = 5
while n > 0 {
    print(n)
    n = n - 1
}
""",
    
    # 합계
    """
sum = 0
i = 1
while i <= 10 {
    sum = sum + i
    i = i + 1
}
print(sum)
""",
    
    # 한 번도 안 도는 경우 (조건이 처음부터 거짓)
    """
i = 100
while i < 10 {
    print(i)
}
print(99)
""",
    
    # while 안에 if (짝수만)
    """
i = 0
while i < 10 {
    if i % 2 == 0 {
        print(i)
    }
    i = i + 1
}
""",
    
    # 중첩 while (구구단 2,3단)
    """
i = 2
while i <= 3 {
    j = 1
    while j <= 5 {
        print(i * j)
        j = j + 1
    }
    i = i + 1
}
""",
    
    # factorial 6! = 720
    """
n = 6
result = 1
i = 1
while i <= n {
    result = result * i
    i = i + 1
}
print(result)
""",
    
    # 피보나치 첫 10개
    """
a = 0
b = 1
i = 0
while i < 10 {
    print(a)
    next = a + b
    a = b
    b = next
    i = i + 1
}
""",
    
    # 두 수의 최대공약수 (유클리드)
    """
a = 48
b = 18
while b != 0 {
    t = b
    b = a % b
    a = t
}
print(a)
""",
    
    # while 후 코드 이어짐
    """
i = 0
while i < 3 {
    i = i + 1
}
print(i + 100)
""",
    
    # ----- 6-6b: 함수 -----
    
    # 인자 없는 함수
    "fn five() -> i32 { return 5 }\nprint(five())",
    
    # 인자 한 개
    "fn double(x: i32) -> i32 { return x * 2 }\nprint(double(21))",
    
    # 인자 두 개
    "fn add(a: i32, b: i32) -> i32 { return a + b }\nprint(add(2, 3))\nprint(add(100, 200))",
    
    # 함수 안 지역 변수
    """
fn calc(x: i32) -> i32 {
    y = x * 2
    z = y + 10
    return z
}
print(calc(5))
""",
    
    # if + early return
    """
fn abs(x: i32) -> i32 {
    if x < 0 { return -x }
    return x
}
print(abs(-7))
print(abs(7))
print(abs(0))
""",
    
    # 함수 안 while
    """
fn sum_to(n: i32) -> i32 {
    total = 0
    i = 1
    while i <= n {
        total = total + i
        i = i + 1
    }
    return total
}
print(sum_to(10))
print(sum_to(100))
""",
    
    # 재귀: factorial
    """
fn factorial(n: i32) -> i32 {
    if n <= 1 { return 1 }
    return n * factorial(n - 1)
}
print(factorial(5))
print(factorial(10))
""",
    
    # 재귀: 피보나치
    """
fn fib(n: i32) -> i32 {
    if n < 2 { return n }
    return fib(n - 1) + fib(n - 2)
}
print(fib(10))
""",
    
    # 상호 재귀
    """
fn is_even(n: i32) -> i32 {
    if n == 0 { return 1 }
    return is_odd(n - 1)
}
fn is_odd(n: i32) -> i32 {
    if n == 0 { return 0 }
    return is_even(n - 1)
}
print(is_even(4))
print(is_odd(7))
""",
    
    # 변수 격리
    """
fn f(x: i32) -> i32 { return x * 10 }
x = 999
print(f(5))
print(x)
""",
    
    # 함수에서 함수 호출
    """
fn double(x: i32) -> i32 { return x * 2 }
fn quadruple(x: i32) -> i32 { return double(double(x)) }
print(quadruple(3))
""",
    
    # early return 연속
    """
fn clamp(x: i32) -> i32 {
    if x < 0 { return 0 }
    if x > 100 { return 100 }
    return x
}
print(clamp(-5))
print(clamp(50))
print(clamp(200))
""",
    
    # 독립 호출 (결과 안 쓰고 부수효과만)
    """
fn show(x: i32) -> i32 {
    print(x)
    return 0
}
show(77)
""",
    
    # 함수 호출 결과를 변수에 저장해서 다시 호출
    """
fn square(x: i32) -> i32 { return x * x }
a = square(3)
b = square(a)
print(b)
""",
    
    # 호출 결과를 다른 함수 인자로
    """
fn add(a: i32, b: i32) -> i32 { return a + b }
fn mul(a: i32, b: i32) -> i32 { return a * b }
print(add(mul(2, 3), mul(4, 5)))
""",
    
    # 재귀 안에서 지역변수 격리
    """
fn test(n: i32) -> i32 {
    x = n * 10
    if n > 0 {
        test(n - 1)
    }
    print(x)
    return 0
}
test(3)
""",
    
    # 여러 함수 조합
    """
fn max(a: i32, b: i32) -> i32 {
    if a > b { return a }
    return b
}
fn min(a: i32, b: i32) -> i32 {
    if a < b { return a }
    return b
}
print(max(10, 20))
print(min(10, 20))
print(max(min(5, 3), min(7, 2)))
""",
    
    # 어노테이션 없는 함수도 컴파일됨
    "fn add(a, b) { return a + b }\nprint(add(2, 3))",
    
    # ----- 논리 연산: and / or / not -----
    
    # 기본
    "print(true and true)",
    "print(true and false)",
    "print(false and true)",
    "print(false and false)",
    "print(true or true)",
    "print(true or false)",
    "print(false or true)",
    "print(false or false)",
    "print(not true)",
    "print(not false)",
    
    # 비교 결과 조합
    "print(5 > 3 and 2 < 4)",      # true
    "print(5 > 3 and 2 > 4)",      # false
    "print(5 > 3 or 2 > 4)",       # true
    "print(5 < 3 or 2 > 4)",       # false
    "print(not 5 > 3)",            # false
    "print(not 5 < 3)",            # true
    
    # 변수와 함께
    """
x = 10
y = 20
print(x > 5 and y > 15)
print(x > 100 or y > 15)
""",
    
    # if 조건에 and/or
    """
score = 75
if score >= 60 and score < 80 {
    print(1)
} else {
    print(0)
}
""",
    
    # if 조건에 or
    """
x = 5
if x == 1 or x == 5 or x == 10 {
    print(1)
} else {
    print(0)
}
""",
    
    # while 조건에 and
    """
i = 0
sum = 0
while i < 10 and sum < 20 {
    sum = sum + i
    i = i + 1
}
print(i)
print(sum)
""",
    
    # not과 if
    """
done = false
if not done {
    print(1)
}
""",
    
    # 단락 평가: 왼쪽이 거짓이면 오른쪽 함수 호출 안 됨
    # side_effect가 호출되면 999가 찍힐 텐데, 안 찍히는 게 정답.
    """
fn side_effect() -> i32 {
    print(999)
    return 1
}
fn always_true() -> i32 {
    if false and side_effect() > 0 {
        return 1
    }
    return 0
}
print(always_true())
""",  # 0만 찍히고 999는 안 찍혀야 함
    
    # 단락 평가 or: 왼쪽이 참이면 오른쪽 호출 안 됨
    """
fn side_effect() -> i32 {
    print(999)
    return 1
}
fn check() -> i32 {
    if true or side_effect() > 0 {
        return 0
    }
    return 1
}
print(check())
""",  # 0만 찍히고 999는 안 찍혀야 함
    
    # 단락 평가 활용: 0으로 나누기 회피
    """
x = 0
if x != 0 and 10 / x > 0 {
    print(1)
} else {
    print(0)
}
""",
    
    # 중첩 and/or
    "print((true and false) or true)",       # true
    "print(true and (false or true))",       # true
    "print(not (true and false))",            # true
    "print(not true or not false)",           # true
    
    # 함수 반환에 논리 연산
    """
fn in_range(x: i32, lo: i32, hi: i32) -> i32 {
    if x >= lo and x <= hi {
        return 1
    }
    return 0
}
print(in_range(5, 1, 10))
print(in_range(15, 1, 10))
print(in_range(1, 1, 10))
print(in_range(10, 1, 10))
""",
    
    # ----- 블록 스코프 (6.8) -----
    # 에러 케이스는 인터프리터 비교 테스트 모양에 안 맞아서, 정상 동작만 회귀 보호.
    # "안 보임" 케이스는 따로 수동 검증 + test_danha.py가 인터프리터로 커버.
    
    # 블록 안에서 바깥 변수 수정 가능
    """
x = 10
if 1 > 0 {
    x = 20
}
print(x)
""",
    
    # 같은 이름을 다른 if 블록에서 따로 쓰기 (격리 덕에 충돌 없음)
    """
if 1 > 0 {
    tmp = 1
    print(tmp)
}
if 1 > 0 {
    tmp = 2
    print(tmp)
}
""",
    
    # 중첩 블록에서 바깥 변수 수정
    """
total = 0
i = 0
while i < 3 {
    j = 0
    while j < 2 {
        total = total + 1
        j = j + 1
    }
    i = i + 1
}
print(total)
""",
    
    # 함수 안에서도 블록 스코프 잘 동작 (재귀와 같이)
    """
fn count_down(n: i32) -> i32 {
    if n > 0 {
        local = n * 10
        print(local)
        return count_down(n - 1)
    }
    return 0
}
count_down(3)
""",
    
    # ----- for 루프 정수 범위 (6.9) -----
    
    # 기본 카운트 (끝값 미포함)
    "for i in 0..5 { print(i) }",
    
    # 다른 시작값
    "for i in 3..7 { print(i) }",
    
    # 빈 범위 (start == end)
    """
print(99)
for i in 5..5 { print(i) }
print(100)
""",
    
    # 음수 시작
    "for i in -3..2 { print(i) }",
    
    # 합계 1..10
    """
total = 0
for i in 1..11 { total = total + i }
print(total)
""",
    
    # 변수로 된 끝값
    """
n = 5
for i in 0..n { print(i * i) }
""",
    
    # 식으로 된 끝값
    """
for i in 0..(2 + 3) { print(i) }
""",
    
    # 중첩 for
    """
for i in 1..4 {
    for j in 1..4 {
        print(i * j)
    }
}
""",
    
    # for 안에서 if
    """
for i in 0..10 {
    if i % 2 == 0 {
        print(i)
    }
}
""",
    
    # for 안에서 바깥 변수 갱신
    """
sum = 0
count = 0
for i in 1..6 {
    sum = sum + i
    count = count + 1
}
print(sum)
print(count)
""",
    
    # 함수 안에서 for (factorial)
    """
fn factorial(n: i32) -> i32 {
    result = 1
    for i in 1..(n + 1) {
        result = result * i
    }
    return result
}
print(factorial(5))
print(factorial(7))
""",
    
    # for 끝나고 이어지는 코드
    """
for i in 0..3 { print(i) }
print(999)
""",
    
    # while과 for 섞기
    """
i = 0
while i < 2 {
    for j in 0..3 {
        print(j)
    }
    i = i + 1
}
""",
    
    # ----- 구조체 (6.10) -----
    
    # 기본 읽기
    """
struct Player { hp: i32, atk: i32 }
p = Player { hp: 100, atk: 25 }
print(p.hp)
print(p.atk)
""",
    
    # 어노테이션 없는 옛 문법 (i32 기본값)
    """
struct P { hp, atk }
p = P { hp: 100, atk: 25 }
print(p.hp + p.atk)
""",
    
    # 필드 자기 갱신
    """
struct P { hp: i32 }
p = P { hp: 100 }
p.hp = p.hp - 30
print(p.hp)
""",
    
    # 식에서 필드 사용
    """
struct V { x: i32, y: i32 }
v = V { x: 3, y: 4 }
print(v.x * v.x + v.y * v.y)
""",
    
    # 실수 필드
    """
struct V { x: f64, y: f64 }
v = V { x: 3.0, y: 4.0 }
print(v.x + v.y)
""",
    
    # 정수→실수 자동 승격 (필드 생성 시)
    """
struct V { x: f64 }
v = V { x: 5 }
print(v.x * 2.0)
""",
    
    # 필드 쓰기 후 읽기 여러 번
    """
struct C { n: i32 }
c = C { n: 0 }
c.n = 10
print(c.n)
c.n = 20
print(c.n)
c.n = c.n + 5
print(c.n)
""",
    
    # if 조건에 필드
    """
struct P { hp: i32 }
p = P { hp: 100 }
if p.hp > 50 { print(1) } else { print(0) }
p.hp = 30
if p.hp > 50 { print(1) } else { print(0) }
""",
    
    # for 안에서 필드 누적
    """
struct Sum { total: i32 }
s = Sum { total: 0 }
for i in 1..11 {
    s.total = s.total + i
}
print(s.total)
""",
    
    # while 안에서 필드
    """
struct Counter { n: i32, max: i32 }
c = Counter { n: 0, max: 5 }
while c.n < c.max {
    print(c.n)
    c.n = c.n + 1
}
""",
    
    # 여러 구조체 같이
    """
struct A { x: i32 }
struct B { y: i32 }
a = A { x: 10 }
b = B { y: 20 }
print(a.x + b.y)
""",
    
    # ----- 메서드 (6.11) -----
    
    # 기본 메서드 (필드 수정)
    """
struct P { hp: i32 }
impl P {
    fn damage(self, amt: i32) {
        self.hp = self.hp - amt
    }
}
p = P { hp: 100 }
p.damage(30)
print(p.hp)
""",
    
    # return 있는 메서드
    """
struct V { x: i32, y: i32 }
impl V {
    fn length_sq(self) -> i32 {
        return self.x * self.x + self.y * self.y
    }
}
v = V { x: 3, y: 4 }
print(v.length_sq())
""",
    
    # self만 있는 메서드
    """
struct C { n: i32 }
impl C {
    fn get(self) -> i32 { return self.n }
}
c = C { n: 42 }
print(c.get())
""",
    
    # 여러 메서드
    """
struct C { n: i32 }
impl C {
    fn inc(self) { self.n = self.n + 1 }
    fn dec(self) { self.n = self.n - 1 }
    fn get(self) -> i32 { return self.n }
}
c = C { n: 10 }
c.inc()
c.inc()
c.dec()
print(c.get())
""",
    
    # 메서드가 다른 메서드 호출
    """
struct P { hp: i32 }
impl P {
    fn is_alive(self) -> i32 {
        if self.hp > 0 { return 1 }
        return 0
    }
    fn check(self) -> i32 {
        return self.is_alive()
    }
}
p = P { hp: 50 }
print(p.check())
p.hp = 0
print(p.check())
""",
    
    # for 안에서 메서드 호출 (게임 루프 패턴)
    """
struct C { n: i32 }
impl C {
    fn add(self, x: i32) { self.n = self.n + x }
}
c = C { n: 0 }
for i in 1..6 {
    c.add(i)
}
print(c.n)
""",
    
    # 여러 impl 블록 (같은 구조체)
    """
struct T { v: i32 }
impl T {
    fn one(self) -> i32 { return 1 }
}
impl T {
    fn two(self) -> i32 { return 2 }
}
t = T { v: 0 }
print(t.one() + t.two())
""",
    
    # 실수 필드와 메서드
    """
struct V { x: f64, y: f64 }
impl V {
    fn dot(self, ox: f64, oy: f64) -> f64 {
        return self.x * ox + self.y * oy
    }
}
v = V { x: 3.0, y: 4.0 }
print(v.dot(1.0, 2.0))
""",
    
    # 메서드 호출 결과를 식에 사용
    """
struct V { x: i32, y: i32 }
impl V {
    fn sum(self) -> i32 { return self.x + self.y }
}
a = V { x: 1, y: 2 }
b = V { x: 10, y: 20 }
print(a.sum() + b.sum())
""",
    
    # 메서드 안에서 if/while
    """
struct P { hp: i32 }
impl P {
    fn heal_to(self, target: i32) {
        while self.hp < target {
            self.hp = self.hp + 1
        }
    }
}
p = P { hp: 50 }
p.heal_to(60)
print(p.hp)
""",

    # ===== 7.1.3 참조 매개변수 양성 케이스 =====
    # 인터프리터와 컴파일러가 같은 출력을 내야 함.
    # 인터프리터는 참조/값 구분이 느슨하지만 (dict 전달이 기본 참조), 이 케이스들은
    # 두 경로가 같은 숫자를 찍게 고른 것.

    # 7.1.3: &mut로 받은 구조체 쓰기 → 호출자 원본 바뀜
    """
struct P { hp: i32 }
fn heal(p: &mut P) { p.hp = 99 }
pl = P { hp: 1 }
heal(&mut pl)
print(pl.hp)
""",

    # 7.1.3: & (읽기) 매개변수로 필드 읽기
    """
struct P { hp: i32 }
fn show(p: &P) { print(p.hp) }
pl = P { hp: 42 }
show(&pl)
""",

    # 7.1.3: 참조와 값 매개변수 섞어 쓰기
    """
struct P { hp: i32 }
fn mixed(a: P, b: &mut P) {
    a.hp = 0
    b.hp = 999
}
x = P { hp: 1 }
y = P { hp: 2 }
mixed(x, &mut y)
print(y.hp)
""",

    # 7.1.3: 강등 허용 — &mut를 & 시그니처에 넘김
    """
struct P { hp: i32 }
fn show(p: &P) { print(p.hp) }
pl = P { hp: 42 }
show(&mut pl)
""",

    # 7.1.3: 메서드의 참조 매개변수
    """
struct P { hp: i32 }
impl P {
    fn set_from(self, other: &mut P) { other.hp = 77 }
}
a = P { hp: 1 }
b = P { hp: 2 }
a.set_from(&mut b)
print(b.hp)
""",

    # 7.1.3: 여러 참조 매개변수
    """
struct P { hp: i32 }
fn swap_hp(a: &mut P, b: &mut P) {
    tmp = a.hp
    a.hp = b.hp
    b.hp = tmp
}
x = P { hp: 1 }
y = P { hp: 2 }
swap_hp(&mut x, &mut y)
print(x.hp)
print(y.hp)
""",

    # ----- 7.2: 고정 배열 -----

    # 배열 리터럴 + 인덱스 읽기
    """
arr = [10, 20, 30]
print(arr[0])
print(arr[1])
print(arr[2])
""",

    # 인덱스 쓰기
    """
arr = [1, 2, 3]
arr[1] = 99
print(arr[0])
print(arr[1])
print(arr[2])
""",

    # while로 배열 순회
    """
arr = [10, 20, 30, 40, 50]
sum = 0
i = 0
while i < 5 {
    sum = sum + arr[i]
    i = i + 1
}
print(sum)
""",

    # for 범위로 배열 채우기
    """
arr = [0, 0, 0, 0, 0]
for i in 0..5 {
    arr[i] = i * i
}
print(arr[0])
print(arr[3])
print(arr[4])
""",

    # f64 배열
    """
arr = [1.5, 2.5, 3.5]
print(arr[0])
print(arr[2])
""",

    # i32→f64 자동 승격
    """
arr = [1, 2.5, 3]
print(arr[0])
print(arr[1])
""",

    # 타입 어노테이션 + 배열
    """
arr: [i32; 3] = [10, 20, 30]
print(arr[1])
""",

    # 함수에 배열 전달 (값 복사)
    """
fn sum3(a: [i32; 3]) -> i32 {
    return a[0] + a[1] + a[2]
}
arr = [10, 20, 30]
print(sum3(arr))
""",

    # 함수에서 배열 반환
    """
fn make() -> [i32; 3] {
    return [100, 200, 300]
}
r = make()
print(r[0])
print(r[2])
""",

    # &mut 참조로 배열 수정
    """
fn zero_first(a: &mut [i32; 3]) {
    a[0] = 0
}
arr = [10, 20, 30]
zero_first(&mut arr)
print(arr[0])
print(arr[1])
""",

    # & 읽기 참조로 배열 읽기
    """
fn sum_ref(a: &[i32; 3]) -> i32 {
    return a[0] + a[1] + a[2]
}
arr = [10, 20, 30]
print(sum_ref(&arr))
""",

    # 배열 안에 계산식
    """
x = 5
arr = [x, x + 1, x * 2]
print(arr[0])
print(arr[1])
print(arr[2])
""",

    # ----- 7.3: 배열 for each -----

    # 기본 순회
    """
arr = [10, 20, 30]
for x in arr {
    print(x)
}
""",

    # 합계
    """
arr = [1, 2, 3, 4, 5]
sum = 0
for x in arr {
    sum = sum + x
}
print(sum)
""",

    # f64 배열 순회
    """
arr = [1.5, 2.5, 3.5]
total = 0.0
for x in arr {
    total = total + x
}
print(total)
""",

    # for each 안에서 if
    """
arr = [1, 2, 3, 4, 5, 6]
for x in arr {
    if x % 2 == 0 {
        print(x)
    }
}
""",

    # for each 끝나고 코드 이어짐
    """
arr = [10, 20]
for x in arr {
    print(x)
}
print(999)
""",

    # for each로 최대값 찾기
    """
arr = [3, 7, 2, 9, 1]
best = arr[0]
for x in arr {
    if x > best {
        best = x
    }
}
print(best)
""",

    # 함수 안에서 for each
    """
fn sum_arr(a: [i32; 4]) -> i32 {
    total = 0
    for x in a {
        total = total + x
    }
    return total
}
arr = [10, 20, 30, 40]
print(sum_arr(arr))
""",

    # 참조 배열에 for each
    """
fn sum_ref(a: &[i32; 3]) -> i32 {
    total = 0
    for x in a {
        total = total + x
    }
    return total
}
arr = [100, 200, 300]
print(sum_ref(&arr))
""",

    # 중첩: for range + for each
    """
arr = [10, 20, 30]
for i in 0..2 {
    for x in arr {
        print(x + i)
    }
}
""",

    # ----- 7.4.1: len() 내장 함수 -----

    # 기본 len
    """
arr = [10, 20, 30]
print(len(arr))
""",

    # len으로 for 범위
    """
arr = [1, 2, 3, 4, 5]
for i in 0..len(arr) {
    print(arr[i])
}
""",

    # len + 산술
    """
arr = [10, 20, 30]
print(len(arr) + 100)
""",

    # len을 함수 안에서
    """
fn count(a: [i32; 4]) -> i32 {
    return len(a)
}
arr = [1, 2, 3, 4]
print(count(arr))
""",

    # len + 참조 배열
    """
fn count_ref(a: &[i32; 3]) -> i32 {
    return len(a)
}
arr = [10, 20, 30]
print(count_ref(&arr))
""",

    # ----- 7.4: 동적 배열 -----

    # 빈 리스트 + push + 인덱스
    """
arr = []
push(arr, 10)
push(arr, 20)
push(arr, 30)
print(arr[0])
print(arr[1])
print(arr[2])
""",

    # 동적 배열 len
    """
arr = []
push(arr, 10)
push(arr, 20)
push(arr, 30)
print(len(arr))
""",

    # 인덱스 쓰기
    """
arr = []
push(arr, 1)
push(arr, 2)
push(arr, 3)
arr[1] = 99
print(arr[0])
print(arr[1])
print(arr[2])
""",

    # for each 순회
    """
arr = []
push(arr, 100)
push(arr, 200)
push(arr, 300)
for x in arr {
    print(x)
}
""",

    # 용량 자동 증가 (10개 push)
    """
arr = []
i = 0
while i < 10 {
    push(arr, i * i)
    i = i + 1
}
print(len(arr))
print(arr[0])
print(arr[9])
""",

    # for each + if
    """
arr = []
push(arr, 1)
push(arr, 2)
push(arr, 3)
push(arr, 4)
push(arr, 5)
push(arr, 6)
for x in arr {
    if x % 2 == 0 {
        print(x)
    }
}
""",

    # push 후 합계
    """
arr = []
push(arr, 10)
push(arr, 20)
push(arr, 30)
sum = 0
for x in arr {
    sum = sum + x
}
print(sum)
""",

    # while + len
    """
arr = []
push(arr, 5)
push(arr, 10)
push(arr, 15)
i = 0
while i < len(arr) {
    print(arr[i])
    i = i + 1
}
""",

    # ----- 7.5: 아레나 -----

    # arena_reset 후 새 배열
    """
arr = []
push(arr, 10)
push(arr, 20)
print(len(arr))
arena_reset()
arr2 = []
push(arr2, 100)
print(len(arr2))
print(arr2[0])
""",

    # 여러 동적 배열이 아레나에서 공존
    """
a = []
b = []
push(a, 1)
push(a, 2)
push(b, 10)
push(b, 20)
push(b, 30)
print(len(a))
print(len(b))
print(a[0])
print(b[2])
""",

    # 큰 배열 (용량 여러 번 확장 - 4→8→16→32→64→128)
    """
arr = []
i = 0
while i < 100 {
    push(arr, i)
    i = i + 1
}
print(len(arr))
print(arr[0])
print(arr[50])
print(arr[99])
""",

    # ===== 7.7: 벡터 타입 =====

    # --- 생성과 출력 ---
    """
v = vec2(1.0, 2.0)
print(v)
""",
    """
v = vec3(1.0, 2.0, 3.0)
print(v)
""",
    """
v = vec4(1.0, 2.0, 3.0, 4.0)
print(v)
""",
    # 정수 인자 → f64 자동 승격
    """
v = vec3(1, 2, 3)
print(v)
""",
    # --- 필드 읽기 ---
    """
v = vec3(10.0, 20.0, 30.0)
print(v.x)
print(v.y)
print(v.z)
""",
    """
v = vec2(5.0, 7.0)
print(v.x)
print(v.y)
""",
    """
v = vec4(1.0, 2.0, 3.0, 4.0)
print(v.w)
""",
    # --- 필드 쓰기 ---
    """
v = vec3(1.0, 2.0, 3.0)
v.x = 99.0
print(v)
""",
    """
v = vec2(0.0, 0.0)
v.x = 5
print(v)
""",
    # --- 벡터 + 벡터 ---
    """
a = vec3(1.0, 2.0, 3.0)
b = vec3(4.0, 5.0, 6.0)
print(a + b)
""",
    """
a = vec2(10.0, 20.0)
b = vec2(3.0, 7.0)
print(a - b)
""",
    """
a = vec3(2.0, 3.0, 4.0)
b = vec3(5.0, 6.0, 7.0)
print(a * b)
""",
    """
a = vec3(10.0, 20.0, 30.0)
b = vec3(2.0, 5.0, 10.0)
print(a / b)
""",
    # --- 벡터 * 스칼라, 스칼라 * 벡터 ---
    """
v = vec3(1.0, 2.0, 3.0)
print(v * 2.0)
""",
    """
v = vec3(1.0, 2.0, 3.0)
print(3.0 * v)
""",
    """
v = vec3(10.0, 20.0, 30.0)
print(v / 2.0)
""",
    # 정수 스칼라 곱
    """
v = vec3(1.0, 2.0, 3.0)
print(v * 2)
""",
    # --- 단항 마이너스 ---
    """
v = vec3(1.0, -2.0, 3.0)
print(-v)
""",
    # --- 복합 연산: pos + vel * dt ---
    """
pos = vec3(0.0, 0.0, 0.0)
vel = vec3(10.0, 0.0, -5.0)
dt = 0.016
new_pos = pos + vel * dt
print(new_pos)
""",

    # ===== 7.8a: 벡터 수학 함수 =====

    # length
    """
v = vec3(3.0, 4.0, 0.0)
print(length(v))
""",
    """
v = vec2(3.0, 4.0)
print(length(v))
""",
    """
v = vec3(1.0, 0.0, 0.0)
print(length(v))
""",
    # dot
    """
a = vec3(1.0, 0.0, 0.0)
b = vec3(0.0, 1.0, 0.0)
print(dot(a, b))
""",
    """
a = vec3(2.0, 3.0, 4.0)
b = vec3(2.0, 3.0, 4.0)
print(dot(a, b))
""",
    """
a = vec2(3.0, 4.0)
b = vec2(1.0, 2.0)
print(dot(a, b))
""",
    # normalize
    """
v = vec3(3.0, 4.0, 0.0)
n = normalize(v)
print(length(n))
""",
    """
v = vec3(10.0, 0.0, 0.0)
n = normalize(v)
print(n)
""",
    # cross
    """
x = vec3(1.0, 0.0, 0.0)
y = vec3(0.0, 1.0, 0.0)
print(cross(x, y))
""",
    """
x = vec3(1.0, 0.0, 0.0)
y = vec3(0.0, 1.0, 0.0)
print(cross(y, x))
""",
    """
a = vec3(2.0, 3.0, 4.0)
b = vec3(5.0, 6.0, 7.0)
print(cross(a, b))
""",
    # 복합: 거리 계산
    """
player = vec3(10.0, 0.0, 0.0)
enemy = vec3(13.0, 4.0, 0.0)
dist = length(enemy - player)
print(dist)
""",
    # 복합: 방향 + 속도
    """
from_pos = vec3(0.0, 0.0, 0.0)
to_pos = vec3(6.0, 8.0, 0.0)
dir = normalize(to_pos - from_pos)
speed = 5.0
vel = dir * speed
print(vel)
""",

    # ===== 7.9a: 행렬 mat4 =====
    
    # 단위 행렬
    """
m = mat4_identity()
print(m)
""",
    # 단위 행렬 * vec4 = vec4 그대로
    """
m = mat4_identity()
v = vec4(1.0, 2.0, 3.0, 1.0)
print(m * v)
""",
    # 이동 행렬 * 점
    """
m = mat4_translate(10.0, 20.0, 30.0)
v = vec4(1.0, 2.0, 3.0, 1.0)
result = m * v
print(result)
""",
    # 이동 행렬 * 방향(w=0) 변화 없음
    """
m = mat4_translate(10.0, 20.0, 30.0)
dir = vec4(1.0, 0.0, 0.0, 0.0)
print(m * dir)
""",
    # 크기 행렬 * 벡터
    """
m = mat4_scale(2.0, 3.0, 4.0)
v = vec4(1.0, 1.0, 1.0, 1.0)
print(m * v)
""",
    # 단위 * 단위 = 단위
    """
a = mat4_identity()
b = mat4_identity()
print(a * b)
""",
    # 이동 합성
    """
a = mat4_translate(5.0, 0.0, 0.0)
b = mat4_translate(3.0, 0.0, 0.0)
combined = a * b
v = vec4(0.0, 0.0, 0.0, 1.0)
print(combined * v)
""",
    # 크기 후 이동
    """
s = mat4_scale(2.0, 2.0, 2.0)
t = mat4_translate(10.0, 0.0, 0.0)
combined = t * s
v = vec4(1.0, 1.0, 1.0, 1.0)
print(combined * v)
""",

    # ===== 7.9b: 회전 행렬 =====
    
    # rotate_y 90도: X축 벡터 → Z축 음의 방향
    """
angle = 1.5707963267948966
m = mat4_rotate_y(angle)
v = vec4(1.0, 0.0, 0.0, 1.0)
result = m * v
print(result.x < 0.0001)
print(result.z < -0.999)
""",
    # rotate_z 90도: X축 벡터 → Y축 방향
    """
angle = 1.5707963267948966
m = mat4_rotate_z(angle)
v = vec4(1.0, 0.0, 0.0, 1.0)
result = m * v
print(result.x < 0.0001)
print(result.y > 0.999)
""",
    # rotate_x 90도: Y축 벡터 → Z축 방향
    """
angle = 1.5707963267948966
m = mat4_rotate_x(angle)
v = vec4(0.0, 1.0, 0.0, 1.0)
result = m * v
print(result.y < 0.0001)
print(result.z > 0.999)
""",
    # rotate_y 0도 = 단위 행렬
    """
m = mat4_rotate_y(0.0)
v = vec4(1.0, 2.0, 3.0, 1.0)
result = m * v
print(result)
""",
    # 회전 후 이동 합성
    """
angle = 1.5707963267948966
r = mat4_rotate_y(angle)
t = mat4_translate(10.0, 0.0, 0.0)
combined = t * r
v = vec4(1.0, 0.0, 0.0, 1.0)
result = combined * v
print(result.x > 9.999)
print(result.x < 10.001)
""",

    # ===== 7.9c: transpose, inverse =====
    
    # 단위 행렬 전치 = 단위
    """
m = mat4_identity()
t = mat4_transpose(m)
print(t)
""",
    # 단위 행렬 역 = 단위
    """
m = mat4_identity()
inv = mat4_inverse(m)
print(inv)
""",
    # 이동 역행렬
    """
m = mat4_translate(5.0, 0.0, 0.0)
inv = mat4_inverse(m)
v = vec4(0.0, 0.0, 0.0, 1.0)
print(inv * v)
""",
    # M * inverse(M) 검증
    """
m = mat4_translate(3.0, 7.0, -2.0)
inv = mat4_inverse(m)
result = m * inv
v = vec4(42.0, 13.0, -5.0, 1.0)
back = result * v
print(back.x)
print(back.y)
print(back.z)
""",
    # 크기 역행렬
    """
m = mat4_scale(2.0, 4.0, 5.0)
inv = mat4_inverse(m)
v = vec4(1.0, 1.0, 1.0, 1.0)
print(inv * v)
""",

    # ----- 7.12a: component 파서만 연결. 실행 의미는 아직 없음. -----
    # 컴파일러에서도 component 정의가 크래시 없이 무시되고
    # 인터프리터와 같은 결과가 나오는지 확인.
    """
component Position { x: f64, y: f64, z: f64 }
print(42)
""",
    """
component Health { current, max }
print(1)
""",
    """
component Position { x: f64, y: f64 }
component Velocity { x: f64, y: f64 }
component Health { hp }
print(7)
""",
    """
component Transform {
    pos: vec3
    rot: f64
    scale: vec3
}
print(9)
""",
    # struct와 component 공존 — 기존 struct 경로가 안 깨졌음을 명시적으로 확인
    """
struct Point { x: i32, y: i32 }
component Position { x: f64, y: f64 }
p = Point { x: 10, y: 20 }
print(p.x)
print(p.y)
""",

    # ----- 7.12d1: 엔티티 생명주기 (컴파일러) -----
    # 인터프리터 7.12b와 동일한 결과가 나와야 함 (의미 일치 약속).
    "e = spawn()\nprint(e)",
    
    """
a = spawn()
b = spawn()
c = spawn()
print(a)
print(b)
print(c)
""",
    
    # destroy 후 재사용: 같은 index, 다음 세대
    """
a = spawn()
b = spawn()
destroy(a)
c = spawn()
print(a)
print(b)
print(c)
""",
    
    # is_alive 기본 동작
    """
a = spawn()
print(is_alive(a))
destroy(a)
print(is_alive(a))
""",
    
    # 낡은 참조 감지: 자리를 다른 엔티티가 차지해도 옛 ID는 false
    """
a = spawn()
destroy(a)
b = spawn()
print(is_alive(a))
print(is_alive(b))
""",
    
    # destroy 반환값
    """
a = spawn()
print(destroy(a))
print(destroy(a))
""",
    
    # if 조건에서 is_alive
    """
e = spawn()
if is_alive(e) { print(100) } else { print(200) }
destroy(e)
if is_alive(e) { print(300) } else { print(400) }
""",
    
    # 루프 + spawn
    """
i = 0
while i < 5 {
    e = spawn()
    print(e)
    i = i + 1
}
""",
    
    # LIFO 재사용: destroy 순서 A, B → spawn 순서는 B, A
    """
a = spawn()
b = spawn()
c = spawn()
destroy(a)
destroy(b)
d = spawn()
e = spawn()
print(d)
print(e)
""",
    
    # 여러 엔티티 중 하나만 죽이기
    """
a = spawn()
b = spawn()
c = spawn()
destroy(b)
print(is_alive(a))
print(is_alive(b))
print(is_alive(c))
""",
    
    # component 정의와 엔티티 조합 (컴포넌트 부착은 d2에서)
    """
component Position { x: f64, y: f64 }
e = spawn()
print(e)
""",

    # ----- 7.12d2: add / has / remove -----
    # has 전후
    """
component Position { x, y }
e = spawn()
print(has(e, Position))
add(e, Position { x: 1.0, y: 2.0 })
print(has(e, Position))
remove(e, Position)
print(has(e, Position))
""",

    # remove 반환값
    """
component Position { x, y }
e = spawn()
print(remove(e, Position))
add(e, Position { x: 1.0, y: 2.0 })
print(remove(e, Position))
""",

    # destroy가 컴포넌트 정리 → 재사용 자리에 안 묻음
    """
component Position { x, y }
a = spawn()
add(a, Position { x: 42.0, y: 42.0 })
destroy(a)
b = spawn()
print(has(b, Position))
""",

    # has: 죽은 엔티티 → false
    """
component Position { x, y }
e = spawn()
add(e, Position { x: 1.0, y: 2.0 })
destroy(e)
print(has(e, Position))
""",

    # 여러 컴포넌트 종류 공존
    """
component Position { x, y }
component Velocity { x, y }
e = spawn()
add(e, Position { x: 0.0, y: 0.0 })
add(e, Velocity { x: 1.0, y: 2.0 })
print(has(e, Position))
print(has(e, Velocity))
""",

    # 여러 엔티티에 같은 컴포넌트
    """
component Position { x, y }
a = spawn()
b = spawn()
c = spawn()
add(a, Position { x: 1.0, y: 1.0 })
add(b, Position { x: 2.0, y: 2.0 })
add(c, Position { x: 3.0, y: 3.0 })
print(has(a, Position))
print(has(b, Position))
print(has(c, Position))
""",

    # swap-remove: 중간 엔티티 제거해도 나머지 유지
    """
component Position { x, y }
a = spawn()
b = spawn()
c = spawn()
add(a, Position { x: 1.0, y: 1.0 })
add(b, Position { x: 2.0, y: 2.0 })
add(c, Position { x: 3.0, y: 3.0 })
remove(b, Position)
print(has(a, Position))
print(has(b, Position))
print(has(c, Position))
""",

    # ----- 7.12d3: get + 필드 접근 -----

    # 기본 get
    """
component Position { x, y }
e = spawn()
add(e, Position { x: 10.0, y: 20.0 })
print(get(e, Position))
""",

    # get + 필드 접근
    """
component Position { x, y }
e = spawn()
add(e, Position { x: 10.0, y: 20.0 })
p = get(e, Position)
print(p.x)
print(p.y)
""",

    # 여러 엔티티 조회
    """
component Position { x, y }
a = spawn()
b = spawn()
add(a, Position { x: 1.0, y: 1.0 })
add(b, Position { x: 2.0, y: 2.0 })
print(get(a, Position))
print(get(b, Position))
""",

    # 덮어쓰기 후 get
    """
component Position { x, y }
e = spawn()
add(e, Position { x: 1.0, y: 1.0 })
add(e, Position { x: 99.0, y: 99.0 })
print(get(e, Position))
""",

    # 두 컴포넌트 종류
    """
component Position { x, y }
component Velocity { x, y }
e = spawn()
add(e, Position { x: 0.0, y: 0.0 })
add(e, Velocity { x: 1.0, y: 2.0 })
print(get(e, Position))
print(get(e, Velocity))
""",

    # i32 필드 값 → f64 저장/반환
    """
component Hp { current, max }
e = spawn()
add(e, Hp { current: 50, max: 100 })
print(get(e, Hp))
""",

    # 게임다운 시뮬: 위치를 속도로 10번 업데이트
    """
component Position { x, y }
component Velocity { x, y }
e = spawn()
add(e, Position { x: 0.0, y: 0.0 })
add(e, Velocity { x: 1.0, y: 2.0 })
i = 0
while i < 10 {
    p = get(e, Position)
    v = get(e, Velocity)
    add(e, Position { x: p.x + v.x, y: p.y + v.y })
    i = i + 1
}
print(get(e, Position))
""",

    # swap-remove 후 남은 엔티티 데이터 일치 확인
    """
component Position { x, y }
a = spawn()
b = spawn()
c = spawn()
add(a, Position { x: 1.0, y: 1.0 })
add(b, Position { x: 2.0, y: 2.0 })
add(c, Position { x: 3.0, y: 3.0 })
remove(b, Position)
print(get(a, Position))
print(get(c, Position))
""",

    # ===== 7.13 system 문법 =====

    # system — 단일 바인딩, 매개변수 없음
    """
component Position { x, y }

system reset_pos() {
    for each (p: Position) {
        p.x = 0
        p.y = 0
    }
}

e = spawn()
add(e, Position { x: 5, y: 10 })
reset_pos()
p = get(e, Position)
print(p.x)
print(p.y)
""",

    # system — 매개변수 + 단일 바인딩
    """
component Position { x, y }

system move_all(dx: f64) {
    for each (p: Position) {
        p.x = p.x + dx
    }
}

e1 = spawn()
add(e1, Position { x: 1, y: 2 })
e2 = spawn()
add(e2, Position { x: 10, y: 20 })
move_all(5)
p1 = get(e1, Position)
p2 = get(e2, Position)
print(p1.x)
print(p2.x)
""",

    # system — 다중 컴포넌트 교집합 순회
    """
component Position { x, y }
component Velocity { vx, vy }

system update_movement(dt: f64) {
    for each (p: Position, v: Velocity) {
        p.x = p.x + v.vx * dt
        p.y = p.y + v.vy * dt
    }
}

e1 = spawn()
add(e1, Position { x: 0, y: 0 })
add(e1, Velocity { vx: 10, vy: 20 })

e2 = spawn()
add(e2, Position { x: 100, y: 200 })

update_movement(0.5)
p1 = get(e1, Position)
p2 = get(e2, Position)
print(p1.x)
print(p1.y)
print(p2.x)
print(p2.y)
""",

    # system — 여러 번 호출
    """
component Position { x, y }
component Velocity { vx, vy }

system step(dt: f64) {
    for each (p: Position, v: Velocity) {
        p.x = p.x + v.vx * dt
        p.y = p.y + v.vy * dt
    }
}

e = spawn()
add(e, Position { x: 0, y: 0 })
add(e, Velocity { vx: 2, vy: 4 })

step(1)
step(1)
step(1)
p = get(e, Position)
print(p.x)
print(p.y)
""",

    # system — 엔티티 없으면 아무 일도 안 함
    """
component Position { x, y }

system do_nothing() {
    for each (p: Position) {
        p.x = 999
    }
}

do_nothing()
print(1)
""",

    # system — destroy된 엔티티는 순회 안 함
    """
component Position { x, y }

system count_print() {
    for each (p: Position) {
        print(p.x)
    }
}

e1 = spawn()
add(e1, Position { x: 1, y: 0 })
e2 = spawn()
add(e2, Position { x: 2, y: 0 })
e3 = spawn()
add(e3, Position { x: 3, y: 0 })

destroy(e2)
count_print()
""",

    # system — 3개 컴포넌트 교집합
    """
component Position { x, y }
component Velocity { vx, vy }
component Health { hp }

system apply_all(dt: f64) {
    for each (p: Position, v: Velocity, h: Health) {
        p.x = p.x + v.vx * dt
        h.hp = h.hp - 1
    }
}

e1 = spawn()
add(e1, Position { x: 0, y: 0 })
add(e1, Velocity { vx: 10, vy: 0 })
add(e1, Health { hp: 100 })

e2 = spawn()
add(e2, Position { x: 0, y: 0 })
add(e2, Velocity { vx: 5, vy: 0 })

apply_all(1)
p1 = get(e1, Position)
h1 = get(e1, Health)
print(p1.x)
print(h1.hp)
""",

    # ===== 7.11 parallel system =====

    # parallel system — 기본 동작
    """
component Position { x, y }
component Velocity { vx, vy }

parallel system update_movement(dt: f64) {
    for each (p: Position, v: Velocity) {
        p.x = p.x + v.vx * dt
        p.y = p.y + v.vy * dt
    }
}

e1 = spawn()
add(e1, Position { x: 0, y: 0 })
add(e1, Velocity { vx: 10, vy: 20 })

e2 = spawn()
add(e2, Position { x: 100, y: 200 })
add(e2, Velocity { vx: 5, vy: 10 })

update_movement(1)
p1 = get(e1, Position)
p2 = get(e2, Position)
print(p1.x)
print(p1.y)
print(p2.x)
print(p2.y)
""",

    # parallel system — 읽기만 하는 바인딩
    """
component Position { x, y }
component Tag { val }

parallel system print_tagged(dt: f64) {
    for each (p: Position, t: Tag) {
        print(p.x)
    }
}

e = spawn()
add(e, Position { x: 42, y: 0 })
add(e, Tag { val: 1 })
print_tagged(0)
""",

    # ===== 7.6 소유권 (읽기 전용 store back 생략) =====

    # 읽기 전용 바인딩 — 쓰기 안 해도 값이 보존됨
    """
component Position { x, y }
component Velocity { vx, vy }

system update(dt: f64) {
    for each (p: Position, v: Velocity) {
        p.x = p.x + v.vx * dt
    }
}

e = spawn()
add(e, Position { x: 0, y: 0 })
add(e, Velocity { vx: 10, vy: 20 })

update(1)
v = get(e, Velocity)
print(v.vx)
print(v.vy)
""",

    # 읽기 전용 바인딩 — 여러 엔티티
    """
component Position { x, y }
component Tag { val }

system move_tagged(dx: f64) {
    for each (p: Position, t: Tag) {
        p.x = p.x + t.val * dx
    }
}

e1 = spawn()
add(e1, Position { x: 0, y: 0 })
add(e1, Tag { val: 3 })

e2 = spawn()
add(e2, Position { x: 100, y: 0 })
add(e2, Tag { val: 5 })

move_tagged(10)
p1 = get(e1, Position)
p2 = get(e2, Position)
t1 = get(e1, Tag)
t2 = get(e2, Tag)
print(p1.x)
print(p2.x)
print(t1.val)
print(t2.val)
""",

    # ===== 7.8b 트레잇 =====

    # trait 기본 — impl로 메서드 구현
    """
trait Describable {
    fn describe(self) -> i32 {
        return 0
    }
}

struct Enemy { hp: i32 }

impl Describable for Enemy {
    fn describe(self) -> i32 {
        return self.hp
    }
}

e = Enemy { hp: 42 }
print(e.describe())
""",

    # trait 기본 구현 사용
    """
trait HasDefault {
    fn get_val(self) -> i32 {
        return 999
    }
}

struct Empty { x: i32 }

impl HasDefault for Empty {
}

e = Empty { x: 1 }
print(e.get_val())
""",

    # trait — 여러 타입이 같은 트레잇 구현
    """
trait Printable {
    fn value(self) -> i32 {
        return 0
    }
}

struct Cat { lives: i32 }
struct Dog { tricks: i32 }

impl Printable for Cat {
    fn value(self) -> i32 {
        return self.lives
    }
}

impl Printable for Dog {
    fn value(self) -> i32 {
        return self.tricks
    }
}

c = Cat { lives: 9 }
d = Dog { tricks: 3 }
print(c.value())
print(d.value())
""",

    # trait + 일반 impl 공존
    """
trait Greetable {
    fn greet(self) -> i32 {
        return 0
    }
}

struct Player { hp: i32 }

impl Player {
    fn get_hp(self) -> i32 {
        return self.hp
    }
}

impl Greetable for Player {
    fn greet(self) -> i32 {
        return 1
    }
}

p = Player { hp: 100 }
print(p.get_hp())
print(p.greet())
""",

    # ----- 7.14: schedule (system 스케줄링) -----
    
    # 의존 순서 자동 결정: apply_gravity(Velocity 쓰기) → move(Velocity 읽기)
    """
component Position { x, y }
component Velocity { vx, vy }

system apply_gravity(dt: f64) {
    for each (v: Velocity) {
        v.vy = v.vy - 9.8 * dt
    }
}

system move(dt: f64) {
    for each (p: Position, v: Velocity) {
        p.x = p.x + v.vx * dt
        p.y = p.y + v.vy * dt
    }
}

e = spawn()
add(e, Position { x: 0.0, y: 0.0 })
add(e, Velocity { vx: 10.0, vy: 0.0 })

schedule(1.0)

vel = get(e, Velocity)
print(vel.vy)
pos = get(e, Position)
print(pos.y)
""",

    # 정의 순서 반대여도 올바른 실행 순서
    """
component Position { x, y }
component Velocity { vx, vy }

system move(dt: f64) {
    for each (p: Position, v: Velocity) {
        p.x = p.x + v.vx * dt
    }
}

system apply_gravity(dt: f64) {
    for each (v: Velocity) {
        v.vx = v.vx + 1.0 * dt
    }
}

e = spawn()
add(e, Position { x: 0.0, y: 0.0 })
add(e, Velocity { vx: 0.0, vy: 0.0 })

schedule(1.0)

pos = get(e, Position)
print(pos.x)
""",

    # 3개 system 체인
    """
component Position { x, y }
component Velocity { vx, vy }
component Render { visible }

system render_check(dt: f64) {
    for each (p: Position, r: Render) {
        print(p.x)
    }
}

system move(dt: f64) {
    for each (p: Position, v: Velocity) {
        p.x = p.x + v.vx * dt
    }
}

system apply_force(dt: f64) {
    for each (v: Velocity) {
        v.vx = v.vx + 5.0 * dt
    }
}

e = spawn()
add(e, Position { x: 0.0, y: 0.0 })
add(e, Velocity { vx: 0.0, vy: 0.0 })
add(e, Render { visible: 1.0 })

schedule(1.0)
""",

    # 독립 system은 등록 순서 유지
    """
component Health { hp }
component Mana { mp }

system regen_hp(dt: f64) {
    for each (h: Health) {
        h.hp = h.hp + 1.0 * dt
    }
}

system regen_mp(dt: f64) {
    for each (m: Mana) {
        m.mp = m.mp + 2.0 * dt
    }
}

e = spawn()
add(e, Health { hp: 100.0 })
add(e, Mana { mp: 50.0 })

schedule(1.0)

h = get(e, Health)
print(h.hp)
m = get(e, Mana)
print(m.mp)
""",

    # 여러 엔티티
    """
component Position { x, y }
component Velocity { vx, vy }

system move(dt: f64) {
    for each (p: Position, v: Velocity) {
        p.x = p.x + v.vx * dt
    }
}

e1 = spawn()
add(e1, Position { x: 0.0, y: 0.0 })
add(e1, Velocity { vx: 3.0, vy: 0.0 })

e2 = spawn()
add(e2, Position { x: 10.0, y: 0.0 })
add(e2, Velocity { vx: -1.0, vy: 0.0 })

schedule(1.0)

p1 = get(e1, Position)
print(p1.x)
p2 = get(e2, Position)
print(p2.x)
""",

    # schedule 두 번 호출 (2프레임)
    """
component Position { x, y }
component Velocity { vx, vy }

system move(dt: f64) {
    for each (p: Position, v: Velocity) {
        p.x = p.x + v.vx * dt
    }
}

e = spawn()
add(e, Position { x: 0.0, y: 0.0 })
add(e, Velocity { vx: 5.0, vy: 0.0 })

schedule(1.0)
schedule(1.0)

pos = get(e, Position)
print(pos.x)
""",

    # 문제 4: 양방향 read/write 교차는 허용 (상호 참조 패턴)
    # sys_a: Position 쓰기, Velocity 읽기
    # sys_b: Velocity 쓰기, Position 읽기
    # → 이전엔 순환 의존으로 거부됐지만 이제는 등록 순서(a→b) 유지
    """
component Position { x, y }
component Velocity { vx, vy }

system sys_a(dt: f64) {
    for each (p: Position, v: Velocity) {
        p.x = p.x + v.vx * dt
    }
}

system sys_b(dt: f64) {
    for each (p: Position, v: Velocity) {
        v.vx = v.vx + p.x * dt
    }
}

e = spawn()
add(e, Position { x: 1.0, y: 0.0 })
add(e, Velocity { vx: 1.0, vy: 0.0 })

schedule(1.0)

pos = get(e, Position)
vel = get(e, Velocity)
print(pos.x)
print(vel.vx)
""",

    # ----- 7.15e: 시그니처 기반 의존 분석 (& / &mut 명시) -----
    # &mut로 쓰기 약속
    """
component Position { x, y }

system move(dt: f64) {
    for each (p: &mut Position) {
        p.x = p.x + dt * 2.0
    }
}

e = spawn()
add(e, Position { x: 0.0, y: 0.0 })
schedule(1.5)
p = get(e, Position)
print(p.x)
""",

    # 시그니처 기반 양방향 교차 (몬스터 AI 패턴)
    """
component Position { x, y }
component AIState { phase }

system ai_think(dt: f64) {
    for each (p: &Position, s: &mut AIState) {
        if p.x > 5.0 {
            s.phase = 1.0
        } else {
            s.phase = 0.0
        }
    }
}

system ai_act(dt: f64) {
    for each (p: &mut Position, s: &AIState) {
        if s.phase > 0.5 {
            p.x = p.x + 2.0 * dt
        } else {
            p.x = p.x + 0.5 * dt
        }
    }
}

e = spawn()
add(e, Position { x: 0.0, y: 0.0 })
add(e, AIState { phase: 0.0 })

i = 0
while i < 15 {
    schedule(1.0)
    i = i + 1
}
p = get(e, Position)
print(p.x)
""",

    # ----- 7.15f: 컴포넌트 필드 실제 타입 -----
    # i32 필드
    """
component Score { value: i32 }
e = spawn()
add(e, Score { value: 42 })
s = get(e, Score)
print(s.value)
""",

    # 혼합 타입 컴포넌트 (i32/u8/f32/f64)
    """
component Mixed {
    hp: i32
    team: u8
    mass: f32
    pos_x: f64
}
e = spawn()
add(e, Mixed { hp: 100, team: 2, mass: 7.5, pos_x: 3.14 })
m = get(e, Mixed)
print(m.hp)
print(m.team)
print(m.mass)
print(m.pos_x)
""",

    # 정수 리터럴 → f64 명시 필드 암묵 승격 (Q3=3)
    """
component Position { x: f64, y: f64 }
e = spawn()
add(e, Position { x: 1, y: 2 })
p = get(e, Position)
print(p.x)
print(p.y)
""",

    # 타입 생략 필드는 f64 기본 (Q1=1 호환)
    """
component Pos { x, y }
e = spawn()
add(e, Pos { x: 1.5, y: 2.5 })
p = get(e, Pos)
print(p.x)
print(p.y)
""",

    # system이 i32 필드 수정 (for each에서 타입 유지)
    """
component Score { value: i32 }
system inc(dt: f64) {
    for each (s: &mut Score) {
        s.value = s.value + 1
    }
}
e = spawn()
add(e, Score { value: 10 })
schedule(1.0)
schedule(1.0)
schedule(1.0)
s = get(e, Score)
print(s.value)
""",

    # 다양한 정수 크기 (i8/i16/i64)
    """
component Sizes {
    a: i8
    b: i16
    c: i64
}
e = spawn()
add(e, Sizes { a: 7, b: 1000, c: 999999999 })
s = get(e, Sizes)
print(s.a)
print(s.b)
print(s.c)
""",

    # ----- 7.15a: const -----
    "const X = 42\nprint(X)",
    "const G = 10\nprint(G * 2)",
    "const PI = 3.14\nprint(PI)",
    """
const GRAVITY = 9.8
const DT = 0.5
print(GRAVITY * DT)
""",
    """
const MAX_HP = 100
hp = 80
if hp < MAX_HP {
    print(1)
}
""",
    """
const SPEED = 5
fn double(x: i32) -> i32 { return x * 2 }
print(double(SPEED))
""",

    # ----- 7.15b: enum -----
    """
enum Color { Red, Green, Blue }
print(Color.Red)
print(Color.Green)
print(Color.Blue)
""",
    """
enum Phase { Patrol, Chase, Attack }
state = Phase.Chase
if state == Phase.Chase {
    print(1)
}
if state == Phase.Patrol {
    print(0)
}
""",
    """
enum Dir { Up, Down, Left, Right }
d = Dir.Left
if d == Dir.Left {
    print(2)
}
""",
    """
enum Phase { Patrol, Chase }
fn is_chasing(p: i32) -> i32 {
    if p == 1 {
        return 1
    }
    return 0
}
print(is_chasing(Phase.Chase))
print(is_chasing(Phase.Patrol))
""",
    """
enum State { Idle, Running }
const INITIAL = State.Idle
s = INITIAL
if s == State.Idle {
    print(42)
}
""",
    """
enum Phase { Patrol, Chase, Attack }
component AIState { phase }

e = spawn()
add(e, AIState { phase: Phase.Chase })

ai = get(e, AIState)
if ai.phase == Phase.Chase {
    print(1)
}
""",

    # ----- 7.15c: 문자열 -----
    'print("hello")',
    's = "world"\nprint(s)',
    """
s = "hello"
if s == "hello" {
    print(1)
}
if s == "world" {
    print(0)
}
""",
    """
s = "hello"
if s != "world" {
    print(42)
}
""",
    """
print("first")
print("second")
print("third")
""",
    """
s = ""
if s == "" {
    print(1)
}
""",

    # ----- 7.15d: break / continue -----
    """
for i in 0..10 {
    if i == 3 {
        break
    }
    print(i)
}
""",
    """
for i in 0..5 {
    if i == 2 {
        continue
    }
    print(i)
}
""",
    """
i = 0
while true {
    if i == 3 {
        break
    }
    print(i)
    i = i + 1
}
""",
    """
i = 0
while i < 6 {
    i = i + 1
    if i % 2 == 0 {
        continue
    }
    print(i)
}
""",
    """
for i in 0..3 {
    for j in 0..5 {
        if j == 2 {
            break
        }
        print(i * 10 + j)
    }
}
""",
    """
for i in 0..2 {
    for j in 0..4 {
        if j == 1 {
            continue
        }
        print(i * 10 + j)
    }
}
""",
    """
enum Phase { Alive, Dead }
component AI { phase }

e1 = spawn()
add(e1, AI { phase: Phase.Alive })
e2 = spawn()
add(e2, AI { phase: Phase.Dead })
e3 = spawn()
add(e3, AI { phase: Phase.Alive })

system think(dt: f64) {
    for each (a: AI) {
        if a.phase == Phase.Dead {
            continue
        }
        print(1)
    }
}

schedule(1.0)
""",
    """
component T { v }
e1 = spawn()
add(e1, T { v: 0.0 })
e2 = spawn()
add(e2, T { v: 0.0 })
e3 = spawn()
add(e3, T { v: 0.0 })

system once(dt: f64) {
    for each (t: T) {
        print(100)
        break
    }
}

schedule(1.0)
""",

    # ----- 7.15d: 몬스터 AI 통합 패턴 -----
    """
enum Phase { Patrol, Chase }
component Position { x, y }
component AIState { phase }
component PlayerTag { v }

const DETECT_SQ = 25.0

fn dist_sq(ax: f64, ay: f64, bx: f64, by: f64) -> f64 {
    dx = ax - bx
    dy = ay - by
    return dx * dx + dy * dy
}

player = spawn()
add(player, Position { x: 0.0, y: 0.0 })
add(player, PlayerTag { v: 1.0 })

mob = spawn()
add(mob, Position { x: 3.0, y: 0.0 })
add(mob, AIState { phase: Phase.Patrol })

system ai_think(dt: f64, px: f64, py: f64) {
    for each (p: Position, a: AIState) {
        d2 = dist_sq(p.x, p.y, px, py)
        if a.phase == Phase.Patrol {
            if d2 <= DETECT_SQ {
                a.phase = Phase.Chase
            }
        }
    }
}

ppos = get(player, Position)
ai_think(0.016, ppos.x, ppos.y)

a = get(mob, AIState)
print(a.phase)
""",
    """
enum Phase { Alive, Dead }
component AIState { phase }
component Health { hp }

m1 = spawn()
add(m1, AIState { phase: Phase.Alive })
add(m1, Health { hp: 100.0 })

m2 = spawn()
add(m2, AIState { phase: Phase.Dead })
add(m2, Health { hp: 0.0 })

m3 = spawn()
add(m3, AIState { phase: Phase.Alive })
add(m3, Health { hp: 50.0 })

system tick(dt: f64) {
    for each (a: AIState, h: Health) {
        if a.phase == Phase.Dead {
            continue
        }
        h.hp = h.hp - 10.0
    }
}

tick(1.0)

h1 = get(m1, Health)
print(h1.hp)
h2 = get(m2, Health)
print(h2.hp)
h3 = get(m3, Health)
print(h3.hp)
""",

    # ----- 7.16a: extern fn (C-FFI 기초) -----
    # extern fn 선언만 — 호출 없이. 인터프리터와 컴파일러 모두 통과해야 함.
    """
extern fn some_c_function(x: i32) -> i32
print(42)
""",

    # extern fn 반환타입 없는 경우 (void). 호출 없이 선언만.
    """
extern fn do_nothing()
print(99)
""",

    # ----- 13단계: Tagged Union -----

    # 기본 tagged enum 생성 + print
    """
enum Shape {
    Circle(f64)
    Rect(f64, f64)
    None
}
print(Shape.Circle(5.0))
print(Shape.Rect(3.0, 4.0))
print(Shape.None)
""",

    # match 기본 — 데이터 있는 variant
    """
enum Shape {
    Circle(f64)
    Rect(f64, f64)
}
x = Shape.Circle(5.0)
match x {
    Circle(r) => {
        print(r)
    }
    Rect(w, h) => {
        print(w + h)
    }
}
""",

    # match — 두 번째 arm 매칭
    """
enum Shape {
    Circle(f64)
    Rect(f64, f64)
}
x = Shape.Rect(3.0, 4.0)
match x {
    Circle(r) => { print(r) }
    Rect(w, h) => { print(w * h) }
}
""",

    # match — 데이터 없는 variant
    """
enum Action {
    Move(f64, f64)
    Attack(i32)
    Wait
}
x = Action.Wait
match x {
    Move(dx, dy) => { print(dx) }
    Attack(dmg) => { print(dmg) }
    Wait => { print(0) }
}
""",

    # match — 와일드카드
    """
enum Action {
    Move(f64, f64)
    Attack(i32)
    Wait
}
x = Action.Wait
match x {
    Move(dx, dy) => { print(dx) }
    _ => { print(999) }
}
""",

    # match — payload로 계산
    """
enum Shape {
    Circle(f64)
    Rect(f64, f64)
}
s = Shape.Rect(5.0, 3.0)
match s {
    Circle(r) => { print(r * r * 3.14) }
    Rect(w, h) => { print(w * h) }
}
""",

    # 함수 인자 + match
    """
enum Shape {
    Circle(f64)
    Rect(f64, f64)
}

fn area(s: Shape) -> f64 {
    match s {
        Circle(r) => {
            return r * r * 3.14
        }
        Rect(w, h) => {
            return w * h
        }
    }
}

print(area(Shape.Circle(10.0)))
print(area(Shape.Rect(3.0, 4.0)))
""",

    # 함수 반환값
    """
enum Result {
    Ok(i32)
    Err(str)
}

fn divide(a: i32, b: i32) -> Result {
    if b == 0 {
        return Result.Err("zero")
    }
    return Result.Ok(a / b)
}

r = divide(10, 2)
match r {
    Ok(v) => { print(v) }
    Err(msg) => { print(msg) }
}
""",

    # 여러 match 연속
    """
enum Result {
    Ok(i32)
    Err(str)
}

fn safe_div(a: i32, b: i32) -> Result {
    if b == 0 { return Result.Err("div0") }
    return Result.Ok(a / b)
}

r1 = safe_div(10, 2)
r2 = safe_div(10, 0)

match r1 {
    Ok(v) => { print(v) }
    Err(e) => { print(e) }
}
match r2 {
    Ok(v) => { print(v) }
    Err(e) => { print(e) }
}
""",

    # 혼합 variant (데이터 있는 + 없는)
    """
enum Msg {
    Quit
    Move(f64, f64)
    Say(str)
}

fn handle(m: Msg) -> i32 {
    match m {
        Quit => { return 0 }
        Move(x, y) => { return 1 }
        Say(s) => { return 2 }
    }
}

print(handle(Msg.Quit))
print(handle(Msg.Move(1.0, 2.0)))
print(handle(Msg.Say("hi")))
""",

    # i32 + f64 혼합 payload
    """
enum Damage {
    Physical(i32)
    Magical(i32, f64)
    Pure
}

fn calc(d: Damage) -> f64 {
    match d {
        Physical(atk) => { return atk + 0.0 }
        Magical(base, mult) => { return base * mult }
        Pure => { return 9999.0 }
    }
}

print(calc(Damage.Physical(50)))
print(calc(Damage.Magical(30, 1.5)))
print(calc(Damage.Pure))
""",

    # 게임 이벤트 패턴
    """
enum GameEvent {
    PlayerMove(f64, f64)
    PlayerAttack(i32)
    ItemPickup(str)
    Quit
}

fn handle_event(e: GameEvent) -> i32 {
    match e {
        PlayerMove(x, y) => { return 1 }
        PlayerAttack(dmg) => { return 2 }
        ItemPickup(name) => { return 3 }
        Quit => { return 0 }
    }
}

print(handle_event(GameEvent.PlayerMove(10.0, 20.0)))
print(handle_event(GameEvent.PlayerAttack(50)))
print(handle_event(GameEvent.ItemPickup("sword")))
print(handle_event(GameEvent.Quit))
""",

    # 변수에 저장 후 match
    """
enum Color {
    Red
    Custom(i32, i32, i32)
}
c = Color.Custom(255, 128, 0)
match c {
    Red => { print(0) }
    Custom(r, g, b) => { print(r + g + b) }
}
""",

    # 13c: match + return으로 unwrap 패턴
    """
enum Option {
    Some(i32)
    None
}

fn unwrap(o: Option) -> i32 {
    match o {
        Some(v) => { return v }
        None => { return -1 }
    }
}

print(unwrap(Option.Some(42)))
print(unwrap(Option.None))
""",

    # ----- 14단계: 제네릭 -----

    # 제네릭 함수 identity — i32
    """
fn identity<T>(x: T) -> T {
    return x
}
print(identity(42))
""",

    # 제네릭 함수 identity — f64
    """
fn identity<T>(x: T) -> T {
    return x
}
print(identity(3.14))
""",

    # 제네릭 함수 max — i32
    """
fn max<T>(a: T, b: T) -> T {
    if a > b { return a }
    return b
}
print(max(3, 7))
print(max(10, 2))
""",

    # 제네릭 함수 max — f64
    """
fn max<T>(a: T, b: T) -> T {
    if a > b { return a }
    return b
}
print(max(1.5, 2.5))
print(max(10.0, 3.0))
""",

    # 같은 제네릭 함수를 i32, f64로 각각 호출 (단형화 두 벌)
    """
fn double<T>(x: T) -> T {
    return x + x
}
print(double(5))
print(double(3.14))
""",

    # 제네릭 함수 clamp
    """
fn clamp<T>(val: T, low: T, high: T) -> T {
    if val < low { return low }
    if val > high { return high }
    return val
}
print(clamp(5, 0, 10))
print(clamp(-3, 0, 10))
print(clamp(15, 0, 10))
""",

    # 제네릭 enum Option<T> + match
    """
enum Option<T> {
    Some(T)
    None
}

x = Option.Some(42)
match x {
    Some(v) => { print(v) }
    None => { print(-1) }
}
""",

    # 제네릭 enum Result<T, E>
    """
enum Result<T, E> {
    Ok(T)
    Err(E)
}

x = Result.Ok(100)
match x {
    Ok(v) => { print(v) }
    Err(e) => { print(e) }
}
""",

    # ----- 16단계: 타입 캐스팅 (as) -----

    # f64 → i32
    """
print(3.14 as i32)
print(-3.7 as i32)
""",

    # i32 → f64
    """
print(42 as f64)
""",

    # i32 → str
    """
print(65 as str)
""",

    # bool → i32
    """
print(true as i32)
print(false as i32)
""",

    # 산술과 조합
    """
x = 10.7
y = x as i32
print(y + 5)
""",

    # 괄호 식 캐스팅
    """
z = (3 + 4) as f64
print(z)
""",

    # ----- 15단계: 람다 (타입 어노테이션 있는 경우) -----

    # 변수에 람다 저장 + 호출
    """
add = fn(a: i32, b: i32) -> i32 { return a + b }
print(add(1, 2))
""",

    # 람다에서 f64 연산
    """
mul = fn(a: f64, b: f64) -> f64 { return a * b }
print(mul(3.0, 4.0))
""",

    # 람다에서 조건분기
    """
max = fn(a: i32, b: i32) -> i32 {
    if a > b { return a }
    return b
}
print(max(3, 7))
print(max(10, 2))
""",

    # ===== 20: comptime =====
    
    # 기본 comptime 상수
    """
const X = comptime { 42 }
print(X)
""",
    
    # comptime 산술
    """
const Y = comptime { 10 + 20 + 12 }
print(Y)
""",
    
    # comptime 체이닝
    """
const A = comptime { 10 }
const B = comptime { A + 20 }
print(B)
""",
    
    # comptime for 루프
    """
const SUM = comptime {
    total = 0
    for i in 0..10 {
        total = total + i
    }
    total
}
print(SUM)
""",
    
    # comptime while (팩토리얼)
    """
const FACT = comptime {
    n = 10
    result = 1
    i = 1
    while i <= n {
        result = result * i
        i = i + 1
    }
    result
}
print(FACT)
""",
    
    # comptime 함수 호출
    """
fn square(x: i32) -> i32 {
    return x * x
}
const SQ = comptime { square(7) }
print(SQ)
""",

    # comptime bool
    """
const FLAG = comptime { true }
print(FLAG)
""",

    # comptime 문자열
    """
const MSG = comptime { "hello" }
print(MSG)
""",

    # comptime에서 if 분기
    """
const VAL = comptime {
    x = 10
    if x > 5 {
        100
    } else {
        200
    }
}
print(VAL)
""",

    # 일반 변수에 comptime 사용
    """
x = comptime { 10 * 10 }
print(x)
""",

    # comptime에서 변수
    """
const RES = comptime {
    x = 100
    y = 200
    x + y
}
print(RES)
""",

    # ===== 21: unsafe =====
    
    # unsafe 블록 기본
    """
x = 10
unsafe {
    x = x + 5
}
print(x)
""",
    
    # unsafe 식 위치
    """
x = unsafe { 42 }
print(x)
""",
    
    # unsafe fn
    """
unsafe fn dangerous(x: i32) -> i32 {
    return x * 2
}
result = unsafe { dangerous(21) }
print(result)
""",
    
    # unsafe 블록에서 산술
    """
x = unsafe {
    a = 10
    b = 20
    a + b
}
print(x)
""",
    
    # unsafe fn 인자 전달
    """
unsafe fn add_raw(a: i32, b: i32) -> i32 {
    return a + b
}
r = unsafe { add_raw(100, 200) }
print(r)
""",
    
    # unsafe 블록에서 일반 함수 호출
    """
fn safe_add(a: i32, b: i32) -> i32 {
    return a + b
}
x = unsafe { safe_add(3, 7) }
print(x)
""",

    # ===== 22: 매크로 =====
    
    # 단순 매크로
    """
macro double!(x) {
    x * 2
}
print(double!(21))
""",
    
    # 복수 파라미터
    """
macro add!(a, b) {
    a + b
}
print(add!(10, 20))
""",
    
    # 매크로에서 print
    """
macro say!(msg) {
    print(msg)
}
say!(42)
""",
    
    # 매크로 결과 대입
    """
macro square!(x) {
    x * x
}
result = square!(7)
print(result)
""",

    # === 23a: tagged enum 함수 매개변수/반환 ===
    """
enum Shape {
    Circle(f64)
    Rect(f64, f64)
}
fn area(s: Shape) -> f64 {
    match s {
        Circle(r) => { return r * r * 3.14 }
        Rect(w, h) => { return w * h }
    }
    return 0.0
}
print(area(Shape.Circle(10.0)))
print(area(Shape.Rect(3.0, 4.0)))
""",

    """
enum Result {
    Ok(i32)
    Err(str)
}
fn safe_div(a: i32, b: i32) -> Result {
    if b == 0 { return Result.Err("div0") }
    return Result.Ok(a / b)
}
fn show(r: Result) {
    match r {
        Ok(v) => { print(v) }
        Err(e) => { print(e) }
    }
}
show(safe_div(10, 2))
show(safe_div(10, 0))
""",

    # === 23c: ? 연산자 ===
    """
enum Result {
    Ok(i32)
    Err(str)
}
fn safe_div(a: i32, b: i32) -> Result {
    if b == 0 { return Result.Err("div0") }
    return Result.Ok(a / b)
}
fn compute() -> Result {
    val = safe_div(10, 2)?
    return Result.Ok(val + 1)
}
match compute() {
    Ok(v) => { print(v) }
    Err(e) => { print(e) }
}
""",

    """
enum Result {
    Ok(i32)
    Err(str)
}
fn safe_div(a: i32, b: i32) -> Result {
    if b == 0 { return Result.Err("div0") }
    return Result.Ok(a / b)
}
fn compute() -> Result {
    val = safe_div(10, 0)?
    return Result.Ok(val + 1)
}
match compute() {
    Ok(v) => { print(v) }
    Err(e) => { print(e) }
}
""",

    # ? 연쇄
    """
enum Result {
    Ok(i32)
    Err(str)
}
fn safe_div(a: i32, b: i32) -> Result {
    if b == 0 { return Result.Err("div0") }
    return Result.Ok(a / b)
}
fn chain() -> Result {
    a = safe_div(100, 5)?
    b = safe_div(a, 4)?
    return Result.Ok(b)
}
match chain() {
    Ok(v) => { print(v) }
    Err(e) => { print(e) }
}
""",

    # ? 연쇄 중간 실패
    """
enum Result {
    Ok(i32)
    Err(str)
}
fn safe_div(a: i32, b: i32) -> Result {
    if b == 0 { return Result.Err("div0") }
    return Result.Ok(a / b)
}
fn chain() -> Result {
    a = safe_div(100, 5)?
    b = safe_div(a, 0)?
    return Result.Ok(b)
}
match chain() {
    Ok(v) => { print(v) }
    Err(e) => { print(e) }
}
""",

    # === 24b: Arena 컴파일러 ===
    """
a = Arena.new(1024)
print(Arena.capacity(a))
print(Arena.used(a))
Arena.reset(a)
print(Arena.used(a))
Arena.destroy(a)
print("done")
""",

    """
frame = Arena.new(512)
level = Arena.new(2048)
print(frame.capacity())
print(level.capacity())
frame.reset()
print(frame.used())
level.destroy()
frame.destroy()
print("ok")
""",

    """
a = Arena.new(1024)
print(a.used())
a.reset()
print(a.capacity())
a.destroy()
print("destroyed")
""",

    # === 25b: Arena.alloc 컴파일러 ===
    """
a = Arena.new(1024)
p1 = a.alloc(64)
p2 = a.alloc(128)
print(p1)
print(p2)
print(a.used())
a.destroy()
""",

    """
a = Arena.new(256)
p = Arena.alloc(a, 32)
print(p)
print(a.used())
a.alloc(100)
print(a.used())
a.reset()
print(a.used())
a.destroy()
""",

    # 커스텀 얼로케이터 — trait + struct + impl
    """
trait Allocator {
    fn alloc(self, size: i32) -> i32 {
        return -1
    }
    fn reset(self) {
        return 0
    }
}

struct BumpAllocator {
    capacity: i32
    offset: i32
}

impl Allocator for BumpAllocator {
    fn alloc(self, size: i32) -> i32 {
        pos = self.offset
        self.offset = self.offset + size
        return pos
    }
    fn reset(self) {
        self.offset = 0
        return 0
    }
}

alloc = BumpAllocator { capacity: 1024, offset: 0 }
p1 = alloc.alloc(64)
p2 = alloc.alloc(128)
print(p1)
print(p2)
print(alloc.offset)
alloc.reset()
print(alloc.offset)
""",

    # ===== 28단계: ECS 쿼리 확장 (Optional + Exclude) =====

    # Optional 컴포넌트 — Velocity 없어도 처리
    """
component Position { x: f64, y: f64 }
component Velocity { vx: f64, vy: f64 }

e1 = spawn()
add(e1, Position { x: 1.0, y: 0.0 })
add(e1, Velocity { vx: 2.0, vy: 0.0 })

e2 = spawn()
add(e2, Position { x: 5.0, y: 0.0 })

system move(dt: f64) {
    for each (p: Position, ?v: Velocity) {
        if v == null {
            print(0)
        } else {
            print(1)
        }
    }
}
move(1.0)
""",

    # Exclude 필터 — Dead 있는 엔티티 제외
    """
component Position { x: f64, y: f64 }
component Dead { code: i32 }

e1 = spawn()
add(e1, Position { x: 1.0, y: 0.0 })

e2 = spawn()
add(e2, Position { x: 2.0, y: 0.0 })
add(e2, Dead { code: 0 })

e3 = spawn()
add(e3, Position { x: 3.0, y: 0.0 })

system alive() {
    for each (p: Position, !Dead) {
        print(p.x)
    }
}
alive()
""",

    # Optional + Exclude 조합
    """
component Position { x: f64, y: f64 }
component Velocity { vx: f64, vy: f64 }
component Frozen { duration: f64 }

e1 = spawn()
add(e1, Position { x: 10.0, y: 0.0 })
add(e1, Velocity { vx: 1.0, vy: 0.0 })

e2 = spawn()
add(e2, Position { x: 20.0, y: 0.0 })
add(e2, Velocity { vx: 1.0, vy: 0.0 })
add(e2, Frozen { duration: 3.0 })

e3 = spawn()
add(e3, Position { x: 30.0, y: 0.0 })

system update(dt: f64) {
    for each (p: Position, ?v: Velocity, !Frozen) {
        if v == null {
            print(0)
        } else {
            p.x = p.x + v.vx * dt
            print(p.x)
        }
    }
}
update(1.0)
""",

    # Exclude 여러 조건
    """
component Unit { hp: f64 }
component Dead { code: i32 }
component Frozen { duration: f64 }

e1 = spawn()
add(e1, Unit { hp: 100.0 })

e2 = spawn()
add(e2, Unit { hp: 50.0 })
add(e2, Dead { code: 0 })

e3 = spawn()
add(e3, Unit { hp: 80.0 })
add(e3, Frozen { duration: 1.0 })

e4 = spawn()
add(e4, Unit { hp: 60.0 })

system active() {
    for each (u: Unit, !Dead, !Frozen) {
        print(u.hp)
    }
}
active()
""",

    # ===== 29: dyn 동적 디스패치 =====
    
    # 기본 동적 디스패치
    """
struct Circle { r: f64 }
struct Rect { w: f64, h: f64 }
trait Drawable {
    fn draw(self) { print(0) }
}
impl Drawable for Circle {
    fn draw(self) { print(self.r) }
}
impl Drawable for Rect {
    fn draw(self) { print(self.w) }
}
c = Circle { r: 5.0 }
r = Rect { w: 10.0, h: 20.0 }
dc = c as dyn Drawable
dr = r as dyn Drawable
dc.draw()
dr.draw()
""",

    # 기본 구현 사용 (메서드 미구현)
    """
struct Empty {}
trait Greeter {
    fn greet(self) { print(42) }
}
impl Greeter for Empty {}
e = Empty {} as dyn Greeter
e.greet()
""",

    # 필드 접근
    """
struct Player { hp: f64, atk: f64 }
trait Unit {
    fn info(self) { print(self.hp) }
}
impl Unit for Player {
    fn info(self) {
        print(self.hp)
        print(self.atk)
    }
}
p = Player { hp: 100.0, atk: 25.0 } as dyn Unit
p.info()
""",

    # 반환값 있는 메서드
    """
struct Square { s: f64 }
trait Shape {
    fn area(self) -> f64 { return 0.0 }
}
impl Shape for Square {
    fn area(self) -> f64 { return self.s * self.s }
}
sq = Square { s: 7.0 } as dyn Shape
print(sq.area())
""",

    # 일부만 구현 — 나머지는 기본 구현
    """
struct Foo {}
trait Bar {
    fn do_it(self) { print(1) }
    fn also(self) { print(2) }
}
impl Bar for Foo {
    fn do_it(self) { print(3) }
}
f = Foo {} as dyn Bar
f.do_it()
f.also()
""",

    # dyn 값의 메서드 반환값 사용
    """
struct A { x: f64 }
trait T {
    fn val(self) -> f64 { return self.x }
}
impl T for A {
    fn val(self) -> f64 { return self.x }
}
a = A { x: 42.0 } as dyn T
print(a.val())
""",

    # dyn을 함수 매개변수로 넘기기 (타입 어노테이션)
    """
struct Cat {}
struct Dog {}
trait Animal {
    fn speak(self) { print(0) }
}
impl Animal for Cat {
    fn speak(self) { print(1) }
}
impl Animal for Dog {
    fn speak(self) { print(2) }
}
fn make_sound(a: dyn Animal) {
    a.speak()
}
make_sound(Cat {} as dyn Animal)
make_sound(Dog {} as dyn Animal)
""",

    # dyn == null 비교
    """
struct B { v: f64 }
trait V {
    fn get(self) -> f64 { return self.v }
}
impl V for B {
    fn get(self) -> f64 { return self.v }
}
b = B { v: 7.0 } as dyn V
print(b == null)
print(b.get())
""",

    # ===== 30: HashMap =====
    
    # HashMap 기본 set/get
    """
m = HashMap.new()
m.set("a", 10)
m.set("b", 20)
print(m.get("a"))
print(m.get("b"))
""",

    # HashMap has
    """
m = HashMap.new()
m.set("x", 1)
print(m.has("x"))
print(m.has("y"))
""",

    # HashMap remove
    """
m = HashMap.new()
m.set("a", 1)
m.set("b", 2)
print(m.remove("a"))
print(m.has("a"))
print(m.remove("z"))
""",

    # HashMap len
    """
m = HashMap.new()
print(m.len())
m.set("a", 1)
m.set("b", 2)
print(m.len())
m.remove("a")
print(m.len())
""",

    # HashMap 값 덮어쓰기
    """
m = HashMap.new()
m.set("a", 10)
m.set("a", 99)
print(m.get("a"))
print(m.len())
""",

    # === 34단계: 클로저 캡처 ===
    
    # filter에서 외부 변수 캡처
    """
threshold: i32 = 10
nums = [1, 5, 15, 20, 3, 25]
big = nums.filter(fn(x: i32) -> bool { return x > threshold })
print(big.len())
""",

    # map에서 외부 변수 캡처
    """
offset: i32 = 100
nums = [1, 2, 3]
result = nums.map(fn(x: i32) -> i32 { return x + offset })
print(result[0])
print(result[1])
print(result[2])
""",

    # 여러 외부 변수 캡처
    """
lo: i32 = 5
hi: i32 = 20
nums = [1, 3, 7, 15, 25, 30]
mid = nums.filter(fn(x: i32) -> bool { return x > lo and x < hi })
print(mid[0])
print(mid[1])
""",

    # reduce에서 클로저
    """
bonus: i32 = 10
nums = [1, 2, 3]
total = nums.reduce(0, fn(acc: i32, x: i32) -> i32 { return acc + x + bonus })
print(total)
""",

    # count에서 클로저
    """
min_val: i32 = 10
nums = [5, 10, 15, 20, 3]
c = nums.count(fn(x: i32) -> bool { return x >= min_val })
print(c)
""",

    # any에서 클로저
    """
target: i32 = 15
nums = [1, 2, 3, 15, 20]
found = nums.any(fn(x: i32) -> bool { return x == target })
if found { print(1) } else { print(0) }
""",

    # all에서 클로저
    """
min_val: i32 = 0
nums = [1, 2, 3]
ok = nums.all(fn(x: i32) -> bool { return x > min_val })
if ok { print(1) } else { print(0) }
""",

    # 캡처 없는 기존 Lambda가 여전히 동작하는지 확인
    """
nums = [3, 1, 2]
doubled = nums.map(fn(x: i32) -> i32 { return x * 2 })
print(doubled[0])
print(doubled[1])
print(doubled[2])
""",

    # === 35단계: @attribute 시스템 ===

    # 기본 attribute 조회
    """
@serialize
struct Player {
    hp: i32
}
if has_attribute("Player", "serialize") { print(1) } else { print(0) }
if has_attribute("Player", "networked") { print(1) } else { print(0) }
""",

    # 다중 attribute
    """
@serialize
@networked
struct GameState {
    score: i32
}
if has_attribute("GameState", "serialize") { print(1) } else { print(0) }
if has_attribute("GameState", "networked") { print(1) } else { print(0) }
if has_attribute("GameState", "replicated") { print(1) } else { print(0) }
""",

    # 함수에 attribute
    """
@inline
fn fast(a: i32) -> i32 {
    return a * 2
}
print(fast(5))
if has_attribute("fast", "inline") { print(1) } else { print(0) }
""",

    # attribute 없는 대상
    """
struct Plain {
    x: i32
}
if has_attribute("Plain", "serialize") { print(1) } else { print(0) }
""",

    # attribute가 있는 struct에서 정상 인스턴스 생성
    """
@serialize
struct Vec2D {
    x: i32
    y: i32
}
v = Vec2D { x: 10, y: 20 }
print(v.x + v.y)
""",

    # === 36a단계: Result 메서드 ===

    # is_ok / is_err (변수)
    """
enum Result {
    Ok(i32)
    Err(i32)
}
fn divide(a: i32, b: i32) -> Result {
    if b == 0 { return Result.Err(-1) }
    return Result.Ok(a / b)
}
r1 = divide(10, 2)
if r1.is_ok() { print(1) } else { print(0) }
r2 = divide(10, 0)
if r2.is_err() { print(1) } else { print(0) }
""",

    # unwrap_or (변수)
    """
enum Result {
    Ok(i32)
    Err(i32)
}
fn divide(a: i32, b: i32) -> Result {
    if b == 0 { return Result.Err(0) }
    return Result.Ok(a / b)
}
r1 = divide(10, 5)
print(r1.unwrap_or(-1))
r2 = divide(10, 0)
print(r2.unwrap_or(-1))
""",

    # 직접 체이닝: divide(10, 5).unwrap_or(-1)
    """
enum Result {
    Ok(i32)
    Err(i32)
}
fn divide(a: i32, b: i32) -> Result {
    if b == 0 { return Result.Err(0) }
    return Result.Ok(a / b)
}
print(divide(10, 5).unwrap_or(-99))
print(divide(10, 0).unwrap_or(-99))
""",

    # is_ok/is_err 직접 체이닝
    """
enum Result {
    Ok(i32)
    Err(i32)
}
fn divide(a: i32, b: i32) -> Result {
    if b == 0 { return Result.Err(0) }
    return Result.Ok(a / b)
}
if divide(10, 2).is_ok() { print(1) } else { print(0) }
if divide(10, 0).is_err() { print(1) } else { print(0) }
""",

    # === 41단계: 고정 크기 정수 + 비트 연산 ===

    # 16진수 리터럴
    "print(0xFF)",

    # 비트 OR
    "print(0xFF00 | 0x00FF)",

    # 비트 AND + 시프트 → 성분 추출
    """
a = 0xAABBCC
print((a >> 16) & 0xFF)
print((a >> 8) & 0xFF)
print(a & 0xFF)
""",

    # 비트 XOR
    "print(10 ^ 12)",

    # 왼쪽 시프트
    "print(1 << 10)",

    # 오른쪽 시프트
    "print(256 >> 4)",

    # 비트 NOT
    "print(~5)",

    # 픽셀 패킹 함수
    """
fn pack_color(r: i32, g: i32, b: i32) -> i32 {
    return (r << 16) | (g << 8) | b
}
print(pack_color(0xAA, 0xBB, 0xCC))
""",

    # as u8 잘라내기
    """
x: i32 = 300
y: i32 = x & 0xFF
print(y)
""",

    # 비트 연산 우선순위: & 결과를 == 으로 비교
    """
flags: i32 = 6
mask: i32 = 4
if flags & mask == 4 {
    print(1)
} else {
    print(0)
}
""",

    # === 42단계: @sizeof/@alignof + 함수 포인터 + defer ===

    # @sizeof 기본
    "print(@sizeof(u8))",
    "print(@sizeof(i32))",
    "print(@sizeof(f64))",

    # @alignof 기본
    "print(@alignof(u8))",
    "print(@alignof(u32))",

    # @sizeof 활용: 스트라이드 계산
    "print(@sizeof(i32) * 4)",

    # 함수 포인터: 변수에 대입 후 호출
    """
fn double(x: i32) -> i32 { return x * 2 }
f = double
print(f(7))
""",

    # 함수 포인터: 함수 인자로 전달
    """
fn triple(x: i32) -> i32 { return x * 3 }
fn apply(f: fn(i32) -> i32, v: i32) -> i32 { return f(v) }
print(apply(triple, 5))
""",

    # 함수 포인터: 교체 패턴
    """
fn add_one(x: i32) -> i32 { return x + 1 }
fn sub_one(x: i32) -> i32 { return x - 1 }
op = add_one
print(op(10))
op = sub_one
print(op(10))
""",

    # defer: 기본 실행 순서 (LIFO)
    """
fn run() {
    defer { print("C") }
    defer { print("B") }
    print("A")
}
run()
""",

    # defer: return 전에도 실행
    """
fn run() -> i32 {
    defer { print("cleanup") }
    print("work")
    return 42
}
x = run()
print(x)
""",

    # === 43단계: parallel system (컴파일러) ===
    """
component Num { val }

parallel system double_all() {
    for each (n: Num) {
        n.val = n.val * 2
    }
}

e1 = spawn()
e2 = spawn()
e3 = spawn()
add(e1, Num { val: 1 })
add(e2, Num { val: 2 })
add(e3, Num { val: 3 })

double_all()

n1 = get(e1, Num)
n2 = get(e2, Num)
n3 = get(e3, Num)
print(n1.val)
print(n2.val)
print(n3.val)
""",

    # === 46단계: @packed struct + union ===

    # @packed struct — 필드 접근
    """
@packed struct Header { a: i32, b: i32, c: i32 }
h = Header { a: 1, b: 2, c: 3 }
print(h.a)
print(h.b)
print(h.c)
""",

    # union — i32 필드 초기화 및 읽기
    """
union Data { x: i32, y: i32 }
d = Data { x: 7 }
print(d.x)
""",

    # union — 필드 쓰기
    """
union Reg { lo: i32, hi: i32 }
r = Reg { lo: 0 }
r.lo = 255
print(r.lo)
""",

    # union — f64 필드
    """
union Num { i: i32, f: f64 }
n = Num { f: 1.5 }
print(n.f)
""",

    # union — i32 필드 → 표현식에서 읽기
    """
union Tag { code: i32, flags: i32 }
t = Tag { code: 42 }
x = t.code + 1
print(x)
""",

    # === 45단계: 수학 내장 함수 + export fn ===

    # @sqrt
    "print(@sqrt(4.0))",

    # @abs 정수
    "print(@abs(-7))",

    # @floor / @ceil
    "print(@floor(2.9))",
    "print(@ceil(2.1))",

    # @pow
    "print(@pow(2.0, 10.0))",

    # @min / @max 정수
    "print(@min(3, 7))",
    "print(@max(3, 7))",

    # @sin + @cos 항등식
    """
s = @sin(0.0)
c = @cos(0.0)
print(s * s + c * c)
""",

    # export fn — 일반 함수처럼 호출 가능
    """
export fn add(a: i32, b: i32) -> i32 { return a + b }
print(add(3, 4))
""",

    # === 47~54단계: Phase B ===

    # 48단계: test 블록 — 컴파일러에서 무시 (출력 없음, 일반 코드 정상 실행)
    """
test "덧셈" {
    assert(1 + 1 == 2)
}
print(42)
""",

    # 49단계: /// doc comment — 선언 전 주석, 실행에 영향 없음
    """
/// 두 수를 더한다
fn add(a: i32, b: i32) -> i32 { return a + b }
print(add(10, 20))
""",

    # 49단계: /// doc comment + struct
    """
/// 2D 점
struct Point { x: i32, y: i32 }
p = Point { x: 3, y: 4 }
print(p.x)
print(p.y)
""",

    # test 블록 여러 개 — 컴파일러는 모두 무시, 일반 코드만 실행
    """
test "a" { assert(true) }
test "b" { assert(false) }
x = 7
print(x)
""",

    # === 55~58단계: Phase C ===

    # 55단계: @clink("danha_gl") extern fn 선언 — 컴파일러에서 무시 없이 파싱/등록
    """
@clink("danha_gl")
extern fn gl_open(width: i32, height: i32, title: ptr) -> i32
extern fn gl_close()
print(99)
""",

    # 56단계: @vert / @frag — Attributed 노드로 파싱, 컴파일러는 내부 fn으로 처리
    """
@vert
fn vertex_main(x: i32, y: i32) -> i32 {
    return x + y
}
print(vertex_main(3, 4))
""",

    # 57단계: --target ios/android는 CLI 수준 처리, 언어 레벨은 일반 코드와 동일
    """
fn mobile_entry() -> i32 { return 42 }
print(mobile_entry())
""",

    # 58단계: 씬 에디터는 인터랙티브 GUI — 언어 레벨 테스트는 일반 코드 실행으로 검증
    """
scene_name = "MainScene"
print(scene_name)
""",

]


def normalize_floats(text):
    """인터프리터의 '10.0'과 컴파일러의 '10'은 같은 값이라 같은 것으로 취급.
    파이썬 str(10.0)='10.0' vs C printf %g=10 의 표시 차이일 뿐.
    줄 단위로 보고, 각 줄이 '<정수>.0'이면 '.0'을 떼서 비교.
    7.7: 벡터 출력 안의 값도 정규화 — vec3(3.0, 6.0, 9.0) vs vec3(3, 6, 9)."""
    import re
    out = []
    for line in text.split('\n'):
        # 벡터 출력인지 확인: vec2(...) / vec3(...) / vec4(...)
        # 인터프리터의 '3.0'을 컴파일러의 '3'과 맞추기 위해 '.0' 제거
        if re.match(r'^vec[234]\(', line):
            # 숫자.0을 숫자로 변환 (예: 3.0 → 3, -1.0 → -1)
            line = re.sub(r'(?<![.\d])(-?\d+)\.0(?!\d)', r'\1', line)
            out.append(line)
        # 7.9a: 행렬 출력 행 (| 1.0 0.0 ... |)
        elif re.match(r'^\|', line):
            line = re.sub(r'(?<![.\d])(-?\d+)\.0(?!\d)', r'\1', line)
            out.append(line)
        # 7.12d3: component/struct 출력 — "Name { f: v, ... }"
        # 인터프리터는 필드 값에 '.0'을 붙이고, 컴파일러 %g는 안 붙임. 떼서 비교.
        elif '{' in line and '}' in line and ':' in line:
            line = re.sub(r'(?<![.\d])(-?\d+)\.0(?!\d)', r'\1', line)
            out.append(line)
        # 13: tagged enum 출력 — "Shape.Circle(5.0)" 패턴
        # EnumName.Variant(...) 안의 숫자.0을 정규화
        elif re.match(r'^[A-Z]\w*\.\w+\(', line):
            line = re.sub(r'(?<![.\d])(-?\d+)\.0(?!\d)', r'\1', line)
            out.append(line)
        elif line.endswith('.0') and line[:-2].lstrip('-').isdigit():
            out.append(line[:-2])
        else:
            out.append(line)
    return '\n'.join(out)


def _run_compile_tests():
    import platform
    _opt = 0 if platform.system() == "Windows" else 2
    passed = 0
    failed = 0
    
    print("=== 인터프리터와 동일 결과 비교 ===")
    _win_jit_skip = frozenset()
    skipped = 0
    for i, src in enumerate(CASES):
        if i in _win_jit_skip:
            skipped += 1
            first_line = src.strip().split(chr(10))[0]
            print(f"SKIP {first_line!r:<48} → Windows JIT 알려진 이슈")
            continue
        try:
            expected = capture_interp(src)
            actual = jit_print_output(src, _opt)
            
            if normalize_floats(expected) == normalize_floats(actual):
                passed += 1
                first_line = src.strip().split(chr(10))[0]
                print(f"OK   {first_line!r:<48} → {expected.strip()[:30]}")
            else:
                failed += 1
                print(f"FAIL {src!r}")
                print(f"     interp: {expected!r}")
                print(f"     native: {actual!r}")
        except Exception as e:
            failed += 1
            first_line = src.strip().split(chr(10))[0]
            print(f"FAIL {first_line!r:<48} → 예외: {e}")
    
    print()
    print(f"{passed} 통과, {failed} 실패, {skipped} 건너뜀 / 총 {len(CASES)}")
    
    # ===== 7.1.3 부정 케이스 =====
    # 컴파일러가 의도대로 거부하는지 확인. 인터프리터는 아직 의미가 느슨해 여긴 컴파일러만.
    # 각 케이스: (설명, 코드, 에러 메시지에 들어 있어야 할 문구)
    NEGATIVE_CASES = [
        # 검사 A: 본문 쓰기 검사
        ("&P 매개변수의 필드에 쓰기", """
struct P { hp: i32 }
fn f(p: &P) { p.hp = 0 }
pl = P { hp: 1 }
f(&pl)
        """, "읽기 참조"),
    
        ("&P 매개변수를 재대입", """
struct P { hp: i32 }
fn f(p: &P, other: P) { p = other }
pl = P { hp: 1 }
q = P { hp: 2 }
f(&pl, q)
        """, "읽기 참조"),
    
        # 검사 B: 호출자-시그니처 매칭
        ("값 시그니처에 & 넘김", """
struct P { hp: i32 }
fn f(p: P) { print(p.hp) }
pl = P { hp: 1 }
f(&pl)
        """, "값으로 전달"),
    
        ("&mut 시그니처에 값 넘김", """
struct P { hp: i32 }
fn f(p: &mut P) { p.hp = 99 }
pl = P { hp: 1 }
f(pl)
        """, "'&mut'"),
    
        ("&mut 시그니처에 &만 넘김 (권한 부족)", """
struct P { hp: i32 }
fn f(p: &mut P) { p.hp = 99 }
pl = P { hp: 1 }
f(&pl)
        """, "권한 부족"),
    
        # 메서드
        ("메서드의 &P 인자에 쓰기", """
struct P { hp: i32 }
impl P { fn touch(self, other: &P) { other.hp = 0 } }
a = P { hp: 1 }
b = P { hp: 2 }
a.touch(&b)
        """, "읽기 참조"),
    
        ("메서드의 &mut P 인자에 값 넘김", """
struct P { hp: i32 }
impl P { fn set(self, other: &mut P) { other.hp = 99 } }
a = P { hp: 1 }
b = P { hp: 2 }
a.set(b)
        """, "'&mut'"),
    
        # 파서 수준 거부 (7.1 결정)
        ("반환 타입에 참조", """
fn f() -> &i32 { return 5 }
        """, "반환 타입"),
    
        ("구조체 필드에 참조", """
struct P { hp: &i32 }
        """, "구조체 필드"),
    
        # 문제 4 해결: schedule writer/writer 충돌 거부
        # (양방향 read/write 교차는 이제 허용 — 상호 참조 패턴)
        ("schedule writer 충돌", """
component Position { x, y }

system move_a(dt: f64) {
    for each (p: Position) {
        p.x = p.x + dt
    }
}

system move_b(dt: f64) {
    for each (p: Position) {
        p.x = p.x - dt
    }
}

e = spawn()
add(e, Position { x: 0.0, y: 0.0 })

schedule(1.0)
        """, "같은 컴포넌트"),
    
        # 7.15e: 시그니처로 &mut 명시한 writer/writer 충돌도 거부
        ("7.15e &mut writer 충돌", """
component Position { x, y }

system a(dt: f64) {
    for each (p: &mut Position) {
        p.x = p.x + dt
    }
}

system b(dt: f64) {
    for each (p: &mut Position) {
        p.x = p.x - dt
    }
}

e = spawn()
add(e, Position { x: 0.0, y: 0.0 })
schedule(1.0)
        """, "같은 컴포넌트"),
    
        # 7.15f: 부동값을 정수 필드에 넣으면 거부
        ("7.15f 부동값을 정수 필드에", """
component S { n: i32 }
e = spawn()
add(e, S { n: 3.14 })
        """, "부동"),
    
        # 7.15f: 지원 안 하는 필드 타입
        ("7.15f 알 수 없는 필드 타입", """
component X { v: weird_type }
        """, "지원"),
    
        # 7.15a: const 재대입 거부
        ("const 재대입", "const X = 5\nX = 10", "const"),
    
        # 7.15b: enum 없는 variant 거부
        ("enum 없는 variant", """
enum Phase { A, B }
print(Phase.C)
        """, "variant"),
    
        # 7.15d: break/continue 루프 바깥 거부
        ("루프 바깥 break", "break", "루프"),
        ("루프 바깥 continue", "continue", "루프"),
    
        # 7.16a: extern fn 거부 케이스
        ("extern 뒤에 fn 없음", "extern x = 10", "fn"),
        ("extern fn 이름 중복", """
fn abs(x: i32) -> i32 { return x }
extern fn abs(x: i32) -> i32
print(1)
        """, "중복"),
    ]
    
    neg_passed = 0
    neg_failed = 0
    print()
    print("=== 7.1.3 거부 케이스 ===")
    for label, src, must_contain in NEGATIVE_CASES:
        try:
            run_native(src, opt_level=_opt)
            neg_failed += 1
            print(f"FAIL {label} — 거부돼야 했는데 통과")
        except Exception as e:
            if must_contain in str(e):
                neg_passed += 1
                print(f"OK   {label}")
            else:
                neg_failed += 1
                print(f"FAIL {label} — 에러 메시지 기대와 다름: {e}")
    
    print()
    print(f"거부 케이스: {neg_passed} 통과, {neg_failed} 실패 / 총 {len(NEGATIVE_CASES)}")
    
    total_failed = failed + neg_failed
    
    # ===== 7.16a: extern fn 호출 (컴파일러 전용) =====
    # 인터프리터에서는 extern fn을 호출할 수 없으므로, 컴파일러만으로 검증.
    EXTERN_CASES = [
        # C 표준 라이브러리의 abs 호출
        ("extern abs(-7)", """
extern fn abs(x: i32) -> i32
print(abs(-7))
print(abs(3))
        """, "7\n3"),
    
        # 여러 extern fn 선언 + 호출
        ("extern abs(-42)", """
extern fn abs(x: i32) -> i32
result = abs(-42)
print(result)
        """, "42"),
    
        # void 반환 extern fn 호출 (7.16b)
        # srand는 libc의 void 함수 — 별도 라이브러리 로드 불필요
        ("extern void fn 호출", """
extern fn srand(seed: i32)
extern fn abs(x: i32) -> i32
srand(42)
print(abs(-3))
        """, "3"),
    ]
    
    ext_passed = 0
    ext_failed = 0
    print()
    print("=== 7.16a extern fn 호출 (컴파일러 전용) ===")
    for label, src, expected in EXTERN_CASES:
        actual = jit_print_output(src, _opt).strip()
        if actual == expected:
            ext_passed += 1
            print(f"OK   {label}")
        else:
            ext_failed += 1
            print(f"FAIL {label}")
            print(f"     expected: {expected!r}")
            print(f"     actual:   {actual!r}")
    
    print()
    print(f"extern 호출: {ext_passed} 통과, {ext_failed} 실패 / 총 {len(EXTERN_CASES)}")
    
    total_failed = total_failed + ext_failed
    sys.exit(0 if total_failed == 0 else 1)

if __name__ == "__main__":
    _run_compile_tests()
