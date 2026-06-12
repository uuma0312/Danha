# danha_parser.py
# Danha 언어의 파서
#
# AST 노드 규칙: 모든 노드는 튜플이고, 마지막 원소가 줄 번호다.
# 예: ('Number', 42, 5)         -> 5번째 줄의 숫자 42
#     ('Add', left, right, 7)   -> 7번째 줄에서 시작한 덧셈
#     ('Call', 'f', [args], 3)  -> 3번째 줄의 함수 호출
#
# 줄 번호는 에러 메시지에만 쓰인다. 평가기는 인덱스 1..n-2까지만 쓰고
# 마지막 원소는 에러 때만 쓴다.

from lexer import lex
from danha_errors import DanhaSyntaxError


def parse(tokens, source_code=None):
    pos = [0]
    # 구조체 리터럴 (Name { ... }) 허용 여부.
    # if/while 조건 파싱 중에는 False로 해서 `if done { ... }` 같은
    # 케이스에서 '{'가 블록의 시작으로 해석되게 한다.
    # (Rust가 쓰는 방식과 같은 해결책)
    allow_struct_literal = [True]
    
    def error(message, line=None):
        """파서 에러를 DanhaSyntaxError로 발생시킨다."""
        if line is None:
            line = current_line()
        raise DanhaSyntaxError(message, line=line, source=source_code)
    
    def current_token():
        return tokens[pos[0]]
    
    _peek_cache = {}   # pos → token. 파서는 전진만 하므로 무효화 불필요.

    def peek_token():
        """다음 토큰을 소비하지 않고 미리 본다."""
        next_pos = pos[0] + 1
        cached = _peek_cache.get(next_pos)
        if cached is not None:
            return cached
        result = tokens[next_pos] if next_pos < len(tokens) else tokens[-1]
        _peek_cache[next_pos] = result
        return result
    
    def current_line():
        """현재 토큰의 줄 번호 (새 노드에 붙일 때 씀)"""
        return tokens[pos[0]][2]
    
    def advance():
        pos[0] += 1
    
    def skip_newlines():
        while current_token()[0] == 'NEWLINE':
            advance()
    
    def parse_atom():
        """진짜 원자: 숫자, 이름, 함수 호출, 괄호식, 구조체 인스턴스"""
        token = current_token()
        line = token[2]
        
        if token[0] == 'NUMBER':
            advance()
            return ('Number', token[1], line)
        
        if token[0] == 'STRING':
            advance()
            return ('String', token[1], line)
        
        # 17: 문자열 보간 — "hello {name}, age {age}"
        # 렉서가 parts 리스트로 분해해둠.
        # 각 ('expr', text) 부분을 별도 렉서+파서로 AST 노드로 변환.
        if token[0] == 'INTERP_STRING':
            parts = token[1]
            advance()
            ast_parts = []
            for kind, value in parts:
                if kind == 'str':
                    ast_parts.append(('String', value, line))
                else:  # 'expr'
                    from lexer import lex as lex_inner
                    inner_tokens = lex_inner(value)
                    inner_ast = parse(inner_tokens)
                    # parse는 ('Program', [stmts], line) 반환
                    # 식이 하나인 경우 그 식을 꺼낸다
                    if inner_ast[0] == 'Program' and len(inner_ast[1]) == 1:
                        ast_parts.append(inner_ast[1][0])
                    else:
                        ast_parts.append(inner_ast[1][0] if inner_ast[1] else ('String', '', line))
            return ('InterpString', ast_parts, line)
        
        # 리스트 리터럴: [1, 2, 3] 또는 [] 또는 [value; N] (repeat)
        if token[0] == 'LBRACK':
            advance()
            skip_newlines()
            elements = []
            if current_token()[0] != 'RBRACK':
                first_elem = parse_logic_or()
                # repeat 리터럴: `[value; count]` — count는 정수 리터럴
                if current_token()[0] == 'SEMICOLON':
                    advance()
                    if current_token()[0] != 'NUMBER' or not isinstance(current_token()[1], int):
                        error("배열 repeat의 길이는 정수 리터럴이어야 해 (예: [0; 8])")
                    count = current_token()[1]
                    if count < 0:
                        error(f"배열 repeat 길이는 0 이상이어야 해 (지금: {count})")
                    advance()
                    skip_newlines()
                    if current_token()[0] != 'RBRACK':
                        error("repeat 리터럴 끝에 ']' 가 있어야 해")
                    advance()
                    # 단순 expansion — count 만큼 같은 노드 반복
                    elements = [first_elem] * count
                    return ('List', elements, line)
                elements.append(first_elem)
                while True:
                    if current_token()[0] == 'COMMA':
                        advance()
                        skip_newlines()
                        if current_token()[0] == 'RBRACK':
                            break  # 후행 쉼표 허용
                        elements.append(parse_logic_or())
                    elif current_token()[0] == 'NEWLINE':
                        skip_newlines()
                        if current_token()[0] == 'RBRACK':
                            break
                        # 쉼표 없이 개행으로만 구분하는 건 허용하지 않음
                        error("리스트 원소 사이에 ',' 가 있어야 해")
                    else:
                        break
            skip_newlines()
            if current_token()[0] != 'RBRACK':
                error("']' 가 있어야 해")
            advance()
            return ('List', elements, line)
        
        if token[0] == 'TRUE':
            advance()
            return ('Bool', True, line)
        
        if token[0] == 'FALSE':
            advance()
            return ('Bool', False, line)
        
        if token[0] == 'NULL':
            advance()
            return ('Null', None, line)
        
        # self는 식별자처럼 사용될 수 있다 (메서드 본체에서)
        if token[0] == 'SELF':
            advance()
            return ('Name', 'self', line)
        
        if token[0] == 'IDENTIFIER':
            advance()
            
            # 22a: 매크로 호출 — NAME!(args)
            if current_token()[0] == 'BANG' and peek_token()[0] == 'LPAREN':
                advance()  # BANG
                advance()  # LPAREN
                args = []
                if current_token()[0] != 'RPAREN':
                    args.append(parse_logic_or())
                    while current_token()[0] == 'COMMA':
                        advance()
                        args.append(parse_logic_or())
                if current_token()[0] != 'RPAREN':
                    error("매크로 호출의 ')' 가 있어야 해")
                advance()
                return ('MacroCall', token[1], args, line)
            
            # 함수 호출
            if current_token()[0] == 'LPAREN':
                advance()
                
                args = []
                if current_token()[0] != 'RPAREN':
                    args.append(parse_logic_or())
                    while current_token()[0] == 'COMMA':
                        advance()
                        args.append(parse_logic_or())
                
                if current_token()[0] != 'RPAREN':
                    error("')'가 있어야 해")
                advance()
                return ('Call', token[1], args, line)
            
            # 구조체 인스턴스 생성: Name { field: value, ... }
            # 단, if/while 조건 파싱 중에는 '{'를 블록 시작으로 해석하도록 비활성화
            if current_token()[0] == 'LBRACE' and allow_struct_literal[0]:
                advance()
                skip_newlines()
                
                field_values = {}
                
                if current_token()[0] != 'RBRACE':
                    if current_token()[0] != 'IDENTIFIER':
                        error("필드 이름이 있어야 해")
                    field_name = current_token()[1]
                    advance()
                    
                    if current_token()[0] != 'COLON':
                        error("필드 이름 다음에 ':'가 있어야 해")
                    advance()
                    
                    field_values[field_name] = parse_logic_or()
                    
                    while True:
                        if current_token()[0] == 'COMMA':
                            advance()
                        elif current_token()[0] == 'NEWLINE':
                            skip_newlines()
                            if current_token()[0] == 'RBRACE':
                                break
                        else:
                            break
                        
                        skip_newlines()
                        if current_token()[0] == 'RBRACE':
                            break
                        
                        if current_token()[0] != 'IDENTIFIER':
                            error("필드 이름이 있어야 해")
                        field_name = current_token()[1]
                        advance()
                        
                        if current_token()[0] != 'COLON':
                            error("필드 이름 다음에 ':'가 있어야 해")
                        advance()
                        
                        field_values[field_name] = parse_logic_or()
                
                skip_newlines()
                if current_token()[0] != 'RBRACE':
                    error("'}}'가 있어야 해")
                advance()
                
                return ('StructInstance', token[1], field_values, line)
            
            # 그냥 변수 참조
            return ('Name', token[1], line)
        
        # 15: 익명 함수(람다) — fn(a, b) { return a + b }
        # 식(expression) 위치에서 fn 키워드가 나오면 이름 없는 함수로 파싱.
        # parse_statement의 FnDef와 달리, 이름이 없고 값으로 반환된다.
        if token[0] == 'FN':
            advance()
            if current_token()[0] != 'LPAREN':
                error("익명 함수: 'fn' 다음에 '(' 가 있어야 해")
            advance()
            
            params = []
            param_types = []
            if current_token()[0] != 'RPAREN':
                if current_token()[0] != 'IDENTIFIER':
                    error("매개변수 이름이 있어야 해")
                params.append(current_token()[1])
                advance()
                param_types.append(try_parse_type_annotation())
                
                while current_token()[0] == 'COMMA':
                    advance()
                    if current_token()[0] != 'IDENTIFIER':
                        error("',' 다음에 매개변수 이름이 있어야 해")
                    params.append(current_token()[1])
                    advance()
                    param_types.append(try_parse_type_annotation())
            
            if current_token()[0] != 'RPAREN':
                error("')' 가 있어야 해")
            advance()
            
            # 선택적 반환 타입
            return_type = None
            if current_token()[0] == 'ARROW':
                advance()
                return_type = parse_type_node()
            
            skip_newlines()
            body = parse_block()
            
            return ('Lambda', params, body, param_types, return_type, line)
        
        if token[0] == 'LPAREN':
            advance()
            inner = parse_logic_or()
            if current_token()[0] != 'RPAREN':
                error(f"')'가 있어야 하는데 {current_token()[0]}이(가) 나왔어")
            advance()
            return inner
        
        # 18: if 식 — x = if cond { a } else { b }
        # 식 위치에서 if가 나오면 IfExpr 노드로 파싱.
        # else가 필수 (값을 반환해야 하니까).
        if token[0] == 'IF':
            advance()
            allow_struct_literal[0] = False
            condition = parse_logic_or()
            allow_struct_literal[0] = True
            skip_newlines()
            then_block = parse_block()
            skip_newlines()
            if current_token()[0] != 'ELSE':
                error("if 식에는 else가 있어야 해")
            advance()
            skip_newlines()
            if current_token()[0] == 'IF':
                else_block = ('Block', [parse_atom()], line)  # 중첩 if 식
            else:
                else_block = parse_block()
            return ('If', condition, then_block, else_block, line)
        
        # 18: match 식 — x = match val { ... }
        if token[0] == 'MATCH':
            advance()
            allow_struct_literal[0] = False
            target = parse_logic_or()
            allow_struct_literal[0] = True
            skip_newlines()
            if current_token()[0] != 'LBRACE':
                error("match 다음에 '{' 가 있어야 해")
            advance()
            skip_newlines()
            
            arms = []
            while current_token()[0] != 'RBRACE':
                skip_newlines()
                if current_token()[0] == 'RBRACE':
                    break
                # 패턴
                if current_token()[0] == 'IDENTIFIER' and current_token()[1] == '_':
                    advance()
                    pattern = ('MatchWildcard',)
                elif current_token()[0] == 'IDENTIFIER':
                    vname = current_token()[1]
                    advance()
                    bindings = []
                    if current_token()[0] == 'LPAREN':
                        advance()
                        if current_token()[0] != 'RPAREN':
                            if current_token()[0] != 'IDENTIFIER':
                                error("match 패턴에 변수 이름이 있어야 해")
                            bindings.append(current_token()[1])
                            advance()
                            while current_token()[0] == 'COMMA':
                                advance()
                                if current_token()[0] != 'IDENTIFIER':
                                    error("match 패턴에 변수 이름이 있어야 해")
                                bindings.append(current_token()[1])
                                advance()
                        if current_token()[0] != 'RPAREN':
                            error("match 패턴의 ')' 가 있어야 해")
                        advance()
                    pattern = ('MatchVariant', vname, bindings)
                else:
                    error("match arm에 variant 이름이나 '_'가 있어야 해")
                
                skip_newlines()
                if current_token()[0] != 'FAT_ARROW':
                    error("match 패턴 뒤에 '=>'가 있어야 해")
                advance()
                skip_newlines()
                
                body = parse_block()
                arms.append((pattern, body))
                skip_newlines()
            
            if current_token()[0] != 'RBRACE':
                error("match의 '}' 가 있어야 해")
            advance()
            
            return ('Match', target, arms, line)
        
        # 20a: comptime 블록 — comptime { ... }
        # 컴파일 타임에 코드를 실행하고 결과값으로 치환된다.
        # 블록의 마지막 식이 결과값이 된다 (Rust/Zig 스타일).
        # 예: const TABLE_SIZE = comptime { 360 }
        #     const PI2 = comptime { 3.14159 * 2.0 }
        if token[0] == 'COMPTIME':
            advance()
            skip_newlines()
            body = parse_block()
            return ('Comptime', body, line)
        
        # 21a: unsafe 블록 — unsafe { ... }
        # 식 위치에서도 사용 가능: x = unsafe { dangerous_op() }
        if token[0] == 'UNSAFE':
            advance()
            skip_newlines()
            body = parse_block()
            return ('UnsafeBlock', body, line)
        
        # 42단계: @sizeof(Type) / @alignof(Type) — 컴파일 타임 크기/정렬 조회
        # 45단계: @sin(x), @cos(x), @sqrt(x), @abs(x), @floor(x), @ceil(x),
        #         @pow(x,y), @atan2(y,x), @min(a,b), @max(a,b) — 수학 내장 함수
        _MATH_UNARY  = frozenset({'sin','cos','tan','sqrt','abs','floor','ceil','round','log','exp'})
        _MATH_BINARY = frozenset({'pow','atan2','min','max','hypot'})
        if token[0] == 'AT':
            advance()  # '@' 소비
            if current_token()[0] != 'IDENTIFIER':
                error("'@' 다음에 내장 함수 이름이 있어야 해")
            intrinsic = current_token()[1]
            if intrinsic in ('sizeof', 'alignof'):
                advance()  # 이름 소비
                if current_token()[0] != 'LPAREN':
                    error(f"@{intrinsic} 다음에 '('가 있어야 해")
                advance()  # '(' 소비
                type_node = parse_type_node()
                if current_token()[0] != 'RPAREN':
                    error(f"@{intrinsic}의 ')' 가 있어야 해")
                advance()  # ')' 소비
                if intrinsic == 'sizeof':
                    return ('SizeOf', type_node, line)
                else:
                    return ('AlignOf', type_node, line)
            elif intrinsic in _MATH_UNARY or intrinsic in _MATH_BINARY:
                advance()  # 이름 소비
                if current_token()[0] != 'LPAREN':
                    error(f"@{intrinsic} 다음에 '('가 있어야 해")
                advance()  # '(' 소비
                math_args = []
                if current_token()[0] != 'RPAREN':
                    math_args.append(parse_logic_or())
                    while current_token()[0] == 'COMMA':
                        advance()
                        math_args.append(parse_logic_or())
                if current_token()[0] != 'RPAREN':
                    error(f"@{intrinsic} 인자 목록의 ')' 가 있어야 해")
                advance()  # ')' 소비
                expected = 1 if intrinsic in _MATH_UNARY else 2
                if len(math_args) != expected:
                    error(f"@{intrinsic}은(는) 인자 {expected}개가 필요해")
                return ('MathIntrinsic', intrinsic, math_args, line)
            else:
                error(f"식 위치에서 '@{intrinsic}'은(는) 쓸 수 없어")

        error(f"숫자, 이름, 또는 '('가 있어야 하는데 {token[0]}이(가) 나왔어", line=line)
    
    def parse_primary():
        """원자에 후위 연산(필드 접근, 인덱싱, 호출)을 적용"""
        node = parse_atom()
        
        # 점, 대괄호, 또는 괄호(호출)가 나오는 동안 감싸기
        # 15: LPAREN 추가 — callbacks[i](10) 같은 "식 결과를 호출"하는 패턴
        while current_token()[0] in ('DOT', 'LBRACK', 'LPAREN', 'QUESTION'):
            if current_token()[0] == 'DOT':
                dot_line = current_line()
                advance()  # '.' 소비
                
                if current_token()[0] != 'IDENTIFIER':
                    error("'.' 다음에 필드 이름이 있어야 해")
                name = current_token()[1]
                advance()
                
                # '.'name 다음에 '('가 오면 메서드 호출, 아니면 필드 접근
                if current_token()[0] == 'LPAREN':
                    advance()  # '(' 소비
                    args = []
                    if current_token()[0] != 'RPAREN':
                        args.append(parse_logic_or())
                        while current_token()[0] == 'COMMA':
                            advance()
                            args.append(parse_logic_or())
                    if current_token()[0] != 'RPAREN':
                        error("메서드 호출의 ')'가 있어야 해")
                    advance()
                    node = ('MethodCall', node, name, args, dot_line)
                else:
                    node = ('FieldAccess', node, name, dot_line)
            elif current_token()[0] == 'LBRACK':
                bracket_line = current_line()
                advance()  # '[' 소비
                index_expr = parse_logic_or()
                if current_token()[0] != 'RBRACK':
                    error("인덱스 뒤에 ']' 가 있어야 해")
                advance()
                node = ('Index', node, index_expr, bracket_line)
            elif current_token()[0] == 'LPAREN':
                # LPAREN — 식 결과를 함수로 호출: expr(args...)
                # 기존 parse_atom에서 IDENTIFIER + LPAREN → Call 노드로 처리하므로
                # 여기는 그 외 경우만 (인덱스 결과, 필드 접근 결과 등)
                # 이미 Call이나 MethodCall이면 중복이 되므로 Name 노드가 아닌 것만 처리
                call_line = current_line()
                advance()  # '(' 소비
                args = []
                if current_token()[0] != 'RPAREN':
                    args.append(parse_logic_or())
                    while current_token()[0] == 'COMMA':
                        advance()
                        args.append(parse_logic_or())
                if current_token()[0] != 'RPAREN':
                    error("호출의 ')' 가 있어야 해")
                advance()
                node = ('CallExpr', node, args, call_line)
            elif current_token()[0] == 'QUESTION':
                # 23b: ? 연산자 — Result 에러 전파.
                # expr? → Ok면 값 추출, Err면 현재 함수에서 Err 자동 반환.
                q_line = current_line()
                advance()  # '?' 소비
                node = ('QuestionOp', node, q_line)
        
        return node
    
    def parse_unary():
        """단항 연산: -x, ~x, &x, &mut x"""
        if current_token()[0] == 'MINUS':
            op_line = current_line()
            advance()
            operand = parse_unary()
            return ('Neg', operand, op_line)
        # ~ — 비트 NOT
        if current_token()[0] == 'TILDE':
            op_line = current_line()
            advance()
            operand = parse_unary()
            return ('BitNot', operand, op_line)
        # 7.1.3 파트 2: 참조 식 '&x' / '&mut x'
        if current_token()[0] == 'AMP':
            op_line = current_line()
            advance()  # '&' 소비
            is_mut = False
            if current_token()[0] == 'MUT':
                advance()  # 'mut' 소비
                is_mut = True
            operand = parse_unary()
            return ('AddrOf', is_mut, operand, op_line)
        return parse_primary()
    
    def parse_cast():
        """16: 타입 캐스팅 — expr as type
        곱셈보다 높은 우선순위: (base as f64) * mult"""
        left = parse_unary()
        
        while current_token()[0] == 'AS':
            as_line = current_line()
            advance()
            # 29단계: as dyn TraitName — 동적 디스패치 트레이트 객체로 캐스팅
            if current_token()[0] == 'DYN':
                advance()
                if current_token()[0] != 'IDENTIFIER':
                    error("'as dyn' 다음에 트레잇 이름이 있어야 해 (예: as dyn Drawable)")
                trait_name = current_token()[1]
                advance()
                left = ('DynCast', left, trait_name, as_line)
            else:
                if current_token()[0] != 'IDENTIFIER':
                    error("'as' 다음에 타입 이름이 있어야 해 (i32, f64, str, bool, dyn Trait)")
                target_type = current_token()[1]
                advance()
                left = ('Cast', left, target_type, as_line)
        
        return left
    
    def parse_mul_div():
        """곱셈/나눗셈/나머지"""
        left = parse_cast()
        
        while current_token()[0] in ('STAR', 'SLASH', 'PERCENT'):
            op_line = current_line()
            operator_token = current_token()
            advance()
            right = parse_cast()
            
            if operator_token[0] == 'STAR':
                left = ('Mul', left, right, op_line)
            elif operator_token[0] == 'SLASH':
                left = ('Div', left, right, op_line)
            else:  # PERCENT
                left = ('Mod', left, right, op_line)
        
        return left
    
    def parse_add_sub():
        """덧셈/뺄셈"""
        left = parse_mul_div()
        
        while current_token()[0] in ('PLUS', 'MINUS'):
            op_line = current_line()
            operator_token = current_token()
            advance()
            right = parse_mul_div()
            
            if operator_token[0] == 'PLUS':
                left = ('Add', left, right, op_line)
            else:
                left = ('Sub', left, right, op_line)
        
        return left
    
    def parse_bitshift():
        """비트 시프트: <<, >>"""
        left = parse_add_sub()
        while current_token()[0] in ('LSHIFT', 'RSHIFT'):
            op_line = current_line()
            op = current_token()[0]
            advance()
            right = parse_add_sub()
            if op == 'LSHIFT':
                left = ('LShift', left, right, op_line)
            else:
                left = ('RShift', left, right, op_line)
        return left

    def parse_bitand():
        """비트 AND: & (이항 위치에서만 — 단항 위치는 parse_unary가 처리)"""
        left = parse_bitshift()
        while current_token()[0] == 'AMP':
            op_line = current_line()
            advance()
            right = parse_bitshift()
            left = ('BitAnd', left, right, op_line)
        return left

    def parse_bitxor():
        """비트 XOR: ^"""
        left = parse_bitand()
        while current_token()[0] == 'CARET':
            op_line = current_line()
            advance()
            right = parse_bitand()
            left = ('BitXor', left, right, op_line)
        return left

    def parse_bitor():
        """비트 OR: |"""
        left = parse_bitxor()
        while current_token()[0] == 'PIPE':
            op_line = current_line()
            advance()
            right = parse_bitxor()
            left = ('BitOr', left, right, op_line)
        return left

    def parse_comparison():
        """비교 연산. 비트 연산보다 낮은 우선순위 — 피연산자로 bitor 식을 받는다."""
        left = parse_bitor()

        while current_token()[0] in ('GT', 'LT', 'GTE', 'LTE', 'EQEQ', 'NEQ'):
            op_line = current_line()
            operator_token = current_token()
            advance()
            right = parse_add_sub()
            
            op_type = operator_token[0]
            if op_type == 'GT':
                left = ('Gt', left, right, op_line)
            elif op_type == 'LT':
                left = ('Lt', left, right, op_line)
            elif op_type == 'GTE':
                left = ('Gte', left, right, op_line)
            elif op_type == 'LTE':
                left = ('Lte', left, right, op_line)
            elif op_type == 'EQEQ':
                left = ('Eq', left, right, op_line)
            else:
                left = ('Neq', left, right, op_line)
        
        return left
    
    def parse_logic_not():
        """not 단항 연산자"""
        if current_token()[0] == 'NOT':
            line = current_line()
            advance()
            operand = parse_logic_not()
            return ('Not', operand, line)
        return parse_comparison()
    
    def parse_logic_and():
        """and"""
        left = parse_logic_not()
        
        while current_token()[0] == 'AND':
            op_line = current_line()
            advance()
            right = parse_logic_not()
            left = ('And', left, right, op_line)
        
        return left
    
    def parse_logic_or():
        """or (식의 최상층)"""
        left = parse_logic_and()
        
        while current_token()[0] == 'OR':
            op_line = current_line()
            advance()
            right = parse_logic_and()
            left = ('Or', left, right, op_line)
        
        return left
    
    def parse_block():
        """블록: { 문장들 }"""
        block_line = current_line()
        if current_token()[0] != 'LBRACE':
            error(f"'{{'가 있어야 하는데 {current_token()[0]}이(가) 나왔어", line=block_line)
        advance()
        
        statements = []
        skip_newlines()
        
        while current_token()[0] != 'RBRACE' and current_token()[0] != 'EOF':
            stmt = parse_statement()
            statements.append(stmt)
            
            if current_token()[0] == 'NEWLINE':
                skip_newlines()
            elif current_token()[0] == 'SEMICOLON':
                advance()
                skip_newlines()
            elif current_token()[0] != 'RBRACE':
                error(f"줄바꿈이나 '}}'가 있어야 하는데 {current_token()[0]}이(가) 나왔어")
        
        if current_token()[0] != 'RBRACE':
            error(f"'}}'가 있어야 하는데 {current_token()[0]}이(가) 나왔어")
        advance()
        
        return ('Block', statements, block_line)
    
    def parse_type_node():
        """
        타입 위치에서 한 타입을 읽어 AST 노드로 돌려준다.
        
        단아의 타입 모양:
          - 단순 타입:     'Player'         → ('TypeName', 'Player', line)
          - 읽기 참조:     '&Player'        → ('RefType', False, inner_node, line)
          - 쓰기 참조:     '&mut Player'    → ('RefType', True,  inner_node, line)
          - 고정 배열:     '[i32; 5]'       → ('ArrayType', elem_node, length, line)
        
        고정 배열은 중첩 가능: '[[i32; 3]; 4]' → ArrayType(ArrayType(i32, 3), 4)
        길이 자리는 숫자 리터럴만 허용 (상수식은 comptime 도입 후 재검토).
        """
        start_line = current_line()
        
        # 참조 타입: '&' 또는 '&mut' 가 선두에 올 수 있음
        if current_token()[0] == 'AMP':
            advance()  # '&' 소비
            is_mut = False
            if current_token()[0] == 'MUT':
                advance()  # 'mut' 소비
                is_mut = True
            # RefType의 내부는 다시 타입 노드. 다만 이중 참조(&&T)는 금지.
            if current_token()[0] == 'AMP':
                kind = "'&mut'" if is_mut else "'&'"
                error(f"{kind} 다음에 타입 이름이 있어야 해")
            if current_token()[0] not in ('IDENTIFIER', 'LBRACK'):
                kind = "'&mut'" if is_mut else "'&'"
                error(f"{kind} 다음에 타입 이름이 있어야 해")
            inner = parse_type_node()
            return ('RefType', is_mut, inner, start_line)

        # 고정 배열 타입: '[' 원소타입 ';' 길이 ']'
        # 동적 배열 타입: '[' 원소타입 ']'  (길이 생략)
        # 예: [i32; 5] (고정), [f64] (동적, push/pop 가능)
        # 원소타입은 다시 parse_type_node를 재귀 호출 → 중첩 배열 자연스럽게 지원.
        if current_token()[0] == 'LBRACK':
            advance()  # '[' 소비
            elem_node = parse_type_node()
            # ';' 있으면 고정 배열, 없으면 동적 배열
            if current_token()[0] == 'RBRACK':
                advance()  # ']' 소비 — 동적 배열
                return ('DynArrayType', elem_node, start_line)
            if current_token()[0] != 'SEMICOLON':
                error(f"배열 타입에서 ';' 또는 ']'가 있어야 해 (예: [i32; 5] 고정 또는 [i32] 동적)")
            advance()  # ';' 소비
            if current_token()[0] != 'NUMBER' or not isinstance(current_token()[1], int):
                error(f"배열 길이는 정수 리터럴이어야 해 (예: [i32; 5]). 상수식은 아직 지원 안 함.")
            length = current_token()[1]
            if length < 0:
                error(f"배열 길이는 0 이상이어야 해 (지금: {length})")
            advance()  # 숫자 소비
            if current_token()[0] != 'RBRACK':
                error(f"배열 타입의 ']'가 있어야 해 (예: [i32; 5])")
            advance()  # ']' 소비
            return ('ArrayType', elem_node, length, start_line)

        # 29단계: dyn Trait — 동적 디스패치 타입
        if current_token()[0] == 'DYN':
            advance()  # 'dyn' 소비
            if current_token()[0] != 'IDENTIFIER':
                error("'dyn' 다음에 트레잇 이름이 있어야 해 (예: dyn Drawable)")
            trait_name = current_token()[1]
            advance()
            return ('DynType', trait_name, start_line)

        # 42단계: fn(T1, T2) -> R — 함수 포인터 타입
        # 예: render_fn: fn(i32, i32) -> unit = software_renderer
        if current_token()[0] == 'FN':
            advance()  # 'fn' 소비
            if current_token()[0] != 'LPAREN':
                error("함수 포인터 타입: 'fn' 다음에 '('가 있어야 해")
            advance()  # '(' 소비
            fp_param_types = []
            if current_token()[0] != 'RPAREN':
                fp_param_types.append(parse_type_node())
                while current_token()[0] == 'COMMA':
                    advance()
                    fp_param_types.append(parse_type_node())
            if current_token()[0] != 'RPAREN':
                error("함수 포인터 타입의 ')' 가 있어야 해")
            advance()  # ')' 소비
            fp_ret_type = None
            if current_token()[0] == 'ARROW':
                advance()  # '->' 소비
                fp_ret_type = parse_type_node()
            return ('FnPtrType', fp_param_types, fp_ret_type, start_line)

        # 단순 타입: 식별자 하나
        # 'ptr' 은 C의 void* — 불투명 포인터. extern fn에서 SDL_Window*, SDL_Renderer* 등에 사용.
        if current_token()[0] == 'IDENTIFIER':
            type_name = current_token()[1]
            advance()
            if type_name == 'ptr':
                return ('PtrType', start_line)
            if current_token()[0] == 'LT':
                advance()  # '<'
                type_args = []
                if current_token()[0] == 'GT':
                    error("제네릭 타입에는 타입 인자가 1개 이상 필요해")
                while True:
                    type_args.append(parse_type_node())
                    if current_token()[0] == 'COMMA':
                        advance()
                        continue
                    break
                if current_token()[0] != 'GT':
                    error("제네릭 타입의 '>'가 있어야 해")
                advance()
                return ('GenericType', type_name, type_args, start_line)
            return ('TypeName', type_name, start_line)
        
        error("타입 이름이 있어야 해")

    def try_parse_type_annotation():
        """
        ':' 이 보이면 소비하고 타입 노드를 돌려준다. 없으면 None.
        7.2.1b 이전: (타입_문자열, 참조_종류) 튜플을 돌려줬음.
        7.2.1b 이후: 타입 AST 노드를 그대로 돌려줌. 호출자가 노드를 해석.
        """
        if current_token()[0] != 'COLON':
            return None
        advance()  # ':' 소비
        return parse_type_node()
    
    def parse_fn_rest(stmt_line, is_method):
        """
        'fn' 키워드를 이미 소비한 뒤, 이름과 매개변수와 본체를 읽는다.
        is_method=True면 메서드 정의 — 첫 매개변수가 반드시 'self'여야 함.
        반환: ('FnDef', name, params, body, param_types, return_type, stmt_line)
        
        7.2.1b 설계 갱신: 타입을 AST 노드로 통일. param_ref_kinds 사이드 채널 제거.
        
        params: 이름 문자열 리스트 (변경 없음) — 평가기가 그대로 씀.
        param_types: 같은 길이의 타입 노드 리스트. 어노테이션 없으면 None.
                     타입 노드가 참조 정보(RefType)와 배열 정보(ArrayType, 7.2.1c)를
                     스스로 품으므로 더 이상 평행한 메타데이터 리스트가 필요 없다.
                     인터프리터는 여전히 무시. 컴파일러가 6-6b부터 해석.
        return_type: '-> 타입' 어노테이션의 타입 노드, 없으면 None.
                     7.1 결정에 따라 RefType은 여기서 거부 (참조 반환은 7.5 이후).
        
        주의: node[-1]이 여전히 line 번호를 가리키도록 마지막에 stmt_line.
              그래서 `node[-1]`로 line 번호를 뽑는 옛 코드는 그대로 동작.
        """
        if current_token()[0] != 'IDENTIFIER':
            error("'fn' 다음에 함수 이름이 있어야 해")
        name = current_token()[1]
        advance()
        
        # 8.5: 제네릭 타입 매개변수 <T, U, ...>
        type_params = []
        if current_token()[0] == 'LT':
            advance()
            if current_token()[0] != 'IDENTIFIER':
                error("'<' 다음에 타입 매개변수 이름이 있어야 해")
            type_params.append(current_token()[1])
            advance()
            while current_token()[0] == 'COMMA':
                advance()
                if current_token()[0] != 'IDENTIFIER':
                    error("',' 다음에 타입 매개변수 이름이 있어야 해")
                type_params.append(current_token()[1])
                advance()
            if current_token()[0] != 'GT':
                error("'>' 가 있어야 해")
            advance()
        
        if current_token()[0] != 'LPAREN':
            error("함수 이름 다음에 '(' 또는 '<'가 있어야 해")
        advance()
        
        # 7.2.1b 이후:
        # params: 매개변수 이름 문자열 리스트 (변경 없음)
        # param_types: 타입 AST 노드(또는 None) 리스트.
        #   타입 노드가 참조 정보를 품으므로 param_ref_kinds 사이드 채널은 사라짐.
        params = []
        param_types = []
        if current_token()[0] != 'RPAREN':
            # 첫 매개변수: 메서드면 'self', 아니면 일반 식별자
            if is_method:
                if current_token()[0] != 'SELF':
                    error("메서드의 첫 매개변수는 'self'여야 해")
                params.append('self')
                advance()
                # self도 ':타입' 어노테이션을 받는다 (보통은 안 적지만 일관성).
                # self는 항상 구조체 포인터 의미라 참조 표기가 어색 → 그대로 저장.
                # 컴파일러가 self의 첫 매개변수는 별도 처리하므로 내용은 중요하지 않음.
                param_types.append(try_parse_type_annotation())
            else:
                if current_token()[0] != 'IDENTIFIER':
                    error("매개변수 이름이 있어야 해")
                params.append(current_token()[1])
                advance()
                param_types.append(try_parse_type_annotation())
            
            while current_token()[0] == 'COMMA':
                advance()
                if current_token()[0] != 'IDENTIFIER':
                    error("',' 다음에 매개변수 이름이 있어야 해")
                params.append(current_token()[1])
                advance()
                param_types.append(try_parse_type_annotation())
        elif is_method:
            error("메서드는 최소한 'self' 매개변수가 있어야 해")
        
        if current_token()[0] != 'RPAREN':
            error("')'가 있어야 해")
        advance()
        
        # 선택적 반환 타입: '-> 타입'
        # 7.1 결정: 반환은 항상 복사. 참조 반환(&T)은 아직 허용하지 않음 — 7.5에서 재검토.
        # 7.2.1b: return_type은 이제 AST 노드(또는 None). 참조 반환은 여기서 거부.
        return_type = None
        if current_token()[0] == 'ARROW':
            advance()  # '->' 소비
            if current_token()[0] not in ('IDENTIFIER', 'AMP', 'LBRACK'):
                error("'->' 다음에 타입 이름이 있어야 해")
            return_type = parse_type_node()
            if return_type[0] == 'RefType':
                error(f"반환 타입에는 아직 '&'를 못 써 (7.1 결정: 반환은 복사). 참조 반환은 7.5 이후.")
        
        skip_newlines()
        body = parse_block()
        
        # 7.2.1b: FnDef 튜플에서 param_ref_kinds 제거.
        # 이전: ('FnDef', name, params, body, param_types, return_type, param_ref_kinds, line)
        # 이후: ('FnDef', name, params, body, param_types, return_type, type_params, line)
        return ('FnDef', name, params, body, param_types, return_type, type_params, stmt_line)
    
    def parse_extern_fn_rest(stmt_line):
        """
        'extern fn'을 이미 소비한 뒤, 이름과 매개변수를 읽는다.
        본문(body)은 없다 — 외부 C 라이브러리에 구현이 있으니까.
        반환: ('ExternFn', name, params, param_types, return_type, is_vararg, line)
        
        7.17a: 가변인자 지원. 마지막 매개변수로 '...' 가 오면 is_vararg=True.
               예: extern fn printf(fmt: str, ...) -> i32
        """
        if current_token()[0] != 'IDENTIFIER':
            error("'extern fn' 다음에 함수 이름이 있어야 해")
        name = current_token()[1]
        advance()
        
        if current_token()[0] != 'LPAREN':
            error("함수 이름 다음에 '('가 있어야 해")
        advance()
        
        params = []
        param_types = []
        is_vararg = False
        if current_token()[0] != 'RPAREN':
            # '...' 단독으로 올 수도 있음 (매개변수 없는 vararg — 드물지만 허용)
            if current_token()[0] == 'ELLIPSIS':
                is_vararg = True
                advance()
            else:
                if current_token()[0] != 'IDENTIFIER':
                    error("매개변수 이름이 있어야 해")
                params.append(current_token()[1])
                advance()
                param_types.append(try_parse_type_annotation())
                
                while current_token()[0] == 'COMMA':
                    advance()
                    # ',' 다음에 '...' 이면 가변인자 끝
                    if current_token()[0] == 'ELLIPSIS':
                        is_vararg = True
                        advance()
                        break
                    if current_token()[0] != 'IDENTIFIER':
                        error("',' 다음에 매개변수 이름이 있어야 해")
                    params.append(current_token()[1])
                    advance()
                    param_types.append(try_parse_type_annotation())
        
        if current_token()[0] != 'RPAREN':
            error("')'가 있어야 해")
        advance()
        
        # 선택적 반환 타입
        return_type = None
        if current_token()[0] == 'ARROW':
            advance()
            if current_token()[0] not in ('IDENTIFIER', 'AMP', 'LBRACK'):
                error("'->' 다음에 타입 이름이 있어야 해")
            return_type = parse_type_node()
            if return_type[0] == 'RefType':
                error(f"extern 함수는 참조를 반환할 수 없어")
        
        # 본문 없음 — 개행이나 EOF로 끝남
        return ('ExternFn', name, params, param_types, return_type, is_vararg, stmt_line)
    
    def parse_statement():
        """한 문장"""
        stmt_line = current_line()

        # 49단계: /// doc comment — 다음 선언에 붙일 문서 문자열 수집
        if current_token()[0] == 'DOC_COMMENT':
            doc_lines = []
            while current_token()[0] == 'DOC_COMMENT':
                doc_lines.append(current_token()[1])
                advance()
                # skip only newlines between consecutive doc comment lines
                while current_token()[0] == 'NEWLINE':
                    advance()
            doc_text = '\n'.join(doc_lines)
            # parse the immediately following declaration
            inner = parse_statement()
            return ('DocAnnotated', doc_text, inner, stmt_line)

        # 48단계: test 블록 — test "이름" { 본문 }
        # contextual keyword: IDENTIFIER 'test' 다음에 STRING 또는 '{' 가 오면 test 블록
        if (current_token()[0] == 'IDENTIFIER' and current_token()[1] == 'test' and
                peek_token()[0] in ('STRING', 'LBRACE')):
            advance()  # 'test' 소비
            name = ''
            if current_token()[0] == 'STRING':
                name = current_token()[1]
                advance()
            skip_newlines()
            body = parse_block()
            return ('TestBlock', name, body, stmt_line)

        # 22a: 매크로 정의 — macro NAME!(params) { body }
        if current_token()[0] == 'MACRO':
            advance()
            if current_token()[0] != 'IDENTIFIER':
                error("macro 다음에 매크로 이름이 있어야 해")
            name = current_token()[1]
            advance()
            # ! 는 선택적 — 정의할 때 붙여도 되고 안 붙여도 됨
            if current_token()[0] == 'BANG':
                advance()
            skip_newlines()
            if current_token()[0] != 'LPAREN':
                error("매크로 매개변수 목록의 '(' 가 있어야 해")
            advance()
            skip_newlines()
            # 매개변수 파싱
            params = []
            is_variadic = False
            while current_token()[0] != 'RPAREN':
                if current_token()[0] != 'IDENTIFIER':
                    error("매크로 매개변수 이름이 있어야 해")
                param_name = current_token()[1]
                advance()
                # 가변 인자: args...
                if current_token()[0] == 'ELLIPSIS':
                    advance()
                    is_variadic = True
                    params.append((param_name, True))  # (이름, 가변여부)
                else:
                    params.append((param_name, False))
                skip_newlines()
                if current_token()[0] == 'COMMA':
                    advance()
                    skip_newlines()
            advance()  # RPAREN
            skip_newlines()
            body = parse_block()
            return ('MacroDef', name, params, body, is_variadic, stmt_line)
        
        # 21a: unsafe 블록 — unsafe { ... }
        # 블록 안에서는 포인터 산술, raw 캐스팅 등 위험한 연산을 허용한다.
        # 21d에서 unsafe fn도 추가 예정.
        if current_token()[0] == 'UNSAFE':
            advance()
            skip_newlines()
            # unsafe fn — 함수 전체가 unsafe (21d)
            if current_token()[0] == 'FN':
                advance()
                fn_node = parse_fn_rest(stmt_line, is_method=False)
                # FnDef 노드를 UnsafeFn으로 감싸기
                return ('UnsafeFn', fn_node, stmt_line)
            # unsafe { ... } — 블록
            body = parse_block()
            return ('UnsafeBlock', body, stmt_line)
        
        # if 문 (else if 처리 포함)
        if current_token()[0] == 'IF':
            advance()
            # 조건 안에서는 구조체 리터럴을 허용하지 않는다.
            # 그래야 `if flag { ... }`에서 '{'가 블록 시작으로 해석된다.
            allow_struct_literal[0] = False
            condition = parse_logic_or()
            allow_struct_literal[0] = True
            skip_newlines()
            then_block = parse_block()
            
            else_block = None
            saved_pos = pos[0]
            skip_newlines()
            if current_token()[0] == 'ELSE':
                advance()
                skip_newlines()
                
                if current_token()[0] == 'IF':
                    else_block = parse_statement()
                else:
                    else_block = parse_block()
            else:
                pos[0] = saved_pos
            
            return ('If', condition, then_block, else_block, stmt_line)
        
        # while 문
        if current_token()[0] == 'WHILE':
            advance()
            allow_struct_literal[0] = False
            condition = parse_logic_or()
            allow_struct_literal[0] = True
            skip_newlines()
            body = parse_block()
            return ('While', condition, body, stmt_line)
        
        # for 문: for x in expr { ... }
        if current_token()[0] == 'FOR':
            advance()
            
            if current_token()[0] != 'IDENTIFIER':
                error("'for' 다음에 변수 이름이 있어야 해")
            var_name = current_token()[1]
            advance()
            
            if current_token()[0] != 'IN':
                error("for 변수 다음에 'in'이 있어야 해")
            advance()
            
            # 반복할 식 파싱. if/while 조건과 마찬가지로 구조체 리터럴 금지.
            allow_struct_literal[0] = False
            iterable = parse_logic_or()
            
            # 범위 문법: 'for i in start..end'
            # parse_logic_or가 start까지만 읽고 멈췄을 때, 다음 토큰이 '..'면 범위.
            # Range 노드는 for 헤더에서만 의미 있음 (그래서 일반 식 파서에 안 박음).
            if current_token()[0] == 'DOTDOT':
                range_line = current_line()
                advance()
                end_expr = parse_logic_or()
                iterable = ('Range', iterable, end_expr, range_line)
            
            allow_struct_literal[0] = True
            
            skip_newlines()
            body = parse_block()
            return ('For', var_name, iterable, body, stmt_line)
        
        # 9.1a: import 문
        # import math          → ('Import', 'math', None, line)
        # import physics.collision → ('Import', 'physics.collision', None, line)
        if current_token()[0] == 'IMPORT':
            advance()
            if current_token()[0] != 'IDENTIFIER':
                error("'import' 다음에 모듈 이름이 있어야 해")
            module_path = current_token()[1]
            advance()
            # 점으로 연결된 하위 모듈: import physics.collision
            while current_token()[0] == 'DOT':
                advance()
                if current_token()[0] != 'IDENTIFIER':
                    error("'.' 다음에 모듈 이름이 있어야 해")
                module_path += '.' + current_token()[1]
                advance()
            return ('Import', module_path, None, stmt_line)
        
        # 9.1a: from ... import 문
        # from math import sin, cos   → ('FromImport', 'math', ['sin', 'cos'], line)
        # from math import *          → ('FromImport', 'math', '*', line)
        if current_token()[0] == 'FROM':
            advance()
            if current_token()[0] != 'IDENTIFIER':
                error("'from' 다음에 모듈 이름이 있어야 해")
            module_path = current_token()[1]
            advance()
            while current_token()[0] == 'DOT':
                advance()
                if current_token()[0] != 'IDENTIFIER':
                    error("'.' 다음에 모듈 이름이 있어야 해")
                module_path += '.' + current_token()[1]
                advance()
            if current_token()[0] != 'IMPORT':
                error(f"'from {module_path}' 다음에 'import'가 있어야 해")
            advance()
            # from math import *
            if current_token()[0] == 'STAR':
                advance()
                return ('FromImport', module_path, '*', stmt_line)
            # from math import sin, cos
            names = []
            if current_token()[0] != 'IDENTIFIER':
                error("'import' 다음에 가져올 이름이 있어야 해")
            names.append(current_token()[1])
            advance()
            while current_token()[0] == 'COMMA':
                advance()
                if current_token()[0] != 'IDENTIFIER':
                    error("',' 다음에 이름이 있어야 해")
                names.append(current_token()[1])
                advance()
            return ('FromImport', module_path, names, stmt_line)
        
        # 45단계: export fn — 외부 공개 함수
        if current_token()[0] == 'EXPORT':
            advance()
            if current_token()[0] != 'FN':
                error("'export' 다음에는 'fn'이 있어야 해")
            advance()
            fn_node = parse_fn_rest(stmt_line, is_method=False)
            return ('ExportFn', fn_node, stmt_line)

        # 함수 정의
        if current_token()[0] == 'FN':
            advance()
            return parse_fn_rest(stmt_line, is_method=False)
        
        # 외부 함수 선언: extern fn 이름(매개변수) -> 반환타입
        # C-FFI용. 본문(body) 없이 선언만 한다.
        # LLVM에서 declare로 등록되고, 링크 시 실제 C 라이브러리에서 찾는다.
        # @clink("libname") — C 라이브러리 링크 어노테이션 (7.17c)
        # @attribute — 범용 어노테이션 (35단계)
        # 예: @serialize struct Player { ... }
        # 예: @clink("SDL2") extern { fn SDL_Init(flags: i32) -> i32 }
        if current_token()[0] == 'AT':
            # 어노테이션을 모은다. 여러 개를 연달아 붙일 수 있다.
            attributes = []
            while current_token()[0] == 'AT':
                advance()  # '@' 소비
                if current_token()[0] != 'IDENTIFIER':
                    error("'@' 다음에 어노테이션 이름이 있어야 해 (예: @serialize)")
                attr_name = current_token()[1]
                attr_line = current_line()
                advance()  # 이름 소비
                
                # 인자가 있는 경우: @name(arg1, arg2) 또는 @name(key = val)
                attr_args = []
                if current_token()[0] == 'LPAREN':
                    advance()  # '(' 소비
                    while current_token()[0] != 'RPAREN':
                        if current_token()[0] == 'STRING':
                            attr_args.append(('StringArg', current_token()[1], current_line()))
                            advance()
                        elif current_token()[0] == 'NUMBER':
                            attr_args.append(('NumArg', current_token()[1], current_line()))
                            advance()
                        elif current_token()[0] == 'IDENTIFIER':
                            key = current_token()[1]
                            advance()
                            if current_token()[0] == 'EQUALS':
                                advance()  # '=' 소비
                                if current_token()[0] == 'NUMBER':
                                    attr_args.append(('KeyVal', key, current_token()[1], current_line()))
                                    advance()
                                elif current_token()[0] == 'STRING':
                                    attr_args.append(('KeyVal', key, current_token()[1], current_line()))
                                    advance()
                                elif current_token()[0] == 'IDENTIFIER':
                                    attr_args.append(('KeyVal', key, current_token()[1], current_line()))
                                    advance()
                                else:
                                    error("@attribute 인자의 값이 필요해")
                            else:
                                attr_args.append(('NameArg', key, current_line()))
                        else:
                            error(f"@attribute 인자로 올 수 없는 토큰: {current_token()[0]}")
                        if current_token()[0] == 'COMMA':
                            advance()
                    advance()  # ')' 소비
                
                attributes.append(('Attribute', attr_name, attr_args, attr_line))
                skip_newlines()
            
            # @clink은 기존 로직으로 분기
            if len(attributes) == 1 and attributes[0][1] == 'clink':
                attr = attributes[0]
                # clink은 인자가 문자열 1개여야 함
                if len(attr[2]) != 1 or attr[2][0][0] != 'StringArg':
                    error("@clink에는 문자열 라이브러리 이름이 있어야 해 (예: @clink(\"SDL2\"))")
                lib_name = attr[2][0][1]
                # 다음에 extern fn 또는 extern { } 가 와야 함
                if current_token()[0] != 'EXTERN':
                    error("@clink 다음에 'extern' 선언이 있어야 해")
                advance()  # 'extern' 소비
                if current_token()[0] == 'LBRACE':
                    # extern 블록
                    advance()  # '{' 소비
                    extern_nodes = []
                    skip_newlines()
                    while current_token()[0] != 'RBRACE':
                        if current_token()[0] != 'FN':
                            error("extern 블록 안에는 'fn' 선언만 올 수 있어")
                        fn_line = current_line()
                        advance()
                        extern_nodes.append(parse_extern_fn_rest(fn_line))
                        skip_newlines()
                    advance()  # '}' 소비
                    return ('CLink', lib_name, extern_nodes, stmt_line)
                elif current_token()[0] == 'FN':
                    advance()  # 'fn' 소비
                    efn = parse_extern_fn_rest(stmt_line)
                    return ('CLink', lib_name, [efn], stmt_line)
                else:
                    error(f"@clink 다음에 'extern fn' 또는 'extern {{}}' 가 있어야 해")
            
            # 범용 @attribute → 다음 선언에 붙인다
            # struct, fn, component, trait 앞에 올 수 있다
            next_stmt = parse_statement()
            # 다음 문이 붙일 수 있는 선언인지 확인
            valid_targets = ('StructDef', 'FnDef', 'ComponentDef', 'TraitDef',
                             'Attributed')  # 중첩 attribute
            if isinstance(next_stmt, tuple) and next_stmt[0] in valid_targets:
                return ('Attributed', attributes, next_stmt, stmt_line)
            else:
                # 선언이 아니어도 붙인다 (유연성)
                return ('Attributed', attributes, next_stmt, stmt_line)

        if current_token()[0] == 'EXTERN':
            advance()
            # extern { fn ... fn ... } — 블록 형태 (7.17b)
            # C 헤더처럼 여러 extern 선언을 묶어서 쓸 수 있다.
            if current_token()[0] == 'LBRACE':
                advance()  # '{' 소비
                extern_nodes = []
                skip_newlines()
                while current_token()[0] != 'RBRACE':
                    if current_token()[0] != 'FN':
                        error(f"extern 블록 안에는 'fn' 선언만 올 수 있어")
                    fn_line = current_line()
                    advance()  # 'fn' 소비
                    extern_nodes.append(parse_extern_fn_rest(fn_line))
                    skip_newlines()
                advance()  # '}' 소비
                return ('ExternBlock', extern_nodes, stmt_line)
            # extern fn 이름(...) — 단일 선언 (기존)
            if current_token()[0] != 'FN':
                error(f"'extern' 다음에 'fn'이나 '{{'가 있어야 해")
            advance()
            return parse_extern_fn_rest(stmt_line)
        
        # 구조체 정의: struct 이름 { 필드들 }
        if current_token()[0] == 'STRUCT':
            advance()
            
            if current_token()[0] != 'IDENTIFIER':
                error("'struct' 다음에 구조체 이름이 있어야 해")
            name = current_token()[1]
            advance()
            
            if current_token()[0] != 'LBRACE':
                error(f"구조체 이름 다음에 '{{'가 있어야 해")
            advance()
            
            # 필드는 [(이름, 타입_또는_None), ...] 형태로 모은다.
            # 어노테이션 없으면 타입은 None — 인터프리터는 무시, 컴파일러는 i32로 가정.
            # 6.10 이후 게임 언어 최종형: 'pos: vec3, hp: i32' 같은 혼합 타입.
            fields = []
            skip_newlines()
            
            def parse_one_field():
                if current_token()[0] != 'IDENTIFIER':
                    error("필드 이름이 있어야 해")
                fname = current_token()[1]
                advance()
                ftype_node = try_parse_type_annotation()  # ':타입'이 있으면 타입 노드, 없으면 None
                # 7.2.1b: 참조 필드 금지 검사를 타입 노드에서 직접.
                # 구조체 필드에 '&'/'&mut'은 '라이프타임 문제'가 따라와서 아직 못 씀.
                if ftype_node is not None and ftype_node[0] == 'RefType':
                    error(f"구조체 필드에는 '&' / '&mut'을 못 써 (필드 '{fname}')")
                fields.append((fname, ftype_node))
            
            if current_token()[0] != 'RBRACE':
                parse_one_field()
                
                while True:
                    if current_token()[0] == 'COMMA':
                        advance()
                    elif current_token()[0] == 'NEWLINE':
                        skip_newlines()
                        if current_token()[0] == 'RBRACE':
                            break
                    else:
                        break
                    
                    skip_newlines()
                    if current_token()[0] == 'RBRACE':
                        break
                    
                    parse_one_field()
            
            skip_newlines()
            if current_token()[0] != 'RBRACE':
                error("'}}'가 있어야 해")
            advance()
            
            return ('StructDef', name, fields, stmt_line)

        # union 정의: union 이름 { 필드들 }
        # 모든 필드가 같은 메모리를 공유 (C union 의미론).
        # 파싱 형태는 struct와 동일 — 의미 차이는 평가기/컴파일러가 처리.
        if current_token()[0] == 'UNION':
            advance()

            if current_token()[0] != 'IDENTIFIER':
                error("'union' 다음에 union 이름이 있어야 해")
            name = current_token()[1]
            advance()

            if current_token()[0] != 'LBRACE':
                error(f"union 이름 다음에 '{{'가 있어야 해")
            advance()

            fields = []
            skip_newlines()

            def parse_one_union_field():
                if current_token()[0] != 'IDENTIFIER':
                    error("필드 이름이 있어야 해")
                fname = current_token()[1]
                advance()
                ftype_node = try_parse_type_annotation()
                if ftype_node is not None and ftype_node[0] == 'RefType':
                    error(f"union 필드에는 '&' / '&mut'을 못 써 (필드 '{fname}')")
                fields.append((fname, ftype_node))

            if current_token()[0] != 'RBRACE':
                parse_one_union_field()

                while True:
                    if current_token()[0] == 'COMMA':
                        advance()
                    elif current_token()[0] == 'NEWLINE':
                        skip_newlines()
                        if current_token()[0] == 'RBRACE':
                            break
                    else:
                        break

                    skip_newlines()
                    if current_token()[0] == 'RBRACE':
                        break

                    parse_one_union_field()

            skip_newlines()
            if current_token()[0] != 'RBRACE':
                error("'}}'가 있어야 해")
            advance()

            return ('UnionDef', name, fields, stmt_line)

        # 컴포넌트 정의: component 이름 { 필드들 }
        # 문법은 struct와 동일하지만 의미가 다르다:
        # - struct는 한 인스턴스가 한 덩어리 (AoS)
        # - component는 entity들에 부착되고, 저장은 필드별 배열 (SoA)
        # 이 단계(7.12a)에서는 파서까지만. 저장/접근 API는 7.12b, c에서.
        if current_token()[0] == 'COMPONENT':
            advance()
            
            if current_token()[0] != 'IDENTIFIER':
                error("'component' 다음에 컴포넌트 이름이 있어야 해")
            name = current_token()[1]
            advance()
            
            if current_token()[0] != 'LBRACE':
                error(f"컴포넌트 이름 다음에 '{{'가 있어야 해")
            advance()
            
            # 필드 목록. struct와 같은 규칙:
            # - 이름 필수, 타입 어노테이션은 선택 (없으면 i32 기본)
            # - 참조 필드 (&T, &mut T) 는 금지 — SoA 배열에 저장 불가
            fields = []
            skip_newlines()
            
            def parse_one_component_field():
                if current_token()[0] != 'IDENTIFIER':
                    error("필드 이름이 있어야 해")
                fname = current_token()[1]
                advance()
                ftype_node = try_parse_type_annotation()
                if ftype_node is not None and ftype_node[0] == 'RefType':
                    error(f"컴포넌트 필드에는 '&' / '&mut'을 못 써 (필드 '{fname}')")
                fields.append((fname, ftype_node))
            
            if current_token()[0] != 'RBRACE':
                parse_one_component_field()
                
                while True:
                    if current_token()[0] == 'COMMA':
                        advance()
                    elif current_token()[0] == 'NEWLINE':
                        skip_newlines()
                        if current_token()[0] == 'RBRACE':
                            break
                    else:
                        break
                    
                    skip_newlines()
                    if current_token()[0] == 'RBRACE':
                        break
                    
                    parse_one_component_field()
            
            skip_newlines()
            if current_token()[0] != 'RBRACE':
                error("'}}'가 있어야 해")
            advance()
            
            return ('ComponentDef', name, fields, stmt_line)
        
        # 7.8b: trait 정의
        # trait 이름 { fn 메서드(self, ...) -> 타입 { 본문 } }
        # 트레잇은 메서드 시그니처 + 선택적 기본 구현의 묶음.
        # 이 단계에서는 기본 구현만 지원 (추상 메서드는 나중에).
        #
        # AST: ('TraitDef', name, [method_nodes...], line)
        if current_token()[0] == 'TRAIT':
            advance()
            
            if current_token()[0] != 'IDENTIFIER':
                error("'trait' 다음에 트레잇 이름이 있어야 해")
            name = current_token()[1]
            advance()
            
            if current_token()[0] != 'LBRACE':
                error(f"트레잇 이름 다음에 '{{'가 있어야 해")
            advance()
            
            methods = []
            skip_newlines()
            
            while current_token()[0] != 'RBRACE' and current_token()[0] != 'EOF':
                if current_token()[0] != 'FN':
                    error("trait 블록 안에는 'fn' 정의만 올 수 있어")
                method_line = current_line()
                advance()  # 'fn' 소비
                method = parse_fn_rest(method_line, is_method=True)
                methods.append(method)
                
                if current_token()[0] == 'NEWLINE':
                    skip_newlines()
            
            if current_token()[0] != 'RBRACE':
                error("'}}'가 있어야 해")
            advance()
            
            return ('TraitDef', name, methods, stmt_line)
        
        # system 정의: system 이름(매개변수) { for each (바인딩) { 본문 } }
        # 7.13a: 단아의 ECS 일괄 처리 문법.
        # 7.11a: parallel system도 지원. parallel 키워드가 앞에 오면 is_parallel=True.
        # system은 "이 컴포넌트를 가진 모든 엔티티를 순회하며 처리"하는 함수.
        # 내부적으로는 SoA 배열을 쭉 순회하는 루프로 변환된다.
        #
        # 예:
        #   system update_movement(dt: f64) {
        #       for each (p: Position, v: Velocity) {
        #           p.x = p.x + v.x * dt
        #       }
        #   }
        #
        #   parallel system update_particles(dt: f64) {
        #       for each (p: Position, v: Velocity) {
        #           p.x = p.x + v.vx * dt
        #       }
        #   }
        #
        # AST: ('SystemDef', name, params, param_types, bindings, body, is_parallel, line)
        is_parallel = False
        if current_token()[0] == 'PARALLEL':
            is_parallel = True
            advance()
            if current_token()[0] != 'SYSTEM':
                error("'parallel' 다음에는 'system'이 와야 해")
        
        if current_token()[0] == 'SYSTEM':
            advance()
            
            if current_token()[0] != 'IDENTIFIER':
                error("'system' 다음에 시스템 이름이 있어야 해")
            name = current_token()[1]
            advance()
            
            # 매개변수 목록 (일반 함수와 같은 형태)
            if current_token()[0] != 'LPAREN':
                error("시스템 이름 다음에 '('가 있어야 해")
            advance()
            
            params = []
            param_types = []
            if current_token()[0] != 'RPAREN':
                if current_token()[0] != 'IDENTIFIER':
                    error("매개변수 이름이 있어야 해")
                params.append(current_token()[1])
                advance()
                param_types.append(try_parse_type_annotation())
                
                while current_token()[0] == 'COMMA':
                    advance()
                    if current_token()[0] != 'IDENTIFIER':
                        error("',' 다음에 매개변수 이름이 있어야 해")
                    params.append(current_token()[1])
                    advance()
                    param_types.append(try_parse_type_annotation())
            
            if current_token()[0] != 'RPAREN':
                error("')'가 있어야 해")
            advance()
            
            skip_newlines()
            
            # system 본문: { for each (...) { ... } }
            if current_token()[0] != 'LBRACE':
                error(f"system 매개변수 뒤에 '{{'가 있어야 해")
            advance()
            skip_newlines()
            
            # 'for each' 파싱 — 'for' 키워드 + 'each'라는 식별자 조합
            if current_token()[0] != 'FOR':
                error(f"system 본문은 'for each (...) {{ ... }}'여야 해")
            advance()
            
            if current_token()[0] != 'IDENTIFIER' or current_token()[1] != 'each':
                error("'for' 다음에 'each'가 있어야 해 (system 안에서는 'for each' 문법만 가능)")
            advance()
            
            # 바인딩 목록: (변수: 컴포넌트, 변수: 컴포넌트, ...)
            if current_token()[0] != 'LPAREN':
                error("'for each' 다음에 '('가 있어야 해")
            advance()
            
            bindings = []
            
            # 헬퍼: 바인딩 하나 파싱 — "p: Position" 또는 "p: &Position" 또는 "p: &mut Position"
            # 반환: (bind_var, comp_name, access) 여기서 access는:
            #   'unspecified'  — 권한 생략 (기존 호환: 본문 스캔으로 자동 판단)
            #   'read'         — &로 명시 (읽기 약속)
            #   'write'        — &mut로 명시 (쓰기 약속, 스케줄러의 진실)
            def parse_binding():
                # 지원 문법:
                #   "p: Position"        — 일반 필수
                #   "?p: Position"       — Optional (없어도 처리, p는 None)
                #   "!Dead"              — Exclude (Dead 있는 엔티티 제외, 변수명 없음)
                # 반환: (bind_var, comp_name, access, kind)
                #   kind: 'required' | 'optional' | 'exclude'
                kind = 'required'
                if current_token()[0] == 'QUESTION':
                    kind = 'optional'
                    advance()
                elif current_token()[0] == 'BANG':
                    kind = 'exclude'
                    advance()
                    if current_token()[0] != 'IDENTIFIER':
                        error("'!' 다음에 컴포넌트 이름이 있어야 해 (예: !Dead)")
                    cn = current_token()[1]
                    advance()
                    return (None, cn, 'unspecified', 'exclude')

                if current_token()[0] != 'IDENTIFIER':
                    error("바인딩 변수 이름이 있어야 해")
                bv = current_token()[1]
                advance()
                if current_token()[0] != 'COLON':
                    error("바인딩 변수 다음에 ':'가 있어야 해 (예: p: Position)")
                advance()
                access = 'unspecified'
                if current_token()[0] == 'AMP':
                    advance()
                    if current_token()[0] == 'MUT':
                        advance()
                        access = 'write'
                    else:
                        access = 'read'
                if current_token()[0] != 'IDENTIFIER':
                    error("컴포넌트 이름이 있어야 해 (예: Position 또는 &mut Position)")
                cn = current_token()[1]
                advance()
                return (bv, cn, access, kind)
            
            if current_token()[0] != 'RPAREN':
                bindings.append(parse_binding())
                while current_token()[0] == 'COMMA':
                    advance()
                    bindings.append(parse_binding())
            
            if len(bindings) == 0:
                error("'for each'에는 최소 1개의 바인딩이 필요해 (예: for each (p: Position))")
            
            if current_token()[0] != 'RPAREN':
                error("')'가 있어야 해")
            advance()
            
            skip_newlines()
            body = parse_block()  # for each 안쪽 { ... }
            
            skip_newlines()
            if current_token()[0] != 'RBRACE':
                error("system 본문을 닫는 '}}'가 있어야 해")
            advance()
            
            return ('SystemDef', name, params, param_types, bindings, body, is_parallel, stmt_line)
        
        # impl 블록: impl TypeName { fn ... fn ... }
        if current_token()[0] == 'IMPL':
            advance()
            
            if current_token()[0] != 'IDENTIFIER':
                error("'impl' 다음에 이름이 있어야 해")
            first_name = current_token()[1]
            advance()
            
            # 7.8b: 'impl Trait for Type { ... }' vs 'impl Type { ... }'
            # 다음 토큰이 FOR이면 트레잇 구현, LBRACE이면 기존 impl.
            trait_name = None
            if current_token()[0] == 'FOR':
                advance()
                trait_name = first_name
                if current_token()[0] != 'IDENTIFIER':
                    error("'for' 다음에 구현할 타입 이름이 있어야 해")
                type_name = current_token()[1]
                advance()
            else:
                type_name = first_name
            
            if current_token()[0] != 'LBRACE':
                error(f"'impl' 블록에 '{{'가 있어야 해")
            advance()
            
            methods = []
            skip_newlines()
            
            while current_token()[0] != 'RBRACE' and current_token()[0] != 'EOF':
                if current_token()[0] != 'FN':
                    error("impl 블록 안에는 'fn' 정의만 올 수 있어")
                method_line = current_line()
                advance()  # 'fn' 소비
                method = parse_fn_rest(method_line, is_method=True)
                methods.append(method)
                
                if current_token()[0] == 'NEWLINE':
                    skip_newlines()
            
            if current_token()[0] != 'RBRACE':
                error("'}}'가 있어야 해")
            advance()
            
            if trait_name is not None:
                # ('ImplTrait', trait_name, type_name, methods, line)
                return ('ImplTrait', trait_name, type_name, methods, stmt_line)
            return ('Impl', type_name, methods, stmt_line)
        
        # const 정의: const NAME = expr
        # 컴파일 타임 상수. 한 번 정하면 재대입 불가.
        if current_token()[0] == 'CONST':
            advance()
            
            if current_token()[0] != 'IDENTIFIER':
                error("'const' 다음에 이름이 있어야 해")
            name = current_token()[1]
            advance()
            
            if current_token()[0] != 'EQUALS':
                error(f"const '{name}' 다음에 '='가 있어야 해")
            advance()
            
            value = parse_logic_or()
            return ('ConstDef', name, value, stmt_line)
        
        # 8.4: match 문 — tagged enum 패턴 매칭
        # match expr {
        #     VariantName(a, b) => { ... }
        #     VariantName => { ... }
        #     _ => { ... }
        # }
        if current_token()[0] == 'MATCH':
            advance()
            allow_struct_literal[0] = False
            target = parse_add_sub()
            allow_struct_literal[0] = True
            
            if current_token()[0] != 'LBRACE':
                error(f"match 다음에 '{{' 가 있어야 해")
            advance()
            skip_newlines()
            
            arms = []  # [(패턴, 본문), ...]
            # 패턴: ('MatchVariant', variant_name, [바인딩이름, ...])
            #        ('MatchWildcard',)
            while current_token()[0] != 'RBRACE':
                skip_newlines()
                if current_token()[0] == 'RBRACE':
                    break
                
                # 패턴 파싱
                if current_token()[0] == 'IDENTIFIER' and current_token()[1] == '_':
                    # 와일드카드
                    advance()
                    pattern = ('MatchWildcard',)
                elif current_token()[0] == 'IDENTIFIER':
                    vname = current_token()[1]
                    advance()
                    bindings = []
                    if current_token()[0] == 'LPAREN':
                        advance()
                        if current_token()[0] != 'RPAREN':
                            if current_token()[0] != 'IDENTIFIER':
                                error("match 패턴에 변수 이름이 있어야 해")
                            bindings.append(current_token()[1])
                            advance()
                            while current_token()[0] == 'COMMA':
                                advance()
                                if current_token()[0] != 'IDENTIFIER':
                                    error("match 패턴에 변수 이름이 있어야 해")
                                bindings.append(current_token()[1])
                                advance()
                        if current_token()[0] != 'RPAREN':
                            error("match 패턴의 ')' 가 있어야 해")
                        advance()
                    pattern = ('MatchVariant', vname, bindings)
                else:
                    error("match arm에 variant 이름이나 '_'가 있어야 해")
                
                # =>
                if current_token()[0] != 'FAT_ARROW':
                    error("match 패턴 뒤에 '=>'가 있어야 해")
                advance()
                skip_newlines()
                
                # 본문: { ... } 블록
                if current_token()[0] != 'LBRACE':
                    error(f"'=>' 다음에 '{{' 가 있어야 해")
                body = parse_block()
                
                arms.append((pattern, body))
                
                # arm 사이 줄바꿈/쉼표
                while current_token()[0] in ('NEWLINE', 'COMMA'):
                    advance()
            
            if current_token()[0] != 'RBRACE':
                error("match의 '}}' 가 있어야 해")
            advance()
            
            return ('Match', target, arms, stmt_line)
        
        # 7.15b / 8.3: enum 정의
        # 기본: enum Name { Variant1, Variant2 }              — 단순 enum (i32 매핑)
        # 8.3:  enum Name { Variant1(Type), Variant2(T1, T2) } — tagged union
        # 혼합도 허용: enum Msg { Quit, Move(f64, f64), Say(str) }
        if current_token()[0] == 'ENUM':
            advance()
            
            if current_token()[0] != 'IDENTIFIER':
                error("'enum' 다음에 이름이 있어야 해")
            name = current_token()[1]
            advance()
            
            # 8.6: 제네릭 타입 매개변수 <T, E, ...>
            enum_type_params = []
            if current_token()[0] == 'LT':
                advance()
                if current_token()[0] != 'IDENTIFIER':
                    error("'<' 다음에 타입 매개변수 이름이 있어야 해")
                enum_type_params.append(current_token()[1])
                advance()
                while current_token()[0] == 'COMMA':
                    advance()
                    if current_token()[0] != 'IDENTIFIER':
                        error("',' 다음에 타입 매개변수 이름이 있어야 해")
                    enum_type_params.append(current_token()[1])
                    advance()
                if current_token()[0] != 'GT':
                    error("'>' 가 있어야 해")
                advance()
            
            if current_token()[0] != 'LBRACE':
                error(f"enum '{name}' 다음에 '{{' 가 있어야 해")
            advance()
            
            variants = []  # [(이름, [타입노드, ...] or None), ...]
            skip_newlines()
            
            def _parse_variant():
                if current_token()[0] != 'IDENTIFIER':
                    error("enum variant 이름이 있어야 해")
                vname = current_token()[1]
                advance()
                # variant 뒤에 '('가 오면 타입 리스트 파싱
                vtypes = None
                if current_token()[0] == 'LPAREN':
                    advance()
                    vtypes = []
                    if current_token()[0] != 'RPAREN':
                        vtypes.append(parse_type_node())
                        while current_token()[0] == 'COMMA':
                            advance()
                            vtypes.append(parse_type_node())
                    if current_token()[0] != 'RPAREN':
                        error(f"variant '{vname}'의 ')' 가 있어야 해")
                    advance()
                return (vname, vtypes)
            
            if current_token()[0] != 'RBRACE':
                variants.append(_parse_variant())
                
                while True:
                    if current_token()[0] == 'COMMA':
                        advance()
                    elif current_token()[0] == 'NEWLINE':
                        skip_newlines()
                        if current_token()[0] == 'RBRACE':
                            break
                    else:
                        break
                    
                    skip_newlines()
                    if current_token()[0] == 'RBRACE':
                        break
                    
                    variants.append(_parse_variant())
            
            skip_newlines()
            if current_token()[0] != 'RBRACE':
                error(f"enum '{name}'의 '}}' 가 있어야 해")
            advance()
            
            return ('EnumDef', name, variants, enum_type_params, stmt_line)
        
        # return 문
        if current_token()[0] == 'RETURN':
            advance()
            
            if current_token()[0] in ('NEWLINE', 'RBRACE', 'EOF'):
                return ('Return', None, stmt_line)
            
            value = parse_logic_or()
            return ('Return', value, stmt_line)
        
        # break / continue (7.15d)
        # 루프 안에 있는지 검사는 실행 시점에. 함수를 거쳐서 들어갈 수도 있어서
        # 정적으로 완벽히 잡기 어려움. 동적 검사가 더 정직.
        if current_token()[0] == 'BREAK':
            advance()
            return ('Break', stmt_line)
        
        if current_token()[0] == 'CONTINUE':
            advance()
            return ('Continue', stmt_line)

        # 42단계: defer { ... } — 블록 탈출 시 자동 실행
        if current_token()[0] == 'DEFER':
            advance()
            skip_newlines()
            body = parse_block()
            return ('Defer', body, stmt_line)

        # print 문
        if current_token()[0] == 'PRINT':
            advance()
            
            if current_token()[0] != 'LPAREN':
                error("'print' 다음에 '('가 있어야 해")
            advance()
            
            value = parse_logic_or()
            
            if current_token()[0] != 'RPAREN':
                error("'print'의 ')'가 있어야 해")
            advance()
            
            return ('Print', value, stmt_line)
        
        # 대입문이거나 그냥 식
        # 먼저 식을 읽고, 그 다음에 ':'(타입 어노테이션) 또는 '='가 오면 대입문으로 재해석
        expr = parse_logic_or()
        
        # x: i32 = 5 패턴.
        # ':'은 단순 변수 이름 뒤에서만 의미 있다 (필드/인덱스 뒤에는 어노테이션 안 함).
        var_type = None
        if current_token()[0] == 'COLON' and expr[0] == 'Name':
            var_type = try_parse_type_annotation()
            # 7.2.1b: 변수 어노테이션에 참조 금지 — 타입 노드에서 직접 검사.
            # 변수 자체가 참조를 "담는" 건 7단계 뒤쪽에서 라이프타임과 함께 다룰 일.
            if var_type is not None and var_type[0] == 'RefType':
                error(f"변수 어노테이션에는 '&' / '&mut'을 못 써 (변수 '{expr[1]}')")
            # 어노테이션 뒤에는 '='가 와야 한다
            if current_token()[0] != 'EQUALS':
                error("타입 어노테이션 다음에 '='가 있어야 해")
        
        if current_token()[0] == 'EQUALS':
            eq_line = current_line()
            advance()
            value = parse_logic_or()
            
            # 왼쪽 종류에 따라 다른 대입 노드를 만든다
            if expr[0] == 'Name':
                # var_type은 어노테이션이 있으면 'i32' 같은 문자열, 없으면 None.
                # 평가기는 [-1]로 line만 보고 [4](var_type)는 안 본다.
                return ('Assign', expr[1], value, var_type, eq_line)
            if expr[0] == 'FieldAccess':
                # ('FieldAccess', 객체식, 필드이름, 줄) -> ('FieldAssign', 객체식, 필드이름, 값, 줄)
                return ('FieldAssign', expr[1], expr[2], value, eq_line)
            if expr[0] == 'Index':
                # ('Index', 객체식, 인덱스식, 줄) -> ('IndexAssign', 객체식, 인덱스식, 값, 줄)
                return ('IndexAssign', expr[1], expr[2], value, eq_line)
            error("'=' 왼쪽에는 변수나 필드나 인덱스만 올 수 있어", line=eq_line)
        
        return expr
    
    def parse_program():
        statements = []
        
        skip_newlines()
        
        while current_token()[0] != 'EOF':
            stmt = parse_statement()
            # 7.17b: extern { fn ... } 블록은 개별 ExternFn으로 풀어서 넣는다.
            # 7.17c: @clink("lib") extern { fn ... } 도 동일하게 풀되, 라이브러리 정보를 각 ExternFn에 태그.
            if stmt[0] == 'ExternBlock':
                for extern_fn in stmt[1]:
                    statements.append(extern_fn)
            elif stmt[0] == 'CLink':
                lib_name = stmt[1]
                for extern_fn in stmt[2]:
                    # ExternFn 튜플에 clink 정보를 추가한 새 노드로 감싸지 않고,
                    # 메타데이터 딕셔너리로 따로 기록한다 (AST 노드 형태 불변 원칙).
                    # CLinkedExternFn = ('ExternFn', ...) + clink 정보
                    # 간단하게: ('CLinkedFn', lib_name, extern_fn, line) 으로 래핑
                    statements.append(('CLinkedFn', lib_name, extern_fn, stmt[3]))
            else:
                statements.append(stmt)
            
            if current_token()[0] == 'NEWLINE':
                skip_newlines()
            elif current_token()[0] != 'EOF':
                error(f"줄바꿈이나 끝이 있어야 하는데 {current_token()[0]}이(가) 나왔어")
        
        return ('Program', statements, 1)
    
    return parse_program()
