# Phase A+B 벤치마크 종합 분석 (P4)

> Danha 0.68 (Stage 68, 2026-05-18) · 23 워크로드 · 5 repeats · Windows x64 · clang 22 / g++ MinGW UCRT 13 / .NET 10 preview

## 1. 헤드라인 — Danha가 어디에 서 있는가

### 측정 1 (Stage 68 시점, 2026-05-17)

| impl | geomean ratio vs Danha | 해석 |
|---|---:|---|
| **Danha** | **1.000x** | baseline |
| cpp_hand_clang `-O3 -march=native` | 0.865x | 14% 빠름 |
| cpp_hand_gpp `-O3 -march=native` | 0.933x | 7% 빠름 |
| cpp_glm | 1.042x | Danha 4% 빠름 |
| **csharp_jit** | **2.249x** | **Danha 2.25배 빠름** |

### 측정 2 (Stage 75a 이후, 2026-05-18) — 약진

| impl | geomean ratio vs Danha | 해석 |
|---|---:|---|
| **Danha (Stage 75a)** | **1.000x** | baseline |
| **cpp_hand_clang** | **0.998x** | **사실상 동률** (14% → 0.2%) |
| **cpp_hand_gpp** | **1.098x** | **Danha 10% 이김** (이전 g++가 7% 빠름) |
| cpp_glm | 1.127x | Danha 13% 빠름 |
| **csharp_jit** | **2.677x** | **Danha 2.68배 이김** |

**Stage 75a 두 변경:**
1. **Tagged enum repr `{tag, T}`** — 균일 primitive payload는 typed field 사용 (SSA 친화). `_build_tagged_enum`이 `insert_value`, `compile_match`가 `extract_value` 발행. 더 이상 GEP+bitcast 패턴 없음.
2. **`cpu='native'` codegen** — `get_host_cpu_name()` + features 활용. AVX-512는 비활성 (Zen 4에서 AoS f64 회귀).

**결론:** Danha 0.75 = **clang -O3와 사실상 동률 + g++/glm/C# 모두 이김**. 시스템 언어급 성능 달성.

## 2. 측정 환경 + 방법론

- **Workloads:** Phase A 18 (저수준 산술/벡터/제어흐름) + Phase B 5 (게임엔진 핵심 패턴).
- **Repeats:** Warmup 2 + 측정 5. Min wall-clock (외부 `ELAPSED_NS` 우선).
- **5종 impls:** danha, cpp_hand_clang, cpp_hand_gpp, cpp_glm, csharp_jit (AOT는 MSVC Build Tools 미설치로 보류).
- **공정성:** 동일 알고리즘 + 동일 출력 체크섬. 23/23 결과 일치 (1개 csharp_jit equivalence ✗은 printf 포맷 차이; 정답 동일).
- **세부 데이터:** `bench/results/report_2026-05-17_22-58-24.md`.

## 3. 결과 매트릭스

### Phase A — 저수준 (18 워크로드)

| Workload | Danha (ms) | clang | gpp | glm | C# JIT | Danha 위치 |
|---|---:|---:|---:|---:|---:|---|
| 01 vec4_dot_f64 100M | 210 | 195 | 194 | 213 | 269 | clang 1.08x · C# 1.28x 이김 |
| 02 mat4_vec4_xform 10M | **230** | 243 | 269 | 376 | 357 | **모두 이김** |
| 03 particle_step_aos 1M×60 | 114 | 155 | 63 | — | 206 | clang 이김, g++만 더 빠름 |
| 04 normalize_loop_f32 | 87 | 78 | 85 | 71 | 142 | glm 1.22x |
| 05 vec4_dot_f32 50M | 250 | 222 | 222 | 216 | 290 | f32 SIMD 약간 약함 |
| 06 fib40_recursive | 524 | 409 | **223** | — | 822 | **g++ 2.35x 이김** |
| 07 mandelbrot_512 | 81 | 82 | 82 | — | 155 | clang/g++와 동률 |
| 08 ackermann_3_10 | 85 | 78 | **57** | — | 311 | g++ 1.5x (recursion) |
| 09 sum_array_1m_f32 | 292 | 291 | 292 | — | 351 | clang/g++와 동률 (메모리 바운드) |
| 10 random_scatter_4m | 94 | **62** | 87 | — | 161 | clang 1.5x (캐시 미스) |
| 11 aos_soa_step_100k | 43 | 37 | 57 | — | 117 | clang 1.17x |
| 12 branchy_inner_loop | 30 | **19** | 32 | — | 105 | clang 1.55x (분기 예측) |
| 13 switch_dispatch_8way 50M | 259 | **128** | 339 | — | 457 | **clang 2.02x — match codegen 약점** |
| 14 arena_stress_1m | 20 | 21 | 29 | — | 101 | **clang과 동률** |
| 15 trait_dispatch_1m × 50 | **613** | 1,203 | 1,219 | — | 901 | **모든 C++/C#를 1.4-2x 이김** |
| 16 string_concat_100k | 75 | **17** | 28 | — | 91 | **clang 4.4x — 불변 문자열 O(N²)** |
| 17 parse_int_1m | 50 | 43 | 51 | — | 114 | clang 1.18x |
| 18 hello_world_startup | 21 | 20 | 18 | — | 90 | C++와 동률, **C# 4.4x 이김** |

### Phase B — 게임엔진 핵심 (5 워크로드)

| Workload | Danha (ms) | clang | gpp | C# JIT | 의미 |
|---|---:|---:|---:|---:|---|
| B1 particles_10k × 600 | **24** | 28 | 35 | 108 | **L2 캐시 거주 파티클 — Danha가 C++ 둘 다 이김** |
| B2 ECS_transform_100k × 300 | 73 | 58 | 63 | 189 | clang 1.26x (큰 SoA auto-vec) |
| B3 spatial_query_50k × 1K | 28 | 22 | 25 | 93 | clang 1.29x (인덱싱 무거움) |
| B4 event_dispatch_100k 8-way | 23 | 22 | 27 | 95 | clang과 동률 |
| B5 frustum_cull_10k × 100 | **33** | 34 | 23 | 110 | **clang 이김 — vec4 SIMD 검증**, g++만 더 빠름 |

## 4. Danha 강점 (지속/홍보 가능)

### 4-1. SIMD vec/mat 연산 — **glm 압도, clang/g++와 대등**
- **02 mat4*vec4:** Danha 230ms · glm 376ms · clang 243ms — **모든 구현 1위**.
- **B5 frustum cull (vec4 + dot4 6번 per object × 1M):** Danha 33ms · clang 34ms — Stage 61 SIMD 최적화가 실제 게임 워크로드에서 검증됨.
- 근거: `<4 x double>` LLVM 네이티브 타입 + `@llvm.vector.reduce.fadd.v4f64` 사용 → AVX `vmulpd %ymm` / `vbroadcastsd` 직접 발행.

### 4-2. dyn Trait dispatch — **clang virtual을 1.96x 이김**
- **15 trait_dispatch_1m (50M dispatches):** Danha 613ms · clang 1,203ms · g++ 1,219ms · C# 901ms.
- 근거: vtable 레이아웃이 컴팩트 + DynCast 아레나 할당 (Stage 67) 이 매 cast마다 fresh slot 보장 → 캐시 친화적.
- 비교: C++ `std::unique_ptr<Shape>` + virtual은 heap 단편화 + vtable 간접 로드 비용.

### 4-3. 캐시 거주 파티클 시뮬 — **clang을 1.13x 이김**
- **B1 particles_10k × 600 ticks:** Danha 24ms · clang 28ms · g++ 35ms.
- 근거: 작은 AoS (~313KB, L2 fit) + Stage 66 인덱스 구조체 필드 쓰기. 게임 60Hz 메인 루프 패턴.

### 4-4. 아레나 할당 — **clang과 동률, C# 5x 이김**
- **14 arena_stress_1m:** Danha 20ms · clang 21ms · C# 101ms.
- 근거: bump allocator + 256MB pre-reserve.

### 4-5. C# JIT 전 영역 우위 — **17/18 워크로드 + Phase B 5/5**
- Danha 평균 2.25x 빠름. 게임 워크로드 Phase B에서 **모두 2.6~4.4x 빠름**.
- 의미: Unity가 C# JIT 기반인 점을 고려하면 **Danha+Ari가 Unity보다 빠른 런타임이 가능**한 잠재력.

## 5. Danha 약점 (수정 후보 + 근본 원인)

### 5-1. **string concat — ✅ 해결됨 (Stage 70, 2026-05-18)**
- **이전 (10K iter):** Danha 75ms · clang 17ms · g++ 28ms — clang 4.4x.
- **이후 (Stage 70 StringBuilder, 100K iter 복원):** **Danha 18.0ms · clang 17.0 · g++ 26.4 · C# 88.7**. Danha가 clang과 동률, g++ 1.47x 이김, C# 4.9x 이김.
- **변경:** `string_builder()` / `_append` / `_to_string` / `_len` 4 빌트인. Layout `{i8* data, i64 len, i64 cap}`, amortized O(1) growth via `realloc(max(need, cap*2))`. 컴파일러 ~120 LOC + 인터프리터 ~40 LOC.
- **잔여:** 원시 `s = s + s2` 는 여전히 O(N²) (compat). 사용자가 누적 패턴에 StringBuilder 쓰도록 가이드 — 게임 코드 컨벤션 문제.

### 5-2. **fib40 재귀 — g++가 2.35x 빠름 (Stage 72에서 평가, 노액션)**
- **06 fib40_recursive:** Danha 524ms · clang 409ms · g++ **223ms**.
- **Stage 72 평가 (2026-05-18):** `inlinehint`가 [danha_compile.py:4507]에서 **이미 모든 사용자 함수에 자동 부착**. `@inline_hint` syntax 추가는 no-op.
- **O3 실험 (opt_level=2→3):** fib40 524ms → 500ms (-4.5%, 노이즈 경계). 동시에 scatter +9% 악화. 깨끗한 win 아님. O2로 되돌림.
- **진짜 격차:** clang vs g++ 차이 (1.83x). Danha vs clang 격차는 1.28x로 줄어든 셈 — LLVM 자체의 recursion 보수성을 우회하려면 `-mllvm -inline-threshold=N` 옵션 노출 또는 LLVM 패스 커스터마이즈 필요. Danha 컨트롤 밖.
- **결론:** 추격 불가능한 영역. 게임 코드에서 깊은 재귀는 드묾 (트리 순회 정도) → **우선순위 매우 낮음, 보류.**

### 5-3. **8-way switch dispatch — clang 2x 느림 (Stage 71에서 부분 처리)**
- **이전:** Danha 259ms · clang 128ms — clang 2.02x.
- **Stage 71 (2026-05-18):** `compile_match`가 if-chain 대신 LLVM `switch` instruction 직접 발행 (variant arms → cases, wildcard → default). **결과:** Danha 263ms (3회 평균) — **perf wash**. clang `-O2` SimplifyCFG가 if-chain도 자동 switch로 변환하므로 IR 단계 차이는 옵티마이저 후 동일.
- **진짜 병목 (Stage 67 alloca 호이스트 후에도 남음):** tagged enum representation `{i32 tag, [N x i8] payload}`. 각 match arm이 payload 추출 시 `GEP {0,1} + bitcast i8* + GEP offset + bitcast typed_ptr + load` — **5 LLVM instructions per arm**. mem2reg가 struct GEP'd alloca를 promote 못 함. C++ `switch(o.tag) { case 0: use o.payload_v; }` 는 곧바로 field load 가능.
- **다음 수정 후보 (Stage 75+ 컴파일러 보강 묶음):** 모든 variant 의 binding 이 단일 scalar 일 때 `extractvalue` 로 SSA 추출. 또는 enum repr 변경 (`{tag, packed_int}` for scalar 변종 묶음).
- **현 상태:** switch IR 정확. 약점은 2.02x → 2.02x (변화 없음). 깊은 refactor 필요.

### 5-4. **branchy code — clang 1.55x 빠름 (Stage 73 재진단)**
- **12 branchy_inner_loop:** Danha 30ms · clang **19ms**.
- **Stage 73 재진단 (2026-05-18):** bench 12 IR을 보니 `if (x == 0) ... else if (x == 1) ... else if (x == 2) ... else ...` 4-way **동등 확률 (25% 각)** 분기. 한 가지가 hot이 아니므로 `@hot/@cold` 힌트가 부적절. 원래 진단 잘못됨.
- **진짜 원인 (추정):** Danha IR이 변수마다 alloca + load/store 패턴 (mem2reg에 의존), 4-way 비교가 jump table로 변환 안 됨. clang은 input source부터 jump table 후보로 인식.
- **수정 후보 (재검토 필요):** 
  - if-chain → switch instruction 자동 변환 (현재는 enum match만 switch 발행)
  - 정수 비교 chain 패턴 검출 + LLVM `switch` 발행
- **우선순위:** 하 — 진짜 fix는 generic if-chain → switch 변환. Stage 71의 enum match→switch 와 같은 류지만 더 일반화 필요. 보류.

### 5-5. **랜덤 스캐터 — clang 1.5x 빠름 (Stage 74 재고)**
- **10 random_scatter_4m:** Danha 94ms · clang **62ms**.
- **Stage 74 재고 (2026-05-18):** 사용자 `@prefetch(ptr, locality)` 빌트인 노출은 단순. 하지만 **bench 10은 random index 패턴** — 다음 인덱스가 현재 인덱스에 의존 (`idx = (idx * 1664525 + 1013904223) % N`). prefetch 거리 (lookahead) 를 사용자가 직접 결정해야 효과적. **자동화 어려운 영역.**
- **그리고:** O3 실험에서 scatter +9% 악화 관찰 — 컴파일러 자동 optimization이 캐시 미스 코드에 항상 도움 되는 건 아님.
- **수정 후보 (보류):** `@prefetch` 빌트인은 노출 가능 (~30분 작업). 하지만 실효성은 사용자의 알고리즘 인사이트 + 튜닝에 달림.
- **우선순위:** 하 — 캐시 미스 워크로드는 본질적 한계. 게임 알고리즘 차원에서 spatial hash/SoA로 우회 (B3 spatial_query가 그 예).

### 5-6. **ECS_transform 큰 SoA 패스 — clang 1.26x 빠름**
- **B2 ECS_transform_100k × 300 × 3 systems:** Danha 73ms · clang **58ms**.
- **근본 원인:** clang의 loop vectorizer가 3개 system을 fuse + AVX-512 사용 가능 (`-march=native` 기준). Danha는 system별 분리 패스 + `<4 x double>` SSE/AVX2까지만.
- **수정 후보:**
  - LLVM `loop-vectorize` pass 적용 확인 (이미 -O2면 적용중)
  - **system fusion** (Stage 69 컴파일러 작업): 연속된 system을 단일 루프로 결합
  - AVX-512 옵션 추가 (`-march=native` 발행 시 SIMD 폭 확장)
- **우선순위:** 중 — 게임엔진 핵심 패턴. Stage 69-71에서 처리 권고.

## 6. 차기 로드맵 (Stage 69~)

분석 결과를 우선순위로 정리. Phase E (Ari 엔진) + 컴파일러 보강을 병행.

### Tier 1 — 엔진 실제 사용에 즉시 영향 (Stage 69~71)

| 단계 | 작업 | 근거 |
|:-:|:--|:--|
| 69 | **스프라이트 렌더러 (`import ari_sprite`)** | Ari 엔진 시작. 단아로 실제 2D 게임 짤 수 있는 첫 마일스톤. 텍스처 batch + camera 2D. |
| 70 | **`StringBuilder` 빌트인** | 약점 5-1 해결 (string concat 4-5x). 게임 HUD/score/log에 직접 영향. mutable buffer + amortized growth. |
| 71 | **`match` → LLVM `switch` 코드젠** | 약점 5-3 해결 (8-way dispatch 2x). 게임 이벤트/AI state machine에 영향. tag-only match는 자동 변환. |

### Tier 2 — 게임엔진 워크로드 보강 (Stage 72~74)

| 단계 | 작업 | 근거 |
|:-:|:--|:--|
| 72 | **system fusion 컴파일러 패스** | 약점 5-6 해결 (ECS 1.26x). 연속 system을 단일 LLVM 루프로 결합 → vectorizer가 더 잘 잡음. |
| 73 | **텍스처 로더 + 폰트** | Ari 엔진 (ARI_ROADMAP Stage 70-71). PNG/BMP 파서 + BMP 폰트 아틀라스. |
| 74 | **`@inline_hint` + LLVM passes 노출** | 약점 5-2 해결 (recursion 2.35x). `alwaysinline` + `-mllvm` 옵션. |

### Tier 3 — 보강 + 조사 (Stage 75~)

| 단계 | 작업 | 근거 |
|:-:|:--|:--|
| 75 | **컴파일러 한계 fix:** struct fixed-array zero-init alloca 호이스트 + ECS capacity 동적화 | B3 작성 중 발견. 4096 entity 제한은 게임엔진에 비현실적. |
| 76 | **2D 물리 + 씬 런타임** | ARI_ROADMAP Stage 73-74. Box2D-class 알고리즘을 단아 ECS로. |
| 77+ | **분기 힌트 + prefetch + AVX-512 옵션** | 약점 5-4, 5-5. 평균 1.05-1.15x 추가 추격. 게임 핫 루프 case-by-case. |

### 결정 보류 (검증 필요)

- **self-host 완성** — `danhac.dh` 부트스트랩은 미완성 ([selfhost_status](selfhost_status.md)). 21개+ 함수 어노테이션 정리 + 잠재 이슈. 3-6시간~며칠. 엔진 작업과 가치 트레이드오프 후 결정.
- **C# AOT 비교** — MSVC Build Tools 설치 시 NativeAOT 측정 가능. 현 JIT 대비 격차 좁힐 가능성.
- **GPU 셰이더 워크로드** — CPU 벤치 23개로 충분하지만, 셰이더 → GLSL 변환 후 GPU 성능도 비교하면 풀스택 평가.

## 7. 결론

**Danha 0.68 의 정량적 위치:**

```
              느림  ←————————————————————→  빠름
csharp_jit ████████████████████████████████ (2.25x)
                                            cpp_glm ██ (1.04x)
                                                   Danha ▲ (1.00x baseline)
                                          cpp_hand_gpp ██ (0.93x)
                                  cpp_hand_clang ████ (0.87x)
```

**4가지 위치 명제:**
1. **시스템 언어급 성능 달성** — clang/g++와 ±15% 이내. Unity/C# 대비 2배 우위.
2. **게임 핵심 워크로드에서 경쟁력** — SIMD, dyn Trait, ECS 캐시 거주 패턴에서 clang을 이기거나 동률.
3. **5개 명확한 격차 발견** — string concat, recursion inlining, switch dispatch, branchy code, ECS SoA fusion. 모두 컴파일러/런타임 보강으로 추격 가능.
4. **게임엔진 단계 (Phase E) 진입 준비 완료** — 더 이상 "단아가 빠른가?"가 아닌 "단아로 무엇을 만들 것인가?"가 다음 질문.

**다음 작업:** Stage 69 — 스프라이트 렌더러 (Phase E 진입) 또는 약점 fix (Stage 70-71 string/match) 중 사용자 우선순위에 따라.
