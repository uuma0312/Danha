# danha_compile.py
# Danha의 LLVM 백엔드 (6단계)
#
# 6-1 범위: 정수 리터럴, + - * / %, 괄호, print(식)
# 그 외는 다 NotImplementedError를 던진다. 일부러.
# 작은 것부터 단단하게 만들고 위에 쌓는다.
#
# 인터프리터(danha_evaluator.py)와 별개의 파일이다. 둘은 나란히 살아간다.
# 같은 AST를 인터프리터는 "직접 실행"하고, 컴파일러는 "기계어로 번역"한다.

import sys
import ctypes
from llvmlite import ir
from lexer import lex
from danha_parser import parse
from danha_errors import (
    DanhaError, DanhaSyntaxError, DanhaTypeError, DanhaNameError,
    DanhaValueError, DanhaImportError, DanhaECSError, DanhaRuntimeError,
    DanhaComptimeError,
)


# llvmlite.binding은 네이티브 LLVM 래퍼 — JIT(run)과 내장 오브젝트 생성에만 필요.
# AOT는 텍스트 .ll + 외부 clang 경로(emit_object의 clang 백엔드)로도 가능하므로
# binding이 없어도 모듈 로드는 성공해야 한다 (llvmlite.ir은 순수 파이썬).
try:
    from llvmlite import binding as llvm
    # LLVM 초기화 (지금 도는 컴퓨터 용 타겟 등록).
    # 모듈 로드 시 한 번만 일어나도록 모듈 레벨에 둠.
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()
    _HAS_LLVM_BINDING = True
except Exception:
    llvm = None
    _HAS_LLVM_BINDING = False


def _require_llvm_binding(what):
    if not _HAS_LLVM_BINDING:
        raise DanhaRuntimeError(
            f"{what}에는 llvmlite 네이티브 바인딩이 필요해. "
            "binding 없는 환경에서는 `danha compile --clang`(외부 clang 경로)을 사용해줘."
        )


def _default_triple():
    """binding 없이도 쓸 수 있는 타겟 트리플 결정."""
    if _HAS_LLVM_BINDING:
        return llvm.get_default_triple()
    import platform
    m = platform.machine().lower()
    arch = {'amd64': 'x86_64', 'x86_64': 'x86_64',
            'arm64': 'aarch64', 'aarch64': 'aarch64'}.get(m, m)
    s = platform.system()
    if s == 'Windows':
        return f'{arch}-pc-windows-msvc'
    if s == 'Darwin':
        return f'{arch}-apple-darwin'
    return f'{arch}-pc-linux-gnu'


# ===== 타입 별칭 =====
# 6-1에서는 정수만 있다. 32비트로 고정.
# 6-2 이후 실수가 들어오면 i32와 double을 구분하게 됨.
# 7.15f: ECS 컴포넌트에 다양한 타입 허용을 위해 크기별 정수 + f32 추가.
i32 = ir.IntType(32)
i8 = ir.IntType(8)
i16 = ir.IntType(16)
i64 = ir.IntType(64)
i8p = i8.as_pointer()
i1 = ir.IntType(1)   # 불리언. 비교 명령어 결과의 타입.
f64 = ir.DoubleType()  # 64비트 실수 (IEEE 754 double). Danha의 'f64' 타입.
f32 = ir.FloatType()   # 32비트 실수 (IEEE 754 single). Danha의 'f32' 타입.


class Compiler:
    """
    AST를 받아 LLVM 모듈을 만든다.
    
    인터프리터의 evaluate(node, scope)와 평행한 구조:
    - evaluate는 파이썬 값을 돌려준다
    - compile_expr은 ir.Value (IR 레지스터를 가리키는 핸들)를 돌려준다
    
    'IR 레지스터를 가리키는 핸들'이란, "이 자리에 들어갈 값"이라는 약속표 같은 거야.
    아직 진짜 계산이 일어나진 않았고, 우리가 builder에 차곡차곡 쌓아둔
    명령어들이 나중에 LLVM에 의해 진짜 기계어로 번역돼서 실행될 때 비로소 값이 생긴다.
    """
    
    def __init__(self, runtime_mode='libc', entry_arg_mode=False):
        if runtime_mode not in ('libc', 'direct-os'):
            raise DanhaValueError(f"알 수 없는 런타임 모드: {runtime_mode}")
        self.runtime_mode = runtime_mode
        self.entry_arg_mode = entry_arg_mode
        self.main_argc = None
        self.main_argv = None
        self.module = ir.Module(name="danha_program", context=ir.context.Context())
        self.module.triple = _default_triple()
        self.argc_global = None
        self.argv_global = None
        if self.entry_arg_mode:
            self.argc_global = ir.GlobalVariable(self.module, i32, name="_danha_argc")
            self.argc_global.linkage = "internal"
            self.argc_global.global_constant = False
            self.argc_global.initializer = ir.Constant(i32, 0)
            self.argv_global = ir.GlobalVariable(self.module, i8p.as_pointer(), name="_danha_argv")
            self.argv_global.linkage = "internal"
            self.argv_global.global_constant = False
            self.argv_global.initializer = ir.Constant(i8p.as_pointer(), None)
        
        # 12단계: 에러 메시지에 소스 코드 줄을 포함하기 위한 속성.
        # compile_module()에서 설정됨.
        self._source_code = None
        
        # printf 외부 함수 선언.
        # direct-os 모드에서는 print의 단순 갈래를 OS API로 내보내고,
        # 호환 모드에서는 기존 C printf 경로를 유지한다.
        printf_ty = ir.FunctionType(i32, [i8p], var_arg=True)
        if self.runtime_mode == 'direct-os':
            self.printf = self._define_direct_os_i32_stub("_danha_os_printf_stub", [i8p], var_arg=True)
        else:
            self.printf = ir.Function(self.module, printf_ty, name="printf")
        
        # 모듈 전역에 박을 문자열 상수들.
        # printf의 포맷 문자열과 "true"/"false" 표시용 문자열.
        # 같은 print를 여러 번 호출해도 글자 자체는 한 번만 메모리에 박혀 재사용된다.
        self.fmt_int = self._make_global_string("fmt_int", "%d\n\0")
        # P1B: i64 출력용 (Windows에서 %lld가 64-bit 정수, %I64d도 호환)
        self.fmt_lld = self._make_global_string("fmt_lld", "%lld\n\0")
        # 실수 포맷: %g는 상황에 맞게 짧게 출력 (3.14는 "3.14", 1000000.0은 "1e+06").
        # 파이썬 str()과 완벽히 같진 않지만 가장 가까운 선택.
        # 인터프리터는 10.0을 "10.0"으로 찍고 컴파일러는 "10"으로 찍는 차이가 있지만
        # 값의 의미는 같다. 테스트에서 정상화해서 비교.
        self.fmt_float = self._make_global_string("fmt_float", "%g\n\0")
        self.str_true = self._make_global_string("str_true", "true\n\0")
        self.str_false = self._make_global_string("str_false", "false\n\0")
        # 문자열 보간: 인터프리터와 같이 줄바꿈 없이 "true"/"false"
        self.str_true_plain = self._make_global_string("str_true_plain", "true\0")
        self.str_false_plain = self._make_global_string("str_false_plain", "false\0")
        # %s 포맷: true/false를 찍을 때는 정수 대신 문자열 포맷을 써야 한다.
        # 단, 우리 true/false 문자열에 이미 "\n"이 들어 있어서 %s 도 "%s\0"으로 충분.
        self.fmt_str = self._make_global_string("fmt_str", "%s\0")
        # 7.15c: 문자열 print용 — 줄바꿈 포함.
        self.fmt_str_nl = self._make_global_string("fmt_str_nl", "%s\n\0")
        
        # 8.2: to_string용 format 문자열
        self.fmt_to_str_int = self._make_global_string("fmt_to_str_int", "%d\0")
        self.fmt_to_str_i64 = self._make_global_string("fmt_to_str_i64", "%lld\0")
        self.fmt_to_str_float = self._make_global_string("fmt_to_str_float", "%g\0")
        
        # ----- 6-6b: 함수 컴파일 인프라 -----
        # 사용자 함수 이름 → ir.Function 매핑.
        # 두 단계 통과의 1단계에서 채워지고, 2단계에서 본문 컴파일과 호출에 사용.
        # 두 단계로 가는 이유: 재귀(자기 자신 호출)와 상호 재귀(서로 호출)를 위해서.
        # 본문을 컴파일하기 *전에* 모든 함수 이름이 등록돼 있어야 함.
        self.functions = {}
        # 15: 람다 카운터 — 고유 이름 생성용
        self._lambda_counter = 0
        self._lambda_captures = {}  # lambda_name -> [(capture_name, capture_llvm_val)]
        # 7.1.3: 각 함수의 매개변수별 참조 종류(list) — 호출 사이트 검사에 사용.
        # 예: fn(name) = [None, 'ref', 'mut_ref']
        self.fn_param_ref_kinds = {}
        # 7.1.3: 메서드도 동일. 키는 (struct_name, method_name).
        self.method_param_ref_kinds = {}
        # 7.17a: 가변인자 함수 여부 — 호출 사이트에서 인자 개수 검사를 건너뛸 때 사용.
        self.fn_is_vararg = {}
        # 7.17c: @clink로 선언된 라이브러리 이름 목록. AOT 링크 시 자동 플래그 생성에 활용.
        self.clink_libs = []
        
        # 6.10: 구조체 저장소.
        # 같은 모듈 안에서 'struct P'가 두 번 나오면 에러를 내야 하지만,
        # 다른 모듈/다른 컴파일에서 같은 이름이 나오는 건 OK.
        # LLVM의 명명된 타입은 컨텍스트 전역이라, 컴파일러 인스턴스마다 고유 접미사를 붙여
        # 충돌을 피한다. 이름은 디버깅용이지 의미에 영향 없음.
        self.structs = {}
        self.unions = set()   # union 이름 집합 — field access 시 bitcast 경로 판별용
        self._reflection_persistence_defs = {}
        self._struct_suffix = f"_{id(self):x}"
        
        # 6.11: 메서드 저장소.
        # (struct_name, method_name) → ir.Function
        # 메서드는 사실상 일반 함수인데 첫 인자가 구조체 포인터(self).
        # impl이 여러 번 같은 메서드 이름을 정의하면 뒤에 나온 게 덮어씀
        # (인터프리터와 같은 정책).
        self.methods = {}
        
        # 7.12d2: 컴포넌트 저장소.
        # key: 컴포넌트 이름 (예: 'Position')
        # value: {
        #   'fields': [필드이름, ...]                     필드 순서 보존
        #   'field_globals': {필드이름: GlobalVariable},  f64* 포인터 (SoA 배열)
        #   'dense_to_entity': GlobalVariable,            i32* (각 슬롯 → entity idx)
        #   'sparse': GlobalVariable,                     i32* (entity idx → dense idx, -1이면 없음)
        #   'count': GlobalVariable,                      i32
        #   'capacity': GlobalVariable,                   i32
        # }
        # 7.12d2 단순화: 필드 타입은 모두 f64. 정수/혼합은 나중에.
        self.components = {}
        # 7.15b: enum 저장소. {name: {variant_name: i32_index, ...}}
        self.enums = {}
        # 8.3: tagged enum 저장소.
        # {name: (llvm_type, {variant_name: (tag, [llvm_type, ...] or None)})}
        self.tagged_enums = {}
        # 9.1c: 모듈 시스템.
        # _modules: {모듈이름: {원래이름: 접두사이름}} — import된 모듈의 이름 매핑
        # _from_imports: {가져온이름: 접두사이름} — from ... import로 가져온 직접 이름
        self._modules = {}
        self._from_imports = {}
        # 고정 용량 (ECS World와 동일 기준).
        # Stage 75d: 게임엔진 실용성 위해 4096 → 65536. 메모리 ~1MB/component, 64K 엔티티까지 ECS 운용 가능.
        self.COMPONENT_CAPACITY = 65536
        
        # ----- 7.4: 동적 배열용 C 런타임 함수 선언 -----
        # malloc(size) → void*: size 바이트만큼 힙에 메모리 확보
        # realloc(ptr, new_size) → void*: 기존 메모리를 new_size로 확장/축소
        # free(ptr): 더 이상 안 쓰는 메모리 반납
        # 비유: malloc = 창고 빌리기, realloc = 더 큰 창고로 이사, free = 창고 반납
        i64 = ir.IntType(64)
        void_ptr = i8p  # C의 void* = i8*
        
        malloc_ty = ir.FunctionType(void_ptr, [i64])
        realloc_ty = ir.FunctionType(void_ptr, [void_ptr, i64])
        free_ty = ir.FunctionType(ir.VoidType(), [void_ptr])
        if self.runtime_mode == 'direct-os':
            self._declare_direct_os_runtime()
        else:
            self.malloc_fn = ir.Function(self.module, malloc_ty, name="malloc")
            self.realloc_fn = ir.Function(self.module, realloc_ty, name="realloc")
            self.free_fn = ir.Function(self.module, free_ty, name="free")
        
        # 7.5: memcpy — 아레나 기반 용량 확장 시 기존 데이터 복사에 필요.
        # realloc은 "크기 변경 + 복사"를 한 번에 해주지만,
        # 아레나에서는 새 위치를 잡고 직접 복사해야 함.
        memcpy_ty = ir.FunctionType(void_ptr, [void_ptr, void_ptr, i64])
        if self.runtime_mode == 'direct-os' and self.module.globals.get("memcpy") is not None:
            self.memcpy_fn = self.module.get_global("memcpy")
        else:
            self.memcpy_fn = ir.Function(self.module, memcpy_ty, name="memcpy")
        
        # 30: memset — HashMap 초기화에 필요.
        # llvm.memset.p0.i64(dest, val, len, isvolatile)
        memset_ty = ir.FunctionType(ir.VoidType(), [void_ptr, i8, i64, ir.IntType(1)])
        self.memset_fn = ir.Function(self.module, memset_ty, name="llvm.memset.p0.i64")
        
        # 30: sprintf — 정수 키를 문자열로 변환할 때 사용.
        sprintf_ty = ir.FunctionType(i32, [void_ptr, void_ptr], var_arg=True)
        if self.runtime_mode == 'direct-os':
            self.sprintf_fn = self._define_direct_os_i32_stub("sprintf", [void_ptr, void_ptr], var_arg=True)
        else:
            self.sprintf_fn = ir.Function(self.module, sprintf_ty, name="sprintf")
        
        # 7.8a: sqrt — 벡터 길이(length), 정규화(normalize)에 필요.
        # C 표준 라이브러리의 sqrt(double) → double.
        sqrt_ty = ir.FunctionType(f64, [f64])
        if self.runtime_mode == 'direct-os':
            self.sqrt_fn = self._define_direct_os_f64_stub("sqrt", [f64])
        else:
            self.sqrt_fn = ir.Function(self.module, sqrt_ty, name="sqrt")
        # Phase 4: f32 변형 (vec*f.length)
        sqrtf_ty = ir.FunctionType(f32, [f32])
        if self.runtime_mode == 'direct-os':
            self.sqrtf_fn = self._define_direct_os_f32_stub("sqrtf", [f32])
        else:
            self.sqrtf_fn = ir.Function(self.module, sqrtf_ty, name="sqrtf")
        
        # 7.9b: sin, cos — 회전 행렬에 필요.
        sin_ty = ir.FunctionType(f64, [f64])
        if self.runtime_mode == 'direct-os':
            self.sin_fn = self._define_direct_os_f64_stub("sin", [f64])
        else:
            self.sin_fn = ir.Function(self.module, sin_ty, name="sin")
        cos_ty = ir.FunctionType(f64, [f64])
        if self.runtime_mode == 'direct-os':
            self.cos_fn = self._define_direct_os_f64_stub("cos", [f64])
        else:
            self.cos_fn = ir.Function(self.module, cos_ty, name="cos")
        
        # 7.15c: strcmp — 문자열 비교. strcmp(a, b) → i32 (0이면 같음)
        strcmp_ty = ir.FunctionType(i32, [i8p, i8p])
        if self.runtime_mode == 'direct-os':
            self.strcmp_fn = self._declare_direct_os_strcmp()
        else:
            self.strcmp_fn = ir.Function(self.module, strcmp_ty, name="strcmp")
        
        # 8.2: strlen — 문자열 길이. strlen(s) → i64
        i64 = ir.IntType(64)
        strlen_ty = ir.FunctionType(i64, [i8p])
        if self.runtime_mode == 'direct-os':
            self.strlen_fn = self._os_strlen_fn
        else:
            self.strlen_fn = ir.Function(self.module, strlen_ty, name="strlen")
        
        # 8.2: snprintf — 포맷 문자열 출력. snprintf(buf, size, fmt, ...) → i32
        snprintf_ty = ir.FunctionType(i32, [i8p, i64, i8p], var_arg=True)
        if self.runtime_mode == 'direct-os':
            self.snprintf_fn = self._define_direct_os_i32_stub("snprintf", [i8p, i64, i8p], var_arg=True)
        else:
            self.snprintf_fn = ir.Function(self.module, snprintf_ty, name="snprintf")

        # parse_int(str) → i32 — atoi 기반.
        atoi_ty = ir.FunctionType(i32, [i8p])
        if self.runtime_mode == 'direct-os':
            self.atoi_fn = self._declare_direct_os_atoi()
        else:
            self.atoi_fn = ir.Function(self.module, atoi_ty, name="atoi")

        self._define_core_string_index_runtime()
        
        # ----- 7.5: 아레나 글로벌 변수 -----
        # 아레나 구조체: { base: i8*, offset: i32, capacity: i32 }
        # 글로벌로 만들어야 사용자 함수 안에서도 push가 아레나에 접근 가능.
        # main 시작에 초기화, main 끝에 free.
        self.arena_type = ir.LiteralStructType([i8p, i32, i32])
        arena_init = ir.Constant(self.arena_type, [
            ir.Constant(i8p, None),
            ir.Constant(i32, 0),
            ir.Constant(i32, 0),
        ])
        self.arena_slot = ir.GlobalVariable(self.module, self.arena_type, name="_danha_arena")
        self.arena_slot.linkage = "internal"
        self.arena_slot.initializer = arena_init
        # 아레나 할당 런타임 (경계 검사 + 청크 성장) — 모든 _arena_alloc이 이 함수를 거침
        self._define_arena_runtime()

        # ----- 7.12d1: ECS World 글로벌 + EntityId 타입 -----
        # EntityId = {i32 index, i32 gen}. 값으로 주고받음.
        # LLVM은 작은 구조체 반환을 레지스터 쌍으로 최적화해준다.
        self.entity_id_type = ir.LiteralStructType([i32, i32])
        
        # World 레이아웃:
        #   gens:       i32*    슬롯별 현재 세대 (아레나 할당)
        #   alive:      i8*     슬롯별 생존 (0/1)
        #   count:      i32     지금까지 확장된 슬롯 수 (<= capacity)
        #   capacity:   i32     gens/alive 배열의 실제 크기
        #   free_list:  i32*    재사용 가능한 index 스택 (아레나 할당)
        #   free_count: i32     스택 높이
        #
        # 7.12d1에서는 고정 용량으로 시작. 초과 시 printf 후 abort.
        # 동적 성장은 7.15 최적화 단계에서 (동적 배열의 realloc과 같은 방식으로).
        self.ecs_world_type = ir.LiteralStructType([
            i32.as_pointer(),   # gens
            i8p,                # alive (i8*)
            i32,                # count
            i32,                # capacity
            i32.as_pointer(),   # free_list
            i32,                # free_count
        ])
        world_init = ir.Constant(self.ecs_world_type, [
            ir.Constant(i32.as_pointer(), None),
            ir.Constant(i8p, None),
            ir.Constant(i32, 0),
            ir.Constant(i32, 0),
            ir.Constant(i32.as_pointer(), None),
            ir.Constant(i32, 0),
        ])
        self.ecs_world = ir.GlobalVariable(self.module, self.ecs_world_type, name="_danha_ecs_world")
        self.ecs_world.linkage = "internal"
        self.ecs_world.initializer = world_init
        
        # 고정 용량 (MVP). 4096 엔티티면 프로토타입엔 충분.
        # Stage 75d: 게임엔진 실용성 위해 4096 → 65536. gens/alive/free_list 합쳐 ~640KB.
        self.ECS_CAPACITY = 65536
        
        # 런타임 함수 선언 (본문은 나중에 _define_ecs_runtime에서 작성).
        # C에서 구현해도 되지만, 단아가 '자족형'이 되도록 LLVM IR로 직접.
        spawn_ty = ir.FunctionType(self.entity_id_type, [])
        self.ecs_spawn_fn = ir.Function(self.module, spawn_ty, name="_danha_ecs_spawn")
        
        destroy_ty = ir.FunctionType(ir.IntType(1), [self.entity_id_type])
        self.ecs_destroy_fn = ir.Function(self.module, destroy_ty, name="_danha_ecs_destroy")
        
        is_alive_ty = ir.FunctionType(ir.IntType(1), [self.entity_id_type])
        self.ecs_is_alive_fn = ir.Function(self.module, is_alive_ty, name="_danha_ecs_is_alive")
        
        # World 초기화 함수 (main 시작에서 호출).
        init_ty = ir.FunctionType(ir.VoidType(), [])
        self.ecs_init_fn = ir.Function(self.module, init_ty, name="_danha_ecs_init")
        
        # EntityId print 포맷: "Entity(%d, %d)\n"
        self.fmt_entity = self._make_global_string("fmt_entity", "Entity(%d, %d)\n\0")
        
        # 이 함수들의 본문은 _define_ecs_runtime에서 채워짐 (compile_program 시작 때 호출).
        
        # 30: HashMap 런타임 함수 선언
        # HashMap은 불투명 포인터(i8*)로 표현. 내부는 C의 구조체를 malloc으로 할당.
        # 키: i8* (문자열), 값: i64 (정수/실수를 bitcast)
        # 해시 테이블: 오픈 어드레싱, 선형 탐사
        self.HASHMAP_INIT_CAP = 1024
        
        hm_new_ty = ir.FunctionType(i8p, [])
        self.hm_new_fn = ir.Function(self.module, hm_new_ty, name="_danha_hm_new")

        hm_new_cap_ty = ir.FunctionType(i8p, [i32])
        self.hm_new_cap_fn = ir.Function(self.module, hm_new_cap_ty, name="_danha_hm_new_cap")
        
        hm_set_ty = ir.FunctionType(i8p, [i8p, i8p, i64])
        self.hm_set_fn = ir.Function(self.module, hm_set_ty, name="_danha_hm_set")
        
        hm_get_ty = ir.FunctionType(i64, [i8p, i8p])
        self.hm_get_fn = ir.Function(self.module, hm_get_ty, name="_danha_hm_get")
        
        hm_has_ty = ir.FunctionType(ir.IntType(1), [i8p, i8p])
        self.hm_has_fn = ir.Function(self.module, hm_has_ty, name="_danha_hm_has")
        
        hm_remove_ty = ir.FunctionType(ir.IntType(1), [i8p, i8p])
        self.hm_remove_fn = ir.Function(self.module, hm_remove_ty, name="_danha_hm_remove")
        
        hm_len_ty = ir.FunctionType(i32, [i8p])
        self.hm_len_fn = ir.Function(self.module, hm_len_ty, name="_danha_hm_len")
        
        # '지금 컴파일하고 있는 함수와 그 변수들'.
        # 함수마다 자기만의 변수 스택이 있어야 main의 x와 add 안의 x가 안 섞임.
        # 함수 컴파일 들어갈 때 갈아끼우고 나올 때 복구.
        # vars는 스코프들의 스택 (리스트). 가장 끝이 가장 안쪽 스코프.
        # 양파 껍질 비유: 변수 찾을 때는 안쪽부터 바깥으로 훑고,
        # 만들 때는 가장 안쪽(맨 끝)에 만든다.
        self.current_fn = None
        self.builder = None
        # 7.1.2: 현재 컴파일 중인 함수에서 명시적 'return'이 한 번이라도 실행됐는지.
        # 안전망(_compile_function_body 끝)이 구조체 반환 함수에서 친절한 에러를
        # 낼지 결정하는 데 쓴다. 도달 불가능 블록(return 후의 빈 블록)이 만들어져
        # 블록 종결 상태만으로는 판단할 수 없기 때문.
        self._saw_return = False
        # 7.1.3 파트 3: 현재 함수의 '읽기 참조' 매개변수 이름 집합.
        # '&T'로 받은 매개변수는 본문에서 쓰기(필드 대입, 재대입) 금지.
        # 함수 본문 진입 시 채우고 나올 때는 다음 함수가 다시 갈아끼움.
        self._readonly_params = set()
        self._const_vars = set()  # 7.15a: const로 선언된 변수 이름들
        # 7.15d: 중첩 루프에서 break/continue의 대상 블록을 찾기 위한 스택.
        # 새 루프에 들어갈 때 (continue_target_block, break_target_block) 튜플을 push,
        # 루프를 벗어날 때 pop. 루프 바깥에서 break/continue 쓰면 스택이 비어있어 에러.
        # 튜플의 순서: (continue로 점프할 곳, break로 점프할 곳).
        self._loop_stack = []
        # 7.15d: 최상위에서 선언된 변수는 LLVM 글로벌 변수로 올려 system/함수가 볼 수 있게.
        # 이름 → LLVM GlobalVariable. _lookup_var가 지역 스코프 못 찾으면 여기도 확인.
        # 인터프리터가 최상위 변수를 global_scope에 두는 것과 의미 일치.
        self._globals = {}
        # 현재 컴파일 중인 함수가 'main'인지. 최상위 Assign을 글로벌로 승격할지 결정.
        # compile_program의 main 진입 시 True, 사용자 함수/메서드/system 진입 시 False.
        self._in_main = False
        self.vars = None  # list[dict[str, ir.Value]] (alloca 슬롯들)
        
        # 20a: comptime 지원 — 인터프리터 평가를 위한 상태
        # _comptime_consts: comptime에서 참조 가능한 상수값 (인터프리터 값)
        # _comptime_fns: comptime에서 호출 가능한 함수 정의 (인터프리터 AST)
        # _comptime_arr_counter: 글로벌 배열 이름 충돌 방지용 카운터
        self._comptime_consts = {}
        self._comptime_fns = {}  # fn_name → FnDef AST 노드
        self._comptime_arr_counter = 0
        # 21a: unsafe 관련 상태
        self._in_unsafe = 0  # unsafe 블록 깊이
        self._unsafe_fns = set()  # unsafe fn 이름 집합
        self._macros = {}  # 22a: 매크로 정의 저장 {이름: ('Macro', params, body, is_variadic)}

    def _define_direct_os_i32_stub(self, name, arg_types, var_arg=False):
        fn = ir.Function(self.module, ir.FunctionType(i32, arg_types, var_arg=var_arg), name=name)
        fn.linkage = "internal"
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        b.ret(ir.Constant(i32, 0))
        return fn

    def _ensure_runtime_fn(self, name, ret_ty, arg_tys):
        existing = self.module.globals.get(name)
        if existing is not None:
            return existing
        return ir.Function(self.module, ir.FunctionType(ret_ty, arg_tys), name=name)

    def _define_direct_os_f64_stub(self, name, arg_types):
        fn = ir.Function(self.module, ir.FunctionType(f64, arg_types), name=name)
        fn.linkage = "internal"
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        b.ret(ir.Constant(f64, 0.0))
        return fn

    def _define_direct_os_f32_stub(self, name, arg_types):
        fn = ir.Function(self.module, ir.FunctionType(f32, arg_types), name=name)
        fn.linkage = "internal"
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        b.ret(ir.Constant(f32, 0.0))
        return fn

    def _declare_direct_os_runtime(self):
        """Windows direct-OS runtime primitives.

        This first slice targets Windows AOT. It keeps LLVM as the backend but
        moves core allocation and simple stdout writes away from libc symbols.
        """
        void_ptr = i8p
        void_ty = ir.VoidType()

        self._GetStdHandle = ir.Function(
            self.module,
            ir.FunctionType(void_ptr, [i32]),
            name="GetStdHandle",
        )
        self._WriteFile = ir.Function(
            self.module,
            ir.FunctionType(i32, [void_ptr, void_ptr, i32, i32.as_pointer(), void_ptr]),
            name="WriteFile",
        )
        self._GetProcessHeap = ir.Function(
            self.module,
            ir.FunctionType(void_ptr, []),
            name="GetProcessHeap",
        )
        self._HeapAlloc = ir.Function(
            self.module,
            ir.FunctionType(void_ptr, [void_ptr, i32, i64]),
            name="HeapAlloc",
        )
        self._HeapReAlloc = ir.Function(
            self.module,
            ir.FunctionType(void_ptr, [void_ptr, i32, void_ptr, i64]),
            name="HeapReAlloc",
        )
        self._HeapFree = ir.Function(
            self.module,
            ir.FunctionType(i32, [void_ptr, i32, void_ptr]),
            name="HeapFree",
        )
        self.exit_fn = ir.Function(
            self.module,
            ir.FunctionType(void_ty, [i32]),
            name="ExitProcess",
        )

        self.malloc_fn = ir.Function(self.module, ir.FunctionType(void_ptr, [i64]), name="_danha_os_malloc")
        self.realloc_fn = ir.Function(self.module, ir.FunctionType(void_ptr, [void_ptr, i64]), name="_danha_os_realloc")
        self.free_fn = ir.Function(self.module, ir.FunctionType(void_ty, [void_ptr]), name="_danha_os_free")

        self._os_strlen_fn = ir.Function(self.module, ir.FunctionType(i64, [i8p]), name="_danha_os_strlen")
        self._os_write_fn = ir.Function(self.module, ir.FunctionType(void_ty, [i8p, i64]), name="_danha_os_write")
        self._os_write_nl_fn = ir.Function(self.module, ir.FunctionType(void_ty, []), name="_danha_os_write_nl")
        self._define_direct_os_puts()
        self._define_direct_os_memset()
        self._define_direct_os_memcpy()
        if self.module.globals.get("_fltused") is None:
            fltused = ir.GlobalVariable(self.module, i32, name="_fltused")
            fltused.initializer = ir.Constant(i32, 0)

        self._define_direct_os_runtime_bodies()

    def _declare_direct_os_strcmp(self):
        fn = ir.Function(self.module, ir.FunctionType(i32, [i8p, i8p]), name="strcmp")
        entry = fn.append_basic_block("entry")
        loop = fn.append_basic_block("loop")
        diff = fn.append_basic_block("diff")
        same = fn.append_basic_block("same")
        end_ret = fn.append_basic_block("end_ret")
        cont = fn.append_basic_block("cont")
        b = ir.IRBuilder(entry)
        idx_slot = b.alloca(i64, name="idx")
        b.store(ir.Constant(i64, 0), idx_slot)
        b.branch(loop)
        b.position_at_end(loop)
        idx = b.load(idx_slot)
        a_ch = b.load(b.gep(fn.args[0], [idx], inbounds=True))
        b_ch = b.load(b.gep(fn.args[1], [idx], inbounds=True))
        chars_same = b.icmp_unsigned("==", a_ch, b_ch)
        b.cbranch(chars_same, same, diff)
        b.position_at_end(diff)
        av = b.zext(a_ch, i32)
        bv = b.zext(b_ch, i32)
        b.ret(b.sub(av, bv))
        b.position_at_end(same)
        is_end = b.icmp_unsigned("==", a_ch, ir.Constant(i8, 0))
        b.cbranch(is_end, end_ret, cont)
        b.position_at_end(end_ret)
        b.ret(ir.Constant(i32, 0))
        b.position_at_end(cont)
        b.store(b.add(idx, ir.Constant(i64, 1)), idx_slot)
        b.branch(loop)
        return fn

    def _declare_direct_os_atoi(self):
        fn = ir.Function(self.module, ir.FunctionType(i32, [i8p]), name="atoi")
        entry = fn.append_basic_block("entry")
        sign = fn.append_basic_block("sign")
        loop = fn.append_basic_block("loop")
        body = fn.append_basic_block("body")
        done = fn.append_basic_block("done")
        b = ir.IRBuilder(entry)
        idx_slot = b.alloca(i64, name="idx")
        acc_slot = b.alloca(i32, name="acc")
        neg_slot = b.alloca(i1, name="neg")
        b.store(ir.Constant(i64, 0), idx_slot)
        b.store(ir.Constant(i32, 0), acc_slot)
        b.store(ir.Constant(i1, 0), neg_slot)
        first = b.load(fn.args[0])
        is_minus = b.icmp_unsigned("==", first, ir.Constant(i8, 45))
        b.cbranch(is_minus, sign, loop)
        b.position_at_end(sign)
        b.store(ir.Constant(i1, 1), neg_slot)
        b.store(ir.Constant(i64, 1), idx_slot)
        b.branch(loop)
        b.position_at_end(loop)
        idx = b.load(idx_slot)
        ch = b.load(b.gep(fn.args[0], [idx], inbounds=True))
        ge0 = b.icmp_unsigned(">=", ch, ir.Constant(i8, 48))
        le9 = b.icmp_unsigned("<=", ch, ir.Constant(i8, 57))
        is_digit = b.and_(ge0, le9)
        b.cbranch(is_digit, body, done)
        b.position_at_end(body)
        digit = b.sub(b.zext(ch, i32), ir.Constant(i32, 48))
        acc = b.load(acc_slot)
        b.store(b.add(b.mul(acc, ir.Constant(i32, 10)), digit), acc_slot)
        b.store(b.add(idx, ir.Constant(i64, 1)), idx_slot)
        b.branch(loop)
        b.position_at_end(done)
        acc = b.load(acc_slot)
        neg_acc = b.sub(ir.Constant(i32, 0), acc)
        b.ret(b.select(b.load(neg_slot), neg_acc, acc))
        return fn

    def _define_direct_os_memset(self):
        existing = self.module.globals.get("memset")
        if existing is not None:
            return existing
        fn = ir.Function(self.module, ir.FunctionType(i8p, [i8p, i32, i64]), name="memset")
        entry = fn.append_basic_block("entry")
        loop = fn.append_basic_block("loop")
        body = fn.append_basic_block("body")
        done = fn.append_basic_block("done")
        b = ir.IRBuilder(entry)
        idx_slot = b.alloca(i64, name="idx")
        b.store(ir.Constant(i64, 0), idx_slot)
        fill = b.trunc(fn.args[1], i8)
        b.branch(loop)
        b.position_at_end(loop)
        idx = b.load(idx_slot)
        has_more = b.icmp_unsigned("<", idx, fn.args[2])
        b.cbranch(has_more, body, done)
        b.position_at_end(body)
        ptr = b.gep(fn.args[0], [idx])
        b.store(fill, ptr)
        b.store(b.add(idx, ir.Constant(i64, 1)), idx_slot)
        b.branch(loop)
        b.position_at_end(done)
        b.ret(fn.args[0])
        return fn

    def _define_direct_os_memcpy(self):
        existing = self.module.globals.get("memcpy")
        if existing is not None:
            return existing
        fn = ir.Function(self.module, ir.FunctionType(i8p, [i8p, i8p, i64]), name="memcpy")
        entry = fn.append_basic_block("entry")
        loop = fn.append_basic_block("loop")
        body = fn.append_basic_block("body")
        done = fn.append_basic_block("done")
        b = ir.IRBuilder(entry)
        idx_slot = b.alloca(i64, name="idx")
        b.store(ir.Constant(i64, 0), idx_slot)
        b.branch(loop)
        b.position_at_end(loop)
        idx = b.load(idx_slot)
        has_more = b.icmp_unsigned("<", idx, fn.args[2])
        b.cbranch(has_more, body, done)
        b.position_at_end(body)
        src = b.gep(fn.args[1], [idx])
        dst = b.gep(fn.args[0], [idx])
        b.store(b.load(src), dst)
        b.store(b.add(idx, ir.Constant(i64, 1)), idx_slot)
        b.branch(loop)
        b.position_at_end(done)
        b.ret(fn.args[0])
        return fn

    def _define_direct_os_puts(self):
        fn = ir.Function(self.module, ir.FunctionType(i32, [i8p]), name="puts")
        fn.linkage = "internal"
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        length = b.call(self._os_strlen_fn, [fn.args[0]])
        b.call(self._os_write_fn, [fn.args[0], length])
        b.call(self._os_write_nl_fn, [])
        b.ret(ir.Constant(i32, 0))

    def _define_core_string_index_runtime(self):
        if self.module.globals.get("dnh_str_idx") is not None:
            return
        fn = ir.Function(self.module, ir.FunctionType(i8p, [i8p, i64]), name="dnh_str_idx")
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        buf = b.call(self.malloc_fn, [ir.Constant(i64, 2)], name="stridx_buf")
        ch_ptr = b.gep(fn.args[0], [fn.args[1]], inbounds=True)
        ch = b.load(ch_ptr, name="stridx_ch")
        b.store(ch, buf)
        end_ptr = b.gep(buf, [ir.Constant(i64, 1)], inbounds=True)
        b.store(ir.Constant(i8, 0), end_ptr)
        b.ret(buf)

    def _ensure_function(self, name, ret_ty, arg_tys):
        existing = self.module.globals.get(name)
        if existing is not None:
            return existing
        return ir.Function(self.module, ir.FunctionType(ret_ty, arg_tys), name=name)

    def _define_arena_runtime(self):
        """글로벌 아레나 할당 런타임 — 경계 검사 + 청크 성장.

        이전의 _arena_alloc은 검사 없는 인라인 bump라 용량(256MB) 초과 시
        조용한 힙 손상을 일으켰다 (작은 입력 정상, 대형 입력만 멀리서 크래시).
        이제 모든 아레나 할당이 _danha_arena_alloc(i32) → i8* 를 거친다:
          - 크기를 8바이트 정렬로 올림 (후속 할당의 정렬 보장)
          - 현재 청크에 안 들어가면 새 청크를 malloc:
            용량 2배 vs 요청 크기 중 큰 쪽, 청크 한도는 2^31-8 (offset이 i32)
          - 이전 청크는 그대로 둔다 — 기존 포인터가 계속 유효해야 하므로
            의도적 누수 (아레나는 프로세스 수명, 종료 시 OS가 회수)
          - 요청이 청크 한도를 넘거나 malloc이 실패하면 메시지 + exit(1)
        arena_reset()은 offset=0으로 현재(가장 큰) 청크를 재사용 — 기존 의미 유지.
        """
        if self.module.globals.get("_danha_arena_alloc") is not None:
            self.arena_alloc_fn = self.module.get_global("_danha_arena_alloc")
            return
        fn = ir.Function(self.module, ir.FunctionType(i8p, [i32]), name="_danha_arena_alloc")
        fn.linkage = "internal"
        self.arena_alloc_fn = fn

        entry = fn.append_basic_block("entry")
        grow_bb = fn.append_basic_block("grow")
        fail_bb = fn.append_basic_block("fail")
        do_bb = fn.append_basic_block("do")

        chunk_limit = ir.Constant(i64, 0x7FFFFFF8)  # i32 offset 한도 (8바이트 정렬 여유)

        def field_ptr(b, idx, name):
            return b.gep(self.arena_slot,
                         [ir.Constant(i32, 0), ir.Constant(i32, idx)],
                         inbounds=True, name=name)

        b = ir.IRBuilder(entry)
        size64 = b.zext(fn.args[0], i64, name="aa_size64")
        size_al = b.and_(b.add(size64, ir.Constant(i64, 7), name="aa_size7"),
                         ir.Constant(i64, -8), name="aa_size_al")
        off0 = b.load(field_ptr(b, 1, "aa_off_p0"), name="aa_off0")
        cap0 = b.load(field_ptr(b, 2, "aa_cap_p0"), name="aa_cap0")
        off0_64 = b.zext(off0, i64, name="aa_off0_64")
        cap0_64 = b.zext(cap0, i64, name="aa_cap0_64")
        need = b.add(off0_64, size_al, name="aa_need")
        fits = b.icmp_unsigned("<=", need, cap0_64, name="aa_fits")
        b.cbranch(fits, do_bb, grow_bb)

        # grow: 새 청크 = max(cap*2, size_al), 한도 초과분은 한도로 클램프
        b.position_at_end(grow_bb)
        too_big = b.icmp_unsigned(">", size_al, chunk_limit, name="aa_too_big")
        dbl = b.mul(cap0_64, ir.Constant(i64, 2), name="aa_dbl")
        use_size = b.icmp_unsigned("<", dbl, size_al, name="aa_use_size")
        ncap_a = b.select(use_size, size_al, dbl, name="aa_ncap_a")
        over = b.icmp_unsigned(">", ncap_a, chunk_limit, name="aa_over")
        ncap = b.select(over, chunk_limit, ncap_a, name="aa_ncap")
        grow_ok_bb = fn.append_basic_block("grow_ok")
        alloc_bb = fn.append_basic_block("grow_alloc")
        b.cbranch(too_big, fail_bb, alloc_bb)

        b.position_at_end(alloc_bb)
        mem = b.call(self.malloc_fn, [ncap], name="aa_mem")
        mem_null = b.icmp_unsigned("==", b.ptrtoint(mem, i64, name="aa_mem_i"),
                                   ir.Constant(i64, 0), name="aa_mem_null")
        b.cbranch(mem_null, fail_bb, grow_ok_bb)

        b.position_at_end(grow_ok_bb)
        b.store(mem, field_ptr(b, 0, "aa_base_p1"))
        b.store(ir.Constant(i32, 0), field_ptr(b, 1, "aa_off_p1"))
        b.store(b.trunc(ncap, i32, name="aa_ncap32"), field_ptr(b, 2, "aa_cap_p1"))
        b.branch(do_bb)

        # fail: 조용한 손상 대신 큰 소리로 종료
        b.position_at_end(fail_bb)
        msg = self._make_global_string(
            "_danha_arena_fail_msg",
            "단아 아레나: 메모리 할당 실패 (요청이 너무 크거나 malloc 실패)\n\0",
        )
        msg_ptr = b.gep(msg, [ir.Constant(i32, 0), ir.Constant(i32, 0)],
                        inbounds=True, name="aa_msg")
        b.call(self.printf, [msg_ptr])
        # exit는 모듈에 한 번만 — ECS 런타임의 hasattr(self, 'exit_fn') 검사와 공유
        self.exit_fn = self._ensure_function("exit", ir.VoidType(), [i32])
        b.call(self.exit_fn, [ir.Constant(i32, 1)])
        b.unreachable()

        # do: (성장 후일 수 있으니) 다시 로드해서 bump
        b.position_at_end(do_bb)
        base1 = b.load(field_ptr(b, 0, "aa_base_p2"), name="aa_base1")
        off1 = b.load(field_ptr(b, 1, "aa_off_p2"), name="aa_off1")
        result = b.gep(base1, [off1], inbounds=True, name="aa_result")
        off1_64 = b.zext(off1, i64, name="aa_off1_64")
        new_off = b.trunc(b.add(off1_64, size_al, name="aa_new_off64"),
                          i32, name="aa_new_off")
        b.store(new_off, field_ptr(b, 1, "aa_off_p3"))
        b.ret(result)

    def _define_core_file_runtime(self):
        if self.module.globals.get("dnh_file_read") is not None:
            return
        void_ptr = i8p
        create_file = self._ensure_function(
            "CreateFileA", void_ptr,
            [i8p, i32, i32, void_ptr, i32, i32, void_ptr],
        )
        get_file_size = self._ensure_function("GetFileSize", i32, [void_ptr, void_ptr])
        get_file_attrs = self._ensure_function("GetFileAttributesA", i32, [i8p])
        read_file = self._ensure_function("ReadFile", i32, [void_ptr, void_ptr, i32, i32.as_pointer(), void_ptr])
        write_file = self._ensure_function("WriteFile", i32, [void_ptr, void_ptr, i32, i32.as_pointer(), void_ptr])
        set_file_pointer = self._ensure_function("SetFilePointer", i32, [void_ptr, i32, void_ptr, i32])
        close_handle = self._ensure_function("CloseHandle", i32, [void_ptr])
        file_count_slot = self.module.globals.get("_danha_file_count")
        if file_count_slot is None:
            file_count_slot = ir.GlobalVariable(self.module, i32, name="_danha_file_count")
            file_count_slot.linkage = "internal"
            file_count_slot.initializer = ir.Constant(i32, 0)

        invalid_handle_i64 = ir.Constant(i64, -1)
        null_ptr = ir.Constant(void_ptr, None)

        fn = ir.Function(self.module, ir.FunctionType(i8p, [i8p]), name="dnh_file_read")
        entry = fn.append_basic_block("entry")
        fail = fn.append_basic_block("fail")
        read = fn.append_basic_block("read")
        done = fn.append_basic_block("done")
        b = ir.IRBuilder(entry)
        handle = b.call(create_file, [
            fn.args[0],
            ir.Constant(i32, 0x80000000),  # GENERIC_READ
            ir.Constant(i32, 1),           # FILE_SHARE_READ
            null_ptr,
            ir.Constant(i32, 3),           # OPEN_EXISTING
            ir.Constant(i32, 0),
            null_ptr,
        ], name="fr_handle")
        handle_i64 = b.ptrtoint(handle, i64, name="fr_handle_i64")
        is_invalid = b.icmp_signed("==", handle_i64, invalid_handle_i64)
        b.cbranch(is_invalid, fail, read)

        b.position_at_end(fail)
        empty = b.call(self.malloc_fn, [ir.Constant(i64, 1)], name="fr_empty")
        b.store(ir.Constant(i8, 0), empty)
        b.branch(done)

        b.position_at_end(read)
        size32 = b.call(get_file_size, [handle, null_ptr], name="fr_size32")
        size64 = b.zext(size32, i64, name="fr_size64")
        alloc_size = b.add(size64, ir.Constant(i64, 1), name="fr_alloc_size")
        buf = b.call(self.malloc_fn, [alloc_size], name="fr_buf")
        b.call(read_file, [handle, buf, size32, file_count_slot, null_ptr])
        bytes_read = b.load(file_count_slot, name="fr_bytes")
        bytes_read64 = b.zext(bytes_read, i64, name="fr_bytes64")
        end = b.gep(buf, [bytes_read64], inbounds=True, name="fr_end")
        b.store(ir.Constant(i8, 0), end)
        b.call(close_handle, [handle])
        b.branch(done)

        b.position_at_end(done)
        result = b.phi(i8p, name="fr_result")
        result.add_incoming(empty, fail)
        result.add_incoming(buf, read)
        b.ret(result)

        fn = ir.Function(self.module, ir.FunctionType(ir.VoidType(), [i8p, i8p]), name="dnh_file_write")
        entry = fn.append_basic_block("entry")
        write = fn.append_basic_block("write")
        done = fn.append_basic_block("done")
        b = ir.IRBuilder(entry)
        handle = b.call(create_file, [
            fn.args[0],
            ir.Constant(i32, 0x40000000),  # GENERIC_WRITE
            ir.Constant(i32, 1),           # FILE_SHARE_READ — AV/인덱서가 읽는 중에도 열기 성공
            null_ptr,
            ir.Constant(i32, 2),           # CREATE_ALWAYS
            ir.Constant(i32, 0),
            null_ptr,
        ], name="fw_handle")
        handle_i64 = b.ptrtoint(handle, i64, name="fw_handle_i64")
        is_invalid = b.icmp_signed("==", handle_i64, invalid_handle_i64)
        b.cbranch(is_invalid, done, write)

        b.position_at_end(write)
        text_len64 = b.call(self.strlen_fn, [fn.args[1]], name="fw_len64")
        text_len32 = b.trunc(text_len64, i32, name="fw_len32")
        b.call(write_file, [handle, fn.args[1], text_len32, file_count_slot, null_ptr])
        b.call(close_handle, [handle])
        b.branch(done)

        b.position_at_end(done)
        b.ret_void()

        fn = ir.Function(self.module, ir.FunctionType(ir.VoidType(), [i8p, i8p]), name="dnh_file_append")
        entry = fn.append_basic_block("entry")
        write = fn.append_basic_block("write")
        done = fn.append_basic_block("done")
        b = ir.IRBuilder(entry)
        handle = b.call(create_file, [
            fn.args[0],
            ir.Constant(i32, 0x40000000),  # GENERIC_WRITE
            ir.Constant(i32, 1),           # FILE_SHARE_READ — AV/인덱서가 읽는 중에도 열기 성공
            null_ptr,
            ir.Constant(i32, 4),           # OPEN_ALWAYS
            ir.Constant(i32, 0),
            null_ptr,
        ], name="fa_handle")
        handle_i64 = b.ptrtoint(handle, i64, name="fa_handle_i64")
        is_invalid = b.icmp_signed("==", handle_i64, invalid_handle_i64)
        b.cbranch(is_invalid, done, write)

        b.position_at_end(write)
        b.call(set_file_pointer, [handle, ir.Constant(i32, 0), null_ptr, ir.Constant(i32, 2)])
        text_len64 = b.call(self.strlen_fn, [fn.args[1]], name="fa_len64")
        text_len32 = b.trunc(text_len64, i32, name="fa_len32")
        b.call(write_file, [handle, fn.args[1], text_len32, file_count_slot, null_ptr])
        b.call(close_handle, [handle])
        b.branch(done)

        b.position_at_end(done)
        b.ret_void()

        fn = ir.Function(self.module, ir.FunctionType(i1, [i8p]), name="dnh_file_exists")
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        attrs = b.call(get_file_attrs, [fn.args[0]], name="fe_attrs")
        missing = b.icmp_signed("==", attrs, ir.Constant(i32, -1), name="fe_missing")
        b.ret(b.xor(missing, ir.Constant(i1, 1), name="fe_exists"))

    def _define_direct_os_exec_runtime(self):
        existing = self.module.globals.get("dnh_os_exec")
        if existing is not None:
            return existing

        void_ptr = i8p
        null_ptr = ir.Constant(void_ptr, None)
        create_process = self._ensure_function(
            "CreateProcessA", i32,
            [void_ptr, void_ptr, void_ptr, void_ptr, i32, i32, void_ptr, void_ptr, void_ptr, void_ptr],
        )
        wait_for_single_object = self._ensure_function("WaitForSingleObject", i32, [void_ptr, i32])
        get_exit_code_process = self._ensure_function("GetExitCodeProcess", i32, [void_ptr, i32.as_pointer()])
        close_handle = self._ensure_function("CloseHandle", i32, [void_ptr])

        si_ty = ir.ArrayType(i8, 104)
        pi_ty = ir.ArrayType(i8, 24)
        si = self.module.globals.get("_danha_startupinfo")
        if si is None:
            si = ir.GlobalVariable(self.module, si_ty, name="_danha_startupinfo")
            si.linkage = "internal"
            si.initializer = ir.Constant(si_ty, bytearray(104))
        pi = self.module.globals.get("_danha_processinfo")
        if pi is None:
            pi = ir.GlobalVariable(self.module, pi_ty, name="_danha_processinfo")
            pi.linkage = "internal"
            pi.initializer = ir.Constant(pi_ty, bytearray(24))
        exit_code = self.module.globals.get("_danha_process_exit_code")
        if exit_code is None:
            exit_code = ir.GlobalVariable(self.module, i32, name="_danha_process_exit_code")
            exit_code.linkage = "internal"
            exit_code.initializer = ir.Constant(i32, 0)

        fn = ir.Function(self.module, ir.FunctionType(i32, [i8p]), name="dnh_os_exec")
        entry = fn.append_basic_block("entry")
        copy_loop = fn.append_basic_block("copy_loop")
        copy_done = fn.append_basic_block("copy_done")
        wait = fn.append_basic_block("wait")
        fail = fn.append_basic_block("fail")
        done = fn.append_basic_block("done")
        b = ir.IRBuilder(entry)
        cmd_len = b.call(self.strlen_fn, [fn.args[0]], name="cmd_len")
        cmd_size = b.add(cmd_len, ir.Constant(i64, 1), name="cmd_size")
        cmd_copy = b.call(self.malloc_fn, [cmd_size], name="cmd_copy")
        b.branch(copy_loop)

        b.position_at_end(copy_loop)
        idx = b.phi(i64, name="copy_idx")
        idx.add_incoming(ir.Constant(i64, 0), entry)
        src_p = b.gep(fn.args[0], [idx], inbounds=True, name="cmd_src_p")
        dst_p = b.gep(cmd_copy, [idx], inbounds=True, name="cmd_dst_p")
        ch = b.load(src_p, name="cmd_ch")
        b.store(ch, dst_p)
        is_end = b.icmp_unsigned("==", ch, ir.Constant(i8, 0), name="cmd_end")
        next_idx = b.add(idx, ir.Constant(i64, 1), name="copy_next")
        idx.add_incoming(next_idx, copy_loop)
        b.cbranch(is_end, copy_done, copy_loop)

        b.position_at_end(copy_done)
        si_ptr = b.gep(si, [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True, name="si_ptr")
        pi_ptr = b.gep(pi, [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True, name="pi_ptr")
        si_cb_ptr = b.bitcast(si_ptr, i32.as_pointer(), name="si_cb_ptr")
        b.store(ir.Constant(i32, 104), si_cb_ptr)
        ok = b.call(create_process, [
            null_ptr, cmd_copy, null_ptr, null_ptr,
            ir.Constant(i32, 0), ir.Constant(i32, 0),
            null_ptr, null_ptr, si_ptr, pi_ptr,
        ], name="cp_ok")
        started = b.icmp_signed("!=", ok, ir.Constant(i32, 0), name="cp_started")
        b.cbranch(started, wait, fail)

        b.position_at_end(wait)
        process_handle_ptr = b.bitcast(pi_ptr, i8p.as_pointer(), name="process_handle_ptr")
        process_handle = b.load(process_handle_ptr, name="process_handle")
        thread_slot = b.gep(pi_ptr, [ir.Constant(i64, 8)], inbounds=True, name="thread_slot")
        thread_handle_ptr = b.bitcast(thread_slot, i8p.as_pointer(), name="thread_handle_ptr")
        thread_handle = b.load(thread_handle_ptr, name="thread_handle")
        b.call(wait_for_single_object, [process_handle, ir.Constant(i32, -1)])
        b.call(get_exit_code_process, [process_handle, exit_code])
        b.call(close_handle, [thread_handle])
        b.call(close_handle, [process_handle])
        code = b.load(exit_code, name="proc_exit_code")
        b.branch(done)

        b.position_at_end(fail)
        b.branch(done)

        b.position_at_end(done)
        ret = b.phi(i32, name="os_exec_ret")
        ret.add_incoming(code, wait)
        ret.add_incoming(ir.Constant(i32, -1), fail)
        b.ret(ret)
        return fn

    def _define_direct_os_get_args_runtime(self, dyn_ty):
        existing = self.module.globals.get("dnh_os_get_args")
        if existing is not None:
            return existing

        get_command_line = self._ensure_function("GetCommandLineA", i8p, [])
        fn = ir.Function(self.module, ir.FunctionType(dyn_ty, []), name="dnh_os_get_args")
        entry = fn.append_basic_block("entry")
        copy_loop = fn.append_basic_block("copy_loop")
        copy_done = fn.append_basic_block("copy_done")
        skip_prog = fn.append_basic_block("skip_prog")
        skip_prog_after = fn.append_basic_block("skip_prog_after")
        skip_spaces = fn.append_basic_block("skip_spaces")
        after_space_check = fn.append_basic_block("after_space_check")
        arg_start = fn.append_basic_block("arg_start")
        scan_arg = fn.append_basic_block("scan_arg")
        arg_done = fn.append_basic_block("arg_done")
        done = fn.append_basic_block("done")

        b = ir.IRBuilder(entry)
        raw = b.call(get_command_line, [], name="cmdline")
        raw_len = b.call(self.strlen_fn, [raw], name="cmdline_len")
        raw_size = b.add(raw_len, ir.Constant(i64, 1), name="cmdline_size")
        copy = b.call(self.malloc_fn, [raw_size], name="cmdline_copy")
        data_raw = b.call(self.malloc_fn, [ir.Constant(i64, 256)], name="argv_data_raw")
        data = b.bitcast(data_raw, i8p.as_pointer(), name="argv_data")
        b.branch(copy_loop)

        b.position_at_end(copy_loop)
        copy_i = b.phi(i64, name="copy_i")
        copy_i.add_incoming(ir.Constant(i64, 0), entry)
        src = b.gep(raw, [copy_i], inbounds=True, name="copy_src")
        dst = b.gep(copy, [copy_i], inbounds=True, name="copy_dst")
        ch = b.load(src, name="copy_ch")
        b.store(ch, dst)
        copy_end = b.icmp_unsigned("==", ch, ir.Constant(i8, 0), name="copy_end")
        copy_next = b.add(copy_i, ir.Constant(i64, 1), name="copy_next")
        copy_i.add_incoming(copy_next, copy_loop)
        b.cbranch(copy_end, copy_done, copy_loop)

        b.position_at_end(copy_done)
        b.branch(skip_prog)

        b.position_at_end(skip_prog)
        sp_i = b.phi(i64, name="sp_i")
        sp_i.add_incoming(ir.Constant(i64, 0), copy_done)
        sp_p = b.gep(copy, [sp_i], inbounds=True, name="sp_p")
        sp_ch = b.load(sp_p, name="sp_ch")
        sp_space = b.icmp_unsigned("==", sp_ch, ir.Constant(i8, 32), name="sp_space")
        sp_nul = b.icmp_unsigned("==", sp_ch, ir.Constant(i8, 0), name="sp_nul")
        sp_next = b.add(sp_i, ir.Constant(i64, 1), name="sp_next")
        sp_i.add_incoming(sp_next, skip_prog_after)
        b.cbranch(sp_nul, done, skip_prog_after)

        b.position_at_end(skip_prog_after)
        b.cbranch(sp_space, skip_spaces, skip_prog)

        b.position_at_end(skip_spaces)
        ss_i = b.phi(i64, name="ss_i")
        ss_count = b.phi(i32, name="ss_count")
        ss_i.add_incoming(sp_i, skip_prog_after)
        ss_count.add_incoming(ir.Constant(i32, 0), skip_prog_after)
        ss_p = b.gep(copy, [ss_i], inbounds=True, name="ss_p")
        ss_ch = b.load(ss_p, name="ss_ch")
        ss_space = b.icmp_unsigned("==", ss_ch, ir.Constant(i8, 32), name="ss_space")
        ss_nul = b.icmp_unsigned("==", ss_ch, ir.Constant(i8, 0), name="ss_nul")
        ss_next = b.add(ss_i, ir.Constant(i64, 1), name="ss_next")
        ss_i.add_incoming(ss_next, after_space_check)
        ss_count.add_incoming(ss_count, after_space_check)
        b.cbranch(ss_nul, done, after_space_check)

        b.position_at_end(after_space_check)
        b.cbranch(ss_space, skip_spaces, arg_start)

        b.position_at_end(arg_start)
        start_ptr = b.gep(copy, [ss_i], inbounds=True, name="arg_start_ptr")
        b.branch(scan_arg)

        b.position_at_end(scan_arg)
        sa_i = b.phi(i64, name="sa_i")
        sa_i.add_incoming(ss_i, arg_start)
        sa_p = b.gep(copy, [sa_i], inbounds=True, name="sa_p")
        sa_ch = b.load(sa_p, name="sa_ch")
        sa_space = b.icmp_unsigned("==", sa_ch, ir.Constant(i8, 32), name="sa_space")
        sa_nul = b.icmp_unsigned("==", sa_ch, ir.Constant(i8, 0), name="sa_nul")
        sa_stop = b.or_(sa_space, sa_nul, name="sa_stop")
        sa_next = b.add(sa_i, ir.Constant(i64, 1), name="sa_next")
        sa_i.add_incoming(sa_next, scan_arg)
        b.cbranch(sa_stop, arg_done, scan_arg)

        b.position_at_end(arg_done)
        b.store(ir.Constant(i8, 0), sa_p)
        slot = b.gep(data, [ss_count], inbounds=True, name="arg_slot")
        b.store(start_ptr, slot)
        count_next = b.add(ss_count, ir.Constant(i32, 1), name="count_next")
        ss_i.add_incoming(sa_next, arg_done)
        ss_count.add_incoming(count_next, arg_done)
        b.cbranch(sa_nul, done, skip_spaces)

        b.position_at_end(done)
        done_count = b.phi(i32, name="done_count")
        done_count.add_incoming(ir.Constant(i32, 0), skip_prog)
        done_count.add_incoming(ss_count, skip_spaces)
        done_count.add_incoming(count_next, arg_done)
        result = ir.Constant(dyn_ty, None)
        result = b.insert_value(result, data, 0, name="args_data")
        result = b.insert_value(result, done_count, 1, name="args_len")
        result = b.insert_value(result, ir.Constant(i32, 32), 2, name="args_cap")
        b.ret(result)
        return fn

    def _define_direct_os_runtime_bodies(self):
        saved_builder = getattr(self, 'builder', None)
        saved_fn = getattr(self, 'current_fn', None)
        void_ptr = i8p

        fn = self.malloc_fn
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        heap = b.call(self._GetProcessHeap, [])
        mem = b.call(self._HeapAlloc, [heap, ir.Constant(i32, 8), fn.args[0]])
        b.ret(mem)

        fn = self.realloc_fn
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        heap = b.call(self._GetProcessHeap, [])
        mem = b.call(self._HeapReAlloc, [heap, ir.Constant(i32, 8), fn.args[0], fn.args[1]])
        b.ret(mem)

        fn = self.free_fn
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        heap = b.call(self._GetProcessHeap, [])
        b.call(self._HeapFree, [heap, ir.Constant(i32, 0), fn.args[0]])
        b.ret_void()

        fn = self._os_strlen_fn
        entry = fn.append_basic_block("entry")
        loop = fn.append_basic_block("loop")
        inc = fn.append_basic_block("inc")
        done = fn.append_basic_block("done")
        b = ir.IRBuilder(entry)
        idx_slot = b.alloca(i64, name="idx")
        b.store(ir.Constant(i64, 0), idx_slot)
        b.branch(loop)
        b.position_at_end(loop)
        idx = b.load(idx_slot)
        ch_ptr = b.gep(fn.args[0], [idx], inbounds=True)
        ch = b.load(ch_ptr)
        is_zero = b.icmp_unsigned("==", ch, ir.Constant(i8, 0))
        b.cbranch(is_zero, done, inc)
        b.position_at_end(inc)
        next_idx = b.add(idx, ir.Constant(i64, 1))
        b.store(next_idx, idx_slot)
        b.branch(loop)
        b.position_at_end(done)
        b.ret(b.load(idx_slot))

        fn = self._os_write_fn
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        stdout = b.call(self._GetStdHandle, [ir.Constant(i32, -11)])
        written = b.alloca(i32, name="written")
        n32 = b.trunc(fn.args[1], i32)
        b.call(self._WriteFile, [stdout, fn.args[0], n32, written, ir.Constant(void_ptr, None)])
        b.ret_void()

        fn = self._os_write_nl_fn
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        nl = self._make_global_string(".os_nl", "\n\0")
        b.call(self._os_write_fn, [b.bitcast(nl, i8p), ir.Constant(i64, 1)])
        b.ret_void()

        self.builder = saved_builder
        self.current_fn = saved_fn

    def _direct_os_write_cstr(self, ptr, include_newline=True):
        length = self.builder.call(self._os_strlen_fn, [ptr], name="os_strlen")
        self.builder.call(self._os_write_fn, [ptr, length])
        if include_newline:
            self.builder.call(self._os_write_nl_fn, [])

    def _direct_os_print_i64(self, value):
        """Write a signed i64 as decimal using only stack memory and WriteFile."""
        b = self.builder
        if value.type != i64:
            if isinstance(value.type, ir.IntType) and value.type.width < 64:
                value = b.sext(value, i64)
            else:
                value = b.trunc(value, i64)

        buf = b.alloca(ir.ArrayType(i8, 32), name="itoa_buf")
        tmp = b.alloca(i64, name="itoa_tmp")
        pos = b.alloca(i64, name="itoa_pos")
        neg = b.icmp_signed("<", value, ir.Constant(i64, 0))
        abs_val = b.select(neg, b.sub(ir.Constant(i64, 0), value), value)
        b.store(abs_val, tmp)
        b.store(ir.Constant(i64, 31), pos)
        nul_ptr = b.gep(buf, [ir.Constant(i32, 0), ir.Constant(i64, 31)])
        b.store(ir.Constant(i8, 0), nul_ptr)

        loop = self.current_fn.append_basic_block("itoa_loop")
        after = self.current_fn.append_basic_block("itoa_after")
        b.branch(loop)
        b.position_at_end(loop)
        cur = b.load(tmp)
        p = b.load(pos)
        digit = b.urem(cur, ir.Constant(i64, 10))
        digit8 = b.trunc(b.add(digit, ir.Constant(i64, 48)), i8)
        new_p = b.sub(p, ir.Constant(i64, 1))
        ch_ptr = b.gep(buf, [ir.Constant(i32, 0), new_p])
        b.store(digit8, ch_ptr)
        b.store(new_p, pos)
        next_cur = b.udiv(cur, ir.Constant(i64, 10))
        b.store(next_cur, tmp)
        keep_going = b.icmp_unsigned("!=", next_cur, ir.Constant(i64, 0))
        b.cbranch(keep_going, loop, after)

        b.position_at_end(after)
        sign_bb = self.current_fn.append_basic_block("itoa_sign")
        write_bb = self.current_fn.append_basic_block("itoa_write")
        b.cbranch(neg, sign_bb, write_bb)
        b.position_at_end(sign_bb)
        p2 = b.load(pos)
        p3 = b.sub(p2, ir.Constant(i64, 1))
        sign_ptr = b.gep(buf, [ir.Constant(i32, 0), p3])
        b.store(ir.Constant(i8, 45), sign_ptr)
        b.store(p3, pos)
        b.branch(write_bb)

        b.position_at_end(write_bb)
        start = b.gep(buf, [ir.Constant(i32, 0), b.load(pos)])
        self._direct_os_write_cstr(start, include_newline=True)

    def _direct_os_to_string_i64(self, value, buf):
        b = self.builder
        if value.type != i64:
            if isinstance(value.type, ir.IntType) and value.type.width < 64:
                value = b.sext(value, i64)
            else:
                value = b.trunc(value, i64)

        tmp = b.alloca(i64, name="tostr_tmp")
        pos = b.alloca(i64, name="tostr_pos")
        neg = b.icmp_signed("<", value, ir.Constant(i64, 0))
        abs_val = b.select(neg, b.sub(ir.Constant(i64, 0), value), value)
        b.store(abs_val, tmp)
        b.store(ir.Constant(i64, 31), pos)
        nul_ptr = b.gep(buf, [ir.Constant(i64, 31)])
        b.store(ir.Constant(i8, 0), nul_ptr)

        zero_bb = self.current_fn.append_basic_block("tostr_zero")
        loop_bb = self.current_fn.append_basic_block("tostr_loop")
        after_bb = self.current_fn.append_basic_block("tostr_after")
        sign_bb = self.current_fn.append_basic_block("tostr_sign")
        done_bb = self.current_fn.append_basic_block("tostr_done")
        is_zero = b.icmp_unsigned("==", abs_val, ir.Constant(i64, 0))
        b.cbranch(is_zero, zero_bb, loop_bb)

        b.position_at_end(zero_bb)
        zp = b.gep(buf, [ir.Constant(i64, 30)])
        b.store(ir.Constant(i8, 48), zp)
        b.store(ir.Constant(i64, 30), pos)
        b.branch(done_bb)

        b.position_at_end(loop_bb)
        cur = b.load(tmp)
        p = b.load(pos)
        digit = b.urem(cur, ir.Constant(i64, 10))
        digit8 = b.trunc(b.add(digit, ir.Constant(i64, 48)), i8)
        new_p = b.sub(p, ir.Constant(i64, 1))
        ch_ptr = b.gep(buf, [new_p])
        b.store(digit8, ch_ptr)
        b.store(new_p, pos)
        next_cur = b.udiv(cur, ir.Constant(i64, 10))
        b.store(next_cur, tmp)
        keep_going = b.icmp_unsigned("!=", next_cur, ir.Constant(i64, 0))
        b.cbranch(keep_going, loop_bb, after_bb)

        b.position_at_end(after_bb)
        b.cbranch(neg, sign_bb, done_bb)

        b.position_at_end(sign_bb)
        p2 = b.load(pos)
        p3 = b.sub(p2, ir.Constant(i64, 1))
        sign_ptr = b.gep(buf, [p3])
        b.store(ir.Constant(i8, 45), sign_ptr)
        b.store(p3, pos)
        b.branch(done_bb)

        b.position_at_end(done_bb)
        return b.gep(buf, [b.load(pos)], name="tostr_start")
    
    def _try_register_comptime_const(self, name, expr):
        """리터럴 상수식이면 _comptime_consts에 파이썬 값으로 등록한다.
        comptime 블록에서 이전 const를 참조할 수 있도록."""
        if expr[0] == 'Number':
            self._comptime_consts[name] = expr[1]
        elif expr[0] == 'String':
            self._comptime_consts[name] = expr[1]
        elif expr[0] == 'Bool':
            self._comptime_consts[name] = expr[1]
    
    def _make_comptime_scope(self):
        """comptime 블록 평가용 인터프리터 Scope를 만든다.
        _comptime_consts의 상수와 _comptime_fns의 함수를 등록한 채로 반환."""
        from danha_evaluator import evaluate, Scope
        scope = Scope()
        for cn, cv in self._comptime_consts.items():
            scope.declare_const(cn, cv)
        # FnDef AST를 evaluate해서 ('Function', params, body, scope) 형태로 등록
        for fn_name, fn_ast in self._comptime_fns.items():
            evaluate(fn_ast, scope)
        return scope
    
    def _make_global_string(self, name, text):
        """문자열을 모듈 전역에 박고 글로벌 핸들을 돌려준다."""
        data = bytearray(text.encode("utf8"))
        const = ir.Constant(ir.ArrayType(i8, len(data)), data)
        glob = ir.GlobalVariable(self.module, const.type, name=name)
        glob.linkage = "internal"
        glob.global_constant = True
        glob.initializer = const
        return glob
    
    # ----- 7.2.1b: 타입 노드 → LLVM 타입 해석 (단일 진입점) -----
    # 이전엔 구조체 필드, 일반 함수, 메서드 세 곳에 같은 타입 해석 로직이 복사돼 있었음.
    # 리팩토링으로 하나로 모음. 앞으로 ArrayType(7.2.1c), vec3(7.7) 등을 추가할 때
    # 여기 한 곳만 고치면 됨.
    
    # 내장 기본 타입 매핑. 지금은 i32, f64 두 개. 나중에 i64, f32, bool 등이 추가될 자리.
    # 7.7: 벡터 타입 추가. LLVM 구조체로 표현.
    # vec2 = {f64, f64}, vec3 = {f64, f64, f64}, vec4 = {f64, f64, f64, f64}
    # 나중에 SIMD(7.10)에서 <N x f32>로 바꿀 수 있지만, 지금은 구조체가 간단하고 안전.
    # Phase 1-2: vec2/vec4는 SIMD 벡터 (auto-vectorization).
    # vec3는 vec4와 LLVM 타입이 같으면 구별 불가하므로 struct 유지.
    _vec2_ty = ir.VectorType(f64, 2)
    _vec3_ty = ir.LiteralStructType([f64, f64, f64])
    _vec4_ty = ir.VectorType(f64, 4)
    # Phase 4: f32 SIMD 변형 (게임 엔진 표준 정밀도)
    # vec2f/vec4f → <N x float>, vec3f는 vec4f와 동일 LLVM 타입이라 별도 struct
    _vec2f_ty = ir.VectorType(f32, 2)
    _vec3f_ty = ir.LiteralStructType([f32, f32, f32])
    _vec4f_ty = ir.VectorType(f32, 4)
    # Phase 3: mat4 = ArrayType([4 x <4 x double>]) — 컬럼 우선 SIMD layout
    _mat4_col_ty = ir.VectorType(f64, 4)
    _mat4_ty = ir.ArrayType(_mat4_col_ty, 4)
    # 7.15f: ECS 컴포넌트에서 다양한 타입 허용.
    # u* 와 i* 는 같은 LLVM 타입 (비트수만 같음) — 부호 구분은 언어 레벨 정보.
    # bool은 저장 레이아웃으로는 i8 (1바이트), 의미는 i1(LLVM 비교 결과 타입)과 구분.
    _TYPE_MAP = {
        'i8': i8, 'i16': i16, 'i32': i32, 'i64': i64,
        'u8': i8, 'u16': i16, 'u32': i32, 'u64': i64,
        'f32': f32, 'f64': f64,
        'bool': i8,
        'str': i8p,
        'vec2': _vec2_ty, 'vec3': _vec3_ty, 'vec4': _vec4_ty,
        'vec2f': _vec2f_ty, 'vec3f': _vec3f_ty, 'vec4f': _vec4f_ty,
        # P1C: mat4 함수 파라미터/반환 타입 지원. 컬럼-벡터 layout이라 그대로 값 전달.
        'mat4': _mat4_ty,
        'EntityId': ir.LiteralStructType([i32, i32]),
    }

    # 벡터 타입 → 논리 성분 수 매핑
    _VEC_INFO = {
        'vec2': (_vec2_ty, 2),
        'vec3': (_vec3_ty, 3),
        'vec4': (_vec4_ty, 4),
        'vec2f': (_vec2f_ty, 2),
        'vec3f': (_vec3f_ty, 3),
        'vec4f': (_vec4f_ty, 4),
    }
    _VEC_LANE_COUNT = {
        'vec2': 2, 'vec3': 4, 'vec4': 4,
        'vec2f': 2, 'vec3f': 4, 'vec4f': 4,
    }
    # 벡터 이름 → 성분 스칼라 타입 (vec*는 f64, vec*f는 f32)
    _VEC_ELEM_TY = {
        'vec2': f64, 'vec3': f64, 'vec4': f64,
        'vec2f': f32, 'vec3f': f32, 'vec4f': f32,
    }
    # 필드 이름 → 인덱스 매핑
    _VEC_FIELDS = {
        'vec2': {'x': 0, 'y': 1},
        'vec3': {'x': 0, 'y': 1, 'z': 2},
        'vec4': {'x': 0, 'y': 1, 'z': 2, 'w': 3},
        'vec2f': {'x': 0, 'y': 1},
        'vec3f': {'x': 0, 'y': 1, 'z': 2},
        'vec4f': {'x': 0, 'y': 1, 'z': 2, 'w': 3},
    }
    
    # Phase 3 mat4 type definitions moved above _TYPE_MAP so it can reference _mat4_ty.
    # Layout: ArrayType(VectorType(f64, 4), 4) — 4 columns × <4 x double>
    # mat4 * vec4 → 4× packed multiply + 3× fmuladd (vfmadd231pd 자동 생성).

    def _resolve_type(self, type_node, what, line):
        """타입 AST 노드를 LLVM 타입과 참조 종류로 해석한다.
        
        입력:
          type_node: 파서가 만든 타입 노드. None이면 '어노테이션 없음'.
            - None                               → 기본 i32 (어노테이션 없을 때의 옛 동작 유지)
            - ('TypeName', 'i32' | 'f64', line)  → 기본 타입
            - ('TypeName', '<구조체 이름>', line) → 사용자 정의 구조체 (값 타입)
            - ('RefType', is_mut, inner, line)   → 참조 (inner를 재귀로 해석한 뒤 포인터로)
          what: 에러 메시지용 설명 (예: "매개변수 'p'", "함수 'foo' 반환")
          line: 에러 메시지의 줄 번호
        
        반환: (llvm_type, ref_kind)
          llvm_type: 최종 LLVM 타입. 참조면 이미 포인터로 래핑됨.
          ref_kind: None / 'ref' / 'mut_ref'
                    호출 사이트가 '&'/'&mut' 일치 검사할 때 쓰려고 별도로 돌려줌.
        """
        if type_node is None:
            # 어노테이션 없음 — 옛 동작 유지: i32 기본.
            # 이 기본값은 "타입 적지 않으면 정수"라는 암묵적 약속. 나중에 타입 추론이
            # 들어오면 이 자리를 '추론 대상'으로 바꿔야 함.
            return (i32, None)
        
        kind = type_node[0]
        
        if kind == 'TypeName':
            name = type_node[1]
            if name in self._TYPE_MAP:
                return (self._TYPE_MAP[name], None)
            if name in self.structs:
                # 7.1.2: 구조체 값 타입 — 복사 전달.
                return (self.structs[name][0], None)
            # 23a: tagged enum을 함수 매개변수/반환 타입으로 사용 가능.
            # tagged enum은 { i32_tag, [payload x i8] } 구조체로 값 전달.
            if name in self.tagged_enums:
                return (self.tagged_enums[name][0], None)
            # `from X import *`로 가져온 struct/enum 이름이면 prefix 적용해서 재시도.
            # FnDef의 반환/매개변수 타입 어노테이션이 원래 이름(`Tween`)이지만
            # 실제 등록은 prefix 이름(`_mod_core_ari_tween_Tween`)으로 됐을 때 매칭.
            if name in getattr(self, '_from_imports', {}):
                aliased = self._from_imports[name]
                if aliased in self.structs:
                    return (self.structs[aliased][0], None)
                if aliased in self.tagged_enums:
                    return (self.tagged_enums[aliased][0], None)
            # 제네릭 enum 이름도 확인 — 단형화 전에는 tagged_enums에 없지만
            # 제네릭 enum 이름이 어노테이션에 올 수 있음 (Result, Option 등).
            # 이 경우 단형화는 호출 시점에 결정되므로, 여기서는 에러를 낸다.
            if hasattr(self, '_generic_enums') and name in self._generic_enums:
                raise DanhaTypeError(
                    f"제네릭 enum '{name}'은 타입 어노테이션에 직접 쓸 수 없어 — "
                    f"단형화된 이름을 써야 해 (예: 함수 내에서 직접 사용)",
                    line=line, source=self._source_code
                )
            raise DanhaTypeError(
                f"아직 지원 안 하는 타입이야 ({what}: {name}). "
                f"지원 타입: {', '.join(list(self._TYPE_MAP) + list(self.structs) + list(self.tagged_enums))}",
                line=line, source=self._source_code
            )

        if kind == 'GenericType':
            name = type_node[1]
            args = type_node[2]
            if name == 'HashMap':
                if len(args) != 2:
                    raise DanhaTypeError(
                        "HashMap 타입은 HashMap<key, value>처럼 타입 인자 2개가 필요해",
                        line=line, source=self._source_code
                    )
                key_node = args[0]
                if not (key_node[0] == 'TypeName' and key_node[1] in ('str', 'i32')):
                    raise DanhaTypeError(
                        "HashMap 키 타입은 지금 str 또는 i32만 지원해",
                        line=line, source=self._source_code
                    )
                # 값 타입은 typed get API와 문서화를 위한 표면이다. 저장은 i64 payload로 한다.
                self._resolve_type(args[1], f"{what} HashMap 값", line)
                return (i8p, None)
            raise DanhaTypeError(
                f"아직 지원 안 하는 제네릭 타입이야: {name}",
                line=line, source=self._source_code
            )
        
        if kind == 'RefType':
            # ('RefType', is_mut, inner_node, line)
            # 재귀로 내부 타입을 먼저 해석하고, 그 LLVM 타입을 포인터로 래핑.
            # 이중 참조(&&T)는 파서에서 이미 거부됨 — inner는 TypeName 또는 ArrayType.
            is_mut = type_node[1]
            inner_node = type_node[2]
            inner_ty, inner_ref = self._resolve_type(inner_node, what, line)
            if inner_ref is not None:
                # 방어적 검사: 파서가 허용했어도 의미가 없음.
                raise DanhaNameError(
                    f"참조의 참조는 쓸 수 없어 ({what})",
                    line=line, source=self._source_code
                )
            ref_kind = 'mut_ref' if is_mut else 'ref'
            return (inner_ty.as_pointer(), ref_kind)
        
        if kind == 'PtrType':
            # 7.17a: ptr 타입 = C의 void* = i8*
            # SDL_Window*, SDL_Renderer* 같은 불투명 C 포인터에 사용.
            # 단아 사용자는 내부 구조를 몰라도 되고, 그냥 extern fn에 넘기면 됨.
            return (i8p, None)

        if kind == 'DynType':
            # 29: dyn Trait — 동적 디스패치 타입
            # %dyn.TraitName = {i8*, i8*} (data_ptr, vtable_ptr)
            trait_name = type_node[1]
            if hasattr(self, '_dyn_types') and trait_name in self._dyn_types:
                return (self._dyn_types[trait_name], None)
            raise DanhaTypeError(
                f"정의되지 않은 트레잇이야: {trait_name} ({what})",
                line=line, source=self._source_code
            )

        if kind == 'ArrayType':
            # ('ArrayType', elem_node, length, line)
            # LLVM 고정 배열: ir.ArrayType(원소타입, 길이)
            # 예: [i32; 5] → [5 x i32]
            # 중첩 배열도 재귀로 자연스럽게 해석됨:
            #   [[i32; 3]; 4] → [4 x [3 x i32]]
            elem_node = type_node[1]
            length = type_node[2]
            elem_ty, elem_ref = self._resolve_type(elem_node, what, line)
            if elem_ref is not None:
                raise DanhaTypeError(
                    f"배열의 원소 타입에 참조는 쓸 수 없어 ({what})",
                    line=line, source=self._source_code
                )
            return (ir.ArrayType(elem_ty, length), None)

        if kind == 'DynArrayType':
            # ('DynArrayType', elem_node, line)
            # 동적 배열: 길이 생략 표기법 — [f64], [i32], [str] 등
            # LLVM struct { T*, i32 len, i32 cap } 으로 표현 (_get_dynarray_type)
            # 'arr: [f64] = []' 같은 어노테이션을 받아서 빈 dynarray의 원소 타입을 확정한다.
            elem_node = type_node[1]
            elem_ty, elem_ref = self._resolve_type(elem_node, what, line)
            if elem_ref is not None:
                raise DanhaTypeError(
                    f"동적 배열의 원소 타입에 참조는 쓸 수 없어 ({what})",
                    line=line, source=self._source_code
                )
            return (self._get_dynarray_type(elem_ty), None)
        
        # 42단계: fn(T1, T2) -> R — 함수 포인터 타입
        if kind == 'FnPtrType':
            fp_param_type_nodes = type_node[1]
            fp_ret_type_node = type_node[2]
            fp_param_llvm = []
            for ptn in fp_param_type_nodes:
                pt, _ = self._resolve_type(ptn, what, line)
                fp_param_llvm.append(pt)
            if fp_ret_type_node is None:
                fp_ret_llvm = ir.VoidType()
            else:
                ret_name = fp_ret_type_node[1] if fp_ret_type_node[0] == 'TypeName' else None
                if ret_name == 'unit':
                    fp_ret_llvm = ir.VoidType()
                else:
                    fp_ret_llvm, _ = self._resolve_type(fp_ret_type_node, what, line)
            fn_ty = ir.FunctionType(fp_ret_llvm, fp_param_llvm)
            return (fn_ty.as_pointer(), None)

        raise DanhaTypeError(f"알 수 없는 타입 노드: {type_node}", line=line, source=self._source_code)
    
    def _sizeof_bytes(self, llvm_ty):
        """7.15f: LLVM 스칼라 타입의 바이트 크기.
        컴포넌트 SoA malloc, 메모리 레이아웃 계산에 사용.
        """
        if isinstance(llvm_ty, ir.IntType):
            bits = llvm_ty.width
            return max(1, (bits + 7) // 8)
        if isinstance(llvm_ty, ir.DoubleType):
            return 8
        if isinstance(llvm_ty, ir.FloatType):
            return 4
        if isinstance(llvm_ty, ir.LiteralStructType):
            return sum(self._sizeof_bytes(el) for el in llvm_ty.elements)
        if isinstance(llvm_ty, ir.IdentifiedStructType):
            # 사용자 정의 struct (struct Player { ... }) — 필드 크기 합.
            return sum(self._sizeof_bytes(el) for el in llvm_ty.elements)
        if isinstance(llvm_ty, ir.VectorType):
            # SIMD 벡터: count * element_size (vec3는 lane 4로 패딩이라 32B)
            return llvm_ty.count * self._sizeof_bytes(llvm_ty.element)
        if isinstance(llvm_ty, ir.ArrayType):
            return llvm_ty.count * self._sizeof_bytes(llvm_ty.element)
        if isinstance(llvm_ty, ir.PointerType):
            return 8  # 64-bit 시스템
        raise DanhaTypeError(f"_sizeof_bytes: 지원 안 하는 타입 {llvm_ty}")
    
    def _coerce_to_component_field(self, v, target_ty, target_tname, fname, comp_name, line):
        """7.15f: 컴포넌트 필드 값을 선언 타입으로 변환.
        
        정책 (Q3=3):
          - 같은 타입: 그대로
          - i32 → f64/f32: 암묵 승격 (sitofp)
          - 정수 → 다른 크기 정수: 암묵 리사이즈 (trunc/sext)
          - f64 → f32: fptrunc (허용)
          - 부동 → 정수: 거부 (정밀도 손실)
          - 그 외 부적합: 에러
        """
        src_ty = v.type
        
        # 이미 같은 타입이면 그대로
        if src_ty == target_ty:
            return v
        
        # 정수 → 부동: sitofp (Q3=3 암묵 승격)
        if isinstance(src_ty, ir.IntType) and isinstance(target_ty, (ir.DoubleType, ir.FloatType)):
            return self.builder.sitofp(v, target_ty, name=f"{fname}_fp")
        
        # 정수 → 정수 (크기 조정)
        if isinstance(src_ty, ir.IntType) and isinstance(target_ty, ir.IntType):
            src_bits = src_ty.width
            dst_bits = target_ty.width
            if dst_bits > src_bits:
                # u* 계열은 zext, i* 는 sext. target_tname으로 판별.
                if target_tname.startswith('u') or target_tname == 'bool':
                    return self.builder.zext(v, target_ty, name=f"{fname}_zx")
                return self.builder.sext(v, target_ty, name=f"{fname}_sx")
            # 더 작거나 같음
            return self.builder.trunc(v, target_ty, name=f"{fname}_tr")
        
        # f64 → f32
        if isinstance(src_ty, ir.DoubleType) and isinstance(target_ty, ir.FloatType):
            return self.builder.fptrunc(v, target_ty, name=f"{fname}_ft")
        # f32 → f64 (잘 쓰지 않지만 허용)
        if isinstance(src_ty, ir.FloatType) and isinstance(target_ty, ir.DoubleType):
            return self.builder.fpext(v, target_ty, name=f"{fname}_fe")
        
        # 부동 → 정수: 거부
        if isinstance(src_ty, (ir.DoubleType, ir.FloatType)) and isinstance(target_ty, ir.IntType):
            raise DanhaECSError(
                f"컴포넌트 '{comp_name}'의 필드 '{fname}: {target_tname}'에 "
                f"부동소수값을 넣으려고 해. 정밀도가 손실되니 명시적 변환이 필요 "
                f"(단아 v1.0 이전엔 명시 캐스트 구문 미완성, 정수 리터럴을 써줘).",
                line=line, source=self._source_code
            )
        
        # 구조체(벡터 등) 같은 타입 — 정확 일치만 허용, 위에서 이미 걸러짐.
        raise DanhaTypeError(
            f"컴포넌트 '{comp_name}'의 필드 '{fname}: {target_tname}'에 "
            f"호환 안 되는 타입의 값이 들어왔어.",
            line=line, source=self._source_code
        )
    
    # ----- 7.7: 벡터 헬퍼 -----
    
    def _is_vec_type(self, llvm_ty):
        """LLVM 타입이 벡터 타입(vec2/vec3/vec4)인지 확인.
        반환: 벡터면 ('vec2'|'vec3'|'vec4', 성분수), 아니면 None."""
        for name, (vty, size) in self._VEC_INFO.items():
            if llvm_ty == vty:
                return (name, size)
        return None

    def _is_vec_slot(self, slot):
        """alloca 슬롯이 벡터를 가리키는지 확인."""
        if not hasattr(slot, 'type') or not hasattr(slot.type, 'pointee'):
            return None
        return self._is_vec_type(slot.type.pointee)

    # ----- Phase 1: SIMD 헬퍼 -----
    # 벡터 타입이 LiteralStructType이든 VectorType이든 똑같이 다룰 수 있도록 추상화.
    # 이렇게 두면 vec4를 <4 x double>로 바꿔도 공통 코드(_compile_vec_binop, dot, length 등)가
    # 그대로 작동한다. struct 경로(vec2/vec3 in Phase 1)는 그대로 유지됨.

    def _vec_extract(self, vec_val, idx, name="elt"):
        """벡터 값에서 idx번째 성분을 꺼낸다. struct/<N x T> 모두 지원."""
        if isinstance(vec_val.type, ir.VectorType):
            return self.builder.extract_element(vec_val, ir.Constant(i32, idx), name=name)
        return self.builder.extract_value(vec_val, idx, name=name)

    def _vec_insert(self, vec_val, scalar, idx, name="vec"):
        """벡터 값의 idx번째 성분에 scalar를 넣은 새 벡터를 반환."""
        if isinstance(vec_val.type, ir.VectorType):
            return self.builder.insert_element(vec_val, scalar, ir.Constant(i32, idx), name=name)
        return self.builder.insert_value(vec_val, scalar, idx, name=name)

    def _vec_load_extract(self, slot, idx, name="elt"):
        """벡터 alloca 슬롯에서 idx번째 성분을 읽는다.
        struct: GEP+load (현행). vector: load+extract_element."""
        pointee = slot.type.pointee
        if isinstance(pointee, ir.VectorType):
            v = self.builder.load(slot, name="vec_load")
            return self.builder.extract_element(v, ir.Constant(i32, idx), name=name)
        field_ptr = self.builder.gep(
            slot, [ir.Constant(i32, 0), ir.Constant(i32, idx)],
            inbounds=True, name=f"{name}_ptr"
        )
        return self.builder.load(field_ptr, name=name)

    def _vec_store_insert(self, slot, scalar, idx):
        """벡터 alloca 슬롯의 idx번째 성분을 scalar로 덮어쓴다.
        struct: GEP+store. vector: load+insert_element+store (read-modify-write)."""
        pointee = slot.type.pointee
        if isinstance(pointee, ir.VectorType):
            v = self.builder.load(slot, name="vec_rmw_load")
            new_v = self.builder.insert_element(v, scalar, ir.Constant(i32, idx), name="vec_rmw")
            self.builder.store(new_v, slot)
            return
        field_ptr = self.builder.gep(
            slot, [ir.Constant(i32, 0), ir.Constant(i32, idx)],
            inbounds=True, name="vec_field_ptr"
        )
        self.builder.store(scalar, field_ptr)
    
    def _is_mat_type(self, llvm_ty):
        """LLVM 타입이 mat4인지 확인."""
        return llvm_ty == self._mat4_ty

    # ----- Phase 3: mat4 컬럼 벡터 헬퍼 -----
    def _mat4_col(self, mat_val, col, name="col"):
        """mat4의 col번째 컬럼을 <4 x double>로 꺼낸다."""
        return self.builder.extract_value(mat_val, col, name=f"{name}{col}")

    def _mat4_set_col(self, mat_val, col, col_vec, name="mat"):
        """mat4의 col번째 컬럼을 col_vec(<4 x double>)으로 교체한 새 mat4 반환."""
        return self.builder.insert_value(mat_val, col_vec, col, name=f"{name}_c{col}")

    def _mat4_elem(self, mat_val, col, row, name="m"):
        """mat4의 (col, row) 원소를 f64로 꺼낸다."""
        col_vec = self._mat4_col(mat_val, col, name="ce")
        return self.builder.extract_element(col_vec, ir.Constant(i32, row), name=f"{name}{col}{row}")

    def _mat4_set_elem(self, mat_val, col, row, scalar, name="mat"):
        """mat4의 (col, row) 원소를 scalar로 교체."""
        col_vec = self._mat4_col(mat_val, col, name="ce")
        new_col = self.builder.insert_element(col_vec, scalar, ir.Constant(i32, row), name=f"nc{col}{row}")
        return self._mat4_set_col(mat_val, col, new_col, name=name)

    def _mat4_col_const(self, c0, c1, c2, c3):
        """f64 4개로 컬럼 상수 벡터 생성."""
        return ir.Constant(self._mat4_col_ty, [c0, c1, c2, c3])

    def _mat4_from_flat(self, vals_16):
        """16개 f64 값(또는 ir.Value)을 열 우선 순서로 받아 mat4 생성.
        모든 값이 ir.Constant면 상수 컬럼, 아니면 insert_element 체인.

        vals_16: list of (float | ir.Value), 인덱스 = col*4 + row
        """
        result = ir.Constant(self._mat4_ty, ir.Undefined)
        for c in range(4):
            col_items = [vals_16[c*4 + r] for r in range(4)]
            # 모두 float면 상수 컬럼
            if all(isinstance(v, (int, float)) for v in col_items):
                col = ir.Constant(self._mat4_col_ty, [float(v) for v in col_items])
            else:
                # 일부는 runtime — f64로 변환하고 insert_element 체인
                col = ir.Constant(self._mat4_col_ty, ir.Undefined)
                for r, v in enumerate(col_items):
                    if isinstance(v, (int, float)):
                        v = ir.Constant(f64, float(v))
                    col = self.builder.insert_element(col, v, ir.Constant(i32, r), name=f"mc{c}{r}")
            result = self.builder.insert_value(result, col, c, name=f"mat_c{c}")
        return result
    
    def _compile_vec_binop(self, left, right, int_op, float_op, line, op_name):
        """벡터 이항 연산을 컴파일한다.
        
        벡터+벡터 (같은 타입), 벡터*스칼라, 스칼라*벡터를 처리.
        성분을 하나씩 꺼내서 연산하고 다시 구조체로 조립.
        
        반환: ir.Value (벡터 결과) 또는 None (벡터 연산이 아님)
        """
        left_vec = self._is_vec_type(left.type)
        right_vec = self._is_vec_type(right.type)
        left_scalar = left.type in (i32, f64)
        right_scalar = right.type in (i32, f64)
        
        if left_vec and right_vec:
            # 벡터 op 벡터
            if left_vec[0] != right_vec[0]:
                raise DanhaTypeError(
                    f"{left_vec[0]}과 {right_vec[0]}은 서로 연산할 수 없어",
                    line=line, source=self._source_code
                )
            vec_name, size = left_vec
            vty = self._VEC_INFO[vec_name][0]
            # SIMD 빠른 경로: VectorType끼리는 LLVM이 packed instruction 생성
            if isinstance(vty, ir.VectorType):
                return float_op(left, right)
            result = ir.Constant(vty, ir.Undefined)
            for idx in range(size):
                lc = self._vec_extract(left, idx, name=f"l{idx}")
                rc = self._vec_extract(right, idx, name=f"r{idx}")
                comp = float_op(lc, rc)
                result = self._vec_insert(result, comp, idx, name=f"v{idx}")
            return result

        if left_vec and right_scalar:
            # 벡터 op 스칼라
            vec_name, size = left_vec
            vty = self._VEC_INFO[vec_name][0]
            # 스칼라를 f64로 승격
            s = right
            if s.type == i32:
                s = self.builder.sitofp(s, f64, name="s_promoted")
            # SIMD: 스칼라를 broadcast해서 packed 연산
            if isinstance(vty, ir.VectorType):
                splat = self._splat(s, vty)
                return float_op(left, splat)
            result = ir.Constant(vty, ir.Undefined)
            for idx in range(size):
                lc = self._vec_extract(left, idx, name=f"l{idx}")
                comp = float_op(lc, s)
                result = self._vec_insert(result, comp, idx, name=f"v{idx}")
            return result

        if left_scalar and right_vec:
            # 스칼라 op 벡터
            vec_name, size = right_vec
            vty = self._VEC_INFO[vec_name][0]
            s = left
            if s.type == i32:
                s = self.builder.sitofp(s, f64, name="s_promoted")
            if isinstance(vty, ir.VectorType):
                splat = self._splat(s, vty)
                return float_op(splat, right)
            result = ir.Constant(vty, ir.Undefined)
            for idx in range(size):
                rc = self._vec_extract(right, idx, name=f"r{idx}")
                comp = float_op(s, rc)
                result = self._vec_insert(result, comp, idx, name=f"v{idx}")
            return result

        return None  # 벡터 연산이 아님

    def _splat(self, scalar, vty):
        """스칼라 값을 벡터의 모든 레인에 복제한다.
        LLVM idiom: insertelement undef → shufflevector with zeroinitializer mask."""
        size = vty.count
        elt_ty = vty.element
        undef = ir.Constant(vty, ir.Undefined)
        single = self.builder.insert_element(undef, scalar, ir.Constant(i32, 0), name="splat_seed")
        mask_ty = ir.VectorType(i32, size)
        mask = ir.Constant(mask_ty, [0] * size)
        return self.builder.shuffle_vector(single, undef, mask, name="splat")

    def _vector_reduce_fadd(self, vec_val, name="vred"):
        """LLVM @llvm.vector.reduce.fadd intrinsic으로 수평 합산.
        identity는 -0.0 (IEEE 정확). fast-math 플래그가 있으면 0.0이어도 OK."""
        vty = vec_val.type
        elt_ty = vty.element
        size = vty.count
        # intrinsic 이름: @llvm.vector.reduce.fadd.v4f64 / .v4f32 / .v2f64 등
        suffix = f"v{size}" + ("f64" if elt_ty == f64 else "f32")
        fn_name = f"llvm.vector.reduce.fadd.{suffix}"
        # llvmlite의 module.declare_intrinsic 또는 직접 declare
        if fn_name not in self.module.globals:
            fnty = ir.FunctionType(elt_ty, [elt_ty, vty])
            ir.Function(self.module, fnty, name=fn_name)
        intr = self.module.globals[fn_name]
        identity = ir.Constant(elt_ty, -0.0)
        return self.builder.call(intr, [identity, vec_val], name=name)
    
    # ----- 수치 연산 헬퍼 -----
    # "양쪽이 모두 수치인지 검사하고, 한쪽이 실수면 반대쪽을 실수로 승격해서
    # (둘 다 실수, 공통 불리언)를 돌려준다."
    # 이게 있으면 산술/비교 연산마다 똑같은 승격 로직을 반복 안 해도 됨.
    # 비유: 덧셈 전에 '둘 다 같은 단위로 바꾸기' — 미터와 센티미터를 더하기 전에 하나로 맞추는 거.
    
    def _promote_numeric(self, left, right, line, op_name):
        """
        수치 연산의 양쪽 피연산자를 공통 타입으로 맞춰 돌려준다.

        승격 사다리: i32 ⊂ i64 ⊂ f32 ⊂ f64.
        - 둘 다 정수(i32/i64): 좁은 쪽을 sext로 넓은 쪽으로 → 공통 정수
        - 한쪽이 실수, 한쪽이 정수: 정수를 sitofp로 같은 폭의 실수로
        - 둘 다 실수지만 폭 다름 (f32/f64): f32 쪽을 fpext로 f64로
        - i1(불리언)이 산술에 끼면 에러 — C와 달리 단아는 bool/숫자를 안 섞음
        """
        i64ty = ir.IntType(64)
        numeric = (i32, i64ty, f32, f64)
        if left.type not in numeric or right.type not in numeric:
            raise DanhaRuntimeError(
                f"'{op_name}'는 숫자에만 쓸 수 있어 "
                f"({left.type}, {right.type})",
                line=line, source=self._source_code
            )

        # 둘 다 f32: 결과 f32
        if left.type == f32 and right.type == f32:
            return left, right, True

        # 둘 중 하나라도 f64이면 양쪽 모두 f64로
        if left.type == f64 or right.type == f64:
            if left.type == i32:
                left = self.builder.sitofp(left, f64, name="i32_to_f64")
            elif left.type == i64ty:
                left = self.builder.sitofp(left, f64, name="i64_to_f64")
            elif left.type == f32:
                left = self.builder.fpext(left, f64, name="f32_to_f64")
            if right.type == i32:
                right = self.builder.sitofp(right, f64, name="i32_to_f64")
            elif right.type == i64ty:
                right = self.builder.sitofp(right, f64, name="i64_to_f64")
            elif right.type == f32:
                right = self.builder.fpext(right, f64, name="f32_to_f64")
            return left, right, True

        # 한쪽 f32 + 한쪽 정수 → f32 (f32 + i32, f32 + i64 정수→f32)
        if left.type == f32 or right.type == f32:
            if left.type == i32:
                left = self.builder.sitofp(left, f32, name="i32_to_f32")
            elif left.type == i64ty:
                left = self.builder.sitofp(left, f32, name="i64_to_f32")
            if right.type == i32:
                right = self.builder.sitofp(right, f32, name="i32_to_f32")
            elif right.type == i64ty:
                right = self.builder.sitofp(right, f32, name="i64_to_f32")
            return left, right, True

        # 둘 다 정수: i32 ↔ i64면 i32 쪽을 sext로 i64로
        if left.type != right.type:
            if left.type == i32 and right.type == i64ty:
                left = self.builder.sext(left, i64ty, name="i32_to_i64")
            elif left.type == i64ty and right.type == i32:
                right = self.builder.sext(right, i64ty, name="i32_to_i64")
        return left, right, False

    def _promote_int_pair(self, left, right, line):
        """비트 연산을 위한 정수 타입 맞춤.
        두 피연산자 모두 IntType이어야 한다. 폭이 다르면 좁은 쪽을 zext로 확장."""
        if not isinstance(left.type, ir.IntType) or not isinstance(right.type, ir.IntType):
            raise DanhaRuntimeError(
                f"[{line}번째 줄] 비트 연산은 정수에만 쓸 수 있어 "
                f"({left.type}, {right.type})",
                source=self._source_code
            )
        if left.type.width < right.type.width:
            left = self.builder.zext(left, right.type, name="bitop_ext")
        elif right.type.width < left.type.width:
            right = self.builder.zext(right, left.type, name="bitop_ext")
        return left, right

    # ----- 스코프 헬퍼 -----
    # 양파 껍질 모델. 가장 안쪽이 self.vars[-1], 가장 바깥(함수 entry)이 self.vars[0].
    
    def _push_scope(self):
        """블록에 들어갈 때: 새 빈 껍질 추가."""
        self.vars.append({})
    
    def _pop_scope(self):
        """블록에서 나올 때: 가장 안쪽 껍질 제거.
        그 안에서 alloca한 메모리 자체는 사라지지 않음 (스택은 함수 끝에 회수).
        다만 우리가 그 변수 이름을 더 이상 못 찾게 되는 효과만 있음."""
        self.vars.pop()
    
    def _lookup_var(self, name):
        """안쪽부터 바깥으로 훑으며 변수의 슬롯을 찾는다. 없으면 None.
        7.15d: 지역 스코프에 없으면 글로벌 변수(최상위 선언)도 확인.
        이게 있어야 system/함수/메서드 본문에서 최상위 변수(예: player)를 참조 가능."""
        for scope in reversed(self.vars):
            if name in scope:
                return scope[name]
        # 지역에 없으면 글로벌 확인
        if name in self._globals:
            return self._globals[name]
        return None
    
    def _declare_var(self, name, slot):
        """가장 안쪽 스코프에 새 변수를 만든다."""
        self.vars[-1][name] = slot
    
    def _find_free_vars(self, body_stmts, param_names):
        """Lambda 본문에서 매개변수가 아닌 외부 변수 이름을 찾는다.
        반환: 외부 변수 이름의 리스트 (순서 보장, 중복 없음)."""
        free = []
        seen = set(param_names)
        local_decls = set()
        
        # 내장 함수 / 전역 함수 / 타입 이름은 캡처 대상이 아님
        builtins = {'print', 'println', 'len', 'push', 'pop', 'to_string',
                    'to_int', 'to_float', 'parse_int', 'abs', 'sqrt', 'sin', 'cos', 'tan',
                    'floor', 'ceil', 'round', 'min', 'max', 'pow', 'log',
                    'input', 'typeof', 'assert', 'panic', 'clock', 'sleep',
                    'random', 'random_range', 'array_new', 'Array'}
        
        def walk(node):
            if node is None or not isinstance(node, tuple) or len(node) == 0:
                return
            ntype = node[0]
            
            if ntype == 'Name':
                name = node[1]
                if (name not in seen and name not in local_decls 
                    and name not in builtins
                    and name not in self.functions
                    and name not in self.structs
                    and name not in self.tagged_enums):
                    if name not in free:
                        free.append(name)
                return
            
            # let / var 선언은 지역 변수로 등록
            if ntype in ('VarDecl', 'LetDecl'):
                local_decls.add(node[1])
                if len(node) > 2:
                    walk(node[2])  # 초기값
                return
            
            if ntype == 'Assign':
                # 대입 대상이 새 변수일 수도
                if isinstance(node[1], tuple) and node[1][0] == 'Name':
                    target_name = node[1][1]
                    if (target_name not in seen and target_name not in local_decls
                        and self._lookup_var(target_name) is not None):
                        if target_name not in free:
                            free.append(target_name)
                walk(node[1])
                walk(node[2])
                return
            
            # 중첩 Lambda — 내부 Lambda의 매개변수는 스킵
            if ntype == 'Lambda':
                inner_params = set(node[1])
                for stmt in node[2][1]:
                    _walk_with_shadow(stmt, inner_params)
                return
            
            # 나머지: 모든 자식을 재귀
            for child in node[1:]:
                if isinstance(child, tuple):
                    walk(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, tuple):
                            walk(item)
        
        def _walk_with_shadow(node, shadow):
            """중첩 Lambda에서 추가로 가려진 이름을 처리."""
            if node is None or not isinstance(node, tuple) or len(node) == 0:
                return
            ntype = node[0]
            if ntype == 'Name':
                name = node[1]
                if (name not in seen and name not in local_decls
                    and name not in shadow and name not in builtins
                    and name not in self.functions
                    and name not in self.structs
                    and name not in self.tagged_enums):
                    if name not in free:
                        free.append(name)
                return
            for child in node[1:]:
                if isinstance(child, tuple):
                    _walk_with_shadow(child, shadow)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, tuple):
                            _walk_with_shadow(item, shadow)
        
        for stmt in body_stmts:
            walk(stmt)
        return free
    
    def _root_name_of(self, node):
        """
        식의 '뿌리 변수 이름'을 돌려준다 (있으면).
        - Name이면 그 이름.
        - FieldAccess면 객체 식을 따라 올라가 재귀.
        - 그 외(호출 결과, 구조체 리터럴 등)는 None.
        7.1.3 파트 3: 읽기 참조 매개변수의 '뿌리'에서 쓰기를 시도하는지 검사에 씀.
        예) 'p.inner.x = 0' → 뿌리는 'p'. p가 '&P'로 받은 거면 쓰기 금지.
        """
        if node[0] == 'Name':
            return node[1]
        if node[0] == 'FieldAccess':
            return self._root_name_of(node[1])
        return None
    
    def _check_arg_ref_match(self, callee, idx, caller_ref, sig_ref, line):
        """7.1.3 파트 3 검사 B: 호출자의 참조 종류와 시그니처가 맞는지.
        caller_ref / sig_ref: None / 'ref' / 'mut_ref'
        - 시그니처가 값(None)인데 호출이 참조('&'/'&mut'): 불필요한 참조 — 에러.
        - 시그니처가 참조('&'/'&mut')인데 호출이 값: '&' 빠뜨림 — 에러.
        - 시그니처 '&mut', 호출 '&': 쓰기 권한 부족 — 에러.
        - 시그니처 '&', 호출 '&mut': 권한 강등 — 허용 (더 큰 권한이 작은 권한을 품음).
        - 같은 종류끼리: 허용.
        """
        if caller_ref == sig_ref:
            return
        if sig_ref is None:
            raise DanhaValueError(
                f"{callee}의 {idx+1}번째 인자는 값으로 전달해야 하는데 "
                f"'&' 또는 '&mut'로 넘겼어 — '&'를 빼거나 시그니처를 '&T'로 바꿔",
                line=line, source=self._source_code
            )
        if caller_ref is None:
            expected = "'&mut'" if sig_ref == 'mut_ref' else "'&'"
            raise DanhaValueError(
                f"{callee}의 {idx+1}번째 인자는 {expected}로 넘겨야 해 "
                f"(시그니처가 참조를 기대해)",
                line=line, source=self._source_code
            )
        if sig_ref == 'mut_ref' and caller_ref == 'ref':
            raise DanhaValueError(
                f"{callee}의 {idx+1}번째 인자는 '&mut'로 넘겨야 해 "
                f"(시그니처가 '&mut'을 기대하는데 '&'로 왔음 — 쓰기 권한 부족)",
                line=line, source=self._source_code
            )
        # sig='ref', caller='mut_ref'는 허용 (강등).

    def _alloca_at_entry(self, ty, name):
        """함수 진입 블록의 시작 위치에 alloca를 만든다.
        루프 본문에서 builder.alloca(...)를 직접 호출하면 매 반복마다 스택을
        먹어 ~100K iter에서 STATUS_STACK_OVERFLOW를 일으킨다. LLVM의 mem2reg는
        alloca가 entry에 있을 때만 promotion을 신뢰성 있게 수행하므로 모든
        '루프 안에 보일 수 있는' alloca는 이 헬퍼를 거쳐야 한다.
        """
        entry_block = self.current_fn.entry_basic_block
        cur_block = self.builder.block
        self.builder.position_at_start(entry_block)
        slot = self.builder.alloca(ty, name=name)
        self.builder.position_at_end(cur_block)
        return slot

    def _lvalue_struct(self, node, line):
        """
        구조체에 대한 식의 '주소'를 돌려준다 (필드 접근/대입에 필요).
        Name과 FieldAccess 모두 지원 — 'p.field', 'p.inner.x' 등 중첩 가능.
        
        반환: (포인터_value, struct_info_튜플)
              struct_info는 self.structs[type_name]과 같은 모양.
        
        두 가지 슬롯 모양을 모두 처리:
        - 일반 변수: slot은 구조체* (예: 'p = Player{...}'의 p)
                    → 슬롯 자체를 그대로 돌려줌
        - self 같은 포인터 매개변수: slot은 구조체** (포인터를 담은 슬롯)
                                     → load 한 번 해서 진짜 구조체 주소를 얻음
        """
        # 8.1a: FieldAccess 재귀 — 부모 구조체에서 GEP로 내부 구조체 필드 포인터를 얻음
        if node[0] == 'FieldAccess':
            parent_node = node[1]
            field_name = node[2]
            parent_ptr, parent_info = self._lvalue_struct(parent_node, line)
            llvm_struct, field_names, field_types = parent_info
            if field_name not in field_names:
                raise DanhaNameError(f"그 구조체에 '{field_name}'이라는 필드가 없어", line=line, source=self._source_code)
            idx = field_names.index(field_name)
            # 46단계: union은 GEP(0) + bitcast, struct는 indexed GEP
            _sname = self._struct_name_from_llvm(llvm_struct)
            if _sname in self.unions:
                _base = self.builder.gep(parent_ptr, [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True)
                field_ptr = self.builder.bitcast(_base, field_types[idx].as_pointer(), name=f"{field_name}_ptr")
            else:
                field_ptr = self.builder.gep(
                    parent_ptr, [ir.Constant(i32, 0), ir.Constant(i32, idx)],
                    inbounds=True, name=f"{field_name}_ptr"
                )
            field_ty = field_types[idx]
            # 필드 자체가 구조체인 경우 → 그 구조체의 info를 찾아 반환
            if isinstance(field_ty, ir.IdentifiedStructType):
                for sname, info in self.structs.items():
                    if info[0] is field_ty:
                        return field_ptr, info
                raise DanhaTypeError(f"알 수 없는 구조체 타입: {field_ty}", line=line, source=self._source_code)
            # 8.1a: 필드가 벡터인 경우 → 벡터 info를 struct_info 호환 형태로 반환
            vec_info = self._is_vec_type(field_ty)
            if vec_info is not None:
                vec_name, size = vec_info
                fields_map = self._VEC_FIELDS[vec_name]
                # struct_info 형태: (llvm_type, field_names_list, field_types_list)
                sorted_fields = sorted(fields_map.items(), key=lambda x: x[1])
                vec_field_names = [f[0] for f in sorted_fields]
                vec_field_types = [f64] * size
                return field_ptr, (field_ty, vec_field_names, vec_field_types)
            # 필드가 구조체도 벡터도 아닌 경우 — 호출자가 이걸 구조체로 쓰려 하면 에러 (예: p.x.y)
            raise DanhaTypeError(f"'{field_name}' 필드는 구조체가 아니야", line=line, source=self._source_code)
        
        if node[0] == 'Name':
            var_name = node[1]
            slot = self._lookup_var(var_name)
            if slot is None:
                raise DanhaNameError(f"정의되지 않은 이름이야: {var_name}", line=line, source=self._source_code)

            elem_ty = slot.type.pointee

            # 케이스 1: 슬롯에 구조체 자체가 들어 있음
            if isinstance(elem_ty, ir.IdentifiedStructType):
                struct_ptr = slot
                struct_ty = elem_ty
            # 케이스 2: 슬롯에 구조체 포인터가 들어 있음 (메서드의 self)
            elif isinstance(elem_ty, ir.PointerType) and isinstance(elem_ty.pointee, ir.IdentifiedStructType):
                struct_ptr = self.builder.load(slot, name=f"{var_name}_load")
                struct_ty = elem_ty.pointee
            else:
                raise DanhaTypeError(f"'{var_name}'은(는) 구조체가 아니야", line=line, source=self._source_code)

            # 어느 struct인지 이름으로 역검색
            for sname, info in self.structs.items():
                if info[0] is struct_ty:
                    return struct_ptr, info
            raise DanhaTypeError(f"알 수 없는 구조체 타입: {struct_ty}", line=line, source=self._source_code)

        # ps[j].field = value 같은 경우: obj_node가 Index 노드 (인덱스된 구조체).
        # 동적 배열의 j번째 원소가 구조체일 때, 그 원소의 주소(struct*)를 돌려준다.
        if node[0] == 'Index':
            arr_node = node[1]
            idx_node = node[2]
            if arr_node[0] != 'Name':
                raise DanhaRuntimeError(
                    "인덱스된 구조체 필드 쓰기는 배열 변수에만 가능해",
                    line=line, source=self._source_code
                )
            arr_name = arr_node[1]
            slot = self._lookup_var(arr_name)
            if slot is None:
                raise DanhaNameError(f"정의되지 않은 이름이야: {arr_name}", line=line, source=self._source_code)
            idx_val = self.compile_expr(idx_node)
            if idx_val.type != i32:
                raise DanhaValueError("배열 인덱스는 정수여야 해", line=line, source=self._source_code)
            # 동적 배열: data 포인터에 GEP → 원소 주소
            if self._is_dynarray_slot(slot):
                elem_ty = self._dynarray_elem_ty(slot)
                if not isinstance(elem_ty, ir.IdentifiedStructType):
                    raise DanhaTypeError(
                        f"'{arr_name}'의 원소는 구조체가 아니야 (인덱스된 필드 쓰기 불가)",
                        line=line, source=self._source_code
                    )
                data_ptr = self._dynarray_get_field(slot, 0, f"{arr_name}_data")
                data = self.builder.load(data_ptr, name="data")
                elem_ptr = self.builder.gep(data, [idx_val], inbounds=True, name=f"{arr_name}_elem")
                for sname, info in self.structs.items():
                    if info[0] is elem_ty:
                        return elem_ptr, info
                raise DanhaTypeError(f"알 수 없는 구조체 타입: {elem_ty}", line=line, source=self._source_code)
            # 고정 배열
            arr_ptr, elem_ty, _arr_len = self._lvalue_array(arr_name, line)
            if not isinstance(elem_ty, ir.IdentifiedStructType):
                raise DanhaTypeError(
                    f"'{arr_name}'의 원소는 구조체가 아니야 (인덱스된 필드 쓰기 불가)",
                    line=line, source=self._source_code
                )
            elem_ptr = self.builder.gep(
                arr_ptr, [ir.Constant(i32, 0), idx_val],
                inbounds=True, name=f"{arr_name}_elem"
            )
            for sname, info in self.structs.items():
                if info[0] is elem_ty:
                    return elem_ptr, info
            raise DanhaTypeError(f"알 수 없는 구조체 타입: {elem_ty}", line=line, source=self._source_code)

        raise DanhaRuntimeError("필드 접근/쓰기는 변수나 구조체 필드에서만 가능해", line=line, source=self._source_code)
    
    def _lvalue_struct_or_none(self, node, line):
        """_lvalue_struct와 같지만, lvalue로 풀 수 없는 노드면 (None, None)을 반환.
        8.1b: 함수 반환값 등 rvalue에서 필드 접근 시 폴백 분기에 사용."""
        try:
            return self._lvalue_struct(node, line)
        except Exception:
            return None, None
    
    def _lvalue_array(self, var_name, line):
        """
        배열 변수의 '배열 포인터'와 원소 타입 정보를 돌려준다.
        _lvalue_struct와 같은 패턴 — 참조 매개변수는 포인터가 한 겹 더 있어서
        load 한 번 해줘야 진짜 배열 주소를 얻음.
        
        반환: (arr_ptr, elem_ty, arr_len)
          arr_ptr: [N x T]* — GEP의 첫 인자로 쓸 수 있는 포인터
          elem_ty: T — 원소의 LLVM 타입
          arr_len: N — 배열 길이 (런타임 범위 검사용)
        
        두 가지 슬롯 모양:
        - 일반 변수:     slot은 [N x T]* (alloca 결과)
        - 참조 매개변수: slot은 [N x T]** (포인터를 담은 슬롯)
                         → load 한 번 해서 [N x T]* 를 얻음
        
        동적 배열은 여기서 처리 안 함 — _lvalue_dynarray를 대신 쓴다.
        """
        slot = self._lookup_var(var_name)
        if slot is None:
            raise DanhaNameError(f"정의되지 않은 이름이야: {var_name}", line=line, source=self._source_code)
        
        inner = slot.type.pointee
        
        # 동적 배열이면 여기서 에러 — 호출자가 _is_dynarray로 먼저 확인해야 함
        if self._is_dynarray_slot(slot):
            raise DanhaNameError(
                f"'{var_name}'은(는) 동적 배열이야 — 고정 배열 연산을 쓸 수 없어",
                line=line, source=self._source_code
            )
        
        # 케이스 1: 슬롯에 배열 자체가 들어 있음
        if isinstance(inner, ir.ArrayType):
            return slot, inner.element, inner.count
        
        # 케이스 2: 슬롯에 배열 포인터가 들어 있음 (참조 매개변수)
        if isinstance(inner, ir.PointerType) and isinstance(inner.pointee, ir.ArrayType):
            arr_ptr = self.builder.load(slot, name=f"{var_name}_load")
            arr_ty = inner.pointee
            return arr_ptr, arr_ty.element, arr_ty.count
        
        raise DanhaRuntimeError(f"'{var_name}'은(는) 배열이 아니야", line=line, source=self._source_code)
    
    def _lvalue_array_field(self, node, line):
        """
        ('FieldAccess', parent_node, field_name, line) 에서 배열 필드 포인터를 얻는다.
        struct 안의 고정 배열 필드 읽기/쓰기에 사용.
        반환: (arr_ptr, elem_ty, arr_len)
          arr_ptr : [N x T]* — GEP의 첫 인자로 쓸 수 있는 포인터
          elem_ty : T — 원소의 LLVM 타입
          arr_len : N — 배열 길이
        """
        if node[0] != 'FieldAccess':
            raise DanhaTypeError("배열 필드 접근은 'obj.field' 형태여야 해", line=line, source=self._source_code)
        parent_node = node[1]
        field_name = node[2]
        parent_ptr, parent_info = self._lvalue_struct(parent_node, line)
        llvm_struct, field_names, field_types = parent_info
        if field_name not in field_names:
            raise DanhaNameError(f"그 구조체에 '{field_name}'이라는 필드가 없어", line=line, source=self._source_code)
        fidx = field_names.index(field_name)
        field_ty = field_types[fidx]
        if not isinstance(field_ty, ir.ArrayType):
            raise DanhaTypeError(
                f"'{field_name}'은(는) 배열 타입이 아니야 (타입: {field_ty}) — 인덱스 접근 불가",
                line=line, source=self._source_code
            )
        arr_ptr = self.builder.gep(
            parent_ptr, [ir.Constant(i32, 0), ir.Constant(i32, fidx)],
            inbounds=True, name=f"{field_name}_arr"
        )
        return arr_ptr, field_ty.element, field_ty.count

    # ----- 7.4: 동적 배열 헬퍼 -----
    # 동적 배열의 LLVM 구조체: { T*, i32, i32 } (data 포인터, 현재 길이, 용량)
    # 비유: 고무줄 가방. data=물건이 있는 창고 주소, len=지금 물건 수, cap=창고 최대 칸 수.
    # 물건이 꽉 차면(len==cap) 더 큰 창고를 빌려서(realloc) 이사함.

    # 동적 배열 타입은 원소 타입별로 다른 LLVM 구조체. 캐시해둠.
    _dynarray_types = {}  # 클래스 변수 — elem_ty → ir.LiteralStructType
    
    def _get_dynarray_type(self, elem_ty):
        """원소 타입에 맞는 동적 배열 구조체 타입을 돌려준다.
        { T*, i32, i32 } — data, len, cap."""
        key = str(elem_ty)
        if key not in self._dynarray_types:
            self._dynarray_types[key] = ir.LiteralStructType([
                elem_ty.as_pointer(),  # data: T*
                i32,                    # len
                i32,                    # cap
            ])
        return self._dynarray_types[key]
    
    def _is_dynarray_slot(self, slot):
        """이 변수 슬롯이 동적 배열인지 확인한다.
        슬롯은 alloca 결과 → slot.type = { T*, i32, i32 }*
        내부 구조체가 { 포인터, i32, i32 } 형태면 동적 배열."""
        inner = slot.type.pointee
        if not isinstance(inner, ir.LiteralStructType):
            return False
        elems = inner.elements
        if len(elems) != 3:
            return False
        return (isinstance(elems[0], ir.PointerType) and
                elems[1] == i32 and elems[2] == i32)
    
    def _dynarray_elem_ty(self, slot):
        """동적 배열 슬롯에서 원소 타입(T)을 꺼낸다."""
        return slot.type.pointee.elements[0].pointee
    
    def _dynarray_get_field(self, slot, field_idx, name):
        """동적 배열 구조체의 필드 포인터를 돌려준다.
        field_idx: 0=data, 1=len, 2=cap"""
        return self.builder.gep(
            slot, [ir.Constant(i32, 0), ir.Constant(i32, field_idx)],
            inbounds=True, name=name
        )
    
    def _arena_alloc(self, byte_size_val, name="arena_ptr"):
        """아레나에서 byte_size 만큼 메모리를 할당한다.

        동작: base + offset 위치의 포인터를 돌려주고, offset += byte_size.
        malloc과 달리 시스템 호출 없이 정수 덧셈만으로 끝남 → 매우 빠름.

        과거엔 검사 없는 인라인 bump였지만, 용량 초과 시 조용한 힙 손상을
        일으켜 _danha_arena_alloc 런타임 호출로 교체 (경계 검사 + 청크 성장,
        _define_arena_runtime 참고).

        byte_size_val: i32 LLVM 값 (바이트 수)
        반환: i8* (할당된 메모리 시작 주소)
        """
        return self.builder.call(self.arena_alloc_fn, [byte_size_val], name=name)
    
    # ===== 24b: Arena 컴파일러 메서드 =====
    
    def _compile_arena_static_method(self, method_name, args, line):
        """24b: Arena.new/reset/destroy/used/capacity 정적 메서드 컴파일.
        
        Arena는 { i8* base, i32 offset, i32 capacity } 구조체를 힙에 할당.
        반환값은 arena_type* 포인터.
        """
        i64 = ir.IntType(64)
        
        if method_name == 'new':
            if len(args) != 1:
                raise DanhaValueError("Arena.new(크기)에는 인자 1개가 필요해", line=line, source=self._source_code)
            cap_val = self.compile_expr(args[0])
            if cap_val.type != i32:
                raise DanhaTypeError("Arena.new의 인자는 정수여야 해", line=line, source=self._source_code)
            
            # 구조체 크기 = sizeof(arena_type) = 8 + 4 + 4 = 16 바이트
            arena_mem = self.builder.call(
                self.malloc_fn,
                [ir.Constant(i64, 16)],
                name="arena_struct"
            )
            arena_ptr = self.builder.bitcast(
                arena_mem, self.arena_type.as_pointer(), name="arena_ptr"
            )
            
            # base = malloc(capacity)
            cap_i64 = self.builder.sext(cap_val, i64, name="cap_i64")
            base_mem = self.builder.call(
                self.malloc_fn, [cap_i64], name="arena_base_mem"
            )
            
            # arena.base = base_mem
            base_field = self.builder.gep(
                arena_ptr, [ir.Constant(i32, 0), ir.Constant(i32, 0)],
                inbounds=True, name="arena_base_f"
            )
            self.builder.store(base_mem, base_field)
            
            # arena.offset = 0
            off_field = self.builder.gep(
                arena_ptr, [ir.Constant(i32, 0), ir.Constant(i32, 1)],
                inbounds=True, name="arena_off_f"
            )
            self.builder.store(ir.Constant(i32, 0), off_field)
            
            # arena.capacity = cap_val
            cap_field = self.builder.gep(
                arena_ptr, [ir.Constant(i32, 0), ir.Constant(i32, 2)],
                inbounds=True, name="arena_cap_f"
            )
            self.builder.store(cap_val, cap_field)
            
            return arena_ptr
        
        elif method_name == 'alloc':
            # 25b: Arena.alloc(arena, size) 정적 호출
            if len(args) != 2:
                raise DanhaValueError("Arena.alloc(arena, size)에는 인자 2개가 필요해", line=line, source=self._source_code)
            arena_ptr = self.compile_expr(args[0])
            # args[1]은 instance method에 전달
            return self._compile_arena_instance_method(arena_ptr, 'alloc', [args[1]], line)
        
        elif method_name in ('reset', 'destroy', 'used', 'capacity'):
            if len(args) != 1:
                raise DanhaValueError(f"Arena.{method_name}(arena)에는 인자 1개가 필요해", line=line, source=self._source_code)
            arena_ptr = self.compile_expr(args[0])
            return self._compile_arena_instance_method(arena_ptr, method_name, [], line)
        
        else:
            raise DanhaNameError(f"Arena에 '{method_name}'이라는 메서드는 없어", line=line, source=self._source_code)
    
    def _compile_arena_instance_method(self, arena_ptr, method_name, args, line):
        """24b: arena 인스턴스 메서드 컴파일 — a.alloc()/reset()/destroy()/used()/capacity()"""
        
        if method_name == 'alloc':
            # 25b: arena.alloc(size) — bump 할당, 오프셋 반환
            if len(args) != 1:
                raise DanhaValueError("arena.alloc(size)에는 인자 1개가 필요해", line=line, source=self._source_code)
            size_val = self.compile_expr(args[0])
            if size_val.type != i32:
                raise DanhaTypeError("arena.alloc의 인자는 정수여야 해", line=line, source=self._source_code)
            
            ok_bb = self.current_fn.append_basic_block(name="arena_alloc_ok")
            overflow_bb = self.current_fn.append_basic_block(name="arena_alloc_overflow")

            # 현재 offset/capacity 읽기
            off_field = self.builder.gep(
                arena_ptr, [ir.Constant(i32, 0), ir.Constant(i32, 1)],
                inbounds=True, name="aalloc_off_p"
            )
            cur_off = self.builder.load(off_field, name="aalloc_cur")
            cap_field = self.builder.gep(
                arena_ptr, [ir.Constant(i32, 0), ir.Constant(i32, 2)],
                inbounds=True, name="aalloc_cap_p"
            )
            cap_val = self.builder.load(cap_field, name="aalloc_cap")
            
            new_off = self.builder.add(cur_off, size_val, name="aalloc_new")
            fits = self.builder.icmp_unsigned('<=', new_off, cap_val, name="aalloc_fits")
            self.builder.cbranch(fits, ok_bb, overflow_bb)

            self.builder.position_at_end(overflow_bb)
            msg = self._make_global_string(
                f"arena_instance_overflow_msg_{id(args)}",
                "danha arena: allocation exceeds capacity\n\0",
            )
            msg_ptr = self.builder.gep(
                msg, [ir.Constant(i32, 0), ir.Constant(i32, 0)],
                inbounds=True, name="aalloc_msg"
            )
            puts_fn = self._ensure_function("puts", i32, [i8p])
            self.builder.call(puts_fn, [msg_ptr])
            if not hasattr(self, 'exit_fn'):
                self.exit_fn = self._ensure_function("exit", ir.VoidType(), [i32])
            self.builder.call(self.exit_fn, [ir.Constant(i32, 1)])
            self.builder.unreachable()

            self.builder.position_at_end(ok_bb)
            self.builder.store(new_off, off_field)
            
            # 이전 offset 반환 (할당 시작 위치)
            return cur_off
        
        elif method_name == 'reset':
            # offset = 0
            off_field = self.builder.gep(
                arena_ptr, [ir.Constant(i32, 0), ir.Constant(i32, 1)],
                inbounds=True, name="arst_off"
            )
            self.builder.store(ir.Constant(i32, 0), off_field)
            return ir.Constant(i32, 0)  # void 반환 대용
        
        elif method_name == 'destroy':
            # free(base), 그 다음 free(arena_ptr)
            base_field = self.builder.gep(
                arena_ptr, [ir.Constant(i32, 0), ir.Constant(i32, 0)],
                inbounds=True, name="ards_base"
            )
            base = self.builder.load(base_field, name="ards_base_val")
            # free 함수가 있으면 호출 — 없으면 선언
            if not hasattr(self, 'free_fn'):
                free_ty = ir.FunctionType(ir.VoidType(), [i8p])
                self.free_fn = ir.Function(self.module, free_ty, name="free")
            self.builder.call(self.free_fn, [base])
            arena_i8 = self.builder.bitcast(arena_ptr, i8p, name="arena_i8")
            self.builder.call(self.free_fn, [arena_i8])
            return ir.Constant(i32, 0)
        
        elif method_name == 'used':
            off_field = self.builder.gep(
                arena_ptr, [ir.Constant(i32, 0), ir.Constant(i32, 1)],
                inbounds=True, name="aru_off"
            )
            return self.builder.load(off_field, name="arena_used")
        
        elif method_name == 'capacity':
            cap_field = self.builder.gep(
                arena_ptr, [ir.Constant(i32, 0), ir.Constant(i32, 2)],
                inbounds=True, name="arc_cap"
            )
            return self.builder.load(cap_field, name="arena_cap")
        
        else:
            raise DanhaNameError(f"아레나에 '{method_name}'이라는 메서드는 없어", line=line, source=self._source_code)
    
    def _detect_uniform_payload(self, variant_info):
        """Stage 75a: variant_info {vname: (tag, llvm_types|None)} 에서
        모든 1-binding variant가 동일 primitive 타입을 쓰면 그 타입 반환, 아니면 None.
        0-binding variant는 제약 안 줌. payload 종류 ≥2 또는 multi-binding이면 None.
        """
        common_ty = None
        for vname, (tag, llvm_types) in variant_info.items():
            if llvm_types is None or len(llvm_types) == 0:
                continue
            if len(llvm_types) != 1:
                return None
            t = llvm_types[0]
            # primitive 타입만 허용 (struct/array는 SSA 친화 안 됨)
            if not (t == i32 or t == ir.IntType(64) or t == f64 or t == i8p or t == ir.IntType(8) or t == ir.IntType(1)):
                return None
            if common_ty is None:
                common_ty = t
            elif common_ty != t:
                return None
        return common_ty

    def _tagged_uses_typed_payload(self, tagged_ty):
        """tagged_ty.elements[1] 이 [N x i8] 인지 (구 repr) primitive 인지 (Stage 75a repr) 판별."""
        return not isinstance(tagged_ty.elements[1], ir.ArrayType)

    def _build_tagged_enum(self, enum_name, tag, llvm_types, args, line, compiled_values=None):
        """8.3: tagged enum 값을 LLVM IR로 생성.
        Stage 75a: tagged_ty.elements[1]이 primitive면 SSA insertvalue 경로 (alloca 없음).
        그 외(레거시 [N x i8] repr)는 alloca + GEP + bitcast 경로.
        compiled_values: 이미 컴파일된 LLVM 값 리스트 (있으면 args 무시)
        """
        tagged_ty, variant_info, max_payload = self.tagged_enums[enum_name]

        # Stage 75a: typed payload 경로 — SSA로 빌드, alloca 안 씀
        if self._tagged_uses_typed_payload(tagged_ty):
            payload_ty = tagged_ty.elements[1]
            agg = ir.Constant(tagged_ty, ir.Undefined)
            agg = self.builder.insert_value(agg, ir.Constant(i32, tag), 0, name="tagged_with_tag")
            if llvm_types is not None and len(llvm_types) == 1:
                values = compiled_values if compiled_values is not None else [self.compile_expr(args[0])]
                val = values[0]
                # i32 → f64 자동 승격 (기존 동작 유지)
                if val.type == i32 and payload_ty == f64:
                    val = self.builder.sitofp(val, f64, name="promoted")
                # 타입이 맞으면 그대로 insert. (단일 binding이고 _detect_uniform_payload가 통과시켰으니 일치 보장)
                agg = self.builder.insert_value(agg, val, 1, name="tagged_with_payload")
            elif llvm_types is None or len(llvm_types) == 0:
                # 0-binding variant: payload 필드를 zero로 채움
                agg = self.builder.insert_value(agg, ir.Constant(payload_ty, 0), 1, name="tagged_zero_payload")
            return agg

        # 레거시 경로: { i32, [N x i8] }
        tmp = self.builder.alloca(tagged_ty, name="tagged_tmp")

        # tag 저장
        tag_ptr = self.builder.gep(
            tmp, [ir.Constant(i32, 0), ir.Constant(i32, 0)],
            inbounds=True, name="tag_ptr"
        )
        self.builder.store(ir.Constant(i32, tag), tag_ptr)

        # payload 저장
        if llvm_types is not None and len(llvm_types) > 0:
            payload_ptr = self.builder.gep(
                tmp, [ir.Constant(i32, 0), ir.Constant(i32, 1)],
                inbounds=True, name="payload_ptr"
            )
            payload_i8 = self.builder.bitcast(
                payload_ptr, ir.IntType(8).as_pointer(), name="payload_i8"
            )
            values = compiled_values if compiled_values is not None else [self.compile_expr(a) for a in args]
            offset = 0
            for i, (val, expected_ty) in enumerate(zip(values, llvm_types)):
                if val.type == i32 and expected_ty == f64:
                    val = self.builder.sitofp(val, f64, name="promoted")
                dest = self.builder.gep(
                    payload_i8, [ir.Constant(i32, offset)],
                    inbounds=True, name=f"pay_{i}"
                )
                typed_ptr = self.builder.bitcast(
                    dest, expected_ty.as_pointer(), name=f"pay_{i}_ptr"
                )
                self.builder.store(val, typed_ptr)
                if expected_ty == i32:
                    offset += 4
                elif expected_ty == f64:
                    offset += 8
                elif expected_ty == i8p:
                    offset += 8
                else:
                    offset += 8

        return self.builder.load(tmp, name="tagged_val")
    
    def _instantiate_generic_enum(self, enum_name, variant_name, call_args, line):
        """8.6: 제네릭 enum을 호출 인자 타입에 맞게 단형화.
        Result.Ok(42) → T=i32로 추론, 단형화된 tagged enum 생성.
        """
        gnode = self._generic_enums[enum_name]
        variants = gnode[2]
        enum_tparams = gnode[3]
        
        # variant 찾기
        target_variant = None
        for i, (vname, vtypes) in enumerate(variants):
            if vname == variant_name:
                target_variant = (i, vtypes)
                break
        if target_variant is None:
            raise DanhaNameError(f"enum '{enum_name}'에 '{variant_name}' variant가 없어", line=line, source=self._source_code)
        
        vtag, vtypes = target_variant
        
        # 인자 컴파일해서 타입 매핑 추론
        arg_values = [self.compile_expr(a) for a in call_args]
        
        type_map = {}
        if vtypes is not None:
            if len(call_args) != len(vtypes):
                raise DanhaRuntimeError(
                    f"'{variant_name}'은(는) {len(vtypes)}개의 값이 필요한데 "
                    f"{len(call_args)}개가 들어왔어",
                    line=line, source=self._source_code
                )
            for vt_node, av in zip(vtypes, arg_values):
                if vt_node[0] == 'TypeName' and vt_node[1] in enum_tparams:
                    tp_name = vt_node[1]
                    if tp_name not in type_map:
                        type_map[tp_name] = av.type
        elif len(call_args) > 0:
            raise DanhaRuntimeError(f"'{variant_name}'은(는) 데이터가 없는 variant야", line=line, source=self._source_code)
        
        # 단형화 이름
        suffix = '_'.join(str(type_map.get(tp, 'void')) for tp in enum_tparams)
        mono_name = f"{enum_name}_{suffix}"
        
        # 이미 단형화된 enum이 있으면 재사용
        if mono_name not in self.tagged_enums:
            # 각 variant의 LLVM 타입을 결정
            def resolve_vtype(tnode):
                if tnode[0] == 'TypeName' and tnode[1] in type_map:
                    return type_map[tnode[1]]
                if tnode[0] == 'TypeName' and tnode[1] in enum_tparams:
                    # 추론 안 된 타입 매개변수 — 가장 큰 크기(i64/8바이트)로 폴백
                    return ir.IntType(64)
                return self._resolve_type(tnode, f"enum {enum_name}", line)[0]
            
            variant_info = {}
            max_payload_size = 0
            for i, (vname, vts) in enumerate(variants):
                if vts is not None:
                    llvm_types = [resolve_vtype(t) for t in vts]
                    payload_size = 0
                    for lt in llvm_types:
                        if lt == i32: payload_size += 4
                        elif lt == f64: payload_size += 8
                        else: payload_size += 8
                    variant_info[vname] = (i, llvm_types)
                    if payload_size > max_payload_size:
                        max_payload_size = payload_size
                else:
                    variant_info[vname] = (i, None)
            
            # Stage 75a: 동일 primitive 단일-binding variants는 typed repr {i32, T}.
            uniform_ty = self._detect_uniform_payload(variant_info)
            if uniform_ty is not None:
                tagged_ty = self.module.context.get_identified_type(f"tagged.{mono_name}")
                if not tagged_ty.elements:
                    tagged_ty.set_body(i32, uniform_ty)
            else:
                if max_payload_size == 0:
                    max_payload_size = 1
                payload_array_ty = ir.ArrayType(ir.IntType(8), max_payload_size)
                tagged_ty = self.module.context.get_identified_type(f"tagged.{mono_name}")
                if not tagged_ty.elements:
                    tagged_ty.set_body(i32, payload_array_ty)

            self.tagged_enums[mono_name] = (tagged_ty, variant_info, max_payload_size)
        
        # 이제 단형화된 enum에서 variant 생성
        mono_variant_info = self.tagged_enums[mono_name][1]
        tag, llvm_types = mono_variant_info[variant_name]
        
        if llvm_types is not None and len(llvm_types) > 0:
            return self._build_tagged_enum(mono_name, tag, llvm_types, [], line, compiled_values=arg_values)
        else:
            return self._build_tagged_enum(mono_name, tag, None, [], line)
    
    def _compile_str_concat(self, left, right):
        """8.2: 문자열 concat — 아레나에서 새 버퍼를 할당하고 두 문자열을 복사.
        left, right: i8* LLVM 값
        반환: i8* (새 문자열 포인터)
        """
        i64 = ir.IntType(64)
        # strlen으로 두 문자열 길이 구함
        len_a = self.builder.call(self.strlen_fn, [left], name="len_a")
        len_b = self.builder.call(self.strlen_fn, [right], name="len_b")
        total = self.builder.add(len_a, len_b, name="total_len")
        # +1 for null terminator
        total_plus1 = self.builder.add(total, ir.Constant(i64, 1), name="total_p1")
        # i64 → i32 for arena_alloc
        alloc_size = self.builder.trunc(total_plus1, i32, name="alloc_sz")
        buf = self._arena_alloc(alloc_size, name="concat_buf")
        # memcpy(buf, left, len_a)
        self.builder.call(self.memcpy_fn, [buf, left, len_a])
        # memcpy(buf + len_a, right, len_b + 1)  — +1로 null terminator 포함
        dst = self.builder.gep(buf, [len_a], inbounds=True, name="concat_mid")
        len_b_plus1 = self.builder.add(len_b, ir.Constant(i64, 1), name="len_b_p1")
        self.builder.call(self.memcpy_fn, [dst, right, len_b_plus1])
        return buf

    # ─── StringBuilder (Stage 70): O(N) amortized 누적 문자열 빌드 ───
    # Layout struct { i8* data; i64 len; i64 cap; } — 24 bytes. opaque i8*로 노출.
    # offset 0: data ptr (8B), offset 8: len (8B), offset 16: cap (8B)

    def _sb_data_ptr(self, sb_i8p):
        """sb 의 data 필드 주소(i8**) 반환."""
        dpp = self.builder.bitcast(sb_i8p, i8p.as_pointer(), name="sb_data_pp")
        return dpp

    def _sb_len_ptr(self, sb_i8p):
        """sb 의 len 필드 주소(i64*) 반환."""
        base = self.builder.bitcast(sb_i8p, ir.IntType(64).as_pointer(), name="sb_base64")
        return self.builder.gep(base, [ir.Constant(ir.IntType(64), 1)], inbounds=True, name="sb_len_ptr")

    def _sb_cap_ptr(self, sb_i8p):
        """sb 의 cap 필드 주소(i64*) 반환."""
        base = self.builder.bitcast(sb_i8p, ir.IntType(64).as_pointer(), name="sb_base64")
        return self.builder.gep(base, [ir.Constant(ir.IntType(64), 2)], inbounds=True, name="sb_cap_ptr")

    def _compile_sb_new(self):
        """string_builder() → i8* (StringBuilder 포인터).
        malloc(24) → struct. data = malloc(16), len = 0, cap = 16.
        """
        i64 = ir.IntType(64)
        # 1. struct 자체 할당 (24바이트)
        sb = self.builder.call(self.malloc_fn, [ir.Constant(i64, 24)], name="sb_alloc")
        # 2. 초기 data 버퍼 할당 (16바이트)
        init_cap = ir.Constant(i64, 16)
        data = self.builder.call(self.malloc_fn, [init_cap], name="sb_init_data")
        # 3. 필드 초기화
        self.builder.store(data, self._sb_data_ptr(sb))
        self.builder.store(ir.Constant(i64, 0), self._sb_len_ptr(sb))
        self.builder.store(init_cap, self._sb_cap_ptr(sb))
        return sb

    def _compile_sb_append(self, sb_i8p, s_i8p):
        """string_builder_append(sb, s) — slen 만큼 sb에 추가.
        cap 부족하면 max(need, cap*2) 로 realloc — amortized O(1) per byte.
        """
        i64 = ir.IntType(64)
        # 현재 상태 로드
        cur_len = self.builder.load(self._sb_len_ptr(sb_i8p), name="sb_cur_len")
        cur_cap = self.builder.load(self._sb_cap_ptr(sb_i8p), name="sb_cur_cap")
        # strlen(s)
        slen = self.builder.call(self.strlen_fn, [s_i8p], name="sb_slen")
        need = self.builder.add(cur_len, slen, name="sb_need")

        # if need > cap: realloc
        need_grow = self.builder.icmp_unsigned('>', need, cur_cap, name="sb_need_grow")
        bb_grow = self.current_fn.append_basic_block(name="sb_grow")
        bb_after = self.current_fn.append_basic_block(name="sb_after_grow")
        self.builder.cbranch(need_grow, bb_grow, bb_after)

        # ─ grow 블록 ─
        self.builder.position_at_end(bb_grow)
        cap_x2 = self.builder.shl(cur_cap, ir.Constant(i64, 1), name="sb_cap_x2")
        # new_cap = max(need, cap*2)
        bigger = self.builder.icmp_unsigned('>', need, cap_x2, name="sb_need_gt_x2")
        new_cap = self.builder.select(bigger, need, cap_x2, name="sb_new_cap")
        # realloc(data, new_cap)
        cur_data = self.builder.load(self._sb_data_ptr(sb_i8p), name="sb_data_pre_grow")
        new_data = self.builder.call(self.realloc_fn, [cur_data, new_cap], name="sb_new_data")
        self.builder.store(new_data, self._sb_data_ptr(sb_i8p))
        self.builder.store(new_cap, self._sb_cap_ptr(sb_i8p))
        self.builder.branch(bb_after)

        # ─ after_grow 블록 ─
        self.builder.position_at_end(bb_after)
        # data + cur_len 위치에 s 복사 (null terminator 제외)
        cur_data2 = self.builder.load(self._sb_data_ptr(sb_i8p), name="sb_data_for_copy")
        dst = self.builder.gep(cur_data2, [cur_len], inbounds=True, name="sb_copy_dst")
        self.builder.call(self.memcpy_fn, [dst, s_i8p, slen])
        # len 갱신
        self.builder.store(need, self._sb_len_ptr(sb_i8p))

    def _compile_sb_to_string(self, sb_i8p):
        """string_builder_to_string(sb) → i8* (아레나에 새 null-terminated 문자열)."""
        i64 = ir.IntType(64)
        cur_len = self.builder.load(self._sb_len_ptr(sb_i8p), name="sb_final_len")
        cur_data = self.builder.load(self._sb_data_ptr(sb_i8p), name="sb_final_data")
        # 아레나에서 len+1 바이트 할당 (다른 문자열과 동일한 lifetime 정책)
        len_plus1_64 = self.builder.add(cur_len, ir.Constant(i64, 1), name="sb_str_size")
        len_plus1_32 = self.builder.trunc(len_plus1_64, i32, name="sb_str_size32")
        buf = self._arena_alloc(len_plus1_32, name="sb_str_buf")
        # memcpy(buf, data, len)
        self.builder.call(self.memcpy_fn, [buf, cur_data, cur_len])
        # null terminator
        null_pos = self.builder.gep(buf, [cur_len], inbounds=True, name="sb_str_nullpos")
        self.builder.store(ir.Constant(ir.IntType(8), 0), null_pos)
        return buf

    def _compile_sb_len(self, sb_i8p):
        """string_builder_len(sb) → i32."""
        i64 = ir.IntType(64)
        cur_len = self.builder.load(self._sb_len_ptr(sb_i8p), name="sb_get_len")
        return self.builder.trunc(cur_len, i32, name="sb_len_i32")

    def _ir_value_to_interpolation_str_ptr(self, val, line, uniq_suffix):
        """문자열 보간용: 식 결과(IR)를 null-terminated 문자열 i8*로."""
        if val.type == i8p:
            return val
        if val.type == i1:
            then_b = self.current_fn.append_basic_block(name=f"iip_{uniq_suffix}_t")
            else_b = self.current_fn.append_basic_block(name=f"iip_{uniq_suffix}_f")
            end_b = self.current_fn.append_basic_block(name=f"iip_{uniq_suffix}_e")
            self.builder.cbranch(val, then_b, else_b)
            self.builder.position_at_end(then_b)
            tp = self.builder.bitcast(self.str_true_plain, i8p)
            self.builder.branch(end_b)
            self.builder.position_at_end(else_b)
            fp = self.builder.bitcast(self.str_false_plain, i8p)
            self.builder.branch(end_b)
            self.builder.position_at_end(end_b)
            ph = self.builder.phi(i8p, name=f"iip_{uniq_suffix}_phi")
            ph.add_incoming(tp, then_b)
            ph.add_incoming(fp, else_b)
            return ph
        buf_size = 64
        buf = self._arena_alloc(ir.Constant(i32, buf_size), name=f"iip_{uniq_suffix}_buf")
        if isinstance(val.type, ir.IntType):
            bits = val.type.width
            if bits == 32:
                iv = val
            elif bits < 32:
                iv = self.builder.sext(val, i32, name=f"iip_{uniq_suffix}_sx")
            else:
                iv = self.builder.trunc(val, i32, name=f"iip_{uniq_suffix}_tr")
            fmt = self.builder.bitcast(self.fmt_to_str_int, i8p)
            self.builder.call(self.sprintf_fn, [buf, fmt, iv])
            return buf
        if isinstance(val.type, (ir.DoubleType, ir.FloatType)):
            fv = val if isinstance(val.type, ir.DoubleType) else self.builder.fpext(val, f64, name=f"iip_{uniq_suffix}_fe")
            fmt = self.builder.bitcast(self.fmt_to_str_float, i8p)
            self.builder.call(self.sprintf_fn, [buf, fmt, fv])
            return buf
        raise DanhaRuntimeError(
            f"[{line}번째 줄] 문자열 보간은 숫자·bool·문자열 식만 지원돼",
            line=line, source=self._source_code,
        )
    
    def _compile_to_string(self, args, line):
        """8.2: to_string(value) → i8* — 숫자를 문자열로 변환.
        아레나에서 32바이트 버퍼를 할당하고 snprintf로 포맷.
        """
        if len(args) != 1:
            raise DanhaValueError("to_string은 1개의 인자가 필요해", line=line, source=self._source_code)
        val = self.compile_expr(args[0])
        buf_size = 32
        buf = self._arena_alloc(ir.Constant(i32, buf_size), name="tostr_buf")
        if self.runtime_mode == 'direct-os' and (val.type == i32 or val.type == i64):
            return self._direct_os_to_string_i64(val, buf)
        if val.type == i32:
            fmt = self.builder.bitcast(self.fmt_to_str_int, i8p)
            self.builder.call(self.sprintf_fn, [buf, fmt, val])
        elif val.type == i64:
            fmt = self.builder.bitcast(self.fmt_to_str_i64, i8p)
            self.builder.call(self.sprintf_fn, [buf, fmt, val])
        elif val.type == f64:
            fmt = self.builder.bitcast(self.fmt_to_str_float, i8p)
            self.builder.call(self.sprintf_fn, [buf, fmt, val])
        elif val.type == i8p:
            # 문자열이면 그대로 반환
            return val
        else:
            raise DanhaRuntimeError("to_string은 숫자나 문자열에만 쓸 수 있어", line=line, source=self._source_code)
        return buf
    
    def _compile_strlen(self, args, line):
        """8.2: len(str) → i32 — 문자열 길이 반환."""
        if len(args) != 1:
            raise DanhaValueError("len은 1개의 인자가 필요해", line=line, source=self._source_code)
        val = self.compile_expr(args[0])
        if val.type != i8p:
            raise DanhaRuntimeError("len에 문자열이 아닌 값이 들어왔어", line=line, source=self._source_code)
        result_i64 = self.builder.call(self.strlen_fn, [val], name="str_len")
        return self.builder.trunc(result_i64, i32, name="str_len_i32")
    
    def _compile_dynarray_push(self, var_name, slot, value, line):
        """동적 배열에 원소 하나를 추가한다.
        
        7.5: 아레나 기반 할당으로 동작.
        
        알고리즘:
        1. len과 cap을 읽는다
        2. len == cap이면 → 아레나에서 새 공간 할당 + memcpy로 기존 데이터 복사
        3. data[len] = value
        4. len += 1
        
        용량 확장 전략: cap이 0이면 4로, 아니면 2배.
        비유: 두루마리 종이에서 더 넓은 자리를 잘라 쓰고, 기존 물건을 옮김.
        
        realloc과의 차이: 옛 공간을 free하지 않음 — 아레나가 통째로 관리.
        이게 아레나의 핵심 장점: 개별 free가 불필요.
        """
        i64 = ir.IntType(64)

        elem_ty = self._dynarray_elem_ty(slot)
        # P1A-fix: struct/vector/array element size를 _sizeof_bytes로 계산.
        # 이전엔 'hasattr(width) else 8' → struct가 항상 8로 잘못 계산되어
        # 32B struct를 8B씩만 다뤄 grow 시 데이터 손상.
        elem_size = self._sizeof_bytes(elem_ty)
        
        len_ptr = self._dynarray_get_field(slot, 1, f"{var_name}_len_ptr")
        cap_ptr = self._dynarray_get_field(slot, 2, f"{var_name}_cap_ptr")
        data_ptr = self._dynarray_get_field(slot, 0, f"{var_name}_data_ptr")
        
        cur_len = self.builder.load(len_ptr, name="cur_len")
        cur_cap = self.builder.load(cap_ptr, name="cur_cap")
        cur_data = self.builder.load(data_ptr, name="cur_data")
        
        # len == cap ? → 확장 필요
        need_grow = self.builder.icmp_signed('==', cur_len, cur_cap, name="need_grow")
        
        grow_block = self.current_fn.append_basic_block(name="push_grow")
        store_block = self.current_fn.append_basic_block(name="push_store")
        
        self.builder.cbranch(need_grow, grow_block, store_block)
        
        # --- grow 블록: 아레나에서 새 공간 할당 ---
        self.builder.position_at_end(grow_block)
        # new_cap = (cap == 0) ? 4 : cap * 2
        is_zero = self.builder.icmp_signed('==', cur_cap, ir.Constant(i32, 0), name="cap_zero")
        new_cap = self.builder.select(
            is_zero,
            ir.Constant(i32, 4),
            self.builder.mul(cur_cap, ir.Constant(i32, 2), name="cap_x2"),
            name="new_cap"
        )
        
        # 아레나에서 new_cap * elem_size 바이트 할당
        alloc_bytes = self.builder.mul(new_cap, ir.Constant(i32, elem_size), name="alloc_bytes")
        new_data_i8 = self._arena_alloc(alloc_bytes, name="new_data_i8")
        new_data = self.builder.bitcast(new_data_i8, elem_ty.as_pointer(), name="new_data")
        
        # 기존 데이터가 있으면 memcpy로 복사 (cap > 0일 때만)
        has_old = self.builder.icmp_signed('>', cur_cap, ir.Constant(i32, 0), name="has_old")
        copy_block = self.current_fn.append_basic_block(name="push_copy")
        done_grow = self.current_fn.append_basic_block(name="push_done_grow")
        
        self.builder.cbranch(has_old, copy_block, done_grow)
        
        # copy 블록: memcpy(new, old, len * elem_size)
        self.builder.position_at_end(copy_block)
        copy_bytes = self.builder.mul(cur_len, ir.Constant(i32, elem_size), name="copy_bytes")
        copy_bytes_i64 = self.builder.sext(copy_bytes, i64, name="copy_i64")
        dst_i8 = self.builder.bitcast(new_data, i8p, name="dst_i8")
        src_i8 = self.builder.bitcast(cur_data, i8p, name="src_i8")
        self.builder.call(self.memcpy_fn, [dst_i8, src_i8, copy_bytes_i64])
        self.builder.branch(done_grow)
        
        # done_grow: 구조체 갱신
        self.builder.position_at_end(done_grow)
        self.builder.store(new_data, data_ptr)
        self.builder.store(new_cap, cap_ptr)
        # 옛 data를 free하지 않음! 아레나가 통째로 관리.
        self.builder.branch(store_block)
        
        # --- store 블록: data[len] = value, len++ ---
        self.builder.position_at_end(store_block)
        final_data = self.builder.load(data_ptr, name="final_data")
        final_len = self.builder.load(len_ptr, name="final_len")
        
        elem_ptr = self.builder.gep(
            final_data, [final_len],
            inbounds=True, name="push_elem"
        )
        self.builder.store(value, elem_ptr)
        
        new_len = self.builder.add(final_len, ir.Constant(i32, 1), name="new_len")
        self.builder.store(new_len, len_ptr)
    
    # ----- 31: 동적 배열 내장 메서드 컴파일 -----
    
    _DYNARRAY_METHODS = {
        'map', 'filter', 'reduce', 'any', 'all', 'find', 'count',
        'sort_by', 'reverse', 'take', 'skip', 'for_each', 'contains', 'len', 'push',
    }
    
    def _fixed_to_dynarray(self, slot, line):
        """고정 배열 슬롯을 동적 배열 슬롯으로 변환한다.
        [N x T]* → { T*, i32, i32 }* (data, len, cap)
        원본 데이터를 아레나에 복사."""
        inner = slot.type.pointee
        elem_ty = inner.element
        arr_len = inner.count
        
        darr_ty = self._get_dynarray_type(elem_ty)
        result_slot = self.builder.alloca(darr_ty, name="fixed2dyn_slot")
        
        elem_size = self._sizeof_bytes(elem_ty)
        alloc_bytes = ir.Constant(i32, arr_len * elem_size)
        new_data_i8 = self._arena_alloc(alloc_bytes, name="fixed2dyn_data_i8")
        new_data = self.builder.bitcast(new_data_i8, elem_ty.as_pointer(), name="fixed2dyn_data")
        
        # 원본 데이터 복사
        src_i8 = self.builder.bitcast(
            self.builder.gep(slot, [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True),
            i8p, name="src_i8"
        )
        i64 = ir.IntType(64)
        copy_bytes = ir.Constant(i64, arr_len * elem_size)
        self.builder.call(self.memcpy_fn, [new_data_i8, src_i8, copy_bytes])
        
        # 구조체 초기화
        dp = self._dynarray_get_field(result_slot, 0, "f2d_dp")
        lp = self._dynarray_get_field(result_slot, 1, "f2d_lp")
        cp = self._dynarray_get_field(result_slot, 2, "f2d_cp")
        self.builder.store(new_data, dp)
        self.builder.store(ir.Constant(i32, arr_len), lp)
        self.builder.store(ir.Constant(i32, arr_len), cp)
        return result_slot
    
    def _is_fixed_array_slot(self, slot):
        """이 슬롯이 고정 배열인지 확인한다."""
        inner = slot.type.pointee
        return isinstance(inner, ir.ArrayType)
    
    def _compile_dynarray_method(self, slot, var_name, method_name, args, line):
        """동적 배열의 내장 메서드를 컴파일한다.
        slot: 동적 배열 변수의 alloca 슬롯 ({T*, i32, i32}*)
        """
        elem_ty = self._dynarray_elem_ty(slot)
        
        if method_name == 'len':
            len_ptr = self._dynarray_get_field(slot, 1, "arr_len_ptr")
            return self.builder.load(len_ptr, name="arr_len")
        
        if method_name == 'push':
            if len(args) != 1:
                raise DanhaValueError("push(value)에는 인자 1개가 필요해", line=line, source=self._source_code)
            value = self.compile_expr(args[0])
            if value.type != elem_ty:
                value = self._implicit_cast(value, elem_ty, line)
            self._compile_dynarray_push(var_name, slot, value, line)
            return ir.Constant(i32, 0)
        
        if method_name == 'contains':
            if len(args) != 1:
                raise DanhaValueError("contains(value)에는 인자 1개가 필요해", line=line, source=self._source_code)
            target = self.compile_expr(args[0])
            if target.type != elem_ty:
                target = self._implicit_cast(target, elem_ty, line)
            return self._dynarr_contains(slot, target, elem_ty, line)
        
        if method_name == 'reverse':
            if len(args) != 0:
                raise DanhaValueError("reverse()에는 인자가 없어야 해", line=line, source=self._source_code)
            return self._dynarr_reverse(slot, elem_ty, line)
        
        if method_name == 'take':
            if len(args) != 1:
                raise DanhaValueError("take(n)에는 정수 인자 1개가 필요해", line=line, source=self._source_code)
            n = self.compile_expr(args[0])
            return self._dynarr_take_skip(slot, elem_ty, n, is_take=True, line=line)
        
        if method_name == 'skip':
            if len(args) != 1:
                raise DanhaValueError("skip(n)에는 정수 인자 1개가 필요해", line=line, source=self._source_code)
            n = self.compile_expr(args[0])
            return self._dynarr_take_skip(slot, elem_ty, n, is_take=False, line=line)
        
        # 콜백 필요한 메서드들
        if method_name in ('map', 'filter', 'reduce', 'any', 'all', 'find', 'count', 'for_each', 'sort_by'):
            if method_name == 'reduce':
                if len(args) != 2:
                    raise DanhaValueError("reduce(init, fn)에는 인자 2개가 필요해", line=line, source=self._source_code)
                init_val = self.compile_expr(args[0])
                cb_fn = self.compile_expr(args[1])
            elif method_name == 'sort_by':
                if len(args) != 1:
                    raise DanhaValueError("sort_by(fn)에는 함수 인자 1개가 필요해", line=line, source=self._source_code)
                cb_fn = self.compile_expr(args[0])
                return self._dynarr_sort_by(slot, elem_ty, cb_fn, line)
            else:
                if len(args) != 1:
                    raise DanhaValueError(f"{method_name}(fn)에는 함수 인자 1개가 필요해", line=line, source=self._source_code)
                cb_fn = self.compile_expr(args[0])
                init_val = None
            
            if method_name == 'map':
                ret_elem_ty = cb_fn.function_type.return_type
                return self._dynarr_map(slot, elem_ty, cb_fn, ret_elem_ty, line)
            if method_name == 'filter':
                return self._dynarr_filter(slot, elem_ty, cb_fn, line)
            if method_name == 'reduce':
                return self._dynarr_reduce(slot, elem_ty, cb_fn, init_val, line)
            if method_name == 'any':
                return self._dynarr_any_all(slot, elem_ty, cb_fn, is_any=True, line=line)
            if method_name == 'all':
                return self._dynarr_any_all(slot, elem_ty, cb_fn, is_any=False, line=line)
            if method_name == 'find':
                return self._dynarr_find(slot, elem_ty, cb_fn, line)
            if method_name == 'count':
                return self._dynarr_count(slot, elem_ty, cb_fn, line)
            if method_name == 'for_each':
                return self._dynarr_for_each(slot, elem_ty, cb_fn, line)
        
        raise DanhaNameError(f"배열에 '{method_name}'이라는 메서드가 없어", line=line, source=self._source_code)
    
    def _dynarr_load_len_data(self, slot):
        """동적 배열에서 len, data 포인터를 로드한다."""
        len_ptr = self._dynarray_get_field(slot, 1, "src_len_ptr")
        data_ptr_ptr = self._dynarray_get_field(slot, 0, "src_data_ptr")
        src_len = self.builder.load(len_ptr, name="src_len")
        src_data = self.builder.load(data_ptr_ptr, name="src_data")
        return src_len, src_data
    
    def _dynarr_new_result(self, elem_ty, cap_val, name="result"):
        """새 동적 배열 슬롯을 만들고 아레나에서 공간을 할당한다."""
        darr_ty = self._get_dynarray_type(elem_ty)
        result_slot = self.builder.alloca(darr_ty, name=f"{name}_slot")
        elem_size = self._sizeof_bytes(elem_ty)
        alloc_bytes = self.builder.mul(cap_val, ir.Constant(i32, elem_size), name="alloc_bytes")
        new_data_i8 = self._arena_alloc(alloc_bytes, name=f"{name}_data_i8")
        new_data = self.builder.bitcast(new_data_i8, elem_ty.as_pointer(), name=f"{name}_data")
        # 초기화: data, len=0, cap
        dp = self._dynarray_get_field(result_slot, 0, f"{name}_dp")
        lp = self._dynarray_get_field(result_slot, 1, f"{name}_lp")
        cp = self._dynarray_get_field(result_slot, 2, f"{name}_cp")
        self.builder.store(new_data, dp)
        self.builder.store(ir.Constant(i32, 0), lp)
        self.builder.store(cap_val, cp)
        return result_slot
    
    def _dynarr_append_elem(self, result_slot, elem_val, var_name="res"):
        """결과 동적 배열에 원소를 추가한다. 용량 초과 시 확장."""
        self._compile_dynarray_push(var_name, result_slot, elem_val, 0)
    
    def _closure_call(self, cb_fn, args, name=""):
        """콜백 함수를 호출한다. 클로저면 캡처 변수를 추가 인자로 전달."""
        lambda_name = cb_fn.name if hasattr(cb_fn, 'name') else None
        if lambda_name and lambda_name in self._lambda_captures:
            capture_vals = [v for _, v in self._lambda_captures[lambda_name]]
            return self.builder.call(cb_fn, args + capture_vals, name=name)
        return self.builder.call(cb_fn, args, name=name)
    
    def _dynarr_map(self, slot, elem_ty, cb_fn, ret_elem_ty, line):
        """arr.map(fn) → 새 동적 배열"""
        src_len, src_data = self._dynarr_load_len_data(slot)
        result_slot = self._dynarr_new_result(ret_elem_ty, src_len, "map_res")
        
        # 루프: i = 0; while i < len { result.push(cb(src[i])); i++ }
        idx = self.builder.alloca(i32, name="map_i")
        self.builder.store(ir.Constant(i32, 0), idx)
        
        cond_bb = self.current_fn.append_basic_block("map_cond")
        body_bb = self.current_fn.append_basic_block("map_body")
        end_bb = self.current_fn.append_basic_block("map_end")
        
        self.builder.branch(cond_bb)
        self.builder = ir.IRBuilder(cond_bb)
        i = self.builder.load(idx, "i")
        cmp = self.builder.icmp_signed('<', i, src_len, "map_cmp")
        self.builder.cbranch(cmp, body_bb, end_bb)
        
        self.builder = ir.IRBuilder(body_bb)
        i = self.builder.load(idx, "i")
        elem_ptr = self.builder.gep(src_data, [i], inbounds=True, name="map_elem")
        elem = self.builder.load(elem_ptr, name="map_val")
        result_val = self._closure_call(cb_fn, [elem], name="map_cb")
        self._dynarr_append_elem(result_slot, result_val, "map_res")
        self.builder.store(self.builder.add(i, ir.Constant(i32, 1)), idx)
        self.builder.branch(cond_bb)
        
        self.builder = ir.IRBuilder(end_bb)
        return result_slot
    
    def _dynarr_filter(self, slot, elem_ty, cb_fn, line):
        """arr.filter(fn) → 새 동적 배열"""
        src_len, src_data = self._dynarr_load_len_data(slot)
        result_slot = self._dynarr_new_result(elem_ty, src_len, "filt_res")
        
        idx = self.builder.alloca(i32, name="filt_i")
        self.builder.store(ir.Constant(i32, 0), idx)
        
        cond_bb = self.current_fn.append_basic_block("filt_cond")
        body_bb = self.current_fn.append_basic_block("filt_body")
        push_bb = self.current_fn.append_basic_block("filt_push")
        inc_bb = self.current_fn.append_basic_block("filt_inc")
        end_bb = self.current_fn.append_basic_block("filt_end")
        
        self.builder.branch(cond_bb)
        self.builder = ir.IRBuilder(cond_bb)
        i = self.builder.load(idx, "i")
        cmp = self.builder.icmp_signed('<', i, src_len, "filt_cmp")
        self.builder.cbranch(cmp, body_bb, end_bb)
        
        self.builder = ir.IRBuilder(body_bb)
        i = self.builder.load(idx, "i")
        elem_ptr = self.builder.gep(src_data, [i], inbounds=True, name="filt_elem")
        elem = self.builder.load(elem_ptr, name="filt_val")
        cond_val = self._closure_call(cb_fn, [elem], name="filt_cb")
        # bool result: i1 또는 i32 → trunc/compare
        if cond_val.type == ir.IntType(1):
            pred = cond_val
        else:
            pred = self.builder.icmp_signed('!=', cond_val, ir.Constant(cond_val.type, 0), name="filt_pred")
        self.builder.cbranch(pred, push_bb, inc_bb)
        
        self.builder = ir.IRBuilder(push_bb)
        self._dynarr_append_elem(result_slot, elem, "filt_res")
        self.builder.branch(inc_bb)
        
        self.builder = ir.IRBuilder(inc_bb)
        i = self.builder.load(idx, "i")
        self.builder.store(self.builder.add(i, ir.Constant(i32, 1)), idx)
        self.builder.branch(cond_bb)
        
        self.builder = ir.IRBuilder(end_bb)
        return result_slot
    
    def _dynarr_reduce(self, slot, elem_ty, cb_fn, init_val, line):
        """arr.reduce(init, fn) → 스칼라 값"""
        src_len, src_data = self._dynarr_load_len_data(slot)
        
        acc_slot = self.builder.alloca(init_val.type, name="reduce_acc")
        self.builder.store(init_val, acc_slot)
        idx = self.builder.alloca(i32, name="reduce_i")
        self.builder.store(ir.Constant(i32, 0), idx)
        
        cond_bb = self.current_fn.append_basic_block("reduce_cond")
        body_bb = self.current_fn.append_basic_block("reduce_body")
        end_bb = self.current_fn.append_basic_block("reduce_end")
        
        self.builder.branch(cond_bb)
        self.builder = ir.IRBuilder(cond_bb)
        i = self.builder.load(idx, "i")
        cmp = self.builder.icmp_signed('<', i, src_len, "reduce_cmp")
        self.builder.cbranch(cmp, body_bb, end_bb)
        
        self.builder = ir.IRBuilder(body_bb)
        i = self.builder.load(idx, "i")
        acc = self.builder.load(acc_slot, "acc")
        elem_ptr = self.builder.gep(src_data, [i], inbounds=True, name="reduce_ep")
        elem = self.builder.load(elem_ptr, name="reduce_val")
        new_acc = self._closure_call(cb_fn, [acc, elem], name="reduce_cb")
        self.builder.store(new_acc, acc_slot)
        self.builder.store(self.builder.add(i, ir.Constant(i32, 1)), idx)
        self.builder.branch(cond_bb)
        
        self.builder = ir.IRBuilder(end_bb)
        return self.builder.load(acc_slot, "reduce_result")
    
    def _dynarr_any_all(self, slot, elem_ty, cb_fn, is_any, line):
        """arr.any(fn) / arr.all(fn) → bool (i1)"""
        src_len, src_data = self._dynarr_load_len_data(slot)
        
        # any: 기본 false, 하나라도 true면 true 반환
        # all: 기본 true, 하나라도 false면 false 반환
        result_slot = self.builder.alloca(ir.IntType(1), name="aa_result")
        self.builder.store(ir.Constant(ir.IntType(1), 0 if is_any else 1), result_slot)
        idx = self.builder.alloca(i32, name="aa_i")
        self.builder.store(ir.Constant(i32, 0), idx)
        
        cond_bb = self.current_fn.append_basic_block("aa_cond")
        body_bb = self.current_fn.append_basic_block("aa_body")
        early_bb = self.current_fn.append_basic_block("aa_early")
        inc_bb = self.current_fn.append_basic_block("aa_inc")
        end_bb = self.current_fn.append_basic_block("aa_end")
        
        self.builder.branch(cond_bb)
        self.builder = ir.IRBuilder(cond_bb)
        i = self.builder.load(idx, "i")
        cmp = self.builder.icmp_signed('<', i, src_len, "aa_cmp")
        self.builder.cbranch(cmp, body_bb, end_bb)
        
        self.builder = ir.IRBuilder(body_bb)
        i = self.builder.load(idx, "i")
        elem_ptr = self.builder.gep(src_data, [i], inbounds=True, name="aa_ep")
        elem = self.builder.load(elem_ptr, name="aa_val")
        cond_val = self._closure_call(cb_fn, [elem], name="aa_cb")
        if cond_val.type != ir.IntType(1):
            pred = self.builder.icmp_signed('!=', cond_val, ir.Constant(cond_val.type, 0), name="aa_pred")
        else:
            pred = cond_val
        if is_any:
            self.builder.cbranch(pred, early_bb, inc_bb)
        else:
            not_pred = self.builder.not_(pred, name="aa_not")
            self.builder.cbranch(not_pred, early_bb, inc_bb)
        
        self.builder = ir.IRBuilder(early_bb)
        self.builder.store(ir.Constant(ir.IntType(1), 1 if is_any else 0), result_slot)
        self.builder.branch(end_bb)
        
        self.builder = ir.IRBuilder(inc_bb)
        i = self.builder.load(idx, "i")
        self.builder.store(self.builder.add(i, ir.Constant(i32, 1)), idx)
        self.builder.branch(cond_bb)
        
        self.builder = ir.IRBuilder(end_bb)
        return self.builder.load(result_slot, "aa_final")
    
    def _dynarr_find(self, slot, elem_ty, cb_fn, line):
        """arr.find(fn) → 첫 매칭 원소 또는 0/0.0"""
        src_len, src_data = self._dynarr_load_len_data(slot)
        
        # 결과: 찾으면 원소, 못 찾으면 기본값 (인터프리터에서는 null이지만 컴파일러에서는 0)
        result_slot = self.builder.alloca(elem_ty, name="find_result")
        if isinstance(elem_ty, ir.IntType):
            self.builder.store(ir.Constant(elem_ty, 0), result_slot)
        else:
            self.builder.store(ir.Constant(elem_ty, 0.0), result_slot)
        idx = self.builder.alloca(i32, name="find_i")
        self.builder.store(ir.Constant(i32, 0), idx)
        
        cond_bb = self.current_fn.append_basic_block("find_cond")
        body_bb = self.current_fn.append_basic_block("find_body")
        found_bb = self.current_fn.append_basic_block("find_found")
        inc_bb = self.current_fn.append_basic_block("find_inc")
        end_bb = self.current_fn.append_basic_block("find_end")
        
        self.builder.branch(cond_bb)
        self.builder = ir.IRBuilder(cond_bb)
        i = self.builder.load(idx, "i")
        cmp = self.builder.icmp_signed('<', i, src_len)
        self.builder.cbranch(cmp, body_bb, end_bb)
        
        self.builder = ir.IRBuilder(body_bb)
        i = self.builder.load(idx, "i")
        elem_ptr = self.builder.gep(src_data, [i], inbounds=True)
        elem = self.builder.load(elem_ptr)
        cond_val = self._closure_call(cb_fn, [elem])
        if cond_val.type != ir.IntType(1):
            pred = self.builder.icmp_signed('!=', cond_val, ir.Constant(cond_val.type, 0))
        else:
            pred = cond_val
        self.builder.cbranch(pred, found_bb, inc_bb)
        
        self.builder = ir.IRBuilder(found_bb)
        self.builder.store(elem, result_slot)
        self.builder.branch(end_bb)
        
        self.builder = ir.IRBuilder(inc_bb)
        i = self.builder.load(idx, "i")
        self.builder.store(self.builder.add(i, ir.Constant(i32, 1)), idx)
        self.builder.branch(cond_bb)
        
        self.builder = ir.IRBuilder(end_bb)
        return self.builder.load(result_slot, "find_val")
    
    def _dynarr_count(self, slot, elem_ty, cb_fn, line):
        """arr.count(fn) → i32"""
        src_len, src_data = self._dynarr_load_len_data(slot)
        
        cnt = self.builder.alloca(i32, name="cnt")
        self.builder.store(ir.Constant(i32, 0), cnt)
        idx = self.builder.alloca(i32, name="cnt_i")
        self.builder.store(ir.Constant(i32, 0), idx)
        
        cond_bb = self.current_fn.append_basic_block("cnt_cond")
        body_bb = self.current_fn.append_basic_block("cnt_body")
        inc_cnt_bb = self.current_fn.append_basic_block("cnt_inc_cnt")
        inc_bb = self.current_fn.append_basic_block("cnt_inc")
        end_bb = self.current_fn.append_basic_block("cnt_end")
        
        self.builder.branch(cond_bb)
        self.builder = ir.IRBuilder(cond_bb)
        i = self.builder.load(idx, "i")
        cmp = self.builder.icmp_signed('<', i, src_len)
        self.builder.cbranch(cmp, body_bb, end_bb)
        
        self.builder = ir.IRBuilder(body_bb)
        i = self.builder.load(idx, "i")
        elem_ptr = self.builder.gep(src_data, [i], inbounds=True)
        elem = self.builder.load(elem_ptr)
        cond_val = self._closure_call(cb_fn, [elem])
        if cond_val.type != ir.IntType(1):
            pred = self.builder.icmp_signed('!=', cond_val, ir.Constant(cond_val.type, 0))
        else:
            pred = cond_val
        self.builder.cbranch(pred, inc_cnt_bb, inc_bb)
        
        self.builder = ir.IRBuilder(inc_cnt_bb)
        c = self.builder.load(cnt)
        self.builder.store(self.builder.add(c, ir.Constant(i32, 1)), cnt)
        self.builder.branch(inc_bb)
        
        self.builder = ir.IRBuilder(inc_bb)
        i = self.builder.load(idx, "i")
        self.builder.store(self.builder.add(i, ir.Constant(i32, 1)), idx)
        self.builder.branch(cond_bb)
        
        self.builder = ir.IRBuilder(end_bb)
        return self.builder.load(cnt, "cnt_val")
    
    def _dynarr_for_each(self, slot, elem_ty, cb_fn, line):
        """arr.for_each(fn) → void"""
        src_len, src_data = self._dynarr_load_len_data(slot)
        
        idx = self.builder.alloca(i32, name="fe_i")
        self.builder.store(ir.Constant(i32, 0), idx)
        
        cond_bb = self.current_fn.append_basic_block("fe_cond")
        body_bb = self.current_fn.append_basic_block("fe_body")
        end_bb = self.current_fn.append_basic_block("fe_end")
        
        self.builder.branch(cond_bb)
        self.builder = ir.IRBuilder(cond_bb)
        i = self.builder.load(idx, "i")
        cmp = self.builder.icmp_signed('<', i, src_len)
        self.builder.cbranch(cmp, body_bb, end_bb)
        
        self.builder = ir.IRBuilder(body_bb)
        i = self.builder.load(idx, "i")
        elem_ptr = self.builder.gep(src_data, [i], inbounds=True)
        elem = self.builder.load(elem_ptr)
        self._closure_call(cb_fn, [elem])
        self.builder.store(self.builder.add(i, ir.Constant(i32, 1)), idx)
        self.builder.branch(cond_bb)
        
        self.builder = ir.IRBuilder(end_bb)
        return ir.Constant(i32, 0)
    
    def _dynarr_contains(self, slot, target, elem_ty, line):
        """arr.contains(val) → bool (i1)"""
        src_len, src_data = self._dynarr_load_len_data(slot)
        
        result = self.builder.alloca(ir.IntType(1), name="cont_res")
        self.builder.store(ir.Constant(ir.IntType(1), 0), result)
        idx = self.builder.alloca(i32, name="cont_i")
        self.builder.store(ir.Constant(i32, 0), idx)
        
        cond_bb = self.current_fn.append_basic_block("cont_cond")
        body_bb = self.current_fn.append_basic_block("cont_body")
        found_bb = self.current_fn.append_basic_block("cont_found")
        inc_bb = self.current_fn.append_basic_block("cont_inc")
        end_bb = self.current_fn.append_basic_block("cont_end")
        
        self.builder.branch(cond_bb)
        self.builder = ir.IRBuilder(cond_bb)
        i = self.builder.load(idx, "i")
        cmp = self.builder.icmp_signed('<', i, src_len)
        self.builder.cbranch(cmp, body_bb, end_bb)
        
        self.builder = ir.IRBuilder(body_bb)
        i = self.builder.load(idx, "i")
        elem_ptr = self.builder.gep(src_data, [i], inbounds=True)
        elem = self.builder.load(elem_ptr)
        if isinstance(elem_ty, ir.IntType):
            eq = self.builder.icmp_signed('==', elem, target)
        else:
            eq = self.builder.fcmp_ordered('==', elem, target)
        self.builder.cbranch(eq, found_bb, inc_bb)
        
        self.builder = ir.IRBuilder(found_bb)
        self.builder.store(ir.Constant(ir.IntType(1), 1), result)
        self.builder.branch(end_bb)
        
        self.builder = ir.IRBuilder(inc_bb)
        i = self.builder.load(idx, "i")
        self.builder.store(self.builder.add(i, ir.Constant(i32, 1)), idx)
        self.builder.branch(cond_bb)
        
        self.builder = ir.IRBuilder(end_bb)
        return self.builder.load(result, "cont_val")
    
    def _dynarr_reverse(self, slot, elem_ty, line):
        """arr.reverse() → 새 동적 배열 (역순)"""
        src_len, src_data = self._dynarr_load_len_data(slot)
        result_slot = self._dynarr_new_result(elem_ty, src_len, "rev_res")
        
        # i = len-1 부터 0까지 역순으로 push
        idx = self.builder.alloca(i32, name="rev_i")
        start = self.builder.sub(src_len, ir.Constant(i32, 1), name="rev_start")
        self.builder.store(start, idx)
        
        cond_bb = self.current_fn.append_basic_block("rev_cond")
        body_bb = self.current_fn.append_basic_block("rev_body")
        end_bb = self.current_fn.append_basic_block("rev_end")
        
        self.builder.branch(cond_bb)
        self.builder = ir.IRBuilder(cond_bb)
        i = self.builder.load(idx, "i")
        cmp = self.builder.icmp_signed('>=', i, ir.Constant(i32, 0))
        self.builder.cbranch(cmp, body_bb, end_bb)
        
        self.builder = ir.IRBuilder(body_bb)
        i = self.builder.load(idx, "i")
        elem_ptr = self.builder.gep(src_data, [i], inbounds=True)
        elem = self.builder.load(elem_ptr)
        self._dynarr_append_elem(result_slot, elem, "rev_res")
        self.builder.store(self.builder.sub(i, ir.Constant(i32, 1)), idx)
        self.builder.branch(cond_bb)
        
        self.builder = ir.IRBuilder(end_bb)
        return result_slot
    
    def _dynarr_take_skip(self, slot, elem_ty, n, is_take, line):
        """arr.take(n) / arr.skip(n) → 새 동적 배열"""
        src_len, src_data = self._dynarr_load_len_data(slot)
        result_slot = self._dynarr_new_result(elem_ty, src_len, "ts_res")
        
        idx = self.builder.alloca(i32, name="ts_i")
        if is_take:
            # 0..min(n, len)
            self.builder.store(ir.Constant(i32, 0), idx)
            use_n = self.builder.select(
                self.builder.icmp_signed('<', n, src_len),
                n, src_len, name="ts_end"
            )
            start = ir.Constant(i32, 0)
            end = use_n
        else:
            # n..len
            self.builder.store(n, idx)
            start = n
            end = src_len
        
        self.builder.store(start, idx)
        cond_bb = self.current_fn.append_basic_block("ts_cond")
        body_bb = self.current_fn.append_basic_block("ts_body")
        end_bb = self.current_fn.append_basic_block("ts_end")
        
        self.builder.branch(cond_bb)
        self.builder = ir.IRBuilder(cond_bb)
        i = self.builder.load(idx, "i")
        cmp = self.builder.icmp_signed('<', i, end)
        self.builder.cbranch(cmp, body_bb, end_bb)
        
        self.builder = ir.IRBuilder(body_bb)
        i = self.builder.load(idx, "i")
        elem_ptr = self.builder.gep(src_data, [i], inbounds=True)
        elem = self.builder.load(elem_ptr)
        self._dynarr_append_elem(result_slot, elem, "ts_res")
        self.builder.store(self.builder.add(i, ir.Constant(i32, 1)), idx)
        self.builder.branch(cond_bb)
        
        self.builder = ir.IRBuilder(end_bb)
        return result_slot
    
    def _dynarr_sort_by(self, slot, elem_ty, cb_fn, line):
        """arr.sort_by(fn) → 새 동적 배열 (삽입 정렬)
        컴파일 타임에 간단한 삽입 정렬을 인라인으로 생성.
        O(n²)이지만 작은 배열에서는 충분하고, 구현이 간단."""
        src_len, src_data = self._dynarr_load_len_data(slot)
        
        # 결과 배열에 원본을 복사
        result_slot = self._dynarr_new_result(elem_ty, src_len, "sort_res")
        # 먼저 모든 원소를 결과 배열에 복사
        ci = self.builder.alloca(i32, name="copy_i")
        self.builder.store(ir.Constant(i32, 0), ci)
        ccond = self.current_fn.append_basic_block("sort_copy_cond")
        cbody = self.current_fn.append_basic_block("sort_copy_body")
        cdone = self.current_fn.append_basic_block("sort_copy_done")
        
        self.builder.branch(ccond)
        self.builder = ir.IRBuilder(ccond)
        cv = self.builder.load(ci)
        self.builder.cbranch(self.builder.icmp_signed('<', cv, src_len), cbody, cdone)
        
        self.builder = ir.IRBuilder(cbody)
        cv = self.builder.load(ci)
        ep = self.builder.gep(src_data, [cv], inbounds=True)
        ev = self.builder.load(ep)
        self._dynarr_append_elem(result_slot, ev, "sort_res")
        self.builder.store(self.builder.add(cv, ir.Constant(i32, 1)), ci)
        self.builder.branch(ccond)
        
        self.builder = ir.IRBuilder(cdone)
        
        # 삽입 정렬: for i in 1..len { key=arr[i]; j=i-1; while j>=0 && cmp(arr[j],key)>0 { arr[j+1]=arr[j]; j-- }; arr[j+1]=key }
        res_data_pp = self._dynarray_get_field(result_slot, 0, "sort_data_pp")
        
        oi = self.builder.alloca(i32, name="sort_i")
        self.builder.store(ir.Constant(i32, 1), oi)
        
        ocond = self.current_fn.append_basic_block("sort_ocond")
        obody = self.current_fn.append_basic_block("sort_obody")
        icond = self.current_fn.append_basic_block("sort_icond")
        ibody = self.current_fn.append_basic_block("sort_ibody")
        iend = self.current_fn.append_basic_block("sort_iend")
        oend = self.current_fn.append_basic_block("sort_oend")
        
        self.builder.branch(ocond)
        self.builder = ir.IRBuilder(ocond)
        iv = self.builder.load(oi)
        self.builder.cbranch(self.builder.icmp_signed('<', iv, src_len), obody, oend)
        
        self.builder = ir.IRBuilder(obody)
        res_data = self.builder.load(res_data_pp, "sort_data")
        iv = self.builder.load(oi)
        key_ptr = self.builder.gep(res_data, [iv], inbounds=True)
        key = self.builder.load(key_ptr, "key")
        j_slot = self.builder.alloca(i32, name="sort_j")
        self.builder.store(self.builder.sub(iv, ir.Constant(i32, 1)), j_slot)
        self.builder.branch(icond)
        
        self.builder = ir.IRBuilder(icond)
        jv = self.builder.load(j_slot)
        j_ge_0 = self.builder.icmp_signed('>=', jv, ir.Constant(i32, 0))
        # 조건: j >= 0 이면 cmp 체크, 아니면 끝
        check_bb = self.current_fn.append_basic_block("sort_check")
        self.builder.cbranch(j_ge_0, check_bb, iend)
        
        self.builder = ir.IRBuilder(check_bb)
        res_data2 = self.builder.load(res_data_pp, "sort_data2")
        jv2 = self.builder.load(j_slot)
        aj_ptr = self.builder.gep(res_data2, [jv2], inbounds=True)
        aj = self.builder.load(aj_ptr, "aj")
        cmp_result = self._closure_call(cb_fn, [aj, key], name="sort_cmp")
        # cmp > 0 이면 계속 이동
        if isinstance(cmp_result.type, ir.IntType):
            should_shift = self.builder.icmp_signed('>', cmp_result, ir.Constant(cmp_result.type, 0))
        else:
            should_shift = self.builder.fcmp_ordered('>', cmp_result, ir.Constant(cmp_result.type, 0.0))
        self.builder.cbranch(should_shift, ibody, iend)
        
        self.builder = ir.IRBuilder(ibody)
        res_data3 = self.builder.load(res_data_pp, "sort_data3")
        jv3 = self.builder.load(j_slot)
        src_p = self.builder.gep(res_data3, [jv3], inbounds=True)
        dst_idx = self.builder.add(jv3, ir.Constant(i32, 1))
        dst_p = self.builder.gep(res_data3, [dst_idx], inbounds=True)
        self.builder.store(self.builder.load(src_p), dst_p)
        self.builder.store(self.builder.sub(jv3, ir.Constant(i32, 1)), j_slot)
        self.builder.branch(icond)
        
        self.builder = ir.IRBuilder(iend)
        res_data4 = self.builder.load(res_data_pp, "sort_data4")
        jv4 = self.builder.load(j_slot)
        ins_idx = self.builder.add(jv4, ir.Constant(i32, 1))
        ins_ptr = self.builder.gep(res_data4, [ins_idx], inbounds=True)
        self.builder.store(key, ins_ptr)
        self.builder.store(self.builder.add(self.builder.load(oi), ir.Constant(i32, 1)), oi)
        self.builder.branch(ocond)
        
        self.builder = ir.IRBuilder(oend)
        return result_slot

    # ----- AST 노드별 컴파일 -----
    
    # ----- 7.12d1: ECS 런타임 본문 -----
    def _define_ecs_runtime(self):
        """_danha_ecs_init/spawn/destroy/is_alive의 LLVM IR 본문을 쌓는다.
        
        이 함수들은 사용자 코드 전에 선언만 돼 있고 본문이 비어 있었음.
        compile_program 시작에서 한 번 호출돼 본문을 채움.
        """
        i64 = ir.IntType(64)
        i8_zero = ir.Constant(i8, 0)
        i8_one = ir.Constant(i8, 1)
        
        # ----- _danha_ecs_init -----
        # 아레나 위에 gens/alive/free_list 배열을 확보하고 count/capacity 초기화.
        # 아레나는 자체 malloc 후 1MB라서 공간 충분 (4096 * 9 bytes = 36864 bytes < 1MB).
        #
        # 현재는 아레나 대신 독립 malloc으로 간다. 이유: 아레나 bump 로직이 동적 배열과
        # 엮여 있어서 초기화 순서가 까다로움. 독립 malloc이 단순하고, ECS World는 프로그램
        # 수명 내내 사는 거라 수동 free도 간단.
        #
        # 향후 최적화에서 아레나로 옮기면 캐시/지역성이 좋아짐.
        
        block = self.ecs_init_fn.append_basic_block(name="entry")
        b = ir.IRBuilder(block)
        
        cap = ir.Constant(i32, self.ECS_CAPACITY)
        cap64 = ir.Constant(i64, self.ECS_CAPACITY)
        
        # gens = malloc(capacity * 4); 모두 0으로 초기화 (malloc은 0을 보장 안 하지만,
        # 우리가 count까지만 만진다는 불변식으로 충분. count는 0부터 시작).
        gens_bytes = b.mul(cap64, ir.Constant(i64, 4))
        gens_raw = b.call(self.malloc_fn, [gens_bytes])
        gens_ptr = b.bitcast(gens_raw, i32.as_pointer())
        
        # alive = malloc(capacity). 마찬가지로 count 이하만 유효하므로 초기화 불필요.
        alive_raw = b.call(self.malloc_fn, [cap64])
        alive_ptr = alive_raw  # 이미 i8*
        
        # free_list = malloc(capacity * 4). 스택처럼 사용, free_count가 진짜 크기.
        fl_bytes = gens_bytes
        fl_raw = b.call(self.malloc_fn, [fl_bytes])
        fl_ptr = b.bitcast(fl_raw, i32.as_pointer())
        
        # world 필드 저장
        b.store(gens_ptr, b.gep(self.ecs_world,
            [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True))
        b.store(alive_ptr, b.gep(self.ecs_world,
            [ir.Constant(i32, 0), ir.Constant(i32, 1)], inbounds=True))
        b.store(ir.Constant(i32, 0), b.gep(self.ecs_world,
            [ir.Constant(i32, 0), ir.Constant(i32, 2)], inbounds=True))
        b.store(cap, b.gep(self.ecs_world,
            [ir.Constant(i32, 0), ir.Constant(i32, 3)], inbounds=True))
        b.store(fl_ptr, b.gep(self.ecs_world,
            [ir.Constant(i32, 0), ir.Constant(i32, 4)], inbounds=True))
        b.store(ir.Constant(i32, 0), b.gep(self.ecs_world,
            [ir.Constant(i32, 0), ir.Constant(i32, 5)], inbounds=True))
        
        # ----- 7.12d2: 컴포넌트별 저장소 초기화 -----
        # 각 컴포넌트의 필드 배열, dense_to_entity, sparse, count 초기화.
        # sparse는 "-1로 채운 배열" — 엔티티 e에 컴포넌트가 없음을 뜻함.
        # sparse 크기는 ECS_CAPACITY (entity idx 범위와 같음).
        # count는 0으로 (아직 컴포넌트 인스턴스 없음).
        # capacity는 선언 시 상수로 이미 박혀 있음.
        cap_comp64 = ir.Constant(i64, self.COMPONENT_CAPACITY)
        
        for comp_name, comp_info in self.components.items():
            # 7.15f: 필드별 타입에 맞는 크기로 malloc (이전엔 전부 f64=8바이트 고정)
            field_types = comp_info['field_types']
            for (fname, gv), lty in zip(comp_info['field_globals'].items(), field_types):
                size = self._sizeof_bytes(lty)
                fbytes = b.mul(cap_comp64, ir.Constant(i64, size))
                raw = b.call(self.malloc_fn, [fbytes])
                typed = b.bitcast(raw, lty.as_pointer())
                b.store(typed, gv)
            
            # dense_to_entity: i32 * capacity
            i32_bytes = b.mul(cap_comp64, ir.Constant(i64, 4))
            dte_raw = b.call(self.malloc_fn, [i32_bytes])
            dte_typed = b.bitcast(dte_raw, i32.as_pointer())
            b.store(dte_typed, comp_info['dense_to_entity'])
            
            # sparse: i32 * ECS_CAPACITY, 모두 -1로 채움.
            # sparse 크기는 entity 용량 기준 (ECS_CAPACITY) — 컴포넌트 용량과 우연히 같음.
            sp_bytes = b.mul(ir.Constant(i64, self.ECS_CAPACITY), ir.Constant(i64, 4))
            sp_raw = b.call(self.malloc_fn, [sp_bytes])
            sp_typed = b.bitcast(sp_raw, i32.as_pointer())
            b.store(sp_typed, comp_info['sparse'])
            
            # sparse 배열을 -1로 채우는 루프.
            # for i in 0..ECS_CAPACITY: sparse[i] = -1
            # phi 기반 루프 — predecessor 블록을 명시적으로 기록.
            pre_block = b.block  # 루프로 진입하기 직전 블록
            fill_cond = self.ecs_init_fn.append_basic_block(name=f"sp_fill_cond_{comp_name}")
            fill_body = self.ecs_init_fn.append_basic_block(name=f"sp_fill_body_{comp_name}")
            fill_end = self.ecs_init_fn.append_basic_block(name=f"sp_fill_end_{comp_name}")
            b.branch(fill_cond)
            
            b.position_at_end(fill_cond)
            i_phi = b.phi(i32, name=f"i_{comp_name}")
            i_phi.add_incoming(ir.Constant(i32, 0), pre_block)
            # 다른 incoming (body 끝에서 i+1)은 body 생성 후 추가.
            done = b.icmp_signed('>=', i_phi, ir.Constant(i32, self.ECS_CAPACITY))
            b.cbranch(done, fill_end, fill_body)
            
            b.position_at_end(fill_body)
            # sparse[i] = -1
            slot = b.gep(sp_typed, [i_phi], inbounds=True)
            b.store(ir.Constant(i32, -1), slot)
            next_i = b.add(i_phi, ir.Constant(i32, 1))
            body_end_block = b.block
            b.branch(fill_cond)
            i_phi.add_incoming(next_i, body_end_block)
            
            b.position_at_end(fill_end)
            # 루프 끝 — 이제 builder는 fill_end에 있음. 다음 컴포넌트로 이어짐.
        
        b.ret_void()
        
        # ----- helper: world 필드 접근 헬퍼를 위한 지역 함수 스타일 -----
        # 아래 함수들에서 반복되는 GEP 패턴. 각 함수가 자기 builder로 만들어야 하므로
        # 헬퍼는 클로저 형태.
        def world_field_ptr(builder, idx):
            return builder.gep(self.ecs_world,
                [ir.Constant(i32, 0), ir.Constant(i32, idx)], inbounds=True)
        
        # ----- _danha_ecs_spawn -----
        #   if free_count > 0:
        #     idx = free_list[free_count - 1]; free_count -= 1
        #     alive[idx] = 1
        #     return {idx, gens[idx]}
        #   else:
        #     if count >= capacity: abort (printf + exit)
        #     idx = count
        #     gens[idx] = 0  # 첫 탄생이므로 명시적 0
        #     alive[idx] = 1
        #     count++
        #     return {idx, 0}
        
        entry = self.ecs_spawn_fn.append_basic_block(name="entry")
        reuse_bb = self.ecs_spawn_fn.append_basic_block(name="reuse")
        fresh_bb = self.ecs_spawn_fn.append_basic_block(name="fresh")
        overflow_bb = self.ecs_spawn_fn.append_basic_block(name="overflow")
        ret_bb = self.ecs_spawn_fn.append_basic_block(name="ret")
        
        b = ir.IRBuilder(entry)
        free_count = b.load(world_field_ptr(b, 5))
        has_free = b.icmp_signed('>', free_count, ir.Constant(i32, 0))
        b.cbranch(has_free, reuse_bb, fresh_bb)
        
        # reuse branch
        b.position_at_end(reuse_bb)
        fl_ptr = b.load(world_field_ptr(b, 4))
        new_free_count = b.sub(free_count, ir.Constant(i32, 1))
        slot_gep = b.gep(fl_ptr, [new_free_count], inbounds=True)
        reuse_idx = b.load(slot_gep)
        b.store(new_free_count, world_field_ptr(b, 5))
        # alive[idx] = 1
        alive_ptr = b.load(world_field_ptr(b, 1))
        b.store(i8_one, b.gep(alive_ptr, [reuse_idx], inbounds=True))
        # gen = gens[idx]
        gens_ptr = b.load(world_field_ptr(b, 0))
        reuse_gen = b.load(b.gep(gens_ptr, [reuse_idx], inbounds=True))
        b.branch(ret_bb)
        reuse_end_block = b.block
        
        # fresh branch
        b.position_at_end(fresh_bb)
        count = b.load(world_field_ptr(b, 2))
        capacity = b.load(world_field_ptr(b, 3))
        overflow = b.icmp_signed('>=', count, capacity)
        fresh_ok_bb = self.ecs_spawn_fn.append_basic_block(name="fresh_ok")
        b.cbranch(overflow, overflow_bb, fresh_ok_bb)
        
        # overflow: printf + exit(1)
        b.position_at_end(overflow_bb)
        # 간단하게: printf로 메시지 찍고 free를 무한 루프 대신 호출하는 건 번거로우니,
        # C 런타임 exit를 선언해서 쓰자.
        if not hasattr(self, 'exit_fn'):
            exit_ty = ir.FunctionType(ir.VoidType(), [i32])
            self.exit_fn = ir.Function(self.module, exit_ty, name="exit")
        overflow_msg = self._make_global_string(
            "ecs_overflow_msg",
            f"단아 ECS: 엔티티 용량 초과 (capacity={self.ECS_CAPACITY})\n\0"
        )
        msg_ptr = b.gep(overflow_msg, [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True)
        b.call(self.printf, [msg_ptr])
        b.call(self.exit_fn, [ir.Constant(i32, 1)])
        b.unreachable()
        
        # fresh_ok: 새 슬롯 만들기
        b.position_at_end(fresh_ok_bb)
        fresh_idx = count
        # gens[idx] = 0
        gens_ptr2 = b.load(world_field_ptr(b, 0))
        b.store(ir.Constant(i32, 0), b.gep(gens_ptr2, [fresh_idx], inbounds=True))
        # alive[idx] = 1
        alive_ptr2 = b.load(world_field_ptr(b, 1))
        b.store(i8_one, b.gep(alive_ptr2, [fresh_idx], inbounds=True))
        # count++
        b.store(b.add(count, ir.Constant(i32, 1)), world_field_ptr(b, 2))
        fresh_gen = ir.Constant(i32, 0)
        b.branch(ret_bb)
        fresh_end_block = b.block
        
        # ret: phi로 idx와 gen 합친 후 구조체로 조립해서 반환
        b.position_at_end(ret_bb)
        phi_idx = b.phi(i32, name="idx")
        phi_idx.add_incoming(reuse_idx, reuse_end_block)
        phi_idx.add_incoming(fresh_idx, fresh_end_block)
        phi_gen = b.phi(i32, name="gen")
        phi_gen.add_incoming(reuse_gen, reuse_end_block)
        phi_gen.add_incoming(fresh_gen, fresh_end_block)
        
        # 구조체 값 조립: insertvalue 사용
        eid = b.insert_value(ir.Constant(self.entity_id_type, ir.Undefined), phi_idx, 0)
        eid = b.insert_value(eid, phi_gen, 1)
        b.ret(eid)
        
        # ----- 공통: EntityId 검증 블록을 만드는 헬퍼 -----
        # 반환은 (block_after_check, idx_value, is_valid_i1)
        # "is_valid"는 살아있고 세대도 맞는 경우만 true.
        # 유효하지 않으면 지정된 fail 블록으로 분기, 유효하면 ok 블록으로.
        
        def emit_validity_check(fn, b, eid_arg, ok_block, fail_block):
            """b에서 시작해 eid_arg의 유효성을 체크. ok/fail로 분기.
            ok_block에서 쓸 수 있게 idx를 리턴."""
            idx = b.extract_value(eid_arg, 0)
            gen = b.extract_value(eid_arg, 1)
            
            # 경계 체크: 0 <= idx < count
            neg_check = b.icmp_signed('<', idx, ir.Constant(i32, 0))
            bad1_bb = fn.append_basic_block(name="bad_neg")
            after_neg_bb = fn.append_basic_block(name="after_neg")
            b.cbranch(neg_check, bad1_bb, after_neg_bb)
            b.position_at_end(bad1_bb)
            b.branch(fail_block)
            
            b.position_at_end(after_neg_bb)
            count_v = b.load(world_field_ptr(b, 2))
            oob_check = b.icmp_signed('>=', idx, count_v)
            bad2_bb = fn.append_basic_block(name="bad_oob")
            after_oob_bb = fn.append_basic_block(name="after_oob")
            b.cbranch(oob_check, bad2_bb, after_oob_bb)
            b.position_at_end(bad2_bb)
            b.branch(fail_block)
            
            b.position_at_end(after_oob_bb)
            # alive[idx] == 1?
            alive_ptr_v = b.load(world_field_ptr(b, 1))
            alive_byte = b.load(b.gep(alive_ptr_v, [idx], inbounds=True))
            dead_check = b.icmp_signed('==', alive_byte, i8_zero)
            dead_bb = fn.append_basic_block(name="bad_dead")
            after_dead_bb = fn.append_basic_block(name="after_dead")
            b.cbranch(dead_check, dead_bb, after_dead_bb)
            b.position_at_end(dead_bb)
            b.branch(fail_block)
            
            b.position_at_end(after_dead_bb)
            # gens[idx] == gen?
            gens_ptr_v = b.load(world_field_ptr(b, 0))
            cur_gen = b.load(b.gep(gens_ptr_v, [idx], inbounds=True))
            gen_eq = b.icmp_signed('==', cur_gen, gen)
            b.cbranch(gen_eq, ok_block, fail_block)
            # 여기서 builder는 끝남. 호출자는 ok_block에서 재위치해야 함.
            return idx
        
        # ----- _danha_ecs_is_alive -----
        #   유효 → true, 아니면 false
        entry = self.ecs_is_alive_fn.append_basic_block(name="entry")
        ok_bb = self.ecs_is_alive_fn.append_basic_block(name="ok")
        fail_bb = self.ecs_is_alive_fn.append_basic_block(name="fail")
        
        b = ir.IRBuilder(entry)
        eid_arg = self.ecs_is_alive_fn.args[0]
        emit_validity_check(self.ecs_is_alive_fn, b, eid_arg, ok_bb, fail_bb)
        
        b.position_at_end(ok_bb)
        b.ret(ir.Constant(ir.IntType(1), 1))
        b.position_at_end(fail_bb)
        b.ret(ir.Constant(ir.IntType(1), 0))
        
        # ----- _danha_ecs_destroy -----
        #   유효 체크 실패 → false
        #   유효 → alive[idx]=0, gens[idx]++, free_list push, true
        entry = self.ecs_destroy_fn.append_basic_block(name="entry")
        ok_bb = self.ecs_destroy_fn.append_basic_block(name="ok")
        fail_bb = self.ecs_destroy_fn.append_basic_block(name="fail")
        
        b = ir.IRBuilder(entry)
        eid_arg = self.ecs_destroy_fn.args[0]
        idx_val = emit_validity_check(self.ecs_destroy_fn, b, eid_arg, ok_bb, fail_bb)
        
        b.position_at_end(ok_bb)
        # ok 블록에서는 idx_val이 entry에서 온 것이라 지배(dominate) 관계가 복잡할 수 있음.
        # 실제로는 entry의 첫 두 instruction (extract_value)이 entry를 지배하니까 ok도 지배함.
        # 안전하게 여기서 다시 extract.
        idx = b.extract_value(eid_arg, 0)
        # d2-iii: 이 엔티티에 붙은 모든 컴포넌트를 먼저 정리.
        # 컴파일 시점에 컴포넌트 목록이 다 알려져 있으니 IR에 unroll된 호출 나열.
        # 순서는 무관 (각 remove는 독립).
        for comp_name, info in self.components.items():
            b.call(info['remove_fn'], [idx])
        # alive[idx] = 0
        alive_ptr_v = b.load(world_field_ptr(b, 1))
        b.store(i8_zero, b.gep(alive_ptr_v, [idx], inbounds=True))
        # gens[idx]++
        gens_ptr_v = b.load(world_field_ptr(b, 0))
        gen_slot = b.gep(gens_ptr_v, [idx], inbounds=True)
        old_gen = b.load(gen_slot)
        b.store(b.add(old_gen, ir.Constant(i32, 1)), gen_slot)
        # free_list[free_count++] = idx
        fc = b.load(world_field_ptr(b, 5))
        fl = b.load(world_field_ptr(b, 4))
        b.store(idx, b.gep(fl, [fc], inbounds=True))
        b.store(b.add(fc, ir.Constant(i32, 1)), world_field_ptr(b, 5))
        b.ret(ir.Constant(ir.IntType(1), 1))
        
        b.position_at_end(fail_bb)
        b.ret(ir.Constant(ir.IntType(1), 0))


    def _define_hashmap_runtime(self):
        """30: HashMap 런타임 함수 본문을 LLVM IR로 생성.
        
        HashMap 메모리 레이아웃 (하나의 malloc 블록):
        ┌──────────┬──────────┬─────────────────────────────┐
        │ cap (i32) │ cnt (i32) │ entries[cap] ...             │
        └──────────┴──────────┴─────────────────────────────┘
        각 엔트리 (24바이트):
          i8* key (8) + i64 value (8) + i32 occupied (4) + i32 hash (4)
        
        해시 함수: FNV-1a 32-bit (문자열 → i32)
        충돌 해결: 선형 탐사 (open addressing)
        """
        ENTRY_SIZE = 24  # 바이트
        
        # 해시 함수 (FNV-1a) — _danha_hm_hash(key: i8*) -> i32
        hash_ty = ir.FunctionType(i32, [i8p])
        hash_fn = ir.Function(self.module, hash_ty, name="_danha_hm_hash")
        hash_fn.linkage = 'private'
        entry_bb = hash_fn.append_basic_block("entry")
        loop_bb = hash_fn.append_basic_block("loop")
        done_bb = hash_fn.append_basic_block("done")
        b = ir.IRBuilder(entry_bb)
        key_arg = hash_fn.args[0]
        hash_slot = b.alloca(i32, name="hash")
        b.store(ir.Constant(i32, 2166136261), hash_slot)
        idx_slot = b.alloca(i32, name="idx")
        b.store(ir.Constant(i32, 0), idx_slot)
        b.branch(loop_bb)
        
        b = ir.IRBuilder(loop_bb)
        idx = b.load(idx_slot, name="i")
        char_ptr = b.gep(key_arg, [idx], inbounds=True, name="cp")
        ch = b.load(char_ptr, name="ch")
        is_zero = b.icmp_unsigned('==', ch, ir.Constant(i8, 0), name="end")
        cont_bb = hash_fn.append_basic_block("cont")
        b.cbranch(is_zero, done_bb, cont_bb)
        
        b = ir.IRBuilder(cont_bb)
        h = b.load(hash_slot, name="h")
        ch_ext = b.zext(ch, i32, name="ch32")
        h_xor = b.xor(h, ch_ext, name="hxor")
        h_new = b.mul(h_xor, ir.Constant(i32, 16777619), name="hnew")
        b.store(h_new, hash_slot)
        idx_next = b.add(idx, ir.Constant(i32, 1), name="inext")
        b.store(idx_next, idx_slot)
        b.branch(loop_bb)
        
        b = ir.IRBuilder(done_bb)
        final_h = b.load(hash_slot, name="hfinal")
        # 양수로 만들기: and 0x7FFFFFFF
        pos_h = b.and_(final_h, ir.Constant(i32, 0x7FFFFFFF), name="hpos")
        b.ret(pos_h)
        
        self._hm_hash_fn = hash_fn
        
        # ===== _danha_hm_new() → i8* =====
        fn = self.hm_new_fn
        entry_bb = fn.append_basic_block("entry")
        b = ir.IRBuilder(entry_bb)
        cap = self.HASHMAP_INIT_CAP
        # 8 (header) + cap * 24 (entries)
        total = 8 + cap * ENTRY_SIZE
        mem = b.call(self.malloc_fn, [ir.Constant(i64, total)], name="hm_mem")
        # memset 0
        b.call(self.memset_fn, [
            mem, ir.Constant(i8, 0), ir.Constant(i64, total), ir.Constant(ir.IntType(1), 0)
        ])
        # store capacity
        cap_ptr = b.bitcast(mem, i32.as_pointer(), name="cap_ptr")
        b.store(ir.Constant(i32, cap), cap_ptr)
        # count = 0 already (memset)
        b.ret(mem)

        # ===== _danha_hm_new_cap(cap: i32) → i8* =====
        fn = self.hm_new_cap_fn
        entry_bb = fn.append_basic_block("entry")
        b = ir.IRBuilder(entry_bb)
        requested = fn.args[0]
        too_small = b.icmp_signed('<', requested, ir.Constant(i32, 16), name="cap_small")
        min_cap = b.select(too_small, ir.Constant(i32, 16), requested, name="cap_min")
        round_loop_bb = fn.append_basic_block("round_loop")
        round_done_bb = fn.append_basic_block("round_done")
        cap_slot = b.alloca(i32, name="cap_slot")
        b.store(ir.Constant(i32, 16), cap_slot)
        b.branch(round_loop_bb)

        b = ir.IRBuilder(round_loop_bb)
        cur_cap = b.load(cap_slot, name="cap_cur")
        cap_done = b.icmp_unsigned('>=', cur_cap, min_cap, name="cap_done")
        round_step_bb = fn.append_basic_block("round_step")
        b.cbranch(cap_done, round_done_bb, round_step_bb)

        b = ir.IRBuilder(round_step_bb)
        next_cap = b.shl(cur_cap, ir.Constant(i32, 1), name="cap_next")
        b.store(next_cap, cap_slot)
        b.branch(round_loop_bb)

        b = ir.IRBuilder(round_done_bb)
        chosen_cap = b.load(cap_slot, name="cap_chosen")
        cap64 = b.zext(chosen_cap, i64, name="cap64")
        entries_bytes = b.mul(cap64, ir.Constant(i64, ENTRY_SIZE), name="hm_entries_bytes")
        total = b.add(entries_bytes, ir.Constant(i64, 8), name="hm_total")
        mem = b.call(self.malloc_fn, [total], name="hm_mem_cap")
        b.call(self.memset_fn, [
            mem, ir.Constant(i8, 0), total, ir.Constant(ir.IntType(1), 0)
        ])
        cap_ptr = b.bitcast(mem, i32.as_pointer(), name="cap_ptr")
        b.store(chosen_cap, cap_ptr)
        b.ret(mem)

        # ===== _danha_hm_insert_raw(hm, key, val) =====
        # 성장 검사를 하지 않는 내부 삽입. 리사이즈 중 재해시에 사용한다.
        raw_ty = ir.FunctionType(ir.VoidType(), [i8p, i8p, i64])
        raw_insert_fn = ir.Function(self.module, raw_ty, name="_danha_hm_insert_raw")
        raw_insert_fn.linkage = 'private'
        entry_bb = raw_insert_fn.append_basic_block("entry")
        search_bb = raw_insert_fn.append_basic_block("search")
        found_bb = raw_insert_fn.append_basic_block("found")
        empty_bb = raw_insert_fn.append_basic_block("empty")
        next_bb = raw_insert_fn.append_basic_block("next")
        b = ir.IRBuilder(entry_bb)
        hm, key, val = raw_insert_fn.args
        cap_ptr = b.bitcast(hm, i32.as_pointer(), name="cap_ptr")
        cap_val = b.load(cap_ptr, name="cap")
        cnt_ptr = b.gep(hm, [ir.Constant(i32, 4)], name="cnt_gep")
        cnt_ptr = b.bitcast(cnt_ptr, i32.as_pointer(), name="cnt_ptr")
        entries_base = b.gep(hm, [ir.Constant(i32, 8)], name="entries")
        h = b.call(hash_fn, [key], name="hash")
        cap_mask = b.sub(cap_val, ir.Constant(i32, 1), name="cap_mask")
        start_idx = b.and_(h, cap_mask, name="start")
        idx_slot = b.alloca(i32, name="idx")
        probe_slot = b.alloca(i32, name="probe")
        b.store(start_idx, idx_slot)
        b.store(ir.Constant(i32, 0), probe_slot)
        b.branch(search_bb)

        b = ir.IRBuilder(search_bb)
        idx = b.load(idx_slot, name="i")
        offset = b.mul(idx, ir.Constant(i32, ENTRY_SIZE), name="off")
        offset64 = b.zext(offset, i64, name="off64")
        entry_ptr = b.gep(entries_base, [offset64], name="ep")
        occ_ptr = b.gep(entry_ptr, [ir.Constant(i32, 16)], name="occ_gep")
        occ_ptr = b.bitcast(occ_ptr, i32.as_pointer(), name="occ_ptr")
        occ = b.load(occ_ptr, name="occ")
        is_occupied = b.icmp_unsigned('==', occ, ir.Constant(i32, 1), name="is_occ")
        check_key_bb = raw_insert_fn.append_basic_block("check_key")
        is_empty = b.icmp_unsigned('==', occ, ir.Constant(i32, 0), name="is_empty")
        raw_probe_bb = raw_insert_fn.append_basic_block("raw_probe")
        b.cbranch(is_occupied, check_key_bb, raw_probe_bb)

        b = ir.IRBuilder(raw_probe_bb)
        b.cbranch(is_empty, empty_bb, next_bb)

        b = ir.IRBuilder(check_key_bb)
        hash_ptr = b.gep(entry_ptr, [ir.Constant(i32, 20)], name="hash_gep")
        hash_ptr = b.bitcast(hash_ptr, i32.as_pointer(), name="hash_ptr")
        existing_hash = b.load(hash_ptr, name="ehash")
        hash_same = b.icmp_unsigned('==', existing_hash, h, name="hash_same")
        raw_hash_match_bb = raw_insert_fn.append_basic_block("hash_match")
        b.cbranch(hash_same, raw_hash_match_bb, next_bb)

        b = ir.IRBuilder(raw_hash_match_bb)
        key_ptr = b.bitcast(entry_ptr, i8p.as_pointer(), name="key_ptr")
        existing_key = b.load(key_ptr, name="ekey")
        cmp = b.call(self.strcmp_fn, [existing_key, key], name="scmp")
        is_same = b.icmp_signed('==', cmp, ir.Constant(i32, 0), name="same")
        b.cbranch(is_same, found_bb, next_bb)

        b = ir.IRBuilder(found_bb)
        val_ptr = b.gep(entry_ptr, [ir.Constant(i32, 8)], name="val_gep")
        val_ptr = b.bitcast(val_ptr, i64.as_pointer(), name="val_ptr")
        b.store(val, val_ptr)
        b.ret_void()

        b = ir.IRBuilder(empty_bb)
        key_ptr = b.bitcast(entry_ptr, i8p.as_pointer(), name="key_ptr2")
        b.store(key, key_ptr)
        val_ptr = b.gep(entry_ptr, [ir.Constant(i32, 8)], name="val_gep2")
        val_ptr = b.bitcast(val_ptr, i64.as_pointer(), name="val_ptr2")
        b.store(val, val_ptr)
        occ_ptr2 = b.gep(entry_ptr, [ir.Constant(i32, 16)], name="occ_gep2")
        occ_ptr2 = b.bitcast(occ_ptr2, i32.as_pointer(), name="occ_ptr2")
        b.store(ir.Constant(i32, 1), occ_ptr2)
        hash_ptr2 = b.gep(entry_ptr, [ir.Constant(i32, 20)], name="hash_gep2")
        hash_ptr2 = b.bitcast(hash_ptr2, i32.as_pointer(), name="hash_ptr2")
        b.store(h, hash_ptr2)
        cnt = b.load(cnt_ptr, name="cnt")
        cnt_new = b.add(cnt, ir.Constant(i32, 1), name="cnt1")
        b.store(cnt_new, cnt_ptr)
        b.ret_void()

        b = ir.IRBuilder(next_bb)
        idx_next = b.add(idx, ir.Constant(i32, 1), name="inxt")
        idx_wrap = b.and_(idx_next, cap_mask, name="iwrap")
        b.store(idx_wrap, idx_slot)
        probe = b.load(probe_slot, name="probe")
        probe_next = b.add(probe, ir.Constant(i32, 1), name="probe_next")
        b.store(probe_next, probe_slot)
        is_full = b.icmp_unsigned('>=', probe_next, cap_val, name="hm_full")
        raw_continue_bb = raw_insert_fn.append_basic_block("raw_continue")
        raw_full_bb = raw_insert_fn.append_basic_block("raw_full")
        b.cbranch(is_full, raw_full_bb, raw_continue_bb)
        b = ir.IRBuilder(raw_full_bb)
        b.ret_void()
        b = ir.IRBuilder(raw_continue_bb)
        b.branch(search_bb)

        # ===== _danha_hm_grow(hm) -> i8* =====
        grow_ty = ir.FunctionType(i8p, [i8p])
        grow_fn = ir.Function(self.module, grow_ty, name="_danha_hm_grow")
        grow_fn.linkage = 'private'
        entry_bb = grow_fn.append_basic_block("entry")
        loop_bb = grow_fn.append_basic_block("loop")
        check_bb = grow_fn.append_basic_block("check")
        reinsert_bb = grow_fn.append_basic_block("reinsert")
        next_bb = grow_fn.append_basic_block("next")
        done_bb = grow_fn.append_basic_block("done")
        b = ir.IRBuilder(entry_bb)
        old_hm = grow_fn.args[0]
        old_cap_ptr = b.bitcast(old_hm, i32.as_pointer(), name="old_cap_ptr")
        old_cap = b.load(old_cap_ptr, name="old_cap")
        new_cap = b.mul(old_cap, ir.Constant(i32, 2), name="new_cap")
        new_hm = b.call(self.hm_new_cap_fn, [new_cap], name="new_hm")
        old_entries = b.gep(old_hm, [ir.Constant(i32, 8)], name="old_entries")
        i_slot = b.alloca(i32, name="i")
        b.store(ir.Constant(i32, 0), i_slot)
        b.branch(loop_bb)

        b = ir.IRBuilder(loop_bb)
        idx = b.load(i_slot, name="i")
        done = b.icmp_unsigned('>=', idx, old_cap, name="done")
        b.cbranch(done, done_bb, check_bb)

        b = ir.IRBuilder(check_bb)
        offset = b.mul(idx, ir.Constant(i32, ENTRY_SIZE), name="off")
        offset64 = b.zext(offset, i64, name="off64")
        entry_ptr = b.gep(old_entries, [offset64], name="ep")
        occ_ptr = b.gep(entry_ptr, [ir.Constant(i32, 16)], name="occ_gep")
        occ_ptr = b.bitcast(occ_ptr, i32.as_pointer(), name="occ_ptr")
        occ = b.load(occ_ptr, name="occ")
        is_occupied = b.icmp_unsigned('==', occ, ir.Constant(i32, 1), name="is_occ")
        b.cbranch(is_occupied, reinsert_bb, next_bb)

        b = ir.IRBuilder(reinsert_bb)
        key_ptr = b.bitcast(entry_ptr, i8p.as_pointer(), name="key_ptr")
        existing_key = b.load(key_ptr, name="ekey")
        val_ptr = b.gep(entry_ptr, [ir.Constant(i32, 8)], name="val_gep")
        val_ptr = b.bitcast(val_ptr, i64.as_pointer(), name="val_ptr")
        existing_val = b.load(val_ptr, name="eval")
        b.call(raw_insert_fn, [new_hm, existing_key, existing_val])
        b.branch(next_bb)

        b = ir.IRBuilder(next_bb)
        idx_next = b.add(idx, ir.Constant(i32, 1), name="inxt")
        b.store(idx_next, i_slot)
        b.branch(loop_bb)

        b = ir.IRBuilder(done_bb)
        b.call(self.free_fn, [old_hm])
        b.ret(new_hm)
        
        # ===== _danha_hm_set(hm: i8*, key: i8*, val: i64) =====
        fn = self.hm_set_fn
        entry_bb = fn.append_basic_block("entry")
        search_bb = fn.append_basic_block("search")
        found_bb = fn.append_basic_block("found")
        empty_bb = fn.append_basic_block("empty")
        next_bb = fn.append_basic_block("next")
        b = ir.IRBuilder(entry_bb)
        hm, key, val = fn.args
        hm_slot = b.alloca(i8p, name="hm_slot")
        b.store(hm, hm_slot)
        initial_cap_ptr = b.bitcast(hm, i32.as_pointer(), name="initial_cap_ptr")
        initial_cap = b.load(initial_cap_ptr, name="initial_cap")
        initial_cnt_ptr = b.gep(hm, [ir.Constant(i32, 4)], name="initial_cnt_gep")
        initial_cnt_ptr = b.bitcast(initial_cnt_ptr, i32.as_pointer(), name="initial_cnt_ptr")
        initial_cnt = b.load(initial_cnt_ptr, name="initial_cnt")
        projected_cnt = b.add(initial_cnt, ir.Constant(i32, 1), name="projected_cnt")
        lhs = b.mul(projected_cnt, ir.Constant(i32, 4), name="load_lhs")
        rhs = b.mul(initial_cap, ir.Constant(i32, 3), name="load_rhs")
        should_grow = b.icmp_unsigned('>=', lhs, rhs, name="should_grow")
        grow_bb = fn.append_basic_block("grow")
        insert_bb = fn.append_basic_block("insert")
        b.cbranch(should_grow, grow_bb, insert_bb)

        b = ir.IRBuilder(grow_bb)
        grown_hm = b.call(grow_fn, [hm], name="grown_hm")
        b.store(grown_hm, hm_slot)
        b.branch(insert_bb)

        b = ir.IRBuilder(insert_bb)
        hm_current = b.load(hm_slot, name="hm_current")
        cap_ptr = b.bitcast(hm_current, i32.as_pointer(), name="cap_ptr")
        cap_val = b.load(cap_ptr, name="cap")
        cnt_ptr = b.gep(hm_current, [ir.Constant(i32, 4)], name="cnt_gep")
        cnt_ptr = b.bitcast(cnt_ptr, i32.as_pointer(), name="cnt_ptr")
        entries_base = b.gep(hm_current, [ir.Constant(i32, 8)], name="entries")
        h = b.call(hash_fn, [key], name="hash")
        cap_mask = b.sub(cap_val, ir.Constant(i32, 1), name="cap_mask")
        start_idx = b.and_(h, cap_mask, name="start")
        idx_slot = b.alloca(i32, name="idx")
        probe_slot = b.alloca(i32, name="probe")
        b.store(start_idx, idx_slot)
        b.store(ir.Constant(i32, 0), probe_slot)
        b.branch(search_bb)
        
        b = ir.IRBuilder(search_bb)
        idx = b.load(idx_slot, name="i")
        # entry_ptr = entries_base + idx * 24
        offset = b.mul(idx, ir.Constant(i32, ENTRY_SIZE), name="off")
        offset64 = b.zext(offset, i64, name="off64")
        entry_ptr = b.gep(entries_base, [offset64], name="ep")
        # occupied at entry_ptr + 16
        occ_ptr = b.gep(entry_ptr, [ir.Constant(i32, 16)], name="occ_gep")
        occ_ptr = b.bitcast(occ_ptr, i32.as_pointer(), name="occ_ptr")
        occ = b.load(occ_ptr, name="occ")
        is_occupied = b.icmp_unsigned('==', occ, ir.Constant(i32, 1), name="is_occ")
        check_key_bb = fn.append_basic_block("check_key")
        is_empty = b.icmp_unsigned('==', occ, ir.Constant(i32, 0), name="is_empty")
        set_probe_bb = fn.append_basic_block("set_probe")
        b.cbranch(is_occupied, check_key_bb, set_probe_bb)
        
        b = ir.IRBuilder(set_probe_bb)
        b.cbranch(is_empty, empty_bb, next_bb)
        
        b = ir.IRBuilder(check_key_bb)
        # 기존 키와 비교
        hash_ptr = b.gep(entry_ptr, [ir.Constant(i32, 20)], name="hash_gep")
        hash_ptr = b.bitcast(hash_ptr, i32.as_pointer(), name="hash_ptr")
        existing_hash = b.load(hash_ptr, name="ehash")
        hash_same = b.icmp_unsigned('==', existing_hash, h, name="hash_same")
        set_hash_match_bb = fn.append_basic_block("hash_match")
        b.cbranch(hash_same, set_hash_match_bb, next_bb)

        b = ir.IRBuilder(set_hash_match_bb)
        key_ptr = b.bitcast(entry_ptr, i8p.as_pointer(), name="key_ptr")
        existing_key = b.load(key_ptr, name="ekey")
        cmp = b.call(self.strcmp_fn, [existing_key, key], name="scmp")
        is_same = b.icmp_signed('==', cmp, ir.Constant(i32, 0), name="same")
        b.cbranch(is_same, found_bb, next_bb)
        
        b = ir.IRBuilder(found_bb)
        # 같은 키 → 값 덮어쓰기
        val_ptr = b.gep(entry_ptr, [ir.Constant(i32, 8)], name="val_gep")
        val_ptr = b.bitcast(val_ptr, i64.as_pointer(), name="val_ptr")
        b.store(val, val_ptr)
        b.ret(hm_current)
        
        b = ir.IRBuilder(empty_bb)
        # 빈 슬롯 → 키/값 저장
        key_ptr = b.bitcast(entry_ptr, i8p.as_pointer(), name="key_ptr2")
        b.store(key, key_ptr)
        val_ptr = b.gep(entry_ptr, [ir.Constant(i32, 8)], name="val_gep2")
        val_ptr = b.bitcast(val_ptr, i64.as_pointer(), name="val_ptr2")
        b.store(val, val_ptr)
        occ_ptr2 = b.gep(entry_ptr, [ir.Constant(i32, 16)], name="occ_gep2")
        occ_ptr2 = b.bitcast(occ_ptr2, i32.as_pointer(), name="occ_ptr2")
        b.store(ir.Constant(i32, 1), occ_ptr2)
        hash_ptr2 = b.gep(entry_ptr, [ir.Constant(i32, 20)], name="hash_gep2")
        hash_ptr2 = b.bitcast(hash_ptr2, i32.as_pointer(), name="hash_ptr2")
        b.store(h, hash_ptr2)
        cnt = b.load(cnt_ptr, name="cnt")
        cnt_new = b.add(cnt, ir.Constant(i32, 1), name="cnt1")
        b.store(cnt_new, cnt_ptr)
        b.ret(hm_current)
        
        b = ir.IRBuilder(next_bb)
        idx_next = b.add(idx, ir.Constant(i32, 1), name="inxt")
        idx_wrap = b.and_(idx_next, cap_mask, name="iwrap")
        b.store(idx_wrap, idx_slot)
        probe = b.load(probe_slot, name="probe")
        probe_next = b.add(probe, ir.Constant(i32, 1), name="probe_next")
        b.store(probe_next, probe_slot)
        is_full = b.icmp_unsigned('>=', probe_next, cap_val, name="hm_full")
        set_continue_bb = fn.append_basic_block("set_continue")
        set_full_bb = fn.append_basic_block("set_full")
        b.cbranch(is_full, set_full_bb, set_continue_bb)
        b = ir.IRBuilder(set_full_bb)
        b.ret(hm_current)
        b = ir.IRBuilder(set_continue_bb)
        b.branch(search_bb)
        
        # ===== _danha_hm_get(hm: i8*, key: i8*) → i64 =====
        fn = self.hm_get_fn
        entry_bb = fn.append_basic_block("entry")
        search_bb = fn.append_basic_block("search")
        found_bb = fn.append_basic_block("found")
        not_found_bb = fn.append_basic_block("not_found")
        next_bb = fn.append_basic_block("next")
        b = ir.IRBuilder(entry_bb)
        hm, key = fn.args
        cap_ptr = b.bitcast(hm, i32.as_pointer(), name="cap_ptr")
        cap_val = b.load(cap_ptr, name="cap")
        entries_base = b.gep(hm, [ir.Constant(i32, 8)], name="entries")
        h = b.call(hash_fn, [key], name="hash")
        cap_mask = b.sub(cap_val, ir.Constant(i32, 1), name="cap_mask")
        start_idx = b.and_(h, cap_mask, name="start")
        idx_slot = b.alloca(i32, name="idx")
        probe_slot = b.alloca(i32, name="probe")
        b.store(start_idx, idx_slot)
        b.store(ir.Constant(i32, 0), probe_slot)
        b.branch(search_bb)
        
        b = ir.IRBuilder(search_bb)
        idx = b.load(idx_slot, name="i")
        offset = b.mul(idx, ir.Constant(i32, ENTRY_SIZE), name="off")
        offset64 = b.zext(offset, i64, name="off64")
        entry_ptr = b.gep(entries_base, [offset64], name="ep")
        occ_ptr = b.gep(entry_ptr, [ir.Constant(i32, 16)], name="occ_gep")
        occ_ptr = b.bitcast(occ_ptr, i32.as_pointer(), name="occ_ptr")
        occ = b.load(occ_ptr, name="occ")
        is_occupied = b.icmp_unsigned('==', occ, ir.Constant(i32, 1), name="is_occ")
        check_bb = fn.append_basic_block("check")
        is_empty = b.icmp_unsigned('==', occ, ir.Constant(i32, 0), name="is_empty")
        cont_probe_bb = fn.append_basic_block("cont_probe")
        b.cbranch(is_occupied, check_bb, cont_probe_bb)
        
        b = ir.IRBuilder(cont_probe_bb)
        b.cbranch(is_empty, not_found_bb, next_bb)
        
        b = ir.IRBuilder(check_bb)
        hash_ptr = b.gep(entry_ptr, [ir.Constant(i32, 20)], name="hash_gep")
        hash_ptr = b.bitcast(hash_ptr, i32.as_pointer(), name="hash_ptr")
        existing_hash = b.load(hash_ptr, name="ehash")
        hash_same = b.icmp_unsigned('==', existing_hash, h, name="hash_same")
        get_hash_match_bb = fn.append_basic_block("hash_match")
        b.cbranch(hash_same, get_hash_match_bb, next_bb)

        b = ir.IRBuilder(get_hash_match_bb)
        key_ptr = b.bitcast(entry_ptr, i8p.as_pointer(), name="key_ptr")
        existing_key = b.load(key_ptr, name="ekey")
        cmp = b.call(self.strcmp_fn, [existing_key, key], name="scmp")
        is_same = b.icmp_signed('==', cmp, ir.Constant(i32, 0), name="same")
        b.cbranch(is_same, found_bb, next_bb)
        
        b = ir.IRBuilder(found_bb)
        val_ptr = b.gep(entry_ptr, [ir.Constant(i32, 8)], name="val_gep")
        val_ptr = b.bitcast(val_ptr, i64.as_pointer(), name="val_ptr")
        val = b.load(val_ptr, name="val")
        b.ret(val)
        
        b = ir.IRBuilder(not_found_bb)
        b.ret(ir.Constant(i64, 0))
        
        b = ir.IRBuilder(next_bb)
        idx_next = b.add(idx, ir.Constant(i32, 1), name="inxt")
        idx_wrap = b.and_(idx_next, cap_mask, name="iwrap")
        b.store(idx_wrap, idx_slot)
        probe = b.load(probe_slot, name="probe")
        probe_next = b.add(probe, ir.Constant(i32, 1), name="probe_next")
        b.store(probe_next, probe_slot)
        is_full = b.icmp_unsigned('>=', probe_next, cap_val, name="hm_full")
        get_continue_bb = fn.append_basic_block("get_continue")
        b.cbranch(is_full, not_found_bb, get_continue_bb)
        b = ir.IRBuilder(get_continue_bb)
        b.branch(search_bb)
        
        # ===== _danha_hm_has(hm: i8*, key: i8*) → i1 =====
        fn = self.hm_has_fn
        entry_bb = fn.append_basic_block("entry")
        search_bb = fn.append_basic_block("search")
        found_bb = fn.append_basic_block("found")
        not_found_bb = fn.append_basic_block("not_found")
        next_bb = fn.append_basic_block("next")
        b = ir.IRBuilder(entry_bb)
        hm, key = fn.args
        cap_ptr = b.bitcast(hm, i32.as_pointer(), name="cap_ptr")
        cap_val = b.load(cap_ptr, name="cap")
        entries_base = b.gep(hm, [ir.Constant(i32, 8)], name="entries")
        h = b.call(hash_fn, [key], name="hash")
        cap_mask = b.sub(cap_val, ir.Constant(i32, 1), name="cap_mask")
        start_idx = b.and_(h, cap_mask, name="start")
        idx_slot = b.alloca(i32, name="idx")
        probe_slot = b.alloca(i32, name="probe")
        b.store(start_idx, idx_slot)
        b.store(ir.Constant(i32, 0), probe_slot)
        b.branch(search_bb)
        
        b = ir.IRBuilder(search_bb)
        idx = b.load(idx_slot, name="i")
        offset = b.mul(idx, ir.Constant(i32, ENTRY_SIZE), name="off")
        offset64 = b.zext(offset, i64, name="off64")
        entry_ptr = b.gep(entries_base, [offset64], name="ep")
        occ_ptr = b.gep(entry_ptr, [ir.Constant(i32, 16)], name="occ_gep")
        occ_ptr = b.bitcast(occ_ptr, i32.as_pointer(), name="occ_ptr")
        occ = b.load(occ_ptr, name="occ")
        is_occupied = b.icmp_unsigned('==', occ, ir.Constant(i32, 1), name="is_occ")
        check_bb = fn.append_basic_block("check")
        is_empty = b.icmp_unsigned('==', occ, ir.Constant(i32, 0), name="is_empty")
        cont_probe_bb = fn.append_basic_block("cont_probe")
        b.cbranch(is_occupied, check_bb, cont_probe_bb)
        
        b = ir.IRBuilder(cont_probe_bb)
        b.cbranch(is_empty, not_found_bb, next_bb)
        
        b = ir.IRBuilder(check_bb)
        hash_ptr = b.gep(entry_ptr, [ir.Constant(i32, 20)], name="hash_gep")
        hash_ptr = b.bitcast(hash_ptr, i32.as_pointer(), name="hash_ptr")
        existing_hash = b.load(hash_ptr, name="ehash")
        hash_same = b.icmp_unsigned('==', existing_hash, h, name="hash_same")
        has_hash_match_bb = fn.append_basic_block("hash_match")
        b.cbranch(hash_same, has_hash_match_bb, next_bb)

        b = ir.IRBuilder(has_hash_match_bb)
        key_ptr = b.bitcast(entry_ptr, i8p.as_pointer(), name="key_ptr")
        existing_key = b.load(key_ptr, name="ekey")
        cmp = b.call(self.strcmp_fn, [existing_key, key], name="scmp")
        is_same = b.icmp_signed('==', cmp, ir.Constant(i32, 0), name="same")
        b.cbranch(is_same, found_bb, next_bb)
        
        b = ir.IRBuilder(found_bb)
        b.ret(ir.Constant(ir.IntType(1), 1))
        b = ir.IRBuilder(not_found_bb)
        b.ret(ir.Constant(ir.IntType(1), 0))
        b = ir.IRBuilder(next_bb)
        idx_next = b.add(idx, ir.Constant(i32, 1), name="inxt")
        idx_wrap = b.and_(idx_next, cap_mask, name="iwrap")
        b.store(idx_wrap, idx_slot)
        probe = b.load(probe_slot, name="probe")
        probe_next = b.add(probe, ir.Constant(i32, 1), name="probe_next")
        b.store(probe_next, probe_slot)
        is_full = b.icmp_unsigned('>=', probe_next, cap_val, name="hm_full")
        has_continue_bb = fn.append_basic_block("has_continue")
        b.cbranch(is_full, not_found_bb, has_continue_bb)
        b = ir.IRBuilder(has_continue_bb)
        b.branch(search_bb)
        
        # ===== _danha_hm_remove(hm: i8*, key: i8*) → i1 =====
        fn = self.hm_remove_fn
        entry_bb = fn.append_basic_block("entry")
        search_bb = fn.append_basic_block("search")
        found_bb = fn.append_basic_block("found")
        not_found_bb = fn.append_basic_block("not_found")
        next_bb = fn.append_basic_block("next")
        b = ir.IRBuilder(entry_bb)
        hm, key = fn.args
        cap_ptr = b.bitcast(hm, i32.as_pointer(), name="cap_ptr")
        cap_val = b.load(cap_ptr, name="cap")
        cnt_ptr = b.gep(hm, [ir.Constant(i32, 4)], name="cnt_gep")
        cnt_ptr = b.bitcast(cnt_ptr, i32.as_pointer(), name="cnt_ptr")
        entries_base = b.gep(hm, [ir.Constant(i32, 8)], name="entries")
        h = b.call(hash_fn, [key], name="hash")
        cap_mask = b.sub(cap_val, ir.Constant(i32, 1), name="cap_mask")
        start_idx = b.and_(h, cap_mask, name="start")
        idx_slot = b.alloca(i32, name="idx")
        probe_slot = b.alloca(i32, name="probe")
        b.store(start_idx, idx_slot)
        b.store(ir.Constant(i32, 0), probe_slot)
        b.branch(search_bb)
        
        b = ir.IRBuilder(search_bb)
        idx = b.load(idx_slot, name="i")
        offset = b.mul(idx, ir.Constant(i32, ENTRY_SIZE), name="off")
        offset64 = b.zext(offset, i64, name="off64")
        entry_ptr = b.gep(entries_base, [offset64], name="ep")
        occ_ptr = b.gep(entry_ptr, [ir.Constant(i32, 16)], name="occ_gep")
        occ_ptr = b.bitcast(occ_ptr, i32.as_pointer(), name="occ_ptr")
        occ = b.load(occ_ptr, name="occ")
        is_occupied = b.icmp_unsigned('==', occ, ir.Constant(i32, 1), name="is_occ")
        check_bb = fn.append_basic_block("check")
        is_empty = b.icmp_unsigned('==', occ, ir.Constant(i32, 0), name="is_empty")
        cont_probe_bb = fn.append_basic_block("cont_probe")
        b.cbranch(is_occupied, check_bb, cont_probe_bb)
        
        b = ir.IRBuilder(cont_probe_bb)
        b.cbranch(is_empty, not_found_bb, next_bb)
        
        b = ir.IRBuilder(check_bb)
        hash_ptr = b.gep(entry_ptr, [ir.Constant(i32, 20)], name="hash_gep")
        hash_ptr = b.bitcast(hash_ptr, i32.as_pointer(), name="hash_ptr")
        existing_hash = b.load(hash_ptr, name="ehash")
        hash_same = b.icmp_unsigned('==', existing_hash, h, name="hash_same")
        remove_hash_match_bb = fn.append_basic_block("hash_match")
        b.cbranch(hash_same, remove_hash_match_bb, next_bb)

        b = ir.IRBuilder(remove_hash_match_bb)
        key_ptr = b.bitcast(entry_ptr, i8p.as_pointer(), name="key_ptr")
        existing_key = b.load(key_ptr, name="ekey")
        cmp = b.call(self.strcmp_fn, [existing_key, key], name="scmp")
        is_same = b.icmp_signed('==', cmp, ir.Constant(i32, 0), name="same")
        b.cbranch(is_same, found_bb, next_bb)
        
        b = ir.IRBuilder(found_bb)
        # occupied = 2(tombstone), count--. Tombstone keeps later linear-probed keys reachable.
        occ_ptr3 = b.gep(entry_ptr, [ir.Constant(i32, 16)], name="occ_gep3")
        occ_ptr3 = b.bitcast(occ_ptr3, i32.as_pointer(), name="occ_ptr3")
        b.store(ir.Constant(i32, 2), occ_ptr3)
        cnt = b.load(cnt_ptr, name="cnt")
        cnt_new = b.sub(cnt, ir.Constant(i32, 1), name="cnt_dec")
        b.store(cnt_new, cnt_ptr)
        b.ret(ir.Constant(ir.IntType(1), 1))
        
        b = ir.IRBuilder(not_found_bb)
        b.ret(ir.Constant(ir.IntType(1), 0))
        
        b = ir.IRBuilder(next_bb)
        idx_next = b.add(idx, ir.Constant(i32, 1), name="inxt")
        idx_wrap = b.and_(idx_next, cap_mask, name="iwrap")
        b.store(idx_wrap, idx_slot)
        probe = b.load(probe_slot, name="probe")
        probe_next = b.add(probe, ir.Constant(i32, 1), name="probe_next")
        b.store(probe_next, probe_slot)
        is_full = b.icmp_unsigned('>=', probe_next, cap_val, name="hm_full")
        remove_continue_bb = fn.append_basic_block("remove_continue")
        b.cbranch(is_full, not_found_bb, remove_continue_bb)
        b = ir.IRBuilder(remove_continue_bb)
        b.branch(search_bb)
        
        # ===== _danha_hm_len(hm: i8*) → i32 =====
        fn = self.hm_len_fn
        entry_bb = fn.append_basic_block("entry")
        b = ir.IRBuilder(entry_bb)
        hm = fn.args[0]
        cnt_ptr = b.gep(hm, [ir.Constant(i32, 4)], name="cnt_gep")
        cnt_ptr = b.bitcast(cnt_ptr, i32.as_pointer(), name="cnt_ptr")
        cnt = b.load(cnt_ptr, name="cnt")
        b.ret(cnt)
    
    def _compile_hashmap_method(self, hm_ptr, method_name, args, line, hm_slot=None):
        """30: HashMap 인스턴스 메서드 컴파일.
        
        set(key, val): 키-값 저장. 값은 i64로 bitcast.
        get(key): 키로 값 조회. i64를 i32로 해석.
        get_i32/get_f64/get_str/get_bool(key): 저장값을 명시 타입으로 복원.
        has(key): 키 존재 여부 (i1).
        remove(key): 키 삭제 (i1).
        len(): 저장된 쌍 수 (i32).
        """
        if method_name == 'set':
            if len(args) != 2:
                raise DanhaValueError("set(key, value)에는 인자 2개가 필요해", line=line, source=self._source_code)
            key_val = self.compile_expr(args[0])
            raw_val = self.compile_expr(args[1])
            # 키: 문자열(i8*)이어야 함. 정수는 sprintf로 문자열 변환.
            if key_val.type == i32:
                key_val = self._int_to_str(key_val)
            # 값: i64로 변환
            val64 = self._to_i64(raw_val)
            updated_hm = self.builder.call(self.hm_set_fn, [hm_ptr, key_val, val64], name="hm_set_result")
            if hm_slot is not None:
                self.builder.store(updated_hm, hm_slot)
            return ir.Constant(i32, 0)
        
        if method_name == 'get' or method_name == 'get_i32':
            if len(args) != 1:
                raise DanhaValueError("get(key)에는 인자 1개가 필요해", line=line, source=self._source_code)
            key_val = self.compile_expr(args[0])
            if key_val.type == i32:
                key_val = self._int_to_str(key_val)
            result = self.builder.call(self.hm_get_fn, [hm_ptr, key_val], name="hm_val")
            # 기본 get은 기존 테스트와 호환되도록 i32로 복원한다.
            return self._from_i64(result)

        if method_name == 'get_f64':
            if len(args) != 1:
                raise DanhaValueError("get_f64(key)에는 인자 1개가 필요해", line=line, source=self._source_code)
            key_val = self.compile_expr(args[0])
            if key_val.type == i32:
                key_val = self._int_to_str(key_val)
            result = self.builder.call(self.hm_get_fn, [hm_ptr, key_val], name="hm_val_f64")
            return self.builder.bitcast(result, f64, name="hm_as_f64")

        if method_name == 'get_str':
            if len(args) != 1:
                raise DanhaValueError("get_str(key)에는 인자 1개가 필요해", line=line, source=self._source_code)
            key_val = self.compile_expr(args[0])
            if key_val.type == i32:
                key_val = self._int_to_str(key_val)
            result = self.builder.call(self.hm_get_fn, [hm_ptr, key_val], name="hm_val_str")
            return self.builder.inttoptr(result, i8p, name="hm_as_str")

        if method_name == 'get_bool':
            if len(args) != 1:
                raise DanhaValueError("get_bool(key)에는 인자 1개가 필요해", line=line, source=self._source_code)
            key_val = self.compile_expr(args[0])
            if key_val.type == i32:
                key_val = self._int_to_str(key_val)
            result = self.builder.call(self.hm_get_fn, [hm_ptr, key_val], name="hm_val_bool")
            return self.builder.trunc(result, i1, name="hm_as_bool")
        
        if method_name == 'has':
            if len(args) != 1:
                raise DanhaValueError("has(key)에는 인자 1개가 필요해", line=line, source=self._source_code)
            key_val = self.compile_expr(args[0])
            if key_val.type == i32:
                key_val = self._int_to_str(key_val)
            return self.builder.call(self.hm_has_fn, [hm_ptr, key_val], name="hm_has")
        
        if method_name == 'remove':
            if len(args) != 1:
                raise DanhaValueError("remove(key)에는 인자 1개가 필요해", line=line, source=self._source_code)
            key_val = self.compile_expr(args[0])
            if key_val.type == i32:
                key_val = self._int_to_str(key_val)
            return self.builder.call(self.hm_remove_fn, [hm_ptr, key_val], name="hm_rm")
        
        if method_name == 'len':
            return self.builder.call(self.hm_len_fn, [hm_ptr], name="hm_len")
        
        raise DanhaNameError(f"HashMap에 '{method_name}'이라는 메서드가 없어", line=line, source=self._source_code)
    
    def _to_i64(self, val):
        """값을 i64로 변환 (HashMap 저장용)."""
        if val.type == i64:
            return val
        if val.type == i32:
            return self.builder.sext(val, i64, name="to64")
        if val.type == f64:
            return self.builder.bitcast(val, i64, name="f2i64")
        if val.type == ir.IntType(1):
            return self.builder.zext(val, i64, name="b2i64")
        if isinstance(val.type, ir.PointerType):
            return self.builder.ptrtoint(val, i64, name="p2i64")
        return self.builder.bitcast(val, i64, name="to64")
    
    def _from_i64(self, val):
        """i64를 원래 타입으로 복원. 기본적으로 i32로 (정수 값이 대부분)."""
        return self.builder.trunc(val, i32, name="from64")
    
    def _int_to_str(self, int_val):
        """i32를 문자열로 변환 (sprintf)."""
        buf = self.builder.alloca(ir.ArrayType(i8, 20), name="itoa_buf")
        buf_ptr = self.builder.bitcast(buf, i8p, name="buf_ptr")
        fmt = self._make_global_string("fmt_itoa", "%d\0")
        self.builder.call(self.sprintf_fn, [buf_ptr, fmt, int_val])
        return buf_ptr

    def _define_component_helpers(self):
        """각 컴포넌트의 _danha_comp_<N>_remove_by_entity 본문을 채운다.
        
        swap-remove 로직:
          dense = sparse[e]
          if dense == -1: return false
          count--
          last = dense_to_entity[count]
          if dense != count (자기가 마지막이 아니면):
            # 마지막 원소의 데이터를 빈자리로 복사
            for each field: field[dense] = field[count]
            dense_to_entity[dense] = last
            sparse[last] = dense
          sparse[e] = -1
          return true
        """
        for comp_name, info in self.components.items():
            fn = info['remove_fn']
            entry = fn.append_basic_block(name="entry")
            b = ir.IRBuilder(entry)
            
            e_idx = fn.args[0]
            
            # dense = sparse[e]
            sparse_ptr = b.load(info['sparse'])
            sparse_slot = b.gep(sparse_ptr, [e_idx], inbounds=True)
            dense = b.load(sparse_slot, name="dense")
            
            # if dense == -1: return false
            absent = b.icmp_signed('==', dense, ir.Constant(i32, -1))
            absent_bb = fn.append_basic_block(name="absent")
            present_bb = fn.append_basic_block(name="present")
            b.cbranch(absent, absent_bb, present_bb)
            
            b.position_at_end(absent_bb)
            b.ret(ir.Constant(ir.IntType(1), 0))
            
            b.position_at_end(present_bb)
            # count--
            count_old = b.load(info['count'])
            count_new = b.sub(count_old, ir.Constant(i32, 1))
            b.store(count_new, info['count'])
            
            # 이 원소가 마지막이었나?
            is_last = b.icmp_signed('==', dense, count_new)
            swap_bb = fn.append_basic_block(name="swap")
            skip_swap_bb = fn.append_basic_block(name="skip_swap")
            b.cbranch(is_last, skip_swap_bb, swap_bb)
            
            # swap: 마지막 원소를 빈자리로
            b.position_at_end(swap_bb)
            dte = b.load(info['dense_to_entity'])
            last_entity_slot = b.gep(dte, [count_new], inbounds=True)
            last_entity = b.load(last_entity_slot, name="last_entity")
            
            # 각 필드: field[dense] = field[count]
            for fname in info['fields']:
                farr = b.load(info['field_globals'][fname])
                src = b.gep(farr, [count_new], inbounds=True)
                dst = b.gep(farr, [dense], inbounds=True)
                b.store(b.load(src), dst)
            
            # dense_to_entity[dense] = last_entity
            b.store(last_entity, b.gep(dte, [dense], inbounds=True))
            # sparse[last_entity] = dense
            b.store(dense, b.gep(sparse_ptr, [last_entity], inbounds=True))
            b.branch(skip_swap_bb)
            
            # skip_swap: 둘 다 오는 합류 지점
            b.position_at_end(skip_swap_bb)
            # sparse[e] = -1
            b.store(ir.Constant(i32, -1), sparse_slot)
            b.ret(ir.Constant(ir.IntType(1), 1))


    # ============================================================
    # 7.15d: 최상위 const를 LLVM 글로벌 상수로 사전 등록
    # ============================================================
    def _predeclare_top_level_consts(self, statements):
        """system/함수 본문 컴파일 전에 const 이름을 이미 보이게 만듦.
        우변이 컴파일 타임에 평가 가능한 값이어야 성공.
        평가 불가면 조용히 스킵 — 나중에 main 컴파일 시점에 로컬 const로 처리됨.
        
        20c: 함수 정의도 _comptime_fns에 등록해서 comptime에서 호출 가능하게.
        """
        # 20c: 함수 AST를 comptime용으로 등록 (인터프리터 evaluate가 참조할 수 있도록)
        for stmt in statements:
            if stmt[0] == 'FnDef':
                fn_name = stmt[1]
                # 인터프리터가 FnDef 노드를 함수 정의로 인식하도록 그대로 저장
                self._comptime_fns[fn_name] = stmt
        
        for stmt in statements:
            if stmt[0] != 'ConstDef':
                continue
            name = stmt[1]
            expr = stmt[2]
            line = stmt[-1]
            result = self._eval_const_expr(expr)
            if result is None:
                # 복잡한 식 — 나중에 main에서 로컬 const로 처리
                continue
            ty, py_val = result
            llvm_const = ir.Constant(ty, py_val)
            gvar = ir.GlobalVariable(self.module, ty, name=f"_danha_const_{name}")
            gvar.linkage = "internal"
            gvar.global_constant = True
            gvar.initializer = llvm_const
            self._globals[name] = gvar
            self._const_vars.add(name)
            # 20c: comptime에서 참조 가능하도록 파이썬 값도 등록
            if isinstance(py_val, (int, float)):
                self._comptime_consts[name] = py_val
    
    def _names_referenced_in_subbodies(self, statements):
        """fn/method/system/macro 본문에서 참조되는 이름의 set.

        탑레벨 변수가 main 외 본문에서 쓰이는지 식별용. 여기 있는 이름은
        시스템/함수에서 보여야 하므로 글로벌이 필요. 없는 이름은 main local
        alloca로 떨어뜨려 LLVM mem2reg가 register로 promote → 핫 루프 perf 큼.
        """
        refs = set()

        def walk(n):
            if isinstance(n, tuple):
                if len(n) >= 2 and n[0] == 'Name' and isinstance(n[1], str):
                    refs.add(n[1])
                for c in n[1:]:
                    walk(c)
            elif isinstance(n, list):
                for item in n:
                    walk(item)

        for stmt in statements:
            if not isinstance(stmt, tuple) or not stmt:
                continue
            k = stmt[0]
            if k == 'FnDef':
                # (FnDef, name, params, body, ptypes, rettype, tparams, line)
                if len(stmt) > 3:
                    walk(stmt[3])
            elif k in ('UnsafeFn', 'ExportFn'):
                inner = stmt[1]
                if isinstance(inner, tuple) and inner and inner[0] == 'FnDef' and len(inner) > 3:
                    walk(inner[3])
            elif k == 'Impl':
                # (Impl, type_name, methods, line) — methods는 FnDef 리스트
                methods = stmt[2] if len(stmt) > 2 else []
                if isinstance(methods, list):
                    for m in methods:
                        if isinstance(m, tuple) and m and m[0] == 'FnDef' and len(m) > 3:
                            walk(m[3])
            elif k == 'ImplTrait':
                # (ImplTrait, trait_name, type_name, methods, line)
                methods = stmt[3] if len(stmt) > 3 else []
                if isinstance(methods, list):
                    for m in methods:
                        if isinstance(m, tuple) and m and m[0] == 'FnDef' and len(m) > 3:
                            walk(m[3])
            elif k == 'SystemDef':
                # (SystemDef, name, params, ptypes, bindings, body, is_parallel, line)
                if len(stmt) > 5:
                    walk(stmt[5])
            elif k == 'MacroDef':
                # (MacroDef, name, params, body, is_variadic, line)
                if len(stmt) > 3:
                    walk(stmt[3])
        return refs

    def _predeclare_top_level_vars(self, statements):
        """7.16b: 최상위 Assign 변수를 LLVM 글로벌 변수로 사전 등록.

        system/함수 본문 컴파일 시점에 최상위 변수가 보여야 하는 경우가 있다.
        예: 퐁 게임에서 패들 좌표를 전역에 저장하고 system에서 참조.

        우변이 단순 리터럴(정수/실수)인 Assign만 처리한다.
        우변이 복잡한 식(함수 호출, 구조체 리터럴 등)이면 스킵 —
        main 컴파일 때 로컬 변수로 만들어지고, system에서는 못 봄.

        이미 const로 등록된 이름은 건드리지 않는다.

        Stage 78: fn/system 본문에서 참조되지 않는 탑레벨 변수는 글로벌 등록을
        건너뜀. main 컴파일이 그 이름을 보면 새 alloca를 만들고 LLVM mem2reg가
        register로 promote → 핫 루프(예: xorshift) 메모리 왕복 제거. 글로벌 등록
        조건이 좁아지는 것일 뿐, 시맨틱은 그대로(외부 참조 있으면 글로벌).
        """
        fn_refs = self._names_referenced_in_subbodies(statements)

        for stmt in statements:
            if stmt[0] != 'Assign':
                continue
            name = stmt[1]
            expr = stmt[2]
            var_type = stmt[3] if len(stmt) > 4 else None
            line = stmt[-1]

            # 이미 const나 다른 글로벌로 등록된 이름은 스킵
            if name in self._globals or name in self._const_vars:
                continue

            # Stage 78: 사용자 fn/system에서 안 쓰이면 글로벌 안 만듦 → main local로.
            if name not in fn_refs:
                continue

            # L-3 (2026-05-19 ext): StructInstance 글로벌 등록 (scalar 필드만).
            if isinstance(expr, tuple) and expr[0] == 'StructInstance':
                type_name = expr[1]
                field_values = expr[2]   # dict {field_name: value_expr}
                if type_name in self.structs:
                    struct_info = self.structs[type_name]
                    llvm_struct_ty = struct_info[0]
                    field_names = struct_info[1]
                    field_types = struct_info[2]
                    consts = []
                    ok = True
                    for fname, fty in zip(field_names, field_types):
                        if fname not in field_values:
                            # 기본 0
                            if isinstance(fty, ir.IntType):
                                consts.append(ir.Constant(fty, 0))
                            elif fty == f64 or fty == f32:
                                consts.append(ir.Constant(fty, 0.0))
                            else:
                                ok = False
                                break
                            continue
                        ce = self._eval_const_expr(field_values[fname])
                        if ce is None:
                            ok = False
                            break
                        _, ev = ce
                        if isinstance(fty, ir.IntType) and isinstance(ev, float):
                            ev = int(ev)
                        elif fty == f64 and isinstance(ev, int):
                            ev = float(ev)
                        consts.append(ir.Constant(fty, ev))
                    if ok and len(consts) == len(field_names):
                        gvar = ir.GlobalVariable(self.module, llvm_struct_ty, name=f"_danha_var_{name}")
                        gvar.linkage = "internal"
                        gvar.global_constant = False
                        gvar.initializer = ir.Constant.literal_struct(consts)
                        self._globals[name] = gvar
                        self._global_var_names = getattr(self, '_global_var_names', set())
                        self._global_var_names.add(name)
                        continue

            # L-3 (2026-05-19 ext2): Dynarray 빈 리스트 글로벌 — `g_list: [Item] = []`
            if isinstance(expr, tuple) and expr[0] == 'List' and len(expr[1]) == 0 \
                    and var_type is not None and isinstance(var_type, tuple) \
                    and var_type[0] == 'DynArrayType':
                elem_ty_node = var_type[1]
                if isinstance(elem_ty_node, tuple) and elem_ty_node[0] == 'TypeName':
                    elem_name = elem_ty_node[1]
                    if elem_name in self._TYPE_MAP:
                        elem_ty = self._TYPE_MAP[elem_name]
                    elif elem_name in self.structs:
                        elem_ty = self.structs[elem_name][0]
                    else:
                        elem_ty = None
                    if elem_ty is not None:
                        dyn_ty = self._get_dynarray_type(elem_ty)
                        gvar = ir.GlobalVariable(self.module, dyn_ty, name=f"_danha_var_{name}")
                        gvar.linkage = "internal"
                        gvar.global_constant = False
                        # zero-init {null ptr, 0 len, 0 cap} — llvmlite의 ir.Constant(ty, None) = zeroinitializer
                        gvar.initializer = ir.Constant(dyn_ty, None)
                        self._globals[name] = gvar
                        self._global_var_names = getattr(self, '_global_var_names', set())
                        self._global_var_names.add(name)
                        continue

            # L-3 (2026-05-19): Fixed array `[val; N]` 또는 `[v1, v2, ...]` 글로벌 등록.
            if isinstance(expr, tuple) and expr[0] == 'List' \
                    and var_type is not None and isinstance(var_type, tuple) \
                    and var_type[0] == 'ArrayType':
                elem_ty_node = var_type[1]
                length = var_type[2]
                if isinstance(elem_ty_node, tuple) and elem_ty_node[0] == 'TypeName' \
                        and elem_ty_node[1] in self._TYPE_MAP:
                    elem_ty = self._TYPE_MAP[elem_ty_node[1]]
                    elems = expr[1]
                    const_elems = []
                    ok = True
                    for el in elems:
                        ce = self._eval_const_expr(el)
                        if ce is None:
                            ok = False
                            break
                        _, ev = ce
                        if elem_ty == f64 and isinstance(ev, int):
                            ev = float(ev)
                        elif elem_ty in (i8, i16, i32, i64) and isinstance(ev, float):
                            ev = int(ev)
                        const_elems.append(ir.Constant(elem_ty, ev))
                    if ok and len(const_elems) == length:
                        arr_ty = ir.ArrayType(elem_ty, length)
                        gvar = ir.GlobalVariable(self.module, arr_ty, name=f"_danha_var_{name}")
                        gvar.linkage = "internal"
                        gvar.global_constant = False
                        gvar.initializer = ir.Constant.literal_array(const_elems)
                        self._globals[name] = gvar
                        self._global_var_names = getattr(self, '_global_var_names', set())
                        self._global_var_names.add(name)
                        continue

            if isinstance(expr, tuple) and expr[0] == 'String':
                text = expr[1]
                sglob = self._make_global_string(f"_danha_str_init_{name}", text + "\0")
                gvar = ir.GlobalVariable(self.module, i8p, name=f"_danha_var_{name}")
                gvar.linkage = "internal"
                gvar.global_constant = False
                gvar.initializer = sglob.bitcast(i8p)
                self._globals[name] = gvar
                self._global_var_names = getattr(self, '_global_var_names', set())
                self._global_var_names.add(name)
                continue

            # 우변이 단순 리터럴인지 확인
            result = self._eval_const_expr(expr)
            if result is None:
                continue

            ty, py_val = result
            # P1B: 어노테이션이 있으면 타입을 그것으로 (값 적당히 캐스트)
            if var_type is not None and isinstance(var_type, tuple) and var_type[0] == 'TypeName':
                tname = var_type[1]
                if tname in self._TYPE_MAP:
                    ann_ty = self._TYPE_MAP[tname]
                    if ann_ty != ty:
                        # 정수 → 정수/실수 캐스트, 실수 → 정수도 허용
                        if isinstance(py_val, int) and ann_ty in (ir.IntType(8), ir.IntType(16), ir.IntType(32), ir.IntType(64)):
                            ty = ann_ty
                        elif isinstance(py_val, int) and ann_ty in (f32, f64):
                            ty = ann_ty
                            py_val = float(py_val)
                        elif isinstance(py_val, float) and ann_ty in (f32, f64):
                            ty = ann_ty
            gvar = ir.GlobalVariable(self.module, ty, name=f"_danha_var_{name}")
            gvar.linkage = "internal"
            gvar.global_constant = False  # 변수니까 수정 가능
            gvar.initializer = ir.Constant(ty, py_val)
            self._globals[name] = gvar
            # _global_var_names에 기록 — main에서 이 변수를 다시 alloca 안 하도록
            self._global_var_names = getattr(self, '_global_var_names', set())
            self._global_var_names.add(name)
    
    def _eval_const_expr(self, node):
        """상수식을 컴파일 타임에 평가. 성공하면 (LLVM 타입, 파이썬 값) 튜플, 실패하면 None.
        지원: 숫자 리터럴, enum variant 접근, 두 상수식의 산술.
        지원 안 함: 변수/함수 호출/구조체 리터럴 등.
        """
        if not isinstance(node, tuple) or len(node) == 0:
            return None
        t = node[0]
        
        if t == 'Number':
            v = node[1]
            if isinstance(v, bool):
                return None  # 불리언은 별도 처리 필요 — 현 범위 밖
            if isinstance(v, int):
                return (i32, v)
            if isinstance(v, float):
                return (f64, v)
            return None
        
        # enum variant: ('FieldAccess', ('Name', 'Phase'), 'Patrol', line)
        if t == 'FieldAccess':
            obj = node[1]
            field = node[2]
            if obj[0] == 'Name' and obj[1] in self.enums:
                variants = self.enums[obj[1]]
                if field in variants:
                    return (i32, variants[field])
            return None
        
        # 단항 마이너스: ('Neg', inner, line)
        if t == 'Neg':
            inner = self._eval_const_expr(node[1])
            if inner is None:
                return None
            ty, v = inner
            return (ty, -v)
        
        # 단순 산술: ('Add', l, r, line) 등
        if t in ('Add', 'Sub', 'Mul', 'Div'):
            lhs = self._eval_const_expr(node[1])
            rhs = self._eval_const_expr(node[2])
            if lhs is None or rhs is None:
                return None
            lt, lv = lhs
            rt, rv = rhs
            # 하나라도 f64면 결과 f64
            if lt == f64 or rt == f64:
                result_ty = f64
                lv = float(lv)
                rv = float(rv)
            else:
                result_ty = i32
            if t == 'Add':
                return (result_ty, lv + rv)
            if t == 'Sub':
                return (result_ty, lv - rv)
            if t == 'Mul':
                return (result_ty, lv * rv)
            if t == 'Div':
                if rv == 0:
                    return None
                if result_ty == i32:
                    return (result_ty, lv // rv)
                return (result_ty, lv / rv)
        
        # 20c: comptime 블록 — 인터프리터로 평가
        if t == 'Comptime':
            from danha_evaluator import evaluate
            comptime_scope = self._make_comptime_scope()
            try:
                result = evaluate(node[1], comptime_scope)
            except Exception:
                return None  # 평가 실패 → 나중에 main에서 처리
            # 결과를 (LLVM 타입, 파이썬 값)으로 변환
            if isinstance(result, bool):
                return (i1, 1 if result else 0)
            if isinstance(result, int):
                self._comptime_consts[f"__comptime_tmp_{id(node)}"] = result  # 임시는 넣지 말자
                return (i32, result)
            if isinstance(result, float):
                return (f64, result)
            # 배열이나 문자열 등 복잡한 값은 글로벌 사전 등록 불가 → None
            return None
        
        # Bool 리터럴 — const FLAG = true 같은 경우
        if t == 'Bool':
            return (i1, 1 if node[1] else 0)
        
        # Name — 이미 등록된 const를 참조하는 경우
        if t == 'Name':
            name = node[1]
            if name in self._comptime_consts:
                val = self._comptime_consts[name]
                if isinstance(val, int) and not isinstance(val, bool):
                    return (i32, val)
                if isinstance(val, float):
                    return (f64, val)
            # enum variant 이름일 수도 있음
            for enum_name, variants in self.enums.items():
                if name in variants:
                    return (i32, variants[name])
            return None
        
        return None


    def compile_program(self, node):
        """('Program', [statements...], line)
        
        두 단계 통과:
        1. 사용자 함수의 시그니처(ir.Function)만 먼저 등록.
           재귀와 상호 재귀를 위해서. 본문에서 자기 자신/다른 함수 이름을 보려면
           그 시점에 이름이 이미 모듈에 있어야 함.
        2. 사용자 함수 본문 컴파일.
        3. main 함수 본문 컴파일 (= 함수 정의가 아닌 모든 최상위 문장).
        """
        statements = node[1]
        
        # 9.1c: import 해결 — 모든 다른 패스보다 먼저.
        # Import/FromImport를 처리하여 모듈의 정의를 현재 statements에 합침.
        statements = self._resolve_imports(statements)
        
        # 35단계: @attribute 평탄화 — Attributed 노드를 벗겨서 내부 선언으로 바꾸고,
        # attribute 메타데이터를 별도로 저장한다. 이래야 아래의 각 패스에서
        # StructDef, FnDef 등을 정상적으로 인식할 수 있다.
        self._comp_attributes = {}  # {name: [(attr_name, {args})]}
        unwrapped = []
        for stmt in statements:
            if stmt[0] == 'Attributed':
                attrs = stmt[1]  # [('Attribute', name, args, line), ...]
                inner = stmt[2]
                # 중첩 Attributed 처리
                while inner[0] == 'Attributed':
                    attrs = attrs + inner[1]
                    inner = inner[2]
                # 메타데이터 저장
                target_name = inner[1] if len(inner) > 1 else None
                if target_name:
                    attr_list = []
                    for attr in attrs:
                        attr_name = attr[1]
                        attr_args = {}
                        for arg in attr[2]:
                            if arg[0] == 'KeyVal':
                                attr_args[arg[1]] = arg[2]
                            elif arg[0] in ('StringArg', 'NumArg', 'NameArg'):
                                attr_args[len(attr_args)] = arg[1]
                        attr_list.append((attr_name, attr_args))
                    self._comp_attributes[target_name] = attr_list
                unwrapped.append(inner)
            else:
                unwrapped.append(stmt)
        statements = unwrapped

        # 49단계: DocAnnotated 평탄화 — doc text 제거, 내부 선언만 남김
        unwrapped2 = []
        for stmt in statements:
            if stmt[0] == 'DocAnnotated':
                unwrapped2.append(stmt[2])
            else:
                unwrapped2.append(stmt)
        statements = unwrapped2

        # 1단계: 타입 등록 (struct) → 그 다음 함수 시그니처 등록.
        # struct 먼저 해야 함수 시그니처에 struct 타입을 쓸 수 있음 (장기적으로).
        # 6.10에선 함수가 struct를 매개변수/반환으로 쓰진 않지만, 순서를 미리 옳게 박음.
        for stmt in statements:
            if stmt[0] == 'StructDef':
                self._declare_struct(stmt)
            elif stmt[0] == 'UnionDef':
                self._declare_union(stmt)

        # 7.12d2: 컴포넌트 선언 등록 (글로벌 변수 자리 잡기).
        # struct와 마찬가지로 함수 시그니처에 앞서야 함.
        # 이게 _define_ecs_runtime 앞에 와야 함 — init에서 컴포넌트별 배열도 확보하므로.
        for stmt in statements:
            if stmt[0] == 'ComponentDef':
                self._declare_component(stmt)
        
        # 7.12d1: ECS 런타임 함수 (_danha_ecs_spawn/destroy/is_alive/init) 본문 생성.
        # d2에서 init은 각 컴포넌트의 저장소도 초기화하도록 확장됨.
        # d2-iii에서는 destroy가 "엔티티 제거 전에 모든 컴포넌트 저장소에서 먼저 정리"를
        # 하도록 확장됨. 그래서 component 헬퍼 선언은 이미 끝나 있어야 함.
        self._define_component_helpers()
        self._define_ecs_runtime()
        self._define_hashmap_runtime()
        
        # 7.16a/7.17c: extern fn 선언 등록 (C-FFI).
        # CLinkedFn은 ExternFn과 동일하게 처리하되, 라이브러리 이름을 clink_libs에 수집.
        for stmt in statements:
            if stmt[0] == 'ExternFn':
                self._declare_extern_function(stmt)
            elif stmt[0] == 'CLinkedFn':
                lib_name = stmt[1]
                if lib_name not in self.clink_libs:
                    self.clink_libs.append(lib_name)
                self._declare_extern_function(stmt[2])
        
        # 23a: enum 등록을 함수 시그니처보다 먼저 — 함수 매개변수/반환에 enum 타입 사용 가능.
        # 7.15b / 8.3: enum 등록
        for stmt in statements:
            if stmt[0] == 'EnumDef':
                enum_name = stmt[1]
                variants = stmt[2]  # [(이름, 타입노드리스트|None), ...]
                enum_tparams = stmt[3]  # 8.6: 타입 매개변수 리스트
                
                # 8.6: 제네릭 enum은 AST만 저장
                if enum_tparams:
                    if not hasattr(self, '_generic_enums'):
                        self._generic_enums = {}
                    self._generic_enums[enum_name] = stmt
                    continue
                
                has_data = any(vtypes is not None for _, vtypes in variants)
                
                if not has_data:
                    # 기존 단순 enum — variant → i32 매핑
                    self.enums[enum_name] = {vname: i for i, (vname, _) in enumerate(variants)}
                else:
                    # tagged union: LLVM 타입 생성
                    # 각 variant의 payload 크기를 계산, 최대 크기로 i8 배열
                    variant_info = {}
                    max_payload_size = 0
                    for i, (vname, vtypes) in enumerate(variants):
                        if vtypes is not None:
                            llvm_types = [self._resolve_type(t, f"enum {enum_name} variant {vname}", 0)[0] for t in vtypes]
                            # payload 크기 계산 (대략적: 각 타입의 바이트 수 합)
                            payload_size = 0
                            for lt in llvm_types:
                                if lt == i32:
                                    payload_size += 4
                                elif lt == f64:
                                    payload_size += 8
                                elif lt == i8p:
                                    payload_size += 8  # 포인터
                                else:
                                    payload_size += 8  # 기타
                            variant_info[vname] = (i, llvm_types)
                            if payload_size > max_payload_size:
                                max_payload_size = payload_size
                        else:
                            variant_info[vname] = (i, None)
                    
                    # Stage 75a: 동일 primitive 단일-binding variants는 typed repr {i32, T} 사용.
                    # match payload extraction이 GEP+bitcast+load → extract_value SSA 경로로.
                    uniform_ty = self._detect_uniform_payload(variant_info)
                    if uniform_ty is not None:
                        tagged_ty = self.module.context.get_identified_type(f"tagged.{enum_name}")
                        if not tagged_ty.elements:
                            tagged_ty.set_body(i32, uniform_ty)
                    else:
                        if max_payload_size == 0:
                            max_payload_size = 1  # 최소 1바이트
                        payload_array_ty = ir.ArrayType(ir.IntType(8), max_payload_size)
                        tagged_ty = self.module.context.get_identified_type(f"tagged.{enum_name}")
                        if not tagged_ty.elements:
                            tagged_ty.set_body(i32, payload_array_ty)

                    self.tagged_enums[enum_name] = (tagged_ty, variant_info, max_payload_size)
        
        # 7.8b: TraitDef는 메타데이터만 저장 (LLVM 코드 생성 없음).
        # FnDef 선언보다 먼저 해야 함 — fn(a: dyn Trait) 같은 dyn 타입 매개변수에서
        # _resolve_type이 dyn 타입을 찾을 수 있어야 하므로.
        self._traits = getattr(self, '_traits', {})
        for stmt in statements:
            if stmt[0] == 'TraitDef':
                trait_name = stmt[1]
                trait_methods = stmt[2]  # [FnDef nodes...]
                self._traits[trait_name] = trait_methods
        
        # 29: trait별 dyn 타입 사전 생성 — FnDef 선언 전에 필요
        self._dyn_types = {}
        self._dyn_type_to_trait = {}
        for trait_name in self._traits:
            dyn_ty = self.module.context.get_identified_type(f"dyn.{trait_name}")
            dyn_ty.set_body(i8.as_pointer(), i8.as_pointer())
            self._dyn_types[trait_name] = dyn_ty
            self._dyn_type_to_trait[dyn_ty] = trait_name
        
        for stmt in statements:
            if stmt[0] == 'FnDef':
                self._declare_function(stmt)
            # 21c: unsafe fn도 FnDef와 동일하게 선언
            elif stmt[0] == 'UnsafeFn':
                fn_node = stmt[1]
                self._declare_function(fn_node)
                if not hasattr(self, '_unsafe_fns'):
                    self._unsafe_fns = set()
                self._unsafe_fns.add(fn_node[1])
            # 45단계: export fn은 FnDef와 동일하게 선언 (external linkage는 기본값)
            elif stmt[0] == 'ExportFn':
                self._declare_function(stmt[1])
        
        # 7.13d: system 시그니처 등록 (일반 함수처럼 ir.Function으로).
        for stmt in statements:
            if stmt[0] == 'SystemDef':
                self._declare_system(stmt)
        
        # impl 블록의 메서드 시그니처도 등록 (struct가 다 등록된 뒤).
        for stmt in statements:
            if stmt[0] == 'Impl':
                self._declare_impl(stmt)
        
        # 7.8b: ImplTrait — 트레잇 메서드를 타입에 연결 (Impl과 동일 경로).
        for stmt in statements:
            if stmt[0] == 'ImplTrait':
                self._declare_impl_trait(stmt)
        
        # 29단계: vtable 생성 — 모든 ImplTrait에 대해
        # trait별 메서드 순서를 고정하고, 각 type에 대한 함수 포인터 테이블을 만든다.
        self._build_vtables(statements)
        
        # 7.15d: 최상위 const를 LLVM 글로벌 상수로 사전 등록.
        # 이래야 system/함수 본문 컴파일 시점에 const 이름이 이미 보임.
        # 지원하는 우변: 숫자 리터럴, enum variant 접근, 단순 산술(리터럴끼리).
        # 그 외 복잡한 const는 main 컴파일 시점에 로컬 const로 처리됨.
        self._predeclare_top_level_consts(statements)
        
        # 22c: 매크로 정의 사전 수집 — main/함수 본문에서 매크로 호출 시 참조 가능하게
        for stmt in statements:
            if stmt[0] == 'MacroDef':
                name = stmt[1]
                params = stmt[2]
                body = stmt[3]
                is_variadic = stmt[4]
                self._macros[name] = ('Macro', params, body, is_variadic)
        
        # 7.16b: 최상위 일반 변수를 LLVM 글로벌로 사전 등록.
        # system/함수 본문에서 최상위 변수(예: p1_x, running)를 참조할 수 있게 하려면
        # main의 로컬 alloca가 아니라 모듈 수준 글로벌이어야 한다.
        # 우변이 단순 리터럴(정수/실수/bool)인 경우만 처리.
        # 복잡한 우변(함수 호출 등)은 main에서 alloca → store 후 글로벌에 store.
        self._predeclare_top_level_vars(statements)
        
        # 2단계: FnDef 본문 + 메서드 본문 + System 본문 컴파일
        for stmt in statements:
            if stmt[0] == 'FnDef':
                # 8.5: 제네릭 함수는 호출 시점에 단형화
                if stmt[6]:  # type_params
                    continue
                self._compile_function_body(stmt)
            # 21c: unsafe fn 본문도 FnDef와 동일하게 컴파일
            elif stmt[0] == 'UnsafeFn':
                fn_node = stmt[1]
                if fn_node[6]:  # type_params
                    continue
                self._compile_function_body(fn_node)
            # 45단계: export fn 본문도 FnDef와 동일하게 컴파일
            elif stmt[0] == 'ExportFn':
                fn_node = stmt[1]
                if fn_node[6]:  # type_params
                    continue
                self._compile_function_body(fn_node)
        for stmt in statements:
            if stmt[0] == 'Impl':
                self._compile_impl_bodies(stmt)
        for stmt in statements:
            if stmt[0] == 'ImplTrait':
                self._compile_impl_trait_bodies(stmt)
        for stmt in statements:
            if stmt[0] == 'SystemDef':
                self._compile_system_body(stmt)
        
        # 3단계: main 만들고 (정의가 아닌) 최상위 문장 컴파일
        if self.entry_arg_mode:
            main_ty = ir.FunctionType(i32, [i32, i8p.as_pointer()])
        else:
            main_ty = ir.FunctionType(i32, [])
        main_fn = ir.Function(self.module, main_ty, name="main")
        if self.entry_arg_mode:
            main_fn.args[0].name = "argc"
            main_fn.args[1].name = "argv"
            self.main_argc = main_fn.args[0]
            self.main_argv = main_fn.args[1]
        entry_block = main_fn.append_basic_block(name="entry")
        
        self.current_fn = main_fn
        self.builder = ir.IRBuilder(entry_block)
        if self.entry_arg_mode and self.argc_global is not None and self.argv_global is not None:
            self.builder.store(self.main_argc, self.argc_global)
            self.builder.store(self.main_argv, self.argv_global)
        
        self.vars = [{}]
        # 7.15d: main 안에서 일어나는 최상위 Assign은 글로벌 변수로 승격.
        self._in_main = True
        
        # ----- 7.5: 기본 아레나 초기화 -----
        # 글로벌 아레나에 256MB 첫 청크를 malloc으로 확보.
        # 게임 엔진/벤치마크 워크로드(파티클 1M개 = 32MB, 누적 grow는 더 큼)를 위해 넉넉히.
        # 프로그램 실행 동안 모든 동적 배열이 이 아레나에서 할당.
        # 초과 시에는 _danha_arena_alloc이 새 청크를 잡아 성장한다 (조용한 손상 없음).
        i64 = ir.IntType(64)
        arena_cap = 256 * 1024 * 1024  # 256MB
        
        arena_mem = self.builder.call(
            self.malloc_fn,
            [ir.Constant(i64, arena_cap)],
            name="_arena_mem"
        )
        self.builder.store(
            arena_mem,
            self.builder.gep(self.arena_slot,
                             [ir.Constant(i32, 0), ir.Constant(i32, 0)],
                             inbounds=True, name="_arena_base_ptr")
        )
        self.builder.store(
            ir.Constant(i32, 0),
            self.builder.gep(self.arena_slot,
                             [ir.Constant(i32, 0), ir.Constant(i32, 1)],
                             inbounds=True, name="_arena_off_ptr")
        )
        self.builder.store(
            ir.Constant(i32, arena_cap),
            self.builder.gep(self.arena_slot,
                             [ir.Constant(i32, 0), ir.Constant(i32, 2)],
                             inbounds=True, name="_arena_cap_ptr")
        )
        
        # ----- 7.12d1: ECS World 초기화 -----
        # 아레나가 준비된 후 호출. 지금은 ECS가 자체 malloc을 쓰지만,
        # 향후 아레나로 옮길 수 있도록 이 순서를 유지.
        self.builder.call(self.ecs_init_fn, [])
        
        for stmt in statements:
            if stmt[0] not in ('FnDef', 'StructDef', 'UnionDef', 'Impl', 'ComponentDef', 'SystemDef', 'TraitDef', 'ImplTrait', 'EnumDef', 'ExternFn', 'CLinkedFn', 'Import', 'FromImport', 'UnsafeFn', 'MacroDef', 'Attributed', 'TestBlock', 'DocAnnotated'):
                self.compile_stmt(stmt)
        
        # ----- 7.5: main 끝에서 아레나 메모리 해제 -----
        if not self.builder.block.is_terminated:
            # free(arena.base)
            arena_base = self.builder.load(
                self.builder.gep(self.arena_slot,
                                 [ir.Constant(i32, 0), ir.Constant(i32, 0)],
                                 inbounds=True, name="_arena_base"),
                name="_arena_base_val"
            )
            self.builder.call(self.free_fn, [arena_base])
            self.builder.ret(ir.Constant(i32, 0))
    
    # ===== 9.1c: 모듈 시스템 (컴파일러) =====
    
    _MODULE_CACHE_COMPILE = {}   # 클래스 변수: 모듈 캐시
    _MODULE_LOADING_COMPILE = set()  # 순환 감지
    _MODULE_BASE_DIR_COMPILE = None  # 모듈 파일 검색 기본 디렉토리
    
    def _resolve_imports(self, statements):
        """
        Import/FromImport 문을 처리하여 모듈의 정의를 현재 프로그램에 통합.
        
        전략: 모듈의 AST에서 정의(fn, struct, enum, const)를 추출하고,
        이름에 '_mod_모듈명_' 접두사를 붙여 현재 statements 앞에 삽입.
        _modules 딕셔너리에 원래이름→접두사이름 매핑을 저장.
        
        반환: 수정된 statements 리스트 (모듈 정의가 앞에 삽입됨).
        """
        import os
        
        new_statements = []
        
        for stmt in statements:
            if stmt[0] == 'Import':
                module_name = stmt[1]
                line = stmt[-1]
                mod_stmts = self._load_module_ast(module_name, line)
                prefix = f"_mod_{module_name.replace('.', '_')}_"
                
                # L-3 1차: 모듈의 글로벌 Assign target 이름들 수집
                globals_in_mod = set()
                for ms in mod_stmts:
                    if ms[0] == 'Assign' and len(ms) >= 5 and isinstance(ms[1], str):
                        globals_in_mod.add(ms[1])
                # L-3 2차: 정의를 접두사 이름으로 등록 + fn body 글로벌 참조 변환
                name_map = {}
                for ms in mod_stmts:
                    renamed = self._rename_module_def(ms, prefix, name_map, globals_in_mod)
                    if renamed is not None:
                        new_statements.append(renamed)
                
                self._modules[module_name.split('.')[-1]] = name_map
                
            elif stmt[0] == 'FromImport':
                module_name = stmt[1]
                names = stmt[2]  # ['sin', 'cos'] 또는 '*'
                line = stmt[-1]
                mod_stmts = self._load_module_ast(module_name, line)
                prefix = f"_mod_{module_name.replace('.', '_')}_"
                
                # L-3 1차: 모듈의 글로벌 Assign target 이름들 수집
                globals_in_mod = set()
                for ms in mod_stmts:
                    if ms[0] == 'Assign' and len(ms) >= 5 and isinstance(ms[1], str):
                        globals_in_mod.add(ms[1])
                # L-4 2단계: 모듈의 struct/union 이름 수집 (reflection 매핑용)
                struct_orig_names = set()
                for ms in mod_stmts:
                    if ms[0] in ('StructDef', 'UnionDef'):
                        struct_orig_names.add(ms[1])
                # L-3 2차: 정의를 접두사 이름으로 등록 + fn body 글로벌 참조 변환
                name_map = {}
                for ms in mod_stmts:
                    renamed = self._rename_module_def(ms, prefix, name_map, globals_in_mod)
                    if renamed is not None:
                        new_statements.append(renamed)

                # L-4 2단계 헬퍼: struct가 import되면 그 reflection 함수도 함께 매핑.
                # 사용자 코드에서 `_reflect_Foo_get_f64(&it, 0)`을 그대로 쓰면
                # 내부적으로 `_reflect__mod_X_Foo_get_f64`로 라우팅됨.
                def _alias_reflection(orig_name, prefixed_name):
                    if orig_name not in struct_orig_names:
                        return
                    # Danha core reflection stays self-contained. Ari-backed save/load
                    # helpers are not auto-aliased because they should not be pulled into
                    # standalone Danha programs implicitly.
                    refl_suffixes = (
                        'field_count', 'field_name', 'field_type',
                        'get_f64', 'set_f64',
                        'get_str', 'set_str',
                        'dump',
                    )
                    for suffix in refl_suffixes:
                        self._from_imports[f"_reflect_{orig_name}_{suffix}"] = (
                            f"_reflect_{prefixed_name}_{suffix}"
                        )

                # from ... import로 가져온 이름을 직접 매핑
                if names == '*':
                    for orig_name, prefixed_name in name_map.items():
                        self._from_imports[orig_name] = prefixed_name
                        _alias_reflection(orig_name, prefixed_name)
                else:
                    for name in names:
                        if name not in name_map:
                            raise DanhaNameError(
                                f"모듈 '{module_name}'에 '{name}'이 없어",
                                line=line, source=self._source_code
                            )
                        self._from_imports[name] = name_map[name]
                        _alias_reflection(name, name_map[name])
            else:
                new_statements.append(stmt)
        
        return new_statements
    
    def _load_module_ast(self, module_name, line):
        """모듈 파일을 찾아서 파싱하고 AST의 statements를 반환."""
        import os
        
        # 순환 감지
        if module_name in Compiler._MODULE_LOADING_COMPILE:
            raise DanhaImportError(
                f"순환 임포트를 감지했어: '{module_name}'",
                line=line, source=self._source_code
            )
        
        # 캐시 확인 — (file_path, mtime, stmts) 형태로 저장.
        # mtime이 같으면 파일이 바뀌지 않은 것 → 캐시 유효.
        # danha run --watch 같은 반복 실행에서 변경된 파일만 재파싱.
        cached = Compiler._MODULE_CACHE_COMPILE.get(module_name)

        # 파일 찾기 — 소스 디렉토리 → 단아 설치 디렉토리 순으로 탐색
        parts = module_name.split('.')
        rel_path = os.path.join(*parts) + '.dh'
        base = Compiler._MODULE_BASE_DIR_COMPILE
        danha_dir = os.path.dirname(os.path.abspath(__file__))

        file_path = None
        for search_dir in filter(None, [base, os.getcwd(), danha_dir]):
            candidate = os.path.join(search_dir, rel_path)
            if os.path.exists(candidate):
                file_path = candidate
                break

        if file_path is None:
            searched = base or danha_dir
            raise DanhaNameError(
                f"모듈 '{module_name}'을 찾을 수 없어. "
                f"'{os.path.join(searched, rel_path)}' 파일이 존재하지 않아",
                line=line, source=self._source_code
            )

        # mtime 비교: 파일이 바뀌지 않았으면 캐시 반환
        try:
            current_mtime = os.path.getmtime(file_path)
        except OSError:
            current_mtime = None
        if (cached is not None and current_mtime is not None
                and cached[1] == current_mtime):
            return cached[2]

        with open(file_path, 'r', encoding='utf-8') as f:
            source = f.read()

        Compiler._MODULE_LOADING_COMPILE.add(module_name)
        try:
            tokens = lex(source)
            tree = parse(tokens)
            mod_stmts = tree[1]  # Program 노드의 statements
            
            # Unwrapping Attributed and DocAnnotated
            unwrapped = []
            for stmt in mod_stmts:
                if stmt[0] == 'Attributed':
                    attrs = stmt[1]
                    inner = stmt[2]
                    while inner[0] == 'Attributed':
                        attrs = attrs + inner[1]
                        inner = inner[2]
                    target_name = inner[1] if len(inner) > 1 else None
                    if target_name:
                        prefix = f"_mod_{module_name.replace('.', '_')}_"
                        prefixed_target = prefix + target_name
                        attr_list = []
                        for attr in attrs:
                            attr_name = attr[1]
                            attr_args = {}
                            for arg in attr[2]:
                                if arg[0] == 'KeyVal':
                                    attr_args[arg[1]] = arg[2]
                                elif arg[0] in ('StringArg', 'NumArg', 'NameArg'):
                                    attr_args[len(attr_args)] = arg[1]
                            attr_list.append((attr_name, attr_args))
                        self._comp_attributes[target_name] = attr_list
                        self._comp_attributes[prefixed_target] = attr_list
                    unwrapped.append(inner)
                else:
                    unwrapped.append(stmt)
            mod_stmts = unwrapped

            unwrapped2 = []
            for stmt in mod_stmts:
                if stmt[0] == 'DocAnnotated':
                    unwrapped2.append(stmt[2])
                else:
                    unwrapped2.append(stmt)
            mod_stmts = unwrapped2

            Compiler._MODULE_CACHE_COMPILE[module_name] = (file_path, current_mtime, mod_stmts)
            return mod_stmts
        finally:
            Compiler._MODULE_LOADING_COMPILE.discard(module_name)
    
    def _rewrite_names(self, node, globals_set, prefix):
        """L-3: AST를 재귀 walk하면서 globals_set에 든 Name/Assign target을 prefix로 변환.
        모듈 fn 본문 안의 글로벌 변수 참조를 prefix 이름으로 통일.
        """
        if isinstance(node, tuple):
            if len(node) == 0:
                return node
            kind = node[0]
            # Name 노드 — 이름 자체 변환
            if kind == 'Name' and len(node) >= 2 and isinstance(node[1], str):
                name = node[1]
                if name in globals_set:
                    return (kind, prefix + name) + node[2:]
                return node
            # Assign 노드 — target string 변환 + value 재귀
            if kind == 'Assign' and len(node) >= 3 and isinstance(node[1], str):
                target = node[1]
                new_target = prefix + target if target in globals_set else target
                new_value = self._rewrite_names(node[2], globals_set, prefix)
                return (kind, new_target, new_value) + node[3:]
            # 다른 모든 노드 — 자식 재귀
            return tuple(self._rewrite_names(c, globals_set, prefix) for c in node)
        if isinstance(node, list):
            return [self._rewrite_names(c, globals_set, prefix) for c in node]
        return node

    def _rename_module_def(self, stmt, prefix, name_map, globals_set=None):
        """
        모듈의 정의 AST 노드의 이름에 접두사를 붙인다.
        name_map에 {원래이름: 접두사이름}을 기록.
        L-3: globals_set이 주어지면 FnDef body 안의 글로벌 참조도 prefix로 변환.
        """
        if globals_set is None:
            globals_set = set()
        node_type = stmt[0]

        if node_type == 'FnDef':
            orig_name = stmt[1]
            new_name = prefix + orig_name
            name_map[orig_name] = new_name
            # ('FnDef', name, params, body, param_types, return_type, type_params, line)
            # L-3: body 안 글로벌 변수 참조를 prefix로 변환
            new_body = self._rewrite_names(stmt[3], globals_set, prefix)
            return (node_type, new_name, stmt[2], new_body) + stmt[4:]
        
        if node_type in ('StructDef', 'UnionDef'):
            orig_name = stmt[1]
            new_name = prefix + orig_name
            name_map[orig_name] = new_name
            return (node_type, new_name) + stmt[2:]

        if node_type == 'EnumDef':
            orig_name = stmt[1]
            new_name = prefix + orig_name
            name_map[orig_name] = new_name
            return (node_type, new_name) + stmt[2:]
        
        if node_type == 'ConstDef':
            orig_name = stmt[1]
            new_name = prefix + orig_name
            name_map[orig_name] = new_name
            return (node_type, new_name) + stmt[2:]
        
        if node_type == 'ComponentDef':
            orig_name = stmt[1]
            new_name = prefix + orig_name
            name_map[orig_name] = new_name
            return (node_type, new_name) + stmt[2:]
        
        if node_type == 'SystemDef':
            orig_name = stmt[1]
            new_name = prefix + orig_name
            name_map[orig_name] = new_name
            # bindings는 stmt[4]에 위치함. 컴포넌트 이름에 모듈 접두사(prefix) 적용.
            orig_bindings = stmt[4]
            new_bindings = []
            for b in orig_bindings:
                b_var, c_name = b[0], b[1]
                if c_name in self._from_imports:
                    new_c_name = self._from_imports[c_name]
                else:
                    new_c_name = prefix + c_name
                new_b = (b_var, new_c_name) + b[2:]
                new_bindings.append(new_b)
            # body(stmt[5]) 내부의 글로벌 변수 참조도 prefix로 변환
            new_body = self._rewrite_names(stmt[5], globals_set, prefix)
            return (node_type, new_name, stmt[2], stmt[3], new_bindings, new_body) + stmt[6:]
        
        if node_type in ('Impl', 'ImplTrait', 'TraitDef'):
            # TODO: trait/impl의 모듈 간 이동은 더 복잡. 지금은 있는 그대로 삽입.
            return stmt
        
        if node_type == 'ExternFn':
            # extern 함수는 C 이름을 그대로 써야 하므로 접두사 없이 삽입.
            orig_name = stmt[1]
            name_map[orig_name] = orig_name
            return stmt
        
        if node_type == 'CLinkedFn':
            # CLinkedFn의 경우 stmt[1]은 lib_name이고 stmt[2]가 inner ExternFn node임.
            orig_name = stmt[2][1]
            name_map[orig_name] = orig_name
            return stmt

        # Stage 82: 모듈 탑레벨 Assign도 그대로 삽입 (이름 prefix 없이).
        # 모듈이 자기 글로벌 state를 가질 수 있게. fn body 안의 Name이 원래 이름으로
        # L-3 (2026-05-19): 탑레벨 Assign에 prefix 부여 + name_map 등록.
        # 모듈 fn 본문의 자기 글로벌 참조는 _rewrite_names가 prefix로 변환됨.
        if node_type == 'Assign' and len(stmt) >= 5 and isinstance(stmt[1], str):
            orig_name = stmt[1]
            new_name = prefix + orig_name
            name_map[orig_name] = new_name
            new_value = self._rewrite_names(stmt[2], globals_set, prefix)
            return ('Assign', new_name, new_value) + stmt[3:]

        # 나머지 (Print 등)는 무시 — 모듈의 최상위 실행문은 컴파일 안 함
        return None
    
    def _declare_struct(self, node):
        """('StructDef', name, [(field_name, field_type_or_None), ...], line)
        
        LLVM의 명명된 구조체 타입(named struct type)을 만들어 등록한다.
        명명된 타입을 쓰면 IR이 읽기 좋고, 나중에 자기 참조 (linked list 등)도 가능.
        
        필드 타입 매핑: 'i32' → i32, 'f64' → f64, None → i32 (기본값).
        앞으로 다른 struct를 필드 타입으로 받게 되면 여기에 추가.
        """
        name = node[1]
        fields = node[2]  # [(이름, 타입_또는_None), ...]
        line = node[-1]
        
        if name in self.structs:
            raise DanhaRuntimeError(f"구조체 이름 중복: {name}", line=line, source=self._source_code)
        
        # 7.2.1b: 필드 타입은 이제 AST 노드(또는 None).
        # 참조 필드 거부는 파서가 이미 함 — 여기서는 그냥 _resolve_type으로 해석.
        field_names = []
        field_types = []
        seen = set()
        for fname, ftype_node in fields:
            if fname in seen:
                raise DanhaRuntimeError(f"{name}에 중복된 필드 이름: {fname}", line=line, source=self._source_code)
            seen.add(fname)
            field_names.append(fname)
            
            llvm_ty, ref_kind = self._resolve_type(
                ftype_node, f"{name}.{fname}", line
            )
            # 구조체 필드에 참조는 파서가 거부해서 여기 오면 안 됨. 방어적 검사.
            if ref_kind is not None:
                raise DanhaTypeError(
                    f"{name}.{fname}에 참조 타입은 허용 안 됨",
                    line=line, source=self._source_code
                )
            field_types.append(llvm_ty)
        
        # 명명된 LLVM 구조체 타입 생성.
        # context.get_identified_type 후 set_body가 표준 절차.
        # llvmlite에선 module.context를 통해 접근.
        llvm_struct = self.module.context.get_identified_type(
            f"struct.{name}{self._struct_suffix}"
        )
        llvm_struct.set_body(*field_types)
        # 46단계: @packed 속성 — set_body 후 packed 플래그를 직접 설정 (llvmlite 0.47 호환)
        attrs = self._comp_attributes.get(name, [])
        if any(a[0] == 'packed' for a in attrs):
            llvm_struct.packed = True

        self.structs[name] = (llvm_struct, field_names, field_types)

        # L-4 reflection: 자동 메타 함수 emit.
        # struct 정의 직후 reflection helper들이 LLVM 모듈에 등록되어
        # Danha 코드에서 `_reflect_<Foo>_field_count()` 등으로 직접 호출 가능.
        # 에디터 Inspector / 자동 직렬화 / debug 도구의 토대.
        self._emit_reflection_fns(name, llvm_struct, field_names, field_types)

    def _emit_reflection_fns(self, struct_name, llvm_struct, field_names, field_types):
        """L-4 reflection: struct마다 5개 메타 함수를 LLVM 모듈에 자동 emit.

        - _reflect_<Foo>_field_count() -> i32
        - _reflect_<Foo>_field_name(i: i32) -> i8*
        - _reflect_<Foo>_field_type(i: i32) -> i32  (0=i32, 1=f64, 2=bool, 3=ptr, -1=other)
        - _reflect_<Foo>_get_f64(s: &Foo, i: i32) -> f64  (숫자 필드는 f64로 변환)
        - _reflect_<Foo>_set_f64(s: &mut Foo, i: i32, v: f64)  (숫자 필드 갱신)

        Danha 코드 측에서는 `extern fn` 없이도 `self.functions`에 등록되어 호출 가능.
        포인터 인자는 ref/mut_ref 시그니처로 등록 → 호출 시 `&foo`/`&mut foo`.
        """
        n = len(field_names)
        # 빈 struct는 reflection emit 불필요 (호출해도 의미 없음).
        if n == 0:
            return

        # 중복 emit 방지 — 같은 컴파일러 인스턴스에서 _declare_struct는 한 번씩만 호출되지만
        # 모듈 import 등 예외 케이스 대비.
        count_fname = f"_reflect_{struct_name}_field_count"
        if count_fname in self.functions:
            return

        struct_ptr_ty = llvm_struct.as_pointer()

        # 1) _reflect_<Foo>_field_count() -> i32
        fn_ty = ir.FunctionType(i32, [])
        fn = ir.Function(self.module, fn_ty, name=count_fname)
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        b.ret(ir.Constant(i32, n))
        self.functions[count_fname] = fn
        self.fn_param_ref_kinds[count_fname] = []
        self.fn_is_vararg[count_fname] = False

        # field name 문자열 글로벌 미리 생성
        name_globals = []
        for j, fname in enumerate(field_names):
            text = fname + "\0"
            data = bytearray(text.encode("utf8"))
            const = ir.Constant(ir.ArrayType(i8, len(data)), data)
            # 글로벌 이름 충돌 방지: struct + 인덱스 + suffix
            glob_name = f"_refl_{struct_name}_fname_{j}{self._struct_suffix}"
            glob = ir.GlobalVariable(self.module, const.type, name=glob_name)
            glob.linkage = "internal"
            glob.global_constant = True
            glob.initializer = const
            name_globals.append(glob)

        # 2) _reflect_<Foo>_field_name(i: i32) -> i8*
        name_fname = f"_reflect_{struct_name}_field_name"
        fn_ty = ir.FunctionType(i8p, [i32])
        fn = ir.Function(self.module, fn_ty, name=name_fname)
        fn.args[0].name = "i"
        entry = fn.append_basic_block("entry")
        default_bb = fn.append_basic_block("default")
        b = ir.IRBuilder(entry)
        sw = b.switch(fn.args[0], default_bb)
        for j, glob in enumerate(name_globals):
            case_bb = fn.append_basic_block(f"case_{j}")
            sw.add_case(ir.Constant(i32, j), case_bb)
            cb = ir.IRBuilder(case_bb)
            ptr = cb.bitcast(glob, i8p)
            cb.ret(ptr)
        db = ir.IRBuilder(default_bb)
        db.ret(ir.Constant(i8p, None))
        self.functions[name_fname] = fn
        self.fn_param_ref_kinds[name_fname] = [None]
        self.fn_is_vararg[name_fname] = False

        # 타입 코드 매핑 (L-4 4단계 확장)
        # 0=i32, 1=f64, 2=bool, 3=ptr(generic), 4=i64, 5=f32, 6=string(i8*),
        # 7=nested struct, -1=other
        def _type_code(ty):
            if isinstance(ty, ir.DoubleType):
                return 1
            if isinstance(ty, ir.FloatType):
                return 5
            if isinstance(ty, ir.IntType):
                if ty.width == 1 or ty.width == 8:
                    return 2  # bool
                if ty.width == 32:
                    return 0  # i32
                if ty.width == 64:
                    return 4  # i64
                return -1
            if isinstance(ty, ir.PointerType):
                # i8* 는 string으로 본다. 다른 포인터는 generic ptr.
                pointee = ty.pointee
                if isinstance(pointee, ir.IntType) and pointee.width == 8:
                    return 6
                return 3
            if isinstance(ty, ir.IdentifiedStructType):
                return 7
            return -1

        # 3) _reflect_<Foo>_field_type(i: i32) -> i32
        type_fname = f"_reflect_{struct_name}_field_type"
        fn_ty = ir.FunctionType(i32, [i32])
        fn = ir.Function(self.module, fn_ty, name=type_fname)
        fn.args[0].name = "i"
        entry = fn.append_basic_block("entry")
        default_bb = fn.append_basic_block("default")
        b = ir.IRBuilder(entry)
        sw = b.switch(fn.args[0], default_bb)
        for j, ftype in enumerate(field_types):
            case_bb = fn.append_basic_block(f"case_{j}")
            sw.add_case(ir.Constant(i32, j), case_bb)
            cb = ir.IRBuilder(case_bb)
            cb.ret(ir.Constant(i32, _type_code(ftype)))
        db = ir.IRBuilder(default_bb)
        db.ret(ir.Constant(i32, -1))
        self.functions[type_fname] = fn
        self.fn_param_ref_kinds[type_fname] = [None]
        self.fn_is_vararg[type_fname] = False

        # 4) _reflect_<Foo>_get_f64(s: &Foo, i: i32) -> f64
        get_fname = f"_reflect_{struct_name}_get_f64"
        fn_ty = ir.FunctionType(f64, [struct_ptr_ty, i32])
        fn = ir.Function(self.module, fn_ty, name=get_fname)
        fn.args[0].name = "s"
        fn.args[1].name = "i"
        entry = fn.append_basic_block("entry")
        default_bb = fn.append_basic_block("default")
        b = ir.IRBuilder(entry)
        sw = b.switch(fn.args[1], default_bb)
        for j, ftype in enumerate(field_types):
            case_bb = fn.append_basic_block(f"case_{j}")
            sw.add_case(ir.Constant(i32, j), case_bb)
            cb = ir.IRBuilder(case_bb)
            field_ptr = cb.gep(
                fn.args[0],
                [ir.Constant(i32, 0), ir.Constant(i32, j)],
                inbounds=True,
            )
            if isinstance(ftype, ir.DoubleType):
                val = cb.load(field_ptr)
                cb.ret(val)
            elif isinstance(ftype, ir.FloatType):
                # f32 → f64 확장
                val = cb.load(field_ptr)
                val = cb.fpext(val, f64)
                cb.ret(val)
            elif isinstance(ftype, ir.IntType):
                val = cb.load(field_ptr)
                # bool/i8/i16/i32/i64 → f64 (signed)
                if ftype.width < 32:
                    val = cb.zext(val, i32)  # bool/i8/i16는 unsigned 확장
                    val = cb.sitofp(val, f64)
                else:
                    val = cb.sitofp(val, f64)
                cb.ret(val)
            else:
                # 포인터/struct 등 — 의미 있는 f64 변환 없음. 0.0 반환.
                cb.ret(ir.Constant(f64, 0.0))
        db = ir.IRBuilder(default_bb)
        db.ret(ir.Constant(f64, 0.0))
        self.functions[get_fname] = fn
        # 첫 인자는 read-only ref, 두 번째는 i32 by value
        self.fn_param_ref_kinds[get_fname] = ['ref', None]
        self.fn_is_vararg[get_fname] = False

        # 5) _reflect_<Foo>_set_f64(s: &mut Foo, i: i32, v: f64)
        set_fname = f"_reflect_{struct_name}_set_f64"
        fn_ty = ir.FunctionType(ir.VoidType(), [struct_ptr_ty, i32, f64])
        fn = ir.Function(self.module, fn_ty, name=set_fname)
        fn.args[0].name = "s"
        fn.args[1].name = "i"
        fn.args[2].name = "v"
        entry = fn.append_basic_block("entry")
        default_bb = fn.append_basic_block("default")
        b = ir.IRBuilder(entry)
        sw = b.switch(fn.args[1], default_bb)
        for j, ftype in enumerate(field_types):
            case_bb = fn.append_basic_block(f"case_{j}")
            sw.add_case(ir.Constant(i32, j), case_bb)
            cb = ir.IRBuilder(case_bb)
            field_ptr = cb.gep(
                fn.args[0],
                [ir.Constant(i32, 0), ir.Constant(i32, j)],
                inbounds=True,
            )
            v = fn.args[2]
            if isinstance(ftype, ir.DoubleType):
                cb.store(v, field_ptr)
            elif isinstance(ftype, ir.FloatType):
                # f64 → f32 축소
                fv = cb.fptrunc(v, f32)
                cb.store(fv, field_ptr)
            elif isinstance(ftype, ir.IntType):
                # f64 → 정수 (truncating). bool은 0/1로 클램프.
                if ftype.width == 1:
                    # bool: v != 0.0
                    cmp = cb.fcmp_ordered('!=', v, ir.Constant(f64, 0.0))
                    cb.store(cmp, field_ptr)
                else:
                    iv = cb.fptosi(v, ftype)
                    cb.store(iv, field_ptr)
            # 포인터 등은 set 불가 — silently skip
            cb.ret_void()
        db = ir.IRBuilder(default_bb)
        db.ret_void()
        self.functions[set_fname] = fn
        self.fn_param_ref_kinds[set_fname] = ['mut_ref', None, None]
        self.fn_is_vararg[set_fname] = False

        # L-4 4단계: string(i8*) 필드 전용 get/set
        # i8* 가 아닌 필드는 get_str → null, set_str → no-op.
        # 5b) _reflect_<Foo>_get_str(s: &Foo, i: i32) -> i8*
        get_str_fname = f"_reflect_{struct_name}_get_str"
        fn_ty = ir.FunctionType(i8p, [struct_ptr_ty, i32])
        fn = ir.Function(self.module, fn_ty, name=get_str_fname)
        fn.args[0].name = "s"
        fn.args[1].name = "i"
        entry = fn.append_basic_block("entry")
        default_bb = fn.append_basic_block("default")
        b = ir.IRBuilder(entry)
        sw = b.switch(fn.args[1], default_bb)
        for j, ftype in enumerate(field_types):
            case_bb = fn.append_basic_block(f"case_{j}")
            sw.add_case(ir.Constant(i32, j), case_bb)
            cb = ir.IRBuilder(case_bb)
            is_str = (
                isinstance(ftype, ir.PointerType)
                and isinstance(ftype.pointee, ir.IntType)
                and ftype.pointee.width == 8
            )
            if is_str:
                field_ptr = cb.gep(
                    fn.args[0],
                    [ir.Constant(i32, 0), ir.Constant(i32, j)],
                    inbounds=True,
                )
                val = cb.load(field_ptr)
                cb.ret(val)
            else:
                cb.ret(ir.Constant(i8p, None))
        db = ir.IRBuilder(default_bb)
        db.ret(ir.Constant(i8p, None))
        self.functions[get_str_fname] = fn
        self.fn_param_ref_kinds[get_str_fname] = ['ref', None]
        self.fn_is_vararg[get_str_fname] = False

        # 5c) _reflect_<Foo>_set_str(s: &mut Foo, i: i32, v: i8*)
        set_str_fname = f"_reflect_{struct_name}_set_str"
        fn_ty = ir.FunctionType(ir.VoidType(), [struct_ptr_ty, i32, i8p])
        fn = ir.Function(self.module, fn_ty, name=set_str_fname)
        fn.args[0].name = "s"
        fn.args[1].name = "i"
        fn.args[2].name = "v"
        entry = fn.append_basic_block("entry")
        default_bb = fn.append_basic_block("default")
        b = ir.IRBuilder(entry)
        sw = b.switch(fn.args[1], default_bb)
        for j, ftype in enumerate(field_types):
            case_bb = fn.append_basic_block(f"case_{j}")
            sw.add_case(ir.Constant(i32, j), case_bb)
            cb = ir.IRBuilder(case_bb)
            is_str = (
                isinstance(ftype, ir.PointerType)
                and isinstance(ftype.pointee, ir.IntType)
                and ftype.pointee.width == 8
            )
            if is_str:
                field_ptr = cb.gep(
                    fn.args[0],
                    [ir.Constant(i32, 0), ir.Constant(i32, j)],
                    inbounds=True,
                )
                cb.store(fn.args[2], field_ptr)
            cb.ret_void()
        db = ir.IRBuilder(default_bb)
        db.ret_void()
        self.functions[set_str_fname] = fn
        self.fn_param_ref_kinds[set_str_fname] = ['mut_ref', None, None]
        self.fn_is_vararg[set_str_fname] = False

        # L-4 3단계: debug dump 헬퍼
        # 6) _reflect_<Foo>_dump(s: &Foo)  — printf로 "name = value" 출력
        # Save/load used to be emitted here through Ari's ari_save/ari_load symbols.
        # That made every non-empty Danha struct silently depend on Ari in the JIT.
        # Keep the language core reflection self-contained; engine-specific
        # persistence should live behind explicit Ari modules.
        self._emit_reflection_helpers(
            struct_name, struct_ptr_ty, field_names, field_types, name_globals
        )

    def _emit_reflection_helpers(
        self, struct_name, struct_ptr_ty, field_names, field_types, name_globals
    ):
        """L-4 3단계: dump 자동 emit.

        - dump:  printf로 "Foo.field_name = value" 콘솔 출력 (의존성: printf only)
        """
        # 공통 포맷 문자열 글로벌 (struct마다 새로 만듦 — 충돌 방지)
        def _mkstr(suffix, text):
            data = bytearray(text.encode("utf8"))
            const = ir.Constant(ir.ArrayType(i8, len(data)), data)
            name = f"_refl_{struct_name}_fmt_{suffix}{self._struct_suffix}"
            glob = ir.GlobalVariable(self.module, const.type, name=name)
            glob.linkage = "internal"
            glob.global_constant = True
            glob.initializer = const
            return glob

        fmt_header = _mkstr("hdr", f"{struct_name} {{\n\0")
        fmt_field_num = _mkstr("num", "  %s = %g\n\0")
        fmt_field_str = _mkstr("str", "  %s = \"%s\"\n\0")
        fmt_field_other = _mkstr("oth", "  %s = <%s>\n\0")
        fmt_close = _mkstr("cls", "}\n\0")
        # nested struct / ptr 자리에 표시할 placeholder
        lbl_ptr = _mkstr("lblp", "ptr\0")
        lbl_struct = _mkstr("lbls", "struct\0")
        lbl_other = _mkstr("lblo", "other\0")
        self._reflection_persistence_defs[struct_name] = (
            struct_ptr_ty, field_names, field_types, name_globals
        )

        # 6) _reflect_<Foo>_dump(s: &Foo)
        dump_fname = f"_reflect_{struct_name}_dump"
        fn_ty = ir.FunctionType(ir.VoidType(), [struct_ptr_ty])
        fn = ir.Function(self.module, fn_ty, name=dump_fname)
        fn.args[0].name = "s"
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        # printf("Foo {\n")
        b.call(self.printf, [b.bitcast(fmt_header, i8p)])
        for j, ftype in enumerate(field_types):
            name_ptr = b.bitcast(name_globals[j], i8p)
            field_ptr = b.gep(
                fn.args[0],
                [ir.Constant(i32, 0), ir.Constant(i32, j)],
                inbounds=True,
            )
            # 숫자/bool은 %g, string은 %s, 그 외는 placeholder.
            if isinstance(ftype, ir.DoubleType):
                v_f64 = b.load(field_ptr)
                b.call(self.printf, [b.bitcast(fmt_field_num, i8p), name_ptr, v_f64])
            elif isinstance(ftype, ir.FloatType):
                v = b.load(field_ptr)
                v_f64 = b.fpext(v, f64)
                b.call(self.printf, [b.bitcast(fmt_field_num, i8p), name_ptr, v_f64])
            elif isinstance(ftype, ir.IntType):
                v = b.load(field_ptr)
                if ftype.width < 32:
                    v = b.zext(v, i32)
                v_f64 = b.sitofp(v, f64)
                b.call(self.printf, [b.bitcast(fmt_field_num, i8p), name_ptr, v_f64])
            elif (
                isinstance(ftype, ir.PointerType)
                and isinstance(ftype.pointee, ir.IntType)
                and ftype.pointee.width == 8
            ):
                # string (i8*)
                s_val = b.load(field_ptr)
                b.call(self.printf, [b.bitcast(fmt_field_str, i8p), name_ptr, s_val])
            else:
                if isinstance(ftype, ir.PointerType):
                    lbl = b.bitcast(lbl_ptr, i8p)
                elif isinstance(ftype, ir.IdentifiedStructType):
                    lbl = b.bitcast(lbl_struct, i8p)
                else:
                    lbl = b.bitcast(lbl_other, i8p)
                b.call(self.printf, [b.bitcast(fmt_field_other, i8p), name_ptr, lbl])
        b.call(self.printf, [b.bitcast(fmt_close, i8p)])
        b.ret_void()
        self.functions[dump_fname] = fn
        self.fn_param_ref_kinds[dump_fname] = ['ref']
        self.fn_is_vararg[dump_fname] = False

    def _emit_reflection_persistence_helpers(self, struct_name):
        """Emit Ari-backed reflection persistence only when explicitly called."""
        save_fname = f"_reflect_{struct_name}_save_to_path"
        load_fname = f"_reflect_{struct_name}_load_from_path"
        if save_fname in self.functions and load_fname in self.functions:
            return
        if struct_name not in self._reflection_persistence_defs:
            return

        struct_ptr_ty, field_names, field_types, name_globals = self._reflection_persistence_defs[struct_name]

        def _extern(name, ret_ty, arg_tys):
            existing = self.module.globals.get(name)
            if existing is not None:
                return existing
            ty = ir.FunctionType(ret_ty, arg_tys)
            return ir.Function(self.module, ty, name=name)

        save_open = _extern("ari_save_open", i32, [i8p])
        save_int_fn = _extern("ari_save_int", ir.VoidType(), [i8p, i32])
        save_float = _extern("ari_save_float", ir.VoidType(), [i8p, f64])
        save_string_fn = _extern("ari_save_string", ir.VoidType(), [i8p, i8p])
        save_close = _extern("ari_save_close", i32, [])
        load_open = _extern("ari_load_open", i32, [i8p])
        load_int_fn = _extern("ari_load_int", i32, [i8p, i32])
        load_float = _extern("ari_load_float", f64, [i8p, f64])
        load_close = _extern("ari_load_close", ir.VoidType(), [])

        if save_fname not in self.functions:
            fn_ty = ir.FunctionType(i32, [struct_ptr_ty, i8p])
            fn = ir.Function(self.module, fn_ty, name=save_fname)
            fn.linkage = "internal"
            fn.args[0].name = "s"
            fn.args[1].name = "path"
            entry = fn.append_basic_block("entry")
            write_bb = fn.append_basic_block("write")
            fail_bb = fn.append_basic_block("fail")
            b = ir.IRBuilder(entry)
            opened = b.call(save_open, [fn.args[1]])
            is_open = b.icmp_signed("!=", opened, ir.Constant(i32, 0))
            b.cbranch(is_open, write_bb, fail_bb)

            fb = ir.IRBuilder(fail_bb)
            fb.ret(ir.Constant(i32, 0))

            wb = ir.IRBuilder(write_bb)
            for j, ftype in enumerate(field_types):
                name_ptr = wb.bitcast(name_globals[j], i8p)
                field_ptr = wb.gep(fn.args[0], [ir.Constant(i32, 0), ir.Constant(i32, j)], inbounds=True)
                if isinstance(ftype, ir.DoubleType):
                    wb.call(save_float, [name_ptr, wb.load(field_ptr)])
                elif isinstance(ftype, ir.FloatType):
                    wb.call(save_float, [name_ptr, wb.fpext(wb.load(field_ptr), f64)])
                elif isinstance(ftype, ir.IntType):
                    v = wb.load(field_ptr)
                    if ftype.width < 32:
                        v_i32 = wb.zext(v, i32)
                    elif ftype.width == 32:
                        v_i32 = v
                    else:
                        v_i32 = wb.trunc(v, i32)
                    wb.call(save_int_fn, [name_ptr, v_i32])
                elif (
                    isinstance(ftype, ir.PointerType)
                    and isinstance(ftype.pointee, ir.IntType)
                    and ftype.pointee.width == 8
                ):
                    wb.call(save_string_fn, [name_ptr, wb.load(field_ptr)])
            wb.call(save_close, [])
            wb.ret(ir.Constant(i32, 1))
            self.functions[save_fname] = fn
            self.fn_param_ref_kinds[save_fname] = ['ref', None]
            self.fn_is_vararg[save_fname] = False

        if load_fname not in self.functions:
            fn_ty = ir.FunctionType(i32, [struct_ptr_ty, i8p])
            fn = ir.Function(self.module, fn_ty, name=load_fname)
            fn.linkage = "internal"
            fn.args[0].name = "s"
            fn.args[1].name = "path"
            entry = fn.append_basic_block("entry")
            read_bb = fn.append_basic_block("read")
            fail_bb = fn.append_basic_block("fail")
            b = ir.IRBuilder(entry)
            opened = b.call(load_open, [fn.args[1]])
            is_open = b.icmp_signed("!=", opened, ir.Constant(i32, 0))
            b.cbranch(is_open, read_bb, fail_bb)

            fb = ir.IRBuilder(fail_bb)
            fb.ret(ir.Constant(i32, 0))

            rb = ir.IRBuilder(read_bb)
            for j, ftype in enumerate(field_types):
                name_ptr = rb.bitcast(name_globals[j], i8p)
                field_ptr = rb.gep(fn.args[0], [ir.Constant(i32, 0), ir.Constant(i32, j)], inbounds=True)
                if isinstance(ftype, ir.DoubleType):
                    cur = rb.load(field_ptr)
                    rb.store(rb.call(load_float, [name_ptr, cur]), field_ptr)
                elif isinstance(ftype, ir.FloatType):
                    cur = rb.fpext(rb.load(field_ptr), f64)
                    rb.store(rb.fptrunc(rb.call(load_float, [name_ptr, cur]), f32), field_ptr)
                elif isinstance(ftype, ir.IntType):
                    cur = rb.load(field_ptr)
                    if ftype.width < 32:
                        cur_i32 = rb.zext(cur, i32)
                    elif ftype.width == 32:
                        cur_i32 = cur
                    else:
                        cur_i32 = rb.trunc(cur, i32)
                    v_i32 = rb.call(load_int_fn, [name_ptr, cur_i32])
                    if ftype.width == 1:
                        rb.store(rb.icmp_signed("!=", v_i32, ir.Constant(i32, 0)), field_ptr)
                    elif ftype.width < 32:
                        rb.store(rb.trunc(v_i32, ftype), field_ptr)
                    elif ftype.width == 32:
                        rb.store(v_i32, field_ptr)
                    else:
                        rb.store(rb.sext(v_i32, ftype), field_ptr)
            rb.call(load_close, [])
            rb.ret(ir.Constant(i32, 1))
            self.functions[load_fname] = fn
            self.fn_param_ref_kinds[load_fname] = ['mut_ref', None]
            self.fn_is_vararg[load_fname] = False

    def _llvm_type_size(self, ty):
        """LLVM 타입의 바이트 크기 (스칼라만). union의 가장 큰 필드 선택에 사용."""
        if isinstance(ty, ir.IntType):
            return (ty.width + 7) // 8
        if isinstance(ty, ir.DoubleType):
            return 8
        if isinstance(ty, ir.FloatType):
            return 4
        if isinstance(ty, ir.PointerType):
            return 8  # 64비트 포인터
        if isinstance(ty, ir.IdentifiedStructType):
            # 내포된 구조체: 필드 크기 합산 (근사)
            return sum(self._llvm_type_size(f) for f in ty.elements) or 8
        return 8  # 기본값

    def _struct_name_from_llvm(self, llvm_ty):
        """LLVM IdentifiedStructType에서 structs dict 키 이름을 역검색."""
        for name, info in self.structs.items():
            if info[0] is llvm_ty:
                return name
        return None

    def _declare_union(self, node):
        """('UnionDef', name, [(field_name, field_type_or_None), ...], line)

        C union 의미론: 모든 필드가 같은 메모리를 공유.
        LLVM 표현: packed struct { <가장 큰 타입> }
        필드 접근: GEP(index=0) + bitcast to 원하는 타입*.
        """
        name = node[1]
        fields = node[2]
        line = node[-1]

        if name in self.structs:
            raise DanhaRuntimeError(f"이름 중복: {name}", line=line, source=self._source_code)

        field_names = []
        field_types = []
        seen = set()
        for fname, ftype_node in fields:
            if fname in seen:
                raise DanhaRuntimeError(f"{name}에 중복된 필드: {fname}", line=line, source=self._source_code)
            seen.add(fname)
            field_names.append(fname)
            llvm_ty, ref_kind = self._resolve_type(ftype_node, f"{name}.{fname}", line)
            if ref_kind is not None:
                raise DanhaTypeError(f"{name}.{fname}에 참조 타입은 허용 안 됨", line=line, source=self._source_code)
            field_types.append(llvm_ty)

        if not field_types:
            raise DanhaRuntimeError(f"union '{name}'에 필드가 없어", line=line, source=self._source_code)

        # 가장 큰 타입을 스토리지로 선택
        largest_ty = max(field_types, key=self._llvm_type_size)

        # LLVM 타입: packed struct { <가장 큰 타입> } (llvmlite 0.47 호환)
        llvm_union = self.module.context.get_identified_type(f"union.{name}{self._struct_suffix}")
        llvm_union.set_body(largest_ty)
        llvm_union.packed = True

        self.structs[name] = (llvm_union, field_names, field_types)
        self.unions.add(name)

    def _declare_component(self, node):
        """('ComponentDef', name, [(field_name, type_node_or_None), ...], line)

        d2-i: 컴포넌트 타입마다 LLVM 전역 변수들을 만들어 self.components에 등록.
        실제 배열 확보(malloc)는 _danha_ecs_init에서.

        저장 레이아웃 (4096 용량):
          _danha_comp_<N>_<f>:  ir.PointerType(<필드 LLVM 타입>)  (SoA 배열 포인터)
          _danha_comp_<N>_dense_to_entity: ir.PointerType(i32)
          _danha_comp_<N>_sparse:          ir.PointerType(i32)  (-1이면 없음)
          _danha_comp_<N>_count:           i32
          _danha_comp_<N>_capacity:        i32
        
        7.15f 이전: 모든 필드를 f64로 강제 저장 → 캐시 라인 낭비.
        7.15f 이후: 필드별 선언 타입으로 저장. 타입 생략(=None)은 f64 기본 (Q1=1 호환).
        """
        name = node[1]
        fields = node[2]
        line = node[-1]
        
        if name in self.components:
            raise DanhaECSError(f"component '{name}'이(가) 이미 정의됐어", line=line, source=self._source_code)
        
        field_names = [f[0] for f in fields]
        
        # 7.15f: 필드별 타입 해석
        # fields[i] = (field_name, type_node_or_None)
        # type_node가 None이면 f64 기본 (기존 호환: Q1=1).
        # 'bool'/구조체/참조 등 복잡한 건 나중에. 컴포넌트 필드엔 기본 스칼라만.
        field_types = []     # LLVM 타입 리스트 (순서 맞춤)
        field_type_names = []  # 단아 타입 이름 ('i32', 'u8', 'bool', 'f64' 등). 부호 구분과 ctypes 변환에 필요.
        for fname, type_node in fields:
            if type_node is None:
                # Q1=1: 타입 생략 → f64
                field_types.append(f64)
                field_type_names.append('f64')
            else:
                # type_node는 파서에서 나온 타입 노드. 참조/배열은 파서가 이미 컴포넌트 필드에서
                # 금지했고, 여기선 TypeName만 상정.
                if type_node[0] != 'TypeName':
                    raise DanhaTypeError(
                        f"컴포넌트 '{name}'의 필드 '{fname}' 타입은 "
                        f"기본 타입이어야 해 (i32, u8, bool, f64 등)",
                        line=line, source=self._source_code
                    )
                tname = type_node[1]
                if tname not in self._TYPE_MAP:
                    raise DanhaTypeError(
                        f"컴포넌트 '{name}'의 필드 '{fname}' 타입 '{tname}'은(는) "
                        f"아직 지원 안 해. 지원: {', '.join(sorted(self._TYPE_MAP.keys()))}",
                        line=line, source=self._source_code
                    )
                field_types.append(self._TYPE_MAP[tname])
                field_type_names.append(tname)
        
        field_globals = {}
        for fname, lty in zip(field_names, field_types):
            gv = ir.GlobalVariable(
                self.module, lty.as_pointer(),
                name=f"_danha_comp_{name}_{fname}"
            )
            gv.linkage = "internal"
            gv.initializer = ir.Constant(lty.as_pointer(), None)
            field_globals[fname] = gv
        
        dense_to_entity = ir.GlobalVariable(
            self.module, i32.as_pointer(),
            name=f"_danha_comp_{name}_dense_to_entity"
        )
        dense_to_entity.linkage = "internal"
        dense_to_entity.initializer = ir.Constant(i32.as_pointer(), None)
        
        sparse = ir.GlobalVariable(
            self.module, i32.as_pointer(),
            name=f"_danha_comp_{name}_sparse"
        )
        sparse.linkage = "internal"
        sparse.initializer = ir.Constant(i32.as_pointer(), None)
        
        count_gv = ir.GlobalVariable(
            self.module, i32,
            name=f"_danha_comp_{name}_count"
        )
        count_gv.linkage = "internal"
        count_gv.initializer = ir.Constant(i32, 0)
        
        capacity_gv = ir.GlobalVariable(
            self.module, i32,
            name=f"_danha_comp_{name}_capacity"
        )
        capacity_gv.linkage = "internal"
        capacity_gv.initializer = ir.Constant(i32, self.COMPONENT_CAPACITY)
        
        self.components[name] = {
            'fields': field_names,
            'field_globals': field_globals,
            'field_types': field_types,          # 7.15f: LLVM 타입 리스트
            'field_type_names': field_type_names, # 7.15f: 단아 타입 이름 ('i32', 'u8' 등)
            'dense_to_entity': dense_to_entity,
            'sparse': sparse,
            'count': count_gv,
            'capacity': capacity_gv,
        }
        
        # d3: component 값 타입을 LLVM 구조체로. 7.15f부터 필드별 실제 타입 사용.
        llvm_comp_ty = self.module.context.get_identified_type(
            f"comp.{name}{self._struct_suffix}"
        )
        llvm_comp_ty.set_body(*field_types)
        self.structs[name] = (llvm_comp_ty, field_names, list(field_types))
        self.components[name]['value_type'] = llvm_comp_ty
        
        # d2-iii: 이 컴포넌트의 remove-by-entity 헬퍼 함수 선언.
        # 본문은 _define_component_helpers에서. 인자는 entity index (i32),
        # 반환은 i1 (실제로 제거됐으면 true, 없었으면 false).
        remove_fn_ty = ir.FunctionType(ir.IntType(1), [i32])
        remove_fn = ir.Function(
            self.module, remove_fn_ty,
            name=f"_danha_comp_{name}_remove_by_entity"
        )
        self.components[name]['remove_fn'] = remove_fn
    
    def _declare_extern_function(self, node):
        """7.16a/7.17a: extern fn을 LLVM declare로 등록 (C-FFI).
        
        AST 형태: ('ExternFn', name, params, param_types, return_type, is_vararg, line)
        
        7.17a 추가:
        - is_vararg=True → FunctionType(..., var_arg=True) — printf 등에 필요
        - return_type에 PtrType 허용 — SDL_CreateWindow 등이 ptr 반환
        
        FnDef와 다른 점:
        - body가 없다 — LLVM에 declare만 하고 define은 안 함
        - 링크 시 C 라이브러리에서 실제 구현을 찾음
        - 이미 내부적으로 선언된 C 함수(printf, malloc 등)와 이름이 겹치면
          기존 것을 그대로 쓴다 (중복 declare 방지)
        """
        name = node[1]
        params = node[2]
        param_types_nodes = node[3]
        return_type_node = node[4]
        is_vararg = node[5] if len(node) > 6 else False
        line = node[-1]
        
        # 이미 같은 이름으로 내부 선언된 C 함수가 있으면 재사용.
        # 예: printf, malloc 등은 컴파일러 초기화 때 이미 declare 돼있음.
        existing = self.module.globals.get(name)
        if existing is not None:
            self.functions[name] = existing
            # ref_kinds는 전부 None (C 함수에는 참조 의미 없음)
            self.fn_param_ref_kinds[name] = [None] * len(params)
            self.fn_is_vararg[name] = is_vararg
            return
        
        if name in self.functions:
            raise DanhaRuntimeError(f"함수 이름 중복: {name}", line=line, source=self._source_code)
        
        llvm_param_types = []
        param_ref_kinds = []
        for i, pt_node in enumerate(param_types_nodes):
            llvm_ty, ref_kind = self._resolve_type(
                pt_node, f"매개변수 '{params[i]}'", line
            )
            llvm_param_types.append(llvm_ty)
            param_ref_kinds.append(ref_kind)
        
        # extern fn은 반환타입 없으면 void (일반 fn은 i32 기본).
        # C 함수에서 void 반환은 매우 흔하므로(SDL_Quit, SDL_RenderPresent 등).
        if return_type_node is None:
            llvm_ret_type = ir.VoidType()
            ret_ref = None
        else:
            llvm_ret_type, ret_ref = self._resolve_type(
                return_type_node, f"extern 함수 '{name}' 반환", line
            )
        if ret_ref is not None:
            raise DanhaTypeError(
                f"extern 함수 '{name}' 반환 타입에 참조는 허용 안 됨",
                line=line, source=self._source_code
            )
        
        fn_ty = ir.FunctionType(llvm_ret_type, llvm_param_types, var_arg=is_vararg)
        fn = ir.Function(self.module, fn_ty, name=name)
        for arg, pname in zip(fn.args, params):
            arg.name = pname
        
        self.functions[name] = fn
        self.fn_param_ref_kinds[name] = param_ref_kinds
        self.fn_is_vararg[name] = is_vararg
    
    def _declare_function(self, node):
        """1단계: 사용자 함수의 ir.Function을 만들어 self.functions에 등록.
        
        7.2.1b 이후 AST 형태:
          ('FnDef', name, params, body, param_types, return_type, line)
        
        param_types[i]: 타입 AST 노드 또는 None (참조 정보 포함)
        return_type:    타입 AST 노드 또는 None
        
        참조 매개변수는 _resolve_type이 이미 포인터 타입을 돌려주고,
        ref_kind('ref'/'mut_ref')는 호출 사이트 검사용으로 별도 수집.
        """
        name = node[1]
        params = node[2]
        param_types = node[4]
        return_type = node[5]
        type_params = node[6]  # 8.5: 제네릭 타입 매개변수 리스트 (비어있으면 비제네릭)
        line = node[-1]
        
        # 8.5: 제네릭 함수면 LLVM 함수를 바로 만들지 않고 AST만 저장
        if type_params:
            if not hasattr(self, '_generic_fns'):
                self._generic_fns = {}
            self._generic_fns[name] = node
            return
        
        if name in self.functions:
            raise DanhaRuntimeError(f"함수 이름 중복: {name}", line=line, source=self._source_code)
        
        # 7.2.1b: 단일 진입점 _resolve_type으로 모든 타입 해석.
        # 참조면 이미 포인터로 래핑된 LLVM 타입이 나오고, ref_kind는 별도로 돌려줌.
        llvm_param_types = []
        param_ref_kinds = []
        for i, pt_node in enumerate(param_types):
            llvm_ty, ref_kind = self._resolve_type(
                pt_node, f"매개변수 '{params[i]}'", line
            )
            llvm_param_types.append(llvm_ty)
            param_ref_kinds.append(ref_kind)
        
        llvm_ret_type, ret_ref = self._resolve_type(
            return_type, f"함수 '{name}' 반환", line
        )
        # 반환 타입에 참조는 파서가 거부해서 여기 오면 안 됨. 방어적 검사.
        if ret_ref is not None:
            raise DanhaTypeError(
                f"함수 '{name}' 반환 타입에 참조는 허용 안 됨 (7.1 결정)",
                line=line, source=self._source_code
            )
        
        fn_ty = ir.FunctionType(llvm_ret_type, llvm_param_types)
        fn = ir.Function(self.module, fn_ty, name=name)
        # 매개변수에 이름 붙이기 (디버깅용)
        for arg, pname in zip(fn.args, params):
            arg.name = pname
        # 인라인 힌트: 옵티마이저가 비용을 보고 인라인 여부를 결정할 때 선호하도록.
        # alwaysinline과 달리 강제가 아니라 힌트 — 대형 함수도 안전하게 적용 가능.
        fn.attributes.add('inlinehint')

        self.functions[name] = fn
        # 7.1.3: 호출 사이트가 읽기/쓰기 참조를 시그니처와 맞추는지 검사할 때 씀.
        self.fn_param_ref_kinds[name] = param_ref_kinds
    
    def _instantiate_generic(self, name, call_args, line):
        """8.5: 제네릭 함수를 호출 인자 타입에 맞게 단형화.
        
        1. 인자를 먼저 컴파일해서 LLVM 타입을 얻음
        2. 타입 매개변수 → 실제 LLVM 타입 매핑 생성
        3. 매핑을 적용해 단형화된 함수를 선언+컴파일 (캐싱)
        4. 호출
        """
        gnode = self._generic_fns[name]
        params = gnode[2]
        param_types = gnode[4]
        return_type = gnode[5]
        type_params = gnode[6]
        body = gnode[3]
        
        # 인자 먼저 컴파일
        arg_values = [self.compile_expr(a) for a in call_args]
        
        if len(arg_values) != len(params):
            raise DanhaValueError(
                f"{name}은(는) {len(params)}개의 인자가 필요한데 "
                f"{len(call_args)}개가 들어왔어",
                line=line, source=self._source_code
            )
        
        # 타입 매개변수 매핑 추론: T → i32/f64/i8* 등
        type_map = {}
        for i, pt_node in enumerate(param_types):
            if pt_node is not None and pt_node[0] == 'TypeName' and pt_node[1] in type_params:
                tp_name = pt_node[1]
                actual_ty = arg_values[i].type
                if tp_name in type_map:
                    if type_map[tp_name] != actual_ty:
                        raise DanhaTypeError(
                            f"타입 매개변수 '{tp_name}'이 일관되지 않아: "
                            f"{type_map[tp_name]} vs {actual_ty}",
                            line=line, source=self._source_code
                        )
                else:
                    type_map[tp_name] = actual_ty
        
        # 단형화 이름 생성: max_i32, max_f64 등
        suffix = '_'.join(str(type_map.get(tp, '?')).replace(' ', '') for tp in type_params)
        mono_name = f"{name}_{suffix}"
        
        # 이미 단형화된 함수가 있으면 재사용
        if mono_name not in self.functions:
            # 타입 노드에서 타입 매개변수를 실제 타입으로 치환
            def resolve_with_map(type_node):
                if type_node is None:
                    return i32  # 기본
                if type_node[0] == 'TypeName' and type_node[1] in type_map:
                    return type_map[type_node[1]]
                return self._resolve_type(type_node, f"제네릭 {name}", line)[0]
            
            llvm_param_types = [resolve_with_map(pt) for pt in param_types]
            llvm_ret_type = resolve_with_map(return_type)
            
            fn_ty = ir.FunctionType(llvm_ret_type, llvm_param_types)
            fn = ir.Function(self.module, fn_ty, name=mono_name)
            for arg, pname in zip(fn.args, params):
                arg.name = pname
            
            self.functions[mono_name] = fn
            self.fn_param_ref_kinds[mono_name] = [None] * len(params)
            
            # 본문 컴파일 — 현재 상태 저장 후 복원
            saved_fn = self.current_fn
            saved_builder = self.builder
            saved_vars = self.vars
            
            self.current_fn = fn
            entry = fn.append_basic_block(name="entry")
            self.builder = ir.IRBuilder(entry)
            self.vars = [{}]
            
            # 매개변수를 alloca에 저장
            for arg, pname in zip(fn.args, params):
                slot = self.builder.alloca(arg.type, name=pname)
                self.builder.store(arg, slot)
                self._declare_var(pname, slot)
            
            # 본문 컴파일
            for stmt in body[1]:
                self.compile_stmt(stmt)
            
            # 반환문이 없었으면 기본 반환 추가
            if not self.builder.block.is_terminated:
                if isinstance(llvm_ret_type, ir.VoidType):
                    self.builder.ret_void()
                elif isinstance(llvm_ret_type, ir.PointerType):
                    self.builder.ret(ir.Constant(llvm_ret_type, None))
                else:
                    self.builder.ret(ir.Constant(llvm_ret_type, 0))
            
            # 상태 복원
            self.current_fn = saved_fn
            self.builder = saved_builder
            self.vars = saved_vars
        
        fn = self.functions[mono_name]
        
        # 인자 타입 승격 (i32 → f64)
        for i, (av, param) in enumerate(zip(arg_values, fn.args)):
            if av.type != param.type:
                if av.type == i32 and param.type == f64:
                    arg_values[i] = self.builder.sitofp(av, f64, name="argpromoted")
        
        if isinstance(fn.function_type.return_type, ir.VoidType):
            return self.builder.call(fn, arg_values)
        return self.builder.call(fn, arg_values, name="calltmp")
    
    def _declare_impl(self, node):
        """('Impl', type_name, [method_node, ...], line)
        
        impl 블록 안의 각 메서드를 LLVM 함수로 등록.
        메서드 node는 ('FnDef', name, params, body, param_types, return_type, line) 형태.
        params[0]은 'self'.
        
        LLVM 함수 이름은 'StructName_methodName' 형태로 만들어 일반 함수와 구분.
        첫 인자는 구조체 포인터 (%struct.Player*) — 인터프리터의 self 의미와 같음
        (메서드가 self 필드를 바꾸면 원본이 바뀜).
        """
        type_name = node[1]
        methods = node[2]
        line = node[-1]
        
        if type_name not in self.structs:
            raise DanhaNameError(f"정의되지 않은 구조체야: {type_name}", line=line, source=self._source_code)
        struct_ty = self.structs[type_name][0]
        struct_ptr_ty = struct_ty.as_pointer()
        
        for method in methods:
            method_name = method[1]
            method_params = method[2]
            param_types = method[4]
            return_type = method[5]
            mline = method[-1]
            
            # 7.2.1b: 단일 진입점 _resolve_type으로 모든 타입 해석.
            # self(첫 매개변수)는 특수 케이스 — 항상 구조체 포인터.
            # self의 타입 어노테이션은 설령 붙어도 무시 (의미가 정해져 있음).
            llvm_param_types = [struct_ptr_ty]
            param_ref_kinds = [None]  # self 자리
            for i, pt_node in enumerate(param_types[1:], start=1):
                llvm_ty, ref_kind = self._resolve_type(
                    pt_node, f"매개변수 '{method_params[i]}'", mline
                )
                llvm_param_types.append(llvm_ty)
                param_ref_kinds.append(ref_kind)
            
            # 7.1.2: 메서드 반환 타입도 구조체 허용. 참조는 거부 (7.1 결정).
            llvm_ret_ty, ret_ref = self._resolve_type(
                return_type, f"메서드 '{type_name}.{method_name}' 반환", mline
            )
            if ret_ref is not None:
                raise DanhaTypeError(
                    f"메서드 '{type_name}.{method_name}' 반환 타입에 "
                    f"참조는 허용 안 됨 (7.1 결정)",
                    line=mline, source=self._source_code
                )
            
            fn_ty = ir.FunctionType(llvm_ret_ty, llvm_param_types)
            llvm_name = f"{type_name}_{method_name}{self._struct_suffix}"
            fn = ir.Function(self.module, fn_ty, name=llvm_name)
            for arg, pname in zip(fn.args, method_params):
                arg.name = pname
            
            # 같은 키 덮어쓰기 허용 (인터프리터와 같은 정책: 뒤에 나온 게 이김).
            # 단, 같은 LLVM 함수를 두 번 만들면 LLVM이 거부할 수 있어서, 덮어쓸 땐
            # 옛 LLVM 함수는 그냥 모듈 안에 죽은 채로 둠 (호출되지 않으면 무해).
            self.methods[(type_name, method_name)] = fn
            # 7.1.3: 호출 사이트 검사용.
            self.method_param_ref_kinds[(type_name, method_name)] = param_ref_kinds
    
    def _compile_impl_bodies(self, node):
        """impl의 각 메서드 본문을 컴파일.
        구조는 _compile_function_body와 거의 같지만 self 처리가 추가됨.
        self는 함수의 첫 인자(포인터)를 슬롯에 store해서 일반 변수처럼 다룸.
        _lvalue_struct가 'slot의 element가 포인터'인 케이스를 자동 처리.
        """
        type_name = node[1]
        methods = node[2]
        
        for method in methods:
            method_name = method[1]
            method_params = method[2]
            body = method[3]
            # 7.2.1b: method 튜플에서 param_ref_kinds 제거됨.
            # 선언 단계(_declare_impl)에서 self.method_param_ref_kinds에 저장한 걸 꺼내 씀.
            method_ref_kinds = self.method_param_ref_kinds.get(
                (type_name, method_name), [None] * len(method_params)
            )
            
            fn = self.methods[(type_name, method_name)]
            entry_block = fn.append_basic_block(name="entry")
            
            self.current_fn = fn
            self.builder = ir.IRBuilder(entry_block)
            self.vars = [{}]
            self._saw_return = False  # 7.1.2: 명시적 return 추적
            # 7.1.3: self는 메서드가 자동 가변(self.field 쓰기 허용) — readonly에 안 담음.
            # 나머지 매개변수 중 'ref'만 담음.
            self._readonly_params = set()
            for pname, pref in zip(method_params[1:], method_ref_kinds[1:]):
                if pref == 'ref':
                    self._readonly_params.add(pname)
            # 7.15d: 메서드 경계도 함수와 동일 — 자기만의 loop_stack에서 시작.
            _saved_loop_stack = self._loop_stack
            self._loop_stack = []
            
            # 모든 매개변수를 alloca 슬롯에. self도 마찬가지 — 슬롯의 element type이
            # 포인터(%struct.Player*)가 됨. self.field 접근 시 _lvalue_struct가
            # 한 번 load해서 진짜 구조체 주소를 얻음.
            for arg, pname in zip(fn.args, method_params):
                slot = self.builder.alloca(arg.type, name=pname)
                self.builder.store(arg, slot)
                self._declare_var(pname, slot)
            
            try:
                self.compile_stmt(body)
            finally:
                # 7.15d: loop_stack 복원 — 본문 안에서 에러가 나도 새지 않게
                self._loop_stack = _saved_loop_stack
            
            if not self.builder.block.is_terminated:
                ret_ty = fn.function_type.return_type
                if ret_ty == f64:
                    self.builder.ret(ir.Constant(f64, 0.0))
                elif ret_ty == i32 or ret_ty == i1:
                    self.builder.ret(ir.Constant(ret_ty, 0))
                else:
                    # 7.1.2: 구조체 반환 메서드도 명시적 return 강제.
                    # 도달 불가능 블록(return 뒤)이면 unreachable로 정리.
                    if not self._saw_return:
                        raise DanhaRuntimeError(
                            f"[{method[-1]}번째 줄] 메서드 '{type_name}.{method_name}'은(는) "
                            f"구조체를 반환하니 본문이 명시적 'return'으로 끝나야 해"
                        )
                    self.builder.unreachable()

            # Performance: alwaysinline 적용.
            if len(fn.basic_blocks) <= 4:
                if 'inlinehint' in fn.attributes:
                    fn.attributes.remove('inlinehint')
                fn.attributes.add('alwaysinline')
    
    
    # ================================================================
    # 7.8b: trait impl 선언 + 본문 컴파일
    # ================================================================
    
    def _declare_impl_trait(self, node):
        """('ImplTrait', trait_name, type_name, methods, line)
        
        트레잇의 기본 구현 + impl에서 제공한 메서드를 타입에 등록.
        _declare_impl과 같은 방식으로 LLVM 함수를 만듦.
        """
        trait_name = node[1]
        type_name = node[2]
        impl_methods = node[3]
        line = node[-1]
        
        if trait_name not in self._traits:
            raise DanhaNameError(f"정의되지 않은 트레잇이야: {trait_name}", line=line, source=self._source_code)
        if type_name not in self.structs:
            raise DanhaNameError(f"정의되지 않은 구조체야: {type_name}", line=line, source=self._source_code)
        
        # impl에서 명시적으로 제공한 메서드 이름 목록
        impl_method_names = {m[1] for m in impl_methods}
        
        # 트레잇의 기본 구현 중 impl에서 덮어쓰지 않은 것만 등록
        trait_method_nodes = self._traits[trait_name]
        all_methods = []
        for tm in trait_method_nodes:
            if tm[1] not in impl_method_names:
                all_methods.append(tm)
        # impl에서 제공한 메서드 (덮어쓰기)
        all_methods.extend(impl_methods)
        
        # 기존 _declare_impl과 동일한 방식으로 등록
        fake_impl = ('Impl', type_name, all_methods, line)
        self._declare_impl(fake_impl)
    
    def _compile_impl_trait_bodies(self, node):
        """ImplTrait의 메서드 본문 컴파일. _compile_impl_bodies와 동일."""
        trait_name = node[1]
        type_name = node[2]
        impl_methods = node[3]
        line = node[-1]
        
        impl_method_names = {m[1] for m in impl_methods}
        trait_method_nodes = self._traits[trait_name]
        
        all_methods = []
        for tm in trait_method_nodes:
            if tm[1] not in impl_method_names:
                all_methods.append(tm)
        all_methods.extend(impl_methods)
        
        fake_impl = ('Impl', type_name, all_methods, line)
        self._compile_impl_bodies(fake_impl)
    
    # ================================================================
    # 29단계: 동적 디스패치 (vtable)
    # ================================================================
    
    def _build_vtables(self, statements):
        """모든 ImplTrait에 대해 vtable을 LLVM 글로벌 상수로 생성.
        
        vtable은 함수 포인터 배열이다. 예:
          trait Drawable { fn draw(self); fn area(self) -> f64 }
          impl Drawable for Circle { ... }
        → vtable_Drawable_Circle = [Circle_draw, Circle_area] (i8* 타입)
        
        모든 함수 포인터를 i8*로 통일해서 저장하고,
        호출 시점에 실제 타입으로 bitcast해서 indirect call한다.
        """
        # trait별 메서드 순서 고정
        self._trait_method_order = {}
        for trait_name, method_nodes in self._traits.items():
            self._trait_method_order[trait_name] = [m[1] for m in method_nodes]
        
        # vtable 저장소: (trait_name, type_name) → LLVM 글로벌 변수
        self._vtables = {}
        # impl 매핑: trait_name → [type_name, ...] (어떤 타입들이 이 trait를 구현하는지)
        self._trait_impls = {}
        
        for stmt in statements:
            if stmt[0] != 'ImplTrait':
                continue
            trait_name = stmt[1]
            type_name = stmt[2]
            
            if trait_name not in self._trait_method_order:
                continue  # 에러는 _declare_impl_trait에서 이미 잡음
            
            method_order = self._trait_method_order[trait_name]
            n_methods = len(method_order)
            
            # 이 type에 대한 각 메서드의 LLVM 함수 포인터를 수집
            fn_ptrs = []
            for mname in method_order:
                key = (type_name, mname)
                if key not in self.methods:
                    # 기본 구현이 있으면 _declare_impl_trait에서 등록했을 것
                    raise DanhaNameError(
                        f"vtable 생성 실패: {type_name}에 {mname} 메서드가 없어",
                        line=stmt[-1], source=self._source_code
                    )
                fn = self.methods[key]
                # 함수 포인터를 i8*로 bitcast
                fn_as_i8ptr = fn.bitcast(i8.as_pointer())
                fn_ptrs.append(fn_as_i8ptr)
            
            # 글로벌 상수 배열: [n x i8*]
            vtable_arr_ty = ir.ArrayType(i8.as_pointer(), n_methods)
            vtable_const = ir.Constant(vtable_arr_ty, fn_ptrs)
            vtable_global = ir.GlobalVariable(
                self.module, vtable_arr_ty,
                name=f"vtable_{trait_name}_{type_name}"
            )
            vtable_global.initializer = vtable_const
            vtable_global.global_constant = True
            vtable_global.linkage = 'private'
            
            self._vtables[(trait_name, type_name)] = vtable_global
            
            if trait_name not in self._trait_impls:
                self._trait_impls[trait_name] = []
            self._trait_impls[trait_name].append(type_name)
    
    def _compile_dyn_method_call(self, var_name, method_name, args, line):
        """29: dyn 객체의 메서드를 vtable을 통해 호출.
        
        dyn 값은 {i8*, i8*} 팻 포인터. 
        [0] = data_ptr (구조체 데이터를 가리키는 i8*)
        [1] = vtable_ptr (함수 포인터 배열의 시작 주소 i8*)
        
        vtable에서 메서드 인덱스에 해당하는 함수 포인터를 꺼내고,
        실제 함수 타입으로 bitcast한 뒤 indirect call한다.
        """
        trait_name, type_name = self._dyn_var_meta[var_name]
        
        if trait_name not in self._trait_method_order:
            raise DanhaNameError(
                f"정의되지 않은 트레잇이야: {trait_name}",
                line=line, source=self._source_code
            )
        
        method_order = self._trait_method_order[trait_name]
        if method_name not in method_order:
            raise DanhaNameError(
                f"{trait_name} 트레잇에 '{method_name}' 메서드가 없어",
                line=line, source=self._source_code
            )
        method_idx = method_order.index(method_name)
        
        # 변수에서 dyn 팻 포인터 로드
        slot = self._lookup_var(var_name)
        dyn_val = self.builder.load(slot, name="dyn_load")
        
        # data_ptr, vtable_ptr 추출
        data_ptr = self.builder.extract_value(dyn_val, 0, name="data_ptr")
        vtable_ptr = self.builder.extract_value(dyn_val, 1, name="vtable_ptr")
        
        # vtable_ptr를 [N x i8*]* 로 bitcast
        n_methods = len(method_order)
        vtable_arr_ty = ir.ArrayType(i8.as_pointer(), n_methods)
        vtable_arr_ptr = self.builder.bitcast(
            vtable_ptr, vtable_arr_ty.as_pointer(), name="vtable_arr"
        )
        
        # vtable[method_idx]에서 함수 포인터(i8*) 로드
        idx_ptr = self.builder.gep(
            vtable_arr_ptr,
            [ir.Constant(i32, 0), ir.Constant(i32, method_idx)],
            inbounds=True, name=f"vt_{method_name}_ptr"
        )
        fn_i8ptr = self.builder.load(idx_ptr, name=f"vt_{method_name}")
        
        # 실제 함수 타입을 알아내서 bitcast
        # 메서드 시그니처: self(구조체 포인터) + 나머지 인자
        key = (type_name, method_name)
        if key not in self.methods:
            raise DanhaNameError(
                f"{type_name}에 '{method_name}' 메서드가 없어",
                line=line, source=self._source_code
            )
        real_fn = self.methods[key]
        fn_ty = real_fn.function_type
        fn_ptr_ty = fn_ty.as_pointer()
        
        fn_ptr = self.builder.bitcast(fn_i8ptr, fn_ptr_ty, name=f"fn_{method_name}")
        
        # data_ptr을 실제 구조체 포인터 타입으로 bitcast
        struct_ptr_ty = fn_ty.args[0]  # 첫 인자가 self (구조체 포인터)
        self_ptr = self.builder.bitcast(data_ptr, struct_ptr_ty, name="self_ptr")
        
        # 인자 컴파일
        call_args = [self_ptr]
        for i, arg_node in enumerate(args):
            av = self.compile_expr(arg_node)
            expected_ty = fn_ty.args[i + 1]  # +1: self 건너뛰기
            if av.type != expected_ty:
                if av.type == i32 and expected_ty == f64:
                    av = self.builder.sitofp(av, f64, name="argpromoted")
            call_args.append(av)
        
        # indirect call
        if isinstance(fn_ty.return_type, ir.VoidType):
            return self.builder.call(fn_ptr, call_args)
        return self.builder.call(fn_ptr, call_args, name="dyn_call")
    
    def _compile_dyn_method_call_by_trait(self, var_name, trait_name, method_name, args, line):
        """29: LLVM 타입으로 trait를 감지한 경우의 dyn 메서드 호출.
        
        _compile_dyn_method_call과 비슷하지만, 구체적 type_name을 모르므로
        trait의 메서드 시그니처에서 함수 타입을 구성한다.
        self 매개변수는 i8*로 통일 (어떤 구조체든 i8*로 bitcast해서 넘기니까).
        """
        if trait_name not in self._trait_method_order:
            raise DanhaNameError(
                f"정의되지 않은 트레잇이야: {trait_name}",
                line=line, source=self._source_code
            )
        
        method_order = self._trait_method_order[trait_name]
        if method_name not in method_order:
            raise DanhaNameError(
                f"{trait_name} 트레잇에 '{method_name}' 메서드가 없어",
                line=line, source=self._source_code
            )
        method_idx = method_order.index(method_name)
        
        # dyn 팻 포인터 로드
        slot = self._lookup_var(var_name)
        dyn_val = self.builder.load(slot, name="dyn_load")
        data_ptr = self.builder.extract_value(dyn_val, 0, name="data_ptr")
        vtable_ptr = self.builder.extract_value(dyn_val, 1, name="vtable_ptr")
        
        # vtable에서 함수 포인터 로드
        n_methods = len(method_order)
        vtable_arr_ty = ir.ArrayType(i8.as_pointer(), n_methods)
        vtable_arr_ptr = self.builder.bitcast(
            vtable_ptr, vtable_arr_ty.as_pointer(), name="vtable_arr"
        )
        idx_ptr = self.builder.gep(
            vtable_arr_ptr,
            [ir.Constant(i32, 0), ir.Constant(i32, method_idx)],
            inbounds=True, name=f"vt_{method_name}_ptr"
        )
        fn_i8ptr = self.builder.load(idx_ptr, name=f"vt_{method_name}")
        
        # trait의 메서드 시그니처에서 함수 타입 구성.
        # 이 trait를 구현한 아무 타입에서든 같은 시그니처를 갖고 있으니
        # 첫 번째 impl의 메서드 시그니처를 참조.
        fn_ty = None
        if trait_name in self._trait_impls:
            for impl_type_name in self._trait_impls[trait_name]:
                key = (impl_type_name, method_name)
                if key in self.methods:
                    real_fn = self.methods[key]
                    orig_fn_ty = real_fn.function_type
                    # self 매개변수를 i8*로 교체한 새 함수 타입
                    new_params = [i8.as_pointer()] + list(orig_fn_ty.args[1:])
                    fn_ty = ir.FunctionType(orig_fn_ty.return_type, new_params)
                    break
        
        if fn_ty is None:
            raise DanhaNameError(
                f"{trait_name}의 '{method_name}' 구현을 찾을 수 없어",
                line=line, source=self._source_code
            )
        
        fn_ptr = self.builder.bitcast(fn_i8ptr, fn_ty.as_pointer(), name=f"fn_{method_name}")
        
        # 인자: data_ptr(이미 i8*)을 self로 + 나머지 인자
        call_args = [data_ptr]
        for i, arg_node in enumerate(args):
            av = self.compile_expr(arg_node)
            expected_ty = fn_ty.args[i + 1]
            if av.type != expected_ty:
                if av.type == i32 and expected_ty == f64:
                    av = self.builder.sitofp(av, f64, name="argpromoted")
            call_args.append(av)
        
        if isinstance(fn_ty.return_type, ir.VoidType):
            return self.builder.call(fn_ptr, call_args)
        return self.builder.call(fn_ptr, call_args, name="dyn_call")
    
    # ================================================================
    # 7.13d: system 선언 + 본문 컴파일
    # ================================================================
    
    def _declare_system(self, node):
        """('SystemDef', name, params, param_types, bindings, body, is_parallel, line)
        
        system을 일반 LLVM 함수로 등록한다.
        매개변수는 FnDef와 동일하게 처리. 반환 타입은 항상 void.
        """
        name = node[1]
        params = node[2]
        param_types = node[3]
        bindings = node[4]
        is_parallel = node[6]
        line = node[-1]
        
        if name in self.functions:
            raise DanhaECSError(f"함수/시스템 이름 중복: {name}", line=line, source=self._source_code)
        
        # 7.11b: 중복 컴포넌트 바인딩 검사 (28단계: 4-tuple 호환)
        seen_comps = {}
        for binding in bindings:
            bind_var, comp_name = binding[0], binding[1]
            kind = binding[3] if len(binding) > 3 else 'required'
            if bind_var is not None and comp_name in seen_comps:
                raise DanhaECSError(
                    f"system '{name}'에서 컴포넌트 '{comp_name}'이(가) "
                    f"두 번 바인딩됐어 ('{seen_comps[comp_name]}'과 '{bind_var}')",
                    line=line, source=self._source_code
                )
            if bind_var is not None:
                seen_comps[comp_name] = bind_var
        
        # 바인딩에 사용된 컴포넌트가 실제로 정의돼 있는지 검사
        for binding in bindings:
            comp_name = binding[1]
            if comp_name not in self.components:
                raise DanhaECSError(
                    f"system '{name}'에서 사용하는 "
                    f"컴포넌트 '{comp_name}'이(가) 정의되지 않았어",
                    line=line, source=self._source_code
                )
        
        # 매개변수 LLVM 타입 해석
        llvm_param_types = []
        param_ref_kinds = []
        for i, pt_node in enumerate(param_types):
            llvm_ty, ref_kind = self._resolve_type(
                pt_node, f"매개변수 '{params[i]}'", line
            )
            llvm_param_types.append(llvm_ty)
            param_ref_kinds.append(ref_kind)
        
        # system은 항상 void 반환
        fn_ty = ir.FunctionType(ir.VoidType(), llvm_param_types)
        fn = ir.Function(self.module, fn_ty, name=f"_danha_sys_{name}")
        for arg, pname in zip(fn.args, params):
            arg.name = pname
        
        self.functions[name] = fn
        self.fn_param_ref_kinds[name] = param_ref_kinds
        # system 메타데이터: 본문 컴파일 때 바인딩 정보가 필요
        self._system_meta = getattr(self, '_system_meta', {})
        
        # 7.11b: 바인딩 안전성 분석 (중복 검사 + 읽기/쓰기 분류)
        body = node[5]
        access_map = self._analyze_system_access(name, bindings, body, line)
        
        # 7.14a: 컴포넌트 이름 기준 접근 맵 생성 (28단계: exclude 제외)
        comp_access_map = {}
        for binding in bindings:
            bind_var, comp_name = binding[0], binding[1]
            kind = binding[3] if len(binding) > 3 else 'required'
            if kind != 'exclude' and bind_var is not None:
                comp_access_map[comp_name] = access_map.get(bind_var, 'read')
        
        self._system_meta[name] = {
            'bindings': bindings,
            'is_parallel': is_parallel,
            'access_map': access_map,
            'comp_access_map': comp_access_map,
        }
    
    def _analyze_system_access(self, sys_name, bindings, body, line):
        """7.15e: 시그니처가 진실의 원천. 본문 스캔은 검증 도구.
        
        - 시그니처 &mut → write (무조건)
        - 시그니처 &   → read (본문 쓰면 경고)
        - 시그니처 생략 → 본문 스캔으로 결정 (기존 호환)
        """
        bind_vars = {b[0] for b in bindings if b[0] is not None}
        written_vars = set()
        self._collect_written_vars_ast(body, bind_vars, written_vars)
        
        access_map = {}
        for binding in bindings:
            bind_var, comp_name, declared_access = binding[0], binding[1], binding[2]
            kind = binding[3] if len(binding) > 3 else 'required'
            if kind == 'exclude':
                continue  # exclude는 변수 없음
            body_writes = bind_var in written_vars
            if declared_access == 'write':
                mode = 'write'
            elif declared_access == 'read':
                if body_writes:
                    import sys as _sys
                    print(
                        f"[{line}번째 줄] 경고: system '{sys_name}'의 바인딩 "
                        f"'{bind_var}: &{comp_name}'은(는) 읽기로 선언됐는데 본문에 쓰기가 있어. "
                        f"'&mut {comp_name}'로 바꾸거나 본문의 쓰기를 제거해.",
                        file=_sys.stderr,
                    )
                mode = 'read'
            else:
                mode = 'write' if body_writes else 'read'
            access_map[bind_var] = mode
        return access_map
    
    def _collect_written_vars_ast(self, node, bind_vars, written_vars):
        """AST를 재귀 순회하며 FieldAssign의 대상이 bind_vars에 속하면 written_vars에 추가."""
        if not isinstance(node, tuple) or len(node) == 0:
            return
        node_type = node[0]
        if node_type == 'FieldAssign':
            obj = node[1]
            if isinstance(obj, tuple) and obj[0] == 'Name' and obj[1] in bind_vars:
                written_vars.add(obj[1])
            self._collect_written_vars_ast(node[3], bind_vars, written_vars)
            return
        for child in node:
            if isinstance(child, tuple):
                self._collect_written_vars_ast(child, bind_vars, written_vars)
            elif isinstance(child, list):
                for item in child:
                    if isinstance(item, tuple):
                        self._collect_written_vars_ast(item, bind_vars, written_vars)
    
    def _schedule_systems_topo(self, line):
        """7.14b + 문제 4 해결: 등록된 system들을 토폴로지 정렬.
        
        규칙:
        - 같은 컴포넌트를 두 system이 모두 쓰면 → 에러 (writer/writer 충돌)
        - A가 X를 쓰고 B가 X를 읽고, 반대 방향(B→A)이 없으면 → A 먼저
        - 양방향 읽기/쓰기 교차 → 순환이 아니라 "상호 참조" 로 보고 등록 순서 유지
        반환: 정렬된 system 이름 리스트.
        """
        names = list(self._system_meta.keys())
        if len(names) <= 1:
            return names
        
        # 0) writer/writer 충돌 검사 — 두 system이 같은 컴포넌트에 모두 쓰면 거부
        for i, a_name in enumerate(names):
            a_map = self._system_meta[a_name]['comp_access_map']
            for j in range(i + 1, len(names)):
                b_name = names[j]
                b_map = self._system_meta[b_name]['comp_access_map']
                shared_writes = [
                    c for c, m in a_map.items()
                    if m == 'write' and b_map.get(c) == 'write'
                ]
                if shared_writes:
                    raise DanhaECSError(
                        f"system '{a_name}'와(과) '{b_name}'이(가) "
                        f"같은 컴포넌트 {shared_writes}에 모두 쓰려고 해. "
                        f"둘 중 하나만 쓰도록 고치거나, 로직을 한 system으로 합쳐줘.",
                        line=line, source=self._source_code
                    )
        
        # 1) 의존 간선 수집: writer → reader, 단방향일 때만
        edges = []
        for i, a_name in enumerate(names):
            a_map = self._system_meta[a_name]['comp_access_map']
            for j, b_name in enumerate(names):
                if i == j:
                    continue
                b_map = self._system_meta[b_name]['comp_access_map']
                a_writes_b_reads = any(
                    m == 'write' and b_map.get(c) == 'read'
                    for c, m in a_map.items()
                )
                if not a_writes_b_reads:
                    continue
                # 반대 방향도 있으면 상호 참조 → 간선 생략
                b_writes_a_reads = any(
                    m == 'write' and a_map.get(c) == 'read'
                    for c, m in b_map.items()
                )
                if b_writes_a_reads:
                    continue
                edges.append((a_name, b_name))
        
        # 2) Kahn's algorithm
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
        
        if len(result) != len(names):
            remaining = [n for n in names if n not in result]
            raise DanhaECSError(
                f"system 간 순환 의존이 발견됐어: {remaining}. "
                f"단방향 write→read 체인이 순환을 만들고 있어.",
                line=line, source=self._source_code
            )
        
        return result
    
    def _compile_system_body(self, node):
        """system 본문 컴파일 — for each 루프를 SoA 순회 코드로 변환.
        
        전략:
        1. 교집합 순회: pivot 컴포넌트(가장 작은 count)의 dense 배열을 0..count 루프
        2. 각 반복에서 pivot의 dense_to_entity[i]로 entity_idx를 얻음
        3. 나머지 컴포넌트에 대해 sparse[entity_idx] != -1 검사 (있는지 확인)
        4. 있으면: 각 바인딩 변수에 SoA 필드 alloca를 만들고 값을 로드
        5. 본문 실행
        6. 바인딩 변수의 값을 SoA에 다시 store (쓰기 반영)
        
        7.16f: parallel system이면 worker 함수 + pthread fork-join으로 진짜 병렬 실행.
        """
        name = node[1]
        params = node[2]
        body = node[5]  # Block 노드
        line = node[-1]
        meta = self._system_meta[name]
        bindings = meta['bindings']
        is_parallel = meta.get('is_parallel', False)
        
        if is_parallel:
            self._compile_parallel_system_body(node)
        else:
            self._compile_sequential_system_body(node)
    
    def _build_foreach_loop(self, fn, params, body, bindings, meta, start_val, end_val):
        """for each 루프 본체 IR 생성 — sequential과 parallel worker 양쪽에서 재사용.
        
        start_val, end_val: 순회할 dense 인덱스 범위 [start, end)
        현재 builder 위치에서 이어 생성하고, end_bb에서 builder를 세팅한 채로 반환.
        """
        # 28단계: pivot = 첫 번째 required 바인딩 (optional/exclude 제외)
        pivot_comp_name = next(
            (b[1] for b in bindings if (b[3] if len(b) > 3 else 'required') == 'required'),
            bindings[0][1]  # fallback: 모두 optional이면 첫 번째
        )
        pivot = self.components[pivot_comp_name]
        access_map = meta.get('access_map', {})
        
        idx_slot = self.builder.alloca(i32, name="foreach_idx")
        self.builder.store(start_val, idx_slot)
        
        cond_bb = fn.append_basic_block(name="foreach_cond")
        body_bb = fn.append_basic_block(name="foreach_body")
        skip_bb = fn.append_basic_block(name="foreach_skip")
        inc_bb  = fn.append_basic_block(name="foreach_inc")
        end_bb  = fn.append_basic_block(name="foreach_end")
        
        self.builder.branch(cond_bb)
        
        # --- cond ---
        self.builder = ir.IRBuilder(cond_bb)
        idx_val = self.builder.load(idx_slot, name="idx")
        cmp = self.builder.icmp_signed('<', idx_val, end_val, name="cond")
        self.builder.cbranch(cmp, body_bb, end_bb)
        
        # --- body ---
        self.builder = ir.IRBuilder(body_bb)
        idx_val = self.builder.load(idx_slot, name="idx_body")
        
        d2e_ptr = self.builder.load(pivot['dense_to_entity'], name="d2e_ptr")
        eidx_gep = self.builder.gep(d2e_ptr, [idx_val], inbounds=True, name="eidx_gep")
        entity_idx = self.builder.load(eidx_gep, name="entity_idx")
        
        # required 컴포넌트 교집합 검사 (28단계: exclude는 별도 처리)
        if len(bindings) > 1:
            for bi in range(1, len(bindings)):
                binding = bindings[bi]
                comp_name = binding[1]
                kind = binding[3] if len(binding) > 3 else 'required'
                if kind == 'exclude':
                    # exclude: sparse != -1 이면 skip (있으면 건너뜀)
                    comp = self.components[comp_name]
                    sparse_ptr = self.builder.load(comp['sparse'], name=f"sparse_{comp_name}")
                    sparse_gep = self.builder.gep(sparse_ptr, [entity_idx], inbounds=True,
                                                   name=f"sparse_gep_{comp_name}")
                    sparse_val = self.builder.load(sparse_gep, name=f"sparse_val_{comp_name}")
                    has_cmp = self.builder.icmp_signed('==', sparse_val,
                                                        ir.Constant(i32, -1),
                                                        name=f"not_{comp_name}")
                    next_check_bb = fn.append_basic_block(name=f"excl_ok_{comp_name}")
                    self.builder.cbranch(has_cmp, next_check_bb, skip_bb)
                    self.builder = ir.IRBuilder(next_check_bb)
                elif kind == 'optional':
                    pass  # optional은 교집합 검사 안 함 — 바인딩 시 null 처리
                else:
                    # required: sparse != -1 이어야 함
                    comp = self.components[comp_name]
                    sparse_ptr = self.builder.load(comp['sparse'], name=f"sparse_{comp_name}")
                    sparse_gep = self.builder.gep(sparse_ptr, [entity_idx], inbounds=True,
                                                   name=f"sparse_gep_{comp_name}")
                    sparse_val = self.builder.load(sparse_gep, name=f"sparse_val_{comp_name}")
                    has_cmp = self.builder.icmp_signed('!=', sparse_val,
                                                        ir.Constant(i32, -1),
                                                        name=f"has_{comp_name}")
                    next_check_bb = fn.append_basic_block(name=f"has_ok_{comp_name}")
                    self.builder.cbranch(has_cmp, next_check_bb, skip_bb)
                    self.builder = ir.IRBuilder(next_check_bb)
        
        binding_slots = []  # (slot, comp_name, didx_slot, kind)
        for binding in bindings:
            bind_var, comp_name = binding[0], binding[1]
            kind = binding[3] if len(binding) > 3 else 'required'
            if kind == 'exclude':
                continue  # exclude는 변수 없음 — 슬롯 불필요

            comp = self.components[comp_name]
            field_names = comp['fields']
            comp_val_ty = comp['value_type']

            if kind == 'optional':
                # optional: sparse 값 확인 → 없으면 null 플래그 세팅
                # LLVM에서 null은 i64 0으로 표현 (컴파일러 내 null 표현)
                # 여기서는 간단히: sparse 값으로 존재 여부 확인 후
                # has_flag 를 alloca로 만들고, 없으면 0 있으면 1 저장
                has_flag = self.builder.alloca(i32, name=f"opt_has_{bind_var}")
                sp_ptr = self.builder.load(comp['sparse'], name=f"opt_sp_{comp_name}")
                sp_gep = self.builder.gep(sp_ptr, [entity_idx], inbounds=True,
                                          name=f"opt_sp_gep_{comp_name}")
                sparse_val = self.builder.load(sp_gep, name=f"opt_sparse_{comp_name}")
                has_cmp = self.builder.icmp_signed('!=', sparse_val,
                                                   ir.Constant(i32, -1),
                                                   name=f"opt_has_cmp_{comp_name}")
                has_i32 = self.builder.zext(has_cmp, i32, name=f"opt_has_i32_{bind_var}")
                self.builder.store(has_i32, has_flag)

                # optional 바인딩 슬롯: 있을 때만 로드
                opt_fn = self.builder.block.parent
                opt_have_bb = opt_fn.append_basic_block(name=f"opt_have_{bind_var}")
                opt_skip_bb = opt_fn.append_basic_block(name=f"opt_skip_{bind_var}")
                opt_merge_bb = opt_fn.append_basic_block(name=f"opt_merge_{bind_var}")

                slot = self.builder.alloca(comp_val_ty, name=f"sys_{bind_var}")
                didx_slot = self.builder.alloca(i32, name=f"didx_slot_{bind_var}")
                self.builder.store(ir.Constant(i32, -1), didx_slot)

                self.builder.cbranch(has_cmp, opt_have_bb, opt_skip_bb)

                # have 분기: dense_idx 계산 + 필드 로드
                self.builder = ir.IRBuilder(opt_have_bb)
                dense_idx = self.builder.load(sp_gep, name=f"opt_didx_{comp_name}")
                self.builder.store(dense_idx, didx_slot)
                for fi, fname in enumerate(field_names):
                    fld_arr_ptr = self.builder.load(comp['field_globals'][fname],
                                                    name=f"opt_fld_{comp_name}_{fname}")
                    fld_gep = self.builder.gep(fld_arr_ptr, [dense_idx], inbounds=True,
                                               name=f"opt_fld_gep_{comp_name}_{fname}")
                    fld_val = self.builder.load(fld_gep, name=f"opt_fld_val_{comp_name}_{fname}")
                    field_ptr = self.builder.gep(slot,
                                                 [ir.Constant(i32, 0), ir.Constant(i32, fi)],
                                                 inbounds=True,
                                                 name=f"opt_slot_{bind_var}_{fname}")
                    self.builder.store(fld_val, field_ptr)
                self.builder.branch(opt_merge_bb)

                # skip 분기: slot 초기화 (0)
                self.builder = ir.IRBuilder(opt_skip_bb)
                self.builder.branch(opt_merge_bb)

                self.builder = ir.IRBuilder(opt_merge_bb)

                # 변수 등록: (slot, has_flag) 쌍으로 저장 — optional 표시
                self._declare_var(bind_var, slot)
                # has_flag를 별도 변수로 등록 (null 체크용)
                self._declare_var(f"__opt_has_{bind_var}", has_flag)
                binding_slots.append((slot, comp_name, didx_slot, 'optional', has_flag))
                continue

            # required
            slot = self.builder.alloca(comp_val_ty, name=f"sys_{bind_var}")

            if comp_name == pivot_comp_name:
                dense_idx = idx_val
            else:
                sp_ptr = self.builder.load(comp['sparse'], name=f"sp_{comp_name}")
                sp_gep = self.builder.gep(sp_ptr, [entity_idx], inbounds=True,
                                           name=f"sp_gep_{comp_name}")
                dense_idx = self.builder.load(sp_gep, name=f"didx_{comp_name}")

            didx_slot = self.builder.alloca(i32, name=f"didx_slot_{bind_var}")
            self.builder.store(dense_idx, didx_slot)

            for fi, fname in enumerate(field_names):
                fld_arr_ptr = self.builder.load(comp['field_globals'][fname],
                                                 name=f"fld_{comp_name}_{fname}")
                fld_gep = self.builder.gep(fld_arr_ptr, [dense_idx], inbounds=True,
                                            name=f"fld_gep_{comp_name}_{fname}")
                fld_val = self.builder.load(fld_gep, name=f"fld_val_{comp_name}_{fname}")
                field_ptr = self.builder.gep(slot,
                                              [ir.Constant(i32, 0), ir.Constant(i32, fi)],
                                              inbounds=True,
                                              name=f"slot_{bind_var}_{fname}")
                self.builder.store(fld_val, field_ptr)

            self._declare_var(bind_var, slot)
            binding_slots.append((slot, comp_name, didx_slot, 'required', None))
        
        self._loop_stack.append((inc_bb, end_bb))
        try:
            self.compile_stmt(body)
        finally:
            self._loop_stack.pop()
        
        if not self.builder.block.is_terminated:
            # write-back: binding_slots = (slot, comp_name, didx_slot, kind, has_flag)
            # 대응되는 bindings에서 bind_var를 찾아야 함
            # binding_slots에는 exclude가 없으므로 별도 인덱싱
            wb_bindings = [(b[0], b[1]) for b in bindings
                           if (b[3] if len(b) > 3 else 'required') != 'exclude']
            for (slot, comp_name, didx_slot, kind, has_flag), (bind_var, _) in zip(binding_slots, wb_bindings):
                if access_map.get(bind_var) == 'read':
                    continue
                if kind == 'optional':
                    # optional: has_flag가 1일 때만 write-back
                    hf_val = self.builder.load(has_flag, name=f"wb_hf_{bind_var}")
                    has_cmp = self.builder.icmp_signed('!=', hf_val, ir.Constant(i32, 0),
                                                        name=f"wb_hf_cmp_{bind_var}")
                    wb_fn = self.builder.block.parent
                    wb_do_bb = wb_fn.append_basic_block(name=f"wb_do_{bind_var}")
                    wb_skip_bb = wb_fn.append_basic_block(name=f"wb_skip_{bind_var}")
                    self.builder.cbranch(has_cmp, wb_do_bb, wb_skip_bb)
                    self.builder = ir.IRBuilder(wb_do_bb)
                    dense_idx = self.builder.load(didx_slot, name=f"wb_didx_{bind_var}")
                    comp = self.components[comp_name]
                    field_names = comp['fields']
                    for fi, fname in enumerate(field_names):
                        field_ptr = self.builder.gep(slot,
                                                      [ir.Constant(i32, 0), ir.Constant(i32, fi)],
                                                      inbounds=True, name=f"wb_{bind_var}_{fname}")
                        fld_val = self.builder.load(field_ptr, name=f"wb_val_{bind_var}_{fname}")
                        fld_arr_ptr = self.builder.load(comp['field_globals'][fname],
                                                         name=f"wb_arr_{comp_name}_{fname}")
                        fld_gep = self.builder.gep(fld_arr_ptr, [dense_idx], inbounds=True,
                                                    name=f"wb_gep_{comp_name}_{fname}")
                        self.builder.store(fld_val, fld_gep)
                    self.builder.branch(wb_skip_bb)
                    self.builder = ir.IRBuilder(wb_skip_bb)
                else:
                    # required write-back
                    comp = self.components[comp_name]
                    field_names = comp['fields']
                    dense_idx = self.builder.load(didx_slot, name=f"wb_didx_{bind_var}")
                    for fi, fname in enumerate(field_names):
                        field_ptr = self.builder.gep(slot,
                                                      [ir.Constant(i32, 0), ir.Constant(i32, fi)],
                                                      inbounds=True, name=f"wb_{bind_var}_{fname}")
                        fld_val = self.builder.load(field_ptr, name=f"wb_val_{bind_var}_{fname}")
                        fld_arr_ptr = self.builder.load(comp['field_globals'][fname],
                                                         name=f"wb_arr_{comp_name}_{fname}")
                        fld_gep = self.builder.gep(fld_arr_ptr, [dense_idx], inbounds=True,
                                                    name=f"wb_gep_{comp_name}_{fname}")
                        self.builder.store(fld_val, fld_gep)
            self.builder.branch(inc_bb)
        
        # --- skip ---
        self.builder = ir.IRBuilder(skip_bb)
        self.builder.branch(inc_bb)
        
        # --- inc ---
        self.builder = ir.IRBuilder(inc_bb)
        idx_now = self.builder.load(idx_slot, name="idx_inc")
        idx_next = self.builder.add(idx_now, ir.Constant(i32, 1), name="idx_next")
        self.builder.store(idx_next, idx_slot)
        self.builder.branch(cond_bb)
        
        # builder를 end_bb에 세팅하고 반환
        self.builder = ir.IRBuilder(end_bb)
    
    def _compile_sequential_system_body(self, node):
        """비병렬 system: 기존 순차 루프."""
        name = node[1]
        params = node[2]
        body = node[5]
        meta = self._system_meta[name]
        bindings = meta['bindings']
        
        fn = self.functions[name]
        entry_block = fn.append_basic_block(name="entry")
        
        self.current_fn = fn
        self.builder = ir.IRBuilder(entry_block)
        self.vars = [{}]
        self._saw_return = False
        self._readonly_params = set()
        _saved_loop_stack = self._loop_stack
        self._loop_stack = []
        
        for arg, pname in zip(fn.args, params):
            slot = self.builder.alloca(arg.type, name=pname)
            self.builder.store(arg, slot)
            self._declare_var(pname, slot)
        
        # 28단계: pivot = 첫 번째 required 바인딩
        pivot_comp_name = next(
            (b[1] for b in bindings if (b[3] if len(b) > 3 else 'required') == 'required'),
            bindings[0][1]
        )
        pivot = self.components[pivot_comp_name]
        
        start_val = ir.Constant(i32, 0)
        count_val = self.builder.load(pivot['count'], name="pivot_count")
        
        self._build_foreach_loop(fn, params, body, bindings, meta, start_val, count_val)
        
        self.builder.ret_void()
        self._loop_stack = _saved_loop_stack
    
    def _compile_parallel_system_body(self, node):
        """7.16f: parallel system — pthread fork-join으로 진짜 병렬 실행.
        
        전략:
        1. worker 함수 `_danha_worker_이름(start, end, 원래_인자들)` 를 먼저 컴파일
        2. pthread_arg 구조체 `{ start, end, 원래_인자들... }` 를 malloc으로 스레드마다 생성
        3. pthread_create로 코어 수만큼 스레드 생성
        4. pthread_join으로 모든 스레드 대기
        
        pthread의 start_routine은 void* → void* 형식이라
        인자를 구조체로 묶어 void*로 캐스팅해서 넘긴다.
        """
        name = node[1]
        params = node[2]
        body = node[5]
        meta = self._system_meta[name]
        bindings = meta['bindings']
        # 28단계: pivot = 첫 번째 required 바인딩
        pivot_comp_name = next(
            (b[1] for b in bindings if (b[3] if len(b) > 3 else 'required') == 'required'),
            bindings[0][1]
        )
        pivot = self.components[pivot_comp_name]
        
        # ---- pthread 관련 C 함수 등록 ----
        pthread_t_ty = ir.IntType(64)   # pthread_t = unsigned long (64bit Linux)
        void_ptr_ty  = i8p             # void*
        # pthread_create(thread*, attr, start_routine, arg) -> int
        start_routine_ty = ir.FunctionType(void_ptr_ty, [void_ptr_ty])
        start_fn_ptr_ty  = start_routine_ty.as_pointer()
        pthread_create_ty = ir.FunctionType(
            i32,
            [pthread_t_ty.as_pointer(), void_ptr_ty, start_fn_ptr_ty, void_ptr_ty]
        )
        if 'pthread_create' not in self.module.globals:
            self._pthread_create = ir.Function(self.module, pthread_create_ty,
                                               name="pthread_create")
        else:
            self._pthread_create = self.module.globals['pthread_create']
        
        # pthread_join(thread, retval*) -> int
        pthread_join_ty = ir.FunctionType(i32, [pthread_t_ty, void_ptr_ty])
        if 'pthread_join' not in self.module.globals:
            self._pthread_join = ir.Function(self.module, pthread_join_ty,
                                             name="pthread_join")
        else:
            self._pthread_join = self.module.globals['pthread_join']
        
        # sysconf(_SC_NPROCESSORS_ONLN=84) -> long (i64)
        i64 = ir.IntType(64)
        sysconf_ty = ir.FunctionType(i64, [i32])
        if 'sysconf' not in self.module.globals:
            self._sysconf = ir.Function(self.module, sysconf_ty, name="sysconf")
        else:
            self._sysconf = self.module.globals['sysconf']
        
        # ---- system 매개변수 LLVM 타입 목록 ----
        fn = self.functions[name]
        param_llvm_types = [arg.type for arg in fn.args]  # 원래 system 인자들
        
        # ---- worker 인자 구조체 타입: { i32 start, i32 end, 원래_인자들... } ----
        # 각 스레드에 넘길 데이터 덩어리.
        worker_arg_fields = [i32, i32] + param_llvm_types
        worker_arg_ty = ir.LiteralStructType(worker_arg_fields)
        
        # ---- worker 함수 정의 ----
        # void* worker(void* arg_ptr)
        worker_fn_ty = ir.FunctionType(void_ptr_ty, [void_ptr_ty])
        worker_fn = ir.Function(self.module, worker_fn_ty,
                                name=f"_danha_worker_{name}")
        worker_fn.args[0].name = "arg_ptr"
        
        worker_entry = worker_fn.append_basic_block(name="entry")
        
        # worker 본문 컴파일 컨텍스트 설정
        saved_fn     = self.current_fn
        saved_builder = self.builder
        saved_vars   = self.vars
        saved_loop   = self._loop_stack
        saved_readonly = self._readonly_params
        
        self.current_fn = worker_fn
        self.builder = ir.IRBuilder(worker_entry)
        self.vars = [{}]
        self._loop_stack = []
        self._readonly_params = set()
        
        # arg_ptr → worker_arg_ty* 로 캐스팅
        typed_ptr = self.builder.bitcast(worker_fn.args[0],
                                         worker_arg_ty.as_pointer(),
                                         name="typed_arg")
        
        # start, end 로드
        start_gep = self.builder.gep(typed_ptr, [ir.Constant(i32, 0), ir.Constant(i32, 0)],
                                      inbounds=True, name="start_ptr")
        end_gep   = self.builder.gep(typed_ptr, [ir.Constant(i32, 0), ir.Constant(i32, 1)],
                                      inbounds=True, name="end_ptr")
        start_val = self.builder.load(start_gep, name="start")
        end_val   = self.builder.load(end_gep,   name="end")
        
        # 원래 system 매개변수들 로드 (인덱스 2+)
        for i, (pname, pty) in enumerate(zip(params, param_llvm_types)):
            p_gep = self.builder.gep(typed_ptr,
                                      [ir.Constant(i32, 0), ir.Constant(i32, i + 2)],
                                      inbounds=True, name=f"p_{pname}_ptr")
            p_val = self.builder.load(p_gep, name=f"p_{pname}")
            slot = self.builder.alloca(pty, name=pname)
            self.builder.store(p_val, slot)
            self._declare_var(pname, slot)
        
        # for each 루프 (start..end 범위)
        self._build_foreach_loop(worker_fn, params, body, bindings, meta,
                                  start_val, end_val)
        
        # worker 반환: ret void* null
        null_ptr = ir.Constant(void_ptr_ty, None)
        self.builder.ret(null_ptr)
        
        # 컨텍스트 복원
        self.current_fn    = saved_fn
        self.builder       = saved_builder
        self.vars          = saved_vars
        self._loop_stack   = saved_loop
        self._readonly_params = saved_readonly
        
        # ---- system 래퍼 함수 (pthread fork-join) ----
        fn_entry = fn.append_basic_block(name="entry")
        self.current_fn = fn
        self.builder = ir.IRBuilder(fn_entry)
        self.vars = [{}]
        self._loop_stack = []
        self._readonly_params = set()
        
        # 매개변수 alloca
        for arg, pname in zip(fn.args, params):
            slot = self.builder.alloca(arg.type, name=pname)
            self.builder.store(arg, slot)
            self._declare_var(pname, slot)

        if sys.platform == 'win32':
            # Windows MCJIT에서는 pthread/sysconf 없음 → 단일 스레드 순차 실행
            count_val = self.builder.load(pivot['count'], name="total_count")
            null_arg  = ir.Constant(worker_arg_ty.as_pointer(), None)
            size_gep  = self.builder.gep(null_arg, [ir.Constant(i32, 1)], name="size_gep")
            arg_size  = self.builder.ptrtoint(size_gep, ir.IntType(64), name="arg_size")
            raw_arg   = self.builder.call(self.malloc_fn, [arg_size], name="raw_arg")
            typed_arg = self.builder.bitcast(raw_arg, worker_arg_ty.as_pointer(), name="typed_arg")
            s_ptr = self.builder.gep(typed_arg, [ir.Constant(i32, 0), ir.Constant(i32, 0)],
                                     inbounds=True, name="s_ptr")
            self.builder.store(ir.Constant(i32, 0), s_ptr)
            e_ptr = self.builder.gep(typed_arg, [ir.Constant(i32, 0), ir.Constant(i32, 1)],
                                     inbounds=True, name="e_ptr")
            self.builder.store(count_val, e_ptr)
            for pi, pname in enumerate(params):
                p_slot = self._lookup_var(pname)
                p_val  = self.builder.load(p_slot, name=f"pval_{pname}")
                p_ptr  = self.builder.gep(typed_arg,
                                          [ir.Constant(i32, 0), ir.Constant(i32, pi + 2)],
                                          inbounds=True, name=f"pptr_{pname}")
                self.builder.store(p_val, p_ptr)
            arg_void = self.builder.bitcast(typed_arg, void_ptr_ty, name="arg_void")
            self.builder.call(worker_fn, [arg_void])
            self.builder.call(self.free_fn, [raw_arg])
            self.builder.ret_void()
            self._loop_stack = saved_loop
            return

        # nproc = sysconf(84)   # _SC_NPROCESSORS_ONLN = 84 on Linux
        nproc_i64 = self.builder.call(self._sysconf,
                                       [ir.Constant(i32, 84)],
                                       name="nproc_i64")
        nproc = self.builder.trunc(nproc_i64, i32, name="nproc")
        # 최소 1 (sysconf 실패 시 -1 반환 보호)
        one = ir.Constant(i32, 1)
        ok_cmp = self.builder.icmp_signed('>', nproc, one, name="nproc_ok")
        nproc = self.builder.select(ok_cmp, nproc, one, name="nproc_safe")
        
        # count = pivot.count
        count_val = self.builder.load(pivot['count'], name="total_count")
        
        # pthread_t 배열 — malloc(nproc * 8)
        nproc_i64_for_malloc = self.builder.zext(nproc, ir.IntType(64), name="nproc64")
        thread_size = ir.Constant(ir.IntType(64), 8)  # sizeof(pthread_t) = 8
        threads_bytes = self.builder.mul(nproc_i64_for_malloc, thread_size, name="threads_bytes")
        threads_raw = self.builder.call(self.malloc_fn, [threads_bytes], name="threads_raw")
        threads_arr = self.builder.bitcast(threads_raw,
                                            pthread_t_ty.as_pointer(),
                                            name="threads_arr")
        
        # 각 스레드에 대해: arg 구조체 malloc → 값 채우기 → pthread_create
        # 반복을 LLVM IR 루프로 생성
        t_idx_slot = self.builder.alloca(i32, name="t_idx")
        self.builder.store(ir.Constant(i32, 0), t_idx_slot)
        
        create_cond = fn.append_basic_block(name="create_cond")
        create_body = fn.append_basic_block(name="create_body")
        create_end  = fn.append_basic_block(name="create_end")
        join_cond   = fn.append_basic_block(name="join_cond")
        join_body   = fn.append_basic_block(name="join_body")
        join_end    = fn.append_basic_block(name="join_end")
        
        self.builder.branch(create_cond)
        
        # --- create_cond: i < nproc ---
        self.builder = ir.IRBuilder(create_cond)
        t_i = self.builder.load(t_idx_slot, name="t_i")
        cmp_create = self.builder.icmp_signed('<', t_i, nproc, name="cmp_create")
        self.builder.cbranch(cmp_create, create_body, create_end)
        
        # --- create_body ---
        self.builder = ir.IRBuilder(create_body)
        t_i = self.builder.load(t_idx_slot, name="t_i_body")
        
        # chunk_size = count / nproc
        chunk = self.builder.sdiv(count_val, nproc, name="chunk")
        # start = t_i * chunk
        start_i = self.builder.mul(t_i, chunk, name="start_i")
        # end = (t_i == nproc-1) ? count : (t_i+1)*chunk  — 마지막 스레드가 나머지 처리
        nproc_m1 = self.builder.sub(nproc, ir.Constant(i32, 1), name="nproc_m1")
        is_last   = self.builder.icmp_signed('==', t_i, nproc_m1, name="is_last")
        t_i_1     = self.builder.add(t_i, ir.Constant(i32, 1), name="t_i_1")
        end_normal = self.builder.mul(t_i_1, chunk, name="end_normal")
        end_i = self.builder.select(is_last, count_val, end_normal, name="end_i")
        
        # worker_arg = malloc(sizeof(worker_arg_ty))
        # sizeof는 llvmlite에 없으므로 GEP-null 트릭 사용
        null_arg = ir.Constant(worker_arg_ty.as_pointer(), None)
        size_gep  = self.builder.gep(null_arg, [ir.Constant(i32, 1)], name="size_gep")
        arg_size  = self.builder.ptrtoint(size_gep, ir.IntType(64), name="arg_size")
        raw_arg   = self.builder.call(self.malloc_fn, [arg_size], name="raw_arg")
        typed_arg = self.builder.bitcast(raw_arg, worker_arg_ty.as_pointer(), name="typed_arg")
        
        # struct 필드 채우기
        s_ptr = self.builder.gep(typed_arg, [ir.Constant(i32, 0), ir.Constant(i32, 0)],
                                  inbounds=True, name="s_ptr")
        self.builder.store(start_i, s_ptr)
        e_ptr = self.builder.gep(typed_arg, [ir.Constant(i32, 0), ir.Constant(i32, 1)],
                                  inbounds=True, name="e_ptr")
        self.builder.store(end_i, e_ptr)
        for pi, pname in enumerate(params):
            p_slot = self._lookup_var(pname)
            p_val  = self.builder.load(p_slot, name=f"pval_{pname}")
            p_ptr  = self.builder.gep(typed_arg,
                                       [ir.Constant(i32, 0), ir.Constant(i32, pi + 2)],
                                       inbounds=True, name=f"pptr_{pname}")
            self.builder.store(p_val, p_ptr)
        
        # thread_id_ptr = &threads_arr[t_i]
        tid_ptr = self.builder.gep(threads_arr, [t_i], inbounds=True, name="tid_ptr")
        
        # worker_fn_ptr : 함수 → start_fn_ptr_ty 로 bitcast
        worker_fn_as_start = self.builder.bitcast(worker_fn, start_fn_ptr_ty,
                                                   name="worker_as_start")
        # void* arg_void = (void*)typed_arg
        arg_void = self.builder.bitcast(typed_arg, void_ptr_ty, name="arg_void")
        # pthread_create
        null_attr = ir.Constant(void_ptr_ty, None)
        self.builder.call(self._pthread_create,
                           [tid_ptr, null_attr, worker_fn_as_start, arg_void])
        
        # t_i++
        t_i_next = self.builder.add(t_i, ir.Constant(i32, 1), name="t_i_next")
        self.builder.store(t_i_next, t_idx_slot)
        self.builder.branch(create_cond)
        
        # --- create_end / join 시작 ---
        self.builder = ir.IRBuilder(create_end)
        self.builder.store(ir.Constant(i32, 0), t_idx_slot)
        self.builder.branch(join_cond)
        
        # --- join_cond ---
        self.builder = ir.IRBuilder(join_cond)
        t_j = self.builder.load(t_idx_slot, name="t_j")
        cmp_join = self.builder.icmp_signed('<', t_j, nproc, name="cmp_join")
        self.builder.cbranch(cmp_join, join_body, join_end)
        
        # --- join_body ---
        self.builder = ir.IRBuilder(join_body)
        t_j = self.builder.load(t_idx_slot, name="t_j_body")
        tid_ptr2 = self.builder.gep(threads_arr, [t_j], inbounds=True, name="tid_ptr2")
        tid_val  = self.builder.load(tid_ptr2, name="tid_val")
        null_ret = ir.Constant(void_ptr_ty, None)
        self.builder.call(self._pthread_join, [tid_val, null_ret])
        t_j_next = self.builder.add(t_j, ir.Constant(i32, 1), name="t_j_next")
        self.builder.store(t_j_next, t_idx_slot)
        self.builder.branch(join_cond)
        
        # --- join_end ---
        self.builder = ir.IRBuilder(join_end)
        # threads_arr 메모리 해제
        self.builder.call(self.free_fn, [threads_raw])
        self.builder.ret_void()
        
        # 컨텍스트 복원
        self._loop_stack = saved_loop
    
    def _compile_function_body(self, node):
        """2단계: 사용자 함수 본문 컴파일.
        
        - 이 함수의 entry 블록을 만들고 builder 위치
        - 새 vars 딕셔너리 (격리)
        - 매개변수를 alloca로 받아 store - 그래야 본문 안에서 일반 변수처럼 다룸
        - 본문 컴파일
        - 끝에 return이 없으면 명시적으로 ret 추가 (안전망)
        """
        name = node[1]
        params = node[2]
        body = node[3]
        line = node[-1]
        # 7.2.1b: FnDef 튜플에서 param_ref_kinds 제거됨.
        # 선언 단계에서 self.fn_param_ref_kinds에 저장한 걸 꺼내 씀.
        param_ref_kinds = self.fn_param_ref_kinds.get(name, [None] * len(params))
        
        fn = self.functions[name]
        entry_block = fn.append_basic_block(name="entry")
        
        # 현재 컨텍스트를 이 함수로 갈아끼움.
        # main 컴파일은 compile_program이 다른 컨텍스트로 나중에 다시 세팅.
        # 함수끼리는 서로 영향이 없어서 복구 안 해도 됨 (다음 함수가 다시 갈아끼움).
        self.current_fn = fn
        self.builder = ir.IRBuilder(entry_block)
        self.vars = [{}]  # 함수 entry 스코프
        self._saw_return = False  # 7.1.2: 명시적 return 추적
        self._defer_stacks = [[]]  # 42단계: 함수별 defer 스택
        # 7.1.3: 이 함수의 읽기 참조 매개변수 이름을 집합에 담음.
        # 본문에서 'p.hp = 0' 같은 쓰기 시도가 일어나면 _lvalue_struct가 이 집합을 봐서 에러.
        self._readonly_params = set()
        # L-1 forward (2026-05-19): ref 파라미터 forward — 함수 안에서 ref 변수를 다른 함수 ref 인자로 그대로 넘길 때 자동 매칭.
        self._cur_fn_param_refs = {}
        for pname, pref in zip(params, param_ref_kinds):
            if pref == 'ref':
                self._readonly_params.add(pname)
            if pref is not None:
                self._cur_fn_param_refs[pname] = pref
        # 7.15d: 함수 경계는 break/continue를 가둠.
        # 새 함수는 자기만의 loop_stack에서 시작하고 끝나면 복원.
        _saved_loop_stack = self._loop_stack
        self._loop_stack = []
        
        # 매개변수를 alloca로 받기.
        # LLVM 함수 인자는 그 자체로는 SSA 값이라 직접 store/load할 수 없는데,
        # 우리 컴파일러는 모든 변수를 alloca로 다루니까 인자도 alloca에 복사해 둔다.
        # 그래야 함수 안에서 'a = a + 1' 같은 갱신이 자연스럽게 동작.
        # 슬롯 타입은 인자 타입에 맞춤 (i32 인자면 i32*, f64 인자면 f64*).
        for arg, pname in zip(fn.args, params):
            slot = self.builder.alloca(arg.type, name=pname)
            self.builder.store(arg, slot)
            self._declare_var(pname, slot)
        
        # 본문 컴파일
        try:
            self.compile_stmt(body)
        finally:
            # 7.15d: 함수 경계 loop_stack 복원
            self._loop_stack = _saved_loop_stack
        
        # 본문이 명시적 return으로 안 끝났을 수 있음.
        # 안전망: 끝에 ret을 자동으로 박는다.
        # 반환 타입이 i32면 0, f64면 0.0을 넣는다.
        # 7.1.2: 구조체 반환은 자동 디폴트가 의미가 모호하므로 명시적 return을 강제.
        #        블록이 종결 안 됐더라도, 이건 'return 뒤의 도달 불가능 빈 블록'일
        #        수도 있음. 그래서 도달 가능성을 판단하는 기준으로 _saw_return을 씀.
        if not self.builder.block.is_terminated:
            # 42단계: fall-through에서도 defer 방출
            self._emit_defers()
            ret_ty = fn.function_type.return_type
            if ret_ty == f64:
                self.builder.ret(ir.Constant(f64, 0.0))
            elif ret_ty == i32 or ret_ty == i1:
                self.builder.ret(ir.Constant(ret_ty, 0))
            else:
                # 구조체 등 비정수/비실수 반환 타입.
                # 함수 안에 명시적 return이 한 번도 없었다면 진짜 누락 → 친절한 에러.
                # 있었다면 여긴 도달 불가능 블록이므로 unreachable로 정리.
                if not self._saw_return:
                    raise DanhaRuntimeError(
                        f"함수 '{name}'은(는) 구조체를 반환하니 본문이 명시적 'return'으로 끝나야 해",
                        line=line, source=self._source_code
                    )
                self.builder.unreachable()

        # Performance: alwaysinline 적용.
        # 함수 본문이 작으면 (IR 기본 블록 수 <= 4) 'alwaysinline' 적용하여 오버헤드 제거.
        # 단, main 함수는 항상 제외.
        if name != 'main' and len(fn.basic_blocks) <= 4:
            if 'inlinehint' in fn.attributes:
                fn.attributes.remove('inlinehint')
            fn.attributes.add('alwaysinline')
    
    def compile_stmt(self, node):
        node_type = node[0]

        # 49단계: doc 어노테이션 — 내부 선언만 컴파일
        if node_type == 'DocAnnotated':
            self.compile_stmt(node[2])
            return

        # 48단계: test 블록 — 컴파일러에서는 무시 (danha test는 인터프리터 전용)
        if node_type == 'TestBlock':
            return

        # 42단계: defer { ... } — defer 스택에 본문 등록, 실제 실행은 return/함수 끝에서
        if node_type == 'Defer':
            body = node[1]
            if hasattr(self, '_defer_stacks') and self._defer_stacks:
                self._defer_stacks[-1].append(body)
            return

        # 21c: unsafe 블록 — 블록 안 문장을 그대로 컴파일.
        # _in_unsafe 플래그를 올려서 unsafe 전용 연산(포인터 산술 등)을 허용.
        if node_type == 'UnsafeBlock':
            body = node[1]  # Block 노드
            old_unsafe = getattr(self, '_in_unsafe', 0)
            self._in_unsafe = old_unsafe + 1
            try:
                self.compile_stmt(body)
            finally:
                self._in_unsafe = old_unsafe
            return
        
        # 21c: unsafe fn — 함수 전체가 unsafe.
        # 내부 FnDef를 그대로 컴파일. _unsafe_fns에 등록해서 호출 검사.
        if node_type == 'UnsafeFn':
            fn_node = node[1]
            fn_name = fn_node[1]
            if not hasattr(self, '_unsafe_fns'):
                self._unsafe_fns = set()
            self._unsafe_fns.add(fn_name)
            # FnDef와 동일하게 처리 — compile_program에서 이미 선언/본문 컴파일 경로를 탐
            # main 안에서 만나면 무시 (선언은 이미 됐으므로)
            return

        # 45단계: export fn — 컴파일러에서는 FnDef와 동일. 함수는 이미 external linkage.
        # compile_program의 1-pass가 FnDef/ExportFn 둘 다 선언하므로 main 안에서는 무시.
        if node_type == 'ExportFn':
            return
        
        # 22c: 매크로 정의 — 인터프리터로 평가할 매크로 정보를 저장
        if node_type == 'MacroDef':
            name = node[1]
            params = node[2]
            body = node[3]
            is_variadic = node[4]
            self._macros[name] = ('Macro', params, body, is_variadic)
            return
        
        # 22c: 매크로 호출 (문장 위치) — 인터프리터로 확장 후 컴파일
        if node_type == 'MacroCall':
            self._compile_macro_call(node)
            return
        
        if node_type == 'Print':
            return self.compile_print(node)
        
        if node_type == 'Assign':
            return self.compile_assign(node)
        
        # 7.15a: const 정의
        if node_type == 'ConstDef':
            return self.compile_const_def(node)
        
        if node_type == 'FieldAssign':
            # ('FieldAssign', obj_node, field_name, value_node, line)
            # 필드 주소를 GEP로 구해서 거기 store.
            obj_node = node[1]
            field_name = node[2]
            value_node = node[3]
            line = node[-1]
            
            # 7.1.3: 읽기 참조('&T')로 받은 매개변수에 필드 쓰기 금지.
            # 중첩 필드('p.inner.x = ...')도 뿌리까지 올라가 검사.
            root_name = self._root_name_of(obj_node)
            if root_name is not None and root_name in self._readonly_params:
                raise DanhaValueError(
                    f"'{root_name}'은(는) 읽기 참조('&')로 받은 매개변수야 — "
                    f"쓰기를 허용하려면 '&mut'로 받아야 해",
                    line=line, source=self._source_code
                )
            
            # 7.7: 벡터 필드 쓰기 (v.x = 5.0)
            if obj_node[0] == 'Name':
                slot = self._lookup_var(obj_node[1])
                if slot is not None:
                    vec_info = self._is_vec_slot(slot)
                    if vec_info is not None:
                        vec_name, size = vec_info
                        fields = self._VEC_FIELDS[vec_name]
                        if field_name not in fields:
                            raise DanhaNameError(
                                f"{vec_name}에 '{field_name}'이라는 필드가 없어",
                                line=line, source=self._source_code
                            )
                        fidx = fields[field_name]
                        value = self.compile_expr(value_node)
                        if value.type == i32:
                            value = self.builder.sitofp(value, f64, name="vec_f")
                        # VectorType: load+insert+store, struct: GEP+store
                        self._vec_store_insert(slot, value, fidx)
                        return
            
            obj_ptr, struct_info = self._lvalue_struct(obj_node, line)
            llvm_struct, field_names, field_types = struct_info
            
            if field_name not in field_names:
                raise DanhaNameError(f"그 구조체에 '{field_name}'이라는 필드가 없어", line=line, source=self._source_code)
            idx = field_names.index(field_name)
            expected = field_types[idx]

            # 배열 타입 필드 특별 처리 (StructInstance와 동일 로직)
            if isinstance(expected, ir.ArrayType):
                if value_node[0] == 'List' and len(value_node[1]) == 0:
                    value = ir.Constant(expected, None)
                elif value_node[0] == 'List':
                    elems = value_node[1]
                    if len(elems) != expected.count:
                        raise DanhaTypeError(
                            f"필드 '{field_name}': 배열 길이가 안 맞아 "
                            f"(기대 {expected.count}, 실제 {len(elems)})",
                            line=line, source=self._source_code
                        )
                    elem_vals = [self.compile_expr(e) for e in elems]
                    for ei, ev in enumerate(elem_vals):
                        if ev.type != expected.element:
                            if ev.type == i32 and expected.element == f64:
                                elem_vals[ei] = self.builder.sitofp(ev, f64)
                            else:
                                raise DanhaTypeError(
                                    f"필드 '{field_name}'[{ei}] 타입이 안 맞아 "
                                    f"(기대 {expected.element}, 실제 {ev.type})",
                                    line=line, source=self._source_code
                                )
                    arr_tmp = self.builder.alloca(expected, name=f"{field_name}_arr")
                    for ei, ev in enumerate(elem_vals):
                        ep = self.builder.gep(
                            arr_tmp,
                            [ir.Constant(i32, 0), ir.Constant(i32, ei)],
                            inbounds=True
                        )
                        self.builder.store(ev, ep)
                    value = self.builder.load(arr_tmp, name=f"{field_name}_arr_val")
                else:
                    value = self.compile_expr(value_node)
                    if value.type != expected:
                        raise DanhaTypeError(
                            f"필드 '{field_name}'의 타입이 안 맞아 "
                            f"(기대 {expected}, 실제 {value.type})",
                            line=line, source=self._source_code
                        )
            else:
                value = self.compile_expr(value_node)
                if value.type != expected:
                    if value.type == i32 and expected == f64:
                        value = self.builder.sitofp(value, f64)
                    elif value.type == i1 and expected == i8:
                        value = self.builder.zext(value, i8, name='bool_to_i8')
                    else:
                        raise DanhaTypeError(
                            f"필드 '{field_name}'의 타입이 안 맞아 "
                            f"(기대 {expected}, 실제 {value.type})",
                            line=line, source=self._source_code
                        )

            # 46단계: union은 bitcast, struct는 indexed GEP
            _sname = self._struct_name_from_llvm(llvm_struct)
            if _sname in self.unions:
                _base = self.builder.gep(obj_ptr, [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True)
                field_ptr = self.builder.bitcast(_base, expected.as_pointer(), name=f"{field_name}_ptr")
            else:
                field_ptr = self.builder.gep(
                    obj_ptr, [ir.Constant(i32, 0), ir.Constant(i32, idx)],
                    inbounds=True, name=f"{field_name}_ptr"
                )
            self.builder.store(value, field_ptr)
            return

        # ----- 7.2: 인덱스 쓰기 arr[i] = val -----
        if node_type == 'IndexAssign':
            # ('IndexAssign', obj_node, idx_node, value_node, line)
            obj_node = node[1]
            idx_node = node[2]
            value_node = node[3]
            line = node[-1]

            # 구조체 배열 필드 쓰기: b.lines[3] = x
            if obj_node[0] == 'FieldAccess':
                idx_val = self.compile_expr(idx_node)
                if idx_val.type != i32:
                    raise DanhaValueError("배열 인덱스는 정수여야 해", line=line, source=self._source_code)
                value = self.compile_expr(value_node)
                arr_ptr, elem_ty, _arr_len = self._lvalue_array_field(obj_node, line)
                if value.type != elem_ty:
                    if value.type == i32 and elem_ty == f64:
                        value = self.builder.sitofp(value, f64)
                    else:
                        raise DanhaTypeError(
                            f"배열 원소 타입이 안 맞아 (기대 {elem_ty}, 실제 {value.type})",
                            line=line, source=self._source_code
                        )
                elem_ptr = self.builder.gep(
                    arr_ptr, [ir.Constant(i32, 0), idx_val],
                    inbounds=True, name="arr_field_elem"
                )
                self.builder.store(value, elem_ptr)
                return

            if obj_node[0] != 'Name':
                raise DanhaValueError(
                    f"인덱스 쓰기는 지금 변수에만 할 수 있어",
                    line=line, source=self._source_code
                )
            var_name = obj_node[1]
            
            # 7.1.3: 읽기 참조에 쓰기 금지
            if var_name in self._readonly_params:
                raise DanhaValueError(
                    f"'{var_name}'은(는) 읽기 참조('&')로 받은 매개변수야 — "
                    f"쓰기를 허용하려면 '&mut'로 받아야 해",
                    line=line, source=self._source_code
                )
            
            idx_val = self.compile_expr(idx_node)
            if idx_val.type != i32:
                raise DanhaValueError(
                    f"배열 인덱스는 정수여야 해",
                    line=line, source=self._source_code
                )
            
            value = self.compile_expr(value_node)
            
            # 7.4: 동적 배열인지 확인
            slot = self._lookup_var(var_name)
            if slot is not None and self._is_dynarray_slot(slot):
                elem_ty = self._dynarray_elem_ty(slot)
                if value.type != elem_ty:
                    if value.type == i32 and elem_ty == f64:
                        value = self.builder.sitofp(value, f64)
                    else:
                        raise DanhaTypeError(
                            f"배열 원소 타입이 안 맞아",
                            line=line, source=self._source_code
                        )
                data_ptr = self._dynarray_get_field(slot, 0, f"{var_name}_data")
                data = self.builder.load(data_ptr, name="data")
                elem_ptr = self.builder.gep(
                    data, [idx_val],
                    inbounds=True, name=f"{var_name}_elem"
                )
                self.builder.store(value, elem_ptr)
                return
            
            # 고정 배열: 기존 로직
            arr_ptr, elem_ty, arr_len = self._lvalue_array(var_name, line)
            
            if value.type != elem_ty:
                if value.type == i32 and elem_ty == f64:
                    value = self.builder.sitofp(value, f64)
                else:
                    raise DanhaTypeError(
                        f"배열 원소 타입이 안 맞아 "
                        f"(기대 {elem_ty}, 실제 {value.type})",
                        line=line, source=self._source_code
                    )
            
            elem_ptr = self.builder.gep(
                arr_ptr, [ir.Constant(i32, 0), idx_val],
                inbounds=True, name=f"{var_name}_elem"
            )
            self.builder.store(value, elem_ptr)
            return
        
        if node_type == 'If':
            return self.compile_if(node)
        
        if node_type == 'Match':
            return self.compile_match(node)
        
        if node_type == 'While':
            return self.compile_while(node)
        
        if node_type == 'For':
            return self.compile_for(node)
        
        if node_type == 'Return':
            return self.compile_return(node)
        
        # 7.15d: break / continue
        if node_type == 'Break':
            return self.compile_break(node)
        
        if node_type == 'Continue':
            return self.compile_continue(node)
        
        if node_type == 'Block':
            # 블록 들어갈 때 새 스코프(껍질) 추가, 나올 때 제거.
            # 이 안에서 처음 만든 변수는 블록 끝나면 못 보게 된다.
            # 단 블록 안에서 바깥 변수 이름으로 대입하면 바깥 걸 덮어씀
            # (compile_assign이 체인을 훑어서 찾으니까).
            # try/finally로 감싸서 컴파일 중 에러가 나도 스코프가 안 새도록.
            self._push_scope()
            try:
                for stmt in node[1]:
                    self.compile_stmt(stmt)
            finally:
                self._pop_scope()
            return
        
        if node_type in ('StructDef', 'UnionDef'):
            # struct/union 정의는 모듈 최상위에만 허용.
            raise DanhaImportError(
                f"[{node[-1]}번째 줄] struct/union은 함수 안이 아니라 모듈 최상위에 정의해야 해"
            )
        
        # 위에 해당 안 하면 식 문장 (expression statement).
        # 파서는 대입이나 키워드 문이 아닌 걸 그냥 식으로 돌려준다.
        # 대표적인 예: 'show(77)' 같은 함수 호출을 독립 문장으로 쓸 때.
        # 결과는 무시 — 부수 효과(print 등)만 의미 있다.
        # compile_expr이 처리할 수 있는 노드면 시도, 아니면 에러.
        try:
            self.compile_expr(node)
            return
        except NotImplementedError:
            pass
        
        raise NotImplementedError(
            f"이 노드는 아직 컴파일 못 해: {node_type}"
        )
    
    def _try_eq_const(self, cond_node):
        """('Eq', left, right, line) 에서 (var_name, int_const) 추출. 실패하면 None.
        `x == 5` 또는 `5 == x` 둘 다 인식. var의 타입은 컴파일 시점에 i32로 가정.
        """
        if not (isinstance(cond_node, tuple) and len(cond_node) >= 4 and cond_node[0] == 'Eq'):
            return None
        a, b = cond_node[1], cond_node[2]
        # Name == Number
        if (isinstance(a, tuple) and a[0] == 'Name'
                and isinstance(b, tuple) and b[0] == 'Number' and isinstance(b[1], int)):
            return (a[1], b[1])
        # Number == Name (대칭)
        if (isinstance(b, tuple) and b[0] == 'Name'
                and isinstance(a, tuple) and a[0] == 'Number' and isinstance(a[1], int)):
            return (b[1], a[1])
        return None

    def _collect_switch_chain(self, if_node):
        """If-chain 을 따라가며 (var_name, [(const, then_block), ...], default_node_or_None) 반환.
        모든 If의 condition이 동일 변수에 대한 정수 등치이고 상수가 unique 해야 함.
        실패하면 None.
        """
        first = self._try_eq_const(if_node[1])
        if first is None:
            return None
        var_name = first[0]
        cases = []
        seen_consts = set()
        seen_consts.add(first[1])
        cases.append((first[1], if_node[2]))
        default_node = None
        cur = if_node[3]  # else
        while cur is not None:
            if isinstance(cur, tuple) and cur[0] == 'If':
                eq = self._try_eq_const(cur[1])
                if eq is None or eq[0] != var_name or eq[1] in seen_consts:
                    # 다른 변수거나 중복 상수: 더 이상 chain 아님 — 남은 부분 default로
                    default_node = cur
                    break
                seen_consts.add(eq[1])
                cases.append((eq[1], cur[2]))
                cur = cur[3]
            else:
                # 'Block' 등: terminating else
                default_node = cur
                break
        return (var_name, cases, default_node)

    def compile_if(self, node):
        """('If', condition, then_block, else_block_or_None, line)

        세 경우:
        - else 있음 → 블록 4개 (entry는 이미 있음, then/else/end 새로 만듦)
        - else 없음 → 블록 3개 (then/end만)
        - 어느 쪽이든 then/else 안에서 또 if가 나올 수 있어서, 본문 컴파일이 끝난 뒤
          builder.block을 다시 봐서 거기서 end로 분기해야 함.

        Stage 75b: 동일 변수에 정수 상수 등치 if-chain (≥3개) 은 LLVM switch instruction으로 변환.
        예: `if x==0 {...} else if x==1 {...} else if x==2 {...} else {...}` → switch.
        """
        # Stage 75b: switch 변환 가능성 검사
        chain = self._collect_switch_chain(node)
        if chain is not None and len(chain[1]) >= 3:
            var_name, cases, default_node = chain
            var_val = self.compile_expr(('Name', var_name, node[-1]))
            # i32 정수 변수에만 한정 (i64면 trunc — 정수 등치는 동일 의미)
            if var_val.type == ir.IntType(64):
                var_val = self.builder.trunc(var_val, i32, name="switch_var_i32")
            if var_val.type == i32:
                end_block = self.current_fn.append_basic_block(name="ifchain_end")
                if default_node is not None:
                    default_block = self.current_fn.append_basic_block(name="ifchain_default")
                else:
                    default_block = end_block
                switch_inst = self.builder.switch(var_val, default_block)
                for i, (const_val, then_node) in enumerate(cases):
                    case_block = self.current_fn.append_basic_block(name=f"ifchain_case_{i}_{const_val}")
                    switch_inst.add_case(ir.Constant(i32, const_val), case_block)
                    self.builder.position_at_end(case_block)
                    self.compile_stmt(then_node)
                    if not self.builder.block.is_terminated:
                        self.builder.branch(end_block)
                if default_node is not None:
                    self.builder.position_at_end(default_block)
                    self.compile_stmt(default_node)
                    if not self.builder.block.is_terminated:
                        self.builder.branch(end_block)
                self.builder.position_at_end(end_block)
                return

        condition = self.compile_expr(node[1])
        then_node = node[2]
        else_node = node[3]
        
        # 조건이 i1이 아닐 수도 있다.
        # L-1 패치 (2026-05-19): bool(i8) 반환을 if에 직접 쓸 수 있게 자동 변환.
        # `fn is_alive() -> bool { ... }` 후 `if is_alive() { ... }` 패턴 지원.
        # 정수형도 0 비교로 자동 변환 (C 스타일 truthiness — 안전한 범위에서만).
        if condition.type != i1:
            if condition.type == i8:
                # bool — i8 != 0 → i1
                condition = self.builder.icmp_signed('!=', condition, ir.Constant(i8, 0), name='bool_to_i1')
            elif isinstance(condition.type, ir.IntType):
                # 다른 정수 타입도 0 비교
                zero = ir.Constant(condition.type, 0)
                condition = self.builder.icmp_signed('!=', condition, zero, name='int_to_i1')
            else:
                line = node[-1]
                raise DanhaRuntimeError("if 조건은 불리언이어야 해", line=line, source=self._source_code)
        
        # 새 블록들. main 함수 안에 만든다.
        # 이름은 디버깅용으로만 의미가 있고, LLVM이 알아서 충돌 안 나게 번호를 붙임.
        then_block = self.current_fn.append_basic_block(name="then")
        end_block = self.current_fn.append_basic_block(name="endif")
        
        if else_node is not None:
            else_block = self.current_fn.append_basic_block(name="else")
            # entry 블록의 마지막 명령어로 조건 분기
            self.builder.cbranch(condition, then_block, else_block)
        else:
            # else가 없으면 거짓일 때 바로 end로
            self.builder.cbranch(condition, then_block, end_block)
        
        # then 블록 컴파일.
        # builder의 '쓰기 위치'를 then 블록으로 옮긴다.
        self.builder.position_at_end(then_block)
        self.compile_stmt(then_node)
        # 본문 컴파일 후의 현재 블록 (then 안에 또 if가 있었을 수도 있음)
        # 거기서 end로 무조건 분기. 단 이미 끝맺어진 블록이면 건너뜀.
        if not self.builder.block.is_terminated:
            self.builder.branch(end_block)
        
        # else 블록도 같은 식으로
        if else_node is not None:
            self.builder.position_at_end(else_block)
            self.compile_stmt(else_node)
            if not self.builder.block.is_terminated:
                self.builder.branch(end_block)
        
        # 이후 코드는 end 블록에 이어진다
        self.builder.position_at_end(end_block)
    
    def compile_match(self, node):
        """('Match', target_expr, [(pattern, body), ...], line)

        8.4: tagged enum 패턴 매칭.
        Stage 71: LLVM `switch` instruction 발행 (이전 if-chain → jump table 후보).
        각 variant arm은 switch case, wildcard arm은 default. wildcard 뒤 arm은 무시 (기존 동작).
        """
        target_expr = node[1]
        arms = node[2]
        line = node[-1]

        target_val = self.compile_expr(target_expr)
        target_ty = target_val.type

        # 어떤 tagged enum인지 찾기
        enum_info = None
        enum_name = None
        for ename, (tagged_ty, variant_info, max_payload) in self.tagged_enums.items():
            if target_ty == tagged_ty:
                enum_info = (tagged_ty, variant_info, max_payload)
                enum_name = ename
                break

        if enum_info is None:
            raise DanhaTypeError("match는 tagged enum 값에만 쓸 수 있어", line=line, source=self._source_code)

        tagged_ty, variant_info, max_payload = enum_info

        # Stage 75a: typed payload (Stage 75a repr)면 alloca 필요 없음 — SSA extract_value 만 사용.
        # 레거시 [N x i8] repr 이면 alloca + GEP + bitcast 경로.
        uses_typed = self._tagged_uses_typed_payload(tagged_ty)
        if uses_typed:
            tmp = None
        else:
            tmp = self._alloca_at_entry(tagged_ty, name="match_tmp")
            self.builder.store(target_val, tmp)

        # tag 추출
        tag_val = self.builder.extract_value(target_val, 0, name="match_tag")

        # 끝 블록
        end_block = self.current_fn.append_basic_block(name="match_end")

        # arm 분류: 첫 wildcard 전까지의 variant arms + wildcard (기존 break 의미 유지)
        variant_arms = []
        wildcard_arm = None
        for pattern, body in arms:
            if pattern[0] == 'MatchWildcard':
                wildcard_arm = (pattern, body)
                break
            elif pattern[0] == 'MatchVariant':
                variant_arms.append((pattern, body))

        # default 블록: wildcard 있으면 새 블록, 없으면 end로 직접
        if wildcard_arm is not None:
            default_block = self.current_fn.append_basic_block(name="match_default")
        else:
            default_block = end_block

        # LLVM switch instruction 발행. 각 variant tag별로 case 추가 → LLVM이 jump table 또는 분기트리 생성.
        switch_inst = self.builder.switch(tag_val, default_block)

        for i, (pattern, body) in enumerate(variant_arms):
            vname = pattern[1]
            bindings = pattern[2]

            if vname not in variant_info:
                raise DanhaNameError(f"enum '{enum_name}'에 '{vname}' variant가 없어", line=line, source=self._source_code)

            vtag, vtypes = variant_info[vname]

            case_block = self.current_fn.append_basic_block(name=f"match_arm_{i}_{vname}")
            switch_inst.add_case(ir.Constant(i32, vtag), case_block)

            # case 블록: payload를 바인딩 변수에 풀고 본문 실행
            self.builder.position_at_end(case_block)

            # 새 스코프 열기
            self.vars.append({})

            if vtypes is not None and len(bindings) > 0:
                # Stage 75a: typed payload 경로 — extract_value SSA 추출 (alloca 없음)
                if self._tagged_uses_typed_payload(tagged_ty) and len(bindings) == 1:
                    bname = bindings[0]
                    lty = vtypes[0]
                    val = self.builder.extract_value(target_val, 1, name=f"match_pay_{i}_v")
                    # 바인딩 슬롯도 entry로 호이스트 — 루프 안에서 매 iter 누적 방지.
                    slot = self._alloca_at_entry(lty, name=f"bind_{bname}")
                    self.builder.store(val, slot)
                    self._declare_var(bname, slot)
                else:
                    # 레거시 [N x i8] repr 경로
                    payload_ptr = self.builder.gep(
                        tmp, [ir.Constant(i32, 0), ir.Constant(i32, 1)],
                        inbounds=True, name="match_pay_ptr"
                    )
                    pay_i8 = self.builder.bitcast(
                        payload_ptr, ir.IntType(8).as_pointer()
                    )
                    offset = 0
                    for j, (bname, lty) in enumerate(zip(bindings, vtypes)):
                        ptr = self.builder.gep(
                            pay_i8, [ir.Constant(i32, offset)],
                            inbounds=True, name=f"match_pay_{j}"
                        )
                        typed_ptr = self.builder.bitcast(
                            ptr, lty.as_pointer(), name=f"match_pay_{j}_ptr"
                        )
                        val = self.builder.load(typed_ptr, name=bname)
                        slot = self._alloca_at_entry(lty, name=f"bind_{bname}")
                        self.builder.store(val, slot)
                        self._declare_var(bname, slot)
                        if lty == i32:
                            offset += 4
                        elif lty == f64:
                            offset += 8
                        elif lty == i8p:
                            offset += 8
                        else:
                            offset += 8

            for stmt in body[1]:
                self.compile_stmt(stmt)

            # 스코프 닫기
            self.vars.pop()

            # case 본문이 이미 terminator로 끝났을 수도 있음 (return 등) — 확인 후 branch
            if not self.builder.block.is_terminated:
                self.builder.branch(end_block)

        # wildcard 본문 (default case)
        if wildcard_arm is not None:
            _, body = wildcard_arm
            self.builder.position_at_end(default_block)
            for stmt in body[1]:
                self.compile_stmt(stmt)
            if not self.builder.block.is_terminated:
                self.builder.branch(end_block)

        self.builder.position_at_end(end_block)
    
    def compile_while(self, node):
        """('While', condition, body_block, line)
        
        if보다 단순. 모양:
        
            entry:
              br label %cond                        ; 무조건 cond로 시작
            cond:
              <조건 평가>
              br i1 %t, label %body, label %end     ; 참이면 body, 거짓이면 탈출
            body:
              <본문>
              br label %cond                        ; 끝에서 다시 cond로 (← 루프!)
            end:
              <이후 코드>
        
        핵심 한 가지: body 끝의 'br cond' 가 위로 거슬러 올라가는 화살표.
        분기 명령은 어디로든 점프할 수 있어서 위로 가는 거나 아래로 가는 거나 똑같다.
        그게 루프의 본질. CPU도 같은 식으로 동작.
        
        주의: 조건 평가는 본문 안에서 일어나는 게 아니라 매 반복마다 cond 블록에서
        다시 일어난다. 즉 'i = i + 1'로 i가 바뀌면 다음 반복 때 cond에서 새 i를 load해서 다시 비교.
        우리가 변수를 alloca/store/load로 풀어둔 덕에 이게 자동으로 동작한다.
        (만약 phi로 했다면 루프 변수마다 phi를 따로 만들어줘야 했을 거야 -
        교과서적 LLVM 컴파일러 입문에서 가장 어려운 부분 중 하나. 우리는 alloca 덕에 우회.)
        """
        cond_block = self.current_fn.append_basic_block(name="while_cond")
        body_block = self.current_fn.append_basic_block(name="while_body")
        end_block = self.current_fn.append_basic_block(name="while_end")
        
        # entry → cond 무조건 분기
        self.builder.branch(cond_block)
        
        # cond 블록: 조건 평가 후 분기
        self.builder.position_at_end(cond_block)
        condition = self.compile_expr(node[1])
        if condition.type != i1:
            # L-1: 모든 정수 타입을 0 비교로 자동 변환 (i8 bool, i32 extern, i64 등)
            if isinstance(condition.type, ir.IntType):
                condition = self.builder.icmp_signed(
                    '!=', condition, ir.Constant(condition.type, 0), name="while_bool"
                )
            else:
                line = node[-1]
                raise DanhaRuntimeError("while 조건은 불리언이어야 해", line=line, source=self._source_code)
        self.builder.cbranch(condition, body_block, end_block)
        
        # body 블록: 본문 컴파일 후 cond로 되돌아가기
        self.builder.position_at_end(body_block)
        # 7.15d: 이 루프의 continue 대상 = cond_block (다음 반복 조건 재검사),
        #         break 대상 = end_block (루프 탈출).
        self._loop_stack.append((cond_block, end_block))
        try:
            self.compile_stmt(node[2])
        finally:
            self._loop_stack.pop()
        # 본문 안에 if 등이 있어서 현재 블록이 바뀌었을 수 있음. is_terminated 확인.
        if not self.builder.block.is_terminated:
            self.builder.branch(cond_block)
        
        # 이후 코드는 end 블록에서 이어짐
        self.builder.position_at_end(end_block)
    
    def compile_for(self, node):
        """('For', var_name, iterable, body, line)
        
        지금은 정수 범위만: iterable이 ('Range', start, end, line)인 경우.
        다른 형태(리스트 등)는 그 자료구조가 컴파일러에 들어온 뒤에.
        
        모양 (i in start..end):
        
            init:
              %start = <start 평가>
              %end_val = <end 평가>     ; 매 반복 재평가 안 하려고 한 번만
              %i = alloca i32
              store %start -> %i
              br label %for_cond
            for_cond:
              %i_val = load %i
              %cmp = icmp slt %i_val, %end_val   ; 끝값 미포함이라 <
              br i1 %cmp, label %for_body, label %for_end
            for_body:
              <본문>
              %next = %i_val + 1
              store %next -> %i
              br label %for_cond
            for_end:
              ...
        
        스코프: for 시작에 push, 끝에 pop. 루프 변수 i는 그 사이 declare.
        본문(Block)이 또 자식 스코프를 만들지만, i는 한 단계 바깥에 있어 본문에서 보임.
        """
        var_name = node[1]
        iterable = node[2]
        body = node[3]
        line = node[-1]
        
        # ----- 7.3: 배열 순회 분기 -----
        # iterable이 Range면 기존 정수 범위 루프,
        # 그렇지 않으면 배열 변수 순회를 시도.
        if iterable[0] == 'Range':
            return self._compile_for_range(var_name, iterable, body, line)
        
        # 배열 순회: for x in arr { ... }
        # 내부적으로 for i in 0..len(arr) { x = arr[i] } 와 같은 구조.
        # iterable은 변수 이름(Name 노드)이어야 함.
        if iterable[0] != 'Name':
            raise NotImplementedError(
                f"[{line}번째 줄] 'for x in expr'에서 expr은 배열 변수여야 해"
            )
        
        arr_var_name = iterable[1]
        slot = self._lookup_var(arr_var_name)
        if slot is None:
            raise DanhaNameError(
                f"정의되지 않은 이름이야: {arr_var_name}",
                line=line, source=self._source_code
            )
        
        # 7.4: 동적 배열이면 별도 경로
        if self._is_dynarray_slot(slot):
            return self._compile_for_dynarray(var_name, arr_var_name, slot, body, line)
        
        # 고정 배열 경로 (기존 7.3)
        try:
            arr_ptr, elem_ty, arr_len = self._lvalue_array(arr_var_name, line)
        except Exception:
            raise DanhaRuntimeError(
                f"'{arr_var_name}'은(는) 배열이 아니거나 정의되지 않았어",
                line=line, source=self._source_code
            )
        
        if arr_len == 0:
            return  # 빈 배열이면 아무것도 안 함
        
        # 루프 구조: 숨은 인덱스 카운터로 0..arr_len 범위 루프,
        # 매 반복 시작에 arr[i]를 꺼내서 루프 변수에 넣어줌.
        self._push_scope()
        try:
            # 숨은 인덱스 변수 (사용자한테 안 보임)
            idx_slot = self.builder.alloca(i32, name=f"_foreach_idx")
            self.builder.store(ir.Constant(i32, 0), idx_slot)
            
            # 루프 변수 (사용자가 쓰는 x)
            elem_slot = self.builder.alloca(elem_ty, name=var_name)
            self._declare_var(var_name, elem_slot)
            
            end_val = ir.Constant(i32, arr_len)
            
            cond_block = self.current_fn.append_basic_block(name="foreach_cond")
            body_block = self.current_fn.append_basic_block(name="foreach_body")
            end_block = self.current_fn.append_basic_block(name="foreach_end")
            
            self.builder.branch(cond_block)
            
            # cond: idx < arr_len ?
            self.builder.position_at_end(cond_block)
            idx_val = self.builder.load(idx_slot, name="_foreach_idx")
            cmp = self.builder.icmp_signed('<', idx_val, end_val, name="foreach_cmp")
            self.builder.cbranch(cmp, body_block, end_block)
            
            # body: x = arr[idx]; <본문>; idx++
            self.builder.position_at_end(body_block)
            # arr[idx]를 꺼내서 루프 변수에 저장
            elem_ptr = self.builder.gep(
                arr_ptr, [ir.Constant(i32, 0), idx_val],
                inbounds=True, name=f"{arr_var_name}_elem"
            )
            elem_val = self.builder.load(elem_ptr, name=f"{var_name}_val")
            self.builder.store(elem_val, elem_slot)
            
            # 7.15d: continue = inc 블록으로, break = end.
            inc_block = self.current_fn.append_basic_block(name="foreach_inc")
            self._loop_stack.append((inc_block, end_block))
            try:
                self.compile_stmt(body)
            finally:
                self._loop_stack.pop()
            
            if not self.builder.block.is_terminated:
                self.builder.branch(inc_block)
            # 증가 블록
            self.builder.position_at_end(inc_block)
            idx_after = self.builder.load(idx_slot, name="_foreach_idx")
            next_idx = self.builder.add(
                idx_after, ir.Constant(i32, 1), name="foreach_next"
            )
            self.builder.store(next_idx, idx_slot)
            self.builder.branch(cond_block)
            
            self.builder.position_at_end(end_block)
        finally:
            self._pop_scope()
    
    def _compile_for_range(self, var_name, iterable, body, line):
        """정수 범위 루프: for i in start..end { ... }
        
        기존 compile_for의 Range 처리를 분리한 것."""
        start_val = self.compile_expr(iterable[1])
        end_val = self.compile_expr(iterable[2])
        if start_val.type != i32 or end_val.type != i32:
            raise DanhaValueError("범위의 양 끝은 정수여야 해", line=line, source=self._source_code)
        
        self._push_scope()
        try:
            i_slot = self.builder.alloca(i32, name=var_name)
            self._declare_var(var_name, i_slot)
            self.builder.store(start_val, i_slot)
            
            cond_block = self.current_fn.append_basic_block(name="for_cond")
            body_block = self.current_fn.append_basic_block(name="for_body")
            end_block = self.current_fn.append_basic_block(name="for_end")
            
            self.builder.branch(cond_block)
            
            self.builder.position_at_end(cond_block)
            i_val = self.builder.load(i_slot, name=var_name)
            cmp = self.builder.icmp_signed('<', i_val, end_val, name="for_cmp")
            self.builder.cbranch(cmp, body_block, end_block)
            
            self.builder.position_at_end(body_block)
            # 7.15d: continue = 증가 후 cond로, break = end로.
            # 근데 증가를 cond에서 안 하고 본문 끝에서 하니까, continue는
            # "증가 단계"를 건너뛰면 무한루프가 됨. 해결책: 증가 전용 블록 추가.
            inc_block = self.current_fn.append_basic_block(name="for_inc")
            self._loop_stack.append((inc_block, end_block))
            try:
                self.compile_stmt(body)
            finally:
                self._loop_stack.pop()
            if not self.builder.block.is_terminated:
                self.builder.branch(inc_block)
            # 증가 블록: i = i + 1; br cond
            self.builder.position_at_end(inc_block)
            i_val_after = self.builder.load(i_slot, name=var_name)
            next_val = self.builder.add(
                i_val_after, ir.Constant(i32, 1), name="for_next"
            )
            self.builder.store(next_val, i_slot)
            self.builder.branch(cond_block)
            
            self.builder.position_at_end(end_block)
        finally:
            self._pop_scope()
    
    def _compile_for_dynarray(self, var_name, arr_var_name, slot, body, line):
        """동적 배열 순회: for x in dyn_arr { ... }
        
        고정 배열 순회와 같은 구조이지만:
        - 끝값(len)을 런타임에 구조체에서 읽음
        - 원소 접근은 data 포인터에 GEP (고정 배열은 [0][idx] 형태)
        """
        elem_ty = self._dynarray_elem_ty(slot)
        
        self._push_scope()
        try:
            idx_slot = self.builder.alloca(i32, name="_foreach_idx")
            self.builder.store(ir.Constant(i32, 0), idx_slot)
            
            elem_slot = self.builder.alloca(elem_ty, name=var_name)
            self._declare_var(var_name, elem_slot)
            
            cond_block = self.current_fn.append_basic_block(name="dynfe_cond")
            body_block = self.current_fn.append_basic_block(name="dynfe_body")
            end_block = self.current_fn.append_basic_block(name="dynfe_end")
            
            self.builder.branch(cond_block)
            
            # cond: idx < len ?
            self.builder.position_at_end(cond_block)
            idx_val = self.builder.load(idx_slot, name="_foreach_idx")
            len_ptr = self._dynarray_get_field(slot, 1, f"{arr_var_name}_len")
            cur_len = self.builder.load(len_ptr, name="cur_len")
            cmp = self.builder.icmp_signed('<', idx_val, cur_len, name="dynfe_cmp")
            self.builder.cbranch(cmp, body_block, end_block)
            
            # body: x = data[idx]; <본문>; idx++
            self.builder.position_at_end(body_block)
            data_ptr = self._dynarray_get_field(slot, 0, f"{arr_var_name}_data")
            data = self.builder.load(data_ptr, name="data")
            elem_ptr = self.builder.gep(
                data, [idx_val],
                inbounds=True, name=f"{arr_var_name}_elem"
            )
            elem_val = self.builder.load(elem_ptr, name=f"{var_name}_val")
            self.builder.store(elem_val, elem_slot)
            
            # 7.15d: continue = inc 블록으로 (증가 후 다음 반복), break = end.
            inc_block = self.current_fn.append_basic_block(name="dynfe_inc")
            self._loop_stack.append((inc_block, end_block))
            try:
                self.compile_stmt(body)
            finally:
                self._loop_stack.pop()
            
            if not self.builder.block.is_terminated:
                self.builder.branch(inc_block)
            # 증가 블록
            self.builder.position_at_end(inc_block)
            idx_after = self.builder.load(idx_slot, name="_foreach_idx")
            next_idx = self.builder.add(
                idx_after, ir.Constant(i32, 1), name="dynfe_next"
            )
            self.builder.store(next_idx, idx_slot)
            self.builder.branch(cond_block)
            
            self.builder.position_at_end(end_block)
        finally:
            self._pop_scope()
    
    def _emit_defers(self):
        """42단계: defer 스택에 쌓인 본문을 LIFO 순서로 컴파일해서 방출한다.
        방출 후 스택을 비워서 이중 방출을 막는다 (compile_return + fall-through 양쪽에서 호출되기 때문)."""
        if not hasattr(self, '_defer_stacks'):
            return
        for scope_defers in reversed(self._defer_stacks):
            for defer_body in reversed(scope_defers):
                for stmt in defer_body[1]:  # Block 노드의 statements
                    self.compile_stmt(stmt)
        self._defer_stacks = [[]]  # 방출 후 초기화

    def compile_return(self, node):
        """('Return', expr_or_None, line)

        return은 끝맺음 명령어 (terminator). 이후 코드는 같은 블록에 못 옴.
        본문 안에서 return 후에도 코드가 더 있으면 (일반적이지 않지만)
        그 코드는 도달 불가능한데, 우리는 일단 새 'unreachable' 블록으로 이동만 함.
        그래야 본문 끝의 is_terminated 검사가 깨끗하게 동작.

        값 없는 return은 6-6b에선 0을 돌려준다 (모든 함수 i32 반환이라).
        """
        # 42단계: return 전에 defer 본문 실행
        self._emit_defers()

        if node[1] is not None:
            value = self.compile_expr(node[1])
            # 반환 타입이 함수 시그니처와 맞는지.
            # 정수를 돌려주는데 함수 반환이 f64면 자동 승격.
            expected_ret = self.current_fn.function_type.return_type
            if value.type != expected_ret:
                if isinstance(value.type, ir.IntType) and isinstance(expected_ret, ir.IntType):
                    if expected_ret.width > value.type.width:
                        value = self.builder.sext(value, expected_ret, name="ret_i_sext")
                    else:
                        value = self.builder.trunc(value, expected_ret, name="ret_i_trunc")
                elif isinstance(value.type, ir.IntType) and isinstance(expected_ret, (ir.DoubleType, ir.FloatType)):
                    value = self.builder.sitofp(value, expected_ret, name="ret_i_fp")
                elif isinstance(value.type, ir.FloatType) and isinstance(expected_ret, ir.DoubleType):
                    value = self.builder.fpext(value, expected_ret, name="ret_f_ext")
                elif isinstance(value.type, ir.DoubleType) and isinstance(expected_ret, ir.FloatType):
                    value = self.builder.fptrunc(value, expected_ret, name="ret_f_trunc")
                else:
                    raise DanhaTypeError(
                        f"[{node[-1]}번째 줄] 반환 타입이 안 맞아 "
                        f"(기대 {expected_ret}, 실제 {value.type})"
                    )
            self.builder.ret(value)
        else:
            # 값 없는 return. 반환 타입 기본값 (i32는 0, f64는 0.0).
            ret_ty = self.current_fn.function_type.return_type
            if ret_ty == f64:
                self.builder.ret(ir.Constant(f64, 0.0))
            else:
                self.builder.ret(ir.Constant(ret_ty, 0))
        # 7.1.2: 본문에 명시적 return이 나왔다는 걸 기록.
        # 안전망이 구조체 반환 함수에서 친절한 에러를 낼지 판단하는 데 씀.
        self._saw_return = True
        
        # return은 끝맺음이라 같은 블록엔 더 못 씀.
        # 이후 코드를 위해 새 블록으로 이동 (LLVM이 unreachable 블록을 알아서 정리).
        # 이게 없으면 'return 뒤에 또 문장이 있는' 케이스에서 두 번째 ret을 같은 블록에
        # 쓰려다 깨짐.
        unreach = self.current_fn.append_basic_block(name="after_return")
        self.builder.position_at_end(unreach)
    
    def compile_break(self, node):
        """('Break', line)
        
        7.15d: 가장 안쪽 루프의 end 블록으로 무조건 점프.
        _loop_stack이 비었으면 루프 바깥에서 쓴 것 → 친절한 에러.
        return과 마찬가지로 끝맺음 명령이라 이후 코드는 도달 불가능 블록으로.
        """
        line = node[-1]
        if not self._loop_stack:
            raise DanhaRuntimeError("'break'는 루프 안에서만 쓸 수 있어", line=line, source=self._source_code)
        _continue_target, break_target = self._loop_stack[-1]
        self.builder.branch(break_target)
        unreach = self.current_fn.append_basic_block(name="after_break")
        self.builder.position_at_end(unreach)
    
    def compile_continue(self, node):
        """('Continue', line)
        
        7.15d: 가장 안쪽 루프의 증가/조건 블록으로 점프.
        for-루프의 경우 증가를 건너뛰면 안 돼서 inc_block으로 가야 함.
        while은 cond 블록으로 직행 (증가라는 개념이 없음).
        for-each는 inc_bb (다음 엔티티로).
        """
        line = node[-1]
        if not self._loop_stack:
            raise DanhaRuntimeError("'continue'는 루프 안에서만 쓸 수 있어", line=line, source=self._source_code)
        continue_target, _break_target = self._loop_stack[-1]
        self.builder.branch(continue_target)
        unreach = self.current_fn.append_basic_block(name="after_continue")
        self.builder.position_at_end(unreach)
    
    def compile_assign(self, node):
        """('Assign', name, value_expr, var_type, line)

        var_type은 6-6a에서 추가된 선택적 타입 어노테이션 ('i32' 등 또는 None).
        6-6a 시점의 컴파일러는 아직 어노테이션을 무시 - 모든 변수가 i32라 가정.
        6-6b 이후 다른 타입(f64 등)이 들어오면 진짜로 사용.
        """
        name = node[1]
        var_type = node[3] if len(node) > 4 else None
        line = node[-1]

        # 7.15a: const 변수에 재대입 금지
        if name in self._const_vars:
            raise DanhaNameError(
                f"[{node[-1]}번째 줄] '{name}'은(는) const라서 바꿀 수 없어"
            )

        # 7.1.3: 읽기 참조('&T')로 받은 매개변수에 재대입 금지.
        # 'p = other'는 원본이 가리키는 곳을 바꿔치는 의미라 쓰기.
        if name in self._readonly_params:
            raise DanhaValueError(
                f"[{node[-1]}번째 줄] '{name}'은(는) 읽기 참조('&')로 받은 매개변수야 — "
                f"재대입을 허용하려면 '&mut'로 받아야 해"
            )

        # Phase A 수정: 'arr: [f64] = []' 처리.
        # var_type이 DynArrayType이면 빈 리스트의 원소 타입을 어노테이션에서 가져오게 컨텍스트로 전달.
        prev_hint = getattr(self, '_dynarray_elem_hint', None)
        existing_for_hint = self._lookup_var(name)
        if var_type is not None and isinstance(var_type, tuple) and var_type[0] == 'DynArrayType':
            elem_ty, _ = self._resolve_type(var_type[1], f"변수 '{name}'", line)
            self._dynarray_elem_hint = elem_ty
        elif (existing_for_hint is not None and isinstance(node[2], tuple)
              and node[2][0] == 'List' and len(node[2][1]) == 0
              and self._is_dynarray_slot(existing_for_hint)):
            self._dynarray_elem_hint = self._dynarray_elem_ty(existing_for_hint)
        try:
            value = self.compile_expr(node[2])
        finally:
            self._dynarray_elem_hint = prev_hint

        # P1B: 변수 어노테이션이 원시 수치 타입이고 값이 다른 수치 타입이면 자동 승격.
        # 예: 's: i64 = 0'  →  store i64 0 (sext i32 0 to i64)
        if var_type is not None and isinstance(var_type, tuple) and var_type[0] == 'GenericType':
            target_ty, _ = self._resolve_type(var_type, f"변수 '{name}'", line)
            if value.type != target_ty:
                raise DanhaTypeError(
                    f"[{node[-1]}번째 줄] 변수 '{name}'의 타입이 어노테이션과 맞지 않아 "
                    f"({target_ty} <- {value.type})"
                )

        if var_type is not None and isinstance(var_type, tuple) and var_type[0] == 'TypeName':
            tname = var_type[1]
            if tname in self._TYPE_MAP:
                target_ty = self._TYPE_MAP[tname]
                i64ty = ir.IntType(64)
                if value.type != target_ty:
                    if value.type == i32 and target_ty == i64ty:
                        value = self.builder.sext(value, i64ty, name="ann_i64")
                    elif value.type == i32 and target_ty == f64:
                        value = self.builder.sitofp(value, f64, name="ann_f64")
                    elif value.type == i64ty and target_ty == f64:
                        value = self.builder.sitofp(value, f64, name="ann_f64")
                    elif value.type == f64 and target_ty == f32:
                        value = self.builder.fptrunc(value, f32, name="ann_f32")
                    elif value.type == f32 and target_ty == f64:
                        value = self.builder.fpext(value, f64, name="ann_f64")
                    elif value.type == i64ty and target_ty == i32:
                        value = self.builder.trunc(value, i32, name="ann_i32_trunc")
                    # 다른 mismatch는 그대로 두고 아래 일반 검사에서 에러 처리
        
        # 인터프리터 규칙과 같음: 체인을 훑어서 이미 있는 변수면 거기 덮어쓰고,
        # 어디에도 없으면 *현재(가장 안쪽)* 스코프에 새로 만든다.
        # 이 덕에 함수 안에서 'counter = counter + 1' 하면 바깥 counter가 갱신되고,
        # if 블록 안에서 'x = 1' 했다가 그게 바깥의 x를 덮어쓰지 않고 새 변수가 됨
        # — 단 바깥에 같은 이름이 이미 있으면 덮어씀.
        existing = self._lookup_var(name)
        if existing is None:
            # 새 변수: 가장 안쪽 스코프에 슬롯을 잡는다.
            # Stage 78: alloca를 entry 블록으로 호이스트해야 LLVM mem2reg가
            # register로 promote할 수 있다. 예: 'state = ...' 가 while 다음에
            # 처음 등장하면 builder는 while_end 블록 — 거기 alloca를 두면
            # mem2reg가 promotion을 포기하고 매 iter 메모리 왕복이 남는다.
            if self.current_fn is not None:
                slot = self._alloca_at_entry(value.type, name)
            else:
                slot = self.builder.alloca(value.type, name=name)
            self._declare_var(name, slot)
        else:
            # 이미 있는 변수면, 슬롯의 원소 타입과 값 타입이 같아야 함.
            # 'x = 5' 한 다음에 'x = true'는 금지 (Danha는 정적 타입 지향).
            expected = existing.type.pointee
            if value.type != expected:
                if isinstance(value.type, ir.IntType) and isinstance(expected, ir.IntType):
                    if expected.width > value.type.width:
                        value = self.builder.sext(value, expected, name=f'{name}_i_sext')
                    else:
                        value = self.builder.trunc(value, expected, name=f'{name}_i_trunc')
                elif isinstance(value.type, ir.IntType) and isinstance(expected, (ir.DoubleType, ir.FloatType)):
                    value = self.builder.sitofp(value, expected, name=f'{name}_i_fp')
                elif isinstance(value.type, ir.FloatType) and isinstance(expected, ir.DoubleType):
                    value = self.builder.fpext(value, expected, name=f'{name}_f_ext')
                elif isinstance(value.type, ir.DoubleType) and isinstance(expected, ir.FloatType):
                    value = self.builder.fptrunc(value, expected, name=f'{name}_f_trunc')
                else:
                    raise DanhaTypeError(
                        f"[{node[-1]}번째 줄] 변수 '{name}'의 타입이 바뀔 수 없어 "
                        f"({expected} -> {value.type})"
                    )
            slot = existing
        
        # 31: 배열 메서드 결과가 동적 배열 슬롯(alloca)이면
        #     store 하지 않고 슬롯 자체를 변수로 등록
        if isinstance(value, ir.instructions.AllocaInstr) and self._is_dynarray_slot(value):
            self._declare_var(name, value)
            return
        
        self.builder.store(value, slot)
        
        # 29: dyn 값이 변수에 저장되면, 변수 이름으로 trait/type 메타 조회 가능하게
        if hasattr(self, '_dyn_type_to_trait') and value.type in self._dyn_type_to_trait:
            if not hasattr(self, '_dyn_var_meta'):
                self._dyn_var_meta = {}
            if hasattr(self, '_last_dyn_meta') and self._last_dyn_meta is not None:
                self._dyn_var_meta[name] = self._last_dyn_meta
                self._last_dyn_meta = None
        
        # 30: HashMap.new()의 결과(i8*)가 변수에 저장되면 _hm_vars에 등록
        annotated_hashmap = (
            var_type is not None and isinstance(var_type, tuple)
            and var_type[0] == 'GenericType' and var_type[1] == 'HashMap'
        )
        if (hasattr(self, '_last_is_hashmap') and self._last_is_hashmap) or annotated_hashmap:
            if not hasattr(self, '_hm_vars'):
                self._hm_vars = set()
            self._hm_vars.add(name)
            if hasattr(self, '_last_is_hashmap'):
                self._last_is_hashmap = False
    
    def compile_const_def(self, node):
        """7.15a: ('ConstDef', name, value_expr, line)
        
        const는 일반 변수와 동일하게 alloca/store하되,
        이름을 _const_vars에 등록해서 재대입을 금지한다.
        
        7.15d: 최상위 const가 이미 _globals에 사전 등록돼 있으면 (상수식으로
        평가 가능해서), main 컴파일 시점엔 아무 일도 안 함. 이미 글로벌로 올라가 있음.
        
        20c: const의 value_expr이 Comptime이면 인터프리터로 사전 평가하고,
        그 결과를 _comptime_consts에도 저장해서 이후 comptime 블록이 참조 가능하게.
        """
        name = node[1]
        line = node[-1]
        value_expr = node[2]
        
        # 7.15d: 이미 글로벌로 사전 등록된 const면 스킵 (중복 방지)
        if name in self._globals and name in self._const_vars:
            return
        
        # 20c: Comptime 노드면 인터프리터로 평가 → 결과를 _comptime_consts에 등록
        if value_expr[0] == 'Comptime':
            from danha_evaluator import evaluate
            comptime_scope = self._make_comptime_scope()
            try:
                result = evaluate(value_expr[1], comptime_scope)
            except Exception as e:
                raise DanhaComptimeError(
                    f"const '{name}'의 comptime 블록 실행 중 에러: {e}",
                    line=line, source=self._source_code
                )
            self._comptime_consts[name] = result
        else:
            # 리터럴 상수도 _comptime_consts에 등록 (comptime에서 참조 가능하도록)
            self._try_register_comptime_const(name, value_expr)
        
        value = self.compile_expr(node[2])
        
        # 이미 같은 이름의 변수가 있으면 에러
        existing = self._lookup_var(name)
        if existing is not None:
            raise DanhaRuntimeError(
                f"'{name}'은(는) 이미 선언된 이름이야",
                line=line, source=self._source_code
            )
        
        slot = self.builder.alloca(value.type, name=name)
        self._declare_var(name, slot)
        self.builder.store(value, slot)
        self._const_vars.add(name)
    
    def _compile_macro_call(self, node):
        """22c: ('MacroCall', name, args, line)
        
        매크로 호출을 인라인 확장한다:
        1. 매크로 본문 AST를 복사
        2. 파라미터 이름을 인자 값으로 치환
        3. 치환된 본문을 현재 컨텍스트에서 컴파일
        
        comptime처럼 인터프리터에 위임하는 것이 아니라,
        인자를 먼저 컴파일하고, 매크로 본문을 직접 컴파일한다.
        매크로 파라미터는 로컬 변수로 바인딩된다.
        """
        name = node[1]
        args = node[2]
        line = node[-1]
        
        if name not in self._macros:
            raise DanhaNameError(f"정의되지 않은 매크로야: {name}!", line=line, source=self._source_code)
        
        _, params, body, is_variadic = self._macros[name]
        
        # 매크로 파라미터를 로컬 변수로 바인딩
        self._push_scope()
        
        if is_variadic:
            fixed_count = len(params) - 1
            for i, (pname, _) in enumerate(params[:-1]):
                if i < len(args):
                    val = self.compile_expr(args[i])
                    slot = self.builder.alloca(val.type, name=f"macro_{pname}")
                    self.builder.store(val, slot)
                    self._declare_var(pname, slot)
            # 가변 인자 — 컴파일러에서는 배열로 처리하기 어려우니,
            # 인터프리터 위임 방식으로 폴백
            var_param = params[-1][0]
            rest_values = [self.compile_expr(a) for a in args[fixed_count:]]
            # 간단하게: 가변 인자 배열을 alloca로 만들기
            if rest_values:
                elem_type = rest_values[0].type
                arr_type = ir.ArrayType(elem_type, len(rest_values))
                arr_slot = self.builder.alloca(arr_type, name=f"macro_{var_param}")
                for idx, rv in enumerate(rest_values):
                    ptr = self.builder.gep(arr_slot, [ir.Constant(i32, 0), ir.Constant(i32, idx)], inbounds=True)
                    self.builder.store(rv, ptr)
                self._declare_var(var_param, arr_slot)
                # len도 저장
                self._declare_var(f"_macro_len_{var_param}", ir.Constant(i32, len(rest_values)))
        else:
            if len(args) != len(params):
                raise DanhaValueError(
                    f"매크로 '{name}!'은(는) {len(params)}개의 인자가 필요한데 {len(args)}개가 들어왔어",
                    line=line, source=self._source_code
                )
            for (pname, _), arg in zip(params, args):
                val = self.compile_expr(arg)
                slot = self.builder.alloca(val.type, name=f"macro_{pname}")
                self.builder.store(val, slot)
                self._declare_var(pname, slot)
        
        # 매크로 본문을 현재 컨텍스트에서 컴파일
        self.compile_stmt(body)
        self._pop_scope()
    
    def _compile_macro_call_expr(self, node):
        """22c: MacroCall이 식 위치에서 올 때 — 마지막 값을 반환."""
        name = node[1]
        args = node[2]
        line = node[-1]
        
        if name not in self._macros:
            raise DanhaNameError(f"정의되지 않은 매크로야: {name}!", line=line, source=self._source_code)
        
        _, params, body, is_variadic = self._macros[name]
        
        self._push_scope()
        
        if not is_variadic:
            if len(args) != len(params):
                raise DanhaValueError(
                    f"매크로 '{name}!'은(는) {len(params)}개의 인자가 필요한데 {len(args)}개가 들어왔어",
                    line=line, source=self._source_code
                )
            for (pname, _), arg in zip(params, args):
                val = self.compile_expr(arg)
                slot = self.builder.alloca(val.type, name=f"macro_{pname}")
                self.builder.store(val, slot)
                self._declare_var(pname, slot)
        
        # body는 Block 노드 — 마지막 문장의 값을 반환
        result = self.compile_expr(body)
        self._pop_scope()
        return result
    
    def _compile_comptime(self, node):
        """20c: ('Comptime', body, line)
        
        comptime 블록을 인터프리터로 사전 평가하고, 결과를 LLVM 상수로 변환한다.
        
        작동 방식:
        1. _make_comptime_scope로 인터프리터 Scope 생성 (상수, 함수 포함)
        2. 인터프리터로 블록 평가
        3. 결과값의 타입에 따라 LLVM IR 상수로 변환
        """
        from danha_evaluator import evaluate
        
        body = node[1]
        line = node[-1]
        
        comptime_scope = self._make_comptime_scope()
        
        try:
            result = evaluate(body, comptime_scope)
        except Exception as e:
            raise DanhaComptimeError(
                f"comptime 블록 실행 중 에러: {e}", line=line, source=self._source_code
            )
        
        return self._comptime_value_to_ir(result, line)
    
    def _comptime_value_to_ir(self, value, line):
        """인터프리터 결과값을 LLVM IR 상수로 변환한다."""
        if isinstance(value, bool):
            return ir.Constant(i1, 1 if value else 0)
        if isinstance(value, int):
            return ir.Constant(i32, value)
        if isinstance(value, float):
            return ir.Constant(f64, value)
        if isinstance(value, str):
            str_const = self._make_global_string(
                f"comptime_str_{id(value):x}", value + "\0"
            )
            return self.builder.gep(
                str_const,
                [ir.Constant(i32, 0), ir.Constant(i32, 0)],
                inbounds=True, name="comptime_strptr"
            )
        if isinstance(value, list):
            # comptime 배열 → LLVM 글로벌 배열 상수
            if len(value) == 0:
                raise DanhaComptimeError("comptime 배열이 비어있어", line=line, source=self._source_code)
            
            # 원소 타입 추론
            first = value[0]
            if isinstance(first, int) and not isinstance(first, bool):
                elem_type = i32
                constants = [ir.Constant(i32, v) for v in value]
            elif isinstance(first, float):
                elem_type = f64
                constants = [ir.Constant(f64, v) for v in value]
            elif isinstance(first, bool):
                elem_type = i1
                constants = [ir.Constant(i1, 1 if v else 0) for v in value]
            else:
                raise DanhaComptimeError(
                    f"comptime 배열의 원소 타입을 IR로 변환 못 해: {type(first).__name__}",
                    line=line, source=self._source_code
                )
            
            arr_type = ir.ArrayType(elem_type, len(value))
            arr_const = ir.Constant(arr_type, constants)
            
            # 글로벌 상수 배열로 만들기
            gvar_name = f"comptime_arr_{self._comptime_arr_counter}"
            self._comptime_arr_counter += 1
            gvar = ir.GlobalVariable(self.module, arr_type, name=gvar_name)
            gvar.initializer = arr_const
            gvar.global_constant = True
            gvar.linkage = 'internal'
            
            # 배열의 첫 원소 포인터 반환 (기존 배열 접근 방식과 호환)
            # 하지만 기존 코드에서 배열은 alloca로 스택에 잡는다.
            # comptime 배열은 글로벌에 있으니, alloca로 복사해서 반환하자.
            slot = self.builder.alloca(arr_type, name=f"comptime_arr_local_{self._comptime_arr_counter}")
            self.builder.store(arr_const, slot)
            return slot
        
        raise DanhaComptimeError(
            f"comptime 결과를 IR로 변환 못 해: {type(value).__name__} = {value}",
            line=line, source=self._source_code
        )
    
    def compile_print(self, node):
        """('Print', expr, line)
        
        i32면 %d로, i1이면 분기로 true/false 글자를 골라서 %s로 찍는다.
        i1 갈래는 if문이 들어온 6-4부터 가능.
        """
        value = self.compile_expr(node[1])

        if self.runtime_mode == 'direct-os':
            if value.type == i1:
                then_b = self.current_fn.append_basic_block(name="bool_true")
                else_b = self.current_fn.append_basic_block(name="bool_false")
                end_b = self.current_fn.append_basic_block(name="bool_end")
                self.builder.cbranch(value, then_b, else_b)
                self.builder.position_at_end(then_b)
                true_ptr = self.builder.bitcast(self.str_true_plain, i8p)
                self.builder.branch(end_b)
                self.builder.position_at_end(else_b)
                false_ptr = self.builder.bitcast(self.str_false_plain, i8p)
                self.builder.branch(end_b)
                self.builder.position_at_end(end_b)
                chosen = self.builder.phi(i8p, name="bool_str")
                chosen.add_incoming(true_ptr, then_b)
                chosen.add_incoming(false_ptr, else_b)
                self._direct_os_write_cstr(chosen, include_newline=True)
                return

            if value.type == i8p:
                self._direct_os_write_cstr(value, include_newline=True)
                return

            if isinstance(value.type, ir.IntType):
                self._direct_os_print_i64(value)
                return

            raise DanhaRuntimeError(
                "direct-os 런타임은 현재 print(str/bool/int)만 지원해. "
                "float/vector/reflection 출력은 libc 런타임으로 컴파일해줘."
            )
        
        if value.type == i1:
            # 6-4: 진짜 'true'/'false' 글자로.
            # 핵심: 분기로 두 문자열 중 하나의 주소를 골라야 한다.
            # 그러려면 새 블록 두 개와 합류 블록 하나가 필요한데,
            # 합류 지점에서 "어느 쪽에서 왔든 그 결과를 받는" 도구가 phi다.
            # phi = "이 블록에 어느 길로 도착했느냐에 따라 값을 다르게 받음".
            # 사람 말로: "경우의 수가 합쳐지는 지점에서 골라받기".
            then_b = self.current_fn.append_basic_block(name="bool_true")
            else_b = self.current_fn.append_basic_block(name="bool_false")
            end_b = self.current_fn.append_basic_block(name="bool_end")
            
            self.builder.cbranch(value, then_b, else_b)
            
            # true 갈래: str_true의 i8* 주소
            self.builder.position_at_end(then_b)
            true_ptr = self.builder.bitcast(self.str_true, i8p)
            self.builder.branch(end_b)
            
            # false 갈래: str_false의 i8* 주소
            self.builder.position_at_end(else_b)
            false_ptr = self.builder.bitcast(self.str_false, i8p)
            self.builder.branch(end_b)
            
            # 합류: phi로 두 갈래의 결과 중 하나를 받는다
            self.builder.position_at_end(end_b)
            chosen = self.builder.phi(i8p, name="bool_str")
            chosen.add_incoming(true_ptr, then_b)
            chosen.add_incoming(false_ptr, else_b)
            
            # printf("%s", chosen)
            fmt_ptr = self.builder.bitcast(self.fmt_str, i8p)
            self.builder.call(self.printf, [fmt_ptr, chosen])
            return
        
        # 7.15c: 문자열 갈래: %s\n으로
        if value.type == i8p:
            fmt_ptr = self.builder.bitcast(self.fmt_str_nl, i8p)
            self.builder.call(self.printf, [fmt_ptr, value])
            return
        
        # 정수 갈래.
        # P1B: i64는 %lld로 출력해서 64-bit 전체를 살린다.
        # i32 이하는 sext로 i32로 올리고 %d로.
        if isinstance(value.type, ir.IntType):
            bits = value.type.width
            if bits == 64:
                fmt_ptr = self.builder.bitcast(self.fmt_lld, i8p)
                self.builder.call(self.printf, [fmt_ptr, value])
                return
            if bits == 32:
                iv = value
            elif bits < 32:
                iv = self.builder.sext(value, i32, name="print_sx32")
            else:
                iv = self.builder.trunc(value, i32, name="print_tr32")
            fmt_ptr = self.builder.bitcast(self.fmt_int, i8p)
            self.builder.call(self.printf, [fmt_ptr, iv])
            return
        
        # 실수 갈래: %g로. 7.15f: f32는 f64로 확장.
        if isinstance(value.type, (ir.DoubleType, ir.FloatType)):
            fv = value
            if isinstance(value.type, ir.FloatType):
                fv = self.builder.fpext(value, f64, name="print_fext")
            fmt_ptr = self.builder.bitcast(self.fmt_float, i8p)
            self.builder.call(self.printf, [fmt_ptr, fv])
            return
        
        # 7.12d1: EntityId 출력 — "Entity(idx, gen)\n"
        # value.type이 {i32, i32}면 entity_id_type으로 간주.
        # 구조체 값에서 필드 두 개 꺼내서 printf에 넘김.
        if value.type == self.entity_id_type:
            idx = self.builder.extract_value(value, 0, name="eid_idx")
            gen = self.builder.extract_value(value, 1, name="eid_gen")
            fmt_ptr = self.builder.bitcast(self.fmt_entity, i8p)
            self.builder.call(self.printf, [fmt_ptr, idx, gen])
            return
        
        # 7.7: 벡터 출력 — "vec3(1.0, 2.0, 3.0)" 형태
        vec_info = self._is_vec_type(value.type)
        if vec_info is not None:
            vec_name, size = vec_info
            # printf 포맷 문자열 생성: "vec3(%g, %g, %g)\n"
            inner = ', '.join(['%g'] * size)
            fmt_text = f"{vec_name}({inner})\n\0"
            fmt_const = ir.Constant(
                ir.ArrayType(ir.IntType(8), len(fmt_text)),
                bytearray(fmt_text.encode('utf-8'))
            )
            fmt_global = ir.GlobalVariable(self.module, fmt_const.type,
                                           name=f".vec_fmt_{vec_name}_{id(node):x}")
            fmt_global.linkage = 'internal'
            fmt_global.global_constant = True
            fmt_global.initializer = fmt_const
            fmt_ptr = self.builder.bitcast(fmt_global, i8p)
            
            # 성분들을 꺼내서 printf에 넘김 (vec3 패딩 lane 제외)
            # f32 성분은 printf 가변인자로 보내려면 f64로 promote 필요 (C ABI)
            args = [fmt_ptr]
            for idx in range(size):
                comp = self._vec_extract(value, idx, name=f"p{idx}")
                if comp.type == f32:
                    comp = self.builder.fpext(comp, f64, name=f"p{idx}_d")
                args.append(comp)
            self.builder.call(self.printf, args)
            return
        
        # 7.12d3: component value 출력 — "Position { x: 1.0, y: 2.0 }" 형태.
        # get()이 반환하는 값의 타입은 self.components[name]['value_type'].
        # 해당 타입이면 필드 이름을 포맷에 넣어 출력.
        for comp_name, comp_info in self.components.items():
            if value.type == comp_info['value_type']:
                field_names = comp_info['fields']
                # 포맷 예: "Position { x: %g, y: %g }\n"
                parts = [f"{fn}: %g" for fn in field_names]
                fmt_text = f"{comp_name} {{ " + ", ".join(parts) + " }\n\0"
                fmt_const = ir.Constant(
                    ir.ArrayType(ir.IntType(8), len(fmt_text)),
                    bytearray(fmt_text.encode('utf-8'))
                )
                fmt_global = ir.GlobalVariable(
                    self.module, fmt_const.type,
                    name=f".comp_fmt_{comp_name}_{id(node):x}"
                )
                fmt_global.linkage = 'internal'
                fmt_global.global_constant = True
                fmt_global.initializer = fmt_const
                fmt_ptr = self.builder.bitcast(fmt_global, i8p)
                
                printf_args = [fmt_ptr]
                for i in range(len(field_names)):
                    comp = self.builder.extract_value(value, i, name=f"cp{i}")
                    printf_args.append(comp)
                self.builder.call(self.printf, printf_args)
                return
        
        # 7.9a: mat4 출력 — 4행으로 나눠서
        # | %g %g %g %g |
        # | %g %g %g %g |
        # | %g %g %g %g |
        # | %g %g %g %g |
        if self._is_mat_type(value.type):
            row_fmt = "| %g %g %g %g |"
            fmt_text = "\n".join([row_fmt] * 4) + "\n\0"
            fmt_const = ir.Constant(
                ir.ArrayType(ir.IntType(8), len(fmt_text)),
                bytearray(fmt_text.encode('utf-8'))
            )
            fmt_global = ir.GlobalVariable(self.module, fmt_const.type,
                                           name=f".mat_fmt_{id(node):x}")
            fmt_global.linkage = 'internal'
            fmt_global.global_constant = True
            fmt_global.initializer = fmt_const
            fmt_ptr = self.builder.bitcast(fmt_global, i8p)
            
            # 행 순서로 성분을 꺼냄 (열 우선 저장이라 행 순서로 재배열)
            args = [fmt_ptr]
            for row in range(4):
                for col in range(4):
                    comp = self._mat4_elem(value, col, row, name=f"m{row}{col}")
                    args.append(comp)
            self.builder.call(self.printf, args)
            return
        
        # 8.3: tagged enum — tag 값만 출력 (match 도입 전 임시)
        for enum_name, (tagged_ty, variant_info, max_payload) in self.tagged_enums.items():
            if value.type == tagged_ty:
                # 값을 임시 alloca에 저장 (payload를 GEP로 꺼내려면 주소 필요)
                tmp = self.builder.alloca(tagged_ty, name="print_tagged_tmp")
                self.builder.store(value, tmp)
                
                tag_val = self.builder.extract_value(value, 0, name="tag")
                sorted_variants = sorted(variant_info.items(), key=lambda x: x[1][0])
                for vname, (vtag, vtypes) in sorted_variants:
                    cmp_val = self.builder.icmp_signed('==', tag_val, ir.Constant(i32, vtag), name=f"tag_eq_{vtag}")
                    then_b = self.current_fn.append_basic_block(name=f"tag_{vtag}")
                    cont_b = self.current_fn.append_basic_block(name=f"tag_{vtag}_cont")
                    self.builder.cbranch(cmp_val, then_b, cont_b)
                    
                    self.builder.position_at_end(then_b)
                    if vtypes is None or len(vtypes) == 0:
                        label = f"{enum_name}.{vname}\n\0"
                        label_const = self._make_global_string(
                            f".tag_lbl_{enum_name}_{vname}_{id(node):x}", label
                        )
                        label_ptr = self.builder.bitcast(label_const, i8p)
                        fmt_ptr = self.builder.bitcast(self.fmt_str, i8p)
                        self.builder.call(self.printf, [fmt_ptr, label_ptr])
                    else:
                        # payload 있는 variant — 값들을 꺼내서 printf
                        payload_ptr = self.builder.gep(
                            tmp, [ir.Constant(i32, 0), ir.Constant(i32, 1)],
                            inbounds=True, name="pay_ptr"
                        )
                        pay_i8 = self.builder.bitcast(payload_ptr, ir.IntType(8).as_pointer())
                        
                        # printf 인자 수집
                        fmt_parts = []
                        printf_args_extra = []
                        offset = 0
                        for lty in vtypes:
                            ptr = self.builder.gep(pay_i8, [ir.Constant(i32, offset)], inbounds=True)
                            typed_ptr = self.builder.bitcast(ptr, lty.as_pointer())
                            val = self.builder.load(typed_ptr, name="pay_val")
                            if lty == i32:
                                fmt_parts.append("%d")
                                printf_args_extra.append(val)
                                offset += 4
                            elif lty == f64:
                                fmt_parts.append("%g")
                                printf_args_extra.append(val)
                                offset += 8
                            elif lty == i8p:
                                fmt_parts.append("\\\"%s\\\"")
                                printf_args_extra.append(val)
                                offset += 8
                            else:
                                fmt_parts.append("?")
                                offset += 8
                        
                        fmt_str = f"{enum_name}.{vname}({', '.join(fmt_parts)})\n\0"
                        fmt_const = self._make_global_string(
                            f".tag_fmt_{enum_name}_{vname}_{id(node):x}", fmt_str
                        )
                        fmt_p = self.builder.bitcast(fmt_const, i8p)
                        self.builder.call(self.printf, [fmt_p] + printf_args_extra)
                    
                    self.builder.branch(cont_b)
                    self.builder.position_at_end(cont_b)
                return
        
        raise DanhaTypeError(f"print에 지원 안 하는 타입: {value.type}")
    
    def compile_expr(self, node):
        """식을 컴파일해서 그 값을 담은 IR 핸들을 돌려준다."""
        node_type = node[0]
        
        if node_type == 'Number':
            value = node[1]
            # 정수는 i32, 실수는 f64로.
            # 파이썬에서 bool은 int의 서브클래스라 bool 체크는 필요 없음 (Number 노드엔 안 옴).
            if isinstance(value, float):
                return ir.Constant(f64, value)
            return ir.Constant(i32, value)
        
        if node_type == 'Bool':
            # true/false → i1 상수.
            return ir.Constant(i1, 1 if node[1] else 0)
        
        if node_type == 'Null':
            # null → i32 sentinel -2 (optional 없음 표시)
            # Eq/Neq에서 optional 변수와 비교할 때 특수 처리
            return ir.Constant(i32, -2)
        
        # 7.15c: 문자열 리터럴 → LLVM 글로벌 상수 문자열의 i8* 포인터.
        # C의 "hello"와 동일. 읽기 전용 메모리에 배치됨.
        if node_type == 'String':
            text = node[1]
            str_const = self._make_global_string(
                f"str_{id(node):x}", text + "\0"
            )
            return self.builder.gep(
                str_const,
                [ir.Constant(i32, 0), ir.Constant(i32, 0)],
                inbounds=True, name="strptr"
            )
        
        # 17: 문자열 보간 — 각 파트를 문자열로 만든 뒤 _compile_str_concat으로 이음.
        if node_type == 'InterpString':
            parts = node[1]
            line = node[-1]
            if not parts:
                empty_g = self._make_global_string(f"istr_empty_{id(node):x}", "\0")
                return self.builder.gep(
                    empty_g,
                    [ir.Constant(i32, 0), ir.Constant(i32, 0)],
                    inbounds=True, name="istrempty",
                )
            acc = None
            for i, part in enumerate(parts):
                uniq = f"{id(node):x}_{i}"
                if part[0] == 'String':
                    text = part[1]
                    str_const = self._make_global_string(
                        f"istr_lit_{id(node):x}_{i}", text + "\0"
                    )
                    piece = self.builder.gep(
                        str_const,
                        [ir.Constant(i32, 0), ir.Constant(i32, 0)],
                        inbounds=True, name=f"istrp_{i}",
                    )
                else:
                    v = self.compile_expr(part)
                    pline = part[-1] if len(part) > 2 else line
                    piece = self._ir_value_to_interpolation_str_ptr(v, pline, uniq)
                acc = piece if acc is None else self._compile_str_concat(acc, piece)
            return acc
        
        # 비교 연산: 정수면 icmp, 실수면 fcmp.
        # 'icmp'는 integer compare, 'fcmp'는 float compare. 둘 다 결과는 i1 (불리언).
        # icmp_signed: slt, sgt 등 부호 있는 정수 비교.
        # fcmp_ordered: ordered float compare. 'ordered'는 "NaN이 끼면 비교 결과가 false"
        #   라는 의미. 대부분의 일상 코드에선 ordered가 원하는 거. unordered는 "NaN이면 true"라
        #   주로 NaN 검출 같은 특수 용도.
        # 수치 승격: 한쪽만 실수면 반대쪽을 실수로 올려서 둘 다 fcmp로.
        COMPARE_OPS = {
            'Lt':  ('<',  '<'),
            'Gt':  ('>',  '>'),
            'Lte': ('<=', '<='),
            'Gte': ('>=', '>='),
            'Eq':  ('==', '=='),
            'Neq': ('!=', '!='),
        }
        if node_type in COMPARE_OPS:
            # 28단계: optional null 비교 특수 처리
            # "v == null" → 왼쪽이 optional 바인딩 변수이고 오른쪽이 Null이면
            #   → __opt_has_v == 0 비교로 변환
            if node_type in ('Eq', 'Neq') and node[2][0] == 'Null':
                left_node = node[1]
                if left_node[0] == 'Name':
                    var_name = left_node[1]
                    has_key = f"__opt_has_{var_name}"
                    has_slot = self._lookup_var(has_key)
                    if has_slot is not None:
                        has_val = self.builder.load(has_slot, name=f"null_chk_{var_name}")
                        zero = ir.Constant(i32, 0)
                        int_op = COMPARE_OPS[node_type][0]
                        return self.builder.icmp_signed(int_op, has_val, zero, name="null_cmp")
            if node_type in ('Eq', 'Neq') and node[1][0] == 'Null':
                right_node = node[2]
                if right_node[0] == 'Name':
                    var_name = right_node[1]
                    has_key = f"__opt_has_{var_name}"
                    has_slot = self._lookup_var(has_key)
                    if has_slot is not None:
                        has_val = self.builder.load(has_slot, name=f"null_chk_{var_name}")
                        zero = ir.Constant(i32, 0)
                        int_op = COMPARE_OPS[node_type][0]
                        return self.builder.icmp_signed(int_op, has_val, zero, name="null_cmp")

            # 29: dyn == null 비교 — dyn은 항상 non-null (as dyn로 만들면 유효한 값)
            # Eq → false, Neq → true
            if node_type in ('Eq', 'Neq'):
                # "a == null" 형태에서 a가 dyn 타입인 경우
                if node[2][0] == 'Null' and node[1][0] == 'Name':
                    slot = self._lookup_var(node[1][1])
                    if slot is not None and hasattr(self, '_dyn_type_to_trait') and slot.type.pointee in self._dyn_type_to_trait:
                        return ir.Constant(i1, 0 if node_type == 'Eq' else 1)
                # "null == a" 형태
                if node[1][0] == 'Null' and node[2][0] == 'Name':
                    slot = self._lookup_var(node[2][1])
                    if slot is not None and hasattr(self, '_dyn_type_to_trait') and slot.type.pointee in self._dyn_type_to_trait:
                        return ir.Constant(i1, 0 if node_type == 'Eq' else 1)

            left = self.compile_expr(node[1])
            right = self.compile_expr(node[2])
            int_op, float_op = COMPARE_OPS[node_type]
            
            # 불리언끼리 비교는 Eq/Neq만 허용 (and/or 결과 비교가 의미 있음).
            # 순서 비교(< > <= >=)는 불리언에선 말이 안 되니 막음.
            if left.type == i1 and right.type == i1:
                if node_type in ('Eq', 'Neq'):
                    return self.builder.icmp_unsigned(int_op, left, right, name="cmptmp")
                raise DanhaRuntimeError(f"[{node[-1]}번째 줄] 불리언엔 '<' '>' 같은 순서 비교 못 써")
            
            # 7.15c: 문자열 비교 — strcmp(a, b)를 기준으로 ==, != 및 순서 비교를 지원.
            # danhac.dh의 문자 범위 검사(c >= "a" and c <= "z")도 이 경로를 사용한다.
            if left.type == i8p and right.type == i8p:
                strcmp_result = self.builder.call(self.strcmp_fn, [left, right], name="strcmp")
                zero = ir.Constant(i32, 0)
                return self.builder.icmp_signed(int_op, strcmp_result, zero, name="strcmp_cmp")
            
            left, right, is_float = self._promote_numeric(left, right, node[-1], node_type)
            if is_float:
                return self.builder.fcmp_ordered(float_op, left, right, name="cmptmp")
            return self.builder.icmp_signed(int_op, left, right, name="cmptmp")
        
        # 구조체 인스턴스 생성: Player { hp: 100, atk: 25 }
        # alloca로 자리 잡고, 각 필드에 GEP+store로 값 채움.
        # 이 식의 '값'은 alloca한 구조체 자체 (load해서 값으로). 그래서 'p = Player {...}'
        # 같은 대입에서 compile_assign이 받아서 변수 슬롯에 store 하면 옳게 동작.
        if node_type == 'StructInstance':
            type_name = node[1]
            field_exprs = node[2]
            line = node[-1]
            
            # 9.1c: from ... import로 가져온 구조체 이름 치환
            if type_name in self._from_imports:
                type_name = self._from_imports[type_name]
            
            if type_name not in self.structs:
                raise DanhaNameError(f"정의되지 않은 구조체야: {type_name}", line=line, source=self._source_code)
            llvm_struct, field_names, field_types = self.structs[type_name]

            for fname in field_exprs:
                if fname not in field_names:
                    raise DanhaNameError(f"{type_name}에 '{fname}'이라는 필드는 없어", line=line, source=self._source_code)

            # 46단계: union 초기화 — 부분 필드(1개 이상)를 bitcast로 저장
            if type_name in self.unions:
                if not field_exprs:
                    raise DanhaValueError(f"{type_name} union은 최소 1개 필드를 초기화해야 해", line=line, source=self._source_code)
                # Stage 75c: 루프 안에서도 안전하도록 entry block 호이스트.
                tmp = self._alloca_at_entry(llvm_struct, name="union_tmp")
                for fname, fval_node in field_exprs.items():
                    idx = field_names.index(fname)
                    value = self.compile_expr(fval_node)
                    expected = field_types[idx]
                    if value.type != expected:
                        if value.type == i32 and expected == f64:
                            value = self.builder.sitofp(value, f64)
                        else:
                            raise DanhaTypeError(
                                f"{type_name}.{fname}의 타입이 안 맞아 "
                                f"(기대 {expected}, 실제 {value.type})",
                                line=line, source=self._source_code
                            )
                    _base = self.builder.gep(tmp, [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True)
                    _ptr = self.builder.bitcast(_base, expected.as_pointer(), name=f"{fname}_ptr")
                    self.builder.store(value, _ptr)
                return self.builder.load(tmp, name="union_val")

            # struct 초기화 — 모든 필드 필수
            for fname in field_names:
                if fname not in field_exprs:
                    raise DanhaNameError(f"{type_name}의 필드 '{fname}'이(가) 없어", line=line, source=self._source_code)

            # 임시 자리 잡기 (alloca). 이 식의 결과는 이 자리에 채운 구조체의 '값'.
            # Stage 75c: 루프 안에서도 안전하도록 entry block 호이스트. mem2reg 친화.
            tmp = self._alloca_at_entry(llvm_struct, name="struct_tmp")
            for idx, fname in enumerate(field_names):
                expected = field_types[idx]
                fexpr = field_exprs[fname]

                # 배열 타입 필드 특별 처리
                if isinstance(expected, ir.ArrayType):
                    if fexpr[0] == 'List' and len(fexpr[1]) == 0:
                        # [] → 영 초기화 (zeroinitializer)
                        value = ir.Constant(expected, None)
                    elif fexpr[0] == 'List':
                        elems = fexpr[1]
                        if len(elems) != expected.count:
                            raise DanhaTypeError(
                                f"{type_name}.{fname}: 배열 길이가 안 맞아 "
                                f"(기대 {expected.count}, 실제 {len(elems)})",
                                line=line, source=self._source_code
                            )
                        elem_vals = [self.compile_expr(e) for e in elems]
                        for ei, ev in enumerate(elem_vals):
                            if ev.type != expected.element:
                                if ev.type == i32 and expected.element == f64:
                                    elem_vals[ei] = self.builder.sitofp(ev, f64)
                                else:
                                    raise DanhaTypeError(
                                        f"{type_name}.{fname}[{ei}] 타입이 안 맞아 "
                                        f"(기대 {expected.element}, 실제 {ev.type})",
                                        line=line, source=self._source_code
                                    )
                        # Stage 75c: 루프 안에서도 안전하도록 entry block 호이스트.
                        arr_tmp = self._alloca_at_entry(expected, name=f"{fname}_arr")
                        for ei, ev in enumerate(elem_vals):
                            ep = self.builder.gep(
                                arr_tmp,
                                [ir.Constant(i32, 0), ir.Constant(i32, ei)],
                                inbounds=True
                            )
                            self.builder.store(ev, ep)
                        value = self.builder.load(arr_tmp, name=f"{fname}_arr_val")
                    else:
                        value = self.compile_expr(fexpr)
                        if value.type != expected:
                            raise DanhaTypeError(
                                f"{type_name}.{fname}의 타입이 안 맞아 "
                                f"(기대 {expected}, 실제 {value.type})",
                                line=line, source=self._source_code
                            )
                else:
                    value = self.compile_expr(fexpr)
                    # 정수→실수 자동 승격 (산술과 같은 정신).
                    # L-1 패치 (2026-05-19): bool 리터럴(i1) → bool 필드(i8) 자동 확장.
                    # `struct Foo { x: bool }` + `Foo { x: true }` 패턴 지원.
                    if value.type != expected:
                        if value.type == i32 and expected == f64:
                            value = self.builder.sitofp(value, f64)
                        elif value.type == i32 and expected == i64:
                            value = self.builder.sext(value, i64, name='i32_to_i64')
                        elif value.type == f64 and expected == f32:
                            value = self.builder.fptrunc(value, f32, name='f64_to_f32')
                        elif value.type == i1 and expected == i8:
                            value = self.builder.zext(value, i8, name='bool_to_i8')
                        else:
                            raise DanhaTypeError(
                                f"{type_name}.{fname}의 타입이 안 맞아 "
                                f"(기대 {expected}, 실제 {value.type})",
                                line=line, source=self._source_code
                            )
                # GEP: 0 (포인터 한 단계 들어감), idx (필드 번호).
                field_ptr = self.builder.gep(
                    tmp, [ir.Constant(i32, 0), ir.Constant(i32, idx)],
                    inbounds=True, name=f"{fname}_ptr"
                )
                self.builder.store(value, field_ptr)

            # 식 결과는 구조체 값. compile_assign이 이 값을 변수 슬롯에 통째 store.
            return self.builder.load(tmp, name="struct_val")
        
        # 필드 읽기: p.hp
        # 객체 식 (보통 변수)의 '주소'에 GEP + load.
        if node_type == 'FieldAccess':
            obj_node = node[1]
            field_name = node[2]
            line = node[-1]
            
            # 9.1c: 모듈 네임스페이스 접근 — math.PI, math.Color.Red 등
            if obj_node[0] == 'Name' and obj_node[1] in self._modules:
                mod_name = obj_node[1]
                name_map = self._modules[mod_name]
                if field_name not in name_map:
                    raise DanhaNameError(
                        f"모듈 '{mod_name}'에 '{field_name}'이 없어",
                        line=line, source=self._source_code
                    )
                # 접두사가 붙은 이름으로 치환하여 기존 경로 재활용
                prefixed = name_map[field_name]
                # enum인지 확인
                if prefixed in self.enums:
                    return self.compile_expr(('Name', prefixed, line))
                if prefixed in self.tagged_enums:
                    return self.compile_expr(('Name', prefixed, line))
                if hasattr(self, '_generic_enums') and prefixed in self._generic_enums:
                    return self.compile_expr(('Name', prefixed, line))
                # const/변수인지 확인
                slot = self._lookup_var(prefixed)
                if slot is not None:
                    return self.builder.load(slot, name=prefixed)
                # struct 정의 자체에 접근한 경우 (구조체 인스턴스 생성은 StructInstance에서)
                raise DanhaImportError(
                    f"모듈 '{mod_name}'의 '{field_name}'은 직접 값으로 쓸 수 없는 정의야",
                    line=line, source=self._source_code
                )
            
            # 9.1c: from ... import로 가져온 이름이면 접두사 이름으로 치환
            # 예: from types import Color → Color.Red → _mod_types_Color.Red
            if obj_node[0] == 'Name' and obj_node[1] in self._from_imports:
                prefixed = self._from_imports[obj_node[1]]
                obj_node = ('Name', prefixed, obj_node[-1])
            
            # 7.15b: enum variant 접근 — Phase.Chase → i32 상수
            if obj_node[0] == 'Name' and obj_node[1] in self.enums:
                enum_name = obj_node[1]
                variant_map = self.enums[enum_name]
                if field_name not in variant_map:
                    raise DanhaNameError(
                        f"enum '{enum_name}'에 '{field_name}'이라는 variant가 없어",
                        line=line, source=self._source_code
                    )
                return ir.Constant(i32, variant_map[field_name])
            
            # 8.3: tagged enum — 데이터 없는 variant (예: Msg.Quit)
            if obj_node[0] == 'Name' and obj_node[1] in self.tagged_enums:
                enum_name = obj_node[1]
                tagged_ty, variant_info, max_payload = self.tagged_enums[enum_name]
                if field_name not in variant_info:
                    raise DanhaNameError(
                        f"enum '{enum_name}'에 '{field_name}'이라는 variant가 없어",
                        line=line, source=self._source_code
                    )
                tag, llvm_types = variant_info[field_name]
                if llvm_types is not None:
                    raise DanhaRuntimeError(
                        f"'{field_name}'은(는) 데이터를 가진 variant야 — "
                        f"{enum_name}.{field_name}(...) 형태로 써",
                        line=line, source=self._source_code
                    )
                return self._build_tagged_enum(enum_name, tag, None, [], line)
            
            # 8.6: 제네릭 enum — 데이터 없는 variant (예: Result.None)
            if obj_node[0] == 'Name' and hasattr(self, '_generic_enums') and obj_node[1] in self._generic_enums:
                return self._instantiate_generic_enum(obj_node[1], field_name, [], line)
            
            # 7.7: 벡터 필드 접근 (.x, .y, .z, .w)
            # 벡터는 사용자 정의 구조체가 아니라 내장 타입이라 _lvalue_struct를 안 거침.
            if obj_node[0] == 'Name':
                slot = self._lookup_var(obj_node[1])
                if slot is not None:
                    vec_info = self._is_vec_slot(slot)
                    if vec_info is not None:
                        vec_name, size = vec_info
                        fields = self._VEC_FIELDS[vec_name]
                        if field_name not in fields:
                            raise DanhaNameError(
                                f"{vec_name}에 '{field_name}'이라는 필드가 없어",
                                line=line, source=self._source_code
                            )
                        idx = fields[field_name]
                        # VectorType: load+extract, struct: GEP+load
                        return self._vec_load_extract(slot, idx, name=field_name)
            
            obj_ptr, struct_info = self._lvalue_struct_or_none(obj_node, line)
            
            if obj_ptr is not None:
                # lvalue 경로: 변수 또는 중첩 필드 → GEP/bitcast + load
                llvm_struct, field_names, field_types = struct_info

                if field_name not in field_names:
                    raise DanhaNameError(f"그 구조체에 '{field_name}'이라는 필드가 없어", line=line, source=self._source_code)
                idx = field_names.index(field_name)

                # 46단계: union은 bitcast, struct는 indexed GEP
                _sname = self._struct_name_from_llvm(llvm_struct)
                if _sname in self.unions:
                    _base = self.builder.gep(obj_ptr, [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True)
                    field_ptr = self.builder.bitcast(_base, field_types[idx].as_pointer(), name=f"{field_name}_ptr")
                else:
                    field_ptr = self.builder.gep(
                        obj_ptr, [ir.Constant(i32, 0), ir.Constant(i32, idx)],
                        inbounds=True, name=f"{field_name}_ptr"
                    )
                return self.builder.load(field_ptr, name=field_name)
            
            # 8.1b: rvalue 폴백 — 함수 반환값 등 임시 값에서 필드 읽기
            # compile_expr로 값을 얻고, 임시 alloca에 저장 후 GEP로 필드 꺼냄
            obj_val = self.compile_expr(obj_node)
            obj_ty = obj_val.type
            
            # 벡터 타입이면 벡터 필드 접근
            vec_info = self._is_vec_type(obj_ty)
            if vec_info is not None:
                vec_name, size = vec_info
                fields = self._VEC_FIELDS[vec_name]
                if field_name not in fields:
                    raise DanhaNameError(
                        f"{vec_name}에 '{field_name}'이라는 필드가 없어",
                        line=line, source=self._source_code
                    )
                idx = fields[field_name]
                # VectorType: 값에서 직접 extract_element (alloca 불필요)
                if isinstance(obj_ty, ir.VectorType):
                    return self.builder.extract_element(obj_val, ir.Constant(i32, idx), name=field_name)
                tmp = self.builder.alloca(obj_ty, name="vec_rval_tmp")
                self.builder.store(obj_val, tmp)
                field_ptr = self.builder.gep(
                    tmp, [ir.Constant(i32, 0), ir.Constant(i32, idx)],
                    inbounds=True, name=f"{field_name}_ptr"
                )
                return self.builder.load(field_ptr, name=field_name)
            
            # 구조체 값인지 확인
            if not isinstance(obj_ty, ir.IdentifiedStructType):
                raise DanhaRuntimeError("필드 접근은 구조체나 벡터에만 할 수 있어", line=line, source=self._source_code)
            
            # 어떤 구조체인지 역검색
            target_info = None
            target_sname = None
            for sname, info in self.structs.items():
                if info[0] is obj_ty:
                    target_info = info
                    target_sname = sname
                    break
            if target_info is None:
                raise DanhaTypeError(f"알 수 없는 구조체 타입: {obj_ty}", line=line, source=self._source_code)

            llvm_struct, field_names, field_types = target_info
            if field_name not in field_names:
                raise DanhaNameError(f"그 구조체에 '{field_name}'이라는 필드가 없어", line=line, source=self._source_code)
            idx = field_names.index(field_name)

            # 임시 alloca에 저장 후 GEP/bitcast
            tmp = self.builder.alloca(obj_ty, name="rval_tmp")
            self.builder.store(obj_val, tmp)
            # 46단계: union은 bitcast, struct는 indexed GEP
            if target_sname in self.unions:
                _base = self.builder.gep(tmp, [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True)
                field_ptr = self.builder.bitcast(_base, field_types[idx].as_pointer(), name=f"{field_name}_ptr")
            else:
                field_ptr = self.builder.gep(
                    tmp, [ir.Constant(i32, 0), ir.Constant(i32, idx)],
                    inbounds=True, name=f"{field_name}_ptr"
                )
            return self.builder.load(field_ptr, name=field_name)
        
        # ----- 7.2/7.4: 배열 리터럴 -----
        # 원소가 있으면 → 고정 배열 [N x T] (기존 7.2 동작)
        # 빈 리스트 [] → 동적 배열 { T*, i32, i32 } (7.4 신규)
        if node_type == 'List':
            elements = node[1]
            line = node[-1]
            
            # ----- 7.4: 빈 리스트 → 동적 배열 -----
            if len(elements) == 0:
                # 원소 타입: 'arr: [T] = []' 어노테이션에서 힌트를 받았으면 T 사용, 아니면 i32 기본.
                hint = getattr(self, '_dynarray_elem_hint', None)
                if hint is not None:
                    elem_ty = hint
                else:
                    elem_ty = i32
                # 빈 동적 배열: { data: null, len: 0, cap: 0 }
                dyn_ty = self._get_dynarray_type(elem_ty)
                tmp = self.builder.alloca(dyn_ty, name="dynarray_tmp")
                null_ptr = ir.Constant(elem_ty.as_pointer(), None)
                self.builder.store(
                    null_ptr,
                    self._dynarray_get_field(tmp, 0, "data_init")
                )
                self.builder.store(
                    ir.Constant(i32, 0),
                    self._dynarray_get_field(tmp, 1, "len_init")
                )
                self.builder.store(
                    ir.Constant(i32, 0),
                    self._dynarray_get_field(tmp, 2, "cap_init")
                )
                return self.builder.load(tmp, name="dynarray_val")
            
            # ----- 7.2: 원소 있는 리스트 → 고정 배열 -----
            
            # 원소 평가
            values = [self.compile_expr(e) for e in elements]
            
            # 첫 원소 타입이 배열의 원소 타입. 나머지가 다르면 승격 시도.
            elem_ty = values[0].type
            for idx_v, v in enumerate(values):
                if v.type != elem_ty:
                    # i32 → f64 자동 승격 (산술과 같은 정신)
                    if v.type == i32 and elem_ty == f64:
                        values[idx_v] = self.builder.sitofp(v, f64)
                    elif v.type == f64 and elem_ty == i32:
                        # 첫 원소가 정수였지만 뒤에 실수가 나옴 → 전체를 f64로
                        elem_ty = f64
                        for j in range(len(values)):
                            if values[j].type == i32:
                                values[j] = self.builder.sitofp(values[j], f64)
                    else:
                        raise DanhaTypeError(
                            f"배열의 {idx_v}번째 원소 타입이 안 맞아 "
                            f"(기대 {elem_ty}, 실제 {v.type})",
                            line=line, source=self._source_code
                        )
            
            arr_ty = ir.ArrayType(elem_ty, len(values))
            tmp = self.builder.alloca(arr_ty, name="arr_tmp")
            for idx_v, v in enumerate(values):
                ptr = self.builder.gep(
                    tmp, [ir.Constant(i32, 0), ir.Constant(i32, idx_v)],
                    inbounds=True, name=f"arr_elem_{idx_v}"
                )
                self.builder.store(v, ptr)
            
            return self.builder.load(tmp, name="arr_val")
        
        # ----- 7.2: 인덱스 읽기 arr[i] -----
        # 객체(배열)의 변수 슬롯에 GEP로 원소 접근 → load.
        # 지금은 정수 인덱스만. 범위 검사는 런타임에 해야 하지만
        # 이 단계에서는 상수 인덱스일 때만 컴파일 타임 검사.
        if node_type == 'Index':
            obj_node = node[1]
            idx_node = node[2]
            line = node[-1]

            # 구조체 배열 필드 접근: b.lines[3]
            if obj_node[0] == 'FieldAccess':
                arr_ptr, elem_ty, _arr_len = self._lvalue_array_field(obj_node, line)
                idx_val = self.compile_expr(idx_node)
                if idx_val.type != i32:
                    raise DanhaValueError("배열 인덱스는 정수여야 해", line=line, source=self._source_code)
                elem_ptr = self.builder.gep(
                    arr_ptr, [ir.Constant(i32, 0), idx_val],
                    inbounds=True, name="arr_field_elem"
                )
                return self.builder.load(elem_ptr, name="arr_field_val")

            # 객체는 변수여야 함 (슬롯 주소 필요)
            if obj_node[0] != 'Name':
                raise DanhaValueError(
                    f"인덱스 접근은 지금 변수에만 할 수 있어",
                    line=line, source=self._source_code
                )
            var_name = obj_node[1]

            idx_val = self.compile_expr(idx_node)
            if idx_val.type != i32:
                raise DanhaValueError(
                    f"배열 인덱스는 정수여야 해",
                    line=line, source=self._source_code
                )
            
            # 7.4: 동적 배열인지 확인
            slot = self._lookup_var(var_name)
            if slot is not None and isinstance(slot.type.pointee, ir.PointerType) and slot.type.pointee == i8p:
                idx64 = self.builder.sext(idx_val, i64, name=f"{var_name}_idx64")
                str_val = self.builder.load(slot, name=f"{var_name}_str")
                str_idx_fn = self._ensure_runtime_fn("dnh_str_idx", i8p, [i8p, i64])
                return self.builder.call(str_idx_fn, [str_val, idx64], name=f"{var_name}_ch")
            if slot is not None and self._is_dynarray_slot(slot):
                # 동적 배열: data 포인터에 GEP
                data_ptr = self._dynarray_get_field(slot, 0, f"{var_name}_data")
                data = self.builder.load(data_ptr, name="data")
                elem_ptr = self.builder.gep(
                    data, [idx_val],
                    inbounds=True, name=f"{var_name}_elem"
                )
                return self.builder.load(elem_ptr, name=f"{var_name}_val")
            
            # 고정 배열: 기존 로직
            arr_ptr, elem_ty, arr_len = self._lvalue_array(var_name, line)
            
            elem_ptr = self.builder.gep(
                arr_ptr, [ir.Constant(i32, 0), idx_val],
                inbounds=True, name=f"{var_name}_elem"
            )
            return self.builder.load(elem_ptr, name=f"{var_name}_val")
        
        if node_type == 'Name':
            # 변수에서 값 꺼내기.
            # 안쪽 스코프부터 바깥으로 훑어서 찾는다 (어휘적 스코프).
            name = node[1]
            line = node[-1]
            slot = self._lookup_var(name)
            if slot is None:
                # from ... import * 로 가져온 이름이면 접두사 이름으로 치환 — 상수/변수 모두
                if name in self._from_imports:
                    aliased = self._from_imports[name]
                    aliased_slot = self._lookup_var(aliased)
                    if aliased_slot is not None:
                        return self.builder.load(aliased_slot, name=name)
                    if aliased in self.functions:
                        return self.functions[aliased]
                # 42단계: 함수 포인터 — 함수 이름을 값으로 참조
                if name in self.functions:
                    return self.functions[name]  # ir.Function (포인터 값)
                # extern 함수도 포인터로 참조 가능
                if name in getattr(self, '_extern_fns', {}):
                    return self._extern_fns[name]
                raise DanhaNameError(f"정의되지 않은 이름이야: {name}", line=line, source=self._source_code)
            return self.builder.load(slot, name=name)
        
        # 7.1.3 파트 2: 참조 식 '&x' / '&mut x'
        # 변수의 슬롯 주소를 돌려준다. 함수 호출 시 참조 매개변수에 전달되는 값.
        # 피연산자는 단순 변수(Name)만 허용 — '&5', '&(a+b)', '&f()' 등은
        # 주소를 얻을 '자리'가 없거나 의미가 모호해서 거부.
        # is_mut은 컴파일러가 타입 검사(파트 2.4)와 쓰기 검사(파트 3)에서 사용.
        if node_type == 'AddrOf':
            is_mut = node[1]
            operand = node[2]
            line = node[-1]
            # 참고: & (참조)는 안전한 연산이므로 unsafe가 필요 없음.
            # unsafe가 필요한 건 포인터 산술(정수↔포인터 캐스팅, 오프셋 연산)이다.
            if operand[0] != 'Name':
                kind = "'&mut'" if is_mut else "'&'"
                raise DanhaRuntimeError(
                    f"{kind} 뒤에는 변수 이름만 올 수 있어 (임시값이나 계산 결과는 주소를 못 잡음)",
                    line=line, source=self._source_code
                )
                # 7단계 후반: 필드·인덱스까지 주소 가능하게 확장 예정.
            var_name = operand[1]
            slot = self._lookup_var(var_name)
            if slot is None:
                raise DanhaNameError(f"정의되지 않은 이름이야: {var_name}", line=line, source=self._source_code)
            # 슬롯은 이미 포인터(alloca 결과). 이게 바로 '변수의 주소'.
            # 슬롯 타입: 예) i32 변수 → i32*, Player 변수 → %Player*.
            return slot
        
        if node_type == 'Call':
            # 함수 호출: ('Call', name, args, line)
            # 인자를 평가해서 builder.call에 넘기고, 결과 핸들을 돌려준다.
            name = node[1]
            args = node[2]
            line = node[-1]

            # 9.1c: from ... import로 가져온 이름이면 접두사 이름으로 치환
            if name in self._from_imports:
                name = self._from_imports[name]

            if name not in self.functions:
                for suffix in ('_save_to_path', '_load_from_path'):
                    prefix = '_reflect_'
                    if name.startswith(prefix) and name.endswith(suffix):
                        struct_name = name[len(prefix):-len(suffix)]
                        if struct_name in getattr(self, '_reflection_persistence_defs', {}):
                            self._emit_reflection_persistence_helpers(struct_name)
                        break

            # 42단계: 함수 포인터 변수를 통한 간접 호출
            # 로컬 변수가 함수 포인터 타입(pointer-to-function)이면 indirect call
            fn_ptr_slot = self._lookup_var(name)
            if fn_ptr_slot is not None and not name.startswith('__'):
                slot_pointee = fn_ptr_slot.type.pointee if hasattr(fn_ptr_slot.type, 'pointee') else None
                if isinstance(slot_pointee, ir.PointerType) and isinstance(slot_pointee.pointee, ir.FunctionType):
                    fn_ptr = self.builder.load(fn_ptr_slot, name=f"{name}_fnptr")
                    arg_vals = [self.compile_expr(a) for a in args]
                    return self.builder.call(fn_ptr, arg_vals, name="indirect_call")
            
            # ----- 35단계: @attribute 조회 (컴파일 타임 상수) -----
            if name == 'has_attribute':
                if len(args) != 2:
                    raise DanhaValueError("has_attribute는 2개의 인자가 필요해", line=line, source=self._source_code)
                # 인자가 문자열 리터럴이어야 함
                if args[0][0] != 'String' or args[1][0] != 'String':
                    raise DanhaValueError("has_attribute의 인자는 문자열 리터럴이어야 해", line=line, source=self._source_code)
                target = args[0][1]
                attr = args[1][1]
                attrs = self._comp_attributes.get(target, [])
                result = any(a[0] == attr for a in attrs)
                return ir.Constant(ir.IntType(1), 1 if result else 0)
            
            if name == 'get_attributes':
                # 컴파일러에서는 문자열 리스트 반환이 복잡하므로
                # print와 함께 쓸 수 있게 직접 구현은 보류,
                # has_attribute로 조회하는 것이 주요 사용법.
                raise DanhaValueError(
                    "get_attributes는 인터프리터 전용이야. 컴파일러에서는 has_attribute를 써줘",
                    line=line, source=self._source_code)
            
            # ----- 7.7: 벡터 생성 (vec2, vec3, vec4) -----
            # vec3(1.0, 2.0, 3.0) → LLVM 구조체 {f64, f64, f64} 값을 조립.
            # insert_value로 성분을 하나씩 넣는다.
            # 비유: 빈 상자에 칸마다 숫자를 하나씩 집어넣기.
            if name in self._VEC_INFO:
                vty, size = self._VEC_INFO[name]
                elem_ty = self._VEC_ELEM_TY[name]  # f32 or f64
                if len(args) != size:
                    raise DanhaValueError(
                        f"{name}은(는) {size}개의 인자가 필요한데 "
                        f"{len(args)}개가 들어왔어",
                        line=line, source=self._source_code
                    )
                # 모든 성분이 컴파일 타임 상수면 ir.Constant로 직접 생성
                vals = []
                all_const = True
                for arg_node in args:
                    val = self.compile_expr(arg_node)
                    # 정수/다른 float → elem_ty 변환
                    if val.type == i32:
                        val = self.builder.sitofp(val, elem_ty, name="vec_promote")
                    elif val.type == f64 and elem_ty == f32:
                        val = self.builder.fptrunc(val, f32, name="vec_to_f32")
                    elif val.type == f32 and elem_ty == f64:
                        val = self.builder.fpext(val, f64, name="vec_to_f64")
                    if val.type != elem_ty:
                        raise DanhaRuntimeError(
                            f"{name}의 성분은 숫자여야 해 (got {val.type})",
                            line=line, source=self._source_code
                        )
                    vals.append(val)
                    if not isinstance(val, ir.Constant):
                        all_const = False
                lane_count = self._VEC_LANE_COUNT.get(name, size)
                if all_const and isinstance(vty, ir.VectorType):
                    constants = [v.constant for v in vals]
                    while len(constants) < lane_count:
                        constants.append(0.0)
                    return ir.Constant(vty, constants)
                result = ir.Constant(vty, ir.Undefined)
                for idx, val in enumerate(vals):
                    result = self._vec_insert(result, val, idx, name=f"vec_build{idx}")
                if isinstance(vty, ir.VectorType):
                    for pad_idx in range(size, lane_count):
                        result = self._vec_insert(result, ir.Constant(elem_ty, 0.0), pad_idx, name=f"vec_pad{pad_idx}")
                return result
            
            # ----- 7.12d1: ECS 내장 — spawn / destroy / is_alive -----
            # 각각 LLVM 런타임 함수(_danha_ecs_*)로 라우팅.
            # spawn은 인자 0, destroy/is_alive는 EntityId 1개.
            #
            # 주의: 사용자가 같은 이름의 함수를 정의했다면 그쪽이 우선.
            # 예: fn add(a, b) { a + b } 는 ECS add를 가리는 게 아니라 그냥 사용자 함수.
            # self.functions에 이미 있으면 여기서 건드리지 않고 일반 호출 경로로 떨어짐.
            if name == 'spawn' and name not in self.functions:
                if len(args) != 0:
                    raise DanhaValueError(
                        f"spawn은 인자를 받지 않는데 {len(args)}개가 들어왔어",
                        line=line, source=self._source_code
                    )
                return self.builder.call(self.ecs_spawn_fn, [], name="eid")
            
            if name == 'destroy' and name not in self.functions:
                if len(args) != 1:
                    raise DanhaValueError(
                        f"destroy는 1개의 인자가 필요한데 {len(args)}개가 들어왔어",
                        line=line, source=self._source_code
                    )
                eid_val = self.compile_expr(args[0])
                if eid_val.type != self.entity_id_type:
                    raise DanhaECSError(
                        f"destroy는 EntityId가 필요한데 다른 값이 왔어",
                        line=line, source=self._source_code
                    )
                return self.builder.call(self.ecs_destroy_fn, [eid_val], name="destroyed")
            
            if name == 'is_alive' and name not in self.functions:
                if len(args) != 1:
                    raise DanhaValueError(
                        f"is_alive는 1개의 인자가 필요한데 {len(args)}개가 들어왔어",
                        line=line, source=self._source_code
                    )
                eid_val = self.compile_expr(args[0])
                if eid_val.type != self.entity_id_type:
                    raise DanhaECSError(
                        f"is_alive는 EntityId가 필요한데 다른 값이 왔어",
                        line=line, source=self._source_code
                    )
                return self.builder.call(self.ecs_is_alive_fn, [eid_val], name="alive")
            
            # ----- 7.12d2: add(e, Position{...}) -----
            # 두 번째 인자는 파서의 StructInstance 노드. 이름이 component면 부착 경로.
            # 인터프리터와 달리 컴파일러는 값으로 먼저 평가하지 않고, AST에서 직접 타입 이름을 본다.
            # 이유: struct vs component 구분이 이름 수준에서만 가능, 파서는 둘 다 StructInstance로 만듦.
            #
            # 주의: 사용자가 fn add(a,b)를 정의했으면 이 경로는 무시되고 일반 호출로 떨어짐.
            if name == 'add' and name not in self.functions and self._lookup_var(name) is None:
                if len(args) != 2:
                    raise DanhaValueError(
                        f"add는 2개의 인자가 필요한데 {len(args)}개가 들어왔어",
                        line=line, source=self._source_code
                    )
                comp_lit = args[1]
                if comp_lit[0] != 'StructInstance':
                    raise DanhaValueError(
                        f"add의 두 번째 인자는 component 값이 필요해 "
                        f"(예: 'Position {{ x: 1, y: 2 }}')",
                        line=line, source=self._source_code
                    )
                comp_name = comp_lit[1]
                if comp_name in self._from_imports:
                    comp_name = self._from_imports[comp_name]
                if comp_name not in self.components:
                    # struct 이름이면 친절한 안내, 아니면 일반 에러
                    if comp_name in self.structs:
                        raise DanhaECSError(
                            f"add: '{comp_name}'은(는) struct야. 'component'로 바꿔야 ECS에 붙일 수 있어",
                            line=line, source=self._source_code
                        )
                    raise DanhaNameError(
                        f"add: 정의되지 않은 component '{comp_name}'",
                        line=line, source=self._source_code
                    )
                
                # 엔티티 값 평가
                eid_val = self.compile_expr(args[0])
                if eid_val.type != self.entity_id_type:
                    raise DanhaValueError(
                        f"add의 첫 인자는 EntityId여야 해",
                        line=line, source=self._source_code
                    )
                
                # 유효성 체크 — 죽은 엔티티면 abort.
                # 더 부드럽게 에러 메시지로 내려면 별도 런타임 함수가 필요하지만,
                # 단순하게 is_alive 호출 후 false면 printf+exit.
                alive = self.builder.call(self.ecs_is_alive_fn, [eid_val], name="alive_chk")
                ok_bb = self.current_fn.append_basic_block(name="add_alive_ok")
                bad_bb = self.current_fn.append_basic_block(name="add_alive_bad")
                self.builder.cbranch(alive, ok_bb, bad_bb)
                
                self.builder.position_at_end(bad_bb)
                msg = self._make_global_string(
                    f"add_dead_msg_{id(comp_lit)}",
                    f"단아 ECS: add에 죽은 엔티티 (component {comp_name})\n\0"
                )
                msg_ptr = self.builder.gep(msg, [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True)
                self.builder.call(self.printf, [msg_ptr])
                self.builder.call(self.exit_fn, [ir.Constant(i32, 1)])
                self.builder.unreachable()
                
                self.builder.position_at_end(ok_bb)
                
                # 두 번째 인자의 필드 값들 평가. field_exprs는 dict {fname: expr_node}.
                field_exprs = comp_lit[2]
                comp_info = self.components[comp_name]
                expected_fields = comp_info['fields']
                
                # 필드 개수/이름 검증 (파서가 못 잡는 경우 대비)
                for fname in expected_fields:
                    if fname not in field_exprs:
                        raise DanhaNameError(f"{comp_name}의 필드 '{fname}'이(가) 없어", line=line, source=self._source_code)
                for fname in field_exprs:
                    if fname not in expected_fields:
                        raise DanhaNameError(f"{comp_name}에 '{fname}'이라는 필드는 없어", line=line, source=self._source_code)
                
                # 7.15f: 필드 값들을 컴포넌트 필드의 선언 타입에 맞춰 평가/변환.
                # 정수 리터럴은 i32로 컴파일됨 → 대상 타입에 맞춰 캐스팅.
                # Q3=3: 정수 → f64/f32 암묵 승격 허용.
                field_types = comp_info['field_types']
                field_type_names = comp_info['field_type_names']
                field_vals = {}
                for fname, tty, tname in zip(expected_fields, field_types, field_type_names):
                    v = self.compile_expr(field_exprs[fname])
                    v = self._coerce_to_component_field(v, tty, tname, fname, comp_name, line)
                    field_vals[fname] = v
                
                # 엔티티 idx 추출
                e_idx = self.builder.extract_value(eid_val, 0, name="add_e_idx")
                
                # sparse[e_idx]를 읽어서 이미 있는지 확인
                sparse_ptr = self.builder.load(comp_info['sparse'])
                sparse_slot_ptr = self.builder.gep(sparse_ptr, [e_idx], inbounds=True, name="sparse_slot")
                existing_dense = self.builder.load(sparse_slot_ptr, name="existing_dense")
                has_already = self.builder.icmp_signed('!=', existing_dense, ir.Constant(i32, -1))
                
                overwrite_bb = self.current_fn.append_basic_block(name="add_overwrite")
                fresh_bb = self.current_fn.append_basic_block(name="add_fresh")
                done_bb = self.current_fn.append_basic_block(name="add_done")
                self.builder.cbranch(has_already, overwrite_bb, fresh_bb)
                
                # overwrite: 기존 dense 슬롯에 필드 값만 갱신
                self.builder.position_at_end(overwrite_bb)
                for fname in expected_fields:
                    farr = self.builder.load(comp_info['field_globals'][fname])
                    fslot = self.builder.gep(farr, [existing_dense], inbounds=True,
                                             name=f"ow_{fname}_slot")
                    self.builder.store(field_vals[fname], fslot)
                self.builder.branch(done_bb)
                
                # fresh: 용량 확인 후 count에 삽입
                self.builder.position_at_end(fresh_bb)
                count_val = self.builder.load(comp_info['count'], name="comp_count")
                cap_val = self.builder.load(comp_info['capacity'], name="comp_cap")
                overflow = self.builder.icmp_signed('>=', count_val, cap_val)
                
                overflow_bb = self.current_fn.append_basic_block(name="add_overflow")
                insert_bb = self.current_fn.append_basic_block(name="add_insert")
                self.builder.cbranch(overflow, overflow_bb, insert_bb)
                
                self.builder.position_at_end(overflow_bb)
                omsg = self._make_global_string(
                    f"comp_overflow_msg_{id(comp_lit)}",
                    f"단아 ECS: {comp_name} 컴포넌트 용량 초과 (cap={self.COMPONENT_CAPACITY})\n\0"
                )
                omsg_ptr = self.builder.gep(omsg, [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True)
                self.builder.call(self.printf, [omsg_ptr])
                self.builder.call(self.exit_fn, [ir.Constant(i32, 1)])
                self.builder.unreachable()
                
                self.builder.position_at_end(insert_bb)
                # dense_idx = count
                # dense_to_entity[dense_idx] = e_idx
                # sparse[e_idx] = dense_idx
                # field_arrays[fname][dense_idx] = field_vals[fname]
                # count++
                dense_idx = count_val
                dte_ptr = self.builder.load(comp_info['dense_to_entity'])
                dte_slot = self.builder.gep(dte_ptr, [dense_idx], inbounds=True)
                self.builder.store(e_idx, dte_slot)
                self.builder.store(dense_idx, sparse_slot_ptr)
                for fname in expected_fields:
                    farr = self.builder.load(comp_info['field_globals'][fname])
                    fslot = self.builder.gep(farr, [dense_idx], inbounds=True,
                                             name=f"fr_{fname}_slot")
                    self.builder.store(field_vals[fname], fslot)
                new_count = self.builder.add(count_val, ir.Constant(i32, 1))
                self.builder.store(new_count, comp_info['count'])
                self.builder.branch(done_bb)
                
                self.builder.position_at_end(done_bb)
                # add는 None 반환. 통계용이 아니므로.
                return ir.Constant(i32, 0)  # 사용자 코드가 대입하지 않을 것. 안전한 더미.
            
            # ----- 7.12d2-iii: has(e, ComponentType) -----
            # 두 번째 인자는 Name('Position', line) 같은 타입 이름.
            # 죽은 엔티티에 대해 false (인터프리터와 같은 정책).
            if name == 'has' and name not in self.functions:
                if len(args) != 2:
                    raise DanhaValueError(
                        f"has는 2개의 인자가 필요한데 {len(args)}개가 들어왔어",
                        line=line, source=self._source_code
                    )
                type_arg = args[1]
                if type_arg[0] != 'Name':
                    raise DanhaTypeError(
                        f"has의 두 번째 인자는 component 타입이 필요해 (예: 'Position')",
                        line=line, source=self._source_code
                    )
                comp_name = type_arg[1]
                if comp_name in self._from_imports:
                    comp_name = self._from_imports[comp_name]
                if comp_name not in self.components:
                    if comp_name in self.structs:
                        raise DanhaECSError(
                            f"has: '{comp_name}'은(는) struct야. 'component'여야 해",
                            line=line, source=self._source_code
                        )
                    raise DanhaNameError(
                        f"has: 정의되지 않은 component '{comp_name}'",
                        line=line, source=self._source_code
                    )
                
                eid_val = self.compile_expr(args[0])
                if eid_val.type != self.entity_id_type:
                    raise DanhaValueError(
                        f"has의 첫 인자는 EntityId여야 해",
                        line=line, source=self._source_code
                    )
                
                comp_info = self.components[comp_name]
                
                # 살아있나? 죽은 엔티티 → false.
                alive = self.builder.call(self.ecs_is_alive_fn, [eid_val], name="has_alive")
                alive_bb = self.current_fn.append_basic_block(name="has_alive_ok")
                dead_bb = self.current_fn.append_basic_block(name="has_dead")
                done_bb = self.current_fn.append_basic_block(name="has_done")
                self.builder.cbranch(alive, alive_bb, dead_bb)
                
                # dead → false
                self.builder.position_at_end(dead_bb)
                dead_result = ir.Constant(ir.IntType(1), 0)
                self.builder.branch(done_bb)
                dead_end = self.builder.block
                
                # alive → sparse[idx] != -1
                self.builder.position_at_end(alive_bb)
                e_idx = self.builder.extract_value(eid_val, 0)
                sparse_ptr = self.builder.load(comp_info['sparse'])
                dense = self.builder.load(
                    self.builder.gep(sparse_ptr, [e_idx], inbounds=True)
                )
                alive_result = self.builder.icmp_signed('!=', dense, ir.Constant(i32, -1))
                self.builder.branch(done_bb)
                alive_end = self.builder.block
                
                self.builder.position_at_end(done_bb)
                phi = self.builder.phi(ir.IntType(1), name="has_result")
                phi.add_incoming(dead_result, dead_end)
                phi.add_incoming(alive_result, alive_end)
                return phi
            
            # ----- 7.12d2-iii: remove(e, ComponentType) -----
            # 엔티티 살아있어야 함 (죽은 엔티티 → 에러).
            # 컴포넌트가 없으면 false, 제거 성공이면 true.
            if name == 'remove' and name not in self.functions:
                if len(args) != 2:
                    raise DanhaValueError(
                        f"remove는 2개의 인자가 필요한데 {len(args)}개가 들어왔어",
                        line=line, source=self._source_code
                    )
                type_arg = args[1]
                if type_arg[0] != 'Name':
                    raise DanhaTypeError(
                        f"remove의 두 번째 인자는 component 타입이 필요해",
                        line=line, source=self._source_code
                    )
                comp_name = type_arg[1]
                if comp_name in self._from_imports:
                    comp_name = self._from_imports[comp_name]
                if comp_name not in self.components:
                    if comp_name in self.structs:
                        raise DanhaRuntimeError(
                            f"remove: '{comp_name}'은(는) struct야",
                            line=line, source=self._source_code
                        )
                    raise DanhaNameError(
                        f"remove: 정의되지 않은 component '{comp_name}'",
                        line=line, source=self._source_code
                    )
                
                eid_val = self.compile_expr(args[0])
                if eid_val.type != self.entity_id_type:
                    raise DanhaValueError(
                        f"remove의 첫 인자는 EntityId여야 해",
                        line=line, source=self._source_code
                    )
                
                # 살아있는지 검사 후 죽어 있으면 abort (add와 같은 정책).
                alive = self.builder.call(self.ecs_is_alive_fn, [eid_val], name="rm_alive")
                ok_bb = self.current_fn.append_basic_block(name="rm_alive_ok")
                bad_bb = self.current_fn.append_basic_block(name="rm_alive_bad")
                self.builder.cbranch(alive, ok_bb, bad_bb)
                
                self.builder.position_at_end(bad_bb)
                msg = self._make_global_string(
                    f"rm_dead_msg_{comp_name}_{line}",
                    f"단아 ECS: remove에 죽은 엔티티 (component {comp_name})\n\0"
                )
                msg_ptr = self.builder.gep(msg, [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True)
                self.builder.call(self.printf, [msg_ptr])
                self.builder.call(self.exit_fn, [ir.Constant(i32, 1)])
                self.builder.unreachable()
                
                self.builder.position_at_end(ok_bb)
                e_idx = self.builder.extract_value(eid_val, 0)
                return self.builder.call(
                    self.components[comp_name]['remove_fn'], [e_idx], name="rm_result"
                )
            
            # ----- 7.12d3: get(e, ComponentType) -----
            # SoA에서 필드들을 긁어 struct 값으로 조립해 반환.
            # 엔티티가 죽었거나 컴포넌트가 없으면 abort.
            if name == 'get' and name not in self.functions:
                if len(args) != 2:
                    raise DanhaValueError(
                        f"get은 2개의 인자가 필요한데 {len(args)}개가 들어왔어",
                        line=line, source=self._source_code
                    )
                type_arg = args[1]
                if type_arg[0] != 'Name':
                    raise DanhaTypeError(
                        f"get의 두 번째 인자는 component 타입이 필요해",
                        line=line, source=self._source_code
                    )
                comp_name = type_arg[1]
                if comp_name in self._from_imports:
                    comp_name = self._from_imports[comp_name]
                if comp_name not in self.components:
                    if comp_name in self.structs and comp_name not in self.components:
                        raise DanhaRuntimeError(
                            f"get: '{comp_name}'은(는) struct야",
                            line=line, source=self._source_code
                        )
                    raise DanhaNameError(
                        f"get: 정의되지 않은 component '{comp_name}'",
                        line=line, source=self._source_code
                    )
                
                eid_val = self.compile_expr(args[0])
                if eid_val.type != self.entity_id_type:
                    raise DanhaValueError(
                        f"get의 첫 인자는 EntityId여야 해",
                        line=line, source=self._source_code
                    )
                
                comp_info = self.components[comp_name]
                
                # 살아있는지 검사
                alive = self.builder.call(self.ecs_is_alive_fn, [eid_val], name="get_alive")
                ok_bb = self.current_fn.append_basic_block(name="get_alive_ok")
                bad_alive_bb = self.current_fn.append_basic_block(name="get_dead")
                self.builder.cbranch(alive, ok_bb, bad_alive_bb)
                
                self.builder.position_at_end(bad_alive_bb)
                dmsg = self._make_global_string(
                    f"get_dead_msg_{comp_name}_{line}",
                    f"단아 ECS: get에 죽은 엔티티 (component {comp_name})\n\0"
                )
                dmsg_ptr = self.builder.gep(dmsg, [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True)
                self.builder.call(self.printf, [dmsg_ptr])
                self.builder.call(self.exit_fn, [ir.Constant(i32, 1)])
                self.builder.unreachable()
                
                self.builder.position_at_end(ok_bb)
                # dense = sparse[idx]
                e_idx = self.builder.extract_value(eid_val, 0)
                sparse_ptr = self.builder.load(comp_info['sparse'])
                dense = self.builder.load(
                    self.builder.gep(sparse_ptr, [e_idx], inbounds=True),
                    name="get_dense"
                )
                # 컴포넌트가 붙어 있나?
                not_found = self.builder.icmp_signed('==', dense, ir.Constant(i32, -1))
                absent_bb = self.current_fn.append_basic_block(name="get_absent")
                present_bb = self.current_fn.append_basic_block(name="get_present")
                self.builder.cbranch(not_found, absent_bb, present_bb)
                
                self.builder.position_at_end(absent_bb)
                amsg = self._make_global_string(
                    f"get_absent_msg_{comp_name}_{line}",
                    f"단아 ECS: get에 없는 컴포넌트 {comp_name}\n\0"
                )
                amsg_ptr = self.builder.gep(amsg, [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True)
                self.builder.call(self.printf, [amsg_ptr])
                self.builder.call(self.exit_fn, [ir.Constant(i32, 1)])
                self.builder.unreachable()
                
                self.builder.position_at_end(present_bb)
                # SoA에서 필드들 긁어 struct 값 조립.
                # 7.15f: insertvalue로 조립하면 undef 구조체 위에 필드를 심는 구조라
                # O2가 패딩 영역을 오독할 수 있음(i8+i16+i64 같은 혼합 타입에서 관찰).
                # alloca 후 필드별 store → 단일 load 로 우회하면 모든 바이트가 명시적으로
                # 쓰이고 최적화기도 안전하게 봄.
                value_ty = comp_info['value_type']
                tmp_slot = self.builder.alloca(value_ty, name=f"get_{comp_name}_tmp")
                for i, fname in enumerate(comp_info['fields']):
                    farr = self.builder.load(comp_info['field_globals'][fname])
                    fslot = self.builder.gep(farr, [dense], inbounds=True)
                    fval = self.builder.load(fslot, name=f"get_{fname}")
                    dst = self.builder.gep(
                        tmp_slot,
                        [ir.Constant(i32, 0), ir.Constant(i32, i)],
                        inbounds=True, name=f"get_{fname}_dst"
                    )
                    self.builder.store(fval, dst)
                return self.builder.load(tmp_slot, name=f"get_{comp_name}_val")
            
            # ----- 7.4.1: 내장 함수 len() -----
            # 고정 배열: 컴파일 시점 상수.
            # 동적 배열: 구조체의 len 필드를 런타임에 읽기.
            if name == 'ord':
                if len(args) != 1:
                    raise DanhaValueError(
                        "ord()는 인자가 정확히 1개여야 해",
                        line=line, source=self._source_code
                    )
                val = self.compile_expr(args[0])
                if val.type != i8p:
                    raise DanhaTypeError(
                        "ord()의 인자는 str이어야 해",
                        line=line, source=self._source_code
                    )
                ch = self.builder.load(val, name="ord_ch")
                return self.builder.zext(ch, i32, name="ord_i32")

            if name == 'len':
                if len(args) != 1:
                    raise DanhaValueError(
                        f"len()은 인자가 정확히 1개여야 해",
                        line=line, source=self._source_code
                    )
                arg = args[0]
                
                # 8.2: 문자열 len — 인자를 먼저 평가해서 i8*이면 strlen
                # (Name 아닌 식도 지원: len("hello"), len(s + t) 등)
                val = self.compile_expr(arg)
                if val.type == i8p:
                    result_i64 = self.builder.call(self.strlen_fn, [val], name="str_len")
                    return self.builder.trunc(result_i64, i32, name="str_len_i32")
                
                # 배열 len — 변수 이름이 필요
                if arg[0] != 'Name':
                    raise DanhaValueError(
                        f"len()의 인자는 배열 변수나 문자열이어야 해",
                        line=line, source=self._source_code
                    )
                var_name_arg = arg[1]
                slot = self._lookup_var(var_name_arg)
                if slot is None:
                    raise DanhaNameError(
                        f"정의되지 않은 이름이야: {var_name_arg}",
                        line=line, source=self._source_code
                    )
                
                # 동적 배열이면 구조체의 len 필드 읽기
                if self._is_dynarray_slot(slot):
                    len_ptr = self._dynarray_get_field(slot, 1, f"{var_name_arg}_len")
                    return self.builder.load(len_ptr, name="dyn_len")
                
                # 고정 배열이면 컴파일 시점 상수
                try:
                    _, _, arr_len = self._lvalue_array(var_name_arg, line)
                except Exception:
                    raise DanhaRuntimeError(
                        f"'{var_name_arg}'은(는) 배열이 아니거나 정의되지 않았어",
                        line=line, source=self._source_code
                    )
                return ir.Constant(i32, arr_len)
            
            # ----- 7.4: 내장 함수 push() -----
            # push(arr, value) — 동적 배열에 원소 추가.
            # 비유: 고무줄 가방에 물건 하나 넣기. 꽉 차면 더 큰 가방으로 교체.
            if name == 'push':
                if len(args) != 2:
                    raise DanhaValueError(
                        f"push()는 인자가 정확히 2개여야 해 (배열, 값)",
                        line=line, source=self._source_code
                    )
                arr_arg = args[0]
                if arr_arg[0] != 'Name':
                    raise DanhaValueError(
                        f"push()의 첫 인자는 배열 변수여야 해",
                        line=line, source=self._source_code
                    )
                var_name_arg = arr_arg[1]
                slot = self._lookup_var(var_name_arg)
                if slot is None:
                    raise DanhaNameError(
                        f"정의되지 않은 이름이야: {var_name_arg}",
                        line=line, source=self._source_code
                    )
                if not self._is_dynarray_slot(slot):
                    raise DanhaRuntimeError(
                        f"push()는 동적 배열에만 쓸 수 있어 "
                        f"('{var_name_arg}'은(는) 고정 배열)",
                        line=line, source=self._source_code
                    )
                
                value = self.compile_expr(args[1])
                elem_ty = self._dynarray_elem_ty(slot)
                
                # 타입 검사 + 승격
                if value.type != elem_ty:
                    if value.type == i32 and elem_ty == f64:
                        value = self.builder.sitofp(value, f64)
                    else:
                        raise DanhaTypeError(
                            f"push 값 타입이 배열 원소 타입과 안 맞아",
                            line=line, source=self._source_code
                        )
                
                self._compile_dynarray_push(var_name_arg, slot, value, line)
                return ir.Constant(i32, 0)  # push는 값을 안 돌려줌

            # 자유 함수 pop(arr) — 마지막 원소를 떼서 돌려줌. 비어 있으면 zero값.
            if name == 'pop':
                if len(args) != 1:
                    raise DanhaValueError(
                        "pop()은 인자가 정확히 1개여야 해 (배열)",
                        line=line, source=self._source_code
                    )
                arr_arg = args[0]
                if arr_arg[0] != 'Name':
                    raise DanhaValueError(
                        "pop()의 첫 인자는 배열 변수여야 해",
                        line=line, source=self._source_code
                    )
                var_name_arg = arr_arg[1]
                slot = self._lookup_var(var_name_arg)
                if slot is None:
                    raise DanhaNameError(
                        f"정의되지 않은 이름이야: {var_name_arg}",
                        line=line, source=self._source_code
                    )
                if not self._is_dynarray_slot(slot):
                    raise DanhaRuntimeError(
                        f"pop()은 동적 배열에만 쓸 수 있어 "
                        f"('{var_name_arg}'은(는) 고정 배열)",
                        line=line, source=self._source_code
                    )
                elem_ty = self._dynarray_elem_ty(slot)
                len_ptr = self._dynarray_get_field(slot, 1, f"{var_name_arg}_len_ptr")
                data_ptr = self._dynarray_get_field(slot, 0, f"{var_name_arg}_data_ptr")
                cur_len = self.builder.load(len_ptr, name="pop_len")
                has_elem = self.builder.icmp_signed('>', cur_len, ir.Constant(i32, 0), name="pop_has")
                entry_b = self.builder.block
                do_b = self.current_fn.append_basic_block(name="pop_do")
                done_b = self.current_fn.append_basic_block(name="pop_done")
                self.builder.cbranch(has_elem, do_b, done_b)
                self.builder.position_at_end(do_b)
                new_len = self.builder.sub(cur_len, ir.Constant(i32, 1), name="pop_newlen")
                self.builder.store(new_len, len_ptr)
                data = self.builder.load(data_ptr, name="pop_data")
                elem_p = self.builder.gep(data, [new_len], inbounds=True, name="pop_elem")
                pop_val = self.builder.load(elem_p, name="pop_val")
                self.builder.branch(done_b)
                self.builder.position_at_end(done_b)
                if isinstance(elem_ty, ir.PointerType):
                    zero_v = ir.Constant(elem_ty, None)
                else:
                    zero_v = ir.Constant(elem_ty, 0)
                phi = self.builder.phi(elem_ty, name="pop_ret")
                phi.add_incoming(zero_v, entry_b)
                phi.add_incoming(pop_val, do_b)
                return phi

            # 자유 함수 exit(code) — 프로세스 즉시 종료 (libc exit)
            if name == 'exit' and name not in self.functions:
                if len(args) != 1:
                    raise DanhaValueError(
                        "exit(code)는 인자 1개가 필요해",
                        line=line, source=self._source_code
                    )
                code_v = self.compile_expr(args[0])
                if isinstance(code_v.type, ir.IntType):
                    if code_v.type.width > 32:
                        code_v = self.builder.trunc(code_v, i32, name="exit_code")
                    elif code_v.type.width < 32:
                        code_v = self.builder.sext(code_v, i32, name="exit_code")
                else:
                    raise DanhaTypeError(
                        "exit()의 인자는 정수여야 해",
                        line=line, source=self._source_code
                    )
                exit_fn = self._ensure_function('exit', ir.VoidType(), [i32])
                self.builder.call(exit_fn, [code_v])
                return ir.Constant(i32, 0)

            # ----- 7.5: arena_reset() 내장 함수 -----
            # 아레나의 offset을 0으로 리셋 — 모든 동적 배열 메모리를 한 번에 '비움'.
            # 비유: 쟁반 위의 접시를 하나하나 치우는 게 아니라 쟁반째 비우기.
            # 게임에서는 매 프레임 끝에 호출하면 그 프레임의 임시 데이터가 전부 정리됨.
            # 주의: reset 후에는 이전에 만든 동적 배열의 data가 무효화됨!
            if name == 'arena_reset':
                if len(args) != 0:
                    raise DanhaNameError(
                        f"arena_reset()은 인자가 없어야 해",
                        line=line, source=self._source_code
                    )
                off_ptr = self.builder.gep(
                    self.arena_slot,
                    [ir.Constant(i32, 0), ir.Constant(i32, 1)],
                    inbounds=True, name="arena_off_reset"
                )
                self.builder.store(ir.Constant(i32, 0), off_ptr)
                return ir.Constant(i32, 0)
            
            # ----- 8.2: to_string -----
            if name == 'to_string' and name not in self.functions:
                return self._compile_to_string(args, line)

            # ----- Stage 70: StringBuilder — mutable buffer로 O(N) 누적 문자열 빌드 -----
            # Layout: { i8* data, i64 len, i64 cap }. 24 bytes. opaque i8* 로 사용자에게 노출.
            if name == 'string_builder' and name not in self.functions:
                if len(args) != 0:
                    raise DanhaValueError(
                        "string_builder()는 인자가 없어 — `sb = string_builder()`로 호출",
                        line=line, source=self._source_code
                    )
                return self._compile_sb_new()

            if name == 'string_builder_append' and name not in self.functions:
                if len(args) != 2:
                    raise DanhaValueError(
                        "string_builder_append(sb, str)은 인자 2개가 필요해",
                        line=line, source=self._source_code
                    )
                sb_val = self.compile_expr(args[0])
                s_val = self.compile_expr(args[1])
                if sb_val.type != i8p:
                    raise DanhaTypeError("string_builder_append의 첫 인자는 string_builder()여야 해", line=line, source=self._source_code)
                if s_val.type != i8p:
                    raise DanhaTypeError("string_builder_append의 두번째 인자는 str이어야 해", line=line, source=self._source_code)
                self._compile_sb_append(sb_val, s_val)
                return ir.Constant(i32, 0)

            if name == 'string_builder_to_string' and name not in self.functions:
                if len(args) != 1:
                    raise DanhaValueError(
                        "string_builder_to_string(sb)은 인자 1개가 필요해",
                        line=line, source=self._source_code
                    )
                sb_val = self.compile_expr(args[0])
                if sb_val.type != i8p:
                    raise DanhaTypeError("string_builder_to_string의 인자는 string_builder()여야 해", line=line, source=self._source_code)
                return self._compile_sb_to_string(sb_val)

            if name == 'string_builder_len' and name not in self.functions:
                if len(args) != 1:
                    raise DanhaValueError(
                        "string_builder_len(sb)은 인자 1개가 필요해",
                        line=line, source=self._source_code
                    )
                sb_val = self.compile_expr(args[0])
                if sb_val.type != i8p:
                    raise DanhaTypeError("string_builder_len의 인자는 string_builder()여야 해", line=line, source=self._source_code)
                return self._compile_sb_len(sb_val)

            # parse_int(str) → i32 — libc atoi 호출.
            if name == 'parse_int' and name not in self.functions:
                if len(args) != 1:
                    raise DanhaValueError(
                        "parse_int(str)은 1개의 인자가 필요해",
                        line=line, source=self._source_code
                    )
                val = self.compile_expr(args[0])
                if val.type != i8p:
                    raise DanhaTypeError(
                        "parse_int의 인자는 str이어야 해",
                        line=line, source=self._source_code
                    )
                return self.builder.call(self.atoi_fn, [val], name="parse_int_r")

            # ----- 59단계: os_exec / get_args (셀프 호스팅) -----
            if name == 'file_read' and name not in self.functions:
                if len(args) != 1:
                    raise DanhaValueError("file_read(path)는 인자 1개가 필요해", line=line, source=self._source_code)
                path_val = self.compile_expr(args[0])
                if path_val.type != i8p:
                    raise DanhaTypeError("file_read의 인자는 str이어야 해", line=line, source=self._source_code)
                self._define_core_file_runtime()
                fn = self._ensure_runtime_fn("dnh_file_read", i8p, [i8p])
                return self.builder.call(fn, [path_val], name="file_read_ret")

            if name == 'file_write' and name not in self.functions:
                if len(args) != 2:
                    raise DanhaValueError("file_write(path, text)는 인자 2개가 필요해", line=line, source=self._source_code)
                path_val = self.compile_expr(args[0])
                text_val = self.compile_expr(args[1])
                if path_val.type != i8p or text_val.type != i8p:
                    raise DanhaTypeError("file_write의 인자는 str, str이어야 해", line=line, source=self._source_code)
                self._define_core_file_runtime()
                fn = self._ensure_runtime_fn("dnh_file_write", ir.VoidType(), [i8p, i8p])
                self.builder.call(fn, [path_val, text_val])
                return ir.Constant(i32, 0)

            if name == 'file_append' and name not in self.functions:
                if len(args) != 2:
                    raise DanhaValueError("file_append(path, text)는 인자 2개가 필요해", line=line, source=self._source_code)
                path_val = self.compile_expr(args[0])
                text_val = self.compile_expr(args[1])
                if path_val.type != i8p or text_val.type != i8p:
                    raise DanhaTypeError("file_append의 인자는 str, str이어야 해", line=line, source=self._source_code)
                self._define_core_file_runtime()
                fn = self._ensure_runtime_fn("dnh_file_append", ir.VoidType(), [i8p, i8p])
                self.builder.call(fn, [path_val, text_val])
                return ir.Constant(i32, 0)

            if name == 'file_exists' and name not in self.functions:
                if len(args) != 1:
                    raise DanhaValueError("file_exists(path)는 인자 1개가 필요해", line=line, source=self._source_code)
                path_val = self.compile_expr(args[0])
                if path_val.type != i8p:
                    raise DanhaTypeError("file_exists의 인자는 str이어야 해", line=line, source=self._source_code)
                self._define_core_file_runtime()
                fn = self._ensure_runtime_fn("dnh_file_exists", i1, [i8p])
                return self.builder.call(fn, [path_val], name="file_exists_ret")

            if name == 'os_exec' and name not in self.functions:
                if len(args) != 1:
                    raise DanhaValueError("os_exec(cmd)는 인자 1개가 필요해", line=line, source=self._source_code)
                cmd_val = self.compile_expr(args[0])
                if cmd_val.type != i8p:
                    raise DanhaTypeError("os_exec의 인자는 str이어야 해", line=line, source=self._source_code)
                if self.runtime_mode == 'direct-os':
                    fn = self._define_direct_os_exec_runtime()
                    return self.builder.call(fn, [cmd_val], name='os_exec_ret')
                # system() 외부 선언이 없으면 추가
                if 'system' not in [f.name for f in self.module.functions]:
                    sys_ty = ir.FunctionType(i32, [i8p])
                    self._system_fn = ir.Function(self.module, sys_ty, name='system')
                else:
                    self._system_fn = self.module.get_global('system')
                return self.builder.call(self._system_fn, [cmd_val], name='os_exec_ret')

            if name == 'get_args' and name not in self.functions:
                dyn_ty = self._get_dynarray_type(i8p)
                if self.runtime_mode == 'direct-os':
                    fn = self._define_direct_os_get_args_runtime(dyn_ty)
                    return self.builder.call(fn, [], name="get_args_val")
                tmp = self.builder.alloca(dyn_ty, name="get_args_tmp")
                if self.entry_arg_mode and self.argc_global is not None and self.argv_global is not None:
                    argc_val = self.builder.load(self.argc_global, name="argc_val")
                    argv_val = self.builder.load(self.argv_global, name="argv_val")
                    one = ir.Constant(i32, 1)
                    argc_minus_prog = self.builder.sub(argc_val, one, name="argc_user")
                    is_negative = self.builder.icmp_signed("<", argc_minus_prog, ir.Constant(i32, 0), name="argc_user_neg")
                    user_len = self.builder.select(is_negative, ir.Constant(i32, 0), argc_minus_prog, name="argc_user_len")
                    user_argv = self.builder.gep(argv_val, [one], inbounds=True, name="argv_user")
                    self.builder.store(user_argv, self._dynarray_get_field(tmp, 0, "ga_data"))
                    self.builder.store(user_len, self._dynarray_get_field(tmp, 1, "ga_len"))
                    self.builder.store(user_len, self._dynarray_get_field(tmp, 2, "ga_cap"))
                else:
                    null_ptr = ir.Constant(i8p.as_pointer(), None)
                    self.builder.store(null_ptr, self._dynarray_get_field(tmp, 0, "ga_data"))
                    self.builder.store(ir.Constant(i32, 0), self._dynarray_get_field(tmp, 1, "ga_len"))
                    self.builder.store(ir.Constant(i32, 0), self._dynarray_get_field(tmp, 2, "ga_cap"))
                return self.builder.load(tmp, name="get_args_val")

            # ----- 7.8a: 벡터 수학 내장 함수 -----
            
            # length(v) → scalar: √(x² + y² + ...). vec*f는 f32, vec*는 f64.
            if name == 'length':
                if len(args) != 1:
                    raise DanhaValueError("length()는 1개의 인자가 필요해", line=line, source=self._source_code)
                val = self.compile_expr(args[0])
                vec_info = self._is_vec_type(val.type)
                if vec_info is None:
                    raise DanhaRuntimeError("length()는 벡터에만 쓸 수 있어", line=line, source=self._source_code)
                vec_name, size = vec_info
                elem_ty = self._VEC_ELEM_TY[vec_name]
                if isinstance(val.type, ir.VectorType):
                    sq = self.builder.fmul(val, val, name="vsq")
                    sum_sq = self._vector_reduce_fadd(sq, name="vsum")
                    sqrt_fn = self.sqrtf_fn if elem_ty == f32 else self.sqrt_fn
                    return self.builder.call(sqrt_fn, [sum_sq], name="length")
                sum_sq = ir.Constant(elem_ty, 0.0)
                for idx in range(size):
                    comp = self._vec_extract(val, idx, name=f"c{idx}")
                    sq = self.builder.fmul(comp, comp, name=f"sq{idx}")
                    sum_sq = self.builder.fadd(sum_sq, sq, name=f"sum{idx}")
                sqrt_fn = self.sqrtf_fn if elem_ty == f32 else self.sqrt_fn
                return self.builder.call(sqrt_fn, [sum_sq], name="length")

            # dot(a, b) → scalar
            if name == 'dot':
                if len(args) != 2:
                    raise DanhaValueError("dot()는 2개의 인자가 필요해", line=line, source=self._source_code)
                a = self.compile_expr(args[0])
                b = self.compile_expr(args[1])
                va = self._is_vec_type(a.type)
                vb = self._is_vec_type(b.type)
                if va is None or vb is None:
                    raise DanhaRuntimeError("dot()는 벡터에만 쓸 수 있어", line=line, source=self._source_code)
                if va[0] != vb[0]:
                    raise DanhaTypeError("dot()의 두 벡터는 같은 타입이어야 해", line=line, source=self._source_code)
                vec_name, size = va
                elem_ty = self._VEC_ELEM_TY[vec_name]
                if isinstance(a.type, ir.VectorType):
                    prod = self.builder.fmul(a, b, name="dprod")
                    return self._vector_reduce_fadd(prod, name="dot")
                result = ir.Constant(elem_ty, 0.0)
                for idx in range(size):
                    ac = self._vec_extract(a, idx, name=f"a{idx}")
                    bc = self._vec_extract(b, idx, name=f"b{idx}")
                    prod = self.builder.fmul(ac, bc, name=f"p{idx}")
                    result = self.builder.fadd(result, prod, name=f"dot{idx}")
                return result

            # normalize(v) → vecN: v / length(v)
            if name == 'normalize':
                if len(args) != 1:
                    raise DanhaValueError("normalize()는 1개의 인자가 필요해", line=line, source=self._source_code)
                val = self.compile_expr(args[0])
                vec_info = self._is_vec_type(val.type)
                if vec_info is None:
                    raise DanhaRuntimeError("normalize()는 벡터에만 쓸 수 있어", line=line, source=self._source_code)
                vec_name, size = vec_info
                vty = self._VEC_INFO[vec_name][0]
                elem_ty = self._VEC_ELEM_TY[vec_name]
                sqrt_fn = self.sqrtf_fn if elem_ty == f32 else self.sqrt_fn
                if isinstance(vty, ir.VectorType):
                    sq = self.builder.fmul(val, val, name="nvsq")
                    sum_sq = self._vector_reduce_fadd(sq, name="nvsum")
                    mag = self.builder.call(sqrt_fn, [sum_sq], name="nmag")
                    mag_splat = self._splat(mag, vty)
                    return self.builder.fdiv(val, mag_splat, name="norm")
                sum_sq = ir.Constant(elem_ty, 0.0)
                for idx in range(size):
                    comp = self._vec_extract(val, idx, name=f"c{idx}")
                    sq = self.builder.fmul(comp, comp, name=f"sq{idx}")
                    sum_sq = self.builder.fadd(sum_sq, sq, name=f"sum{idx}")
                mag = self.builder.call(sqrt_fn, [sum_sq], name="mag")
                result = ir.Constant(vty, ir.Undefined)
                for idx in range(size):
                    comp = self._vec_extract(val, idx, name=f"c{idx}")
                    normed = self.builder.fdiv(comp, mag, name=f"n{idx}")
                    result = self._vec_insert(result, normed, idx, name=f"v{idx}")
                return result
            
            # cross(a, b) → vec3: vec3 전용 외적
            if name == 'cross':
                if len(args) != 2:
                    raise DanhaValueError("cross()는 2개의 인자가 필요해", line=line, source=self._source_code)
                a = self.compile_expr(args[0])
                b = self.compile_expr(args[1])
                va = self._is_vec_type(a.type)
                vb = self._is_vec_type(b.type)
                if va is None or vb is None:
                    raise DanhaRuntimeError("cross()는 벡터에만 쓸 수 있어", line=line, source=self._source_code)
                if va[0] != 'vec3' or vb[0] != 'vec3':
                    raise DanhaRuntimeError("cross()는 vec3에만 쓸 수 있어", line=line, source=self._source_code)
                vty = self._VEC_INFO['vec3'][0]
                # SIMD 빠른 경로: shufflevector로 a.yzx/a.zxy 만들고 한 번에 fmul+fsub
                if isinstance(vty, ir.VectorType):
                    mask_yzx = ir.Constant(ir.VectorType(i32, 4), [1, 2, 0, 3])
                    mask_zxy = ir.Constant(ir.VectorType(i32, 4), [2, 0, 1, 3])
                    a_yzx = self.builder.shuffle_vector(a, a, mask_yzx, name="a_yzx")
                    b_zxy = self.builder.shuffle_vector(b, b, mask_zxy, name="b_zxy")
                    a_zxy = self.builder.shuffle_vector(a, a, mask_zxy, name="a_zxy")
                    b_yzx = self.builder.shuffle_vector(b, b, mask_yzx, name="b_yzx")
                    lhs = self.builder.fmul(a_yzx, b_zxy, name="cross_lhs")
                    rhs = self.builder.fmul(a_zxy, b_yzx, name="cross_rhs")
                    result = self.builder.fsub(lhs, rhs, name="cross")
                    # lane 3 = 0.0 (패딩)
                    return self.builder.insert_element(result, ir.Constant(f64, 0.0), ir.Constant(i32, 3), name="cross_pad")
                # struct 폴백 (Phase 1 이전 / 미사용)
                ax = self._vec_extract(a, 0, name="ax")
                ay = self._vec_extract(a, 1, name="ay")
                az = self._vec_extract(a, 2, name="az")
                bx = self._vec_extract(b, 0, name="bx")
                by = self._vec_extract(b, 1, name="by")
                bz = self._vec_extract(b, 2, name="bz")
                rx = self.builder.fsub(
                    self.builder.fmul(ay, bz, name="aybz"),
                    self.builder.fmul(az, by, name="azby"), name="cx"
                )
                ry = self.builder.fsub(
                    self.builder.fmul(az, bx, name="azbx"),
                    self.builder.fmul(ax, bz, name="axbz"), name="cy"
                )
                rz = self.builder.fsub(
                    self.builder.fmul(ax, by, name="axby"),
                    self.builder.fmul(ay, bx, name="aybx"), name="cz"
                )
                result = ir.Constant(vty, ir.Undefined)
                result = self._vec_insert(result, rx, 0, name="cr0")
                result = self._vec_insert(result, ry, 1, name="cr1")
                result = self._vec_insert(result, rz, 2, name="cr2")
                return result
            
            # ----- 7.9a: 행렬 내장 함수 -----
            
            # mat4_identity() → mat4 (column-vector SIMD)
            if name == 'mat4_identity':
                if len(args) != 0:
                    raise DanhaNameError("mat4_identity()는 인자가 없어야 해", line=line, source=self._source_code)
                return self._mat4_from_flat([
                    1.0, 0.0, 0.0, 0.0,
                    0.0, 1.0, 0.0, 0.0,
                    0.0, 0.0, 1.0, 0.0,
                    0.0, 0.0, 0.0, 1.0,
                ])

            # mat4_translate(x, y, z) → mat4
            if name == 'mat4_translate':
                if len(args) != 3:
                    raise DanhaValueError(
                        f"mat4_translate()는 3개의 인자가 필요해 (x, y, z)",
                        line=line, source=self._source_code
                    )
                tx = self.compile_expr(args[0])
                ty = self.compile_expr(args[1])
                tz = self.compile_expr(args[2])
                if tx.type == i32: tx = self.builder.sitofp(tx, f64)
                if ty.type == i32: ty = self.builder.sitofp(ty, f64)
                if tz.type == i32: tz = self.builder.sitofp(tz, f64)
                # 열 우선: col0=[1,0,0,0], col1=[0,1,0,0], col2=[0,0,1,0], col3=[tx,ty,tz,1]
                return self._mat4_from_flat([
                    1.0, 0.0, 0.0, 0.0,
                    0.0, 1.0, 0.0, 0.0,
                    0.0, 0.0, 1.0, 0.0,
                    tx,  ty,  tz,  1.0,
                ])

            # mat4_scale(sx, sy, sz) → mat4
            if name == 'mat4_scale':
                if len(args) != 3:
                    raise DanhaValueError(
                        f"mat4_scale()는 3개의 인자가 필요해 (sx, sy, sz)",
                        line=line, source=self._source_code
                    )
                sx = self.compile_expr(args[0])
                sy = self.compile_expr(args[1])
                sz = self.compile_expr(args[2])
                if sx.type == i32: sx = self.builder.sitofp(sx, f64)
                if sy.type == i32: sy = self.builder.sitofp(sy, f64)
                if sz.type == i32: sz = self.builder.sitofp(sz, f64)
                return self._mat4_from_flat([
                    sx,  0.0, 0.0, 0.0,
                    0.0, sy,  0.0, 0.0,
                    0.0, 0.0, sz,  0.0,
                    0.0, 0.0, 0.0, 1.0,
                ])

            # mat4_rotate_x/y/z(angle) → mat4
            if name in ('mat4_rotate_x', 'mat4_rotate_y', 'mat4_rotate_z'):
                if len(args) != 1:
                    raise DanhaValueError(
                        f"{name}()는 1개의 인자가 필요해 (라디안 각도)",
                        line=line, source=self._source_code
                    )
                angle = self.compile_expr(args[0])
                if angle.type == i32:
                    angle = self.builder.sitofp(angle, f64)
                c = self.builder.call(self.cos_fn, [angle], name="cos")
                s = self.builder.call(self.sin_fn, [angle], name="sin")
                neg_s = self.builder.fneg(s, name="neg_s")
                if name == 'mat4_rotate_x':
                    return self._mat4_from_flat([
                        1.0, 0.0, 0.0,   0.0,
                        0.0, c,   s,     0.0,
                        0.0, neg_s, c,   0.0,
                        0.0, 0.0, 0.0,   1.0,
                    ])
                elif name == 'mat4_rotate_y':
                    return self._mat4_from_flat([
                        c,     0.0, neg_s, 0.0,
                        0.0,   1.0, 0.0,   0.0,
                        s,     0.0, c,     0.0,
                        0.0,   0.0, 0.0,   1.0,
                    ])
                else:  # rotate_z
                    return self._mat4_from_flat([
                        c,     s,   0.0, 0.0,
                        neg_s, c,   0.0, 0.0,
                        0.0,   0.0, 1.0, 0.0,
                        0.0,   0.0, 0.0, 1.0,
                    ])
            
            # ----- 7.9c: transpose, inverse -----
            
            # mat4_transpose(m) → mat4 (column → row)
            if name == 'mat4_transpose':
                if len(args) != 1:
                    raise DanhaValueError("mat4_transpose()는 1개의 인자가 필요해", line=line, source=self._source_code)
                val = self.compile_expr(args[0])
                if not self._is_mat_type(val.type):
                    raise DanhaRuntimeError("mat4_transpose()는 mat4에만 쓸 수 있어", line=line, source=self._source_code)
                # transpose: new column[r] = (old[0][r], old[1][r], old[2][r], old[3][r])
                # 즉 row r를 모아 column으로
                result = ir.Constant(self._mat4_ty, ir.Undefined)
                for r in range(4):
                    new_col = ir.Constant(self._mat4_col_ty, ir.Undefined)
                    for c in range(4):
                        elem = self._mat4_elem(val, c, r, name=f"t{c}{r}")
                        new_col = self.builder.insert_element(new_col, elem, ir.Constant(i32, c), name=f"tc{r}{c}")
                    result = self.builder.insert_value(result, new_col, r, name=f"trc{r}")
                return result
            
            # mat4_inverse(m) → mat4
            # 여수인자(cofactor) 전개 방식. 인터프리터와 동일 알고리즘.
            if name == 'mat4_inverse':
                if len(args) != 1:
                    raise DanhaValueError("mat4_inverse()는 1개의 인자가 필요해", line=line, source=self._source_code)
                val = self.compile_expr(args[0])
                if not self._is_mat_type(val.type):
                    raise DanhaRuntimeError("mat4_inverse()는 mat4에만 쓸 수 있어", line=line, source=self._source_code)
                
                # 헬퍼: m[row][col] — 컬럼 우선 layout이라 col을 먼저 꺼내고 row 추출
                def gv(r, c):
                    return self._mat4_elem(val, c, r, name=f"g{r}")
                
                def fmul(a, b, nm=""):
                    return self.builder.fmul(a, b, name=nm)
                def fsub(a, b, nm=""):
                    return self.builder.fsub(a, b, name=nm)
                def fadd(a, b, nm=""):
                    return self.builder.fadd(a, b, name=nm)
                
                # 2x2 소행렬식
                s0 = fsub(fmul(gv(0,0), gv(1,1)), fmul(gv(1,0), gv(0,1)))
                s1 = fsub(fmul(gv(0,0), gv(1,2)), fmul(gv(1,0), gv(0,2)))
                s2 = fsub(fmul(gv(0,0), gv(1,3)), fmul(gv(1,0), gv(0,3)))
                s3 = fsub(fmul(gv(0,1), gv(1,2)), fmul(gv(1,1), gv(0,2)))
                s4 = fsub(fmul(gv(0,1), gv(1,3)), fmul(gv(1,1), gv(0,3)))
                s5 = fsub(fmul(gv(0,2), gv(1,3)), fmul(gv(1,2), gv(0,3)))
                
                k5 = fsub(fmul(gv(2,2), gv(3,3)), fmul(gv(3,2), gv(2,3)))
                k4 = fsub(fmul(gv(2,1), gv(3,3)), fmul(gv(3,1), gv(2,3)))
                k3 = fsub(fmul(gv(2,1), gv(3,2)), fmul(gv(3,1), gv(2,2)))
                k2 = fsub(fmul(gv(2,0), gv(3,3)), fmul(gv(3,0), gv(2,3)))
                k1 = fsub(fmul(gv(2,0), gv(3,2)), fmul(gv(3,0), gv(2,2)))
                k0 = fsub(fmul(gv(2,0), gv(3,1)), fmul(gv(3,0), gv(2,1)))
                
                # det = s0*k5 - s1*k4 + s2*k3 + s3*k2 - s4*k1 + s5*k0
                det = fadd(
                    fsub(
                        fadd(fsub(fmul(s0, k5), fmul(s1, k4)), fmul(s2, k3)),
                        fmul(s4, k1)
                    ),
                    fadd(fmul(s3, k2), fmul(s5, k0))
                )
                inv_det = self.builder.fdiv(ir.Constant(f64, 1.0), det, name="inv_det")
                
                # 수반 행렬 원소 계산 (행 우선 순서로)
                # adj[row][col], 결과는 열 우선으로 변환
                adj = [[None]*4 for _ in range(4)]
                adj[0][0] = fsub(fadd(fmul(gv(1,1), k5), fmul(gv(1,3), k3)), fmul(gv(1,2), k4))
                adj[0][1] = fsub(fadd(fmul(gv(0,2), k4), ir.Constant(f64, 0.0)), fadd(fmul(gv(0,1), k5), fmul(gv(0,3), k3)))
                adj[0][2] = fsub(fadd(fmul(gv(3,1), s5), fmul(gv(3,3), s3)), fmul(gv(3,2), s4))
                adj[0][3] = fsub(fadd(fmul(gv(2,2), s4), ir.Constant(f64, 0.0)), fadd(fmul(gv(2,1), s5), fmul(gv(2,3), s3)))
                
                adj[1][0] = fsub(fadd(fmul(gv(1,2), k2), ir.Constant(f64, 0.0)), fadd(fmul(gv(1,0), k5), fmul(gv(1,3), k1)))
                adj[1][1] = fsub(fadd(fmul(gv(0,0), k5), fmul(gv(0,3), k1)), fmul(gv(0,2), k2))
                adj[1][2] = fsub(fadd(fmul(gv(3,2), s2), ir.Constant(f64, 0.0)), fadd(fmul(gv(3,0), s5), fmul(gv(3,3), s1)))
                adj[1][3] = fsub(fadd(fmul(gv(2,0), s5), fmul(gv(2,3), s1)), fmul(gv(2,2), s2))
                
                adj[2][0] = fsub(fadd(fmul(gv(1,0), k4), fmul(gv(1,3), k0)), fmul(gv(1,1), k2))
                adj[2][1] = fsub(fadd(fmul(gv(0,1), k2), ir.Constant(f64, 0.0)), fadd(fmul(gv(0,0), k4), fmul(gv(0,3), k0)))
                adj[2][2] = fsub(fadd(fmul(gv(3,0), s4), fmul(gv(3,3), s0)), fmul(gv(3,1), s2))
                adj[2][3] = fsub(fadd(fmul(gv(2,1), s2), ir.Constant(f64, 0.0)), fadd(fmul(gv(2,0), s4), fmul(gv(2,3), s0)))
                
                adj[3][0] = fsub(fadd(fmul(gv(1,1), k1), ir.Constant(f64, 0.0)), fadd(fmul(gv(1,0), k3), fmul(gv(1,2), k0)))
                adj[3][1] = fsub(fadd(fmul(gv(0,0), k3), fmul(gv(0,2), k0)), fmul(gv(0,1), k1))
                adj[3][2] = fsub(fadd(fmul(gv(3,1), s1), ir.Constant(f64, 0.0)), fadd(fmul(gv(3,0), s3), fmul(gv(3,2), s0)))
                adj[3][3] = fsub(fadd(fmul(gv(2,0), s3), fmul(gv(2,2), s0)), fmul(gv(2,1), s1))
                
                # adj * inv_det → 컬럼 벡터별 빌드
                result = ir.Constant(self._mat4_ty, ir.Undefined)
                for col in range(4):
                    new_col = ir.Constant(self._mat4_col_ty, ir.Undefined)
                    for row in range(4):
                        scaled = fmul(adj[row][col], inv_det)
                        new_col = self.builder.insert_element(new_col, scaled, ir.Constant(i32, row), name=f"ic{col}{row}")
                    result = self.builder.insert_value(result, new_col, col, name=f"inv_c{col}")
                return result
            
            
            # ----- 7.14c: schedule(인자들...) — 모든 system을 올바른 순서로 호출 -----
            if name == 'schedule' and name not in self.functions:
                if not hasattr(self, '_system_meta') or len(self._system_meta) == 0:
                    # system이 하나도 없으면 아무것도 안 함
                    return ir.Constant(i32, 0)
                
                # 토폴로지 정렬
                ordered = self._schedule_systems_topo(line)
                
                # 인자 평가
                arg_values = [self.compile_expr(a) for a in args]
                
                # 각 system을 순서대로 호출
                for sys_name in ordered:
                    if sys_name not in self.functions:
                        raise DanhaNameError(
                            f"schedule: system '{sys_name}'을(를) 찾을 수 없어",
                            line=line, source=self._source_code
                        )
                    sys_fn = self.functions[sys_name]
                    expected = len(sys_fn.args)
                    if len(args) != expected:
                        raise DanhaValueError(
                            f"schedule: system '{sys_name}'은(는) "
                            f"{expected}개의 인자가 필요한데 schedule에 {len(args)}개가 넘어왔어",
                            line=line, source=self._source_code
                        )
                    # 인자 타입 승격 (i32 → f64)
                    call_args = []
                    for i, (av, param) in enumerate(zip(arg_values, sys_fn.args)):
                        if av.type == param.type:
                            call_args.append(av)
                        elif av.type == i32 and param.type == f64:
                            call_args.append(
                                self.builder.sitofp(av, f64, name=f"sched_promo_{sys_name}_{i}")
                            )
                        else:
                            raise DanhaTypeError(
                                f"schedule: system '{sys_name}'의 "
                                f"{i+1}번째 인자 타입이 안 맞아",
                                line=line, source=self._source_code
                            )
                    self.builder.call(sys_fn, call_args)
                
                return ir.Constant(i32, 0)
            
            if name not in self.functions:
                # 8.5: 제네릭 함수인지 확인
                if hasattr(self, '_generic_fns') and name in self._generic_fns:
                    return self._instantiate_generic(name, args, line)
                
                # 15: 변수에 저장된 함수 포인터(람다) 호출
                # add = fn(a, b) { a + b }; add(1, 2)
                var_slot = self._lookup_var(name)
                if var_slot is not None:
                    fn_ptr = self.builder.load(var_slot, name=f"{name}_fptr")
                    if isinstance(fn_ptr.type, ir.PointerType) and isinstance(fn_ptr.type.pointee, ir.FunctionType):
                        fn_ty = fn_ptr.type.pointee
                        compiled_args = []
                        for i, arg_node in enumerate(args):
                            val = self.compile_expr(arg_node)
                            if i < len(fn_ty.args) and val.type != fn_ty.args[i]:
                                if val.type == i32 and fn_ty.args[i] == f64:
                                    val = self.builder.sitofp(val, f64)
                            compiled_args.append(val)
                        return self.builder.call(fn_ptr, compiled_args, name=f"{name}_ret")
                
                raise DanhaNameError(f"정의되지 않은 함수야: {name}", line=line, source=self._source_code)
            
            fn = self.functions[name]
            expected = len(fn.args)
            is_vararg = self.fn_is_vararg.get(name, False)
            if is_vararg:
                # 가변인자 함수: 고정 인자 개수 이상이어야 함
                if len(args) < expected:
                    raise DanhaValueError(
                        f"{name}은(는) 최소 {expected}개의 인자가 필요한데 {len(args)}개가 들어왔어",
                        line=line, source=self._source_code
                    )
            else:
                if len(args) != expected:
                    raise DanhaValueError(
                        f"{name}은(는) {expected}개의 인자가 필요한데 {len(args)}개가 들어왔어",
                        line=line, source=self._source_code
                    )
            
            # 7.1.3 파트 3 검사 B: 호출자의 참조 종류와 시그니처 매칭.
            # 값 자리에는 값만, '&'/'&mut' 자리에는 참조만 허용.
            # '&mut'은 '&' 자리에 강등해 넘길 수 있음 (더 큰 권한이 작은 권한을 품음).
            sig_refs = self.fn_param_ref_kinds.get(name, [None] * len(args))
            for i, (arg_node, sig_ref) in enumerate(zip(args, sig_refs)):
                caller_ref = None
                if arg_node[0] == 'AddrOf':
                    caller_ref = 'mut_ref' if arg_node[1] else 'ref'
                # ref forward: bare Name이 현재 함수의 ref 파라미터면 그 ref kind로 추론.
                # sig_ref가 'ref'이고 변수가 'mut_ref'면 강등 허용 (기존 규칙).
                elif arg_node[0] == 'Name' and sig_ref is not None:
                    nm = arg_node[1]
                    if hasattr(self, '_cur_fn_param_refs') and nm in self._cur_fn_param_refs:
                        caller_ref = self._cur_fn_param_refs[nm]
                self._check_arg_ref_match(name, i, caller_ref, sig_ref, arg_node[-1])
            
            arg_values = [self.compile_expr(a) for a in args]
            # 인자 타입이 함수 시그니처와 맞는지 검사.
            # 정수→실수 / i32→i64 자동 승격 (산술 승격과 같은 정신).
            # 가변인자 함수는 고정 인자 부분만 타입 검사, 나머지는 그대로 통과.
            i64ty = ir.IntType(64)
            for i, (av, param) in enumerate(zip(arg_values, fn.args)):
                if av.type == param.type:
                    continue
                if av.type == i32 and param.type == f64:
                    arg_values[i] = self.builder.sitofp(av, f64, name="argpromoted")
                    continue
                if av.type == i32 and param.type == i64ty:
                    arg_values[i] = self.builder.sext(av, i64ty, name="argpromoted_i64")
                    continue
                if av.type == i64ty and param.type == f64:
                    arg_values[i] = self.builder.sitofp(av, f64, name="argpromoted_f64")
                    continue
                # L-1: i1 (bool 식) → i8 (bool 파라미터) 자동 zext
                if av.type == i1 and param.type == i8:
                    arg_values[i] = self.builder.zext(av, i8, name="bool_to_i8")
                    continue
                # i8* ↔ ptr 는 같은 타입이므로 통과
                if isinstance(av.type, ir.PointerType) and isinstance(param.type, ir.PointerType):
                    continue
                raise DanhaTypeError(
                    f"{name}의 {i+1}번째 인자 타입이 안 맞아 "
                    f"(기대 {param.type}, 실제 {av.type})",
                    line=line, source=self._source_code
                )
            # 7.16b: void 반환 함수는 호출 결과에 이름을 붙일 수 없음.
            # LLVM에서 void 결과에 이름을 주면 에러남.
            if isinstance(fn.function_type.return_type, ir.VoidType):
                return self.builder.call(fn, arg_values)
            return self.builder.call(fn, arg_values, name="calltmp")
        
        # 메서드 호출: p.method(args...) → Player_method(addr_of_p, args...)
        if node_type == 'MethodCall':
            obj_node = node[1]
            method_name = node[2]
            args = node[3]
            line = node[-1]
            
            # 30: HashMap 정적 메서드 — HashMap.new()
            if obj_node[0] == 'Name' and obj_node[1] == 'HashMap':
                if method_name == 'new':
                    if len(args) == 1:
                        cap_val = self.compile_expr(args[0])
                        if cap_val.type != i32:
                            raise DanhaTypeError(
                                "HashMap.new(capacity)의 capacity는 i32여야 해",
                                line=line, source=self._source_code
                            )
                        self._last_is_hashmap = True
                        return self.builder.call(self.hm_new_cap_fn, [cap_val], name="hm")
                    if len(args) != 0:
                        raise DanhaValueError(
                            "HashMap.new() 또는 HashMap.new(capacity)만 지원해",
                            line=line, source=self._source_code
                        )
                    self._last_is_hashmap = True
                    return self.builder.call(self.hm_new_fn, [], name="hm")
                raise DanhaNameError(
                    f"HashMap에 '{method_name}'이라는 정적 메서드가 없어",
                    line=line, source=self._source_code
                )
            
            # 30: HashMap 인스턴스 메서드 — m.set/get/has/remove/len
            if obj_node[0] == 'Name':
                slot = self._lookup_var(obj_node[1])
                if slot is not None and slot.type.pointee == i8p:
                    # i8* 변수가 HashMap인지 확인 — _hm_vars에 등록된 경우
                    if hasattr(self, '_hm_vars') and obj_node[1] in self._hm_vars:
                        hm_ptr = self.builder.load(slot, name="hm_ptr")
                        return self._compile_hashmap_method(hm_ptr, method_name, args, line, slot)
            
            # 24b: Arena 정적 메서드 — Arena.new/reset/destroy/used/capacity
            if obj_node[0] == 'Name' and obj_node[1] == 'Arena':
                return self._compile_arena_static_method(method_name, args, line)
            
            # 24b: Arena 인스턴스 메서드 — a.reset()/destroy()/used()/capacity()
            # 변수가 arena 포인터(arena_type*)인지 확인
            if obj_node[0] == 'Name':
                slot = self._lookup_var(obj_node[1])
                if slot is not None:
                    # slot은 alloca 결과 = T*. slot의 pointee가 arena_type*이면 arena임.
                    pointee = slot.type.pointee
                    if pointee == self.arena_type.as_pointer():
                        arena_ptr = self.builder.load(slot, name="arena_ptr")
                        return self._compile_arena_instance_method(arena_ptr, method_name, args, line)
            
            # 9.1c: 모듈 함수 호출 — math.add(1, 2) → _mod_math_add(1, 2)
            if obj_node[0] == 'Name' and obj_node[1] in self._modules:
                mod_name = obj_node[1]
                name_map = self._modules[mod_name]
                if method_name not in name_map:
                    raise DanhaNameError(
                        f"모듈 '{mod_name}'에 '{method_name}'이 없어",
                        line=line, source=self._source_code
                    )
                prefixed = name_map[method_name]
                # tagged enum variant 생성 체크
                if prefixed in self.tagged_enums:
                    tagged_ty, variant_info, max_payload = self.tagged_enums[prefixed]
                    if method_name in variant_info:
                        pass  # 아래의 일반 tagged enum 경로에서 처리됨
                # 일반 함수 호출로 치환
                call_node = ('Call', prefixed, args, line)
                return self.compile_expr(call_node)
            
            # 8.3: tagged enum variant 생성 — Shape.Circle(5.0)
            if obj_node[0] == 'Name' and obj_node[1] in self.tagged_enums:
                enum_name = obj_node[1]
                tagged_ty, variant_info, max_payload = self.tagged_enums[enum_name]
                if method_name not in variant_info:
                    raise DanhaNameError(f"enum '{enum_name}'에 '{method_name}'이라는 variant가 없어", line=line, source=self._source_code)
                tag, llvm_types = variant_info[method_name]
                if llvm_types is None:
                    raise DanhaRuntimeError(f"'{method_name}'은(는) 데이터가 없는 variant야 — 괄호 없이 써", line=line, source=self._source_code)
                if len(args) != len(llvm_types):
                    raise DanhaRuntimeError(
                        f"'{method_name}'은(는) {len(llvm_types)}개의 값이 필요한데 "
                        f"{len(args)}개가 들어왔어",
                        line=line, source=self._source_code
                    )
                return self._build_tagged_enum(enum_name, tag, llvm_types, args, line)
            
            # 8.6: 제네릭 enum variant 생성 — Result.Ok(42)
            if obj_node[0] == 'Name' and hasattr(self, '_generic_enums') and obj_node[1] in self._generic_enums:
                return self._instantiate_generic_enum(obj_node[1], method_name, args, line)
            
            # 29: dyn 객체 메서드 호출 — vtable에서 함수 포인터를 꺼내 indirect call
            # 1) 변수 메타데이터로 감지 (as dyn을 변수에 저장한 경우)
            if obj_node[0] == 'Name' and hasattr(self, '_dyn_var_meta') and obj_node[1] in self._dyn_var_meta:
                return self._compile_dyn_method_call(obj_node[1], method_name, args, line)
            # 2) LLVM 타입으로 감지 (함수 매개변수로 dyn이 넘어온 경우)
            if obj_node[0] == 'Name' and hasattr(self, '_dyn_type_to_trait'):
                slot = self._lookup_var(obj_node[1])
                if slot is not None:
                    pointee = slot.type.pointee
                    if pointee in self._dyn_type_to_trait:
                        trait_name = self._dyn_type_to_trait[pointee]
                        return self._compile_dyn_method_call_by_trait(
                            obj_node[1], trait_name, method_name, args, line
                        )
            
            # 31: 동적 배열 내장 메서드 — arr.map(), arr.filter() 등
            if obj_node[0] == 'Name' and method_name in self._DYNARRAY_METHODS:
                slot = self._lookup_var(obj_node[1])
                if slot is not None:
                    # 고정 배열이면 동적 배열로 변환 후 처리
                    if self._is_fixed_array_slot(slot):
                        dyn_slot = self._fixed_to_dynarray(slot, line)
                        result = self._compile_dynarray_method(dyn_slot, obj_node[1], method_name, args, line)
                        if isinstance(result, ir.instructions.AllocaInstr) and self._is_dynarray_slot(result):
                            tmp_name = f"_chain_{self._lambda_counter}"
                            self._lambda_counter += 1
                            self._declare_var(tmp_name, result)
                        return result
                    # 동적 배열이면 바로 처리
                    if self._is_dynarray_slot(slot):
                        result = self._compile_dynarray_method(slot, obj_node[1], method_name, args, line)
                        if isinstance(result, ir.instructions.AllocaInstr) and self._is_dynarray_slot(result):
                            tmp_name = f"_chain_{self._lambda_counter}"
                            self._lambda_counter += 1
                            self._declare_var(tmp_name, result)
                        return result
            
            # 31: 체이닝 — 메서드 체인의 중간 결과(MethodCall)가 다시 메서드를 호출
            # obj_node가 또 다른 MethodCall이면, 재귀적으로 컴파일 후 결과를 동적 배열로 처리
            if obj_node[0] == 'MethodCall' and method_name in self._DYNARRAY_METHODS:
                inner_result = self.compile_expr(obj_node)
                if isinstance(inner_result, ir.instructions.AllocaInstr) and self._is_dynarray_slot(inner_result):
                    return self._compile_dynarray_method(inner_result, "_chain", method_name, args, line)
            
            # obj의 주소와 어떤 struct인지 알아냄.
            # 36a: Result(tagged enum) 메서드 — .is_ok(), .is_err(), .unwrap_or()
            # obj가 tagged enum 값이면 tag를 비교해서 결과 반환
            if method_name in ('is_ok', 'is_err', 'unwrap_or', 'unwrap', 'context'):
                # obj 값을 컴파일 (Name이든 Call이든 어떤 식이든)
                _result_val = None
                _result_enum = None
                
                if obj_node[0] == 'Name':
                    slot = self._lookup_var(obj_node[1])
                    if slot is not None:
                        pointee = slot.type.pointee
                        for ename, (tagged_ty, vi, mp) in self.tagged_enums.items():
                            if pointee == tagged_ty:
                                _result_val = self.builder.load(slot, name="result_val")
                                _result_enum = ename
                                break
                
                if _result_val is None:
                    # Name이 아니거나 tagged enum이 아닌 경우: 식을 컴파일해서 확인
                    try:
                        expr_val = self.compile_expr(obj_node)
                        for ename, (tagged_ty, vi, mp) in self.tagged_enums.items():
                            if expr_val.type == tagged_ty:
                                _result_val = expr_val
                                _result_enum = ename
                                break
                    except Exception:
                        pass
                
                if _result_val is not None and _result_enum is not None:
                    tagged_ty, variant_info, max_payload = self.tagged_enums[_result_enum]
                    val = _result_val
                    tag_val = self.builder.extract_value(val, 0, name="tag")
                    
                    ok_tag = variant_info.get('Ok', (0, None))[0]
                    
                    if method_name == 'is_ok':
                        return self.builder.icmp_signed(
                            '==', tag_val, ir.Constant(i32, ok_tag), name="is_ok")
                    
                    if method_name == 'is_err':
                        return self.builder.icmp_signed(
                            '!=', tag_val, ir.Constant(i32, ok_tag), name="is_err")
                    
                    if method_name == 'unwrap_or':
                        if len(args) != 1:
                            raise DanhaValueError("unwrap_or()에는 인자 1개가 필요해", line=line, source=self._source_code)
                        default_val = self.compile_expr(args[0])
                        is_ok = self.builder.icmp_signed(
                            '==', tag_val, ir.Constant(i32, ok_tag), name="uo_is_ok")
                        
                        ok_bb = self.current_fn.append_basic_block("uo_ok")
                        err_bb = self.current_fn.append_basic_block("uo_err")
                        merge_bb = self.current_fn.append_basic_block("uo_merge")
                        
                        self.builder.cbranch(is_ok, ok_bb, err_bb)
                        
                        # Ok 경로: payload 추출
                        self.builder.position_at_end(ok_bb)
                        ok_types = variant_info['Ok'][1]
                        if ok_types and len(ok_types) > 0:
                            tmp = self.builder.alloca(tagged_ty, name="uo_tmp")
                            self.builder.store(val, tmp)
                            pay_ptr = self.builder.gep(
                                tmp, [ir.Constant(i32, 0), ir.Constant(i32, 1)],
                                inbounds=True)
                            cast_ptr = self.builder.bitcast(
                                pay_ptr, ok_types[0].as_pointer())
                            ok_val = self.builder.load(cast_ptr, name="uo_ok_val")
                        else:
                            ok_val = default_val
                        self.builder.branch(merge_bb)
                        ok_bb_end = self.builder.block
                        
                        # Err 경로: default 반환
                        self.builder.position_at_end(err_bb)
                        self.builder.branch(merge_bb)
                        err_bb_end = self.builder.block
                        
                        # merge
                        self.builder.position_at_end(merge_bb)
                        phi = self.builder.phi(default_val.type, name="uo_result")
                        phi.add_incoming(ok_val, ok_bb_end)
                        phi.add_incoming(default_val, err_bb_end)
                        return phi
            
            # _lvalue_struct가 일반 변수든 self든 동일하게 처리해줌.
            obj_ptr, struct_info = self._lvalue_struct(obj_node, line)
            llvm_struct, field_names, field_types = struct_info
            # 어느 type_name인지 역검색
            type_name = None
            for sname, sinfo in self.structs.items():
                if sinfo[0] is llvm_struct:
                    type_name = sname
                    break
            
            key = (type_name, method_name)
            if key not in self.methods:
                if method_name in field_names:
                    field_idx = field_names.index(method_name)
                    field_ty = field_types[field_idx]
                    if isinstance(field_ty, ir.PointerType) and isinstance(field_ty.pointee, ir.FunctionType):
                        field_ptr = self.builder.gep(
                            obj_ptr,
                            [ir.Constant(i32, 0), ir.Constant(i32, field_idx)],
                            inbounds=True,
                            name=f"{method_name}_fn_field_ptr"
                        )
                        fn_ptr = self.builder.load(field_ptr, name=f"{method_name}_fn_field")
                        fn_ty = field_ty.pointee
                        if len(args) != len(fn_ty.args):
                            raise DanhaValueError(
                                f"{type_name}.{method_name}은(는) "
                                f"{len(fn_ty.args)}개의 인자가 필요한데 {len(args)}개가 들어왔어",
                                line=line, source=self._source_code
                            )
                        arg_values = []
                        for i, arg_node in enumerate(args):
                            av = self.compile_expr(arg_node)
                            expected_ty = fn_ty.args[i]
                            if av.type != expected_ty:
                                if av.type == i32 and expected_ty == f64:
                                    av = self.builder.sitofp(av, f64, name="argpromoted")
                                elif av.type == i1 and expected_ty == i8:
                                    av = self.builder.zext(av, i8, name="bool_to_i8")
                                else:
                                    raise DanhaTypeError(
                                        f"{type_name}.{method_name}의 "
                                        f"{i+1}번째 인자 타입이 안 맞아 "
                                        f"(기대 {expected_ty}, 실제 {av.type})",
                                        line=line, source=self._source_code
                                    )
                            arg_values.append(av)
                        if isinstance(fn_ty.return_type, ir.VoidType):
                            return self.builder.call(fn_ptr, arg_values)
                        return self.builder.call(fn_ptr, arg_values, name=f"{method_name}_ret")
                raise DanhaNameError(
                    f"{type_name}에 '{method_name}' 메서드가 없어",
                    line=line, source=self._source_code
                )
            fn = self.methods[key]
            
            # 인자 개수 검사 (self는 우리가 자동으로 넣음, 사용자는 self 빼고 셈)
            expected = len(fn.args) - 1
            if len(args) != expected:
                raise DanhaValueError(
                    f"{type_name}.{method_name}은(는) "
                    f"{expected}개의 인자가 필요한데 {len(args)}개가 들어왔어",
                    line=line, source=self._source_code
                )
            
            # 7.1.3 파트 3 검사 B: 메서드 호출자의 참조 종류와 시그니처 매칭.
            # method_param_ref_kinds의 0번은 self, 1번부터가 사용자 인자에 대응.
            method_refs = self.method_param_ref_kinds.get(key, [None] * (len(args) + 1))
            sig_refs = method_refs[1:]  # self 건너뛰기
            for i, (arg_node, sig_ref) in enumerate(zip(args, sig_refs)):
                caller_ref = None
                if arg_node[0] == 'AddrOf':
                    caller_ref = 'mut_ref' if arg_node[1] else 'ref'
                self._check_arg_ref_match(
                    f"{type_name}.{method_name}", i, caller_ref, sig_ref, arg_node[-1]
                )
            
            arg_values = [obj_ptr]  # self가 첫 인자
            for i, a in enumerate(args):
                av = self.compile_expr(a)
                expected_ty = fn.args[i + 1].type  # +1 왜냐면 0번은 self
                if av.type != expected_ty:
                    if av.type == i32 and expected_ty == f64:
                        av = self.builder.sitofp(av, f64, name="argpromoted")
                    else:
                        raise DanhaTypeError(
                            f"{type_name}.{method_name}의 "
                            f"{i+1}번째 인자 타입이 안 맞아 "
                            f"(기대 {expected_ty}, 실제 {av.type})",
                            line=line, source=self._source_code
                        )
                arg_values.append(av)
            
            return self.builder.call(fn, arg_values, name="methodcall")
        
        if node_type == 'Add':
            left = self.compile_expr(node[1])
            right = self.compile_expr(node[2])
            # 7.7: 벡터 덧셈
            vec_result = self._compile_vec_binop(
                left, right,
                lambda a, b: self.builder.add(a, b, name="vadd"),
                lambda a, b: self.builder.fadd(a, b, name="vadd"),
                node[-1], '+'
            )
            if vec_result is not None:
                return vec_result
            # 8.2: 문자열 concat — i8* + i8* → 아레나에서 새 문자열 할당
            if left.type == i8p and right.type == i8p:
                return self._compile_str_concat(left, right)
            left, right, is_float = self._promote_numeric(left, right, node[-1], '+')
            if is_float:
                return self.builder.fadd(left, right, name="addtmp")
            return self.builder.add(left, right, name="addtmp")
        
        if node_type == 'Sub':
            left = self.compile_expr(node[1])
            right = self.compile_expr(node[2])
            # 7.7: 벡터 뺄셈
            vec_result = self._compile_vec_binop(
                left, right,
                lambda a, b: self.builder.sub(a, b, name="vsub"),
                lambda a, b: self.builder.fsub(a, b, name="vsub"),
                node[-1], '-'
            )
            if vec_result is not None:
                return vec_result
            left, right, is_float = self._promote_numeric(left, right, node[-1], '-')
            if is_float:
                return self.builder.fsub(left, right, name="subtmp")
            return self.builder.sub(left, right, name="subtmp")
        
        if node_type == 'Mul':
            left = self.compile_expr(node[1])
            right = self.compile_expr(node[2])
            # 7.9a + Phase 3: mat4 * vec4 → vec4 (SIMD column-vector linear combination)
            # result = col0*splat(v.x) + col1*splat(v.y) + col2*splat(v.z) + col3*splat(v.w)
            # AVX2+에서 4 broadcasts + 4 vmulpd + 3 vaddpd (또는 vfmadd231pd 3개)
            if self._is_mat_type(left.type) and self._is_vec_type(right.type) == ('vec4', 4):
                vty = self._VEC_INFO['vec4'][0]
                cols = [self._mat4_col(left, c, name=f"mc{c}") for c in range(4)]
                splats = [
                    self._splat(self._vec_extract(right, c, name=f"v{c}"), vty)
                    for c in range(4)
                ]
                # FMA 누적: acc = c0*v.x + c1*v.y + c2*v.z + c3*v.w
                acc = self.builder.fmul(cols[0], splats[0], name="mv0")
                for c in range(1, 4):
                    prod = self.builder.fmul(cols[c], splats[c], name=f"mv{c}")
                    acc = self.builder.fadd(acc, prod, name=f"macc{c}")
                return acc

            # 7.9a + Phase 3: mat4 * mat4 → mat4 (column-vector composition)
            # 각 결과 컬럼 c = left * right.col[c] (위의 mat*vec4 적용)
            if self._is_mat_type(left.type) and self._is_mat_type(right.type):
                result = ir.Constant(self._mat4_ty, ir.Undefined)
                left_cols = [self._mat4_col(left, c, name=f"lc{c}") for c in range(4)]
                for out_c in range(4):
                    rc = self._mat4_col(right, out_c, name=f"rc{out_c}")
                    splats = [
                        self._splat(self.builder.extract_element(rc, ir.Constant(i32, r), name=f"re{out_c}{r}"), self._mat4_col_ty)
                        for r in range(4)
                    ]
                    acc = self.builder.fmul(left_cols[0], splats[0], name=f"mm{out_c}0")
                    for k in range(1, 4):
                        prod = self.builder.fmul(left_cols[k], splats[k], name=f"mm{out_c}{k}")
                        acc = self.builder.fadd(acc, prod, name=f"mma{out_c}{k}")
                    result = self.builder.insert_value(result, acc, out_c, name=f"mres{out_c}")
                return result
            # 7.7: 벡터 곱셈 (성분별 또는 스칼라 곱)
            vec_result = self._compile_vec_binop(
                left, right,
                lambda a, b: self.builder.mul(a, b, name="vmul"),
                lambda a, b: self.builder.fmul(a, b, name="vmul"),
                node[-1], '*'
            )
            if vec_result is not None:
                return vec_result
            left, right, is_float = self._promote_numeric(left, right, node[-1], '*')
            if is_float:
                return self.builder.fmul(left, right, name="multmp")
            return self.builder.mul(left, right, name="multmp")
        
        if node_type == 'Div':
            left = self.compile_expr(node[1])
            right = self.compile_expr(node[2])
            # 7.7: 벡터 나눗셈
            vec_result = self._compile_vec_binop(
                left, right,
                lambda a, b: self.builder.sdiv(a, b, name="vdiv"),
                lambda a, b: self.builder.fdiv(a, b, name="vdiv"),
                node[-1], '/'
            )
            if vec_result is not None:
                return vec_result
            left, right, is_float = self._promote_numeric(left, right, node[-1], '/')
            if is_float:
                return self.builder.fdiv(left, right, name="divtmp")
            return self.builder.sdiv(left, right, name="divtmp")
        
        if node_type == 'Mod':
            # % 연산: 정수는 srem. 실수의 경우 LLVM에 frem도 있지만
            # 인터프리터는 파이썬 %를 써서 실수도 허용하는데, 게임 코드에서 실수 %는
            # 자주 버그의 원인이라 컴파일러 레벨에선 정수로만 제한하는 게 나아.
            # 필요하면 나중에 풀자 (혹은 fmod 함수 제공).
            left = self.compile_expr(node[1])
            right = self.compile_expr(node[2])
            if left.type != i32 or right.type != i32:
                raise DanhaRuntimeError(f"[{node[-1]}번째 줄] '%'는 정수에만 쓸 수 있어")
            return self.builder.srem(left, right, name="modtmp")

        # 42단계: @sizeof(Type) / @alignof(Type)
        # i32 반환: _promote_numeric이 i32/f64만 허용하고 i64는 모름.
        # 실제 usize(i64)가 필요하면 그때 _promote_numeric도 같이 확장.
        if node_type == 'SizeOf':
            type_node = node[1]
            line = node[-1]
            llvm_ty, _ = self._resolve_type(type_node, "@sizeof", line)
            size = self._sizeof_bytes(llvm_ty)
            return ir.Constant(i32, size)

        if node_type == 'AlignOf':
            type_node = node[1]
            line = node[-1]
            llvm_ty, _ = self._resolve_type(type_node, "@alignof", line)
            size = self._sizeof_bytes(llvm_ty)
            return ir.Constant(i32, min(size, 8))

        # 45단계: @sin, @cos, @sqrt, ... — 수학 내장 함수 (libm 호출)
        if node_type == 'MathIntrinsic':
            fn_name = node[1]
            arg_exprs = node[2]
            compiled_args = [self.compile_expr(a) for a in arg_exprs]

            def _to_f64(v):
                if v.type == i32:
                    return self.builder.sitofp(v, f64, name="math_f64")
                return v

            def _libm(name, n_args=1):
                if name in self.module.globals:
                    return self.module.globals[name]
                fn_ty = ir.FunctionType(f64, [f64] * n_args)
                return ir.Function(self.module, fn_ty, name=name)

            _FLOAT_FUNS = {'sin','cos','tan','sqrt','log','exp','fabs','floor','ceil','atan2','hypot','pow'}
            if fn_name in {'sin','cos','tan','sqrt','log','exp'}:
                a = _to_f64(compiled_args[0])
                return self.builder.call(_libm(fn_name), [a], name=f"{fn_name}_r")
            if fn_name in {'floor','ceil','round'}:
                a = _to_f64(compiled_args[0])
                libm_name = 'round' if fn_name == 'round' else fn_name
                return self.builder.call(_libm(libm_name), [a], name=f"{fn_name}_r")
            if fn_name in {'pow','atan2','hypot'}:
                a, b = _to_f64(compiled_args[0]), _to_f64(compiled_args[1])
                return self.builder.call(_libm(fn_name, 2), [a, b], name=f"{fn_name}_r")
            if fn_name == 'abs':
                arg = compiled_args[0]
                if arg.type == i32:
                    zero = ir.Constant(i32, 0)
                    neg  = self.builder.neg(arg, name="neg")
                    cmp  = self.builder.icmp_signed('<', arg, zero, name="isneg")
                    return self.builder.select(cmp, neg, arg, name="abs_r")
                a = _to_f64(arg)
                return self.builder.call(_libm('fabs'), [a], name="fabs_r")
            if fn_name in {'min','max'}:
                a, b = compiled_args
                if a.type == i32 and b.type == i32:
                    op = '<' if fn_name == 'min' else '>'
                    cmp = self.builder.icmp_signed(op, a, b, name=f"{fn_name}_cmp")
                    return self.builder.select(cmp, a, b, name=f"{fn_name}_r")
                a, b = _to_f64(a), _to_f64(b)
                op = '<' if fn_name == 'min' else '>'
                cmp = self.builder.fcmp_ordered(op, a, b, name=f"{fn_name}_cmp")
                return self.builder.select(cmp, a, b, name=f"{fn_name}_r")

        # 비트 연산 — LShift, RShift, BitAnd, BitOr, BitXor, BitNot
        if node_type in ('LShift', 'RShift', 'BitAnd', 'BitOr', 'BitXor'):
            left = self.compile_expr(node[1])
            right = self.compile_expr(node[2])
            left, right = self._promote_int_pair(left, right, node[-1])
            if node_type == 'LShift':
                return self.builder.shl(left, right, name="shltmp")
            if node_type == 'RShift':
                return self.builder.lshr(left, right, name="lshrtmp")
            if node_type == 'BitAnd':
                return self.builder.and_(left, right, name="bandtmp")
            if node_type == 'BitOr':
                return self.builder.or_(left, right, name="bortmp")
            # BitXor
            return self.builder.xor(left, right, name="bxortmp")

        if node_type == 'BitNot':
            val = self.compile_expr(node[1])
            if not isinstance(val.type, ir.IntType):
                raise DanhaRuntimeError(f"[{node[-1]}번째 줄] '~'는 정수에만 쓸 수 있어")
            all_ones = ir.Constant(val.type, (1 << val.type.width) - 1)
            return self.builder.xor(val, all_ones, name="bntmp")

        # 논리 연산: and / or 는 단락 평가.
        # 인터프리터는 파이썬의 if로 자연스럽게 했지만, 컴파일러는 분기와 phi로 만든다.
        # 핵심 비유: 갈림길에서 한쪽으로 갔다가 합류 지점에서 "어느 길로 왔지?"
        # 를 보고 값을 고르는 거. phi가 그 '고르기' 역할.
        #
        # 인터프리터 동작과 미세한 차이가 있어서 짚고 넘어감:
        # 인터프리터의 'a and b'는 a가 거짓이면 *a 그 자체*를 돌려준다 (파이썬 관행).
        # 그래서 '0 and 5' 가 0을 돌려주는 식. 컴파일러는 항상 i1을 돌려준다.
        # Danha에서 and/or의 양쪽은 항상 불리언이라고 약속하고 있어서 (i32와 안 섞임)
        # 결과도 i1이면 충분. 더 엄격한 타입 시스템에 가깝다.
        if node_type == 'And':
            left = self.compile_expr(node[1])
            # L-1: i8(bool)/i32 자동 i1 변환
            if left.type != i1 and isinstance(left.type, ir.IntType):
                left = self.builder.icmp_signed('!=', left, ir.Constant(left.type, 0), name='and_l_to_i1')
            if left.type != i1:
                raise DanhaRuntimeError(f"[{node[-1]}번째 줄] 'and'의 왼쪽은 불리언이어야 해")
            
            # 분기 시작 직전의 블록을 기억해 둔다. phi에서 '어디서 왔는지' 알아야 함.
            # 분기와 본문 컴파일 과정에서 builder가 다른 블록으로 옮겨갈 수 있어서,
            # *지금* 잡아둬야 정확한 '들어온 블록'을 가리킬 수 있다.
            entry_b = self.builder.block
            eval_right_b = self.current_fn.append_basic_block(name="and_right")
            end_b = self.current_fn.append_basic_block(name="and_end")
            
            # 왼쪽이 참이면 오른쪽 평가, 거짓이면 끝으로 점프 (오른쪽은 안 봄)
            self.builder.cbranch(left, eval_right_b, end_b)
            
            # 오른쪽 평가
            self.builder.position_at_end(eval_right_b)
            right = self.compile_expr(node[2])
            if right.type != i1 and isinstance(right.type, ir.IntType):
                right = self.builder.icmp_signed('!=', right, ir.Constant(right.type, 0), name='and_r_to_i1')
            if right.type != i1:
                raise DanhaRuntimeError(f"[{node[-1]}번째 줄] 'and'의 오른쪽은 불리언이어야 해")
            # 오른쪽 평가 도중에 블록이 또 갈라졌을 수 있다 (예: b가 또 and/or를 포함).
            # phi의 '들어온 블록'은 컴파일 직후의 builder.block이 정답.
            right_end_b = self.builder.block
            self.builder.branch(end_b)
            
            # 합류: 왼쪽이 거짓이라 일찍 왔으면 false, 오른쪽까지 평가했으면 그 값
            self.builder.position_at_end(end_b)
            result = self.builder.phi(i1, name="andtmp")
            result.add_incoming(ir.Constant(i1, 0), entry_b)
            result.add_incoming(right, right_end_b)
            return result
        
        if node_type == 'Or':
            left = self.compile_expr(node[1])
            if left.type != i1 and isinstance(left.type, ir.IntType):
                left = self.builder.icmp_signed('!=', left, ir.Constant(left.type, 0), name='or_l_to_i1')
            if left.type != i1:
                raise DanhaRuntimeError(f"[{node[-1]}번째 줄] 'or'의 왼쪽은 불리언이어야 해")
            
            entry_b = self.builder.block
            eval_right_b = self.current_fn.append_basic_block(name="or_right")
            end_b = self.current_fn.append_basic_block(name="or_end")
            
            # 왼쪽이 참이면 끝으로 (오른쪽 안 봄), 거짓이면 오른쪽 평가
            # and와 정반대 방향
            self.builder.cbranch(left, end_b, eval_right_b)
            
            self.builder.position_at_end(eval_right_b)
            right = self.compile_expr(node[2])
            if right.type != i1 and isinstance(right.type, ir.IntType):
                right = self.builder.icmp_signed('!=', right, ir.Constant(right.type, 0), name='or_r_to_i1')
            if right.type != i1:
                raise DanhaRuntimeError(f"[{node[-1]}번째 줄] 'or'의 오른쪽은 불리언이어야 해")
            right_end_b = self.builder.block
            self.builder.branch(end_b)
            
            # 왼쪽이 참이라 일찍 왔으면 true, 오른쪽까지 갔으면 그 값
            self.builder.position_at_end(end_b)
            result = self.builder.phi(i1, name="ortmp")
            result.add_incoming(ir.Constant(i1, 1), entry_b)
            result.add_incoming(right, right_end_b)
            return result
        
        if node_type == 'Not':
            # not은 단락 평가 필요 없음. 그냥 비트 뒤집기.
            operand = self.compile_expr(node[1])
            # L-1: 정수 타입(i8 bool, i32 등)을 자동 i1로 변환
            if operand.type != i1 and isinstance(operand.type, ir.IntType):
                operand = self.builder.icmp_signed('!=', operand, ir.Constant(operand.type, 0), name='not_to_i1')
            if operand.type != i1:
                raise DanhaRuntimeError(f"[{node[-1]}번째 줄] 'not'의 피연산자는 불리언이어야 해")
            return self.builder.xor(operand, ir.Constant(i1, 1), name="nottmp")
        
        if node_type == 'Neg':
            # 단항 빼기. 정수면 0 - x, 실수면 fneg (부호 비트 하나 뒤집기, 빠름).
            operand = self.compile_expr(node[1])
            # 7.7: 벡터 부호 반전 — SIMD fneg 또는 성분별 fneg
            vec_info = self._is_vec_type(operand.type)
            if vec_info is not None:
                vec_name, size = vec_info
                vty = self._VEC_INFO[vec_name][0]
                if isinstance(vty, ir.VectorType):
                    return self.builder.fneg(operand, name="vneg")
                result = ir.Constant(vty, ir.Undefined)
                for idx in range(size):
                    comp = self._vec_extract(operand, idx, name=f"c{idx}")
                    neg_comp = self.builder.fneg(comp, name=f"neg{idx}")
                    result = self._vec_insert(result, neg_comp, idx, name=f"v{idx}")
                return result
            if operand.type == f64:
                return self.builder.fneg(operand, name="negtmp")
            if operand.type == i32:
                return self.builder.sub(ir.Constant(i32, 0), operand, name="negtmp")
            raise DanhaRuntimeError(f"[{node[-1]}번째 줄] '-'는 숫자에만 쓸 수 있어")
        
        # 29: 동적 디스패치 캐스팅 — expr as dyn Trait
        # 구조체 값을 팻 포인터 {i8*, i8*}로 변환한다.
        # 첫 번째는 구조체 데이터의 포인터, 두 번째는 vtable의 포인터.
        if node_type == 'DynCast':
            inner_node = node[1]
            trait_name = node[2]
            line = node[-1]
            
            if trait_name not in self._traits:
                raise DanhaNameError(
                    f"정의되지 않은 트레잇이야: {trait_name}",
                    line=line, source=self._source_code
                )
            
            # inner를 컴파일 — 구조체 값이 나와야 함
            val = self.compile_expr(inner_node)
            
            # 어떤 struct 타입인지 찾기
            type_name = None
            for sname, (sty, _, _) in self.structs.items():
                if val.type == sty:
                    type_name = sname
                    break
            
            if type_name is None:
                raise DanhaTypeError(
                    f"'as dyn {trait_name}'은(는) 구조체 값에만 쓸 수 있어",
                    line=line, source=self._source_code
                )
            
            vtable_key = (trait_name, type_name)
            if vtable_key not in self._vtables:
                raise DanhaTypeError(
                    f"{type_name}이(가) {trait_name} 트레잇을 구현하지 않았어",
                    line=line, source=self._source_code
                )
            
            # 구조체 값을 아레나에 복사하고 i8*로 bitcast.
            # 이전엔 builder.alloca로 스택에 두었으나 (a) 루프 안에서 스택을
            # 먹어 ~60K iter에서 오버플로우, (b) 함수 진입 호이스트는 dyn
            # 포인터를 배열에 push할 때 같은 슬롯을 공유시켜 잘못된 결과를
            # 만든다. 아레나에서 매 cast마다 새 영역을 받아야 둘 다 해결.
            struct_ty = self.structs[type_name][0]
            struct_size = self._sizeof_bytes(struct_ty)
            data_i8 = self._arena_alloc(ir.Constant(i32, struct_size), name=f"dyn_data_{type_name}_i8")
            data_slot = self.builder.bitcast(data_i8, struct_ty.as_pointer(), name=f"dyn_data_{type_name}")
            self.builder.store(val, data_slot)
            data_ptr = self.builder.bitcast(data_slot, i8.as_pointer(), name="dyn_data_ptr")
            
            # vtable 글로벌의 첫 원소 주소를 i8*로
            vtable_global = self._vtables[vtable_key]
            vtable_ptr = self.builder.bitcast(vtable_global, i8.as_pointer(), name="dyn_vtable_ptr")
            
            # 팻 포인터 %dyn.TraitName = {i8*, i8*} 조립
            dyn_ty = self._dyn_types[trait_name]
            dyn_val = ir.Constant(dyn_ty, ir.Undefined)
            dyn_val = self.builder.insert_value(dyn_val, data_ptr, 0, name="dyn_with_data")
            dyn_val = self.builder.insert_value(dyn_val, vtable_ptr, 1, name="dyn_with_vtable")
            
            # 어떤 trait/type인지 기억 — compile_assign이 변수에 연결함
            self._last_dyn_meta = (trait_name, type_name)
            
            return dyn_val
        
        # 16: 타입 캐스팅 — expr as type
        if node_type == 'Cast':
            val = self.compile_expr(node[1])
            target = node[2]  # "i32", "f64", "str", "bool"
            line = node[-1]
            
            if target == 'i32':
                if val.type == i32:
                    return val
                if val.type == f64:
                    return self.builder.fptosi(val, i32, name="cast_i32")
                if val.type == ir.IntType(1):
                    return self.builder.zext(val, i32, name="bool_to_i32")
                raise DanhaTypeError(f"이 타입을 i32로 캐스팅할 수 없어", line=line, source=self._source_code)
            
            elif target == 'f64':
                if val.type == f64:
                    return val
                if val.type == i32:
                    return self.builder.sitofp(val, f64, name="cast_f64")
                if val.type == f32:
                    return self.builder.fpext(val, f64, name="f32_to_f64")
                if val.type == i64:
                    return self.builder.sitofp(val, f64, name="i64_to_f64")
                if val.type == ir.IntType(1):
                    val_i32 = self.builder.zext(val, i32, name="bool_i32")
                    return self.builder.sitofp(val_i32, f64, name="bool_to_f64")
                raise DanhaTypeError(f"이 타입을 f64로 캐스팅할 수 없어", line=line, source=self._source_code)

            elif target == 'f32':
                if val.type == f32:
                    return val
                if val.type == f64:
                    return self.builder.fptrunc(val, f32, name="f64_to_f32")
                if val.type == i32:
                    return self.builder.sitofp(val, f32, name="i32_to_f32")
                if val.type == i64:
                    return self.builder.sitofp(val, f32, name="i64_to_f32")
                raise DanhaTypeError(f"이 타입을 f32로 캐스팅할 수 없어", line=line, source=self._source_code)
            
            elif target == 'str':
                # 숫자 → 문자열: 아레나에서 버퍼 할당 + sprintf
                buf_size = 32
                if val.type == i32:
                    buf = self._arena_alloc(ir.Constant(i32, buf_size), name="cast_str_buf")
                    fmt = self.builder.bitcast(self.fmt_to_str_int, i8p)
                    self.builder.call(self.sprintf_fn, [buf, fmt, val])
                    return buf
                if val.type == f64:
                    buf = self._arena_alloc(ir.Constant(i32, buf_size), name="cast_str_buf")
                    fmt = self.builder.bitcast(self.fmt_to_str_float, i8p)
                    self.builder.call(self.sprintf_fn, [buf, fmt, val])
                    return buf
                if val.type == ir.IntType(1):
                    # bool → "true" / "false" (Windows MCJIT에서 select+i8* 조합이 AV 나는 경우가 있어 phi 사용)
                    then_b = self.current_fn.append_basic_block(name="cast_b2s_t")
                    else_b = self.current_fn.append_basic_block(name="cast_b2s_f")
                    end_b = self.current_fn.append_basic_block(name="cast_b2s_e")
                    self.builder.cbranch(val, then_b, else_b)
                    self.builder.position_at_end(then_b)
                    tp = self.builder.bitcast(self.str_true_plain, i8p)
                    self.builder.branch(end_b)
                    self.builder.position_at_end(else_b)
                    fp = self.builder.bitcast(self.str_false_plain, i8p)
                    self.builder.branch(end_b)
                    self.builder.position_at_end(end_b)
                    ph = self.builder.phi(i8p, name="cast_bool_str")
                    ph.add_incoming(tp, then_b)
                    ph.add_incoming(fp, else_b)
                    return ph
                if val.type == i8p:
                    return val
                raise DanhaTypeError(f"이 타입을 str로 캐스팅할 수 없어", line=line, source=self._source_code)
            
            elif target == 'bool':
                if val.type == ir.IntType(1):
                    return val
                if val.type == i32:
                    return self.builder.icmp_signed('!=', val, ir.Constant(i32, 0), name="cast_bool")
                if val.type == f64:
                    return self.builder.fcmp_ordered('!=', val, ir.Constant(f64, 0.0), name="cast_bool")
                raise DanhaTypeError(f"이 타입을 bool로 캐스팅할 수 없어", line=line, source=self._source_code)
            
            elif target in self._TYPE_MAP and isinstance(self._TYPE_MAP[target], ir.IntType):
                target_ty = self._TYPE_MAP[target]
                if isinstance(val.type, ir.IntType):
                    if val.type.width == target_ty.width:
                        return val
                    if val.type.width > target_ty.width:
                        return self.builder.trunc(val, target_ty, name=f"cast_{target}")
                    return self.builder.zext(val, target_ty, name=f"cast_{target}")
                if isinstance(val.type, (ir.DoubleType, ir.FloatType)):
                    return self.builder.fptosi(val, target_ty, name=f"cast_{target}")
                raise DanhaTypeError(f"이 타입을 {target}로 캐스팅할 수 없어", line=line, source=self._source_code)

            else:
                raise DanhaTypeError(f"'{target}'은(는) 캐스팅 대상 타입이 아니야", line=line, source=self._source_code)
        
        # 15: 람다(익명 함수) — fn(a: i32, b: i32) -> i32 { return a + b }
        # 고유 이름으로 LLVM 함수를 만들고 함수 포인터를 반환.
        # 컴파일러에서는 타입 어노테이션이 필요 (정적 타입).
        if node_type == 'Lambda':
            params = node[1]
            body = node[2]
            param_types = node[3]  # 타입 어노테이션 리스트
            return_type = node[4]  # 반환 타입 노드 or None
            line = node[-1]
            
            # 고유 이름 생성
            lambda_name = f"_lambda_{self._lambda_counter}"
            self._lambda_counter += 1
            
            # 클로저 캡처: 본문에서 외부 변수 찾기
            free_vars = self._find_free_vars(body[1], params)
            capture_vals = []  # (name, llvm_val, llvm_type)
            for fv in free_vars:
                slot = self._lookup_var(fv)
                if slot is not None:
                    val = self.builder.load(slot, name=f"cap_{fv}")
                    capture_vals.append((fv, val, val.type))
            
            # 타입 해석
            llvm_param_types = []
            for i, pt_node in enumerate(param_types):
                llvm_ty, _ = self._resolve_type(pt_node, f"람다 매개변수 {i}", line)
                llvm_param_types.append(llvm_ty)
            
            # 캡처 변수를 추가 매개변수로 시그니처에 추가
            capture_types = [ct for _, _, ct in capture_vals]
            full_param_types = llvm_param_types + capture_types
            
            llvm_ret_type, _ = self._resolve_type(return_type, "람다 반환", line)
            
            # LLVM 함수 생성
            fn_ty = ir.FunctionType(llvm_ret_type, full_param_types)
            fn = ir.Function(self.module, fn_ty, name=lambda_name)
            for arg, pname in zip(fn.args[:len(params)], params):
                arg.name = pname
            for arg, (cname, _, _) in zip(fn.args[len(params):], capture_vals):
                arg.name = f"_cap_{cname}"
            
            self.functions[lambda_name] = fn
            self.fn_param_ref_kinds[lambda_name] = [None] * len(full_param_types)
            
            # 캡처 정보 저장 (호출 시 사용)
            self._lambda_captures[lambda_name] = [
                (cname, cval) for cname, cval, _ in capture_vals
            ]
            
            # 현재 컨텍스트 저장
            saved_fn = self.current_fn
            saved_builder = self.builder
            saved_vars = self.vars
            
            # 람다 본문 컴파일
            self.current_fn = fn
            entry = fn.append_basic_block(name="entry")
            self.builder = ir.IRBuilder(entry)
            self.vars = [{}]
            
            # 원래 매개변수 할당
            for arg, pname in zip(fn.args[:len(params)], params):
                slot = self.builder.alloca(arg.type, name=pname)
                self.builder.store(arg, slot)
                self._declare_var(pname, slot)
            
            # 캡처 변수를 지역 변수로 할당
            for arg, (cname, _, _) in zip(fn.args[len(params):], capture_vals):
                slot = self.builder.alloca(arg.type, name=cname)
                self.builder.store(arg, slot)
                self._declare_var(cname, slot)
            
            for stmt in body[1]:
                self.compile_stmt(stmt)
            
            if not self.builder.block.is_terminated:
                if isinstance(llvm_ret_type, ir.VoidType):
                    self.builder.ret_void()
                elif isinstance(llvm_ret_type, ir.PointerType):
                    self.builder.ret(ir.Constant(llvm_ret_type, None))
                else:
                    self.builder.ret(ir.Constant(llvm_ret_type, 0))
            
            # 컨텍스트 복원
            self.current_fn = saved_fn
            self.builder = saved_builder
            self.vars = saved_vars
            
            # 함수 포인터 반환
            return fn
        
        # 23c: ? 연산자 — Result 에러 전파 (컴파일러).
        # expr? → tag 검사:
        #   Ok variant (tag 0 관례) → payload 값 추출
        #   Err variant → 현재 함수에서 tagged enum 값을 그대로 ret
        if node_type == 'QuestionOp':
            inner = node[1]
            line = node[-1]
            val = self.compile_expr(inner)
            val_ty = val.type
            
            # 어떤 tagged enum인지 찾기
            enum_info = None
            enum_name = None
            for ename, (tagged_ty, variant_info, max_payload) in self.tagged_enums.items():
                if val_ty == tagged_ty:
                    enum_info = (tagged_ty, variant_info, max_payload)
                    enum_name = ename
                    break
            
            if enum_info is None:
                raise DanhaTypeError(
                    "? 연산자는 tagged enum (Ok/Err variant가 있는) 값에만 쓸 수 있어",
                    line=line, source=self._source_code
                )
            
            tagged_ty, variant_info, max_payload = enum_info
            
            if 'Ok' not in variant_info or 'Err' not in variant_info:
                raise DanhaTypeError(
                    "? 연산자는 Ok과 Err variant가 둘 다 있는 enum에만 쓸 수 있어",
                    line=line, source=self._source_code
                )
            
            ok_tag, ok_types = variant_info['Ok']
            err_tag, err_types = variant_info['Err']
            
            # 임시 alloca에 값 저장 (payload GEP용)
            tmp = self.builder.alloca(tagged_ty, name="q_tmp")
            self.builder.store(val, tmp)
            
            # tag 추출
            tag_val = self.builder.extract_value(val, 0, name="q_tag")
            
            # tag == ok_tag?
            is_ok = self.builder.icmp_signed(
                '==', tag_val, ir.Constant(i32, ok_tag), name="q_is_ok"
            )
            
            ok_bb = self.current_fn.append_basic_block(name="q_ok")
            err_bb = self.current_fn.append_basic_block(name="q_err")
            cont_bb = self.current_fn.append_basic_block(name="q_cont")
            
            self.builder.cbranch(is_ok, ok_bb, err_bb)
            
            # Err 경로: 현재 함수에서 tagged enum 값 그대로 반환
            self.builder.position_at_end(err_bb)
            self.builder.ret(val)
            
            # Ok 경로: payload 추출
            self.builder.position_at_end(ok_bb)
            
            if ok_types is not None and len(ok_types) > 0:
                payload_ptr = self.builder.gep(
                    tmp, [ir.Constant(i32, 0), ir.Constant(i32, 1)],
                    inbounds=True, name="q_pay_ptr"
                )
                pay_i8 = self.builder.bitcast(
                    payload_ptr, ir.IntType(8).as_pointer()
                )
                # 첫 번째 payload 값 추출 (Ok(val) 패턴)
                first_ty = ok_types[0]
                typed_ptr = self.builder.bitcast(
                    pay_i8, first_ty.as_pointer(), name="q_ok_ptr"
                )
                ok_val = self.builder.load(typed_ptr, name="q_ok_val")
            else:
                ok_val = ir.Constant(i32, 0)  # Ok() — 데이터 없는 경우
            
            self.builder.branch(cont_bb)
            
            self.builder.position_at_end(cont_bb)
            return ok_val
        
        # 15: 식 결과 호출 — callbacks[i](10), make_adder(5)(3) 등
        # 함수 포인터를 통한 간접 호출.
        if node_type == 'CallExpr':
            callee = self.compile_expr(node[1])
            args = node[2]
            line = node[-1]
            
            # callee가 함수 포인터인지 확인
            if not isinstance(callee.type, ir.PointerType):
                raise DanhaTypeError("호출 대상이 함수가 아니야", line=line, source=self._source_code)
            
            arg_values = [self.compile_expr(a) for a in args]
            
            # 함수 포인터의 pointee 타입에서 함수 시그니처 추출
            fn_ty = callee.function_type if hasattr(callee, 'function_type') else callee.type.pointee
            
            # 인자 타입 승격
            for i, (av, expected_ty) in enumerate(zip(arg_values, fn_ty.args)):
                if av.type != expected_ty:
                    if av.type == i32 and expected_ty == f64:
                        arg_values[i] = self.builder.sitofp(av, f64, name="argpromoted")
            
            if isinstance(fn_ty.return_type, ir.VoidType):
                return self.builder.call(callee, arg_values)
            return self.builder.call(callee, arg_values, name="calltmp")
        
        # 20c: comptime 블록 — 인터프리터로 사전 평가 후 LLVM 상수로 변환
        # comptime { ... }의 결과값을 컴파일 타임에 확정하고 리터럴 상수로 방출.
        if node_type == 'Comptime':
            return self._compile_comptime(node)
        
        # 22c: 매크로 호출 (식 위치)
        if node_type == 'MacroCall':
            return self._compile_macro_call_expr(node)
        
        # 21c: unsafe 블록이 식 위치에서 사용될 때 — x = unsafe { 42 }
        if node_type == 'UnsafeBlock':
            body = node[1]
            old_unsafe = getattr(self, '_in_unsafe', 0)
            self._in_unsafe = old_unsafe + 1
            try:
                return self.compile_expr(body)
            finally:
                self._in_unsafe = old_unsafe
        
        # Block이 식 위치에 올 때 — 마지막 문장의 값을 반환.
        # unsafe { expr } 나 comptime { expr } 등에서 내부 Block이 이 경로로 옴.
        if node_type == 'Block':
            stmts = node[1]
            self._push_scope()
            try:
                result = None
                for i, stmt in enumerate(stmts):
                    if i == len(stmts) - 1:
                        # 마지막 문장: 식이면 값을 반환
                        try:
                            result = self.compile_expr(stmt)
                        except (NotImplementedError, AttributeError):
                            self.compile_stmt(stmt)
                            result = ir.Constant(i32, 0)
                    else:
                        self.compile_stmt(stmt)
                return result if result is not None else ir.Constant(i32, 0)
            finally:
                self._pop_scope()
        
        raise NotImplementedError(f"6-1에서 아직 지원 안 함: {node_type}")


def compile_module(ast, base_dir=None, source_code=None, runtime_mode='libc', entry_arg_mode=False):
    """AST를 받아 LLVM 모듈을 만들고 돌려준다."""
    # 9.1c: base_dir 설정. 캐시는 mtime 기반으로 자동 무효화되므로 clear 불필요.
    # 순환 감지 셋만 리셋 (컴파일마다 새 순환 맥락).
    Compiler._MODULE_LOADING_COMPILE = set()
    Compiler._MODULE_BASE_DIR_COMPILE = base_dir
    c = Compiler(runtime_mode=runtime_mode, entry_arg_mode=entry_arg_mode)
    c._source_code = source_code
    c.compile_program(ast)
    return c.module


def _inject_fast_math_flags(llvm_ir):
    """Add fast-math only to LLVM instruction lines, never inside string constants."""
    import re
    return re.sub(
        r'(?m)^(\s*%[^=\n]+ = )(fadd|fsub|fmul|fdiv|fneg) ',
        r'\1\2 fast ',
        llvm_ir,
    )


def run_native(source_code, opt_level=3, libs=None, base_dir=None, runtime_mode='libc'):
    """
    Danha 소스를 받아서 컴파일 → 최적화 → 네이티브 기계어 → 실행.
    print 출력은 표준출력에 그대로 나간다 (인터프리터와 같음).
    main의 반환값(int)을 돌려준다.
    
    opt_level: LLVM 최적화 수준 (0=없음, 2=표준(기본값)).
    libs: 로드할 외부 공유 라이브러리 경로 리스트 (예: ['./libdanha_sdl.so']).
          extern fn이 참조하는 C 함수가 이 라이브러리에 있어야 한다.
          JIT 실행 전에 ctypes.CDLL로 프로세스에 올리면,
          LLVM MCJIT가 심볼을 자동으로 찾을 수 있다.
    base_dir: 모듈 파일을 찾을 기본 디렉토리 (9.1c).
    """
    # 7.16a: extern fn이 참조하는 외부 라이브러리를 프로세스에 로드.
    # ctypes.CDLL(path, mode=RTLD_GLOBAL)로 열면 심볼이 전역으로 노출돼서
    # MCJIT가 resolve할 수 있음.
    _require_llvm_binding("JIT 실행(run)")
    _loaded_libs = []
    if libs:
        for lib_path in libs:
            try:
                dl = ctypes.CDLL(lib_path, mode=ctypes.RTLD_GLOBAL)
                _loaded_libs.append(dl)
            except OSError as e:
                raise DanhaRuntimeError(f"외부 라이브러리 로드 실패: {lib_path} — {e}")
    
    ast = parse(lex(source_code), source_code=source_code)
    module = compile_module(ast, base_dir=base_dir, source_code=source_code,
                            runtime_mode=runtime_mode, entry_arg_mode=False)
    
    # 우리가 만든 IR을 LLVM에 다시 파싱시켜 검증
    llvm_ir = str(module)
    
    # Performance: 부동소수점 연산에 fast-math 플래그 주입.
    llvm_ir = _inject_fast_math_flags(llvm_ir)

    mod_ref = llvm.parse_assembly(llvm_ir)
    mod_ref.verify()
    
    # JIT 컴파일러 만들고 메모리에 기계어 올리기
    target_machine = llvm.Target.from_default_triple().create_target_machine()
    
    # 7.10: LLVM 최적화 패스 적용
    # PipelineTuningOptions로 최적화 수준과 벡터화 옵션을 설정.
    # PassBuilder가 수준에 맞는 패스 세트를 자동으로 골라줌.
    # SLP 벡터화: 비슷한 스칼라 연산을 묶어 SIMD로. vec4 연산에 핵심.
    # 루프 벡터화: for 루프 안의 반복을 SIMD로 묶음. ECS system에서 중요.
    if opt_level > 0:
        pto = llvm.PipelineTuningOptions()
        pto.speed_level = opt_level
        pto.slp_vectorization = True
        pto.loop_vectorization = True
        pto.merge_functions = True   # 동일한 IR 함수를 병합해 코드 크기 + 캐시 효율 개선

        pb = llvm.create_pass_builder(target_machine, pto)
        mpm = pb.getModulePassManager()
        mpm.run(mod_ref, pb)

    engine = llvm.create_mcjit_compiler(mod_ref, target_machine)
    engine.finalize_object()
    engine.run_static_constructors()
    
    # main 함수의 메모리 주소를 받아서 ctypes로 호출
    main_ptr = engine.get_function_address("main")
    cfunc = ctypes.CFUNCTYPE(ctypes.c_int)(main_ptr)
    return cfunc()


# ===== 9.2a: AOT — LLVM IR → 오브젝트 파일 =====

def _emit_object_with_target(source_code, output_path, opt_level=3, base_dir=None,
                             target_triple=None, cpu=None, features=None,
                             reloc='default', codemodel='default',
                             runtime_mode='libc'):
    """
    Danha 소스를 컴파일하여 네이티브 오브젝트 파일(.o)을 생성.
    
    이게 AOT(Ahead-Of-Time) 컴파일의 핵심이야.
    JIT는 "그 자리에서 바로 실행"이고,
    AOT는 "파일로 저장해서 나중에 실행".
    
    source_code: Danha 소스 코드 문자열
    output_path: 저장할 .o 파일 경로
    opt_level: LLVM 최적화 수준 (0=없음, 2=표준)
    base_dir: 모듈 파일 검색 기본 디렉토리
    
    반환: @clink로 선언된 라이브러리 이름 목록 (링크 시 필요)
    """
    _require_llvm_binding("내장 오브젝트 생성(emit_object)")
    if target_triple is None:
        target = llvm.Target.from_default_triple()
    else:
        target = llvm.Target.from_triple(target_triple)

    try:
        if cpu is None and target_triple is None:
            cpu = llvm.get_host_cpu_name()
        elif cpu is None:
            cpu = ''
        if features is None and target_triple is None:
            host_feats = llvm.get_host_cpu_features()
            # AVX-512 계열 끄기 (AoS f64 회귀 회피). AVX2까지 활용.
            for k in list(host_feats.keys()):
                if 'avx512' in k or k == 'evex512':
                    host_feats[k] = False
            features = host_feats.flatten()
        elif features is None:
            features = ''
    except Exception:
        cpu = ''
        features = ''
    target_machine = target.create_target_machine(
        cpu=cpu,
        features=features,
        opt=opt_level,
        reloc=reloc,
        codemodel=codemodel
    )

    ast = parse(lex(source_code), source_code=source_code)
    
    # 9.1c: base_dir 설정. 캐시는 mtime 기반으로 자동 무효화 → clear 불필요.
    Compiler._MODULE_LOADING_COMPILE = set()
    Compiler._MODULE_BASE_DIR_COMPILE = base_dir

    c = Compiler(runtime_mode=runtime_mode, entry_arg_mode=(runtime_mode != 'direct-os'))
    c._source_code = source_code
    if target_triple is not None:
        c.module.triple = target_triple
        c.module.data_layout = str(target_machine.target_data)
    c.compile_program(ast)
    module = c.module
    clink_libs = c.clink_libs
    
    # LLVM IR 검증
    llvm_ir = str(module)

    # Performance: 부동소수점 연산에 fast-math 플래그 주입.
    # LLVM의 'fast' 플래그는 FMA 생성, reciprocal 최적화, reassociation을 허용.
    # 게임 엔진에서 IEEE 754 엄밀 준수보다 성능이 중요하므로 적합.
    # fcmp는 제외 — NaN 비교 동작 변경 시 제어흐름에 영향.
    llvm_ir = _inject_fast_math_flags(llvm_ir)

    mod_ref = llvm.parse_assembly(llvm_ir)
    if target_triple is not None:
        mod_ref.triple = target_triple
    mod_ref.verify()
    
    # 최적화 패스 적용
    if opt_level > 0:
        pto = llvm.PipelineTuningOptions()
        pto.speed_level = opt_level
        pto.slp_vectorization = True
        pto.loop_vectorization = True
        pto.merge_functions = True   # 동일한 IR 함수를 병합해 코드 크기 + 캐시 효율 개선

        pb = llvm.create_pass_builder(target_machine, pto)
        mpm = pb.getModulePassManager()
        mpm.run(mod_ref, pb)

    # 오브젝트 코드 생성 → 파일에 쓰기
    obj_code = target_machine.emit_object(mod_ref)
    with open(output_path, 'wb') as f:
        f.write(obj_code)
    
    return clink_libs


def emit_ll(source_code, output_path, base_dir=None, runtime_mode='libc',
            entry_arg_mode=None):
    """Danha 소스를 텍스트 LLVM IR(.ll)로 출력한다 — binding 불필요 (순수 파이썬).

    외부 clang이 이 .ll을 받아 오브젝트/실행 파일로 컴파일할 수 있다 (danhac과 동일 경로).
    반환: @clink로 선언된 라이브러리 이름 목록.
    """
    ast = parse(lex(source_code), source_code=source_code)
    Compiler._MODULE_LOADING_COMPILE = set()
    Compiler._MODULE_BASE_DIR_COMPILE = base_dir

    if entry_arg_mode is None:
        entry_arg_mode = (runtime_mode != 'direct-os')
    c = Compiler(runtime_mode=runtime_mode, entry_arg_mode=entry_arg_mode)
    c._source_code = source_code
    c.compile_program(ast)

    llvm_ir = _inject_fast_math_flags(str(c.module))
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(llvm_ir)
    return c.clink_libs


def _find_clang():
    """외부 clang 탐색 — .ll 입력은 clang만 가능 (gcc 불가)."""
    import shutil, os
    llvm_clang = os.path.expandvars(r'%LOCALAPPDATA%\LLVM\bin\clang.exe')
    if os.path.isfile(llvm_clang):
        return llvm_clang
    return shutil.which('clang')


def emit_object_via_clang(source_code, output_path, opt_level=3, base_dir=None,
                          runtime_mode='libc'):
    """외부 clang으로 오브젝트 파일 생성 — llvmlite 네이티브 바인딩 불필요.

    경로: Compiler(llvmlite.ir, 순수 파이썬) → 텍스트 .ll → `clang -c -O<n>`.
    danhac(셀프호스트 컴파일러)이 쓰는 것과 같은 외부 툴 경로다.
    최적화 파이프라인은 내장(binding) 경로의 PassBuilder 대신 clang -O<n>에 맡긴다.
    """
    import subprocess, os
    clang = _find_clang()
    if clang is None:
        raise DanhaRuntimeError(
            "외부 clang을 찾을 수 없어. LLVM(clang)을 설치하거나 PATH에 추가해줘 "
            "(권장: %LOCALAPPDATA%\\LLVM\\bin\\clang.exe)."
        )
    ll_path = os.path.splitext(output_path)[0] + '.ll'
    clink_libs = emit_ll(source_code, ll_path, base_dir=base_dir,
                         runtime_mode=runtime_mode)
    cmd = [clang, '-c', ll_path, '-o', output_path, f'-O{max(0, min(3, opt_level))}']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise DanhaRuntimeError(
            f"clang 오브젝트 생성 실패 (exit {result.returncode}):\n"
            f"  명령: {' '.join(cmd)}\n"
            f"  에러: {result.stderr.strip()}"
        )
    try:
        os.remove(ll_path)
    except OSError:
        pass
    return clink_libs


def emit_object(source_code, output_path, opt_level=3, base_dir=None, runtime_mode='libc',
                backend='auto'):
    """
    Danha 소스를 컴파일하여 네이티브 오브젝트 파일(.o)을 생성.

    이게 AOT(Ahead-Of-Time) 컴파일의 핵심이야.
    JIT는 "그 자리에서 바로 실행"이고,
    AOT는 "파일로 저장해서 나중에 실행".

    source_code: Danha 소스 코드 문자열
    output_path: 저장할 .o 파일 경로
    opt_level: LLVM 최적화 수준 (0=없음, 2=표준)
    base_dir: 모듈 파일 검색 기본 디렉토리
    backend: 'auto' | 'llvmlite' | 'clang'
      - auto: binding 있으면 내장(llvmlite), 없으면 외부 clang으로 폴백.
              환경변수 DANHA_LLVM_BACKEND=clang 으로 강제 가능.

    반환: @clink로 선언된 라이브러리 이름 목록 (링크 시 필요)
    """
    import os
    if backend == 'auto':
        backend = os.environ.get('DANHA_LLVM_BACKEND', '').strip().lower() or 'auto'
    if backend == 'auto':
        backend = 'llvmlite' if _HAS_LLVM_BINDING else 'clang'
    if backend == 'clang':
        return emit_object_via_clang(source_code, output_path, opt_level=opt_level,
                                     base_dir=base_dir, runtime_mode=runtime_mode)
    return _emit_object_with_target(source_code, output_path, opt_level=opt_level,
                                    base_dir=base_dir, runtime_mode=runtime_mode)


def emit_android_object(source_code, output_path, opt_level=2, base_dir=None, api=21):
    """Danha 소스를 Android ARM64 오브젝트 파일로 컴파일한다."""
    triple = f'aarch64-linux-android{api}'
    return _emit_object_with_target(
        source_code, output_path, opt_level=opt_level, base_dir=base_dir,
        target_triple=triple, cpu='generic', features='', reloc='pic'
    )


def emit_android_ir(source_code, output_path, base_dir=None, api=21):
    """Danha 소스를 Android ARM64 LLVM IR(.ll)로 출력한다.

    일부 llvmlite 배포판은 Android target object emission을 포함하지 않는다.
    이 경우 Android NDK clang이 이 IR을 받아 object로 컴파일할 수 있다.
    """
    ast = parse(lex(source_code), source_code=source_code)
    Compiler._MODULE_LOADING_COMPILE = set()
    Compiler._MODULE_BASE_DIR_COMPILE = base_dir

    c = Compiler()
    c._source_code = source_code
    c.module.triple = f'aarch64-linux-android{api}'
    c.compile_program(ast)

    llvm_ir = str(c.module)
    llvm_ir = _inject_fast_math_flags(llvm_ir)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(llvm_ir)
    return c.clink_libs


# ===== 9.2b: AOT — 오브젝트 파일 → 실행 파일 =====

def _find_gcc_windows():
    """Windows에서 C 컴파일러(clang 또는 MinGW gcc)를 찾는다.
    Danha .o는 LLVM 기본 트리플(x86_64-pc-windows-msvc)로 emit되어
    MSVC 스타일 __chkstk 호출을 포함한다. MinGW의 ld는 이 심볼을
    해석하지 못하므로 가능하면 clang/lld를 우선 사용한다.
    """
    import shutil, os, glob
    # 0순위: %LOCALAPPDATA%\LLVM\bin\clang.exe (Danha 권장 LLVM 설치)
    llvm_clang = os.path.expandvars(r'%LOCALAPPDATA%\LLVM\bin\clang.exe')
    if os.path.isfile(llvm_clang):
        return llvm_clang
    # 1순위: PATH에 있는 clang 또는 gcc
    for candidate in ['clang', 'gcc', 'cc']:
        found = shutil.which(candidate)
        if found:
            return found
    # 2순위: WinGet 설치 경로 (WinLibs, msys2 via winget)
    winget_base = os.path.expandvars(
        r'%LOCALAPPDATA%\Microsoft\WinGet\Packages'
    )
    if os.path.isdir(winget_base):
        for pattern in ['BrechtSanders.WinLibs*', 'msys2.msys2*']:
            for pkg_dir in glob.glob(os.path.join(winget_base, pattern)):
                for gcc_rel in [
                    r'mingw64\bin\gcc.exe',
                    r'ucrt64\bin\gcc.exe',
                    r'usr\bin\gcc.exe',
                ]:
                    gcc = os.path.join(pkg_dir, gcc_rel)
                    if os.path.isfile(gcc):
                        return gcc
    # 3순위: 직접 설치 경로
    for path in [
        r'C:\msys64\mingw64\bin\gcc.exe',
        r'C:\msys64\ucrt64\bin\gcc.exe',
        r'C:\mingw64\bin\gcc.exe',
        r'C:\mingw-w64\mingw64\bin\gcc.exe',
        r'C:\Program Files\mingw-w64\mingw64\bin\gcc.exe',
    ]:
        if os.path.isfile(path):
            return path
    return None


def _find_sdl2_windows():
    """Windows에서 SDL2 헤더·라이브러리 경로를 찾아 (cflags, lflags) 리스트 쌍을 반환한다.
    못 찾으면 ([], [])를 반환."""
    import shutil, subprocess, os, glob
    # 1순위: sdl2-config (PATH 또는 일반 설치 경로)
    sdl2_config = shutil.which('sdl2-config')
    if sdl2_config is None:
        # sdl2-config가 PATH 외부에 있을 수 있음 — 일반 위치 탐색
        desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
        for pattern in [
            os.path.join(desktop, '*', 'SDL2', '*-w64-mingw32', 'bin', 'sdl2-config'),
            os.path.join(desktop, '*', 'SDL2', '*', 'bin', 'sdl2-config'),
            os.path.join(desktop, 'SDL2', '*-w64-mingw32', 'bin', 'sdl2-config'),
            r'C:\msys64\mingw64\bin\sdl2-config',
            r'C:\msys64\ucrt64\bin\sdl2-config',
        ]:
            matches = glob.glob(pattern)
            if matches:
                sdl2_config = matches[0]
                break
    if sdl2_config:
        try:
            rc = subprocess.run([sdl2_config, '--cflags', '--libs'],
                                capture_output=True, text=True)
            if rc.returncode == 0:
                tokens = rc.stdout.strip().split()
                cflags = [t for t in tokens if t.startswith('-I') or t.startswith('-D')]
                lflags = [t for t in tokens if not t.startswith('-I') and not t.startswith('-D')]
                return cflags, lflags
        except Exception:
            pass
    # 2순위: 헤더 디렉토리 직접 탐색
    desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
    for pattern in [
        os.path.join(desktop, '*', 'SDL2', 'x86_64-w64-mingw32', 'include'),
        os.path.join(desktop, '*', 'SDL2', 'x86_64-w64-mingw32', 'include', 'SDL2'),
        os.path.join(desktop, 'SDL2', 'x86_64-w64-mingw32', 'include'),
        r'C:\SDL2-2.30.3\x86_64-w64-mingw32\include',
        r'C:\SDL2\x86_64-w64-mingw32\include',
        r'C:\SDL2-*\x86_64-w64-mingw32\include',
        r'C:\msys64\mingw64\include',
        r'C:\msys64\ucrt64\include',
    ]:
        for inc in glob.glob(pattern):
            # SDL2/SDL.h 존재 여부 확인
            sdl_h = os.path.join(inc, 'SDL2', 'SDL.h') if not inc.endswith('SDL2') else os.path.join(inc, 'SDL.h')
            if os.path.exists(sdl_h):
                # include 디렉토리는 SDL2/의 부모여야 함
                inc_dir = os.path.dirname(os.path.dirname(sdl_h))
                # lib 디렉토리 찾기
                lib_dir = os.path.join(os.path.dirname(inc_dir), 'lib')
                cflags = [f'-I{inc_dir}']
                lflags = [f'-L{lib_dir}', '-lSDL2', '-lSDL2main'] if os.path.isdir(lib_dir) else ['-lSDL2']
                return cflags, lflags
    return [], []


def _build_win32_obj(output_dir):
    """danha_win32.c를 컴파일하여 .o 파일을 반환한다. 이미 최신이면 재사용."""
    import subprocess, os, platform
    danha_dir = os.path.dirname(os.path.abspath(__file__))
    c_src = os.path.join(danha_dir, 'danha_win32.c')
    if not os.path.exists(c_src):
        raise DanhaNameError(
            f"danha_win32.c를 찾을 수 없어: {c_src}\n"
            "단아 설치 디렉토리에 danha_win32.c가 있어야 해."
        )
    obj_out = os.path.join(output_dir, 'danha_win32.o')
    # 소스보다 오브젝트가 더 최신이면 재사용
    if (os.path.exists(obj_out)
            and os.path.getmtime(obj_out) >= os.path.getmtime(c_src)):
        return obj_out

    gcc = _find_gcc_windows() if platform.system() == 'Windows' else None
    if gcc is None:
        import shutil
        gcc = shutil.which('gcc') or shutil.which('cc') or shutil.which('clang')
    if gcc is None:
        raise DanhaNameError("danha_win32.c를 컴파일할 gcc를 찾을 수 없어.")

    print(f"  C 빌드: danha_win32.c → {obj_out}")
    result = subprocess.run(
        [gcc, '-c', c_src, '-o', obj_out, '-O3', '-march=native', '-ffast-math', '-funroll-loops'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise DanhaRuntimeError(
            f"danha_win32.c 컴파일 실패:\n  {result.stderr.strip()}"
        )
    return obj_out


def _build_sdl2_obj(output_dir):
    """danha_sdl2.c를 컴파일하여 .o 파일을 반환한다. SDL2 헤더가 필요."""
    import subprocess, os, platform, shutil
    danha_dir = os.path.dirname(os.path.abspath(__file__))
    c_src = os.path.join(danha_dir, 'danha_sdl2.c')
    if not os.path.exists(c_src):
        raise DanhaNameError(
            f"danha_sdl2.c를 찾을 수 없어: {c_src}\n"
            "단아 설치 디렉토리에 danha_sdl2.c가 있어야 해."
        )
    obj_out = os.path.join(output_dir, 'danha_sdl2.o')
    if (os.path.exists(obj_out)
            and os.path.getmtime(obj_out) >= os.path.getmtime(c_src)):
        return obj_out

    gcc = _find_gcc_windows() if platform.system() == 'Windows' else None
    if gcc is None:
        gcc = shutil.which('gcc') or shutil.which('cc') or shutil.which('clang')
    if gcc is None:
        raise DanhaNameError("danha_sdl2.c를 컴파일할 gcc를 찾을 수 없어.")

    # sdl2-config로 cflags 가져오기 (없으면 기본 경로 시도)
    cflags = []
    sdl2_config = shutil.which('sdl2-config')
    if sdl2_config:
        r = subprocess.run([sdl2_config, '--cflags'], capture_output=True, text=True)
        if r.returncode == 0:
            cflags = r.stdout.strip().split()

    print(f"  C 빌드: danha_sdl2.c → {obj_out}")
    result = subprocess.run(
        [gcc, '-c', c_src, '-o', obj_out, '-O3', '-march=native', '-ffast-math', '-funroll-loops'] + cflags,
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise DanhaRuntimeError(
            f"danha_sdl2.c 컴파일 실패:\n  {result.stderr.strip()}\n"
            "SDL2 개발 라이브러리가 설치되어 있는지 확인해:"
            " (Linux: apt install libsdl2-dev, macOS: brew install sdl2)"
        )
    return obj_out


def _build_gl_obj(output_dir):
    """danha_gl.c를 컴파일하여 .o 파일을 반환한다. SDL2 + OpenGL 헤더가 필요."""
    import subprocess, os, platform, shutil
    danha_dir = os.path.dirname(os.path.abspath(__file__))
    c_src = os.path.join(danha_dir, 'danha_gl.c')
    if not os.path.exists(c_src):
        raise DanhaNameError(
            f"danha_gl.c를 찾을 수 없어: {c_src}\n"
            "단아 설치 디렉토리에 danha_gl.c가 있어야 해."
        )
    obj_out = os.path.join(output_dir, 'danha_gl.o')
    if (os.path.exists(obj_out)
            and os.path.getmtime(obj_out) >= os.path.getmtime(c_src)):
        return obj_out

    gcc = _find_gcc_windows() if platform.system() == 'Windows' else None
    if gcc is None:
        gcc = shutil.which('gcc') or shutil.which('cc') or shutil.which('clang')
    if gcc is None:
        raise DanhaNameError("danha_gl.c를 컴파일할 gcc를 찾을 수 없어.")

    # Stage 69: sdl2-config가 PATH에 없으면 _find_sdl2_paths로 헤더 위치 탐색.
    cflags = []
    sdl2_config = shutil.which('sdl2-config')
    if sdl2_config:
        r = subprocess.run([sdl2_config, '--cflags'], capture_output=True, text=True)
        if r.returncode == 0:
            cflags = r.stdout.strip().split()
    if not cflags:
        try:
            cf, _ = _find_sdl2_windows()
            cflags = cf
        except Exception:
            pass

    print(f"  C 빌드: danha_gl.c → {obj_out}")
    result = subprocess.run(
        [gcc, '-c', c_src, '-o', obj_out, '-O3', '-march=native', '-ffast-math', '-funroll-loops'] + cflags,
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise DanhaRuntimeError(
            f"danha_gl.c 컴파일 실패:\n  {result.stderr.strip()}\n"
            "SDL2 + OpenGL 개발 라이브러리가 설치되어 있는지 확인해."
        )
    return obj_out


def _build_text_obj(output_dir):
    """danha_text.c를 컴파일하여 .o 파일을 반환. stb_truetype.h + SDL2 + OpenGL 헤더 필요. (Stage 85)"""
    import subprocess, os, platform, shutil
    danha_dir = os.path.dirname(os.path.abspath(__file__))
    c_src = os.path.join(danha_dir, 'danha_text.c')
    stb_h = os.path.join(danha_dir, 'stb_truetype.h')
    if not os.path.exists(c_src):
        raise DanhaNameError(f"danha_text.c를 찾을 수 없어: {c_src}")
    if not os.path.exists(stb_h):
        raise DanhaNameError(f"stb_truetype.h를 찾을 수 없어: {stb_h}")
    obj_out = os.path.join(output_dir, 'danha_text.o')
    if (os.path.exists(obj_out)
            and os.path.getmtime(obj_out) >= os.path.getmtime(c_src)
            and os.path.getmtime(obj_out) >= os.path.getmtime(stb_h)):
        return obj_out

    gcc = _find_gcc_windows() if platform.system() == 'Windows' else None
    if gcc is None:
        gcc = shutil.which('gcc') or shutil.which('cc') or shutil.which('clang')
    if gcc is None:
        raise DanhaNameError("danha_text.c를 컴파일할 gcc를 찾을 수 없어.")

    cflags = []
    sdl2_config = shutil.which('sdl2-config')
    if sdl2_config:
        r = subprocess.run([sdl2_config, '--cflags'], capture_output=True, text=True)
        if r.returncode == 0:
            cflags = r.stdout.strip().split()
    if not cflags:
        try:
            cf, _ = _find_sdl2_windows()
            cflags = cf
        except Exception:
            pass

    # stb_truetype.h 경로 추가
    cflags = cflags + ['-I', danha_dir]

    print(f"  C 빌드: danha_text.c → {obj_out}")
    result = subprocess.run(
        [gcc, '-c', c_src, '-o', obj_out, '-O3', '-march=native', '-ffast-math', '-funroll-loops', '-Wno-implicit-fallthrough'] + cflags,
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise DanhaRuntimeError(
            f"danha_text.c 컴파일 실패:\n  {result.stderr.strip()}\n"
            "SDL2 + OpenGL + stb_truetype.h 가 있는지 확인해."
        )
    return obj_out


def _build_audio_obj(output_dir):
    """danha_audio.c를 컴파일하여 .o 파일을 반환한다. SDL2 헤더 필요. (Stage 80)"""
    import subprocess, os, platform, shutil
    danha_dir = os.path.dirname(os.path.abspath(__file__))
    c_src = os.path.join(danha_dir, 'danha_audio.c')
    if not os.path.exists(c_src):
        raise DanhaNameError(
            f"danha_audio.c를 찾을 수 없어: {c_src}\n"
            "단아 설치 디렉토리에 danha_audio.c가 있어야 해."
        )
    obj_out = os.path.join(output_dir, 'danha_audio.o')
    if (os.path.exists(obj_out)
            and os.path.getmtime(obj_out) >= os.path.getmtime(c_src)):
        return obj_out

    gcc = _find_gcc_windows() if platform.system() == 'Windows' else None
    if gcc is None:
        gcc = shutil.which('gcc') or shutil.which('cc') or shutil.which('clang')
    if gcc is None:
        raise DanhaNameError("danha_audio.c를 컴파일할 gcc를 찾을 수 없어.")

    cflags = []
    sdl2_config = shutil.which('sdl2-config')
    if sdl2_config:
        r = subprocess.run([sdl2_config, '--cflags'], capture_output=True, text=True)
        if r.returncode == 0:
            cflags = r.stdout.strip().split()
    if not cflags:
        try:
            cf, _ = _find_sdl2_windows()
            cflags = cf
        except Exception:
            pass

    print(f"  C 빌드: danha_audio.c → {obj_out}")
    result = subprocess.run(
        [gcc, '-c', c_src, '-o', obj_out, '-O3', '-march=native', '-ffast-math', '-funroll-loops'] + cflags,
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise DanhaRuntimeError(
            f"danha_audio.c 컴파일 실패:\n  {result.stderr.strip()}\n"
            "SDL2 개발 라이브러리가 설치되어 있는지 확인해."
        )
    return obj_out


def _build_ari_gfx_obj(src_dir):
    """ari_gfx.c를 컴파일하여 .o 파일을 반환한다. src_dir → CWD → gfx/ 서브디렉터리 순으로 찾는다."""
    import subprocess, os, platform, shutil
    c_src = None
    cwd = os.getcwd()
    search_dirs = [src_dir, cwd,
                   os.path.join(src_dir, 'gfx'), os.path.join(cwd, 'gfx'),
                   os.path.join(src_dir, '..'), os.path.join(cwd, '..')]
    for search in filter(None, search_dirs):
        candidate = os.path.join(search, 'ari_gfx.c')
        if os.path.exists(candidate):
            c_src = os.path.abspath(candidate)
            break
    if c_src is None:
        raise DanhaNameError(
            f"ari_gfx.c를 찾을 수 없어: {os.path.join(src_dir, 'ari_gfx.c')}\n"
            "danha compile 실행 위치(또는 소스 파일 위치)에 ari_gfx.c가 있어야 해."
        )
    obj_out = os.path.join(os.path.dirname(c_src), 'ari_gfx.o')
    if (os.path.exists(obj_out)
            and os.path.getmtime(obj_out) >= os.path.getmtime(c_src)):
        return obj_out

    gcc = _find_gcc_windows() if platform.system() == 'Windows' else None
    if gcc is None:
        gcc = shutil.which('gcc') or shutil.which('cc') or shutil.which('clang')
    if gcc is None:
        raise DanhaNameError("ari_gfx.c를 컴파일할 gcc를 찾을 수 없어.")

    cflags, _ = _find_sdl2_windows() if platform.system() == 'Windows' else ([], [])
    if not cflags:
        sdl2_config = shutil.which('sdl2-config')
        if sdl2_config:
            r = subprocess.run([sdl2_config, '--cflags'], capture_output=True, text=True)
            if r.returncode == 0:
                cflags = r.stdout.strip().split()

    print(f"  C 빌드: ari_gfx.c → {obj_out}")
    # -mno-stack-arg-probe: GCC 16+가 __chkstk를 호출하면 MinGW libgcc에 없어서 링크 실패.
    #                      대신 ___chkstk_ms를 쓰는 MinGW식 프로브로 폴백.
    extra_flags = ['-mno-stack-arg-probe'] if platform.system() == 'Windows' else []
    result = subprocess.run(
        [gcc, '-c', c_src, '-o', obj_out,
         '-O3', '-march=native', '-ffast-math', '-fno-math-errno',
         '-fno-trapping-math', '-fomit-frame-pointer', '-funroll-loops'] + extra_flags + cflags,
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise DanhaRuntimeError(
            f"ari_gfx.c 컴파일 실패:\n  {result.stderr.strip()}\n"
            "SDL2 + OpenGL 개발 라이브러리가 설치되어 있는지 확인해."
        )
    return obj_out


def _build_ari_audio_obj(src_dir):
    """ari_audio.c를 컴파일하여 .o 파일을 반환한다. src_dir → CWD → audio/ 서브디렉터리 순으로 찾는다."""
    import subprocess, os, platform, shutil
    c_src = None
    cwd = os.getcwd()
    search_dirs = [src_dir, cwd,
                   os.path.join(src_dir, 'audio'), os.path.join(cwd, 'audio'),
                   os.path.join(src_dir, '..'), os.path.join(cwd, '..')]
    for search in filter(None, search_dirs):
        candidate = os.path.join(search, 'ari_audio.c')
        if os.path.exists(candidate):
            c_src = os.path.abspath(candidate)
            break
    if c_src is None:
        raise DanhaNameError(
            f"ari_audio.c를 찾을 수 없어: {os.path.join(src_dir, 'ari_audio.c')}\n"
            "danha compile 실행 위치(또는 소스 파일 위치)에 ari_audio.c가 있어야 해."
        )
    obj_out = os.path.join(os.path.dirname(c_src), 'ari_audio.o')
    if (os.path.exists(obj_out)
            and os.path.getmtime(obj_out) >= os.path.getmtime(c_src)):
        return obj_out

    gcc = _find_gcc_windows() if platform.system() == 'Windows' else None
    if gcc is None:
        gcc = shutil.which('gcc') or shutil.which('cc') or shutil.which('clang')
    if gcc is None:
        raise DanhaNameError("ari_audio.c를 컴파일할 gcc를 찾을 수 없어.")

    print(f"  C 빌드: ari_audio.c → {obj_out}")
    result = subprocess.run(
        [gcc, '-c', c_src, '-o', obj_out, '-O3', '-march=native', '-ffast-math', '-funroll-loops'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise DanhaRuntimeError(
            f"ari_audio.c 컴파일 실패:\n  {result.stderr.strip()}\n"
            "miniaudio.h가 ari_audio.c와 같은 디렉토리에 있어야 해."
        )
    return obj_out


def _build_ari_net_obj(src_dir):
    """ari_net.c를 컴파일하여 .o 파일을 반환한다. (UDP networking)"""
    import subprocess, os, platform, shutil
    c_src = None
    cwd = os.getcwd()
    search_dirs = [src_dir, cwd,
                   os.path.join(src_dir, 'audio'), os.path.join(cwd, 'audio'),
                   os.path.join(src_dir, 'net'), os.path.join(cwd, 'net'),
                   os.path.join(src_dir, '..'), os.path.join(cwd, '..')]
    for search in filter(None, search_dirs):
        candidate = os.path.join(search, 'ari_net.c')
        if os.path.exists(candidate):
            c_src = os.path.abspath(candidate)
            break
    if c_src is None:
        raise DanhaNameError(
            f"ari_net.c를 찾을 수 없어: {os.path.join(src_dir, 'ari_net.c')}"
        )
    obj_out = os.path.join(os.path.dirname(c_src), 'ari_net.o')
    if (os.path.exists(obj_out)
            and os.path.getmtime(obj_out) >= os.path.getmtime(c_src)):
        return obj_out
    gcc = _find_gcc_windows() if platform.system() == 'Windows' else None
    if gcc is None:
        gcc = shutil.which('gcc') or shutil.which('cc') or shutil.which('clang')
    if gcc is None:
        raise DanhaNameError("ari_net.c를 컴파일할 gcc를 찾을 수 없어.")
    print(f"  C 빌드: ari_net.c → {obj_out}")
    result = subprocess.run(
        [gcc, '-c', c_src, '-o', obj_out, '-O3', '-march=native', '-ffast-math', '-funroll-loops'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise DanhaRuntimeError(
            f"ari_net.c 컴파일 실패:\n  {result.stderr.strip()}"
        )
    return obj_out


def _build_ari_scene_obj(src_dir):
    """ari_scene.c를 컴파일하여 .o 파일을 반환한다. src_dir → CWD → scene/ 서브디렉터리 순으로 찾는다."""
    import subprocess, os, platform, shutil
    c_src = None
    cwd = os.getcwd()
    search_dirs = [src_dir, cwd,
                   os.path.join(src_dir, 'scene'), os.path.join(cwd, 'scene'),
                   os.path.join(src_dir, '..'), os.path.join(cwd, '..')]
    for search in filter(None, search_dirs):
        candidate = os.path.join(search, 'ari_scene.c')
        if os.path.exists(candidate):
            c_src = os.path.abspath(candidate)
            break
    if c_src is None:
        raise DanhaNameError(
            f"ari_scene.c를 찾을 수 없어: {os.path.join(src_dir, 'ari_scene.c')}\n"
            "danha compile 실행 위치(또는 소스 파일 위치)에 ari_scene.c가 있어야 해."
        )
    obj_out = os.path.join(os.path.dirname(c_src), 'ari_scene.o')
    if (os.path.exists(obj_out)
            and os.path.getmtime(obj_out) >= os.path.getmtime(c_src)):
        return obj_out

    gcc = _find_gcc_windows() if platform.system() == 'Windows' else None
    if gcc is None:
        gcc = shutil.which('gcc') or shutil.which('cc') or shutil.which('clang')
    if gcc is None:
        raise DanhaNameError("ari_scene.c를 컴파일할 gcc를 찾을 수 없어.")

    print(f"  C 빌드: ari_scene.c → {obj_out}")
    result = subprocess.run(
        [gcc, '-c', c_src, '-o', obj_out, '-O3', '-march=native', '-ffast-math', '-funroll-loops'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise DanhaRuntimeError(
            f"ari_scene.c 컴파일 실패:\n  {result.stderr.strip()}"
        )
    return obj_out


def _build_ari_grid_obj(src_dir):
    """ari_grid.c를 컴파일하여 .o 파일을 반환한다. src_dir → CWD → core/ 서브디렉터리 순으로 찾는다."""
    import subprocess, os, platform, shutil
    c_src = None
    cwd = os.getcwd()
    search_dirs = [src_dir, cwd,
                   os.path.join(src_dir, 'core'), os.path.join(cwd, 'core'),
                   os.path.join(src_dir, '..'), os.path.join(cwd, '..')]
    for search in filter(None, search_dirs):
        candidate = os.path.join(search, 'ari_grid.c')
        if os.path.exists(candidate):
            c_src = os.path.abspath(candidate)
            break
    if c_src is None:
        raise DanhaNameError(
            f"ari_grid.c를 찾을 수 없어: {os.path.join(src_dir, 'ari_grid.c')}\n"
            "danha compile 실행 위치(또는 소스 파일 위치)에 ari_grid.c가 있어야 해."
        )
    obj_out = os.path.join(os.path.dirname(c_src), 'ari_grid.o')
    if (os.path.exists(obj_out)
            and os.path.getmtime(obj_out) >= os.path.getmtime(c_src)):
        return obj_out

    gcc = 'x86_64-w64-mingw32-gcc' if platform.system() == 'Windows' else None
    if gcc is None:
        gcc = shutil.which('gcc') or shutil.which('cc') or shutil.which('clang')
    if gcc is None:
        raise DanhaNameError("ari_grid.c를 컴파일할 gcc를 찾을 수 없어.")

    print(f"  C 빌드: ari_grid.c → {obj_out}")
    result = subprocess.run(
        [gcc, '-c', c_src, '-o', obj_out, '-O3', '-march=native', '-ffast-math', '-funroll-loops'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise DanhaRuntimeError(
            f"ari_grid.c 컴파일 실패:\n  {result.stderr.strip()}"
        )
    return obj_out


def _build_ari_soa_obj(src_dir):
    """ari_soa.c를 컴파일하여 .o 파일을 반환한다."""
    import subprocess, os, platform, shutil
    c_src = None
    cwd = os.getcwd()
    search_dirs = [src_dir, cwd,
                   os.path.join(src_dir, 'core'), os.path.join(cwd, 'core'),
                   os.path.join(src_dir, '..'), os.path.join(cwd, '..')]
    for search in filter(None, search_dirs):
        candidate = os.path.join(search, 'ari_soa.c')
        if os.path.exists(candidate):
            c_src = os.path.abspath(candidate)
            break
    if c_src is None:
        raise DanhaNameError(
            f"ari_soa.c를 찾을 수 없어: {os.path.join(src_dir, 'ari_soa.c')}"
        )
    obj_out = os.path.join(os.path.dirname(c_src), 'ari_soa.o')
    if (os.path.exists(obj_out)
            and os.path.getmtime(obj_out) >= os.path.getmtime(c_src)):
        return obj_out
    gcc = 'x86_64-w64-mingw32-gcc' if platform.system() == 'Windows' else None
    if gcc is None:
        gcc = shutil.which('gcc') or shutil.which('cc') or shutil.which('clang')
    if gcc is None:
        raise DanhaNameError("ari_soa.c를 컴파일할 gcc를 찾을 수 없어.")
    print(f"  C 빌드: ari_soa.c → {obj_out}")
    result = subprocess.run(
        [gcc, '-c', c_src, '-o', obj_out, '-O3', '-march=native', '-ffast-math', '-funroll-loops'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise DanhaRuntimeError(f"ari_soa.c 컴파일 실패:\n  {result.stderr.strip()}")
    return obj_out


def link_executable(obj_path, output_path, extra_libs=None, extra_objs=None, runtime_mode='libc'):
    """
    .o 파일을 링크하여 실행 파일을 만든다.

    내부적으로 gcc 또는 clang을 호출.
    C 런타임(printf, malloc 등)은 자동으로 링크됨.

    obj_path: .o 파일 경로
    output_path: 출력 실행 파일 경로 (예: 'game' 또는 'game.exe')
    extra_libs: 추가 링크 라이브러리 (예: ['SDL2', 'pthread'])
    """
    import subprocess
    import shutil
    import platform
    import os

    is_windows = platform.system() == 'Windows'

    if runtime_mode == 'direct-os' and is_windows and not extra_libs and not extra_objs:
        if os.environ.get('DANHA_EXTERNAL_LINKER', '').lower() not in ('1', 'true', 'yes'):
            try:
                from danha_pe_linker import link_direct_os
                return link_direct_os(obj_path, output_path)
            except Exception as e:
                if os.environ.get('DANHA_LINKER_STRICT', '').lower() in ('1', 'true', 'yes'):
                    raise DanhaRuntimeError(f"Danha PE 링커 실패: {e}")
                print(f"  Danha PE 링커 폴백: {e}")

    # gcc 또는 clang 찾기 — Windows는 MinGW 자동 탐색 포함
    if is_windows:
        cc = _find_gcc_windows()
    else:
        cc = None
        for candidate in ['cc', 'gcc', 'clang']:
            if shutil.which(candidate):
                cc = candidate
                break

    if cc is None:
        if is_windows:
            raise DanhaNameError(
                "MinGW gcc를 찾을 수 없어. 아래 중 하나를 설치해줘:\n"
                "  winget install BrechtSanders.WinLibs.POSIX.UCRT  (권장)\n"
                "  또는 https://www.msys2.org/ 에서 MSYS2 설치 후 pacman -S mingw-w64-ucrt-x86_64-gcc"
            )
        else:
            raise DanhaNameError(
                "C 컴파일러(gcc 또는 clang)를 찾을 수 없어. "
                "설치 후 PATH에 추가해줘.\n"
                "  Ubuntu/Debian: sudo apt install gcc\n"
                "  macOS: xcode-select --install"
            )
    
    # 링커 명령 구성
    # Windows(MinGW): -no-pie와 -lm 불필요 (에러 유발).
    # Linux: -no-pie 필요 (PIE 모드와 LLVM 오브젝트 충돌 방지).
    cmd = [cc, obj_path, '-o', output_path, '-L.']

    if runtime_mode == 'direct-os':
        if not is_windows:
            raise DanhaRuntimeError("direct-os 런타임의 첫 구현은 Windows AOT만 지원해.")
        cc_name = os.path.basename(cc).lower()
        if cc_name.startswith('clang'):
            cmd.extend(['-nostdlib', '-Wl,/entry:main', '-lkernel32'])
        else:
            cmd.extend(['-nostdlib', '-Wl,-e,main', '-lkernel32'])

    if runtime_mode != 'direct-os' and is_windows and os.path.basename(cc).lower().startswith('clang'):
        # clang/lld-link on Windows: provide sprintf/scanf as real symbols.
        # 현대 MSVC stdio.h는 sprintf를 inline으로만 정의하므로 별도 임포트 라이브러리가 필요.
        cmd.append('-llegacy_stdio_definitions')

    if runtime_mode != 'direct-os' and is_windows:
        # GCC 16+ MinGW는 큰 스택 프레임에서 MSVC식 __chkstk 호출. 그러나 libgcc는 ___chkstk_ms만 제공.
        # ld의 --defsym 옵션으로 alias 추가 — 큰 SoA struct(예: ParticleBig 1024)도 작동.
        cmd.append('-Wl,--defsym=__chkstk=___chkstk_ms')

    if runtime_mode != 'direct-os' and not is_windows:
        cmd.append('-lm')       # 수학 라이브러리 (Linux/macOS)
        cmd.append('-no-pie')   # PIE 비활성화 (Linux)
    
    if extra_objs:
        cmd.extend(extra_objs)

    # Stage 69: SDL2 의존 시 라이브러리 경로 자동 추가 (Windows). cmd에 -lSDL2가 있는지 검사 후.
    sdl2_libs_needed = bool(extra_libs and any(
        (l == 'SDL2') or (isinstance(l, str) and l.endswith('SDL2'))
        for l in extra_libs
    ))
    if is_windows and sdl2_libs_needed:
        try:
            _, sdl_lflags = _find_sdl2_windows()
            for lf in sdl_lflags:
                if lf.startswith('-L') and lf not in cmd:
                    cmd.append(lf)
        except Exception:
            pass
        # SDL2 MinGW lib (.a)은 clang/lld-link (MSVC ABI)와 호환 안 됨.
        # 링커를 MinGW ld로 강제: clang에 -fuse-ld=lld 대신 -fuse-ld=ld 사용 안 됨 → MinGW gcc로 교체.
        if os.path.basename(cc).lower().startswith('clang'):
            mingw_gcc = None
            for p in [
                os.path.expandvars(r'%USERPROFILE%\AppData\Local\Microsoft\WinGet\Packages\BrechtSanders.WinLibs.POSIX.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe\mingw64\bin\gcc.exe'),
                r'C:\msys64\mingw64\bin\gcc.exe',
                r'C:\msys64\ucrt64\bin\gcc.exe',
            ]:
                if os.path.exists(p):
                    mingw_gcc = p
                    break
            if mingw_gcc is None:
                mingw_gcc = shutil.which('gcc')
            if mingw_gcc:
                cmd[0] = mingw_gcc
                # legacy_stdio_definitions는 clang 전용 — gcc에선 제거
                cmd = [c for c in cmd if c != '-llegacy_stdio_definitions']

    if extra_libs:
        for lib in extra_libs:
            if lib.startswith('-'):
                cmd.append(lib)
            else:
                cmd.append(f'-l{lib}')

    # 링크 실행
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        raise DanhaRuntimeError(
            f"링크 실패 (exit {result.returncode}):\n"
            f"  명령: {' '.join(cmd)}\n"
            f"  에러: {result.stderr}"
        )
    
    return output_path


# ===== 9.2c: AOT — 통합 빌드 (소스 → exe 한 번에) =====

def build(source_path, output_path=None, opt_level=3, extra_libs=None, keep_obj=False,
          runtime_mode='libc', backend='auto'):
    """
    Danha 소스 파일을 읽어서 실행 파일을 만든다.
    
    이 함수 하나로 .dh → exe 전 과정을 처리.
    
    source_path: .dh 소스 파일 경로
    output_path: 출력 실행 파일 경로 (기본: 소스 이름에서 .dh 뺀 것)
    opt_level: LLVM 최적화 수준
    extra_libs: 추가 링크 라이브러리
    keep_obj: True면 중간 .o 파일을 삭제하지 않음
    
    반환: 생성된 실행 파일 경로
    """
    import os
    
    # 소스 파일 읽기
    if not os.path.exists(source_path):
        raise DanhaNameError(f"소스 파일을 찾을 수 없어: {source_path}")
    
    with open(source_path, 'r', encoding='utf-8') as f:
        source_code = f.read()
    
    # 출력 경로 결정
    base_dir = os.path.dirname(os.path.abspath(source_path))
    base_name = os.path.splitext(os.path.basename(source_path))[0]
    
    if output_path is None:
        output_path = os.path.join(base_dir, base_name)
        # Windows에서는 .exe 확장자 자동 추가
        import platform
        if platform.system() == 'Windows' and not output_path.endswith('.exe'):
            output_path += '.exe'
    
    obj_path = output_path + '.o'

    # 1단계: 컴파일 (.dh → .o)
    print(f"  컴파일: {source_path} → {obj_path}")
    clink_libs = emit_object(source_code, obj_path, opt_level=opt_level, base_dir=base_dir,
                             runtime_mode=runtime_mode, backend=backend)

    # 2단계: 링크 (.o → exe)
    all_libs = list(clink_libs or [])
    if extra_libs:
        all_libs.extend(extra_libs)

    # danha_win32 자동 빌드: @clink("danha_win32") 감지 시 C 백엔드를 그 자리에서 컴파일해서 링크
    extra_objs = []
    if 'danha_win32' in all_libs:
        all_libs = [x for x in all_libs if x != 'danha_win32']
        win32_obj = _build_win32_obj(base_dir)
        extra_objs.append(win32_obj)
        # Win32 API 라이브러리 자동 링크
        for win_lib in ['gdi32', 'user32']:
            if win_lib not in all_libs:
                all_libs.append(win_lib)

    # 44단계: danha_sdl2 자동 빌드: @clink("danha_sdl2") 감지 시 SDL2 백엔드 컴파일 + 링크
    if 'danha_sdl2' in all_libs:
        import shutil, subprocess, platform
        all_libs = [x for x in all_libs if x != 'danha_sdl2']
        sdl2_obj = _build_sdl2_obj(base_dir)
        extra_objs.append(sdl2_obj)
        # SDL2 링크 플래그 (sdl2-config --libs 또는 기본값)
        sdl2_config = shutil.which('sdl2-config')
        if sdl2_config:
            r = subprocess.run([sdl2_config, '--libs'], capture_output=True, text=True)
            if r.returncode == 0:
                for token in r.stdout.strip().split():
                    if token.startswith('-l'):
                        lib = token[2:]
                        if lib not in all_libs:
                            all_libs.append(lib)
        else:
            if 'SDL2' not in all_libs:
                all_libs.append('SDL2')

    # 55단계: danha_gl 자동 빌드: @clink("danha_gl") 감지 시 OpenGL 백엔드 컴파일 + 링크
    if 'danha_gl' in all_libs:
        import shutil, subprocess, platform as _platform
        all_libs = [x for x in all_libs if x != 'danha_gl']
        gl_obj = _build_gl_obj(base_dir)
        extra_objs.append(gl_obj)
        # SDL2 + OpenGL 링크
        sdl2_config = shutil.which('sdl2-config')
        if sdl2_config:
            r = subprocess.run([sdl2_config, '--libs'], capture_output=True, text=True)
            if r.returncode == 0:
                for token in r.stdout.strip().split():
                    if token.startswith('-l'):
                        lib = token[2:]
                        if lib not in all_libs:
                            all_libs.append(lib)
        else:
            if 'SDL2' not in all_libs:
                all_libs.append('SDL2')
        _sys = _platform.system()
        if _sys == 'Windows':
            if 'opengl32' not in all_libs:
                all_libs.append('opengl32')
        elif _sys == 'Darwin':
            if '-framework' not in all_libs:
                all_libs.extend(['-framework', 'OpenGL'])
        else:
            if 'GL' not in all_libs:
                all_libs.append('GL')

    # Stage 85: danha_text 자동 빌드. SDL2 + OpenGL + stb_truetype. 보통 danha_gl과 같이.
    if 'danha_text' in all_libs:
        import platform as _plat_t
        all_libs = [x for x in all_libs if x != 'danha_text']
        text_obj = _build_text_obj(base_dir)
        extra_objs.append(text_obj)
        # SDL2 + OpenGL은 보통 danha_gl이 이미 추가하지만, text 단독 import 시 대비.
        if _plat_t.system() == 'Windows':
            _, _sdl2_lflags = _find_sdl2_windows()
            if _sdl2_lflags:
                for _tok in _sdl2_lflags:
                    if _tok not in all_libs:
                        all_libs.append(_tok)
            if 'opengl32' not in all_libs:
                all_libs.append('opengl32')
        else:
            if 'SDL2' not in all_libs:
                all_libs.append('SDL2')
            if 'GL' not in all_libs:
                all_libs.append('GL')

    # Stage 80: danha_audio 자동 빌드 (@clink("danha_audio") 감지). SDL2 native audio.
    if 'danha_audio' in all_libs:
        import shutil as _shutil_a, subprocess as _sp_a, platform as _plat_a
        all_libs = [x for x in all_libs if x != 'danha_audio']
        audio_obj = _build_audio_obj(base_dir)
        extra_objs.append(audio_obj)
        # SDL2 링크 추가 (sdl2-config 또는 Windows path)
        _added_sdl2 = False
        _sdl2cfg_a = _shutil_a.which('sdl2-config')
        if _sdl2cfg_a:
            _r_a = _sp_a.run([_sdl2cfg_a, '--libs'], capture_output=True, text=True)
            if _r_a.returncode == 0:
                for _tok in _r_a.stdout.strip().split():
                    if _tok.startswith('-l'):
                        _lib = _tok[2:]
                        if _lib not in all_libs:
                            all_libs.append(_lib)
                _added_sdl2 = True
        if not _added_sdl2:
            if _plat_a.system() == 'Windows':
                _, _sdl2_lflags = _find_sdl2_windows()
                if _sdl2_lflags:
                    for _tok in _sdl2_lflags:
                        if _tok not in all_libs:
                            all_libs.append(_tok)
                else:
                    if 'SDL2' not in all_libs:
                        all_libs.append('SDL2')
            else:
                if 'SDL2' not in all_libs:
                    all_libs.append('SDL2')

    # Ari 엔진: ari_gfx 자동 빌드 (@clink("ari_gfx") 감지)
    if 'ari_gfx' in all_libs:
        import shutil as _shutil2, subprocess as _sp2, platform as _plat2
        all_libs = [x for x in all_libs if x != 'ari_gfx']
        ari_gfx_obj = _build_ari_gfx_obj(base_dir)
        extra_objs.append(ari_gfx_obj)
        try:
            gl_obj = _build_gl_obj(base_dir)
            if gl_obj not in extra_objs:
                extra_objs.append(gl_obj)
        except Exception:
            # Older 2D-only Ari copies can still link without the Danha GL helper.
            pass
        _sys2 = _plat2.system()
        if _sys2 == 'Windows':
            _, _sdl2_lflags = _find_sdl2_windows()
            if _sdl2_lflags:
                for _tok in _sdl2_lflags:
                    if _tok not in all_libs:
                        all_libs.append(_tok)
            else:
                if 'SDL2' not in all_libs:
                    all_libs.append('SDL2')
            if 'opengl32' not in all_libs:
                all_libs.append('opengl32')
            if 'ws2_32' not in all_libs:
                all_libs.append('ws2_32')
        else:
            _sdl2cfg2 = _shutil2.which('sdl2-config')
            if _sdl2cfg2:
                _r2 = _sp2.run([_sdl2cfg2, '--libs'], capture_output=True, text=True)
                if _r2.returncode == 0:
                    for _tok in _r2.stdout.strip().split():
                        if _tok not in all_libs:
                            all_libs.append(_tok)
            else:
                if 'SDL2' not in all_libs:
                    all_libs.append('SDL2')
            if _sys2 == 'Darwin':
                if '-framework' not in all_libs:
                    all_libs.extend(['-framework', 'OpenGL'])
            else:
                if 'GL' not in all_libs:
                    all_libs.append('GL')

    # Ari 엔진: ari_audio 자동 빌드 (@clink("ari_audio") 감지)
    if 'ari_audio' in all_libs:
        import platform as _plat3
        all_libs = [x for x in all_libs if x != 'ari_audio']
        ari_audio_obj = _build_ari_audio_obj(base_dir)
        extra_objs.append(ari_audio_obj)
        _sys3 = _plat3.system()
        if _sys3 == 'Windows':
            if 'winmm' not in all_libs:
                all_libs.append('winmm')
        elif _sys3 != 'Darwin':
            for _alib in ['pthread', 'dl', 'm']:
                if _alib not in all_libs:
                    all_libs.append(_alib)

    # Ari 엔진: ari_scene 자동 빌드 (@clink("ari_scene") 감지)
    if 'ari_scene' in all_libs:
        all_libs = [x for x in all_libs if x != 'ari_scene']
        ari_scene_obj = _build_ari_scene_obj(base_dir)
        extra_objs.append(ari_scene_obj)

    # Ari 엔진: ari_net 자동 빌드 (@clink("ari_net") — UDP networking)
    if 'ari_net' in all_libs:
        import platform as _plat_net
        all_libs = [x for x in all_libs if x != 'ari_net']
        ari_net_obj = _build_ari_net_obj(base_dir)
        extra_objs.append(ari_net_obj)
        if _plat_net.system() == 'Windows':
            if 'ws2_32' not in all_libs:
                all_libs.append('ws2_32')

    # Ari 엔진: ari_grid 자동 빌드 (@clink("ari_grid") 감지)
    if 'ari_grid' in all_libs:
        all_libs = [x for x in all_libs if x != 'ari_grid']
        ari_grid_obj = _build_ari_grid_obj(base_dir)
        extra_objs.append(ari_grid_obj)

    # Ari 엔진: ari_soa 자동 빌드 (@clink("ari_soa") 감지)
    if 'ari_soa' in all_libs:
        all_libs = [x for x in all_libs if x != 'ari_soa']
        ari_soa_obj = _build_ari_soa_obj(base_dir)
        extra_objs.append(ari_soa_obj)

    print(f"  링크:   {obj_path} → {output_path}")
    link_executable(obj_path, output_path, extra_libs=all_libs if all_libs else None,
                    extra_objs=extra_objs if extra_objs else None,
                    runtime_mode=runtime_mode)

    # 중간 파일 정리
    if not keep_obj and os.path.exists(obj_path):
        os.remove(obj_path)

    # Windows: SDL2.dll 자동 복사 (ari_gfx 사용 시)
    if any('ari_gfx' in str(o) for o in extra_objs):
        import platform as _plat_dll, shutil as _shutil_dll
        if _plat_dll.system() == 'Windows':
            out_dir = os.path.dirname(os.path.abspath(output_path))
            dll_dest = os.path.join(out_dir, 'SDL2.dll')
            if not os.path.exists(dll_dest):
                # SDL2.dll 탐색: gfx/ → Ari 루트 → SDL2 설치 경로
                dll_src = None
                for _sd in [base_dir,
                             os.path.join(base_dir, '..'),
                             os.path.join(base_dir, '..', 'gfx'),
                             os.getcwd()]:
                    _cand = os.path.join(_sd, 'SDL2.dll')
                    if os.path.exists(_cand):
                        dll_src = os.path.abspath(_cand)
                        break
                if dll_src is None:
                    _, _lf = _find_sdl2_windows()
                    for _tok in _lf:
                        if _tok.startswith('-L'):
                            _cand = os.path.join(_tok[2:], '..', 'bin', 'SDL2.dll')
                            if os.path.exists(_cand):
                                dll_src = os.path.abspath(_cand)
                                break
                if dll_src:
                    _shutil_dll.copy2(dll_src, dll_dest)
                    print(f"  DLL 복사: SDL2.dll → {out_dir}")

    obj_size = os.path.getsize(output_path)
    print(f"  완료!   {output_path} ({obj_size:,} bytes)")

    return output_path


# ===== 54단계: WebAssembly 타겟 =====

def build_wasm(source_path, output_path=None):
    """
    Danha 소스를 WebAssembly(.wasm)로 컴파일.

    전략:
    1. LLVM IR 생성 (wasm32-unknown-wasi 트리플)
    2. wasm-ld 또는 emcc가 있으면 .wasm 링크
    3. 없으면 .wat (LLVM IR 텍스트) + .bc (비트코드) 출력
    """
    import os, shutil, subprocess

    if not os.path.exists(source_path):
        raise DanhaNameError(f"소스 파일을 찾을 수 없어: {source_path}")

    with open(source_path, 'r', encoding='utf-8') as f:
        source_code = f.read()

    base_dir = os.path.dirname(os.path.abspath(source_path))
    base_name = os.path.splitext(os.path.basename(source_path))[0]

    if output_path is None:
        output_path = os.path.join(base_dir, base_name + '.wasm')

    ir_path = output_path.replace('.wasm', '.ll')
    bc_path = output_path.replace('.wasm', '.bc')

    # LLVM IR 생성 (wasm32 타겟 트리플 사용)
    print(f"  WASM 컴파일: {source_path}")
    compiler = DanhaCompiler()
    compiler._source_code = source_code
    compiler._base_dir = base_dir
    ast = compiler._parse(source_code)
    compiler.compile_program(ast)

    # wasm32 타겟 설정
    wasm_triple = 'wasm32-unknown-wasi'
    try:
        target = llvm.Target.from_triple(wasm_triple)
        target_machine = target.create_target_machine(
            triple=wasm_triple,
            features='',
            opt=2,
        )
        llvm_module = llvm.parse_assembly(str(compiler.module))
        llvm_module.triple = wasm_triple

        # 비트코드 출력
        bc_data = target_machine.emit_object(llvm_module)
        with open(bc_path, 'wb') as f:
            f.write(bc_data)

        # wasm-ld 또는 emcc로 링크 시도
        wasm_ld = shutil.which('wasm-ld')
        emcc = shutil.which('emcc')

        if wasm_ld:
            print(f"  wasm-ld 링크: {bc_path} → {output_path}")
            result = subprocess.run(
                [wasm_ld, '--no-entry', '--export-all', '-o', output_path, bc_path],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print(f"  완료! {output_path}")
                return output_path
            else:
                print(f"  wasm-ld 실패: {result.stderr.strip()}")
        elif emcc:
            print(f"  emcc 링크: {bc_path} → {output_path}")
            result = subprocess.run(
                [emcc, bc_path, '-o', output_path],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print(f"  완료! {output_path}")
                return output_path
            else:
                print(f"  emcc 실패: {result.stderr.strip()}")

        # 링커 없으면 IR 텍스트 저장
        with open(ir_path, 'w', encoding='utf-8') as f:
            f.write(str(compiler.module))
        print(f"  LLVM IR 저장: {ir_path}")
        print(f"  .wasm 생성을 위해 wasm-ld 또는 emcc를 설치해줘:")
        print(f"    wasm-ld {ir_path} --no-entry --export-all -o {output_path}")
        return ir_path

    except Exception as e:
        # WASM 백엔드 미지원 시 IR 텍스트만 저장
        with open(ir_path, 'w', encoding='utf-8') as f:
            f.write(str(compiler.module))
        print(f"  WASM 백엔드 불가 ({e}), IR 저장: {ir_path}")
        return ir_path


if __name__ == '__main__':
    # 첫 시연: print(1 + 2 * 3) → 7
    print("=== Danha 컴파일러 첫 시연 ===")
    print("소스: print(1 + 2 * 3)")
    print("결과:")
    run_native("print(1 + 2 * 3)")
