# danha_pkg.py
# 47단계: 단아 패키지 매니저
#
# danha.toml 형식:
#   [package]
#   name = "my_game"
#   version = "0.1.0"
#
#   [dependencies]
#   ari_physics = { path = "./libs/ari_physics" }
#   ari_audio = { path = "./libs/ari_audio" }

import os
import re
import sys


# ===== TOML 파서 (최소 구현 — 외부 의존 없음) =====

def _parse_toml(text):
    """단순 TOML 파서 (단아 패키지 매니저에서 필요한 부분만)."""
    result = {}
    current_section = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        # 주석·빈 줄 스킵
        if not line or line.startswith('#'):
            continue

        # 섹션 헤더: [package] / [dependencies]
        m = re.match(r'^\[([^\]]+)\]$', line)
        if m:
            current_section = m.group(1).strip()
            if current_section not in result:
                result[current_section] = {}
            continue

        # key = value
        if '=' in line:
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip()

            parsed = _parse_toml_value(val)

            if current_section is None:
                result[key] = parsed
            else:
                result[current_section][key] = parsed

    return result


def _parse_toml_value(val):
    """TOML 값 파싱 (문자열, 숫자, 인라인 테이블)."""
    # 문자열
    if (val.startswith('"') and val.endswith('"')) or \
       (val.startswith("'") and val.endswith("'")):
        return val[1:-1]

    # 불리언
    if val == 'true':
        return True
    if val == 'false':
        return False

    # 숫자
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass

    # 인라인 테이블: { path = "./libs/ari_physics" }
    if val.startswith('{') and val.endswith('}'):
        inner = val[1:-1].strip()
        table = {}
        for pair in _split_inline_table(inner):
            k, _, v = pair.partition('=')
            table[k.strip()] = _parse_toml_value(v.strip())
        return table

    return val


def _split_inline_table(s):
    """인라인 테이블 내부를 쉼표로 분리 (인용 부호 안은 무시)."""
    parts = []
    current = ''
    in_str = False
    str_char = ''
    for ch in s:
        if in_str:
            current += ch
            if ch == str_char:
                in_str = False
        elif ch in ('"', "'"):
            in_str = True
            str_char = ch
            current += ch
        elif ch == ',':
            parts.append(current.strip())
            current = ''
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


# ===== danha.toml 로드 =====

def load_manifest(project_dir=None):
    """danha.toml을 읽어 파싱된 딕셔너리 반환."""
    if project_dir is None:
        project_dir = os.getcwd()

    toml_path = os.path.join(project_dir, 'danha.toml')
    if not os.path.exists(toml_path):
        return None

    with open(toml_path, 'r', encoding='utf-8') as f:
        return _parse_toml(f.read())


def resolve_dependencies(manifest, project_dir):
    """
    [dependencies] 섹션을 읽어 모듈 이름 → 실제 경로 매핑을 반환.
    반환: {module_name: absolute_path}
    """
    deps = manifest.get('dependencies', {})
    resolved = {}

    for name, spec in deps.items():
        if isinstance(spec, dict) and 'path' in spec:
            raw_path = spec['path']
            abs_path = os.path.normpath(os.path.join(project_dir, raw_path))
            resolved[name] = abs_path
        elif isinstance(spec, str):
            # 단순 버전 문자열 — 로컬 패키지로 해석 (레지스트리 미구현)
            local = os.path.join(project_dir, name)
            if os.path.isdir(local):
                resolved[name] = local

    return resolved


# ===== CLI 명령어 =====

def cmd_init(project_dir=None):
    """danha pkg init — danha.toml 생성."""
    if project_dir is None:
        project_dir = os.getcwd()

    toml_path = os.path.join(project_dir, 'danha.toml')
    if os.path.exists(toml_path):
        print(f"이미 danha.toml이 있어: {toml_path}")
        return 1

    name = os.path.basename(os.path.abspath(project_dir))
    content = f'[package]\nname = "{name}"\nversion = "0.1.0"\n\n[dependencies]\n'
    with open(toml_path, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f"✅ danha.toml 생성 완료: {toml_path}")
    return 0


def cmd_add(package_name, path=None, project_dir=None):
    """danha pkg add <name> [--path <경로>] — 의존성 추가."""
    if project_dir is None:
        project_dir = os.getcwd()

    toml_path = os.path.join(project_dir, 'danha.toml')
    if not os.path.exists(toml_path):
        print("❌ danha.toml이 없어. 먼저 'danha pkg init'을 실행해줘")
        return 1

    with open(toml_path, 'r', encoding='utf-8') as f:
        content = f.read()

    if path:
        entry = f'{package_name} = {{ path = "{path}" }}'
    else:
        entry = f'{package_name} = "*"'

    # [dependencies] 섹션에 추가
    if '[dependencies]' in content:
        content = content.rstrip() + f'\n{entry}\n'
    else:
        content = content.rstrip() + f'\n\n[dependencies]\n{entry}\n'

    with open(toml_path, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f"✅ 의존성 추가: {entry}")
    return 0


def cmd_list(project_dir=None):
    """danha pkg list — 현재 의존성 목록 출력."""
    if project_dir is None:
        project_dir = os.getcwd()

    manifest = load_manifest(project_dir)
    if manifest is None:
        print("❌ danha.toml이 없어")
        return 1

    pkg = manifest.get('package', {})
    print(f"📦 {pkg.get('name', '?')} v{pkg.get('version', '?')}")

    deps = manifest.get('dependencies', {})
    if not deps:
        print("  의존성 없음")
    else:
        for name, spec in deps.items():
            if isinstance(spec, dict) and 'path' in spec:
                print(f"  - {name} (path: {spec['path']})")
            else:
                print(f"  - {name} ({spec})")
    return 0


def main(args):
    """danha pkg <sub-command> 처리."""
    if not args:
        print("사용법: danha pkg <init|add|list>")
        print("  init              — danha.toml 생성")
        print("  add <name> [--path <경로>]  — 의존성 추가")
        print("  list              — 의존성 목록")
        return 1

    sub = args[0]

    if sub == 'init':
        return cmd_init()

    elif sub == 'add':
        if len(args) < 2:
            print("❌ 패키지 이름을 지정해줘: danha pkg add <name>")
            return 1
        name = args[1]
        path = None
        for i, a in enumerate(args[2:], 2):
            if a == '--path' and i + 1 < len(args):
                path = args[i + 1]
        return cmd_add(name, path=path)

    elif sub == 'list':
        return cmd_list()

    else:
        print(f"❌ 알 수 없는 pkg 명령: {sub}")
        return 1
