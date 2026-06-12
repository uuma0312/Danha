# danha_doc.py
# 49단계: 단아 문서 자동 생성
#
# /// doc comment를 읽어서 HTML 또는 Markdown으로 출력한다.
#
# 사용:
#   danha doc file.dh             → file.html
#   danha doc file.dh --md        → file.md

import os
import html as _html


# ===== AST 순회하며 문서 정보 수집 =====

def _collect_docs(statements):
    """
    AST 문장 목록을 순회하며 문서화된 항목 목록을 반환.
    반환: [{'kind': 'fn'|'struct'|'union'|'enum'|'trait'|'component',
             'name': str, 'doc': str, 'signature': str}]
    """
    items = []
    pending_doc = None

    for stmt in statements:
        if not isinstance(stmt, tuple):
            pending_doc = None
            continue

        kind = stmt[0]

        if kind == 'DocAnnotated':
            pending_doc = stmt[1]
            inner = stmt[2]
            _add_item(items, inner, pending_doc)
            pending_doc = None
            continue

        if kind in ('FnDef', 'StructDef', 'UnionDef', 'EnumDef', 'TraitDef', 'ComponentDef'):
            if pending_doc:
                _add_item(items, stmt, pending_doc)
                pending_doc = None
            else:
                _add_item(items, stmt, '')
        else:
            pending_doc = None

    return items


def _add_item(items, stmt, doc):
    kind = stmt[0]
    if kind == 'FnDef':
        name = stmt[1]
        params = stmt[2]
        ret = stmt[4] if len(stmt) > 4 else None
        sig = _fn_sig(name, params, ret)
        items.append({'kind': 'fn', 'name': name, 'doc': doc, 'signature': sig})
    elif kind == 'StructDef':
        name = stmt[1]
        fields = stmt[2] if len(stmt) > 2 else []
        items.append({'kind': 'struct', 'name': name, 'doc': doc, 'fields': _field_list(fields)})
    elif kind == 'UnionDef':
        name = stmt[1]
        fields = stmt[2] if len(stmt) > 2 else []
        items.append({'kind': 'union', 'name': name, 'doc': doc, 'fields': _field_list(fields)})
    elif kind == 'EnumDef':
        name = stmt[1]
        variants = [v[0] if isinstance(v, tuple) else v for v in (stmt[2] if len(stmt) > 2 else [])]
        items.append({'kind': 'enum', 'name': name, 'doc': doc, 'variants': variants})
    elif kind == 'TraitDef':
        name = stmt[1]
        items.append({'kind': 'trait', 'name': name, 'doc': doc})
    elif kind == 'ComponentDef':
        name = stmt[1]
        fields = stmt[2] if len(stmt) > 2 else []
        items.append({'kind': 'component', 'name': name, 'doc': doc, 'fields': _field_list(fields)})


def _fn_sig(name, params, ret):
    param_strs = []
    for p in params:
        if isinstance(p, tuple) and len(p) >= 2:
            pname, ptype = p[0], p[1]
            param_strs.append(f"{pname}: {_type_str(ptype)}")
        else:
            param_strs.append(str(p))
    ret_str = f" -> {_type_str(ret)}" if ret else ''
    return f"fn {name}({', '.join(param_strs)}){ret_str}"


def _type_str(t):
    if t is None:
        return 'void'
    if isinstance(t, str):
        return t
    if isinstance(t, tuple):
        if t[0] == 'ArrayType':
            return f"[{_type_str(t[1])}; {t[2]}]"
        if t[0] == 'RefType':
            return f"&{_type_str(t[1])}"
        return str(t)
    return str(t)


def _field_list(fields):
    result = []
    for f in fields:
        if isinstance(f, tuple) and len(f) >= 2:
            result.append({'name': f[0], 'type': _type_str(f[1])})
        elif isinstance(f, str):
            result.append({'name': f, 'type': '?'})
    return result


# ===== HTML 출력 =====

def generate_html(source_path, output_path=None):
    from lexer import lex
    from danha_parser import parse

    with open(source_path, 'r', encoding='utf-8') as f:
        source = f.read()

    tokens = lex(source)
    ast = parse(tokens, source)
    items = _collect_docs(ast[1])

    if output_path is None:
        output_path = os.path.splitext(source_path)[0] + '.html'

    module_name = os.path.splitext(os.path.basename(source_path))[0]
    html = _render_html(module_name, items)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    return output_path


def _render_html(module_name, items):
    esc = _html.escape
    parts = [f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>{esc(module_name)} — 단아 문서</title>
<style>
body {{font-family: 'Segoe UI', sans-serif; max-width: 900px; margin: 2rem auto; color: #222;}}
h1 {{border-bottom: 2px solid #3a7bd5; color: #3a7bd5;}}
h2 {{margin-top: 2rem; font-size: 1.1rem; color: #555;}}
.item {{border: 1px solid #ddd; border-radius: 6px; padding: 1rem; margin: 1rem 0;}}
.sig {{font-family: monospace; background: #f5f5f5; padding: 0.4rem 0.8rem; border-radius: 4px; font-size: 0.95rem;}}
.doc {{color: #444; margin-top: 0.6rem; white-space: pre-wrap;}}
.badge {{display: inline-block; font-size: 0.75rem; padding: 2px 8px; border-radius: 10px; margin-right: 4px; font-weight: bold;}}
.fn-badge {{background: #d0e8ff; color: #1a5fa8;}}
.struct-badge {{background: #d4f5d4; color: #2a7a2a;}}
.union-badge {{background: #f5e0d4; color: #8a3a1a;}}
.enum-badge {{background: #ede0f5; color: #6a2a8a;}}
.trait-badge {{background: #f5f0d0; color: #8a7a1a;}}
.component-badge {{background: #d4eaf5; color: #1a5a7a;}}
table {{border-collapse: collapse; width: 100%; margin-top: 0.5rem;}}
th, td {{text-align: left; padding: 0.3rem 0.6rem; border-bottom: 1px solid #eee; font-size: 0.9rem;}}
th {{background: #f9f9f9; font-weight: 600;}}
code {{font-family: monospace; background: #f0f0f0; padding: 1px 4px; border-radius: 3px;}}
</style>
</head>
<body>
<h1>{esc(module_name)}</h1>
''']

    fn_items = [i for i in items if i['kind'] == 'fn']
    struct_items = [i for i in items if i['kind'] in ('struct', 'union', 'component')]
    enum_items = [i for i in items if i['kind'] in ('enum', 'trait')]

    if fn_items:
        parts.append('<h2>함수</h2>')
        for item in fn_items:
            doc_html = f'<div class="doc">{esc(item["doc"])}</div>' if item["doc"] else ''
            parts.append(f'''<div class="item">
<span class="badge fn-badge">fn</span>
<code class="sig">{esc(item["signature"])}</code>
{doc_html}
</div>''')

    if struct_items:
        parts.append('<h2>타입</h2>')
        for item in struct_items:
            badge = item['kind']
            doc_html = f'<div class="doc">{esc(item["doc"])}</div>' if item["doc"] else ''
            fields = item.get('fields', [])
            field_rows = ''.join(
                f'<tr><td><code>{esc(f["name"])}</code></td><td><code>{esc(f["type"])}</code></td></tr>'
                for f in fields
            )
            field_table = f'<table><tr><th>필드</th><th>타입</th></tr>{field_rows}</table>' if fields else ''
            parts.append(f'''<div class="item">
<span class="badge {badge}-badge">{badge}</span>
<strong>{esc(item["name"])}</strong>
{doc_html}
{field_table}
</div>''')

    if enum_items:
        parts.append('<h2>열거형 / 트레이트</h2>')
        for item in enum_items:
            badge = item['kind']
            doc_html = f'<div class="doc">{esc(item["doc"])}</div>' if item["doc"] else ''
            variants = item.get('variants', [])
            vlist = ', '.join(f'<code>{esc(v)}</code>' for v in variants)
            vhtml = f'<div style="margin-top:0.4rem">{vlist}</div>' if vlist else ''
            parts.append(f'''<div class="item">
<span class="badge {badge}-badge">{badge}</span>
<strong>{esc(item["name"])}</strong>
{doc_html}
{vhtml}
</div>''')

    if not items:
        parts.append('<p><em>문서화된 항목이 없어. <code>///</code> 주석을 추가해봐.</em></p>')

    parts.append('</body></html>')
    return '\n'.join(parts)


# ===== Markdown 출력 =====

def generate_markdown(source_path, output_path=None):
    from lexer import lex
    from danha_parser import parse

    with open(source_path, 'r', encoding='utf-8') as f:
        source = f.read()

    tokens = lex(source)
    ast = parse(tokens, source)
    items = _collect_docs(ast[1])

    if output_path is None:
        output_path = os.path.splitext(source_path)[0] + '.md'

    module_name = os.path.splitext(os.path.basename(source_path))[0]
    md = _render_markdown(module_name, items)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)

    return output_path


def _render_markdown(module_name, items):
    lines = [f'# {module_name}\n']

    fn_items = [i for i in items if i['kind'] == 'fn']
    type_items = [i for i in items if i['kind'] in ('struct', 'union', 'component', 'enum', 'trait')]

    if fn_items:
        lines.append('## 함수\n')
        for item in fn_items:
            lines.append(f'### `{item["signature"]}`\n')
            if item['doc']:
                lines.append(f'{item["doc"]}\n')
            lines.append('')

    if type_items:
        lines.append('## 타입\n')
        for item in type_items:
            lines.append(f'### {item["kind"]} `{item["name"]}`\n')
            if item['doc']:
                lines.append(f'{item["doc"]}\n')
            fields = item.get('fields', [])
            if fields:
                lines.append('| 필드 | 타입 |')
                lines.append('| --- | --- |')
                for f in fields:
                    lines.append(f'| `{f["name"]}` | `{f["type"]}` |')
                lines.append('')
            variants = item.get('variants', [])
            if variants:
                lines.append('**Variants:** ' + ', '.join(f'`{v}`' for v in variants))
                lines.append('')

    return '\n'.join(lines)
