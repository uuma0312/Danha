# danha_errors.py
# Danha 언어의 에러 타입 체계
#
# 모든 Danha 에러는 DanhaError를 상속한다.
# 각 에러 종류에 따라 다른 메시지 접두사를 붙여서 사용자에게 보여준다.
# line과 source를 받아서 에러 위치를 표시할 수 있다.

class DanhaError(Exception):
    """모든 Danha 에러의 기본 클래스"""
    def __init__(self, message, line=None, source=None):
        self.message = message
        self.line = line
        self.source = source
        
        # 줄 번호와 소스가 있으면 위치 정보를 메시지에 추가
        full_msg = self._format_message()
        super().__init__(full_msg)
    
    def _error_kind(self):
        return "에러"
    
    def _format_message(self):
        parts = [f"[{self._error_kind()}] {self.message}"]
        
        if self.line is not None:
            parts.append(f"  → {self.line}번째 줄")
            
            # 소스에서 해당 줄을 찾아서 보여주기
            if self.source:
                lines = self.source.split('\n')
                if 1 <= self.line <= len(lines):
                    source_line = lines[self.line - 1]
                    if source_line.strip():
                        parts.append(f"  | {source_line}")
        
        return '\n'.join(parts)


class DanhaSyntaxError(DanhaError):
    """문법 에러 — 코드 형태가 잘못됨"""
    def _error_kind(self):
        return "문법 에러"


class DanhaTypeError(DanhaError):
    """타입 에러 — 타입이 안 맞음"""
    def _error_kind(self):
        return "타입 에러"


class DanhaNameError(DanhaError):
    """이름 에러 — 정의 안 된 이름 사용"""
    def _error_kind(self):
        return "이름 에러"


class DanhaValueError(DanhaError):
    """값 에러 — 값이 유효하지 않음"""
    def _error_kind(self):
        return "값 에러"


class DanhaRuntimeError(DanhaError):
    """런타임 에러 — 실행 중 문제 발생"""
    def _error_kind(self):
        return "런타임 에러"


class DanhaECSError(DanhaError):
    """ECS 에러 — 엔티티/컴포넌트/시스템 관련 문제"""
    def _error_kind(self):
        return "ECS 에러"


class DanhaImportError(DanhaError):
    """임포트 에러 — 모듈 로딩 실패"""
    def _error_kind(self):
        return "임포트 에러"


class DanhaComptimeError(DanhaError):
    """컴파일 타임 에러 — comptime 블록 실행 중 문제"""
    def _error_kind(self):
        return "comptime 에러"
