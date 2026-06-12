# danha_debugger.py
# 53단계: 단아 디버거 — `danha debug <파일.dh>`
#
# 사용법:
#   danha debug file.dh
#   - 실행 시작 전 첫 줄에서 멈춤
#   - (dnh) 프롬프트에서 명령어 입력
#
# 명령어:
#   n / next / step   — 한 문장 실행
#   c / continue      — 다음 브레이크포인트까지 실행
#   b <줄>            — 브레이크포인트 추가
#   d <줄>            — 브레이크포인트 삭제
#   p <변수>          — 변수 값 출력
#   l / list          — 현재 위치 주변 소스 출력
#   bt / backtrace    — 콜 스택 (미구현)
#   q / quit          — 종료

import os
import sys


def run_debug(filepath):
    """디버그 모드로 파일 실행."""
    if not os.path.exists(filepath):
        print(f"❌ 파일을 찾을 수 없어: {filepath}")
        return 1

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()
    except Exception as e:
        print(f"❌ 파일 읽기 실패: {e}")
        return 1

    import danha_evaluator as ev

    # 디버거 모드 활성화
    ev._DEBUG_MODE = True
    ev._DEBUG_STEP[0] = True   # 첫 문장부터 멈춤
    ev._DEBUG_BREAKPOINTS = set()

    base_dir = os.path.dirname(os.path.abspath(filepath))

    print(f"[단아 디버거] {filepath}")
    print("  n(ext) — 단계 실행  |  c(ontinue) — 계속  |  b <줄> — 브레이크  |  p <변수> — 출력  |  q — 종료")
    print()

    try:
        ev.run(source, base_dir=base_dir)
        print("\n[디버거] 실행 완료")
    except KeyboardInterrupt:
        print("\n[디버거] 사용자가 종료했어")
    except Exception as e:
        print(f"\n[디버거] 오류 발생: {e}")
        return 1
    finally:
        ev._DEBUG_MODE = False
        ev._DEBUG_STEP[0] = False
        ev._DEBUG_BREAKPOINTS = set()

    return 0
