# danha_evaluator.py
# Danha 언어의 평가기

import sys
import os as _os_cpu
import threading as _threading
from lexer import lex
from danha_parser import parse
from danha_errors import (
    DanhaError, DanhaSyntaxError, DanhaTypeError, DanhaNameError,
    DanhaValueError, DanhaImportError, DanhaECSError, DanhaRuntimeError,
)

# Danha는 트리 워킹 인터프리터라서 호스트(파이썬)의 재귀 한도에 영향을 받는다.
# Danha 함수 호출 하나당 파이썬 스택이 10~15 프레임씩 쌓이므로,
# 파이썬 기본값(1000)으로는 Danha 재귀가 100번도 못 가서 터진다.
# 6단계 네이티브 컴파일로 가면 이 제한은 사라진다.
sys.setrecursionlimit(10000)


class ReturnValue(Exception):
    def __init__(self, value):
        self.value = value


# 7.15d: 루프 제어 흐름.
# ReturnValue와 같은 패턴 — 파이썬 예외로 본체 실행을 튕겨나와
# 바깥 루프 헤더에서 잡는다.
class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


# 12단계: 에러 메시지에 소스 코드 줄을 포함하기 위한 글로벌 변수.
# run() 호출 시 설정됨.
_CURRENT_SOURCE = None

# 21a: unsafe 블록 깊이. 0이면 안전한 코드, 1 이상이면 unsafe 컨텍스트.
# 리스트로 감싸서 중첩 함수에서 수정 가능하게.
_UNSAFE_DEPTH = [0]

# 셀프 호스팅 59단계: 스크립트 인자 목록.
# danha selfhost 명령 시 채워짐. get_args() 빌트인으로 접근.
_SCRIPT_ARGS = []

# 48단계: 테스트 모드 — danha test 명령어가 활성화할 때 True
_TEST_MODE = False
_TEST_RESULTS = []  # [{'name': str, 'passed': bool, 'error': str|None}]

# 51단계: 프로파일링 모드
_PROFILING = False
_PROFILE_STATS = {}  # {fn_name: {'calls': 0, 'total': 0.0}}

# 52단계: 소켓 레지스트리 {fd: socket_obj}
_SOCKETS = {}

# 53단계: 디버거 상태
_DEBUG_MODE = False
_DEBUG_BREAKPOINTS = set()  # {line_number}
_DEBUG_STEP = [False]       # 단일 스텝 모드
_DEBUG_SCOPE_REF = [None]   # 현재 스코프 참조 (디버거 명령에서 접근)


class Scope:
    """
    한 스코프(양파 껍질 하나).
    자기 변수들을 담고, 바깥 스코프(parent)를 가리킨다.
    전역 스코프는 parent가 None이다.
    """
    
    def __init__(self, parent=None):
        self.vars = {}
        self.parent = parent
        self.consts = set()  # 7.15a: const로 선언된 이름들
    
    def get(self, name):
        """
        이름 찾기: 자기 스코프에 있으면 여기서, 없으면 부모한테.
        끝까지 없으면 KeyError.
        """
        if name in self.vars:
            return self.vars[name]
        if self.parent is not None:
            return self.parent.get(name)
        raise KeyError(name)
    
    def has(self, name):
        """체인 전체에 이 이름이 있는지만 확인 (값은 안 가져옴)"""
        if name in self.vars:
            return True
        if self.parent is not None:
            return self.parent.has(name)
        return False
    
    def is_const(self, name):
        """체인 전체에서 이 이름이 const인지 확인"""
        if name in self.consts:
            return True
        if self.parent is not None:
            return self.parent.is_const(name)
        return False
    
    def set_existing(self, name, value):
        """
        체인에서 이미 있는 변수를 찾아서 수정.
        성공하면 True, 어디에도 없으면 False.
        """
        if name in self.vars:
            if name in self.consts:
                raise DanhaValueError(f"'{name}'은(는) const라서 바꿀 수 없어", source=_CURRENT_SOURCE)
            self.vars[name] = value
            return True
        if self.parent is not None:
            return self.parent.set_existing(name, value)
        return False
    
    def declare(self, name, value):
        """이 스코프에 새 변수를 만듦 (같은 스코프의 기존 걸 덮어씀)"""
        self.vars[name] = value
    
    def declare_const(self, name, value):
        """7.15a: 이 스코프에 상수를 만듦. 재대입 시도 시 에러."""
        self.vars[name] = value
        self.consts.add(name)


def format_value(value, inside_struct=False):
    """
    Danha 값을 사람이 읽기 좋게 문자열로 만든다.
    inside_struct=True면 문자열을 따옴표로 감싼다 (구조체 디버그 출력용).
    """
    # bool 체크를 먼저 해야 한다. 파이썬에서 bool은 int의 서브클래스라
    # isinstance(True, int)가 True라서, bool을 int로 오인할 수 있다.
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        if inside_struct:
            return '"' + value + '"'
        return value
    if isinstance(value, list):
        parts = [format_value(el, inside_struct=True) for el in value]
        return '[' + ', '.join(parts) + ']'
    if isinstance(value, tuple) and len(value) >= 1:
        # 7.7: 벡터 출력 — vec3(1.0, 2.0, 3.0) 형태로 보여줌.
        if value[0] == 'VecValue':
            vec_type = value[1]  # 'vec2', 'vec3', 'vec4'
            components = value[2]  # [x, y] or [x, y, z] or [x, y, z, w]
            parts = []
            for c in components:
                # 정수처럼 보이는 실수는 .0 붙여서 출력 (1.0, 2.0 등)
                if isinstance(c, float) and c == int(c):
                    parts.append(f"{int(c)}.0")
                else:
                    parts.append(str(c))
            return f"{vec_type}({', '.join(parts)})"
        # 7.9a: 행렬 출력 — 4행으로 나눠서 보여줌.
        # | 1.0 0.0 0.0 0.0 |
        # | 0.0 1.0 0.0 0.0 |
        # | 0.0 0.0 1.0 0.0 |
        # | 0.0 0.0 0.0 1.0 |
        if value[0] == 'MatValue':
            data = value[2]

            def _fmt_comp(c):
                if isinstance(c, float) and c == int(c):
                    return f"{int(c)}.0"
                return str(c)

            rows = []
            for row in range(4):
                cells = []
                for col in range(4):
                    cells.append(_fmt_comp(data[col * 4 + row]))
                rows.append("| " + " ".join(cells) + " |")
            return "\n".join(rows)
        if value[0] == 'StructValue':
            type_name = value[1]
            fields = value[2]
            field_strs = []
            for field_name, field_value in fields.items():
                field_strs.append(f"{field_name}: {format_value(field_value, inside_struct=True)}")
            return f"{type_name} {{ {', '.join(field_strs)} }}"
        # 7.12c: ComponentValue도 같은 모양. 사용자 관점에서 "데이터 덩어리"는 똑같이 보임.
        if value[0] == 'ComponentValue':
            type_name = value[1]
            fields = value[2]
            field_strs = []
            for field_name, field_value in fields.items():
                field_strs.append(f"{field_name}: {format_value(field_value, inside_struct=True)}")
            return f"{type_name} {{ {', '.join(field_strs)} }}"
        if value[0] == 'Function':
            return "<function>"
        if value[0] == 'Builtin':
            return f"<builtin {value[2]}>"
        if value[0] == 'StructDef':
            return f"<struct definition>"
        if value[0] == 'UnionDef':
            return f"<union definition>"
        # 7.12b: EntityId 출력. 'Entity(index, gen)' 형태.
        # 사용자가 print(e)했을 때 어느 엔티티인지 한눈에.
        if value[0] == 'EntityId':
            return f"Entity({value[1]}, {value[2]})"
        # 8.3: tagged enum 값 출력
        if value[0] == 'TaggedEnumValue':
            enum_name = value[1]
            variant_name = value[2]
            data = value[4]
            if len(data) == 0:
                return f"{enum_name}.{variant_name}"
            parts = [format_value(d, inside_struct=True) for d in data]
            return f"{enum_name}.{variant_name}({', '.join(parts)})"
        # 24a: Arena 값 출력
        if value[0] == 'ArenaValue':
            info = value[1]
            status = "alive" if info['alive'] else "destroyed"
            return f"Arena({status}, used={info['used']}, capacity={info['capacity']})"
        if value[0] == 'ArenaType':
            return "<Arena>"
        # 30: HashMap 출력
        if value[0] == 'HashMapValue':
            data = value[1]
            if not data:
                return "HashMap {}"
            pairs = [f"{format_value(k)}: {format_value(v)}" for k, v in data.items()]
            return "HashMap {" + ", ".join(pairs) + "}"
        if value[0] == 'HashMapType':
            return "<HashMap>"
    return str(value)


def values_equal(a, b):
    """
    두 Danha 값의 의미 비교 (deep equality).
    - 구조체: 같은 타입이고 모든 필드가 의미적으로 같으면 같음
    - 리스트: 길이가 같고 원소가 모두 의미적으로 같으면 같음
    - 그 외: 파이썬의 == 그대로 (숫자, 불리언, 문자열)
    함수/내장/구조체정의는 비교 안 함 (정체성 비교로 떨어지지만 거의 안 씀)
    """
    # bool과 int 혼동 방지: True == 1이 파이썬에서 True지만,
    # Danha에서는 다른 타입이라 비교 자체를 막을지 정해야 함.
    # 일단은 파이썬 동작 그대로 둠 (나중에 타입 시스템 들어오면 결정).
    if isinstance(a, tuple) and isinstance(b, tuple):
        # 7.7: 벡터 비교
        if len(a) >= 1 and len(b) >= 1 and a[0] == 'VecValue' and b[0] == 'VecValue':
            if a[1] != b[1]:
                return False
            return all(x == y for x, y in zip(a[2], b[2]))
        if len(a) >= 1 and len(b) >= 1 and a[0] == 'StructValue' and b[0] == 'StructValue':
            if a[1] != b[1]:  # 다른 타입
                return False
            af, bf = a[2], b[2]
            if set(af.keys()) != set(bf.keys()):
                return False
            for k in af:
                if not values_equal(af[k], bf[k]):
                    return False
            return True
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        for x, y in zip(a, b):
            if not values_equal(x, y):
                return False
        return True
    return a == b


# ===== 7.7: 벡터 헬퍼 함수 =====
# 벡터는 ('VecValue', 'vec3', [x, y, z]) 형태로 표현.
# 리스트를 쓰는 이유: 필드 수정(pos.x = 5.0)이 가능하려면 가변(mutable) 자료구조가 필요.
# 딕셔너리보다 리스트가 빠르고, 인덱스로 접근하면 됨.

# 각 벡터 타입별로 필드 이름 → 인덱스 매핑
_VEC_FIELDS = {
    'vec2': {'x': 0, 'y': 1},
    'vec3': {'x': 0, 'y': 1, 'z': 2},
    'vec4': {'x': 0, 'y': 1, 'z': 2, 'w': 3},
    'vec2f': {'x': 0, 'y': 1},
    'vec3f': {'x': 0, 'y': 1, 'z': 2},
    'vec4f': {'x': 0, 'y': 1, 'z': 2, 'w': 3},
}

# 각 벡터 타입이 가져야 할 성분 수
_VEC_SIZES = {'vec2': 2, 'vec3': 3, 'vec4': 4, 'vec2f': 2, 'vec3f': 3, 'vec4f': 4}


def _vec_field_map(vec_type):
    """벡터 타입의 필드 이름→인덱스 맵을 돌려준다."""
    return _VEC_FIELDS[vec_type]


def _is_vec(value):
    """값이 벡터인지 확인."""
    return isinstance(value, tuple) and len(value) >= 1 and value[0] == 'VecValue'


def _is_number(value):
    """값이 숫자(int 또는 float)인지 확인. bool은 제외."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _vec_binop(left, right, op, line):
    """벡터 이항 연산 수행.
    
    지원하는 조합:
    - 벡터 op 벡터 (같은 타입): 성분별(component-wise) 연산
    - 벡터 op 스칼라: 모든 성분에 스칼라를 적용
    - 스칼라 op 벡터: Mul만 허용 (교환법칙: 2.0 * vec3 = vec3 * 2.0)
    
    결과 성분은 항상 float.
    """
    if _is_vec(left) and _is_vec(right):
        # 벡터 + 벡터: 같은 타입이어야 함
        if left[1] != right[1]:
            raise DanhaTypeError(
                f"{left[1]}과 {right[1]}은 서로 연산할 수 없어 (같은 벡터 타입이어야 해)",
                line=line, source=_CURRENT_SOURCE
            )
        lc, rc = left[2], right[2]
        result = [op(float(a), float(b)) for a, b in zip(lc, rc)]
        return ('VecValue', left[1], result)
    
    if _is_vec(left) and _is_number(right):
        # 벡터 op 스칼라
        s = float(right)
        result = [op(float(c), s) for c in left[2]]
        return ('VecValue', left[1], result)
    
    if _is_number(left) and _is_vec(right):
        # 스칼라 op 벡터
        s = float(left)
        result = [op(s, float(c)) for c in right[2]]
        return ('VecValue', right[1], result)
    
    return None  # 벡터 연산이 아님


# ===== 7.9a: 행렬 헬퍼 함수 =====
# mat4는 ('MatValue', 'mat4', [f64 x 16]) 형태로 표현.
# 16개 원소는 열 우선(column-major) 순서로 저장.
# 열 우선이란: 첫 4개가 1열, 다음 4개가 2열, ...
# OpenGL/Vulkan/대부분의 게임 엔진이 이 방식.
#
#   행렬을 수학적으로 쓰면:
#   | m[0] m[4] m[8]  m[12] |     col0  col1  col2  col3
#   | m[1] m[5] m[9]  m[13] |
#   | m[2] m[6] m[10] m[14] |
#   | m[3] m[7] m[11] m[15] |
#
# m[col*4 + row] 로 접근.

def _is_mat(value):
    """값이 행렬인지 확인."""
    return isinstance(value, tuple) and len(value) >= 1 and value[0] == 'MatValue'


def _mat4_get(m, row, col):
    """mat4에서 (row, col) 원소를 가져온다. 열 우선 순서."""
    return m[2][col * 4 + row]


def _mat4_mul_vec4(m, v):
    """mat4 * vec4 → vec4. 행렬 변환 적용.
    
    결과의 각 성분 = 행렬의 해당 행과 벡터의 내적.
    비유: 벡터를 새 좌표계로 옮기는 것.
    """
    result = []
    for row in range(4):
        s = 0.0
        for col in range(4):
            s += _mat4_get(m, row, col) * v[col]
        result.append(s)
    return result


def _mat4_mul_mat4(a, b):
    """mat4 * mat4 → mat4. 두 변환을 합치기.
    
    결과[col][row] = a의 row행과 b의 col열의 내적.
    """
    result = [0.0] * 16
    for col in range(4):
        for row in range(4):
            s = 0.0
            for k in range(4):
                s += _mat4_get(a, row, k) * _mat4_get(b, k, col)
            result[col * 4 + row] = s
    return result


def _call_value(func, arg_values, scope, line):
    """함수 값(Function 튜플)을 인자 리스트로 호출하는 헬퍼.
    배열 메서드(.map, .filter 등)에서 콜백을 실행할 때 사용."""
    if not isinstance(func, tuple) or func[0] != 'Function':
        raise DanhaTypeError("콜백이 함수가 아니야", line=line, source=_CURRENT_SOURCE)
    func_params = func[1]
    func_body = func[2]
    func_scope = func[3]
    if len(arg_values) != len(func_params):
        raise DanhaValueError(
            f"콜백 함수는 {len(func_params)}개의 인자가 필요한데 {len(arg_values)}개가 들어왔어",
            line=line, source=_CURRENT_SOURCE
        )
    call_scope = Scope(parent=func_scope)
    for pname, pval in zip(func_params, arg_values):
        call_scope.declare(pname, pval)
    try:
        evaluate(func_body, call_scope)
    except ReturnValue as ret:
        return ret.value
    return None


def evaluate(node, scope):
    node_type = node[0]

    # 53단계: 디버거 — 브레이크포인트 또는 단일 스텝 모드에서 멈춤
    if _DEBUG_MODE and node_type not in ('Program', 'Block', 'DocAnnotated'):
        _line = node[-1] if isinstance(node, tuple) and len(node) > 1 else None
        if _line is not None and (_DEBUG_STEP[0] or _line in _DEBUG_BREAKPOINTS):
            _debug_break(_line, node, scope)

    if node_type == 'Program':
        statements = node[1]
        result = None
        for stmt in statements:
            result = evaluate(stmt, scope)
        return result
    
    # 블록: 새 자식 스코프를 만들고 그 안에서 문장들을 평가한다.
    # 블록이 끝나면 자식 스코프는 버려진다 -> 블록 안에서 선언된 변수는
    # 블록 밖에서 안 보인다. (C/C++/Rust와 같은 동작)
    if node_type == 'Block':
        statements = node[1]
        block_scope = Scope(parent=scope)
        defer_list = []  # 42단계: LIFO defer 스택
        result = None
        saved_exc = None
        try:
            for stmt in statements:
                if isinstance(stmt, tuple) and stmt[0] == 'Defer':
                    defer_list.append(stmt[1])
                else:
                    result = evaluate(stmt, block_scope)
        except (ReturnValue, BreakSignal, ContinueSignal) as exc:
            saved_exc = exc
        # defer는 LIFO 순서로 실행 (예외 발생 여부와 무관)
        for defer_body in reversed(defer_list):
            try:
                evaluate(defer_body, block_scope)
            except Exception:
                pass  # defer 본문의 에러는 무시 (best-effort)
        if saved_exc is not None:
            raise saved_exc
        return result
    
    # 20a: comptime 블록 — 컴파일 타임 코드 실행
    # 인터프리터에서는 블록을 즉시 평가하고 결과값을 반환한다.
    # 격리된 스코프를 사용해서 comptime 안에서 선언한 변수가 바깥에 새지 않는다.
    # 하지만 바깥 상수(const)와 함수는 읽을 수 있다.
    if node_type == 'Comptime':
        body = node[1]  # Block 노드
        # comptime 블록의 결과는 블록의 마지막 식 값
        result = evaluate(body, scope)
        return result
    
    # 21a: unsafe 블록 — unsafe { ... }
    # 인터프리터에서는 _in_unsafe 플래그를 켜고 블록을 실행한다.
    # 21b에서 이 플래그를 확인해서 포인터 산술 등을 허용/금지한다.
    if node_type == 'UnsafeBlock':
        body = node[1]
        old_unsafe = _UNSAFE_DEPTH[0]
        _UNSAFE_DEPTH[0] += 1
        try:
            result = evaluate(body, scope)
        finally:
            _UNSAFE_DEPTH[0] = old_unsafe
        return result
    
    # 21a: unsafe fn — 함수 전체가 unsafe
    # FnDef와 동일하게 처리하되, 호출 시 unsafe 컨텍스트인지 검사 (21b에서).
    if node_type == 'UnsafeFn':
        fn_node = node[1]  # 내부 FnDef
        name = fn_node[1]
        params = fn_node[2]
        body = fn_node[3]
        scope.declare(name, ('UnsafeFunction', params, body, scope))
        return None
    
    if node_type == 'If':
        condition = evaluate(node[1], scope)
        if not isinstance(condition, bool):
            raise DanhaTypeError(f"if 조건은 bool이어야 해 (지금: {type(condition).__name__} 값 {condition})", line=node[-1], source=_CURRENT_SOURCE)
        if condition:
            return evaluate(node[2], scope)
        elif node[3] is not None:
            return evaluate(node[3], scope)
        return None
    
    # 8.4: match — tagged enum 패턴 매칭
    if node_type == 'Match':
        target = evaluate(node[1], scope)
        arms = node[2]
        line = node[-1]
        
        # target이 TaggedEnumValue인지 확인
        if not (isinstance(target, tuple) and target[0] == 'TaggedEnumValue'):
            # 단순 값(정수 등)에 대한 match도 지원 — variant 이름 대신 값 비교
            raise DanhaTypeError("match는 tagged enum 값에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)
        
        target_enum = target[1]
        target_variant = target[2]
        target_tag = target[3]
        target_data = target[4]
        
        matched = False
        for pattern, body in arms:
            if pattern[0] == 'MatchWildcard':
                # 와일드카드 — 항상 매칭
                match_scope = Scope(parent=scope)
                result = evaluate(body, match_scope)
                matched = True
                break
            elif pattern[0] == 'MatchVariant':
                vname = pattern[1]
                bindings = pattern[2]
                if vname == target_variant:
                    # 매칭! 바인딩 변수에 payload 값 할당
                    match_scope = Scope(parent=scope)
                    if len(bindings) != len(target_data):
                        raise DanhaRuntimeError(
                            f"'{vname}' 패턴의 바인딩 수({len(bindings)})와 "
                            f"데이터 수({len(target_data)})가 안 맞아",
                            line=line, source=_CURRENT_SOURCE
                        )
                    for bname, bval in zip(bindings, target_data):
                        match_scope.declare(bname, bval)
                    result = evaluate(body, match_scope)
                    matched = True
                    break
        
        if not matched:
            raise DanhaNameError(f"match에서 '{target_variant}' variant에 매칭되는 arm이 없어", line=line, source=_CURRENT_SOURCE)
        return result
    
    if node_type == 'While':
        # 7.15d: break/continue 지원.
        # ContinueSignal은 본체 한 번을 튕겨나와 다음 반복으로.
        # BreakSignal은 루프 전체를 종료.
        while True:
            cond_val = evaluate(node[1], scope)
            if not isinstance(cond_val, bool):
                raise DanhaTypeError(f"while 조건은 bool이어야 해 (지금: {type(cond_val).__name__} 값 {cond_val})", line=node[-1], source=_CURRENT_SOURCE)
            if not cond_val:
                break
            try:
                evaluate(node[2], scope)
            except ContinueSignal:
                continue
            except BreakSignal:
                break
        return None
    
    # for 문: for x in iterable { body }
    # 매 반복마다 새 스코프에서 x를 선언하고 body를 실행.
    # x는 for 블록 안에서만 보이고 끝나면 사라진다.
    if node_type == 'For':
        var_name = node[1]
        iterable = evaluate(node[2], scope)
        body = node[3]
        line = node[-1]
        
        # for의 iterable로 허용되는 것: 리스트, 문자열, range 객체.
        # range는 컴파일러와 의미를 맞추기 위해 추가됨 (6.9). 'for i in 0..n'.
        if not isinstance(iterable, (list, str, range)):
            raise DanhaTypeError("for는 리스트나 문자열만 순회할 수 있어", line=line, source=_CURRENT_SOURCE)
        
        # 시작 시점에 스냅샷을 떠서 그만큼만 돈다.
        # 안 그러면 본문에서 push로 같은 리스트에 원소를 더할 때
        # 파이썬 for가 무한히 따라가서 메모리 폭발.
        # (사용자가 새로 추가한 원소는 다음 프레임/다음 호출에서 처리하라는 정책.)
        # 리스트는 얕은 복사, 문자열/range는 불변이라 그대로 써도 안전.
        if isinstance(iterable, list):
            snapshot = list(iterable)
        else:
            snapshot = iterable
        
        for item in snapshot:
            # 반복마다 새 스코프를 만들어 반복 변수를 선언.
            # body가 Block이면 그 안에서 또 자식 스코프를 만들지만,
            # 반복 변수는 여기 이 for_scope에 있어서 body에서 보인다.
            for_scope = Scope(parent=scope)
            for_scope.declare(var_name, item)
            try:
                evaluate(body, for_scope)
            except ContinueSignal:
                continue
            except BreakSignal:
                break
        
        return None
    
    # 함수 정의: 함수값에 '정의된 곳의 스코프'를 같이 저장한다.
    # 이게 어휘적 스코프의 핵심 - 함수는 자기가 태어난 곳을 기억한다.
    if node_type == 'FnDef':
        name = node[1]
        params = node[2]
        body = node[3]
        scope.declare(name, ('Function', params, body, scope))
        return None

    # 45단계: export fn — 평가기에서는 일반 함수와 동일 (공개 여부는 AOT에서만 의미 있음)
    if node_type == 'ExportFn':
        return evaluate(node[1], scope)
    
    # 22a: 매크로 정의 — macro NAME!(params) { body }
    # 매크로는 호출 시점의 스코프에서 본문을 실행하는 "인라인 확장" 방식.
    # 함수와 달리 호출자의 변수를 직접 읽고 수정할 수 있다.
    if node_type == 'MacroDef':
        name = node[1]
        params = node[2]  # [(이름, 가변여부), ...]
        body = node[3]
        is_variadic = node[4]
        scope.declare(name, ('Macro', params, body, is_variadic))
        return None
    
    # 22a: 매크로 호출 — NAME!(args)
    if node_type == 'MacroCall':
        name = node[1]
        args = node[2]
        line = node[-1]
        
        if not scope.has(name):
            raise DanhaNameError(f"정의되지 않은 매크로야: {name}!", line=line, source=_CURRENT_SOURCE)
        macro = scope.get(name)
        if not isinstance(macro, tuple) or macro[0] != 'Macro':
            raise DanhaNameError(f"'{name}'은(는) 매크로가 아니야", line=line, source=_CURRENT_SOURCE)
        
        _, params, body, is_variadic = macro
        
        # 매크로는 호출자의 스코프에서 본문을 실행한다 (인라인 확장)
        # 파라미터를 인자 값으로 바인딩한 새 스코프를 만들되,
        # 호출자 스코프를 부모로 해서 호출자의 변수에 접근 가능하게.
        macro_scope = Scope(parent=scope)
        
        if is_variadic:
            # 가변 인자: 마지막 파라미터에 나머지 인자를 리스트로 묶음
            fixed_count = len(params) - 1
            for i, (pname, _) in enumerate(params[:-1]):
                if i < len(args):
                    macro_scope.declare(pname, evaluate(args[i], scope))
                else:
                    raise DanhaValueError(
                        f"매크로 '{name}!'은(는) 최소 {fixed_count}개의 인자가 필요한데 {len(args)}개가 들어왔어",
                        line=line, source=_CURRENT_SOURCE
                    )
            # 나머지 인자를 리스트로
            var_param_name = params[-1][0]
            rest = [evaluate(a, scope) for a in args[fixed_count:]]
            macro_scope.declare(var_param_name, rest)
        else:
            if len(args) != len(params):
                raise DanhaValueError(
                    f"매크로 '{name}!'은(는) {len(params)}개의 인자가 필요한데 {len(args)}개가 들어왔어",
                    line=line, source=_CURRENT_SOURCE
                )
            for (pname, _), arg in zip(params, args):
                macro_scope.declare(pname, evaluate(arg, scope))
        
        try:
            result = evaluate(body, macro_scope)
        except ReturnValue as rv:
            result = rv.value
        return result
    
    # 15: 익명 함수(람다) — fn(a, b) { return a + b }
    # FnDef와 같은 값을 만들되, 스코프에 등록하지 않고 값으로 반환.
    # 정의 시점의 스코프를 캡처하므로 클로저가 자연스럽게 동작.
    if node_type == 'Lambda':
        params = node[1]
        body = node[2]
        return ('Function', params, body, scope)
    
    # 외부 함수 선언 (C-FFI): 인터프리터에서는 등록만 한다.
    # 실제 C 함수를 호출할 수는 없지만, 파싱 에러 없이 넘어가게.
    # 호출하면 에러 메시지로 안내한다.
    if node_type == 'ExternFn':
        name = node[1]
        params = node[2]
        scope.declare(name, ('ExternFunction', name, params))
        return None
    
    if node_type == 'CLinkedFn':
        # @clink 어노테이션이 붙은 extern fn — 내부 ExternFn과 동일하게 처리
        inner = node[2]
        name = inner[1]
        params = inner[2]
        scope.declare(name, ('ExternFunction', name, params))
        return None
    
    # 49단계: /// doc comment 어노테이션 — 내부 선언만 실행
    if node_type == 'DocAnnotated':
        return evaluate(node[2], scope)

    # 48단계: test 블록
    if node_type == 'TestBlock':
        global _TEST_MODE, _TEST_RESULTS
        if not _TEST_MODE:
            return None
        test_name = node[1]
        test_body = node[2]
        line = node[3]
        try:
            evaluate(test_body, Scope(parent=scope))
            _TEST_RESULTS.append({'name': test_name, 'passed': True, 'error': None})
        except AssertionError as e:
            _TEST_RESULTS.append({'name': test_name, 'passed': False, 'error': str(e)})
        except Exception as e:
            _TEST_RESULTS.append({'name': test_name, 'passed': False, 'error': str(e)})
        return None

    # 35단계: @attribute가 붙은 선언 처리
    if node_type == 'Attributed':
        global _ATTRIBUTES
        attrs = node[1]   # [('Attribute', name, args, line), ...]
        inner = node[2]   # 실제 선언 (StructDef, FnDef 등)
        
        # 내부 선언을 먼저 실행 (struct/fn/trait 등록)
        result = evaluate(inner, scope)
        
        # 선언의 이름을 추출
        target_name = None
        if isinstance(inner, tuple):
            if inner[0] in ('StructDef', 'UnionDef', 'FnDef', 'ComponentDef', 'TraitDef'):
                target_name = inner[1]
            elif inner[0] == 'Attributed':
                # 중첩 — 안쪽의 이름 추출
                if isinstance(inner[2], tuple) and len(inner[2]) > 1:
                    target_name = inner[2][1]
        
        if target_name:
            attr_list = []
            for attr in attrs:
                attr_name = attr[1]
                attr_args = {}
                for arg in attr[2]:
                    if arg[0] == 'KeyVal':
                        attr_args[arg[1]] = arg[2]
                    elif arg[0] == 'StringArg':
                        attr_args[len(attr_args)] = arg[1]
                    elif arg[0] == 'NumArg':
                        attr_args[len(attr_args)] = arg[1]
                    elif arg[0] == 'NameArg':
                        attr_args[len(attr_args)] = arg[1]
                attr_list.append((attr_name, attr_args))
            
            if target_name in _ATTRIBUTES:
                _ATTRIBUTES[target_name].extend(attr_list)
            else:
                _ATTRIBUTES[target_name] = attr_list
        
        return result
    
    # 함수 호출: 함수가 기억하고 있는 '정의된 곳의 스코프'를 부모로 삼아
    # 새 함수 스코프를 만든다. 호출한 쪽의 지역 변수는 안 보인다.
    # 내장 함수(Builtin)는 특별히 처리한다.
    # 15: 식 결과 호출 — callbacks[i](10), make_adder(5)(3) 등
    # 이름이 아닌 식(expression)의 결과가 함수 값일 때 호출.
    # 23b: ? 연산자 — Result 에러 전파.
    # expr? → tagged enum 값을 검사:
    #   Ok(val) → val을 꺼냄
    #   Err(e)  → 현재 함수에서 Err(e)를 그대로 반환 (ReturnValue로 던짐)
    # 어떤 tagged enum이든 첫 variant가 "성공", 나머지가 "실패"로 간주하지 않고,
    # 정확히 Ok/Err 이름을 가진 variant만 인식한다.
    if node_type == 'QuestionOp':
        inner = node[1]
        line = node[-1]
        val = evaluate(inner, scope)
        
        if not (isinstance(val, tuple) and val[0] == 'TaggedEnumValue'):
            raise DanhaTypeError(
                "? 연산자는 tagged enum (Ok/Err variant가 있는) 값에만 쓸 수 있어",
                line=line, source=_CURRENT_SOURCE
            )
        
        variant_name = val[2]
        payload = val[4]
        
        if variant_name == 'Ok':
            # Ok(val) → 내부 값 추출
            if len(payload) == 1:
                return payload[0]
            elif len(payload) == 0:
                return None
            else:
                return tuple(payload)
        elif variant_name == 'Err':
            # Err(e) → 현재 함수를 Err(e)로 즉시 반환
            raise ReturnValue(val)
        else:
            raise DanhaTypeError(
                f"? 연산자는 Ok 또는 Err variant에만 쓸 수 있어 (지금: {variant_name})",
                line=line, source=_CURRENT_SOURCE
            )
    
    if node_type == 'CallExpr':
        callee_expr = node[1]
        args = node[2]
        line = node[-1]
        
        func = evaluate(callee_expr, scope)
        
        if not isinstance(func, tuple) or func[0] != 'Function':
            raise DanhaTypeError("호출 대상이 함수가 아니야", line=line, source=_CURRENT_SOURCE)
        
        func_params = func[1]
        func_body = func[2]
        func_scope = func[3]
        
        arg_values = [evaluate(arg, scope) for arg in args]
        
        if len(arg_values) != len(func_params):
            raise DanhaValueError(
                f"함수는 {len(func_params)}개의 인자가 필요한데 {len(arg_values)}개가 들어왔어",
                line=line, source=_CURRENT_SOURCE
            )
        
        call_scope = Scope(parent=func_scope)
        for pname, pval in zip(func_params, arg_values):
            call_scope.declare(pname, pval)
        
        try:
            evaluate(func_body, call_scope)
        except ReturnValue as ret:
            return ret.value
        return None
    
    if node_type == 'Call':
        name = node[1]
        args = node[2]
        line = node[-1]
        
        if not scope.has(name):
            raise DanhaNameError(f"정의되지 않은 함수야: {name}", line=line, source=_CURRENT_SOURCE)
        
        func = scope.get(name)
        
        if not isinstance(func, tuple):
            raise DanhaTypeError(f"{name}은(는) 함수가 아니야", line=line, source=_CURRENT_SOURCE)
        
        # 내장 함수 (파이썬으로 구현된 것)
        if func[0] == 'Builtin':
            builtin_fn = func[1]
            # 인자를 호출한 쪽 스코프에서 평가
            arg_values = []
            for arg in args:
                arg_values.append(evaluate(arg, scope))
            # 파이썬 함수 호출. 첫 인자로 줄 번호를 넘겨서 에러 메시지에 쓸 수 있게 함.
            return builtin_fn(line, arg_values)
        
        if func[0] != 'Function' and func[0] != 'System' and func[0] != 'UnsafeFunction':
            if func[0] == 'ExternFunction':
                raise DanhaNameError(f"'{name}'은(는) extern 함수야. 인터프리터에서는 C 함수를 호출할 수 없어 — 컴파일러로 실행해봐", line=line, source=_CURRENT_SOURCE)
            raise DanhaTypeError(f"{name}은(는) 함수가 아니야", line=line, source=_CURRENT_SOURCE)
        
        # 21b: unsafe 함수는 unsafe 컨텍스트에서만 호출 가능
        if func[0] == 'UnsafeFunction' and _UNSAFE_DEPTH[0] == 0:
            raise DanhaRuntimeError(
                f"unsafe 함수 '{name}'은(는) unsafe 블록 안에서만 호출할 수 있어",
                line=line, source=_CURRENT_SOURCE
            )
        
        # 7.13b/c: system 호출 — for each 순회 실행
        if func[0] == 'System':
            sys_params = func[1]      # 매개변수 이름 리스트
            sys_bindings = func[2]    # [(변수명, 컴포넌트명), ...]
            sys_body = func[3]        # Block 노드
            sys_is_parallel = func[4] # True/False
            sys_access_map = func[5]  # {바인딩변수: 'read'|'write'}
            sys_defining_scope = func[6]
            
            if len(args) != len(sys_params):
                raise DanhaValueError(
                    f"{name}은(는) {len(sys_params)}개의 인자가 필요한데 {len(args)}개가 들어왔어",
                    line=line, source=_CURRENT_SOURCE
                )
            
            # 인자 평가
            arg_values = [evaluate(arg, scope) for arg in args]
            
            # 바인딩을 종류별로 분류: required, optional, exclude
            required_bindings = []
            optional_bindings = []
            exclude_bindings  = []
            for binding in sys_bindings:
                bind_var, comp_name, _access = binding[0], binding[1], binding[2]
                kind = binding[3] if len(binding) > 3 else 'required'
                if kind == 'exclude':
                    exclude_bindings.append((comp_name,))
                elif kind == 'optional':
                    optional_bindings.append((bind_var, comp_name))
                else:
                    required_bindings.append((bind_var, comp_name))

            # required 컴포넌트 저장소 확인
            req_stores = []
            for bind_var, comp_name in required_bindings:
                if comp_name not in _WORLD.stores:
                    raise DanhaECSError(
                        f"system '{name}'에서 사용하는 컴포넌트 '{comp_name}'이(가) 정의되지 않았어",
                        line=line, source=_CURRENT_SOURCE
                    )
                req_stores.append((bind_var, comp_name, _WORLD.stores[comp_name]))

            # optional 컴포넌트 저장소 (없어도 괜찮음)
            opt_stores = []
            for bind_var, comp_name in optional_bindings:
                store = _WORLD.stores.get(comp_name)  # 저장소 없으면 None
                opt_stores.append((bind_var, comp_name, store))

            # exclude 저장소 (있으면 제외)
            excl_stores = []
            for (comp_name,) in exclude_bindings:
                store = _WORLD.stores.get(comp_name)
                if store is not None:
                    excl_stores.append(store)

            # 기준 저장소: required 중 가장 작은 것
            if len(req_stores) == 0:
                # required 없고 optional만 있는 경우: 전체 alive 엔티티 순회
                entity_snapshot = [i for i, a in enumerate(_WORLD.alive) if a]
                pivot_store = None
            else:
                pivot_idx = 0
                pivot_count = len(req_stores[0][2].dense_to_entity)
                for ci in range(1, len(req_stores)):
                    c = len(req_stores[ci][2].dense_to_entity)
                    if c < pivot_count:
                        pivot_count = c
                        pivot_idx = ci
                pivot_store = req_stores[pivot_idx][2]
                entity_snapshot = list(pivot_store.dense_to_entity)
            
            # 28단계: 엔티티 하나에 대해 쿼리 체크 + 스코프 바인딩 → 본문 실행
            def _run_entity(entity_idx):
                # alive 체크 (ECS World에 살아있는지)
                if entity_idx >= len(_WORLD.alive) or not _WORLD.alive[entity_idx]:
                    return True  # continue
                # exclude 필터: excl_stores 중 하나라도 가지면 건너뜀
                for excl_store in excl_stores:
                    if excl_store.has(entity_idx):
                        return True  # continue
                # required 필터: req_stores 모두 가져야 함
                for _, comp_name, req_store in req_stores:
                    if not req_store.has(entity_idx):
                        return True  # continue
                # 스코프 생성
                iter_scope = Scope(parent=sys_defining_scope)
                for param_name, arg_value in zip(sys_params, arg_values):
                    iter_scope.declare(param_name, arg_value)
                # required 바인딩
                for bind_var, comp_name, req_store in req_stores:
                    is_readonly = sys_access_map.get(bind_var) == 'read'
                    proxy = _SystemComponentProxy(req_store, entity_idx, readonly=is_readonly, bind_var=bind_var)
                    iter_scope.declare(bind_var, proxy)
                # optional 바인딩: 컴포넌트 없으면 None
                for bind_var, comp_name, opt_store in opt_stores:
                    if opt_store is not None and opt_store.has(entity_idx):
                        is_readonly = sys_access_map.get(bind_var) == 'read'
                        proxy = _SystemComponentProxy(opt_store, entity_idx, readonly=is_readonly, bind_var=bind_var)
                        iter_scope.declare(bind_var, proxy)
                    else:
                        iter_scope.declare(bind_var, None)
                evaluate(sys_body, iter_scope)
                return False  # 정상 실행

            # 43단계: parallel system이면 Python 스레드로 실제 병렬 실행.
            if sys_is_parallel:
                _n = min(_os_cpu.cpu_count() or 1, len(entity_snapshot)) if entity_snapshot else 0
                if _n <= 1:
                    for entity_idx in entity_snapshot:
                        try:
                            _run_entity(entity_idx)
                        except ContinueSignal:
                            continue
                        except BreakSignal:
                            break
                else:
                    _chunk_sz = (len(entity_snapshot) + _n - 1) // _n
                    _chunks = [entity_snapshot[i:i+_chunk_sz]
                               for i in range(0, len(entity_snapshot), _chunk_sz)]
                    _stop = [False]
                    _errs = []
                    def _run_chunk(ch):
                        for eid in ch:
                            if _stop[0]:
                                break
                            try:
                                _run_entity(eid)
                            except ContinueSignal:
                                continue
                            except BreakSignal:
                                _stop[0] = True
                                break
                            except Exception as _exc:
                                _errs.append(_exc)
                                _stop[0] = True
                                break
                    _ts = [_threading.Thread(target=_run_chunk, args=(ch,)) for ch in _chunks]
                    for _t in _ts: _t.start()
                    for _t in _ts: _t.join()
                    if _errs:
                        raise _errs[0]
            else:
                for entity_idx in entity_snapshot:
                    try:
                        _run_entity(entity_idx)
                    except ContinueSignal:
                        continue
                    except BreakSignal:
                        break
            
            return None
        
        func_params = func[1]
        func_body = func[2]
        func_defining_scope = func[3]
        
        if len(args) != len(func_params):
            raise DanhaValueError(
                f"{name}은(는) {len(func_params)}개의 인자가 필요한데 {len(args)}개가 들어왔어",
                line=line, source=_CURRENT_SOURCE
            )
        
        # 인자는 '호출한 쪽' 스코프에서 평가한다 (그래야 호출한 쪽의 변수를 쓸 수 있음)
        arg_values = []
        for arg in args:
            arg_values.append(evaluate(arg, scope))
        
        # 함수 본체는 '정의된 곳'의 스코프를 부모로 하는 새 스코프에서 실행
        func_scope = Scope(parent=func_defining_scope)
        for param_name, arg_value in zip(func_params, arg_values):
            func_scope.declare(param_name, arg_value)
        
        # 21a: unsafe fn이면 본문 실행 중 _UNSAFE_DEPTH를 올림
        is_unsafe_fn = (func[0] == 'UnsafeFunction')
        if is_unsafe_fn:
            _UNSAFE_DEPTH[0] += 1

        # 51단계: 프로파일링
        if _PROFILING:
            import time as _time
            _t0 = _time.perf_counter()

        try:
            result = evaluate(func_body, func_scope)
        except ReturnValue as rv:
            result = rv.value
        # 7.15d: break/continue는 함수 경계를 넘지 않음.
        # (아래 BreakSignal/ContinueSignal은 try 바깥에서 자연 전파 — try에 걸리지 않음.
        #  따로 잡아서 친절한 메시지로 바꿔준다.)
        except BreakSignal:
            if is_unsafe_fn:
                _UNSAFE_DEPTH[0] -= 1
            raise DanhaRuntimeError(f"함수 '{name}' 안의 'break'가 루프 바깥까지 나왔어", line=line, source=_CURRENT_SOURCE)
        except ContinueSignal:
            if is_unsafe_fn:
                _UNSAFE_DEPTH[0] -= 1
            raise DanhaRuntimeError(f"함수 '{name}' 안의 'continue'가 루프 바깥까지 나왔어", line=line, source=_CURRENT_SOURCE)
        
        if is_unsafe_fn:
            _UNSAFE_DEPTH[0] -= 1

        # 51단계: 프로파일링 결과 기록
        if _PROFILING:
            _elapsed = _time.perf_counter() - _t0
            if name not in _PROFILE_STATS:
                _PROFILE_STATS[name] = {'calls': 0, 'total': 0.0}
            _PROFILE_STATS[name]['calls'] += 1
            _PROFILE_STATS[name]['total'] += _elapsed

        return result

    if node_type == 'Return':
        value = None
        if node[1] is not None:
            value = evaluate(node[1], scope)
        raise ReturnValue(value)
    
    # 7.15d: 루프 제어 흐름.
    # 예외로 던져서 가장 가까운 루프 헤더가 잡는다.
    # 루프 바깥이면 아무도 안 잡아서 최상위까지 전파 → 사용자 친화 메시지로 변환.
    if node_type == 'Break':
        raise BreakSignal()
    
    if node_type == 'Continue':
        raise ContinueSignal()
    
    # 7.15a: const 정의. 값을 평가해서 스코프에 상수로 등록.
    # 재대입하려 하면 Scope.set_existing에서 에러를 낸다.
    # 9.1b: import 문 — 모듈 파일을 로드하여 네임스페이스로 등록
    if node_type == 'Import':
        module_name = node[1]
        line = node[-1]
        ns = _load_module(module_name, line)
        # import physics.collision → scope에 'collision'으로 등록
        # (마지막 이름만 사용 — Python과 다르게 짧은 이름으로)
        short_name = module_name.split('.')[-1]
        scope.declare(short_name, ns)
        return None
    
    # 9.1b: from ... import 문 — 모듈에서 특정 이름만 가져오기
    if node_type == 'FromImport':
        module_name = node[1]
        names = node[2]  # ['sin', 'cos'] 또는 '*'
        line = node[-1]
        ns = _load_module(module_name, line)
        if names == '*':
            # from math import * → 모듈의 모든 이름을 현재 스코프에 등록
            for name, value in ns.members.items():
                scope.declare(name, value)
        else:
            # from math import sin, cos → 지정된 이름만 등록
            for name in names:
                if name not in ns.members:
                    raise DanhaNameError(
                        f"모듈 '{module_name}'에 '{name}'이 없어",
                        line=line, source=_CURRENT_SOURCE
                    )
                scope.declare(name, ns.members[name])
        return None
    
    if node_type == 'ConstDef':
        name = node[1]
        value = evaluate(node[2], scope)
        scope.declare_const(name, value)
        return None
    
    # 7.15b / 8.3: enum 정의.
    # 단순 enum: 모든 variant에 데이터가 없으면 기존처럼 정수 매핑.
    # tagged union: 하나라도 데이터가 있으면 TaggedEnumDef로 등록.
    if node_type == 'EnumDef':
        name = node[1]
        variants = node[2]  # [(이름, 타입리스트|None), ...]
        
        # 하나라도 데이터가 있으면 tagged union
        has_data = any(vtypes is not None for _, vtypes in variants)
        
        if not has_data:
            # 기존 단순 enum
            variant_map = {}
            for i, (vname, _) in enumerate(variants):
                variant_map[vname] = i
            scope.declare(name, ('EnumDef', variant_map))
        else:
            # tagged union: variant_info = {이름: (태그, [타입문자열, ...]|None)}
            variant_info = {}
            for i, (vname, vtypes) in enumerate(variants):
                variant_info[vname] = (i, vtypes)
            scope.declare(name, ('TaggedEnumDef', name, variant_info))
        return None
    
    # 구조체 정의: 현재 스코프에 선언
    # methods는 딕셔너리로 같이 저장 - impl 블록에서 채워짐.
    # 메서드와 필드가 한 곳에 모여 있으면 캡슐화도 자연스럽다.
    if node_type == 'StructDef':
        name = node[1]
        fields = node[2]
        scope.declare(name, ('StructDef', fields, {}))
        return None

    # union 정의: ('UnionDef', name, fields, line)
    # 인터프리터에서는 StructDef와 동일한 레이아웃으로 저장.
    # 필드 딕셔너리로 값을 보관하며, 메모리 공유 의미론은 컴파일러가 처리.
    if node_type == 'UnionDef':
        name = node[1]
        fields = node[2]
        scope.declare(name, ('UnionDef', fields, {}))
        return None

    # 컴포넌트 정의. 7.12a 단계에서는 파서만 연결하고 실행 의미는 비워둔다.
    # 7.12b에서 SoA 저장소를 만들면서 본격 구현.
    # 지금은 선언해두지 않으면 다음 줄에서 'Position'을 이름으로 써먹을 수 없고,
    # 실수로 써먹으면 "정의되지 않은 이름" 같은 엉뚱한 에러가 나올 수 있으니
    # 빈 껍질이라도 스코프에 등록해 둔다.
    # 7.12c: 저장 형태에 이름을 추가 — 내장 함수(get/has/remove)가 이름으로 저장소를 찾음.
    if node_type == 'ComponentDef':
        name = node[1]
        fields = node[2]
        scope.declare(name, ('ComponentDef', name, fields, {}))
        # 7.13: system의 for each가 컴포넌트를 찾을 수 있도록 미리 저장소 등록.
        # 아직 아무 엔티티에도 add하지 않았어도 빈 저장소가 있어야 순회가 0회로 끝남.
        _WORLD.get_or_create_store(name, fields)
        return None
    
    # 7.13: system 등록.
    # ('SystemDef', name, params, param_types, bindings, body, is_parallel, line)
    # 스코프에 ('System', params, bindings, body, is_parallel, scope) 형태로 저장.
    # 호출은 일반 함수처럼 이름(인자) 형태.
    if node_type == 'SystemDef':
        name = node[1]
        params = node[2]
        # param_types (node[3])는 인터프리터에서 무시 (컴파일러용)
        bindings = node[4]
        body = node[5]
        is_parallel = node[6]
        line = node[-1]
        # 7.11b: 바인딩 안전성 분석 (중복 검사 + 읽기/쓰기 분류)
        access_map, comp_access_map = _analyze_system_bindings(name, bindings, body, is_parallel, line)
        scope.declare(name, ('System', params, bindings, body, is_parallel, access_map, scope))
        # 7.14a: 전역 system 레지스트리에 등록 — schedule()이 사용
        _SYSTEM_REGISTRY[name] = {
            'comp_access_map': comp_access_map,
            'is_parallel': is_parallel,
            'params': params,
            'defining_scope': scope,  # 7.14c: schedule이 system을 찾을 수 있도록
        }
        return None
    
    # impl 블록: 이미 정의된 구조체에 메서드를 추가한다.
    # 한 구조체에 여러 impl을 허용 (누적). 같은 이름의 메서드가 있으면 덮어씀.
    if node_type == 'Impl':
        type_name = node[1]
        methods = node[2]
        line = node[-1]
        
        if not scope.has(type_name):
            raise DanhaNameError(f"정의되지 않은 구조체야: {type_name}", line=line, source=_CURRENT_SOURCE)
        
        struct_def = scope.get(type_name)
        if not isinstance(struct_def, tuple) or struct_def[0] not in ('StructDef', 'UnionDef', 'ComponentDef'):
            raise DanhaTypeError(f"{type_name}은(는) 구조체/union이 아니야 (impl은 구조체/union에만 쓸 수 있어)", line=line, source=_CURRENT_SOURCE)

        # StructDef: ('StructDef', fields, methods)  → methods at [2]
        # UnionDef:  ('UnionDef',  fields, methods)  → methods at [2]
        # ComponentDef: ('ComponentDef', name, fields, methods)  → methods at [3]
        if struct_def[0] in ('StructDef', 'UnionDef'):
            struct_methods = struct_def[2]
        else:
            struct_methods = struct_def[3]
        for method_node in methods:
            method_name = method_node[1]
            method_params = method_node[2]
            method_body = method_node[3]
            struct_methods[method_name] = ('Function', method_params, method_body, scope)
        
        return None
    
    # 7.8b: 트레잇 정의 등록.
    # ('TraitDef', name, [method_nodes...], line)
    # 스코프에 ('TraitDef', name, {method_name: ('Function', params, body, scope), ...}) 저장.
    if node_type == 'TraitDef':
        name = node[1]
        methods = node[2]
        line = node[-1]
        
        trait_methods = {}
        for method_node in methods:
            method_name = method_node[1]
            method_params = method_node[2]
            method_body = method_node[3]
            trait_methods[method_name] = ('Function', method_params, method_body, scope)
        
        scope.declare(name, ('TraitDef', name, trait_methods))
        return None
    
    # 7.8b: impl Trait for Type — 트레잇 메서드를 타입에 연결.
    # ('ImplTrait', trait_name, type_name, methods, line)
    # 트레잇에 정의된 메서드를 타입의 메서드 딕셔너리에 추가.
    # impl에서 제공한 메서드가 트레잇의 기본 구현을 덮어씀.
    if node_type == 'ImplTrait':
        trait_name = node[1]
        type_name = node[2]
        methods = node[3]
        line = node[-1]
        
        if not scope.has(trait_name):
            raise DanhaNameError(f"정의되지 않은 트레잇이야: {trait_name}", line=line, source=_CURRENT_SOURCE)
        trait_def = scope.get(trait_name)
        if not isinstance(trait_def, tuple) or trait_def[0] != 'TraitDef':
            raise DanhaTypeError(f"{trait_name}은(는) 트레잇이 아니야", line=line, source=_CURRENT_SOURCE)
        
        if not scope.has(type_name):
            raise DanhaTypeError(f"정의되지 않은 타입이야: {type_name}", line=line, source=_CURRENT_SOURCE)
        type_def = scope.get(type_name)
        if not isinstance(type_def, tuple) or type_def[0] not in ('StructDef', 'UnionDef', 'ComponentDef'):
            raise DanhaTypeError(f"{type_name}은(는) 구조체/union/컴포넌트가 아니야", line=line, source=_CURRENT_SOURCE)

        # 타입의 메서드 딕셔너리
        if type_def[0] in ('StructDef', 'UnionDef'):
            type_methods = type_def[2]
        else:
            type_methods = type_def[3]
        
        # 1) 트레잇의 기본 구현을 먼저 등록
        trait_methods = trait_def[2]
        for mname, mfunc in trait_methods.items():
            if mname not in type_methods:
                type_methods[mname] = mfunc
        
        # 2) impl에서 제공한 메서드로 덮어쓰기
        for method_node in methods:
            method_name = method_node[1]
            method_params = method_node[2]
            method_body = method_node[3]
            type_methods[method_name] = ('Function', method_params, method_body, scope)
        
        return None
    
    # 구조체/컴포넌트 인스턴스 생성.
    # 문법은 'Name { field: value, ... }'로 동일. Name이 StructDef인지 ComponentDef인지에 따라
    # StructValue 또는 ComponentValue로 평가됨. 구분은 add() 같은 내장 함수가 타입 체크할 때 필요.
    if node_type == 'StructInstance':
        type_name = node[1]
        field_exprs = node[2]
        line = node[-1]
        
        if not scope.has(type_name):
            raise DanhaNameError(f"정의되지 않은 구조체야: {type_name}", line=line, source=_CURRENT_SOURCE)
        
        type_def = scope.get(type_name)
        if not isinstance(type_def, tuple):
            raise DanhaTypeError(f"{type_name}은(는) 구조체가 아니야", line=line, source=_CURRENT_SOURCE)
        
        # StructDef: ('StructDef', fields, methods) — fields 위치 [1]
        # UnionDef:  ('UnionDef',  fields, methods) — fields 위치 [1]
        # ComponentDef: ('ComponentDef', name, fields, methods) — fields 위치 [2]
        if type_def[0] == 'StructDef':
            type_fields = type_def[1]
            is_component = False
            is_union = False
        elif type_def[0] == 'UnionDef':
            type_fields = type_def[1]
            is_component = False
            is_union = True
        elif type_def[0] == 'ComponentDef':
            type_fields = type_def[2]
            is_component = True
            is_union = False
        else:
            raise DanhaTypeError(f"{type_name}은(는) 구조체/union/컴포넌트가 아니야", line=line, source=_CURRENT_SOURCE)

        expected_fields = [f[0] for f in type_fields]

        # union은 부분 필드 초기화 허용 (최소 1개). struct/component는 전체 필드 필수.
        if not is_union:
            for field_name in expected_fields:
                if field_name not in field_exprs:
                    raise DanhaNameError(f"{type_name}의 필드 '{field_name}'이(가) 없어", line=line, source=_CURRENT_SOURCE)

        for field_name in field_exprs:
            if field_name not in expected_fields:
                raise DanhaNameError(f"{type_name}에 '{field_name}'이라는 필드는 없어", line=line, source=_CURRENT_SOURCE)
        
        # 필드 타입 노드 맵 구성 (배열 자동 초기화에 사용)
        field_type_map = {f[0]: f[1] for f in type_fields}

        field_values = {}
        for field_name, field_expr in field_exprs.items():
            val = evaluate(field_expr, scope)
            ftype = field_type_map.get(field_name)
            # 배열 타입 필드: [] 제공 시 올바른 크기로 자동 확장
            if (ftype is not None and isinstance(ftype, tuple) and ftype[0] == 'ArrayType'
                    and isinstance(val, list) and len(val) == 0):
                count = ftype[2]
                elem_node = ftype[1]
                # 원소 타입에 따른 기본값
                elem_kind = elem_node[0] if isinstance(elem_node, tuple) else ''
                elem_name = elem_node[1] if (isinstance(elem_node, tuple) and len(elem_node) > 1) else ''
                if elem_kind == 'TypeName' and elem_name in ('i32', 'i16', 'i64', 'i8'):
                    default = 0
                elif elem_kind == 'TypeName' and elem_name in ('f64', 'f32'):
                    default = 0.0
                elif elem_kind == 'TypeName' and elem_name == 'bool':
                    default = False
                else:
                    default = None
                val = [default] * count
            field_values[field_name] = val

        if is_component:
            # ComponentValue는 타입 필드 정보도 같이 들고 다님 — add()가 저장소를 만들 때 필요.
            return ('ComponentValue', type_name, field_values, type_fields)
        return ('StructValue', type_name, field_values)
    
    # 필드 접근: 객체에서 특정 필드의 값을 꺼냄
    if node_type == 'FieldAccess':
        obj = evaluate(node[1], scope)
        field_name = node[2]
        line = node[-1]
        
        # 29단계: TraitObject 필드 접근 → 내부 값에 위임
        if isinstance(obj, tuple) and obj[0] == 'TraitObject':
            obj = obj[2]
        
        # 9.1b: 모듈 네임스페이스 필드 접근 — math.PI, math.Vec3 등
        if isinstance(obj, ModuleNamespace):
            try:
                return obj.get(field_name)
            except KeyError:
                raise DanhaNameError(f"모듈 '{obj.name}'에 '{field_name}'이 없어", line=line, source=_CURRENT_SOURCE)
        
        # 7.15b: enum variant 접근 — Phase.Chase → 정수 값
        if isinstance(obj, tuple) and len(obj) >= 1 and obj[0] == 'EnumDef':
            variant_map = obj[1]
            if field_name not in variant_map:
                raise DanhaNameError(f"이 enum에 '{field_name}'이라는 variant가 없어", line=line, source=_CURRENT_SOURCE)
            return variant_map[field_name]
        
        # 8.3: tagged enum — 데이터 없는 variant는 FieldAccess, 있는 variant는 MethodCall
        if isinstance(obj, tuple) and obj[0] == 'TaggedEnumDef':
            enum_name = obj[1]
            variant_info = obj[2]
            if field_name not in variant_info:
                raise DanhaNameError(f"enum '{enum_name}'에 '{field_name}'이라는 variant가 없어", line=line, source=_CURRENT_SOURCE)
            tag, vtypes = variant_info[field_name]
            if vtypes is not None:
                raise DanhaRuntimeError(f"'{field_name}'은(는) 데이터를 가진 variant야 — {enum_name}.{field_name}(...) 형태로 써", line=line, source=_CURRENT_SOURCE)
            return ('TaggedEnumValue', enum_name, field_name, tag, [])
        
        # 7.13: system for each 프록시 — SoA에서 직접 읽기
        if isinstance(obj, _SystemComponentProxy):
            if not obj.has_field(field_name):
                raise DanhaNameError(f"{obj.comp_name}에 '{field_name}'이라는 필드가 없어", line=line, source=_CURRENT_SOURCE)
            return obj.get_field(field_name)
        
        # 7.7: 벡터 필드 접근 (.x, .y, .z, .w)
        if isinstance(obj, tuple) and len(obj) >= 1 and obj[0] == 'VecValue':
            vec_type = obj[1]
            components = obj[2]
            field_map = _vec_field_map(vec_type)
            if field_name not in field_map:
                raise DanhaNameError(f"{vec_type}에 '{field_name}'이라는 필드가 없어", line=line, source=_CURRENT_SOURCE)
            return components[field_map[field_name]]
        
        # 7.12c: StructValue와 ComponentValue 둘 다 필드 접근 허용.
        # 저장 형태의 [1]은 타입 이름, [2]는 {필드이름: 값} 딕셔너리로 같음.
        if isinstance(obj, tuple) and len(obj) >= 1 and obj[0] in ('StructValue', 'ComponentValue'):
            type_name = obj[1]
            fields = obj[2]
            if field_name not in fields:
                raise DanhaNameError(f"{type_name}에 '{field_name}'이라는 필드가 없어", line=line, source=_CURRENT_SOURCE)
            return fields[field_name]
        
        raise DanhaRuntimeError("필드 접근은 구조체나 벡터에만 할 수 있어", line=line, source=_CURRENT_SOURCE)
    
    # 메서드 호출: obj.method(args...)
    # 해석: 평가된 obj의 타입을 찾아서 해당 메서드를 실행.
    # self 매개변수에 obj를 바인딩.
    if node_type == 'MethodCall':
        obj = evaluate(node[1], scope)
        method_name = node[2]
        args = node[3]
        line = node[-1]
        
        # 24a: Arena 내장 타입 메서드 — Arena.new/reset/destroy/used
        if isinstance(obj, tuple) and obj[0] == 'ArenaType':
            arg_values = [evaluate(a, scope) for a in args]
            if method_name == 'new':
                if len(arg_values) != 1 or not isinstance(arg_values[0], int):
                    raise DanhaValueError("Arena.new(크기)에는 정수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                cap = arg_values[0]
                # 인터프리터에서 아레나 = 파이썬 딕셔너리 (컴파일러에서는 실제 메모리)
                return ('ArenaValue', {'capacity': cap, 'used': 0, 'alive': True})
            elif method_name == 'alloc':
                # 25a: Arena.alloc(arena, size) 정적 호출
                if len(arg_values) != 2:
                    raise DanhaValueError("Arena.alloc(arena, size)에는 인자 2개가 필요해", line=line, source=_CURRENT_SOURCE)
                arena = arg_values[0]
                size = arg_values[1]
                if not (isinstance(arena, tuple) and arena[0] == 'ArenaValue'):
                    raise DanhaTypeError("Arena.alloc의 첫 번째 인자는 아레나여야 해", line=line, source=_CURRENT_SOURCE)
                if not isinstance(size, int):
                    raise DanhaTypeError("Arena.alloc의 두 번째 인자는 정수여야 해", line=line, source=_CURRENT_SOURCE)
                if not arena[1]['alive']:
                    raise DanhaRuntimeError("이미 파괴된 아레나야", line=line, source=_CURRENT_SOURCE)
                offset = arena[1]['used']
                if offset + size > arena[1]['capacity']:
                    raise DanhaRuntimeError(
                        f"아레나 용량 초과: {offset + size} > {arena[1]['capacity']}",
                        line=line, source=_CURRENT_SOURCE
                    )
                arena[1]['used'] += size
                return offset
            elif method_name == 'reset':
                if len(arg_values) != 1:
                    raise DanhaValueError("Arena.reset(arena)에는 아레나 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                arena = arg_values[0]
                if not (isinstance(arena, tuple) and arena[0] == 'ArenaValue'):
                    raise DanhaTypeError("Arena.reset의 인자는 아레나여야 해", line=line, source=_CURRENT_SOURCE)
                if not arena[1]['alive']:
                    raise DanhaRuntimeError("이미 파괴된 아레나야", line=line, source=_CURRENT_SOURCE)
                arena[1]['used'] = 0
                return None
            elif method_name == 'destroy':
                if len(arg_values) != 1:
                    raise DanhaValueError("Arena.destroy(arena)에는 아레나 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                arena = arg_values[0]
                if not (isinstance(arena, tuple) and arena[0] == 'ArenaValue'):
                    raise DanhaTypeError("Arena.destroy의 인자는 아레나여야 해", line=line, source=_CURRENT_SOURCE)
                if not arena[1]['alive']:
                    raise DanhaRuntimeError("이미 파괴된 아레나야", line=line, source=_CURRENT_SOURCE)
                arena[1]['alive'] = False
                arena[1]['used'] = 0
                return None
            elif method_name == 'used':
                if len(arg_values) != 1:
                    raise DanhaValueError("Arena.used(arena)에는 아레나 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                arena = arg_values[0]
                if not (isinstance(arena, tuple) and arena[0] == 'ArenaValue'):
                    raise DanhaTypeError("Arena.used의 인자는 아레나여야 해", line=line, source=_CURRENT_SOURCE)
                if not arena[1]['alive']:
                    raise DanhaRuntimeError("이미 파괴된 아레나야", line=line, source=_CURRENT_SOURCE)
                return arena[1]['used']
            elif method_name == 'capacity':
                if len(arg_values) != 1:
                    raise DanhaValueError("Arena.capacity(arena)에는 아레나 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                arena = arg_values[0]
                if not (isinstance(arena, tuple) and arena[0] == 'ArenaValue'):
                    raise DanhaTypeError("Arena.capacity의 인자는 아레나여야 해", line=line, source=_CURRENT_SOURCE)
                if not arena[1]['alive']:
                    raise DanhaRuntimeError("이미 파괴된 아레나야", line=line, source=_CURRENT_SOURCE)
                return arena[1]['capacity']
            else:
                raise DanhaNameError(f"Arena에 '{method_name}'이라는 메서드는 없어", line=line, source=_CURRENT_SOURCE)
        
        # 24a: Arena 인스턴스의 메서드 호출 — arena.reset(), arena.used() 등
        if isinstance(obj, tuple) and obj[0] == 'ArenaValue':
            if method_name == 'alloc':
                # 25a: arena.alloc(size) — 아레나에서 size 바이트 할당, 오프셋 반환
                if len(args) != 1:
                    raise DanhaValueError("arena.alloc(size)에는 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                size = evaluate(args[0], scope)
                if not isinstance(size, int):
                    raise DanhaTypeError("arena.alloc의 인자는 정수여야 해", line=line, source=_CURRENT_SOURCE)
                if not obj[1]['alive']:
                    raise DanhaRuntimeError("이미 파괴된 아레나야", line=line, source=_CURRENT_SOURCE)
                offset = obj[1]['used']
                if offset + size > obj[1]['capacity']:
                    raise DanhaRuntimeError(
                        f"아레나 용량 초과: {offset + size} > {obj[1]['capacity']}",
                        line=line, source=_CURRENT_SOURCE
                    )
                obj[1]['used'] += size
                return offset
            elif method_name == 'reset':
                if len(args) != 0:
                    raise DanhaValueError("arena.reset()은 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
                if not obj[1]['alive']:
                    raise DanhaRuntimeError("이미 파괴된 아레나야", line=line, source=_CURRENT_SOURCE)
                obj[1]['used'] = 0
                return None
            elif method_name == 'destroy':
                if len(args) != 0:
                    raise DanhaValueError("arena.destroy()은 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
                if not obj[1]['alive']:
                    raise DanhaRuntimeError("이미 파괴된 아레나야", line=line, source=_CURRENT_SOURCE)
                obj[1]['alive'] = False
                obj[1]['used'] = 0
                return None
            elif method_name == 'used':
                if len(args) != 0:
                    raise DanhaValueError("arena.used()은 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
                if not obj[1]['alive']:
                    raise DanhaRuntimeError("이미 파괴된 아레나야", line=line, source=_CURRENT_SOURCE)
                return obj[1]['used']
            elif method_name == 'capacity':
                if len(args) != 0:
                    raise DanhaValueError("arena.capacity()은 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
                if not obj[1]['alive']:
                    raise DanhaRuntimeError("이미 파괴된 아레나야", line=line, source=_CURRENT_SOURCE)
                return obj[1]['capacity']
            else:
                raise DanhaNameError(f"아레나에 '{method_name}'이라는 메서드는 없어", line=line, source=_CURRENT_SOURCE)
        
        # 30: HashMap 정적 메서드 — HashMap.new()
        if isinstance(obj, tuple) and obj[0] == 'HashMapType':
            if method_name == 'new':
                if len(args) > 1:
                    raise DanhaValueError("HashMap.new() 또는 HashMap.new(capacity)만 지원해", line=line, source=_CURRENT_SOURCE)
                if len(args) == 1:
                    cap_val = evaluate(args[0], scope)
                    if not isinstance(cap_val, int):
                        raise DanhaTypeError("HashMap.new(capacity)의 capacity는 i32여야 해", line=line, source=_CURRENT_SOURCE)
                return ('HashMapValue', {})
            raise DanhaNameError(f"HashMap에 '{method_name}'이라는 정적 메서드가 없어", line=line, source=_CURRENT_SOURCE)
        
        # 30: HashMap 인스턴스 메서드 — .set/.get/.has/.remove/.len/.keys
        if isinstance(obj, tuple) and obj[0] == 'HashMapValue':
            data = obj[1]
            arg_values = [evaluate(a, scope) for a in args]
            
            if method_name == 'set':
                if len(arg_values) != 2:
                    raise DanhaValueError("set(key, value)에는 인자 2개가 필요해", line=line, source=_CURRENT_SOURCE)
                key = arg_values[0]
                if not isinstance(key, (str, int)):
                    raise DanhaTypeError("HashMap의 키는 문자열(str) 또는 정수(i32)만 돼", line=line, source=_CURRENT_SOURCE)
                data[key] = arg_values[1]
                return None
            
            if method_name in ('get', 'get_i32', 'get_f64', 'get_str', 'get_bool'):
                if len(arg_values) != 1:
                    raise DanhaValueError("get(key)에는 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                key = arg_values[0]
                if key not in data:
                    raise DanhaRuntimeError(f"HashMap에 키 '{key}'가 없어", line=line, source=_CURRENT_SOURCE)
                value = data[key]
                if method_name == 'get_i32':
                    return int(value)
                if method_name == 'get_f64':
                    return float(value)
                if method_name == 'get_str':
                    return str(value)
                if method_name == 'get_bool':
                    return bool(value)
                return value
            
            if method_name == 'has':
                if len(arg_values) != 1:
                    raise DanhaValueError("has(key)에는 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                return arg_values[0] in data
            
            if method_name == 'remove':
                if len(arg_values) != 1:
                    raise DanhaValueError("remove(key)에는 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                key = arg_values[0]
                if key in data:
                    del data[key]
                    return True
                return False
            
            if method_name == 'len':
                if len(arg_values) != 0:
                    raise DanhaValueError("len()에는 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
                return len(data)
            
            if method_name == 'keys':
                if len(arg_values) != 0:
                    raise DanhaValueError("keys()에는 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
                return list(data.keys())
            
            raise DanhaNameError(f"HashMap에 '{method_name}'이라는 메서드가 없어", line=line, source=_CURRENT_SOURCE)
        
        # 31: 배열(리스트) 내장 메서드 — .map(), .filter(), .reduce() 등 함수형 체이닝
        if isinstance(obj, list):
            _LIST_METHODS = {
                'map', 'filter', 'reduce', 'any', 'all', 'find', 'count',
                'sort_by', 'reverse', 'take', 'skip', 'enumerate',
                'flat_map', 'for_each', 'len', 'push', 'contains', 'join',
            }
            if method_name in _LIST_METHODS:
                arg_values = [evaluate(a, scope) for a in args]
                
                if method_name == 'map':
                    if len(arg_values) != 1:
                        raise DanhaValueError("map(fn)에는 함수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    fn = arg_values[0]
                    return [_call_value(fn, [x], scope, line) for x in obj]
                
                if method_name == 'filter':
                    if len(arg_values) != 1:
                        raise DanhaValueError("filter(fn)에는 함수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    fn = arg_values[0]
                    return [x for x in obj if _call_value(fn, [x], scope, line)]
                
                if method_name == 'reduce':
                    if len(arg_values) != 2:
                        raise DanhaValueError("reduce(init, fn)에는 초기값과 함수 인자 2개가 필요해", line=line, source=_CURRENT_SOURCE)
                    acc = arg_values[0]
                    fn = arg_values[1]
                    for x in obj:
                        acc = _call_value(fn, [acc, x], scope, line)
                    return acc
                
                if method_name == 'any':
                    if len(arg_values) != 1:
                        raise DanhaValueError("any(fn)에는 함수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    fn = arg_values[0]
                    for x in obj:
                        if _call_value(fn, [x], scope, line):
                            return True
                    return False
                
                if method_name == 'all':
                    if len(arg_values) != 1:
                        raise DanhaValueError("all(fn)에는 함수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    fn = arg_values[0]
                    for x in obj:
                        if not _call_value(fn, [x], scope, line):
                            return False
                    return True
                
                if method_name == 'find':
                    if len(arg_values) != 1:
                        raise DanhaValueError("find(fn)에는 함수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    fn = arg_values[0]
                    for x in obj:
                        if _call_value(fn, [x], scope, line):
                            return x
                    return None
                
                if method_name == 'count':
                    if len(arg_values) != 1:
                        raise DanhaValueError("count(fn)에는 함수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    fn = arg_values[0]
                    c = 0
                    for x in obj:
                        if _call_value(fn, [x], scope, line):
                            c += 1
                    return c
                
                if method_name == 'sort_by':
                    if len(arg_values) != 1:
                        raise DanhaValueError("sort_by(fn)에는 함수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    fn = arg_values[0]
                    import functools
                    def cmp(a, b):
                        r = _call_value(fn, [a, b], scope, line)
                        if isinstance(r, bool):
                            return -1 if r else 1
                        return -1 if r < 0 else (1 if r > 0 else 0)
                    return sorted(obj, key=functools.cmp_to_key(cmp))
                
                if method_name == 'reverse':
                    if len(arg_values) != 0:
                        raise DanhaValueError("reverse()에는 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
                    return list(reversed(obj))
                
                if method_name == 'take':
                    if len(arg_values) != 1:
                        raise DanhaValueError("take(n)에는 정수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    n = arg_values[0]
                    if not isinstance(n, int):
                        raise DanhaTypeError("take의 인자는 정수여야 해", line=line, source=_CURRENT_SOURCE)
                    return obj[:n]
                
                if method_name == 'skip':
                    if len(arg_values) != 1:
                        raise DanhaValueError("skip(n)에는 정수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    n = arg_values[0]
                    if not isinstance(n, int):
                        raise DanhaTypeError("skip의 인자는 정수여야 해", line=line, source=_CURRENT_SOURCE)
                    return obj[n:]
                
                if method_name == 'enumerate':
                    if len(arg_values) != 0:
                        raise DanhaValueError("enumerate()에는 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
                    return [[i, obj[i]] for i in range(len(obj))]
                
                if method_name == 'flat_map':
                    if len(arg_values) != 1:
                        raise DanhaValueError("flat_map(fn)에는 함수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    fn = arg_values[0]
                    result = []
                    for x in obj:
                        sub = _call_value(fn, [x], scope, line)
                        if isinstance(sub, list):
                            result.extend(sub)
                        else:
                            result.append(sub)
                    return result
                
                if method_name == 'for_each':
                    if len(arg_values) != 1:
                        raise DanhaValueError("for_each(fn)에는 함수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    fn = arg_values[0]
                    for x in obj:
                        _call_value(fn, [x], scope, line)
                    return None
                
                if method_name == 'len':
                    if len(arg_values) != 0:
                        raise DanhaValueError("len()에는 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
                    return len(obj)
                
                if method_name == 'push':
                    if len(arg_values) != 1:
                        raise DanhaValueError("push(value)에는 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    obj.append(arg_values[0])
                    return None
                
                if method_name == 'contains':
                    if len(arg_values) != 1:
                        raise DanhaValueError("contains(value)에는 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    return arg_values[0] in obj
                
                if method_name == 'join':
                    if len(arg_values) != 1 or not isinstance(arg_values[0], str):
                        raise DanhaValueError("join(sep)에는 문자열 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    return arg_values[0].join(format_value(x) if not isinstance(x, str) else x for x in obj)
            
            raise DanhaNameError(f"배열에 '{method_name}'이라는 메서드가 없어", line=line, source=_CURRENT_SOURCE)
        
        # 36a: Result(TaggedEnumValue) 메서드 — .context(), .unwrap(), .unwrap_or() 등
        if isinstance(obj, tuple) and obj[0] == 'TaggedEnumValue':
            variant_name = obj[2]
            payload = obj[4]
            arg_values = [evaluate(a, scope) for a in args]
            
            if method_name == 'context':
                # .context("메시지") → Err이면 에러 메시지에 컨텍스트 추가, Ok이면 그대로
                if len(arg_values) != 1 or not isinstance(arg_values[0], str):
                    raise DanhaValueError("context()에는 문자열 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                ctx_msg = arg_values[0]
                if variant_name == 'Ok':
                    return obj  # Ok은 그대로 통과
                elif variant_name == 'Err':
                    # Err의 payload에 컨텍스트 추가
                    original = payload[0] if payload else ""
                    if isinstance(original, str):
                        new_msg = f"{ctx_msg}: {original}"
                    else:
                        new_msg = f"{ctx_msg}: {format_value(original)}"
                    return ('TaggedEnumValue', obj[1], 'Err', obj[3], [new_msg])
                else:
                    return obj  # Ok/Err 아닌 variant는 그대로
            
            if method_name == 'unwrap':
                # .unwrap() → Ok이면 값 추출, Err이면 패닉
                if variant_name == 'Ok':
                    if len(payload) == 1:
                        return payload[0]
                    elif len(payload) == 0:
                        return None
                    return tuple(payload)
                elif variant_name == 'Err':
                    err_msg = payload[0] if payload else "unknown error"
                    raise DanhaValueError(f"unwrap() 호출했는데 Err: {format_value(err_msg)}", line=line, source=_CURRENT_SOURCE)
                else:
                    raise DanhaTypeError(f"unwrap()은 Ok/Err variant에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)
            
            if method_name == 'unwrap_or':
                # .unwrap_or(default) → Ok이면 값, Err이면 default
                if len(arg_values) != 1:
                    raise DanhaValueError("unwrap_or()에는 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                if variant_name == 'Ok':
                    if len(payload) == 1:
                        return payload[0]
                    elif len(payload) == 0:
                        return None
                    return tuple(payload)
                elif variant_name == 'Err':
                    return arg_values[0]
                else:
                    return arg_values[0]
            
            if method_name == 'is_ok':
                return variant_name == 'Ok'
            
            if method_name == 'is_err':
                return variant_name == 'Err'
            
            if method_name == 'map_err':
                # .map_err(fn(e) { ... }) → Err이면 변환 함수 적용, Ok이면 그대로
                if len(arg_values) != 1:
                    raise DanhaValueError("map_err()에는 함수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                if variant_name == 'Ok':
                    return obj
                elif variant_name == 'Err':
                    transform_fn = arg_values[0]
                    err_val = payload[0] if payload else None
                    new_err = _call_value(transform_fn, [err_val], scope, line)
                    return ('TaggedEnumValue', obj[1], 'Err', obj[3], [new_err])
                else:
                    return obj
        
        # 32: 문자열 내장 메서드 — "hello".split(" "), "abc".to_upper() 등
        if isinstance(obj, str):
            _STR_METHODS = {
                'len', 'split', 'trim', 'starts_with', 'ends_with',
                'replace', 'char_at', 'substr', 'contains', 'to_upper',
                'to_lower', 'index_of', 'repeat', 'reverse',
            }
            if method_name in _STR_METHODS:
                arg_values = [evaluate(a, scope) for a in args]
                
                if method_name == 'len':
                    if len(arg_values) != 0:
                        raise DanhaValueError("len()에는 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
                    return len(obj)
                
                if method_name == 'split':
                    if len(arg_values) != 1 or not isinstance(arg_values[0], str):
                        raise DanhaValueError("split(sep)에는 문자열 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    return obj.split(arg_values[0])
                
                if method_name == 'trim':
                    if len(arg_values) != 0:
                        raise DanhaValueError("trim()에는 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
                    return obj.strip()
                
                if method_name == 'starts_with':
                    if len(arg_values) != 1 or not isinstance(arg_values[0], str):
                        raise DanhaValueError("starts_with(prefix)에는 문자열 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    return obj.startswith(arg_values[0])
                
                if method_name == 'ends_with':
                    if len(arg_values) != 1 or not isinstance(arg_values[0], str):
                        raise DanhaValueError("ends_with(suffix)에는 문자열 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    return obj.endswith(arg_values[0])
                
                if method_name == 'replace':
                    if len(arg_values) != 2 or not all(isinstance(a, str) for a in arg_values):
                        raise DanhaValueError("replace(old, new)에는 문자열 인자 2개가 필요해", line=line, source=_CURRENT_SOURCE)
                    return obj.replace(arg_values[0], arg_values[1])
                
                if method_name == 'char_at':
                    if len(arg_values) != 1 or not isinstance(arg_values[0], int):
                        raise DanhaValueError("char_at(index)에는 정수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    idx = arg_values[0]
                    if idx < 0 or idx >= len(obj):
                        raise DanhaRuntimeError(f"인덱스 {idx}가 문자열 길이 {len(obj)}를 벗어났어", line=line, source=_CURRENT_SOURCE)
                    return obj[idx]
                
                if method_name == 'substr':
                    if len(arg_values) != 2 or not isinstance(arg_values[0], int) or not isinstance(arg_values[1], int):
                        raise DanhaValueError("substr(start, len)에는 정수 인자 2개가 필요해", line=line, source=_CURRENT_SOURCE)
                    return obj[arg_values[0]:arg_values[0] + arg_values[1]]
                
                if method_name == 'contains':
                    if len(arg_values) != 1 or not isinstance(arg_values[0], str):
                        raise DanhaValueError("contains(substr)에는 문자열 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    return arg_values[0] in obj
                
                if method_name == 'to_upper':
                    if len(arg_values) != 0:
                        raise DanhaValueError("to_upper()에는 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
                    return obj.upper()
                
                if method_name == 'to_lower':
                    if len(arg_values) != 0:
                        raise DanhaValueError("to_lower()에는 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
                    return obj.lower()
                
                if method_name == 'index_of':
                    if len(arg_values) != 1 or not isinstance(arg_values[0], str):
                        raise DanhaValueError("index_of(substr)에는 문자열 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    idx = obj.find(arg_values[0])
                    return idx  # 없으면 -1
                
                if method_name == 'repeat':
                    if len(arg_values) != 1 or not isinstance(arg_values[0], int):
                        raise DanhaValueError("repeat(n)에는 정수 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
                    return obj * arg_values[0]
                
                if method_name == 'reverse':
                    if len(arg_values) != 0:
                        raise DanhaValueError("reverse()에는 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
                    return obj[::-1]
            
            raise DanhaNameError(f"문자열에 '{method_name}'이라는 메서드가 없어", line=line, source=_CURRENT_SOURCE)
        
        # 9.1b: 모듈 네임스페이스를 통한 함수 호출 — math.add(1, 2)
        if isinstance(obj, ModuleNamespace):
            try:
                fn = obj.get(method_name)
            except KeyError:
                raise DanhaNameError(f"모듈 '{obj.name}'에 '{method_name}'이 없어", line=line, source=_CURRENT_SOURCE)
            # 모듈 함수를 임시로 현재 스코프에 넣고 Call 노드처럼 실행
            call_node = ('Call', method_name, args, line)
            temp_scope = Scope(parent=scope)
            temp_scope.declare(method_name, fn)
            return evaluate(call_node, temp_scope)
        
        # 8.3: tagged enum variant 생성 — Shape.Circle(5.0)
        if isinstance(obj, tuple) and obj[0] == 'TaggedEnumDef':
            enum_name = obj[1]
            variant_info = obj[2]
            if method_name not in variant_info:
                raise DanhaNameError(f"enum '{enum_name}'에 '{method_name}'이라는 variant가 없어", line=line, source=_CURRENT_SOURCE)
            tag, vtypes = variant_info[method_name]
            if vtypes is None:
                raise DanhaRuntimeError(f"'{method_name}'은(는) 데이터가 없는 variant야 — 괄호 없이 써", line=line, source=_CURRENT_SOURCE)
            if len(args) != len(vtypes):
                raise DanhaRuntimeError(
                    f"'{method_name}'은(는) {len(vtypes)}개의 값이 필요한데 "
                    f"{len(args)}개가 들어왔어",
                    line=line, source=_CURRENT_SOURCE
                )
            values = [evaluate(a, scope) for a in args]
            return ('TaggedEnumValue', enum_name, method_name, tag, values)
        
        # 29단계: TraitObject인 경우 내부 값을 꺼내서 처리
        if isinstance(obj, tuple) and obj[0] == 'TraitObject':
            # TraitObject: ('TraitObject', trait_name, actual_value, actual_type_name)
            obj = obj[2]  # 실제 값으로 언래핑
        
        if not isinstance(obj, tuple) or len(obj) < 1 or obj[0] != 'StructValue':
            raise DanhaRuntimeError("메서드 호출은 구조체에만 할 수 있어", line=line, source=_CURRENT_SOURCE)
        
        type_name = obj[1]
        
        # 스코프에서 구조체 정의를 찾는다
        if not scope.has(type_name):
            raise DanhaTypeError(f"{type_name} 타입을 찾을 수 없어", line=line, source=_CURRENT_SOURCE)
        
        struct_def = scope.get(type_name)
        # StructDef: ('StructDef', fields, methods)  → methods at [2]
        # UnionDef:  ('UnionDef',  fields, methods)  → methods at [2]
        # ComponentDef: ('ComponentDef', name, fields, methods)  → methods at [3]
        if struct_def[0] in ('StructDef', 'UnionDef'):
            struct_methods = struct_def[2]
        elif struct_def[0] == 'ComponentDef':
            struct_methods = struct_def[3]
        else:
            raise DanhaTypeError(f"{type_name}은(는) 구조체/union이 아니야", line=line, source=_CURRENT_SOURCE)
        
        if method_name not in struct_methods:
            fields = obj[2]
            if method_name in fields:
                field_value = fields[method_name]
                if isinstance(field_value, tuple) and field_value[0] == 'Function':
                    arg_values = [evaluate(arg, scope) for arg in args]
                    return _call_value(field_value, arg_values, scope, line)
            raise DanhaNameError(f"{type_name}에 '{method_name}' 메서드가 없어", line=line, source=_CURRENT_SOURCE)
        
        method = struct_methods[method_name]
        method_params = method[1]  # self 포함한 전체 매개변수
        method_body = method[2]
        method_defining_scope = method[3]
        
        # 인자 개수 검사: method_params[0]은 self, 나머지가 실제 호출 인자에 대응
        expected_arg_count = len(method_params) - 1  # self 제외
        if len(args) != expected_arg_count:
            raise DanhaValueError(
                f"{type_name}.{method_name}은(는) {expected_arg_count}개의 인자가 필요한데 {len(args)}개가 들어왔어",
                line=line, source=_CURRENT_SOURCE
            )
        
        # 인자를 호출한 쪽 스코프에서 평가
        arg_values = [evaluate(arg, scope) for arg in args]
        
        # 메서드 본체 실행: 정의된 곳의 스코프를 부모로, self에 obj 바인딩
        method_scope = Scope(parent=method_defining_scope)
        method_scope.declare('self', obj)
        for param_name, arg_value in zip(method_params[1:], arg_values):
            method_scope.declare(param_name, arg_value)
        
        try:
            result = evaluate(method_body, method_scope)
        except ReturnValue as rv:
            result = rv.value
        # 7.15d: 메서드 경계도 함수 경계와 동일 — break/continue 못 넘음
        except BreakSignal:
            raise DanhaRuntimeError(f"메서드 '{type_name}.{method_name}' 안의 'break'가 루프 바깥까지 나왔어", line=line, source=_CURRENT_SOURCE)
        except ContinueSignal:
            raise DanhaRuntimeError(f"메서드 '{type_name}.{method_name}' 안의 'continue'가 루프 바깥까지 나왔어", line=line, source=_CURRENT_SOURCE)
        
        return result
    
    # 인덱스 쓰기: arr[i] = v
    if node_type == 'IndexAssign':
        obj = evaluate(node[1], scope)
        idx = evaluate(node[2], scope)
        new_value = evaluate(node[3], scope)
        line = node[-1]
        if not isinstance(obj, list):
            raise DanhaValueError("인덱스 쓰기는 리스트에만 할 수 있어", line=line, source=_CURRENT_SOURCE)
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise DanhaValueError("인덱스는 정수여야 해", line=line, source=_CURRENT_SOURCE)
        if idx < 0 or idx >= len(obj):
            raise DanhaValueError(f"인덱스 범위 밖이야: {idx} (리스트 길이 {len(obj)})", line=line, source=_CURRENT_SOURCE)
        obj[idx] = new_value
        return None
    
    # 변수 대입: 체인에서 이미 있는 걸 찾아 수정하고, 없으면 현재 스코프에 새로 만듦
    if node_type == 'Assign':
        name = node[1]
        value = evaluate(node[2], scope)
        if not scope.set_existing(name, value):
            scope.declare(name, value)
        return None
    
    # 필드 쓰기: 객체의 필드에 새 값을 넣는다
    if node_type == 'FieldAssign':
        obj = evaluate(node[1], scope)
        field_name = node[2]
        new_value = evaluate(node[3], scope)
        line = node[-1]
        
        # 7.13: system for each 프록시 — SoA에 직접 쓰기
        if isinstance(obj, _SystemComponentProxy):
            if not obj.has_field(field_name):
                raise DanhaNameError(f"{obj.comp_name}에 '{field_name}'이라는 필드가 없어", line=line, source=_CURRENT_SOURCE)
            obj.set_field(field_name, new_value)
            return None
        
        # 7.7: 벡터 필드 수정 (pos.x = 5.0)
        if isinstance(obj, tuple) and len(obj) >= 1 and obj[0] == 'VecValue':
            vec_type = obj[1]
            components = obj[2]
            field_map = _vec_field_map(vec_type)
            if field_name not in field_map:
                raise DanhaNameError(f"{vec_type}에 '{field_name}'이라는 필드가 없어", line=line, source=_CURRENT_SOURCE)
            # 값을 float으로 변환 (벡터 성분은 항상 실수)
            if isinstance(new_value, int) and not isinstance(new_value, bool):
                new_value = float(new_value)
            components[field_map[field_name]] = new_value
            return None
        
        if not isinstance(obj, tuple) or len(obj) < 1 or obj[0] != 'StructValue':
            raise DanhaRuntimeError("필드 쓰기는 구조체나 벡터에만 할 수 있어", line=line, source=_CURRENT_SOURCE)

        type_name = obj[1]
        fields = obj[2]

        if field_name not in fields:
            raise DanhaNameError(f"{type_name}에 '{field_name}'이라는 필드가 없어", line=line, source=_CURRENT_SOURCE)

        # 배열 타입 필드에 [] 대입 시 올바른 크기로 자동 확장
        if isinstance(new_value, list) and len(new_value) == 0 and scope.has(type_name):
            struct_def = scope.get(type_name)
            if isinstance(struct_def, tuple) and struct_def[0] in ('StructDef', 'UnionDef'):
                type_fields = struct_def[1]
                ftype_map = {f[0]: f[1] for f in type_fields}
                ftype = ftype_map.get(field_name)
                if (ftype is not None and isinstance(ftype, tuple) and ftype[0] == 'ArrayType'):
                    count = ftype[2]
                    elem_node = ftype[1]
                    elem_kind = elem_node[0] if isinstance(elem_node, tuple) else ''
                    elem_name = elem_node[1] if (isinstance(elem_node, tuple) and len(elem_node) > 1) else ''
                    if elem_kind == 'TypeName' and elem_name in ('i32', 'i16', 'i64', 'i8'):
                        default = 0
                    elif elem_kind == 'TypeName' and elem_name in ('f64', 'f32'):
                        default = 0.0
                    elif elem_kind == 'TypeName' and elem_name == 'bool':
                        default = False
                    else:
                        default = None
                    new_value = [default] * count

        fields[field_name] = new_value
        return None
    
    if node_type == 'Print':
        value = evaluate(node[1], scope)
        print(format_value(value))
        return None
    
    if node_type == 'Name':
        name = node[1]
        line = node[-1]
        # 24a: Arena는 언어 내장 타입 — 스코프에 없어도 접근 가능.
        if name == 'Arena':
            return ('ArenaType',)
        # 30: HashMap 내장 타입
        if name == 'HashMap':
            return ('HashMapType',)
        if not scope.has(name):
            raise DanhaNameError(f"정의되지 않은 이름이야: {name}", line=line, source=_CURRENT_SOURCE)
        return scope.get(name)
    
    if node_type == 'Number':
        return node[1]
    
    if node_type == 'Bool':
        return node[1]
    
    if node_type == 'Null':
        return None
    
    if node_type == 'String':
        return node[1]
    
    # 17: 문자열 보간 — "hello {name}, age {age}"
    # 각 파트를 평가하고, 문자열이 아니면 자동 변환 후 결합.
    if node_type == 'InterpString':
        parts = node[1]
        result_parts = []
        for part in parts:
            val = evaluate(part, scope)
            # 값을 문자열로 변환
            if isinstance(val, str):
                result_parts.append(val)
            elif isinstance(val, bool):
                result_parts.append("true" if val else "false")
            elif isinstance(val, int):
                result_parts.append(str(val))
            elif isinstance(val, float):
                result_parts.append(str(val))
            else:
                result_parts.append(str(val))
        return ''.join(result_parts)
    
    # 리스트 리터럴: 각 원소를 평가해서 파이썬 리스트로
    if node_type == 'List':
        return [evaluate(el, scope) for el in node[1]]
    
    # 범위 식: start..end (끝값 미포함). 6.9에서 추가.
    # 평가하면 파이썬 range를 돌려준다 — for 안에서 그대로 순회 가능.
    if node_type == 'Range':
        start = evaluate(node[1], scope)
        end = evaluate(node[2], scope)
        line = node[-1]
        if not isinstance(start, int) or isinstance(start, bool):
            raise DanhaValueError("범위의 시작은 정수여야 해", line=line, source=_CURRENT_SOURCE)
        if not isinstance(end, int) or isinstance(end, bool):
            raise DanhaValueError("범위의 끝은 정수여야 해", line=line, source=_CURRENT_SOURCE)
        return range(start, end)
    
    # 인덱스 읽기: arr[i]
    # 주의: 지금은 리스트에만 동작. 나중에 문자열/맵도 지원할 수 있음.
    if node_type == 'Index':
        obj = evaluate(node[1], scope)
        idx = evaluate(node[2], scope)
        line = node[-1]
        if not isinstance(obj, (list, str)):
            raise DanhaValueError("인덱스 접근은 리스트에만 또는 문자열에만 할 수 있어", line=line, source=_CURRENT_SOURCE)
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise DanhaValueError("인덱스는 정수여야 해", line=line, source=_CURRENT_SOURCE)
        if idx < 0 or idx >= len(obj):
            raise DanhaValueError(f"인덱스 범위 밖이야: {idx} (길이 {len(obj)})", line=line, source=_CURRENT_SOURCE)
        return obj[idx]
    
    # 사칙연산
    if node_type == 'Add':
        left = evaluate(node[1], scope)
        right = evaluate(node[2], scope)
        if isinstance(left, bool) or isinstance(right, bool):
            raise DanhaTypeError(f"bool은 산술 연산에 쓸 수 없어", line=node[-1], source=_CURRENT_SOURCE)
        # 7.7: 벡터 덧셈
        vec_result = _vec_binop(left, right, lambda a, b: a + b, node[-1])
        if vec_result is not None:
            return vec_result
        # 문자열 + 문자열 = 이어붙이기. 혼합은 금지.
        if isinstance(left, str) or isinstance(right, str):
            if isinstance(left, str) and isinstance(right, str):
                return left + right
            raise DanhaTypeError(
                f"문자열과 숫자를 + 로 합칠 수 없어. "
                f"문자열끼리만 이어붙일 수 있어.",
                line=node[-1], source=_CURRENT_SOURCE
            )
        return left + right
    
    if node_type == 'Sub':
        left = evaluate(node[1], scope)
        right = evaluate(node[2], scope)
        if isinstance(left, bool) or isinstance(right, bool):
            raise DanhaTypeError(f"bool은 산술 연산에 쓸 수 없어", line=node[-1], source=_CURRENT_SOURCE)
        # 7.7: 벡터 뺄셈
        vec_result = _vec_binop(left, right, lambda a, b: a - b, node[-1])
        if vec_result is not None:
            return vec_result
        return left - right
    
    if node_type == 'Mul':
        left = evaluate(node[1], scope)
        right = evaluate(node[2], scope)
        if isinstance(left, bool) or isinstance(right, bool):
            raise DanhaTypeError(f"bool은 산술 연산에 쓸 수 없어", line=node[-1], source=_CURRENT_SOURCE)
        # 7.9a: mat4 * vec4 → vec4 (행렬 변환)
        if _is_mat(left) and _is_vec(right) and right[1] == 'vec4':
            result = _mat4_mul_vec4(left, right[2])
            return ('VecValue', 'vec4', result)
        # 7.9a: mat4 * mat4 → mat4 (변환 합성)
        if _is_mat(left) and _is_mat(right):
            result = _mat4_mul_mat4(left, right)
            return ('MatValue', 'mat4', result)
        # 7.7: 벡터 곱셈 (성분별 또는 스칼라 곱)
        vec_result = _vec_binop(left, right, lambda a, b: a * b, node[-1])
        if vec_result is not None:
            return vec_result
        return left * right
    
    if node_type == 'Div':
        left = evaluate(node[1], scope)
        right = evaluate(node[2], scope)
        if isinstance(left, bool) or isinstance(right, bool):
            raise DanhaTypeError(f"bool은 산술 연산에 쓸 수 없어", line=node[-1], source=_CURRENT_SOURCE)
        # 7.7: 벡터 나눗셈
        if _is_vec(left) or _is_vec(right):
            vec_result = _vec_binop(left, right, lambda a, b: a / b, node[-1])
            if vec_result is not None:
                return vec_result
        if right == 0:
            raise DanhaValueError(f"0으로 나눌 수 없어", line=node[-1], source=_CURRENT_SOURCE)
        # C 계열 규칙: 양쪽이 모두 정수면 정수 몫, 하나라도 실수면 실수 나눗셈
        if isinstance(left, int) and isinstance(right, int):
            return int(left / right)
        return left / right
    
    if node_type == 'Mod':
        left = evaluate(node[1], scope)
        right = evaluate(node[2], scope)
        if isinstance(left, bool) or isinstance(right, bool):
            raise DanhaTypeError(f"bool은 산술 연산에 쓸 수 없어", line=node[-1], source=_CURRENT_SOURCE)
        if right == 0:
            raise DanhaValueError(f"0으로 나머지 연산을 할 수 없어", line=node[-1], source=_CURRENT_SOURCE)
        return left % right
    
    # 비트 연산
    if node_type == 'BitAnd':
        left = evaluate(node[1], scope)
        right = evaluate(node[2], scope)
        if not isinstance(left, int) or not isinstance(right, int) or isinstance(left, bool) or isinstance(right, bool):
            raise DanhaTypeError("'&'는 정수에만 쓸 수 있어", line=node[-1], source=_CURRENT_SOURCE)
        return left & right

    if node_type == 'BitOr':
        left = evaluate(node[1], scope)
        right = evaluate(node[2], scope)
        if not isinstance(left, int) or not isinstance(right, int) or isinstance(left, bool) or isinstance(right, bool):
            raise DanhaTypeError("'|'는 정수에만 쓸 수 있어", line=node[-1], source=_CURRENT_SOURCE)
        return left | right

    if node_type == 'BitXor':
        left = evaluate(node[1], scope)
        right = evaluate(node[2], scope)
        if not isinstance(left, int) or not isinstance(right, int) or isinstance(left, bool) or isinstance(right, bool):
            raise DanhaTypeError("'^'는 정수에만 쓸 수 있어", line=node[-1], source=_CURRENT_SOURCE)
        return left ^ right

    if node_type == 'LShift':
        left = evaluate(node[1], scope)
        right = evaluate(node[2], scope)
        if not isinstance(left, int) or not isinstance(right, int) or isinstance(left, bool) or isinstance(right, bool):
            raise DanhaTypeError("'<<'는 정수에만 쓸 수 있어", line=node[-1], source=_CURRENT_SOURCE)
        if right < 0:
            raise DanhaValueError("시프트 횟수는 0 이상이어야 해", line=node[-1], source=_CURRENT_SOURCE)
        return left << right

    if node_type == 'RShift':
        left = evaluate(node[1], scope)
        right = evaluate(node[2], scope)
        if not isinstance(left, int) or not isinstance(right, int) or isinstance(left, bool) or isinstance(right, bool):
            raise DanhaTypeError("'>>'는 정수에만 쓸 수 있어", line=node[-1], source=_CURRENT_SOURCE)
        if right < 0:
            raise DanhaValueError("시프트 횟수는 0 이상이어야 해", line=node[-1], source=_CURRENT_SOURCE)
        return left >> right

    # 42단계: @sizeof(Type) / @alignof(Type) — 타입의 바이트 크기/정렬
    _EVAL_TYPE_SIZES = {
        'u8': 1, 'i8': 1, 'u16': 2, 'i16': 2,
        'u32': 4, 'i32': 4, 'f32': 4,
        'u64': 8, 'i64': 8, 'f64': 8,
        'bool': 1, 'ptr': 8,
        'vec2': 16, 'vec3': 24, 'vec4': 32, 'mat4': 128,
    }
    if node_type == 'SizeOf':
        type_node = node[1]
        line = node[-1]
        if type_node[0] == 'TypeName' and type_node[1] in _EVAL_TYPE_SIZES:
            return _EVAL_TYPE_SIZES[type_node[1]]
        raise DanhaTypeError(
            f"@sizeof: '{type_node[1] if type_node[0] == 'TypeName' else type_node}'은(는) 지원 안 하는 타입이야",
            line=line, source=_CURRENT_SOURCE
        )

    if node_type == 'AlignOf':
        type_node = node[1]
        line = node[-1]
        if type_node[0] == 'TypeName' and type_node[1] in _EVAL_TYPE_SIZES:
            return min(_EVAL_TYPE_SIZES[type_node[1]], 8)
        raise DanhaTypeError(
            f"@alignof: '{type_node[1] if type_node[0] == 'TypeName' else type_node}'은(는) 지원 안 하는 타입이야",
            line=line, source=_CURRENT_SOURCE
        )

    # 45단계: @sin, @cos, @sqrt, @abs, @floor, @ceil, @pow, @atan2, @min, @max
    if node_type == 'MathIntrinsic':
        import math as _math
        fn_name = node[1]
        args = [evaluate(a, scope) for a in node[2]]
        _MATH_FNS = {
            'sin':   lambda a: _math.sin(float(a[0])),
            'cos':   lambda a: _math.cos(float(a[0])),
            'tan':   lambda a: _math.tan(float(a[0])),
            'sqrt':  lambda a: _math.sqrt(float(a[0])),
            'floor': lambda a: int(_math.floor(float(a[0]))),
            'ceil':  lambda a: int(_math.ceil(float(a[0]))),
            'round': lambda a: round(float(a[0])),
            'log':   lambda a: _math.log(float(a[0])),
            'exp':   lambda a: _math.exp(float(a[0])),
            'pow':   lambda a: float(a[0]) ** float(a[1]),
            'atan2': lambda a: _math.atan2(float(a[0]), float(a[1])),
            'hypot': lambda a: _math.hypot(float(a[0]), float(a[1])),
            'min':   lambda a: a[0] if a[0] <= a[1] else a[1],
            'max':   lambda a: a[0] if a[0] >= a[1] else a[1],
            'abs':   lambda a: abs(a[0]),
        }
        return _MATH_FNS[fn_name](args)

    if node_type == 'BitNot':
        val = evaluate(node[1], scope)
        if not isinstance(val, int) or isinstance(val, bool):
            raise DanhaTypeError("'~'는 정수에만 쓸 수 있어", line=node[-1], source=_CURRENT_SOURCE)
        return ~val

    # 비교 연산
    if node_type == 'Gt':
        return evaluate(node[1], scope) > evaluate(node[2], scope)
    
    if node_type == 'Lt':
        return evaluate(node[1], scope) < evaluate(node[2], scope)
    
    if node_type == 'Gte':
        return evaluate(node[1], scope) >= evaluate(node[2], scope)
    
    if node_type == 'Lte':
        return evaluate(node[1], scope) <= evaluate(node[2], scope)
    
    if node_type == 'Eq':
        return values_equal(evaluate(node[1], scope), evaluate(node[2], scope))
    
    if node_type == 'Neq':
        return not values_equal(evaluate(node[1], scope), evaluate(node[2], scope))
    
    # 논리 연산 (단락 평가)
    if node_type == 'And':
        left = evaluate(node[1], scope)
        if not left:
            return left
        return evaluate(node[2], scope)
    
    if node_type == 'Or':
        left = evaluate(node[1], scope)
        if left:
            return left
        return evaluate(node[2], scope)
    
    if node_type == 'Not':
        return not evaluate(node[1], scope)
    
    if node_type == 'Neg':
        val = evaluate(node[1], scope)
        if isinstance(val, bool):
            raise DanhaTypeError(f"bool은 산술 연산에 쓸 수 없어", line=node[-1], source=_CURRENT_SOURCE)
        # 7.7: 벡터 부호 반전 (-vec3(1,2,3) → vec3(-1,-2,-3))
        if _is_vec(val):
            result = [-float(c) for c in val[2]]
            return ('VecValue', val[1], result)
        return -val
    
    # 29단계: dyn 캐스팅 — expr as dyn Trait
    if node_type == 'DynCast':
        val = evaluate(node[1], scope)
        trait_name = node[2]
        line = node[-1]
        
        # 값의 타입 확인
        if not isinstance(val, tuple) or val[0] != 'StructValue':
            raise DanhaTypeError(
                f"'as dyn {trait_name}'은(는) 구조체 값에만 쓸 수 있어",
                line=line, source=_CURRENT_SOURCE
            )
        actual_type_name = val[1]
        
        # 트레잇이 정의되어 있는지 확인
        if not scope.has(trait_name):
            raise DanhaNameError(f"정의되지 않은 트레잇이야: {trait_name}", line=line, source=_CURRENT_SOURCE)
        trait_def = scope.get(trait_name)
        if not isinstance(trait_def, tuple) or trait_def[0] != 'TraitDef':
            raise DanhaTypeError(f"{trait_name}은(는) 트레잇이 아니야", line=line, source=_CURRENT_SOURCE)
        
        # 해당 타입이 트레잇을 구현했는지 확인
        # (impl Trait for Type으로 메서드가 등록되어 있으면 됨)
        type_def = scope.get(actual_type_name)
        if type_def[0] in ('StructDef', 'UnionDef'):
            type_methods = type_def[2]
        elif type_def[0] == 'ComponentDef':
            type_methods = type_def[3]
        else:
            raise DanhaTypeError(f"{actual_type_name}은(는) 구조체/union이 아니야", line=line, source=_CURRENT_SOURCE)
        
        trait_methods = trait_def[2]
        for mname in trait_methods:
            if mname not in type_methods:
                raise DanhaTypeError(
                    f"{actual_type_name}이(가) {trait_name} 트레잇의 '{mname}' 메서드를 구현하지 않았어",
                    line=line, source=_CURRENT_SOURCE
                )
        
        # TraitObject 생성
        return ('TraitObject', trait_name, val, actual_type_name)
    
    # 16: 타입 캐스팅 — expr as type
    if node_type == 'Cast':
        val = evaluate(node[1], scope)
        target = node[2]  # 타입 이름 문자열: "i32", "f64", "str", "bool"
        line = node[-1]
        
        if target == 'i32':
            if isinstance(val, bool):
                return 1 if val else 0
            if isinstance(val, int):
                return val
            if isinstance(val, float):
                return int(val)  # 0 방향 버림 (C 규칙)
            if isinstance(val, str):
                try:
                    return int(val)
                except ValueError:
                    raise DanhaValueError(f"문자열 \"{val}\"을(를) i32로 변환할 수 없어", line=line, source=_CURRENT_SOURCE)
            raise DanhaTypeError(f"{type(val).__name__}을(를) i32로 변환할 수 없어", line=line, source=_CURRENT_SOURCE)
        
        elif target == 'f64':
            if isinstance(val, bool):
                return 1.0 if val else 0.0
            if isinstance(val, int):
                return float(val)
            if isinstance(val, float):
                return val
            if isinstance(val, str):
                try:
                    return float(val)
                except ValueError:
                    raise DanhaValueError(f"문자열 \"{val}\"을(를) f64로 변환할 수 없어", line=line, source=_CURRENT_SOURCE)
            raise DanhaTypeError(f"{type(val).__name__}을(를) f64로 변환할 수 없어", line=line, source=_CURRENT_SOURCE)
        
        elif target == 'str':
            if isinstance(val, bool):
                return "true" if val else "false"
            if isinstance(val, int):
                return str(val)
            if isinstance(val, float):
                return str(val)
            if isinstance(val, str):
                return val
            raise DanhaTypeError(f"{type(val).__name__}을(를) str로 변환할 수 없어", line=line, source=_CURRENT_SOURCE)
        
        elif target == 'bool':
            if isinstance(val, bool):
                return val
            if isinstance(val, int):
                return val != 0
            if isinstance(val, float):
                return val != 0.0
            if isinstance(val, str):
                return len(val) > 0
            raise DanhaTypeError(f"{type(val).__name__}을(를) bool로 변환할 수 없어", line=line, source=_CURRENT_SOURCE)
        
        elif target in ('i8', 'i16', 'i32', 'i64', 'u8', 'u16', 'u32', 'u64'):
            _BITS = {'i8': 8, 'u8': 8, 'i16': 16, 'u16': 16,
                     'i32': 32, 'u32': 32, 'i64': 64, 'u64': 64}
            _SIGNED = {'i8', 'i16', 'i32', 'i64'}
            bits = _BITS[target]
            mask = (1 << bits) - 1
            if isinstance(val, bool):
                val = 1 if val else 0
            elif isinstance(val, float):
                val = int(val)
            if not isinstance(val, int):
                raise DanhaTypeError(f"{type(val).__name__}을(를) {target}로 변환할 수 없어", line=line, source=_CURRENT_SOURCE)
            truncated = val & mask
            if target in _SIGNED and truncated >= (1 << (bits - 1)):
                truncated -= (1 << bits)
            return truncated

        else:
            raise DanhaTypeError(f"'{target}'은(는) 캐스팅 대상 타입이 아니야 (i8~u64, f64, str, bool)", line=line, source=_CURRENT_SOURCE)
    
    # 7.1.3: 참조 식 '&x' / '&mut x'
    # 인터프리터에선 의미상 값과 같음. 참조·복사 구분은 의미 일치 정책상
    # 7단계 후반 ECS에서 제대로 맞춘다. 지금은 파서가 받는 걸 인터프리터가
    # 터지지 않고 실행만 해주면 됨. 컴파일러 쪽에서 진짜 참조 의미를 보장.
    if node_type == 'AddrOf':
        # & (참조)는 안전한 연산. unsafe가 필요한 건 포인터 산술(정수↔포인터)뿐.
        return evaluate(node[2], scope)
    
    raise DanhaRuntimeError(f"모르는 노드 종류야: {node_type}", source=_CURRENT_SOURCE)


# ===== 내장 함수 =====
# 모든 내장 함수는 (line, args) -> value 시그니처를 가진다.
# line은 호출된 위치 (에러 메시지용), args는 평가된 인자 리스트.

BUILTINS = {}


def register_builtin(name, fn):
    BUILTINS[name] = ('Builtin', fn, name)


def _builtin_len(line, args):
    if len(args) != 1:
        raise DanhaValueError(f"len은 1개의 인자가 필요한데 {len(args)}개가 들어왔어", line=line, source=_CURRENT_SOURCE)
    val = args[0]
    if isinstance(val, list):
        return len(val)
    if isinstance(val, str):
        return len(val)
    raise DanhaTypeError("len은 리스트나 문자열에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)


def _builtin_push(line, args):
    if len(args) != 2:
        raise DanhaValueError(f"push는 2개의 인자가 필요한데 {len(args)}개가 들어왔어", line=line, source=_CURRENT_SOURCE)
    lst, val = args
    if not isinstance(lst, list):
        raise DanhaValueError("push의 첫 인자는 리스트여야 해", line=line, source=_CURRENT_SOURCE)
    lst.append(val)
    return None


def _builtin_to_string(line, args):
    if len(args) != 1:
        raise DanhaValueError(f"to_string은 1개의 인자가 필요한데 {len(args)}개가 들어왔어", line=line, source=_CURRENT_SOURCE)
    # format_value와 같은 방식으로 변환.
    # 구조체/리스트도 출력 형식 그대로.
    return format_value(args[0])


def _builtin_arena_reset(line, args):
    """7.5: 아레나 리셋. 인터프리터에서는 파이썬이 메모리를 관리하므로 아무것도 안 함.
    컴파일러에서는 아레나의 offset을 0으로 되돌려 메모리를 통째로 '비운다'."""
    if len(args) != 0:
        raise DanhaNameError("arena_reset은 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
    return None


def _builtin_pop(line, args):
    """자유 함수 pop(arr) — 마지막 원소를 떼서 돌려줌. 비어 있으면 0."""
    if len(args) != 1 or not isinstance(args[0], list):
        raise DanhaValueError("pop()은 리스트 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
    if not args[0]:
        return 0
    return args[0].pop()


def _builtin_exit(line, args):
    """프로세스 즉시 종료. exit(code)"""
    code = int(args[0]) if args else 0
    raise SystemExit(code)


register_builtin('exit', _builtin_exit)
register_builtin('len', _builtin_len)
register_builtin('push', _builtin_push)
register_builtin('pop', _builtin_pop)
register_builtin('to_string', _builtin_to_string)
register_builtin('arena_reset', _builtin_arena_reset)


def _builtin_ord(line, args):
    if len(args) != 1 or not isinstance(args[0], str) or len(args[0]) != 1:
        raise DanhaValueError("ord은 문자 1개짜리 문자열이 필요해", line=line, source=_CURRENT_SOURCE)
    return ord(args[0])

def _builtin_chr(line, args):
    if len(args) != 1 or not isinstance(args[0], int):
        raise DanhaValueError("chr은 정수 1개가 필요해", line=line, source=_CURRENT_SOURCE)
    return chr(args[0])

register_builtin('ord', _builtin_ord)
register_builtin('chr', _builtin_chr)


# ===== 19: 수학 + 유틸리티 내장 함수 =====

import math as _math
import random as _random

def _builtin_abs(line, args):
    if len(args) != 1:
        raise DanhaValueError("abs는 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    v = args[0]
    if isinstance(v, (int, float)):
        return abs(v)
    raise DanhaTypeError("abs는 숫자에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

def _builtin_sqrt(line, args):
    if len(args) != 1:
        raise DanhaValueError("sqrt는 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    v = args[0]
    if isinstance(v, int):
        v = float(v)
    if isinstance(v, float):
        if v < 0:
            raise DanhaValueError("sqrt에 음수를 넣을 수 없어", line=line, source=_CURRENT_SOURCE)
        return _math.sqrt(v)
    raise DanhaTypeError("sqrt는 숫자에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

def _builtin_floor(line, args):
    if len(args) != 1:
        raise DanhaValueError("floor는 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    v = args[0]
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(_math.floor(v))
    raise DanhaTypeError("floor는 숫자에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

def _builtin_ceil(line, args):
    if len(args) != 1:
        raise DanhaValueError("ceil은 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    v = args[0]
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(_math.ceil(v))
    raise DanhaTypeError("ceil은 숫자에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

def _builtin_round(line, args):
    if len(args) != 1:
        raise DanhaValueError("round는 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    v = args[0]
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(round(v))
    raise DanhaTypeError("round는 숫자에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

def _builtin_min(line, args):
    if len(args) != 2:
        raise DanhaValueError("min은 2개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    a, b = args
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return min(a, b)
    raise DanhaTypeError("min은 숫자에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

def _builtin_max(line, args):
    if len(args) != 2:
        raise DanhaValueError("max는 2개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    a, b = args
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return max(a, b)
    raise DanhaTypeError("max는 숫자에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

def _builtin_pow(line, args):
    if len(args) != 2:
        raise DanhaValueError("pow는 2개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    base, exp = args
    if isinstance(base, (int, float)) and isinstance(exp, (int, float)):
        result = base ** exp
        if isinstance(base, int) and isinstance(exp, int) and exp >= 0:
            return int(result)
        return float(result)
    raise DanhaTypeError("pow는 숫자에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

def _builtin_sin(line, args):
    if len(args) != 1:
        raise DanhaValueError("sin은 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    v = args[0]
    if isinstance(v, int):
        v = float(v)
    if isinstance(v, float):
        return _math.sin(v)
    raise DanhaTypeError("sin은 숫자에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

def _builtin_cos(line, args):
    if len(args) != 1:
        raise DanhaValueError("cos는 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    v = args[0]
    if isinstance(v, int):
        v = float(v)
    if isinstance(v, float):
        return _math.cos(v)
    raise DanhaTypeError("cos는 숫자에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

def _builtin_tan(line, args):
    if len(args) != 1:
        raise DanhaValueError("tan은 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    v = args[0]
    if isinstance(v, int):
        v = float(v)
    if isinstance(v, float):
        return _math.tan(v)
    raise DanhaTypeError("tan은 숫자에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

def _builtin_atan2(line, args):
    if len(args) != 2:
        raise DanhaValueError("atan2는 2개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    y, x = args
    if isinstance(y, int):
        y = float(y)
    if isinstance(x, int):
        x = float(x)
    if isinstance(y, float) and isinstance(x, float):
        return _math.atan2(y, x)
    raise DanhaTypeError("atan2는 숫자에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

def _builtin_random(line, args):
    """random() → 0.0 이상 1.0 미만 랜덤 실수"""
    if len(args) != 0:
        raise DanhaValueError("random은 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
    return _random.random()

def _builtin_random_int(line, args):
    """random_int(min, max) → min 이상 max 이하 랜덤 정수"""
    if len(args) != 2:
        raise DanhaValueError("random_int는 2개의 인자가 필요해 (min, max)", line=line, source=_CURRENT_SOURCE)
    lo, hi = args
    if isinstance(lo, int) and isinstance(hi, int):
        return _random.randint(lo, hi)
    raise DanhaTypeError("random_int는 정수에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

def _builtin_clamp(line, args):
    if len(args) != 3:
        raise DanhaValueError("clamp은 3개의 인자가 필요해 (val, min, max)", line=line, source=_CURRENT_SOURCE)
    val, lo, hi = args
    if isinstance(val, (int, float)) and isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        return max(lo, min(val, hi))
    raise DanhaTypeError("clamp은 숫자에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

def _builtin_lerp(line, args):
    """lerp(a, b, t) → a + (b - a) * t — 선형 보간"""
    if len(args) != 3:
        raise DanhaValueError("lerp은 3개의 인자가 필요해 (a, b, t)", line=line, source=_CURRENT_SOURCE)
    a, b, t = args
    if isinstance(a, int):
        a = float(a)
    if isinstance(b, int):
        b = float(b)
    if isinstance(t, int):
        t = float(t)
    if isinstance(a, float) and isinstance(b, float) and isinstance(t, float):
        return a + (b - a) * t
    raise DanhaTypeError("lerp은 숫자에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)

register_builtin('abs', _builtin_abs)
register_builtin('sqrt', _builtin_sqrt)
register_builtin('floor', _builtin_floor)
register_builtin('ceil', _builtin_ceil)
register_builtin('round', _builtin_round)
register_builtin('min', _builtin_min)
register_builtin('max', _builtin_max)
register_builtin('pow', _builtin_pow)
register_builtin('sin', _builtin_sin)
register_builtin('cos', _builtin_cos)
register_builtin('tan', _builtin_tan)
register_builtin('atan2', _builtin_atan2)
register_builtin('random', _builtin_random)
register_builtin('random_int', _builtin_random_int)
register_builtin('clamp', _builtin_clamp)
register_builtin('lerp', _builtin_lerp)


# 7.7: 벡터 생성 내장 함수
def _make_vec_builtin(vec_type, size):
    """vec2/vec3/vec4 내장 함수를 만드는 팩토리.
    
    vec3(1.0, 2.0, 3.0) 같은 호출을 ('VecValue', 'vec3', [1.0, 2.0, 3.0])으로.
    인자는 숫자(int 또는 float)만 허용. int는 자동으로 float으로 변환.
    """
    def builtin_fn(line, args):
        if len(args) != size:
            raise DanhaValueError(
                f"{vec_type}은(는) {size}개의 인자가 필요한데 {len(args)}개가 들어왔어",
                line=line, source=_CURRENT_SOURCE
            )
        components = []
        for i, a in enumerate(args):
            if isinstance(a, bool):
                raise DanhaRuntimeError(f"{vec_type}의 성분은 숫자여야 해", line=line, source=_CURRENT_SOURCE)
            if isinstance(a, int):
                a = float(a)
            if not isinstance(a, float):
                raise DanhaRuntimeError(f"{vec_type}의 성분은 숫자여야 해", line=line, source=_CURRENT_SOURCE)
            components.append(a)
        return ('VecValue', vec_type, components)
    return builtin_fn


register_builtin('vec2', _make_vec_builtin('vec2', 2))
register_builtin('vec3', _make_vec_builtin('vec3', 3))
register_builtin('vec4', _make_vec_builtin('vec4', 4))
# Phase 4: f32 SIMD 변형. 평가기에서는 f32 정밀도 표현 안 하고 vec*와 동일하게 처리
# (Python float은 항상 f64). 컴파일러에서만 <N x float>로 진짜 f32 SIMD 발생.
register_builtin('vec2f', _make_vec_builtin('vec2f', 2))
register_builtin('vec3f', _make_vec_builtin('vec3f', 3))
register_builtin('vec4f', _make_vec_builtin('vec4f', 4))


# 7.8a: 벡터 수학 내장 함수
# 게임에서 매 프레임 수백~수천 번 호출되는 핵심 연산들.
# length, dot, normalize, cross — 3D 게임 수학의 4대 연산.

import math


def _builtin_length(line, args):
    """벡터의 길이(magnitude)를 구한다. √(x² + y² + z²).
    
    비유: 원점에서 벡터가 가리키는 점까지의 거리.
    게임에서 "적이 얼마나 멀리 있나?" 같은 거리 계산의 기본.
    """
    if len(args) != 1:
        raise DanhaValueError("length()는 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    v = args[0]
    if not _is_vec(v):
        raise DanhaRuntimeError("length()는 벡터에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)
    return math.sqrt(sum(c * c for c in v[2]))


def _builtin_dot(line, args):
    """두 벡터의 내적(dot product). 결과는 스칼라(숫자 하나).
    
    계산: a.x*b.x + a.y*b.y + a.z*b.z
    비유: 두 벡터가 얼마나 같은 방향인지를 숫자 하나로 알려줌.
    - 같은 방향이면 양수 (큰 값)
    - 수직이면 0
    - 반대 방향이면 음수
    게임에서 "적이 내 앞에 있나 뒤에 있나?" 판별에 핵심.
    """
    if len(args) != 2:
        raise DanhaValueError("dot()는 2개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    a, b = args
    if not _is_vec(a) or not _is_vec(b):
        raise DanhaRuntimeError("dot()는 벡터에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)
    if a[1] != b[1]:
        raise DanhaTypeError("dot()의 두 벡터는 같은 타입이어야 해", line=line, source=_CURRENT_SOURCE)
    return sum(float(x) * float(y) for x, y in zip(a[2], b[2]))


def _builtin_normalize(line, args):
    """벡터를 단위 벡터(길이 1)로 만든다.
    
    계산: 각 성분을 벡터 길이로 나눔.
    비유: 방향만 남기고 크기를 1로 통일. "어디로 가는지"만 알려줌.
    게임에서 "적을 향해 초속 5m로 이동" 같은 방향 계산의 기본.
    """
    if len(args) != 1:
        raise DanhaValueError("normalize()는 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    v = args[0]
    if not _is_vec(v):
        raise DanhaRuntimeError("normalize()는 벡터에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)
    mag = math.sqrt(sum(c * c for c in v[2]))
    if mag == 0.0:
        raise DanhaNameError("길이가 0인 벡터는 정규화할 수 없어", line=line, source=_CURRENT_SOURCE)
    return ('VecValue', v[1], [c / mag for c in v[2]])


def _builtin_cross(line, args):
    """두 vec3의 외적(cross product). 결과는 두 벡터에 수직인 새 vec3.
    
    계산: (a.y*b.z - a.z*b.y, a.z*b.x - a.x*b.z, a.x*b.y - a.y*b.x)
    비유: 두 벡터가 만드는 평면에 수직으로 서는 벡터.
    게임에서 법선 벡터(표면이 어느 쪽을 바라보는지) 계산에 핵심.
    vec3 전용 — vec2와 vec4에는 수학적으로 정의되지 않음.
    """
    if len(args) != 2:
        raise DanhaValueError("cross()는 2개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    a, b = args
    if not _is_vec(a) or not _is_vec(b):
        raise DanhaRuntimeError("cross()는 벡터에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)
    if a[1] != 'vec3' or b[1] != 'vec3':
        raise DanhaRuntimeError("cross()는 vec3에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)
    ax, ay, az = [float(c) for c in a[2]]
    bx, by, bz = [float(c) for c in b[2]]
    return ('VecValue', 'vec3', [
        ay * bz - az * by,
        az * bx - ax * bz,
        ax * by - ay * bx,
    ])


register_builtin('length', _builtin_length)
register_builtin('dot', _builtin_dot)
register_builtin('normalize', _builtin_normalize)
register_builtin('cross', _builtin_cross)


# 7.9a: 행렬 생성 내장 함수

def _builtin_mat4_identity(line, args):
    """단위 행렬(identity matrix)을 만든다.
    
    대각선이 1이고 나머지가 0인 4x4 행렬.
    어떤 벡터에 곱해도 그 벡터가 그대로 나옴 — 곱셈의 '1' 같은 존재.
    변환을 조합할 때 시작점으로 사용.
    """
    if len(args) != 0:
        raise DanhaNameError("mat4_identity()는 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
    # 열 우선: col0=[1,0,0,0], col1=[0,1,0,0], col2=[0,0,1,0], col3=[0,0,0,1]
    data = [
        1.0, 0.0, 0.0, 0.0,  # col 0
        0.0, 1.0, 0.0, 0.0,  # col 1
        0.0, 0.0, 1.0, 0.0,  # col 2
        0.0, 0.0, 0.0, 1.0,  # col 3
    ]
    return ('MatValue', 'mat4', data)


def _builtin_mat4_translate(line, args):
    """이동 행렬을 만든다. mat4_translate(x, y, z)
    
    이 행렬을 vec4(px, py, pz, 1.0)에 곱하면 (px+x, py+y, pz+z, 1.0)이 됨.
    게임에서 물체를 특정 위치로 옮길 때 사용.
    w=1인 점은 이동하고, w=0인 방향은 이동 안 함 — 이게 4D 벡터의 핵심.
    """
    if len(args) != 3:
        raise DanhaValueError("mat4_translate()는 3개의 인자가 필요해 (x, y, z)", line=line, source=_CURRENT_SOURCE)
    vals = []
    for a in args:
        if isinstance(a, bool):
            raise DanhaValueError("mat4_translate()의 인자는 숫자여야 해", line=line, source=_CURRENT_SOURCE)
        if isinstance(a, int):
            a = float(a)
        if not isinstance(a, float):
            raise DanhaValueError("mat4_translate()의 인자는 숫자여야 해", line=line, source=_CURRENT_SOURCE)
        vals.append(a)
    tx, ty, tz = vals
    data = [
        1.0, 0.0, 0.0, 0.0,  # col 0
        0.0, 1.0, 0.0, 0.0,  # col 1
        0.0, 0.0, 1.0, 0.0,  # col 2
        tx,  ty,  tz,  1.0,  # col 3 — 이동 값이 여기에
    ]
    return ('MatValue', 'mat4', data)


def _builtin_mat4_scale(line, args):
    """크기 행렬을 만든다. mat4_scale(sx, sy, sz)
    
    이 행렬을 벡터에 곱하면 각 축 방향으로 크기가 바뀜.
    게임에서 물체를 키우거나 줄일 때 사용.
    """
    if len(args) != 3:
        raise DanhaValueError("mat4_scale()는 3개의 인자가 필요해 (sx, sy, sz)", line=line, source=_CURRENT_SOURCE)
    vals = []
    for a in args:
        if isinstance(a, bool):
            raise DanhaValueError("mat4_scale()의 인자는 숫자여야 해", line=line, source=_CURRENT_SOURCE)
        if isinstance(a, int):
            a = float(a)
        if not isinstance(a, float):
            raise DanhaValueError("mat4_scale()의 인자는 숫자여야 해", line=line, source=_CURRENT_SOURCE)
        vals.append(a)
    sx, sy, sz = vals
    data = [
        sx,  0.0, 0.0, 0.0,  # col 0
        0.0, sy,  0.0, 0.0,  # col 1
        0.0, 0.0, sz,  0.0,  # col 2
        0.0, 0.0, 0.0, 1.0,  # col 3
    ]
    return ('MatValue', 'mat4', data)


register_builtin('mat4_identity', _builtin_mat4_identity)
register_builtin('mat4_translate', _builtin_mat4_translate)
register_builtin('mat4_scale', _builtin_mat4_scale)


# 7.9b: 회전 행렬 함수
# 각도는 라디안(radian) 단위. 게임 엔진 관례를 따름.
# 360도 = 2π ≈ 6.283 라디안. 90도 = π/2 ≈ 1.5708.
# 비유: 라디안은 원의 반지름으로 잰 호의 길이. 수학/물리에서 표준.

def _parse_angle_arg(line, args, fn_name):
    """회전 함수의 각도 인자 1개를 파싱해서 float으로 돌려준다."""
    if len(args) != 1:
        raise DanhaValueError(f"{fn_name}()는 1개의 인자가 필요해 (라디안 각도)", line=line, source=_CURRENT_SOURCE)
    a = args[0]
    if isinstance(a, bool):
        raise DanhaValueError(f"{fn_name}()의 인자는 숫자여야 해", line=line, source=_CURRENT_SOURCE)
    if isinstance(a, int):
        a = float(a)
    if not isinstance(a, float):
        raise DanhaValueError(f"{fn_name}()의 인자는 숫자여야 해", line=line, source=_CURRENT_SOURCE)
    return a


def _builtin_mat4_rotate_x(line, args):
    """X축 기준 회전 행렬. mat4_rotate_x(angle)
    
    Y→Z 평면에서 회전. 고개를 끄덕이는 동작(pitch).
    """
    angle = _parse_angle_arg(line, args, 'mat4_rotate_x')
    c = math.cos(angle)
    s = math.sin(angle)
    return ('MatValue', 'mat4', [
        1.0, 0.0, 0.0, 0.0,  # col 0
        0.0,   c,   s, 0.0,  # col 1
        0.0,  -s,   c, 0.0,  # col 2
        0.0, 0.0, 0.0, 1.0,  # col 3
    ])


def _builtin_mat4_rotate_y(line, args):
    """Y축 기준 회전 행렬. mat4_rotate_y(angle)
    
    X→Z 평면에서 회전. 좌우로 고개를 돌리는 동작(yaw).
    게임에서 캐릭터가 왼쪽/오른쪽을 바라보는 데 가장 많이 씀.
    """
    angle = _parse_angle_arg(line, args, 'mat4_rotate_y')
    c = math.cos(angle)
    s = math.sin(angle)
    return ('MatValue', 'mat4', [
          c, 0.0,  -s, 0.0,  # col 0
        0.0, 1.0, 0.0, 0.0,  # col 1
          s, 0.0,   c, 0.0,  # col 2
        0.0, 0.0, 0.0, 1.0,  # col 3
    ])


def _builtin_mat4_rotate_z(line, args):
    """Z축 기준 회전 행렬. mat4_rotate_z(angle)
    
    X→Y 평면에서 회전. 2D 게임의 회전, 3D에서는 고개를 기울이는 동작(roll).
    """
    angle = _parse_angle_arg(line, args, 'mat4_rotate_z')
    c = math.cos(angle)
    s = math.sin(angle)
    return ('MatValue', 'mat4', [
          c,   s, 0.0, 0.0,  # col 0
         -s,   c, 0.0, 0.0,  # col 1
        0.0, 0.0, 1.0, 0.0,  # col 2
        0.0, 0.0, 0.0, 1.0,  # col 3
    ])


register_builtin('mat4_rotate_x', _builtin_mat4_rotate_x)
register_builtin('mat4_rotate_y', _builtin_mat4_rotate_y)
register_builtin('mat4_rotate_z', _builtin_mat4_rotate_z)


# 7.9c: transpose와 inverse

def _builtin_mat4_transpose(line, args):
    """행렬의 전치(transpose). 행과 열을 뒤바꿈.
    
    m[row][col] → m[col][row].
    비유: 표를 대각선으로 뒤집기. 행이 열이 되고 열이 행이 됨.
    법선 벡터 변환 등에 사용.
    """
    if len(args) != 1:
        raise DanhaValueError("mat4_transpose()는 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    m = args[0]
    if not _is_mat(m):
        raise DanhaRuntimeError("mat4_transpose()는 mat4에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)
    data = m[2]
    result = [0.0] * 16
    for row in range(4):
        for col in range(4):
            # 원본: data[col*4 + row]  →  전치: result[row*4 + col]
            result[row * 4 + col] = data[col * 4 + row]
    return ('MatValue', 'mat4', result)


def _builtin_mat4_inverse(line, args):
    """4x4 행렬의 역행렬(inverse).
    
    A * inverse(A) = 단위 행렬.
    비유: "이동 10미터"의 역은 "이동 -10미터". 변환을 되돌리는 행렬.
    카메라 행렬(뷰 행렬)을 만들 때 핵심적으로 사용.
    
    여수인자(cofactor) 전개 방식으로 계산. 공식이 길지만 수학적으로 정확.
    행렬식(determinant)이 0이면 역행렬이 없음 → 에러.
    """
    if len(args) != 1:
        raise DanhaValueError("mat4_inverse()는 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    m = args[0]
    if not _is_mat(m):
        raise DanhaRuntimeError("mat4_inverse()는 mat4에만 쓸 수 있어", line=line, source=_CURRENT_SOURCE)
    
    # 열 우선 배열을 편하게 쓰기 위해 (row, col) 접근 함수
    d = m[2]
    def g(r, c):
        return d[c * 4 + r]
    
    # 여수인자(cofactor) 방식 역행렬. 표준 4x4 공식.
    # 2x2 소행렬식들을 먼저 계산하면 반복 계산을 줄일 수 있음.
    s0 = g(0,0)*g(1,1) - g(1,0)*g(0,1)
    s1 = g(0,0)*g(1,2) - g(1,0)*g(0,2)
    s2 = g(0,0)*g(1,3) - g(1,0)*g(0,3)
    s3 = g(0,1)*g(1,2) - g(1,1)*g(0,2)
    s4 = g(0,1)*g(1,3) - g(1,1)*g(0,3)
    s5 = g(0,2)*g(1,3) - g(1,2)*g(0,3)
    
    c5 = g(2,2)*g(3,3) - g(3,2)*g(2,3)
    c4 = g(2,1)*g(3,3) - g(3,1)*g(2,3)
    c3 = g(2,1)*g(3,2) - g(3,1)*g(2,2)
    c2 = g(2,0)*g(3,3) - g(3,0)*g(2,3)
    c1 = g(2,0)*g(3,2) - g(3,0)*g(2,2)
    c0 = g(2,0)*g(3,1) - g(3,0)*g(2,1)
    
    det = s0*c5 - s1*c4 + s2*c3 + s3*c2 - s4*c1 + s5*c0
    if abs(det) < 1e-12:
        raise DanhaNameError("이 행렬은 역행렬이 없어 (행렬식이 0)", line=line, source=_CURRENT_SOURCE)
    
    inv_det = 1.0 / det
    
    # 수반 행렬(adjugate)의 각 원소 / det
    result = [0.0] * 16
    result[0*4+0] = ( g(1,1)*c5 - g(1,2)*c4 + g(1,3)*c3) * inv_det
    result[0*4+1] = (-g(0,1)*c5 + g(0,2)*c4 - g(0,3)*c3) * inv_det
    result[0*4+2] = ( g(3,1)*s5 - g(3,2)*s4 + g(3,3)*s3) * inv_det
    result[0*4+3] = (-g(2,1)*s5 + g(2,2)*s4 - g(2,3)*s3) * inv_det
    
    result[1*4+0] = (-g(1,0)*c5 + g(1,2)*c2 - g(1,3)*c1) * inv_det
    result[1*4+1] = ( g(0,0)*c5 - g(0,2)*c2 + g(0,3)*c1) * inv_det
    result[1*4+2] = (-g(3,0)*s5 + g(3,2)*s2 - g(3,3)*s1) * inv_det
    result[1*4+3] = ( g(2,0)*s5 - g(2,2)*s2 + g(2,3)*s1) * inv_det
    
    result[2*4+0] = ( g(1,0)*c4 - g(1,1)*c2 + g(1,3)*c0) * inv_det
    result[2*4+1] = (-g(0,0)*c4 + g(0,1)*c2 - g(0,3)*c0) * inv_det
    result[2*4+2] = ( g(3,0)*s4 - g(3,1)*s2 + g(3,3)*s0) * inv_det
    result[2*4+3] = (-g(2,0)*s4 + g(2,1)*s2 - g(2,3)*s0) * inv_det
    
    result[3*4+0] = (-g(1,0)*c3 + g(1,1)*c1 - g(1,2)*c0) * inv_det
    result[3*4+1] = ( g(0,0)*c3 - g(0,1)*c1 + g(0,2)*c0) * inv_det
    result[3*4+2] = (-g(3,0)*s3 + g(3,1)*s1 - g(3,2)*s0) * inv_det
    result[3*4+3] = ( g(2,0)*s3 - g(2,1)*s1 + g(2,2)*s0) * inv_det
    
    # 열 우선으로 재배열 (result는 지금 행 우선: result[row*4+col])
    col_major = [0.0] * 16
    for r in range(4):
        for c in range(4):
            col_major[c * 4 + r] = result[r * 4 + c]
    
    return ('MatValue', 'mat4', col_major)


register_builtin('mat4_transpose', _builtin_mat4_transpose)
register_builtin('mat4_inverse', _builtin_mat4_inverse)


# ===== 7.12b: ECS World (엔티티 생명주기) =====
#
# 설계 결정 요약:
# - World는 단일 전역 객체. 사용자 코드에 노출 안 함.
#   run() 호출 시마다 새로 초기화해서 테스트 간 상태 누수 차단.
# - EntityId는 (index, gen) 쌍. 인터프리터 안에서는 ('EntityId', index, gen) 튜플.
# - free_list: 죽은 엔티티의 index를 스택처럼 쌓음. spawn()은 여기서 먼저 꺼냄.
# - generations[i]: i번 슬롯의 현재 세대 번호. destroy()할 때마다 +1.
#   alive[i]: i번 슬롯이 지금 살아 있나.
#
# 이 단계에서는 컴포넌트 저장소가 아직 없다. 그래서 destroy() 때 컴포넌트 청소 로직도 없다.
# 7.12c에서 컴포넌트 add/get을 붙일 때 destroy가 "이 엔티티의 모든 컴포넌트도 날려라"로 확장된다.


class _World:
    def __init__(self):
        self.generations = []  # [int, ...]  슬롯별 현재 세대
        self.alive = []        # [bool, ...] 슬롯별 생존 여부
        self.free_list = []    # [int, ...]  재사용 가능한 index 스택
        # 7.12c: 컴포넌트 저장소. key는 컴포넌트 타입 이름 (예: 'Position').
        # 값은 _ComponentStore 인스턴스.
        # 컴포넌트 저장소는 ComponentDef를 처음 볼 때, 혹은 add가 처음 호출될 때 생성.
        self.stores = {}
    
    def get_or_create_store(self, comp_name, fields):
        """이 컴포넌트의 저장소가 없으면 만들고, 있으면 그대로 반환."""
        if comp_name not in self.stores:
            self.stores[comp_name] = _ComponentStore(comp_name, fields)
        return self.stores[comp_name]
    
    def spawn(self):
        """빈 엔티티를 하나 만들고 EntityId를 반환."""
        if self.free_list:
            # 빈자리 재사용. 세대는 destroy될 때 이미 +1 됐음.
            idx = self.free_list.pop()
            self.alive[idx] = True
            return ('EntityId', idx, self.generations[idx])
        else:
            # 새 슬롯 할당. 세대는 0부터 시작.
            idx = len(self.generations)
            self.generations.append(0)
            self.alive.append(True)
            return ('EntityId', idx, 0)
    
    def destroy(self, idx, gen):
        """엔티티 죽임. 댕글링 참조면 False, 성공 시 True.
        7.12c 확장: 이 엔티티에 붙은 모든 컴포넌트도 같이 정리."""
        if idx < 0 or idx >= len(self.generations):
            return False
        if not self.alive[idx]:
            return False
        if self.generations[idx] != gen:
            return False
        # 모든 저장소를 훑어서 이 엔티티 것 제거.
        # 저장소가 많아지면 O(컴포넌트 종류 수). 보통 수십 개 이하라 실용적.
        for store in self.stores.values():
            store.remove(idx)
        self.alive[idx] = False
        self.generations[idx] += 1  # 다음 주인은 새 세대
        self.free_list.append(idx)
        return True
    
    def is_alive(self, idx, gen):
        if idx < 0 or idx >= len(self.generations):
            return False
        if not self.alive[idx]:
            return False
        return self.generations[idx] == gen


class _ComponentStore:
    """한 컴포넌트 타입을 위한 Sparse Set + SoA 저장소.

    구조:
      field_arrays: {필드이름: [값, 값, ...]}   ← SoA. 필드별로 빽빽한 배열.
      dense_to_entity: [엔티티_idx, ...]        ← 각 빽빽한 슬롯이 어느 엔티티 거?
      entity_to_dense: {엔티티_idx: 빽빽한_idx} ← 역인덱스. 없으면 키가 없음.
    
    add(): 모든 field_arrays에 append, dense_to_entity에 append, entity_to_dense에 기록.
    remove(): 마지막 원소를 빈자리로 옮겨서 빽빽함 유지 ("swap-remove"). O(1).
    """
    def __init__(self, name, fields):
        self.name = name  # 'Position' 등, 에러 메시지용
        # fields는 [(필드이름, 타입_노드_또는_None), ...]
        self.field_names = [f[0] for f in fields]
        self.field_arrays = {fname: [] for fname in self.field_names}
        self.dense_to_entity = []
        self.entity_to_dense = {}
    
    def has(self, entity_idx):
        return entity_idx in self.entity_to_dense
    
    def add(self, entity_idx, field_values):
        """field_values는 {필드이름: 값} 딕셔너리. 이미 있으면 덮어씀."""
        if entity_idx in self.entity_to_dense:
            # 덮어쓰기: 기존 슬롯에 값만 교체.
            dense_idx = self.entity_to_dense[entity_idx]
            for fname in self.field_names:
                self.field_arrays[fname][dense_idx] = field_values[fname]
            return
        # 새로 추가: 모든 필드 배열 끝에 append, 역인덱스 기록.
        dense_idx = len(self.dense_to_entity)
        for fname in self.field_names:
            self.field_arrays[fname].append(field_values[fname])
        self.dense_to_entity.append(entity_idx)
        self.entity_to_dense[entity_idx] = dense_idx
    
    def get(self, entity_idx):
        """필드 딕셔너리 반환. 없으면 None."""
        if entity_idx not in self.entity_to_dense:
            return None
        dense_idx = self.entity_to_dense[entity_idx]
        return {fname: self.field_arrays[fname][dense_idx] for fname in self.field_names}
    
    def remove(self, entity_idx):
        """swap-remove: 마지막 원소를 빈자리로 옮김. 빽빽함 유지, O(1).
        entity가 이 컴포넌트를 안 가지면 조용히 무시 (destroy의 일괄 정리에 편함)."""
        if entity_idx not in self.entity_to_dense:
            return False
        dense_idx = self.entity_to_dense[entity_idx]
        last_idx = len(self.dense_to_entity) - 1
        
        if dense_idx != last_idx:
            # 마지막 원소를 이 자리로 이동
            last_entity = self.dense_to_entity[last_idx]
            for fname in self.field_names:
                self.field_arrays[fname][dense_idx] = self.field_arrays[fname][last_idx]
            self.dense_to_entity[dense_idx] = last_entity
            self.entity_to_dense[last_entity] = dense_idx
        
        # 이제 마지막 원소를 버림
        for fname in self.field_names:
            self.field_arrays[fname].pop()
        self.dense_to_entity.pop()
        del self.entity_to_dense[entity_idx]
        return True


# ================================================================
# 7.11b: system 바인딩 안전성 분석
# ================================================================

def _analyze_system_bindings(name, bindings, body, is_parallel, line):
    """system의 for each 바인딩을 분석한다. 7.15e: 시그니처가 스케줄러의 진실.
    
    규칙 (Q1=2, Q2=1, Q3=2 결정):
    1) 중복 컴포넌트 바인딩 검사
    2) access 결정:
       - 시그니처 &mut (access='write') → write
       - 시그니처 & (access='read')   → read (본문에서 쓰면 7.6 소유권이 차단)
       - 시그니처 생략 (access='unspecified') → 본문 스캔으로 자동 판단 (기존 호환)
    3) 검증: 명시 &(read)인데 본문에서 쓰려 하면 경고
    
    반환: (access_map, comp_access_map)
    """
    # 1) 중복 컴포넌트 검사
    seen_comps = {}
    for binding in bindings:
        bind_var, comp_name = binding[0], binding[1]
        if comp_name in seen_comps and bind_var is not None:
            raise DanhaECSError(
                f"system '{name}'에서 컴포넌트 '{comp_name}'이(가) "
                f"두 번 바인딩됐어 ('{seen_comps[comp_name]}'과 '{bind_var}')",
                line=line, source=_CURRENT_SOURCE
            )
        if bind_var is not None:
            seen_comps[comp_name] = bind_var
    
    # 2) 본문 스캔 — 휴리스틱은 유지하되 역할 전환 (검증 + 생략 바인딩의 기본값 결정)
    bind_vars = {b[0] for b in bindings if b[0] is not None}
    written_vars = set()
    _collect_written_vars(body, bind_vars, written_vars)
    
    # 3) 최종 access 결정
    access_map = {}
    comp_access_map = {}
    for binding in bindings:
        bind_var, comp_name, declared_access = binding[0], binding[1], binding[2]
        kind = binding[3] if len(binding) > 3 else 'required'
        # exclude 바인딩은 변수 없음 — access_map 불필요
        if kind == 'exclude':
            continue
        body_writes = bind_var in written_vars
        
        if declared_access == 'write':
            mode = 'write'
        elif declared_access == 'read':
            if body_writes:
                import sys as _sys
                print(
                    f"경고 [{line}번째 줄]: system '{name}'의 바인딩 '{bind_var}: &{comp_name}'은(는) "
                    f"읽기로 선언됐는데 본문에 쓰기가 있어. '&mut {comp_name}'로 바꾸거나 본문의 쓰기를 제거해.",
                    file=_sys.stderr,
                )
            mode = 'read'
        else:
            mode = 'write' if body_writes else 'read'
        
        access_map[bind_var] = mode
        comp_access_map[comp_name] = mode
    
    return access_map, comp_access_map


def _collect_written_vars(node, bind_vars, written_vars):
    """AST를 재귀 순회하며 FieldAssign의 대상이 bind_vars에 속하면 written_vars에 추가."""
    if not isinstance(node, tuple) or len(node) == 0:
        return
    
    node_type = node[0]
    
    # FieldAssign: ('FieldAssign', obj_node, field_name, value_node, line)
    # obj_node가 ('Name', var_name, ...) 이고 var_name이 바인딩 변수면 쓰기
    if node_type == 'FieldAssign':
        obj = node[1]
        if isinstance(obj, tuple) and obj[0] == 'Name' and obj[1] in bind_vars:
            written_vars.add(obj[1])
        # value_node 쪽도 순회 (거기서도 다른 바인딩을 쓸 수 있으니)
        _collect_written_vars(node[3], bind_vars, written_vars)
        return
    
    # 모든 자식 노드를 재귀 순회
    for child in node:
        if isinstance(child, tuple):
            _collect_written_vars(child, bind_vars, written_vars)
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, tuple):
                    _collect_written_vars(item, bind_vars, written_vars)


class _SystemComponentProxy:
    """7.13: system의 for each 순회에서 컴포넌트 바인딩 변수가 되는 프록시.
    
    p.x를 읽으면 SoA 저장소에서 직접 읽고,
    p.x = 5를 쓰면 SoA에 직접 써서 원본이 바뀐다.
    
    7.6: readonly=True면 쓰기 시도 시 에러. 자동 분석(7.11b)에서
    'read'로 분류된 바인딩은 readonly로 설정.
    
    FieldAccess와 FieldAssign에서 이 타입을 인식해야 한다.
    """
    def __init__(self, store, entity_idx, readonly=False, bind_var=None):
        self.store = store        # _ComponentStore 인스턴스
        self.entity_idx = entity_idx
        self.dense_idx = store.entity_to_dense[entity_idx]
        self.readonly = readonly
        self.bind_var = bind_var   # 에러 메시지용
    
    def get_field(self, field_name):
        return self.store.field_arrays[field_name][self.dense_idx]
    
    def set_field(self, field_name, value):
        if self.readonly:
            raise DanhaRuntimeError(
                f"'{self.bind_var}'은(는) 읽기 전용 바인딩이야 — "
                f"'{self.comp_name}.{field_name}'에 쓰려면 본문에서 이 필드를 수정하는 코드가 필요해",
                source=_CURRENT_SOURCE
            )
        self.store.field_arrays[field_name][self.dense_idx] = value
    
    def has_field(self, field_name):
        return field_name in self.store.field_arrays
    
    @property
    def comp_name(self):
        return self.store.name


# 전역 한 개. run()에서 매번 새 World로 교체.
_WORLD = _World()

# 7.14a: system 레지스트리. {이름: {comp_access_map, is_parallel, params}}
# run()에서 매번 초기화. schedule()이 이 정보로 실행 순서를 결정.
_SYSTEM_REGISTRY = {}

# 35단계: @attribute 메타데이터 저장소. {이름: [('attr_name', {args})]}
_ATTRIBUTES = {}


def _schedule_systems():
    """7.14b + 문제 4 해결: 등록된 system들을 토폴로지 정렬해서 실행 순서를 결정.
    
    규칙:
    - 같은 컴포넌트를 두 system이 모두 쓰면 → 에러 (writer/writer 충돌)
    - A가 X를 쓰고 B가 X를 읽고, 반대 방향(B가 Y를 쓰고 A가 Y를 읽음)이 없으면 → A 먼저
    - 양방향 읽기/쓰기 교차 → 순환이 아니라 "상호 참조" 로 보고 등록 순서 유지
      (게임 프레임은 "지금 프레임 출력 → 다음 프레임 입력" 경계가 암묵적)
    
    반환: 정렬된 system 이름 리스트.
    """
    names = list(_SYSTEM_REGISTRY.keys())
    if len(names) <= 1:
        return names
    
    # 0) writer/writer 충돌 먼저 검사 — 두 system이 같은 컴포넌트를 모두 쓰면 거부
    for i, a_name in enumerate(names):
        a_map = _SYSTEM_REGISTRY[a_name]['comp_access_map']
        for j in range(i + 1, len(names)):
            b_name = names[j]
            b_map = _SYSTEM_REGISTRY[b_name]['comp_access_map']
            shared_writes = [
                c for c, m in a_map.items()
                if m == 'write' and b_map.get(c) == 'write'
            ]
            if shared_writes:
                raise DanhaECSError(
                    f"system '{a_name}'와(과) '{b_name}'이(가) "
                    f"같은 컴포넌트 {shared_writes}에 모두 쓰려고 해. "
                    f"둘 중 하나만 쓰도록 고치거나, 로직을 한 system으로 합쳐줘.",
                    source=_CURRENT_SOURCE
                )
    
    # 1) 의존 간선 수집: writer → reader, 단방향일 때만
    # A가 X를 쓰고 B가 X를 읽어도, 반대 방향(B가 Y를 쓰고 A가 Y를 읽음)이
    # 동시에 있으면 상호 참조 → 간선 생략 (등록 순서가 역할 대신함)
    edges = []
    for i, a_name in enumerate(names):
        a_map = _SYSTEM_REGISTRY[a_name]['comp_access_map']
        for j, b_name in enumerate(names):
            if i == j:
                continue
            b_map = _SYSTEM_REGISTRY[b_name]['comp_access_map']
            # A가 쓰고 B가 읽는 컴포넌트가 있는가?
            a_writes_b_reads = any(
                m == 'write' and b_map.get(c) == 'read'
                for c, m in a_map.items()
            )
            if not a_writes_b_reads:
                continue
            # 반대 방향(B가 쓰고 A가 읽음)도 있으면 상호 참조 → 간선 생략
            b_writes_a_reads = any(
                m == 'write' and a_map.get(c) == 'read'
                for c, m in b_map.items()
            )
            if b_writes_a_reads:
                continue
            edges.append((a_name, b_name))
    
    # 2) 토폴로지 정렬 (Kahn's algorithm)
    in_degree = {n: 0 for n in names}
    adj = {n: [] for n in names}
    for before, after in edges:
        adj[before].append(after)
        in_degree[after] += 1
    
    queue = [n for n in names if in_degree[n] == 0]
    queue.sort(key=lambda n: names.index(n))
    
    result = []
    while queue:
        node = queue.pop(0)
        result.append(node)
        for neighbor in adj[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
                queue.sort(key=lambda n: names.index(n))
    
    # 양방향 교차는 0단계에서 간선이 생략되므로 여기까지 왔다면 순환 불가
    # 혹시 라도 남는 건 단방향 write→read 간선만으로 만든 진짜 순환뿐
    if len(result) != len(names):
        remaining = [n for n in names if n not in result]
        raise DanhaECSError(
            f"system 간 순환 의존이 발견됐어: {remaining}. "
            f"단방향 write→read 체인이 순환을 만들고 있어.",
            source=_CURRENT_SOURCE
        )
    
    return result


def _unwrap_entity_id(line, v, context):
    """EntityId 튜플 검증 헬퍼. context는 에러 메시지용 (예: 'destroy')."""
    if not (isinstance(v, tuple) and len(v) == 3 and v[0] == 'EntityId'):
        raise DanhaECSError(f"{context}는 EntityId가 필요한데 다른 값이 왔어", line=line, source=_CURRENT_SOURCE)
    return v[1], v[2]


def _builtin_spawn(line, args):
    if len(args) != 0:
        raise DanhaValueError(f"spawn은 인자를 받지 않는데 {len(args)}개가 들어왔어", line=line, source=_CURRENT_SOURCE)
    return _WORLD.spawn()


def _builtin_destroy(line, args):
    if len(args) != 1:
        raise DanhaValueError(f"destroy는 1개의 인자가 필요한데 {len(args)}개가 들어왔어", line=line, source=_CURRENT_SOURCE)
    idx, gen = _unwrap_entity_id(line, args[0], 'destroy')
    return _WORLD.destroy(idx, gen)


def _builtin_is_alive(line, args):
    if len(args) != 1:
        raise DanhaValueError(f"is_alive는 1개의 인자가 필요한데 {len(args)}개가 들어왔어", line=line, source=_CURRENT_SOURCE)
    idx, gen = _unwrap_entity_id(line, args[0], 'is_alive')
    return _WORLD.is_alive(idx, gen)


# ----- 7.12c: 컴포넌트 부착/조회 내장 함수 -----
#
# API 형태: add(e, Position { x: 1.0, y: 2.0 }) / get(e, Position) / has(e, Position) / remove(e, Position)
# 설계: 타입 인자 없이 값/ComponentDef로 받음. 파서 변경 불필요.
#   - add의 두 번째 인자는 StructValue (구조체 리터럴 평가 결과). 타입 이름이 component 이름과 일치해야 함.
#   - get/has/remove의 두 번째 인자는 ComponentDef (타입 그 자체).
# 에러 메시지는 줄 번호와 상세 설명 포함. 게임 엔진 언어답게 실수를 빨리 드러냄.


def _unwrap_component_def(line, v, context):
    """ComponentDef 튜플 검증. ('ComponentDef', fields, {}) 모양."""
    if not (isinstance(v, tuple) and len(v) >= 2 and v[0] == 'ComponentDef'):
        if isinstance(v, tuple) and v[0] == 'StructDef':
            raise DanhaTypeError(
                f"{context}는 component 타입이 필요한데 struct가 왔어. "
                f"'struct'를 'component'로 바꿔봐",
                line=line, source=_CURRENT_SOURCE
            )
        raise DanhaTypeError(f"{context}는 component 타입이 필요한데 다른 값이 왔어", line=line, source=_CURRENT_SOURCE)
    return v[1]  # fields: [(name, type_node_or_None), ...]


def _check_alive_entity(line, idx, gen, context):
    """죽은/낡은 엔티티 참조면 에러. 살아 있으면 None 반환."""
    if not _WORLD.is_alive(idx, gen):
        raise DanhaECSError(
            f"{context}: 이 엔티티는 이미 죽었거나 존재하지 않아 (Entity({idx}, {gen}))",
            line=line, source=_CURRENT_SOURCE
        )


def _builtin_add(line, args):
    """add(entity, ComponentStructLiteral) — 엔티티에 컴포넌트 부착."""
    if len(args) != 2:
        raise DanhaValueError(f"add는 2개의 인자가 필요한데 {len(args)}개가 들어왔어", line=line, source=_CURRENT_SOURCE)
    idx, gen = _unwrap_entity_id(line, args[0], 'add')
    _check_alive_entity(line, idx, gen, 'add')
    
    comp_val = args[1]
    # StructValue여야 함. 그리고 그 타입이 Danha 스코프에 component로 등록돼 있어야.
    # 여기서 스코프 접근은 간접적 — evaluate 경로에서 이미 StructValue로 만들어짐.
    # 그 StructValue의 타입 이름으로 component 정의를 어떻게 찾지?
    # 전략: Danha의 구조체 리터럴 평가는 StructDef든 ComponentDef든 같은 모양의
    # StructValue를 만들어낸다 (평가기 코드를 7.12c에서 그렇게 확장). 그래서 여기서는
    # StructValue의 타입 이름으로 WORLD의 저장소를 찾아 부착.
    # '이 타입이 실제로 component인가'는 평가기가 이미 검증했으므로 여기선 이름만 사용.
    if not (isinstance(comp_val, tuple) and len(comp_val) >= 3 and comp_val[0] == 'ComponentValue'):
        raise DanhaValueError(
            f"add의 두 번째 인자는 component 값이 필요해 "
            f"(예: 'Position {{ x: 1, y: 2 }}')",
            line=line, source=_CURRENT_SOURCE
        )
    comp_name = comp_val[1]
    field_values = comp_val[2]  # {필드이름: 값}
    field_defs = comp_val[3]    # [(필드이름, 타입노드_or_None), ...] — 저장소 생성용
    
    store = _WORLD.get_or_create_store(comp_name, field_defs)
    
    # 7.15f: 컴파일러와 의미 동등성 유지.
    # - 부동 → 정수 필드 거부
    # - 정수 → 명시된 f32/f64 필드 암묵 승격 (Q3=3)
    # - 타입 생략 필드는 기존 동작 그대로 (Python 값이 int면 int로 유지)
    _int_types = {'i8', 'i16', 'i32', 'i64', 'u8', 'u16', 'u32', 'u64', 'bool'}
    _float_types = {'f32', 'f64'}
    for fname, type_node in field_defs:
        if type_node is None or type_node[0] != 'TypeName':
            continue  # 생략 필드: 기존 호환 유지
        tname = type_node[1]
        v = field_values.get(fname)
        if tname in _int_types:
            # Python bool은 int 서브클래스니 통과. float만 거부.
            if isinstance(v, float):
                raise DanhaECSError(
                    f"컴포넌트 '{comp_name}'의 필드 '{fname}: {tname}'에 "
                    f"부동소수값 {v}을(를) 넣으려고 해. 정수 리터럴이 필요해.",
                    line=line, source=_CURRENT_SOURCE
                )
        elif tname in _float_types:
            # 정수 리터럴 → 부동 자동 승격
            if isinstance(v, int) and not isinstance(v, bool):
                field_values[fname] = float(v)
    
    store.add(idx, field_values)
    return None


def _builtin_get(line, args):
    """get(entity, ComponentType) — 컴포넌트 조회. 없으면 에러."""
    if len(args) != 2:
        raise DanhaValueError(f"get은 2개의 인자가 필요한데 {len(args)}개가 들어왔어", line=line, source=_CURRENT_SOURCE)
    idx, gen = _unwrap_entity_id(line, args[0], 'get')
    _check_alive_entity(line, idx, gen, 'get')
    
    comp_def_fields = _unwrap_component_def(line, args[1], 'get')
    # args[1]의 이름을 얻으려면 ComponentDef 튜플에서 찾아야 하는데,
    # 현재 저장 형태는 ('ComponentDef', fields, methods_dict). 이름이 없다!
    # 해결: ComponentDef 저장 형태에 이름을 포함하도록 확장.
    # (scope.declare에서 ('ComponentDef', name, fields, methods)로 저장하게 평가기 수정)
    comp_name = args[1][1]
    comp_fields = args[1][2]
    
    store = _WORLD.stores.get(comp_name)
    if store is None:
        raise DanhaNameError(f"get: 엔티티 Entity({idx}, {gen})에 {comp_name} 컴포넌트가 없어", line=line, source=_CURRENT_SOURCE)
    
    field_values = store.get(idx)
    if field_values is None:
        raise DanhaNameError(f"get: 엔티티 Entity({idx}, {gen})에 {comp_name} 컴포넌트가 없어", line=line, source=_CURRENT_SOURCE)
    
    return ('ComponentValue', comp_name, dict(field_values), comp_fields)


def _builtin_has(line, args):
    """has(entity, ComponentType) — 부착 여부. true/false."""
    if len(args) != 2:
        raise DanhaValueError(f"has는 2개의 인자가 필요한데 {len(args)}개가 들어왔어", line=line, source=_CURRENT_SOURCE)
    idx, gen = _unwrap_entity_id(line, args[0], 'has')
    # has는 죽은 엔티티에 대해 에러 대신 false 반환 (조회용이라 편의성 우선)
    if not _WORLD.is_alive(idx, gen):
        return False
    _unwrap_component_def(line, args[1], 'has')
    comp_name = args[1][1]
    store = _WORLD.stores.get(comp_name)
    if store is None:
        return False
    return store.has(idx)


def _builtin_remove(line, args):
    """remove(entity, ComponentType) — 컴포넌트 제거. 없어도 에러 아님 (조용히 false)."""
    if len(args) != 2:
        raise DanhaValueError(f"remove는 2개의 인자가 필요한데 {len(args)}개가 들어왔어", line=line, source=_CURRENT_SOURCE)
    idx, gen = _unwrap_entity_id(line, args[0], 'remove')
    _check_alive_entity(line, idx, gen, 'remove')
    _unwrap_component_def(line, args[1], 'remove')
    comp_name = args[1][1]
    store = _WORLD.stores.get(comp_name)
    if store is None:
        return False
    return store.remove(idx)


register_builtin('spawn', _builtin_spawn)
register_builtin('destroy', _builtin_destroy)
register_builtin('is_alive', _builtin_is_alive)
register_builtin('add', _builtin_add)
register_builtin('get', _builtin_get)
register_builtin('has', _builtin_has)
register_builtin('remove', _builtin_remove)


def _builtin_schedule(line, args):
    """7.14c: schedule(인자들...) — 등록된 모든 system을 올바른 순서로 실행.
    
    토폴로지 정렬로 의존 순서를 결정하고,
    schedule에 넘긴 인자를 각 system에 그대로 전달한다.
    예: schedule(0.016) → 모든 system에 dt=0.016이 전달됨.
    
    system의 매개변수 개수가 schedule의 인자 개수와 다르면 에러.
    """
    if len(_SYSTEM_REGISTRY) == 0:
        return None
    
    # 토폴로지 정렬
    ordered = _schedule_systems()
    
    # 각 system을 순서대로 호출
    for sys_name in ordered:
        info = _SYSTEM_REGISTRY[sys_name]
        defining_scope = info['defining_scope']
        
        # system을 정의 스코프에서 찾기
        func = defining_scope.get(sys_name)
        if func is None or func[0] != 'System':
            raise DanhaNameError(
                f"schedule: system '{sys_name}'을(를) 찾을 수 없어",
                line=line, source=_CURRENT_SOURCE
            )
        
        sys_params = func[1]
        sys_bindings = func[2]
        sys_body = func[3]
        sys_is_parallel = func[4]
        sys_access_map = func[5]
        sys_defining_scope = func[6]
        
        if len(args) != len(sys_params):
            raise DanhaValueError(
                f"schedule: system '{sys_name}'은(는) "
                f"{len(sys_params)}개의 인자가 필요한데 schedule에 {len(args)}개가 넘어왔어",
                line=line, source=_CURRENT_SOURCE
            )
        
        # 바인딩을 종류별로 분류 (28단계: required/optional/exclude)
        req_stores_s = []
        opt_stores_s = []
        excl_stores_s = []
        skip_system = False
        for binding in sys_bindings:
            bv, cn = binding[0], binding[1]
            kind = binding[3] if len(binding) > 3 else 'required'
            if kind == 'exclude':
                store = _WORLD.stores.get(cn)
                if store is not None:
                    excl_stores_s.append(store)
            elif kind == 'optional':
                store = _WORLD.stores.get(cn)
                opt_stores_s.append((bv, cn, store))
            else:
                if cn not in _WORLD.stores:
                    skip_system = True
                    break
                req_stores_s.append((bv, cn, _WORLD.stores[cn]))

        if skip_system or (len(req_stores_s) == 0 and len(opt_stores_s) == 0):
            continue

        if len(req_stores_s) == 0:
            entity_snapshot = [i for i, a in enumerate(_WORLD.alive) if a]
        else:
            pivot_idx = 0
            pivot_count = len(req_stores_s[0][2].dense_to_entity)
            for ci in range(1, len(req_stores_s)):
                c = len(req_stores_s[ci][2].dense_to_entity)
                if c < pivot_count:
                    pivot_count = c
                    pivot_idx = ci
            entity_snapshot = list(req_stores_s[pivot_idx][2].dense_to_entity)

        def _run_entity_s(entity_idx):
            if entity_idx >= len(_WORLD.alive) or not _WORLD.alive[entity_idx]:
                return
            for excl_store in excl_stores_s:
                if excl_store.has(entity_idx):
                    return
            for bv, cn, req_store in req_stores_s:
                if not req_store.has(entity_idx):
                    return
            iter_scope = Scope(parent=sys_defining_scope)
            for param_name, arg_value in zip(sys_params, args):
                iter_scope.declare(param_name, arg_value)
            for bv, cn, req_store in req_stores_s:
                is_readonly = sys_access_map.get(bv) == 'read'
                proxy = _SystemComponentProxy(req_store, entity_idx, readonly=is_readonly, bind_var=bv)
                iter_scope.declare(bv, proxy)
            for bv, cn, opt_store in opt_stores_s:
                if opt_store is not None and opt_store.has(entity_idx):
                    is_readonly = sys_access_map.get(bv) == 'read'
                    proxy = _SystemComponentProxy(opt_store, entity_idx, readonly=is_readonly, bind_var=bv)
                    iter_scope.declare(bv, proxy)
                else:
                    iter_scope.declare(bv, None)
            evaluate(sys_body, iter_scope)

        if sys_is_parallel:
            _n = min(_os_cpu.cpu_count() or 1, len(entity_snapshot)) if entity_snapshot else 0
            if _n <= 1:
                for entity_idx in entity_snapshot:
                    try:
                        _run_entity_s(entity_idx)
                    except ContinueSignal:
                        continue
                    except BreakSignal:
                        break
            else:
                _chunk_sz = (len(entity_snapshot) + _n - 1) // _n
                _chunks = [entity_snapshot[i:i+_chunk_sz]
                           for i in range(0, len(entity_snapshot), _chunk_sz)]
                _stop = [False]
                _errs = []
                def _run_chunk_s(ch):
                    for eid in ch:
                        if _stop[0]:
                            break
                        try:
                            _run_entity_s(eid)
                        except ContinueSignal:
                            continue
                        except BreakSignal:
                            _stop[0] = True
                            break
                        except Exception as _exc:
                            _errs.append(_exc)
                            _stop[0] = True
                            break
                _ts = [_threading.Thread(target=_run_chunk_s, args=(ch,)) for ch in _chunks]
                for _t in _ts: _t.start()
                for _t in _ts: _t.join()
                if _errs:
                    raise _errs[0]
        else:
            for entity_idx in entity_snapshot:
                try:
                    _run_entity_s(entity_idx)
                except ContinueSignal:
                    continue
                except BreakSignal:
                    break

    return None

register_builtin('schedule', _builtin_schedule)


# ===== 26단계: 표준 라이브러리 — 파일 I/O, 시간, 문자열, 타입 변환 =====

import time as _time
import os as _os


# --- 파일 I/O ---

def _builtin_file_read(line, args):
    """파일 내용을 문자열로 읽기. file_read("path") -> str"""
    if len(args) != 1 or not isinstance(args[0], str):
        raise DanhaValueError("file_read(path)에는 문자열 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
    try:
        with open(args[0], 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        raise DanhaRuntimeError(f"파일을 찾을 수 없어: {args[0]}", line=line, source=_CURRENT_SOURCE)
    except Exception as e:
        raise DanhaRuntimeError(f"파일 읽기 실패: {e}", line=line, source=_CURRENT_SOURCE)


def _builtin_file_write(line, args):
    """문자열을 파일에 쓰기. file_write("path", "content") -> None"""
    if len(args) != 2 or not isinstance(args[0], str) or not isinstance(args[1], str):
        raise DanhaValueError("file_write(path, content)에는 문자열 인자 2개가 필요해", line=line, source=_CURRENT_SOURCE)
    try:
        with open(args[0], 'w', encoding='utf-8') as f:
            f.write(args[1])
        return None
    except Exception as e:
        raise DanhaRuntimeError(f"파일 쓰기 실패: {e}", line=line, source=_CURRENT_SOURCE)


def _builtin_file_append(line, args):
    """문자열을 파일 끝에 추가. file_append("path", "content") -> None"""
    if len(args) != 2 or not isinstance(args[0], str) or not isinstance(args[1], str):
        raise DanhaValueError("file_append(path, content)에는 문자열 인자 2개가 필요해", line=line, source=_CURRENT_SOURCE)
    try:
        with open(args[0], 'a', encoding='utf-8') as f:
            f.write(args[1])
        return None
    except Exception as e:
        raise DanhaRuntimeError(f"파일 추가 쓰기 실패: {e}", line=line, source=_CURRENT_SOURCE)


def _builtin_file_exists(line, args):
    """파일 존재 여부. file_exists("path") -> bool"""
    if len(args) != 1 or not isinstance(args[0], str):
        raise DanhaValueError("file_exists(path)에는 문자열 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
    return _os.path.exists(args[0])


# --- 시간 ---

def _builtin_time(line, args):
    """현재 시간 (초, 소수점 포함). time() -> f64"""
    if len(args) != 0:
        raise DanhaValueError("time()은 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
    return _time.time()


def _builtin_clock(line, args):
    """고해상도 성능 시계 (초). clock() -> f64"""
    if len(args) != 0:
        raise DanhaValueError("clock()은 인자가 없어야 해", line=line, source=_CURRENT_SOURCE)
    return _time.perf_counter()


# --- 문자열 유틸리티 ---

def _builtin_split(line, args):
    """문자열 분할. split("a,b,c", ",") -> ["a", "b", "c"]"""
    if len(args) != 2 or not isinstance(args[0], str) or not isinstance(args[1], str):
        raise DanhaValueError("split(str, sep)에는 문자열 인자 2개가 필요해", line=line, source=_CURRENT_SOURCE)
    return args[0].split(args[1])


def _builtin_trim(line, args):
    """문자열 앞뒤 공백 제거. trim("  hello  ") -> "hello" """
    if len(args) != 1 or not isinstance(args[0], str):
        raise DanhaValueError("trim(str)에는 문자열 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
    return args[0].strip()


def _builtin_starts_with(line, args):
    """접두사 확인. starts_with("hello", "hel") -> true"""
    if len(args) != 2 or not isinstance(args[0], str) or not isinstance(args[1], str):
        raise DanhaValueError("starts_with(str, prefix)에는 문자열 인자 2개가 필요해", line=line, source=_CURRENT_SOURCE)
    return args[0].startswith(args[1])


def _builtin_ends_with(line, args):
    """접미사 확인. ends_with("hello.txt", ".txt") -> true"""
    if len(args) != 2 or not isinstance(args[0], str) or not isinstance(args[1], str):
        raise DanhaValueError("ends_with(str, suffix)에는 문자열 인자 2개가 필요해", line=line, source=_CURRENT_SOURCE)
    return args[0].endswith(args[1])


def _builtin_replace(line, args):
    """문자열 치환. replace("hello world", "world", "danha") -> "hello danha" """
    if len(args) != 3 or not all(isinstance(a, str) for a in args):
        raise DanhaValueError("replace(str, old, new)에는 문자열 인자 3개가 필요해", line=line, source=_CURRENT_SOURCE)
    return args[0].replace(args[1], args[2])


def _builtin_char_at(line, args):
    """인덱스 위치의 문자. char_at("hello", 1) -> "e" """
    if len(args) != 2 or not isinstance(args[0], str) or not isinstance(args[1], int):
        raise DanhaValueError("char_at(str, index)에는 (문자열, 정수) 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    s, idx = args[0], args[1]
    if idx < 0 or idx >= len(s):
        raise DanhaRuntimeError(f"인덱스 {idx}가 문자열 길이 {len(s)}를 벗어났어", line=line, source=_CURRENT_SOURCE)
    return s[idx]


def _builtin_substr(line, args):
    """부분 문자열. substr("hello", 1, 3) -> "ell" """
    if len(args) != 3 or not isinstance(args[0], str) or not isinstance(args[1], int) or not isinstance(args[2], int):
        raise DanhaValueError("substr(str, start, len)에는 (문자열, 정수, 정수) 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    return args[0][args[1]:args[1] + args[2]]


def _builtin_contains(line, args):
    """부분 문자열 포함 여부. contains("hello world", "world") -> true"""
    if len(args) != 2 or not isinstance(args[0], str) or not isinstance(args[1], str):
        raise DanhaValueError("contains(str, substr)에는 문자열 인자 2개가 필요해", line=line, source=_CURRENT_SOURCE)
    return args[1] in args[0]


def _builtin_str_len(line, args):
    """문자열 길이. str_len("hello") -> 5. (len은 배열용이라 별도로 제공)"""
    if len(args) != 1 or not isinstance(args[0], str):
        raise DanhaValueError("str_len(str)에는 문자열 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
    return len(args[0])


# --- 타입 변환 ---

def _builtin_parse_int(line, args):
    """문자열 → 정수. parse_int("42") -> 42, parse_int("abc") -> 에러"""
    if len(args) != 1 or not isinstance(args[0], str):
        raise DanhaValueError("parse_int(str)에는 문자열 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
    try:
        return int(args[0])
    except ValueError:
        raise DanhaRuntimeError(f"'{args[0]}'을(를) 정수로 변환할 수 없어", line=line, source=_CURRENT_SOURCE)


def _builtin_parse_float(line, args):
    """문자열 → 실수. parse_float("3.14") -> 3.14"""
    if len(args) != 1 or not isinstance(args[0], str):
        raise DanhaValueError("parse_float(str)에는 문자열 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
    try:
        return float(args[0])
    except ValueError:
        raise DanhaRuntimeError(f"'{args[0]}'을(를) 실수로 변환할 수 없어", line=line, source=_CURRENT_SOURCE)


def _builtin_to_int(line, args):
    """실수 → 정수 (소수점 버림). to_int(3.7) -> 3"""
    if len(args) != 1:
        raise DanhaValueError("to_int(value)에는 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
    if isinstance(args[0], float):
        return int(args[0])
    if isinstance(args[0], int) and not isinstance(args[0], bool):
        return args[0]
    raise DanhaTypeError("to_int의 인자는 숫자여야 해", line=line, source=_CURRENT_SOURCE)


def _builtin_to_float(line, args):
    """정수 → 실수. to_float(3) -> 3.0"""
    if len(args) != 1:
        raise DanhaValueError("to_float(value)에는 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
    if isinstance(args[0], (int, float)) and not isinstance(args[0], bool):
        return float(args[0])
    raise DanhaTypeError("to_float의 인자는 숫자여야 해", line=line, source=_CURRENT_SOURCE)


# 등록
register_builtin('file_read', _builtin_file_read)
register_builtin('file_write', _builtin_file_write)
register_builtin('file_append', _builtin_file_append)
register_builtin('file_exists', _builtin_file_exists)
register_builtin('time', _builtin_time)
register_builtin('clock', _builtin_clock)
register_builtin('split', _builtin_split)
register_builtin('trim', _builtin_trim)
register_builtin('starts_with', _builtin_starts_with)
register_builtin('ends_with', _builtin_ends_with)
register_builtin('replace', _builtin_replace)
register_builtin('char_at', _builtin_char_at)
register_builtin('substr', _builtin_substr)
register_builtin('contains', _builtin_contains)
register_builtin('str_len', _builtin_str_len)
register_builtin('parse_int', _builtin_parse_int)
register_builtin('parse_float', _builtin_parse_float)
register_builtin('to_int', _builtin_to_int)
register_builtin('to_float', _builtin_to_float)


# ===== 35단계: @attribute 조회 내장 함수 =====

def _builtin_has_attribute(line, args):
    """has_attribute("StructName", "serialize") → true/false"""
    if len(args) != 2:
        raise DanhaValueError("has_attribute는 2개의 인자가 필요해 (이름, 어노테이션)", line=line, source=_CURRENT_SOURCE)
    target, attr = args
    if not isinstance(target, str) or not isinstance(attr, str):
        raise DanhaValueError("has_attribute의 인자는 문자열이어야 해", line=line, source=_CURRENT_SOURCE)
    attrs = _ATTRIBUTES.get(target, [])
    return any(a[0] == attr for a in attrs)

register_builtin('has_attribute', _builtin_has_attribute)


def _builtin_get_attributes(line, args):
    """get_attributes("StructName") → 어노테이션 이름 리스트"""
    if len(args) != 1:
        raise DanhaValueError("get_attributes는 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    target = args[0]
    if not isinstance(target, str):
        raise DanhaValueError("get_attributes의 인자는 문자열이어야 해", line=line, source=_CURRENT_SOURCE)
    attrs = _ATTRIBUTES.get(target, [])
    return [a[0] for a in attrs]

register_builtin('get_attributes', _builtin_get_attributes)


def _builtin_get_attribute_args(line, args):
    """get_attribute_args("StructName", "replicated") → 인자 딕셔너리의 값 리스트"""
    if len(args) != 2:
        raise DanhaValueError("get_attribute_args는 2개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    target, attr = args
    attrs = _ATTRIBUTES.get(target, [])
    for a_name, a_args in attrs:
        if a_name == attr:
            return list(a_args.values()) if a_args else []
    return []

register_builtin('get_attribute_args', _builtin_get_attribute_args)


# ===== 59단계: 셀프 호스팅 지원 내장 함수 =====

def _builtin_os_exec(line, args):
    """os_exec(cmd) → i32  외부 명령 실행, 종료 코드 반환."""
    import subprocess
    if len(args) != 1 or not isinstance(args[0], str):
        raise DanhaValueError("os_exec(cmd)에는 문자열 인자 1개가 필요해", line=line, source=_CURRENT_SOURCE)
    result = subprocess.run(args[0], shell=True)
    return result.returncode

def _builtin_get_args(line, args):
    """get_args() → [] 스크립트 인자 목록 반환 (danha selfhost 시 채워짐)."""
    if len(args) != 0:
        raise DanhaValueError("get_args()는 인자가 없어", line=line, source=_CURRENT_SOURCE)
    return list(_SCRIPT_ARGS)

register_builtin('os_exec', _builtin_os_exec)
register_builtin('get_args', _builtin_get_args)


# ===== Stage 70: StringBuilder (mutable buffer로 amortized O(N) 문자열 누적) =====
# 인터프리터에서는 Python list of strings + 지연 join — 컴파일러 표현과 결과만 같으면 OK.

class _DanhaStringBuilder:
    __slots__ = ('parts', '_len')
    def __init__(self):
        self.parts = []
        self._len = 0
    def append(self, s):
        if not isinstance(s, str):
            raise DanhaTypeError("string_builder_append의 두번째 인자는 str이어야 해")
        self.parts.append(s)
        self._len += len(s)
    def to_string(self):
        return ''.join(self.parts)

def _builtin_string_builder(line, args):
    if len(args) != 0:
        raise DanhaValueError("string_builder()는 인자가 없어", line=line, source=_CURRENT_SOURCE)
    return _DanhaStringBuilder()

def _builtin_string_builder_append(line, args):
    if len(args) != 2 or not isinstance(args[0], _DanhaStringBuilder):
        raise DanhaValueError("string_builder_append(sb, str)은 인자 2개가 필요해 (sb는 string_builder()로 생성)", line=line, source=_CURRENT_SOURCE)
    args[0].append(args[1])
    return 0

def _builtin_string_builder_to_string(line, args):
    if len(args) != 1 or not isinstance(args[0], _DanhaStringBuilder):
        raise DanhaValueError("string_builder_to_string(sb)는 string_builder()의 결과를 받아야 해", line=line, source=_CURRENT_SOURCE)
    return args[0].to_string()

def _builtin_string_builder_len(line, args):
    if len(args) != 1 or not isinstance(args[0], _DanhaStringBuilder):
        raise DanhaValueError("string_builder_len(sb)는 string_builder()의 결과를 받아야 해", line=line, source=_CURRENT_SOURCE)
    return args[0]._len

register_builtin('string_builder', _builtin_string_builder)
register_builtin('string_builder_append', _builtin_string_builder_append)
register_builtin('string_builder_to_string', _builtin_string_builder_to_string)
register_builtin('string_builder_len', _builtin_string_builder_len)


# ===== 48단계: 테스트 내장 함수 =====

def _builtin_assert(line, args):
    if len(args) != 1:
        raise DanhaValueError("assert는 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    if not args[0]:
        raise AssertionError("assert 실패: 조건이 거짓이야")

def _builtin_assert_eq(line, args):
    if len(args) != 2:
        raise DanhaValueError("assert_eq는 2개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    if args[0] != args[1]:
        raise AssertionError(f"assert_eq 실패: {args[0]!r} != {args[1]!r}")

def _builtin_assert_ne(line, args):
    if len(args) != 2:
        raise DanhaValueError("assert_ne는 2개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    if args[0] == args[1]:
        raise AssertionError(f"assert_ne 실패: {args[0]!r} == {args[1]!r}")

register_builtin('assert', _builtin_assert)
register_builtin('assert_eq', _builtin_assert_eq)
register_builtin('assert_ne', _builtin_assert_ne)


# ===== 52단계: 소켓/네트워킹 내장 함수 =====

def _builtin_net_connect(line, args):
    """TCP 연결. net_connect(host, port) → socket_id"""
    import socket as _socket
    if len(args) != 2:
        raise DanhaValueError("net_connect는 2개의 인자가 필요해 (host, port)", line=line, source=_CURRENT_SOURCE)
    host, port = str(args[0]), int(args[1])
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.connect((host, port))
    sid = id(s)
    _SOCKETS[sid] = s
    return sid

def _builtin_net_listen(line, args):
    """TCP 서버 소켓. net_listen(port) → server_id"""
    import socket as _socket
    if len(args) != 1:
        raise DanhaValueError("net_listen은 1개의 인자가 필요해 (port)", line=line, source=_CURRENT_SOURCE)
    port = int(args[0])
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    s.bind(('', port))
    s.listen(5)
    sid = id(s)
    _SOCKETS[sid] = s
    return sid

def _builtin_net_accept(line, args):
    """연결 수락. net_accept(server_id) → client_id"""
    if len(args) != 1:
        raise DanhaValueError("net_accept는 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    sid = args[0]
    server = _SOCKETS.get(sid)
    if server is None:
        raise DanhaValueError("유효하지 않은 소켓 ID", line=line, source=_CURRENT_SOURCE)
    conn, _ = server.accept()
    cid = id(conn)
    _SOCKETS[cid] = conn
    return cid

def _builtin_net_send(line, args):
    """데이터 전송. net_send(socket_id, data) → 0"""
    if len(args) != 2:
        raise DanhaValueError("net_send는 2개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    sid, data = args[0], str(args[1])
    s = _SOCKETS.get(sid)
    if s is None:
        raise DanhaValueError("유효하지 않은 소켓 ID", line=line, source=_CURRENT_SOURCE)
    s.sendall(data.encode('utf-8'))
    return 0

def _builtin_net_recv(line, args):
    """데이터 수신. net_recv(socket_id, size) → str"""
    if len(args) != 2:
        raise DanhaValueError("net_recv는 2개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    sid, size = args[0], int(args[1])
    s = _SOCKETS.get(sid)
    if s is None:
        raise DanhaValueError("유효하지 않은 소켓 ID", line=line, source=_CURRENT_SOURCE)
    data = s.recv(size)
    return data.decode('utf-8', errors='replace')

def _builtin_net_close(line, args):
    """소켓 닫기. net_close(socket_id)"""
    if len(args) != 1:
        raise DanhaValueError("net_close는 1개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    sid = args[0]
    s = _SOCKETS.pop(sid, None)
    if s:
        s.close()
    return 0

def _builtin_net_udp_socket(line, args):
    """UDP 소켓 생성. net_udp_socket() → socket_id"""
    import socket as _socket
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    sid = id(s)
    _SOCKETS[sid] = s
    return sid

def _builtin_net_udp_send(line, args):
    """UDP 전송. net_udp_send(socket_id, host, port, data) → 0"""
    import socket as _socket
    if len(args) != 4:
        raise DanhaValueError("net_udp_send는 4개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    sid, host, port, data = args[0], str(args[1]), int(args[2]), str(args[3])
    s = _SOCKETS.get(sid)
    if s is None:
        raise DanhaValueError("유효하지 않은 소켓 ID", line=line, source=_CURRENT_SOURCE)
    s.sendto(data.encode('utf-8'), (host, port))
    return 0

def _builtin_net_udp_recv(line, args):
    """UDP 수신. net_udp_recv(socket_id, size) → str"""
    if len(args) != 2:
        raise DanhaValueError("net_udp_recv는 2개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    sid, size = args[0], int(args[1])
    s = _SOCKETS.get(sid)
    if s is None:
        raise DanhaValueError("유효하지 않은 소켓 ID", line=line, source=_CURRENT_SOURCE)
    data, _ = s.recvfrom(size)
    return data.decode('utf-8', errors='replace')

def _builtin_net_set_timeout(line, args):
    """소켓 타임아웃 설정. net_set_timeout(socket_id, seconds)"""
    if len(args) != 2:
        raise DanhaValueError("net_set_timeout는 2개의 인자가 필요해", line=line, source=_CURRENT_SOURCE)
    sid, timeout = args[0], float(args[1])
    s = _SOCKETS.get(sid)
    if s:
        s.settimeout(timeout if timeout > 0 else None)
    return 0

register_builtin('net_connect', _builtin_net_connect)
register_builtin('net_listen', _builtin_net_listen)
register_builtin('net_accept', _builtin_net_accept)
register_builtin('net_send', _builtin_net_send)
register_builtin('net_recv', _builtin_net_recv)
register_builtin('net_close', _builtin_net_close)
register_builtin('net_udp_socket', _builtin_net_udp_socket)
register_builtin('net_udp_send', _builtin_net_udp_send)
register_builtin('net_udp_recv', _builtin_net_udp_recv)
register_builtin('net_set_timeout', _builtin_net_set_timeout)


# ===== 9.1b: 모듈 시스템 =====

class ModuleNamespace:
    """
    import된 모듈의 네임스페이스.
    math 모듈을 import하면, math.sin() 같이 접근할 수 있게 해주는 객체.
    내부적으로는 딕셔너리처럼 동작한다.
    """
    def __init__(self, name, members):
        self.name = name        # 모듈 이름 (예: 'math')
        self.members = members  # {이름: 값} 딕셔너리
    
    def get(self, field_name):
        if field_name in self.members:
            return self.members[field_name]
        raise KeyError(f"모듈 '{self.name}'에 '{field_name}'이 없어")
    
    def __repr__(self):
        return f"<module '{self.name}'>"

# 모듈 캐시: 같은 모듈을 두 번 로드하지 않기 위해.
# run()이 호출될 때마다 초기화됨.
_MODULE_CACHE = {}

# 현재 로딩 중인 모듈 경로 스택: 순환 임포트 감지용.
_MODULE_LOADING = set()

# 모듈 파일을 찾을 기본 디렉토리.
# run() 호출 시 설정됨.
_MODULE_BASE_DIR = None


def _resolve_module_path(module_name, base_dir=None):
    """
    모듈 이름을 실제 파일 경로로 변환.
    import math → ./math.dh
    import physics.collision → ./physics/collision.dh
    base_dir가 주어지면 그 폴더 기준으로 찾음.
    """
    import os
    parts = module_name.split('.')
    rel_path = os.path.join(*parts) + '.dh'
    if base_dir:
        full_path = os.path.join(base_dir, rel_path)
    else:
        full_path = rel_path
    return full_path


def _load_module(module_name, line, base_dir=None):
    """
    모듈을 로드한다:
    1. 캐시 확인 → 있으면 바로 반환
    2. 순환 감지 → 로딩 중이면 에러
    3. 파일 읽기 → 파싱 → 실행 → 네임스페이스 수집
    4. 캐시에 저장
    """
    import os
    global _MODULE_CACHE, _MODULE_LOADING, _MODULE_BASE_DIR
    
    if base_dir is None:
        base_dir = _MODULE_BASE_DIR
    
    # 이미 로드된 모듈이면 캐시에서 반환
    if module_name in _MODULE_CACHE:
        return _MODULE_CACHE[module_name]
    
    # 순환 임포트 감지
    if module_name in _MODULE_LOADING:
        raise DanhaImportError(
            f"순환 임포트를 감지했어: '{module_name}'이 "
            f"자기 자신을 (직접 또는 간접으로) import하고 있어",
            line=line, source=_CURRENT_SOURCE
        )
    
    # 파일 찾기 — danha.toml 의존성 → 소스 디렉토리 → 단아 설치 디렉토리 순으로 탐색
    import sys as _sys
    danha_dir = os.path.dirname(os.path.abspath(_sys.modules[__name__].__file__))

    # 47단계: danha.toml 패키지 경로 탐색
    pkg_path = None
    if base_dir:
        try:
            from danha_pkg import load_manifest, resolve_dependencies
            manifest = load_manifest(base_dir)
            if manifest:
                deps = resolve_dependencies(manifest, base_dir)
                if module_name in deps:
                    dep_dir = deps[module_name]
                    # 패키지 디렉토리 안의 동명 .dh 파일 또는 index.dh
                    for candidate_name in [module_name + '.dh', 'index.dh']:
                        c = os.path.join(dep_dir, candidate_name)
                        if os.path.exists(c):
                            pkg_path = c
                            break
                    if not pkg_path and os.path.isfile(dep_dir + '.dh'):
                        pkg_path = dep_dir + '.dh'
        except Exception:
            pass

    file_path = pkg_path
    if file_path is None:
        for search_dir in filter(None, [base_dir, os.getcwd(), danha_dir]):
            candidate = _resolve_module_path(module_name, search_dir)
            if os.path.exists(candidate):
                file_path = candidate
                break

    if file_path is None:
        fallback = _resolve_module_path(module_name, base_dir)
        raise DanhaNameError(
            f"모듈 '{module_name}'을 찾을 수 없어. "
            f"'{fallback}' 파일이 존재하지 않아",
            line=line, source=_CURRENT_SOURCE
        )
    
    # 파일 읽기
    with open(file_path, 'r', encoding='utf-8') as f:
        source = f.read()
    
    # 로딩 시작 표시 (순환 감지용)
    _MODULE_LOADING.add(module_name)
    
    try:
        # 모듈 전용 스코프에서 실행
        tokens = lex(source)
        tree = parse(tokens)
        module_scope = Scope()
        # 내장 함수도 모듈에서 쓸 수 있게
        builtin_originals = {}
        for name, builtin in BUILTINS.items():
            module_scope.declare(name, builtin)
            builtin_originals[name] = builtin
        evaluate(tree, module_scope)
        
        # 모듈 스코프에서 사용자 정의 항목 수집.
        # 내장 함수와 같은 이름이라도 값이 바뀌었으면 수집한다.
        # (사용자가 내장 이름을 덮어쓴 경우)
        members = {}
        for name, value in module_scope.vars.items():
            if name in builtin_originals and value is builtin_originals[name]:
                continue  # 값이 안 바뀐 내장 함수는 건너뜀
            members[name] = value
        
        ns = ModuleNamespace(module_name, members)
        _MODULE_CACHE[module_name] = ns
        return ns
    finally:
        _MODULE_LOADING.discard(module_name)


def _debug_break(line, node, scope):
    """53단계: 디버거 인터랙티브 프롬프트."""
    global _DEBUG_STEP, _DEBUG_BREAKPOINTS
    node_desc = node[0] if isinstance(node, tuple) else str(node)
    print(f"\n[디버거] {line}번째 줄 ({node_desc})")
    # 소스 라인 표시
    if _CURRENT_SOURCE:
        lines = _CURRENT_SOURCE.splitlines()
        if 1 <= line <= len(lines):
            print(f"  > {lines[line-1]}")
    while True:
        try:
            cmd = input("(dnh) ").strip()
        except (EOFError, KeyboardInterrupt):
            cmd = 'q'
        if not cmd:
            continue
        parts = cmd.split()
        op = parts[0]
        if op in ('c', 'continue'):
            _DEBUG_STEP[0] = False
            break
        elif op in ('n', 'next', 's', 'step'):
            _DEBUG_STEP[0] = True
            break
        elif op in ('b', 'break') and len(parts) == 2:
            try:
                _DEBUG_BREAKPOINTS.add(int(parts[1]))
                print(f"  브레이크포인트 추가: {parts[1]}번째 줄")
            except ValueError:
                print("  줄 번호를 숫자로 입력해줘")
        elif op in ('d', 'delete') and len(parts) == 2:
            try:
                _DEBUG_BREAKPOINTS.discard(int(parts[1]))
            except ValueError:
                pass
        elif op in ('p', 'print') and len(parts) >= 2:
            var_name = ' '.join(parts[1:])
            try:
                val = scope.get(var_name)
                print(f"  {var_name} = {format_value(val)}")
            except Exception:
                print(f"  '{var_name}'을(를) 찾을 수 없어")
        elif op in ('l', 'list'):
            if _CURRENT_SOURCE:
                src_lines = _CURRENT_SOURCE.splitlines()
                start = max(0, line - 4)
                end = min(len(src_lines), line + 3)
                for i, ln in enumerate(src_lines[start:end], start + 1):
                    marker = '=>' if i == line else '  '
                    print(f"  {marker} {i:4d}: {ln}")
        elif op in ('bt', 'backtrace'):
            print("  (백트레이스는 인터프리터에서 지원되지 않아)")
        elif op in ('q', 'quit'):
            print("  디버거 종료")
            raise KeyboardInterrupt
        elif op == 'h':
            print("  명령어: c(ontinue), n(ext/step), b <줄> (브레이크), d <줄> (삭제),")
            print("          p <변수> (출력), l(ist), bt (스택), q(uit)")
        else:
            print(f"  알 수 없는 명령: {cmd!r} — h로 도움말")


def run(source_code, base_dir=None, script_args=None):
    tokens = lex(source_code)
    tree = parse(tokens, source_code=source_code)
    global_scope = Scope()
    # 7.12b: 매 실행마다 World 초기화. 테스트 간 상태 누수 방지.
    global _WORLD, _SYSTEM_REGISTRY, _MODULE_CACHE, _MODULE_LOADING, _MODULE_BASE_DIR
    global _CURRENT_SOURCE, _ATTRIBUTES, _TEST_RESULTS, _PROFILE_STATS, _SOCKETS
    global _SCRIPT_ARGS
    _CURRENT_SOURCE = source_code
    _SCRIPT_ARGS = list(script_args) if script_args else []
    _UNSAFE_DEPTH[0] = 0
    _WORLD = _World()
    _SYSTEM_REGISTRY = {}
    _ATTRIBUTES = {}
    _MODULE_CACHE = {}
    _MODULE_LOADING = set()
    _MODULE_BASE_DIR = base_dir
    _TEST_RESULTS = []
    _PROFILE_STATS = {}
    # 소켓 정리
    for _s in _SOCKETS.values():
        try: _s.close()
        except Exception: pass
    _SOCKETS = {}
    # 내장 함수 등록
    for name, builtin in BUILTINS.items():
        global_scope.declare(name, builtin)
    try:
        result = evaluate(tree, global_scope)
    except RecursionError:
        raise DanhaRuntimeError(
            "재귀가 너무 깊어. 함수가 자기 자신을 너무 많이 불렀어. "
            "(Danha 인터프리터는 호스트 언어의 스택 한도에 묶여 있어. "
            "네이티브 컴파일로 가면 이 제한은 없어져.)"
        )
    except BreakSignal:
        raise DanhaRuntimeError("'break'는 루프 안에서만 쓸 수 있어")
    except ContinueSignal:
        raise DanhaRuntimeError("'continue'는 루프 안에서만 쓸 수 있어")
    return result
