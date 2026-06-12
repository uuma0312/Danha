# danha_shader.py — 단아 셰이더 언어 → GLSL 변환기 (56단계)
#
# 사용법:
#   danha shader <파일.dh>             → <파일>.vert + <파일>.frag
#   danha shader --combined <파일.dh>  → <파일>.glsl (두 셰이더 합본)
#
# 문법: @vert / @frag 어노테이션이 붙은 fn 블록을 GLSL로 변환
#
# 예시:
#   @vert
#   fn vertex_main(position: vec2, uv: vec2) -> vec4 {
#       return vec4(position.x, position.y, 0.0, 1.0)
#   }
#   @frag
#   fn fragment_main(uv: vec2, color: vec4) -> vec4 {
#       return color
#   }

import os
import sys

DANHA_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DANHA_DIR)

# 단아 타입 → GLSL 타입 매핑
_TYPE_MAP = {
    'f32': 'float', 'f64': 'float',
    'i32': 'int',   'i64': 'int',
    'bool': 'bool',
    'vec2': 'vec2', 'vec3': 'vec3', 'vec4': 'vec4',
    'mat4': 'mat4', 'mat3': 'mat3', 'mat2': 'mat2',
    'ptr':  'sampler2D',
}


def _danha_type_to_glsl(t):
    return _TYPE_MAP.get(t, t)


def _collect_shader_fns(source_path):
    """소스에서 @vert / @frag 어노테이션이 붙은 함수를 수집한다."""
    from lexer import lex
    from danha_parser import parse

    with open(source_path, 'r', encoding='utf-8') as f:
        source = f.read()

    tokens = lex(source)
    ast = parse(tokens, source)

    vert_fns = []
    frag_fns = []

    stmts = ast[1] if ast[0] == 'Program' else []
    i = 0
    while i < len(stmts):
        stmt = stmts[i]
        # Attributed 노드: ('Attributed', [('Attribute', name, args, line), ...], inner_node, line)
        if stmt[0] == 'Attributed':
            attr_list = stmt[1]  # list of ('Attribute', name, args, line)
            inner = stmt[2]
            attr_names = set()
            for a in attr_list:
                if isinstance(a, tuple) and len(a) >= 2:
                    attr_names.add(a[1])
            if inner[0] == 'FnDef':
                if 'vert' in attr_names:
                    vert_fns.append(inner)
                if 'frag' in attr_names:
                    frag_fns.append(inner)
        i += 1

    return vert_fns, frag_fns


def _fn_to_glsl(fn_node, shader_type):
    """
    ('FnDef', name, param_names, body, param_types, ret_type_node, defaults, line)
    → GLSL 함수 소스.
    entry point(main)는 void main()으로, 나머지는 그대로 변환.
    """
    # FnDef 구조: [1]=name [2]=param_names [3]=body [4]=param_types [5]=ret_type_node
    name = fn_node[1]
    param_names = fn_node[2]
    body = fn_node[3]
    param_types = fn_node[4] if len(fn_node) > 4 else []
    ret_type_node = fn_node[5] if len(fn_node) > 5 else None

    # 타입 이름 추출
    def _type_name(t):
        if isinstance(t, tuple) and len(t) > 1:
            return t[1]
        if isinstance(t, str):
            return t
        return 'float'

    param_type_names = [_type_name(t) for t in param_types]
    # 파라미터 수와 타입 수 맞추기
    while len(param_type_names) < len(param_names):
        param_type_names.append('float')

    ret_type = _type_name(ret_type_node) if ret_type_node else 'void'

    glsl_lines = []

    # 버전 헤더 및 precision
    glsl_lines.append('#version 120')
    if shader_type == 'frag':
        glsl_lines.append('precision mediump float;')
    glsl_lines.append('')

    # 파라미터 → attribute/varying 선언
    for pname, ptype in zip(param_names, param_type_names):
        glsl_type = _danha_type_to_glsl(ptype)
        if shader_type == 'vert':
            glsl_lines.append(f'attribute {glsl_type} {pname};')
        else:
            glsl_lines.append(f'varying {glsl_type} {pname};')

    if param_names:
        glsl_lines.append('')

    # 반환 타입에 따라 entry point 결정
    if ret_type and ret_type != 'void':
        glsl_type = _danha_type_to_glsl(ret_type)
        if shader_type == 'vert' and glsl_type == 'vec4':
            glsl_lines.append('void main() {')
            body_glsl = _body_to_glsl(body, indent='    ', is_entry=True,
                                      entry_out='gl_Position')
        elif shader_type == 'frag' and glsl_type == 'vec4':
            glsl_lines.append('void main() {')
            body_glsl = _body_to_glsl(body, indent='    ', is_entry=True,
                                      entry_out='gl_FragColor')
        else:
            glsl_lines.append(f'{glsl_type} {name}() {{')
            body_glsl = _body_to_glsl(body, indent='    ')
    else:
        glsl_lines.append(f'void {name}() {{')
        body_glsl = _body_to_glsl(body, indent='    ')

    glsl_lines.extend(body_glsl)
    glsl_lines.append('}')
    return '\n'.join(glsl_lines)


def _body_to_glsl(body_node, indent='    ', is_entry=False, entry_out=None):
    """AST body → GLSL 문장 리스트."""
    lines = []
    if body_node is None:
        return lines

    stmts = body_node[1] if body_node[0] == 'Block' else [body_node]
    for stmt in stmts:
        line = _stmt_to_glsl(stmt, indent, is_entry, entry_out)
        if line is not None:
            lines.append(line)
    return lines


def _stmt_to_glsl(node, indent, is_entry, entry_out):
    """단일 AST 문 → GLSL 줄."""
    if node is None:
        return None
    nt = node[0]

    if nt == 'Return':
        expr_src = _expr_to_glsl(node[1])
        if is_entry and entry_out:
            return f'{indent}{entry_out} = {expr_src};'
        return f'{indent}return {expr_src};'

    if nt == 'Assign':
        name = node[1]
        val = _expr_to_glsl(node[2])
        return f'{indent}{name} = {val};'

    if nt == 'VarDecl':
        name = node[1]
        type_hint = node[2]
        init = node[3] if len(node) > 3 else None
        glsl_type = _danha_type_to_glsl(type_hint) if type_hint else 'float'
        if init is not None:
            return f'{indent}{glsl_type} {name} = {_expr_to_glsl(init)};'
        return f'{indent}{glsl_type} {name};'

    if nt == 'ExprStmt':
        return f'{indent}{_expr_to_glsl(node[1])};'

    if nt == 'If':
        cond = _expr_to_glsl(node[1])
        then_lines = _body_to_glsl(node[2], indent + '    ', is_entry, entry_out)
        result = [f'{indent}if ({cond}) {{']
        result.extend(then_lines)
        result.append(f'{indent}}}')
        if len(node) > 3 and node[3] is not None:
            else_lines = _body_to_glsl(node[3], indent + '    ', is_entry, entry_out)
            result[-1] = f'{indent}}} else {{'
            result.extend(else_lines)
            result.append(f'{indent}}}')
        return '\n'.join(result)

    # 기타 노드는 주석으로
    return f'{indent}// [unsupported: {nt}]'


def _expr_to_glsl(node):
    """표현식 AST → GLSL 소스 문자열."""
    if node is None:
        return 'null'
    if isinstance(node, (int, float)):
        return str(node)
    if isinstance(node, str):
        return f'"{node}"'

    nt = node[0]

    if nt == 'Number':
        v = node[1]
        # GLSL float literals need a decimal point
        if isinstance(v, float) or (isinstance(v, int) and '.' not in str(v)):
            return f'{float(v)}'
        return str(v)

    if nt == 'Float':
        return f'{node[1]}'

    if nt == 'Ident':
        return node[1]

    if nt == 'BinOp':
        left = _expr_to_glsl(node[1])
        op = node[2]
        right = _expr_to_glsl(node[3])
        return f'({left} {op} {right})'

    if nt == 'UnaryOp':
        op = node[1]
        operand = _expr_to_glsl(node[2])
        return f'({op}{operand})'

    if nt == 'Call':
        name = node[1]
        args = ', '.join(_expr_to_glsl(a) for a in node[2])
        return f'{name}({args})'

    if nt == 'FieldAccess':
        obj = _expr_to_glsl(node[1])
        field = node[2]
        return f'{obj}.{field}'

    if nt == 'Index':
        obj = _expr_to_glsl(node[1])
        idx = _expr_to_glsl(node[2])
        return f'{obj}[{idx}]'

    if nt in ('String', 'Str'):
        return f'"{node[1]}"'

    if nt == 'Bool':
        return 'true' if node[1] else 'false'

    return f'/* {nt} */'


def transpile(source_path, combined=False):
    """
    .dh 소스를 GLSL로 변환한다.
    combined=False → <base>.vert + <base>.frag
    combined=True  → <base>.glsl (두 셰이더가 //=VERT= 구분자로 구분됨)
    반환값: 생성된 파일 경로 리스트
    """
    vert_fns, frag_fns = _collect_shader_fns(source_path)

    if not vert_fns and not frag_fns:
        raise ValueError(
            f"{source_path}에서 @vert / @frag 함수를 찾을 수 없어.\n"
            "  @vert fn vertex_main(...) { ... }\n"
            "  @frag fn fragment_main(...) { ... }\n"
            "형식으로 작성해봐."
        )

    base = os.path.splitext(source_path)[0]
    outputs = []

    vert_src = '\n\n'.join(_fn_to_glsl(fn, 'vert') for fn in vert_fns) if vert_fns else ''
    frag_src = '\n\n'.join(_fn_to_glsl(fn, 'frag') for fn in frag_fns) if frag_fns else ''

    if combined:
        out_path = base + '.glsl'
        with open(out_path, 'w', encoding='utf-8') as f:
            if vert_src:
                f.write('//=VERT=\n')
                f.write(vert_src)
                f.write('\n')
            if frag_src:
                f.write('//=FRAG=\n')
                f.write(frag_src)
                f.write('\n')
        outputs.append(out_path)
    else:
        if vert_src:
            vert_path = base + '.vert'
            with open(vert_path, 'w', encoding='utf-8') as f:
                f.write(vert_src)
            outputs.append(vert_path)
        if frag_src:
            frag_path = base + '.frag'
            with open(frag_path, 'w', encoding='utf-8') as f:
                f.write(frag_src)
            outputs.append(frag_path)

    return outputs


def main(args):
    if not args:
        print("사용법: danha shader [--combined] <파일.dh>")
        return 1

    combined = '--combined' in args
    files = [a for a in args if not a.startswith('--')]

    if not files:
        print("❌ 변환할 파일을 지정해줘: danha shader <파일.dh>")
        return 1

    source_path = files[0]
    if not os.path.exists(source_path):
        print(f"❌ 파일을 찾을 수 없어: {source_path}")
        return 1

    try:
        outputs = transpile(source_path, combined=combined)
        for out in outputs:
            print(f"✅ GLSL 출력: {out}")
        return 0
    except Exception as e:
        print(f"❌ 셰이더 변환 실패: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
