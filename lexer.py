# lexer.py
# Danha 언어의 렉서
#
# 모든 토큰은 (종류, 값, 줄번호) 세 원소짜리 튜플이다.
# 줄 번호는 1부터 시작 (사람이 읽기 편하게).

from danha_errors import DanhaSyntaxError

KEYWORDS = {
    'print': 'PRINT',
    'if': 'IF',
    'else': 'ELSE',
    'while': 'WHILE',
    'for': 'FOR',
    'in': 'IN',
    'fn': 'FN',
    'return': 'RETURN',
    'and': 'AND',
    'or': 'OR',
    'not': 'NOT',
    'struct': 'STRUCT',
    'component': 'COMPONENT',
    'system': 'SYSTEM',
    'parallel': 'PARALLEL',
    'trait': 'TRAIT',
    'impl': 'IMPL',
    'self': 'SELF',
    'true': 'TRUE',
    'false': 'FALSE',
    'null': 'NULL',
    'dyn': 'DYN',
    'mut': 'MUT',
    'const': 'CONST',
    'enum': 'ENUM',
    'break': 'BREAK',
    'continue': 'CONTINUE',
    'extern': 'EXTERN',
    'match': 'MATCH',
    'import': 'IMPORT',
    'from': 'FROM',
    'as': 'AS',
    'defer': 'DEFER',
    'comptime': 'COMPTIME',
    'unsafe': 'UNSAFE',
    'macro': 'MACRO',
    'export': 'EXPORT',
    'union': 'UNION',
}


def lex(source_code):
    tokens = []
    i = 0
    line = 1
    # L-2 (2026-05-19): 괄호 안에서는 NEWLINE 토큰을 안 emit해서
    # 함수 시그니처/struct 리터럴/긴 호출을 여러 줄에 쓸 수 있게 함.
    # `(`와 `[`만 카운트 — `{`는 블록 경계라 줄바꿈이 유의미.
    paren_depth = 0

    while i < len(source_code):
        char = source_code[i]

        if char == ' ' or char == '\t':
            i += 1
            continue
        
        # 주석: '#' 또는 '//' 부터 줄 끝까지 무시.
        # 토큰을 만들지 않고 그냥 먹어치운다. 줄바꿈은 먹지 않아서
        # 다음 반복에서 NEWLINE 토큰이 정상적으로 만들어진다.
        if char == '#':
            while i < len(source_code) and source_code[i] != '\n':
                i += 1
            continue
        
        if char == '/' and i + 1 < len(source_code) and source_code[i + 1] == '/':
            # '///' doc comment — emit DOC_COMMENT token; '//' regular — skip
            if i + 2 < len(source_code) and source_code[i + 2] == '/':
                i += 3
                doc_text = ''
                while i < len(source_code) and source_code[i] != '\n':
                    doc_text += source_code[i]
                    i += 1
                tokens.append(('DOC_COMMENT', doc_text.strip(), line))
            else:
                while i < len(source_code) and source_code[i] != '\n':
                    i += 1
            continue
        
        if char == '\n':
            # L-2: 괄호 안에서는 NEWLINE을 emit하지 않음 — multi-line 시그니처/리터럴 허용.
            if paren_depth == 0:
                tokens.append(('NEWLINE', '\n', line))
            line += 1
            i += 1
            continue


        if char.isdigit():
            start_line = line
            # 16진수 리터럴: 0x... 또는 0X...
            if char == '0' and i + 1 < len(source_code) and source_code[i + 1] in ('x', 'X'):
                i += 2  # '0x' 소비
                hex_str = ''
                while i < len(source_code) and source_code[i] in '0123456789abcdefABCDEF_':
                    if source_code[i] != '_':
                        hex_str += source_code[i]
                    i += 1
                if not hex_str:
                    raise DanhaSyntaxError("0x 다음에 16진수 숫자가 있어야 해", line=start_line, source=source_code)
                tokens.append(('NUMBER', int(hex_str, 16), start_line))
                continue
            number_str = ''
            while i < len(source_code) and source_code[i].isdigit():
                number_str += source_code[i]
                i += 1

            # 점 뒤에 숫자가 있으면 실수로 계속 읽는다.
            # 점 뒤에 숫자가 없으면 (예: '3.' 또는 '3.to_string()')
            # 점은 건드리지 않고 정수로 끝낸다. 점은 나중에 DOT으로 처리됨.
            if (i < len(source_code) and source_code[i] == '.'
                and i + 1 < len(source_code) and source_code[i + 1].isdigit()):
                number_str += '.'
                i += 1
                while i < len(source_code) and source_code[i].isdigit():
                    number_str += source_code[i]
                    i += 1
                tokens.append(('NUMBER', float(number_str), start_line))
            else:
                tokens.append(('NUMBER', int(number_str), start_line))
            continue
        
        if char.isalpha() or char == '_':
            start_line = line
            name_str = ''
            while i < len(source_code) and (source_code[i].isalnum() or source_code[i] == '_'):
                name_str += source_code[i]
                i += 1
            
            if name_str in KEYWORDS:
                tokens.append((KEYWORDS[name_str], name_str, start_line))
            else:
                tokens.append(('IDENTIFIER', name_str, start_line))
            continue
        
        # 문자열 리터럴 "..."
        # 이스케이프: \n \t \\ \"
        # 문자열 안에 진짜 줄바꿈은 허용하지 않는다 (닫는 따옴표를 잊은 거일 가능성이 높음)
        if char == '"':
            start_line = line
            i += 1  # 여는 " 소비
            # 17: 문자열 보간 — "hello {name}" 형태 감지
            # {가 있으면 INTERP_STRING으로, 없으면 기존 STRING으로.
            parts = []       # [문자열 조각, 식 이름, 문자열 조각, ...]
            current_chars = []
            has_interp = False
            while True:
                if i >= len(source_code):
                    raise DanhaSyntaxError("닫는 \" 가 없어", line=start_line, source=source_code)
                c = source_code[i]
                if c == '"':
                    i += 1  # 닫는 " 소비
                    break
                if c == '\n':
                    raise DanhaSyntaxError("문자열 안에 줄바꿈이 있어. 닫는 \" 를 잊었어?", line=start_line, source=source_code)
                if c == '\\':
                    if i + 1 >= len(source_code):
                        raise DanhaSyntaxError("\\ 다음에 글자가 없어", line=line, source=source_code)
                    nxt = source_code[i + 1]
                    if nxt == 'n':
                        current_chars.append('\n')
                    elif nxt == 't':
                        current_chars.append('\t')
                    elif nxt == '\\':
                        current_chars.append('\\')
                    elif nxt == '"':
                        current_chars.append('"')
                    elif nxt == '{':
                        current_chars.append('{')
                    elif nxt == '}':
                        current_chars.append('}')
                    else:
                        raise DanhaSyntaxError(f"모르는 이스케이프야: \\{nxt}", line=line, source=source_code)
                    i += 2
                    continue
                if c == '{':
                    has_interp = True
                    # 현재까지 모은 문자열을 parts에 추가
                    parts.append(('str', ''.join(current_chars)))
                    current_chars = []
                    i += 1  # { 소비
                    # } 까지 식 텍스트 수집
                    expr_chars = []
                    depth = 1
                    while i < len(source_code) and depth > 0:
                        ec = source_code[i]
                        if ec == '{':
                            depth += 1
                        elif ec == '}':
                            depth -= 1
                            if depth == 0:
                                i += 1  # } 소비
                                break
                        if ec == '\n':
                            raise DanhaSyntaxError("문자열 보간 안에 줄바꿈이 있어", line=start_line, source=source_code)
                        expr_chars.append(ec)
                        i += 1
                    if depth > 0:
                        raise DanhaSyntaxError("문자열 보간의 닫는 '}' 가 없어", line=start_line, source=source_code)
                    expr_text = ''.join(expr_chars).strip()
                    if not expr_text:
                        raise DanhaSyntaxError("문자열 보간 {} 안이 비어있어", line=start_line, source=source_code)
                    parts.append(('expr', expr_text))
                    continue
                current_chars.append(c)
                i += 1
            
            if has_interp:
                # 마지막 문자열 조각 추가
                parts.append(('str', ''.join(current_chars)))
                tokens.append(('INTERP_STRING', parts, start_line))
            else:
                tokens.append(('STRING', ''.join(current_chars), start_line))
            continue
        
        # = 또는 ==
        if char == '=':
            if i + 1 < len(source_code) and source_code[i + 1] == '=':
                tokens.append(('EQEQ', '==', line))
                i += 2
            elif i + 1 < len(source_code) and source_code[i + 1] == '>':
                tokens.append(('FAT_ARROW', '=>', line))
                i += 2
            else:
                tokens.append(('EQUALS', '=', line))
                i += 1
            continue
        
        # > 또는 >= 또는 >>
        if char == '>':
            if i + 1 < len(source_code) and source_code[i + 1] == '>':
                tokens.append(('RSHIFT', '>>', line))
                i += 2
            elif i + 1 < len(source_code) and source_code[i + 1] == '=':
                tokens.append(('GTE', '>=', line))
                i += 2
            else:
                tokens.append(('GT', '>', line))
                i += 1
            continue

        # < 또는 <= 또는 <<
        if char == '<':
            if i + 1 < len(source_code) and source_code[i + 1] == '<':
                tokens.append(('LSHIFT', '<<', line))
                i += 2
            elif i + 1 < len(source_code) and source_code[i + 1] == '=':
                tokens.append(('LTE', '<=', line))
                i += 2
            else:
                tokens.append(('LT', '<', line))
                i += 1
            continue
        
        # !=
        if char == '!':
            if i + 1 < len(source_code) and source_code[i + 1] == '=':
                tokens.append(('NEQ', '!=', line))
                i += 2
                continue
            else:
                # 22a: 매크로 호출/정의에서 ! 사용 — NAME!(args)
                tokens.append(('BANG', '!', line))
                i += 1
                continue
        
        if char == '+':
            tokens.append(('PLUS', '+', line))
            i += 1
            continue
        
        # 23b: ? 연산자 — Result의 에러 전파.
        # expr? → Ok면 값 추출, Err면 현재 함수에서 Err 반환.
        if char == '?':
            tokens.append(('QUESTION', '?', line))
            i += 1
            continue
        
        if char == '-':
            # -> (화살표) vs - (빼기) 구분.
            # 함수 반환 타입에 쓰임: fn add(a, b) -> i32
            if i + 1 < len(source_code) and source_code[i + 1] == '>':
                tokens.append(('ARROW', '->', line))
                i += 2
                continue
            tokens.append(('MINUS', '-', line))
            i += 1
            continue
        
        if char == '*':
            tokens.append(('STAR', '*', line))
            i += 1
            continue
        
        if char == '/':
            tokens.append(('SLASH', '/', line))
            i += 1
            continue
        
        if char == '%':
            tokens.append(('PERCENT', '%', line))
            i += 1
            continue

        # & (참조 또는 비트 AND).
        # 단항 위치(식 앞)에서는 참조 연산자, 이항 위치에서는 비트 AND.
        # 파서에서 문맥으로 구별한다.
        if char == '&':
            tokens.append(('AMP', '&', line))
            i += 1
            continue

        if char == '|':
            tokens.append(('PIPE', '|', line))
            i += 1
            continue

        if char == '^':
            tokens.append(('CARET', '^', line))
            i += 1
            continue

        if char == '~':
            tokens.append(('TILDE', '~', line))
            i += 1
            continue
        
        if char == '(':
            tokens.append(('LPAREN', '(', line))
            paren_depth += 1
            i += 1
            continue

        if char == ')':
            tokens.append(('RPAREN', ')', line))
            if paren_depth > 0:
                paren_depth -= 1
            i += 1
            continue
        
        if char == '{':
            tokens.append(('LBRACE', '{', line))
            i += 1
            continue
        
        if char == '}':
            tokens.append(('RBRACE', '}', line))
            i += 1
            continue
        
        if char == '[':
            tokens.append(('LBRACK', '[', line))
            paren_depth += 1
            i += 1
            continue

        if char == ']':
            tokens.append(('RBRACK', ']', line))
            if paren_depth > 0:
                paren_depth -= 1
            i += 1
            continue
        
        if char == ',':
            tokens.append(('COMMA', ',', line))
            i += 1
            continue
        
        if char == ':':
            tokens.append(('COLON', ':', line))
            i += 1
            continue
        
        # ';' — 7.2에서 도입.
        # 타입 문법 '[T; N]' 안에서만 의미가 있다 (고정 배열의 길이 구분자).
        # 문장 종결자로는 쓰지 않는다 — 단아는 줄바꿈으로 문장을 끝내는 언어.
        # 만약 나중에 세미콜론을 문장 종결자로도 허용하고 싶다면 여기가 아니라
        # 파서의 '문장 경계' 규칙을 바꿔야 한다.
        if char == ';':
            tokens.append(('SEMICOLON', ';', line))
            i += 1
            continue
        
        # '@' — 어노테이션 시작. @clink("...") 같은 데서 사용.
        if char == '@':
            tokens.append(('AT', '@', line))
            i += 1
            continue
        
        if char == '.':
            # ... (가변인자) vs .. (범위) vs . (필드 접근) 순서로 구분.
            # '...' 은 extern fn 가변인자에서만 사용: extern fn printf(fmt: str, ...)
            if (i + 2 < len(source_code)
                    and source_code[i + 1] == '.'
                    and source_code[i + 2] == '.'):
                tokens.append(('ELLIPSIS', '...', line))
                i += 3
                continue
            if i + 1 < len(source_code) and source_code[i + 1] == '.':
                tokens.append(('DOTDOT', '..', line))
                i += 2
                continue
            tokens.append(('DOT', '.', line))
            i += 1
            continue
        
        raise DanhaSyntaxError(f"모르는 글자야: {char}", line=line, source=source_code)
    
    tokens.append(('EOF', None, line))
    return tokens
