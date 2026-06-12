# danha_editor.py — 단아 게임 씬 에디터 (58단계)
#
# 사용법: danha editor [<씬파일.dhs>]
#
# tkinter 기반 씬 계층 에디터.
# 씬 파일(.dhs): JSON 형식으로 엔티티/컴포넌트 저장.

import os
import sys
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

DANHA_DIR = os.path.dirname(os.path.abspath(__file__))

# 씬 파일 포맷 예시:
# {
#   "scene": "MainScene",
#   "entities": [
#     {"id": 1, "name": "Player", "components": [
#       {"type": "Transform", "x": 0.0, "y": 0.0},
#       {"type": "Sprite", "texture": "player.png"}
#     ]}
#   ]
# }

_EMPTY_SCENE = {
    "scene": "NewScene",
    "entities": []
}


class SceneEditor:
    def __init__(self, root, scene_path=None):
        self.root = root
        self.root.title("단아 씬 에디터")
        self.root.geometry("900x600")
        self.scene_path = scene_path
        self.scene_data = None
        self._selected_entity = None
        self._dirty = False

        self._build_ui()

        if scene_path and os.path.exists(scene_path):
            self._load_scene(scene_path)
        else:
            self._new_scene()

    def _build_ui(self):
        # 메뉴
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="새 씬", command=self._new_scene, accelerator="Ctrl+N")
        file_menu.add_command(label="열기...", command=self._open_scene, accelerator="Ctrl+O")
        file_menu.add_command(label="저장", command=self._save_scene, accelerator="Ctrl+S")
        file_menu.add_command(label="다른 이름으로 저장...", command=self._save_as)
        file_menu.add_separator()
        file_menu.add_command(label="단아 코드 내보내기...", command=self._export_danha)
        file_menu.add_separator()
        file_menu.add_command(label="종료", command=self._quit)
        menubar.add_cascade(label="파일", menu=file_menu)

        entity_menu = tk.Menu(menubar, tearoff=0)
        entity_menu.add_command(label="엔티티 추가", command=self._add_entity)
        entity_menu.add_command(label="엔티티 삭제", command=self._delete_entity)
        entity_menu.add_separator()
        entity_menu.add_command(label="컴포넌트 추가", command=self._add_component)
        entity_menu.add_command(label="컴포넌트 삭제", command=self._delete_component)
        menubar.add_cascade(label="엔티티", menu=entity_menu)

        self.root.config(menu=menubar)

        # 키 바인딩
        self.root.bind('<Control-n>', lambda e: self._new_scene())
        self.root.bind('<Control-o>', lambda e: self._open_scene())
        self.root.bind('<Control-s>', lambda e: self._save_scene())

        # 레이아웃: 좌(계층) + 우(인스펙터)
        paned = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, sashrelief=tk.RAISED)
        paned.pack(fill=tk.BOTH, expand=True)

        # 왼쪽: 씬 계층 뷰
        left_frame = tk.Frame(paned, width=280)
        paned.add(left_frame, minsize=200)

        tk.Label(left_frame, text="씬 계층", font=('TkDefaultFont', 10, 'bold'),
                 anchor='w').pack(fill=tk.X, padx=5, pady=(5, 2))

        tree_frame = tk.Frame(left_frame)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=2)

        self.tree = ttk.Treeview(tree_frame, show='tree headings', selectmode='browse')
        self.tree['columns'] = ('type',)
        self.tree.heading('#0', text='이름')
        self.tree.heading('type', text='타입')
        self.tree.column('#0', width=160)
        self.tree.column('type', width=80)

        vsb = ttk.Scrollbar(tree_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind('<<TreeviewSelect>>', self._on_select)
        self.tree.bind('<Double-1>', self._on_double_click)

        # 하단 버튼
        btn_frame = tk.Frame(left_frame)
        btn_frame.pack(fill=tk.X, padx=5, pady=5)
        tk.Button(btn_frame, text="+ 엔티티", command=self._add_entity, width=10).pack(side=tk.LEFT)
        tk.Button(btn_frame, text="- 삭제", command=self._delete_entity, width=10).pack(side=tk.LEFT, padx=4)

        # 오른쪽: 인스펙터
        right_frame = tk.Frame(paned)
        paned.add(right_frame, minsize=300)

        tk.Label(right_frame, text="인스펙터", font=('TkDefaultFont', 10, 'bold'),
                 anchor='w').pack(fill=tk.X, padx=5, pady=(5, 2))

        # 인스펙터 내용 (스크롤 가능)
        canvas = tk.Canvas(right_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(right_frame, orient='vertical', command=canvas.yview)
        self.inspector = tk.Frame(canvas)
        self.inspector_win = canvas.create_window((0, 0), window=self.inspector, anchor='nw')

        def _on_frame_configure(e):
            canvas.configure(scrollregion=canvas.bbox('all'))
        def _on_canvas_configure(e):
            canvas.itemconfig(self.inspector_win, width=e.width)

        self.inspector.bind('<Configure>', _on_frame_configure)
        canvas.bind('<Configure>', _on_canvas_configure)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 상태바
        self.statusvar = tk.StringVar(value="준비")
        tk.Label(self.root, textvariable=self.statusvar, relief=tk.SUNKEN,
                 anchor='w').pack(side=tk.BOTTOM, fill=tk.X)

    # ===== 씬 관리 =====

    def _new_scene(self):
        if self._dirty and not messagebox.askyesno("저장", "변경 사항을 저장할까?"):
            return
        self.scene_data = json.loads(json.dumps(_EMPTY_SCENE))
        self.scene_path = None
        self._dirty = False
        self._refresh_tree()
        self._clear_inspector()
        self._set_status("새 씬 생성됨")

    def _open_scene(self):
        path = filedialog.askopenfilename(
            title="씬 열기", filetypes=[("단아 씬 파일", "*.dhs"), ("JSON", "*.json"), ("모든 파일", "*.*")])
        if path:
            self._load_scene(path)

    def _load_scene(self, path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                self.scene_data = json.load(f)
            self.scene_path = path
            self._dirty = False
            self._refresh_tree()
            self._clear_inspector()
            self._set_status(f"로드: {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("오류", f"씬 로드 실패: {e}")

    def _save_scene(self):
        if self.scene_path is None:
            self._save_as()
            return
        try:
            with open(self.scene_path, 'w', encoding='utf-8') as f:
                json.dump(self.scene_data, f, ensure_ascii=False, indent=2)
            self._dirty = False
            self._set_status(f"저장됨: {os.path.basename(self.scene_path)}")
        except Exception as e:
            messagebox.showerror("오류", f"저장 실패: {e}")

    def _save_as(self):
        path = filedialog.asksaveasfilename(
            title="다른 이름으로 저장",
            defaultextension='.dhs',
            filetypes=[("단아 씬 파일", "*.dhs"), ("JSON", "*.json"), ("모든 파일", "*.*")])
        if path:
            self.scene_path = path
            self._save_scene()

    # ===== 단아 코드 내보내기 =====

    def _export_danha(self):
        if self.scene_data is None:
            return
        path = filedialog.asksaveasfilename(
            title="단아 코드 내보내기",
            defaultextension='.dh',
            filetypes=[("단아 소스", "*.dh"), ("모든 파일", "*.*")])
        if not path:
            return

        try:
            code = self._scene_to_danha()
            with open(path, 'w', encoding='utf-8') as f:
                f.write(code)
            self._set_status(f"내보내기 완료: {os.path.basename(path)}")
            messagebox.showinfo("완료", f"단아 코드 내보내기 완료:\n{path}")
        except Exception as e:
            messagebox.showerror("오류", f"내보내기 실패: {e}")

    def _scene_to_danha(self):
        """씬 데이터를 단아 소스 코드로 변환."""
        scene_name = self.scene_data.get('scene', 'Scene')
        lines = [f'// 씬: {scene_name} (단아 씬 에디터에서 생성됨)', '']

        for entity in self.scene_data.get('entities', []):
            eid = entity.get('id', 0)
            name = entity.get('name', f'Entity{eid}')
            lines.append(f'// 엔티티: {name}')
            lines.append(f'let e{eid} = world.spawn()')

            for comp in entity.get('components', []):
                ctype = comp.get('type', 'Unknown')
                fields = {k: v for k, v in comp.items() if k != 'type'}
                if fields:
                    field_str = ', '.join(f'{k}: {json.dumps(v)}' for k, v in fields.items())
                    lines.append(f'world.add(e{eid}, {ctype} {{ {field_str} }})')
                else:
                    lines.append(f'world.add(e{eid}, {ctype} {{}})')
            lines.append('')

        return '\n'.join(lines)

    # ===== 엔티티/컴포넌트 편집 =====

    def _add_entity(self):
        name = simpledialog.askstring("엔티티 추가", "엔티티 이름:", initialvalue="Entity")
        if name is None:
            return
        entities = self.scene_data.setdefault('entities', [])
        new_id = max((e.get('id', 0) for e in entities), default=0) + 1
        entities.append({'id': new_id, 'name': name, 'components': []})
        self._dirty = True
        self._refresh_tree()
        self._set_status(f"엔티티 추가: {name}")

    def _delete_entity(self):
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        # 루트 아이템(엔티티)만 삭제
        if self.tree.parent(item) != '':
            messagebox.showinfo("안내", "엔티티를 선택해서 삭제해줘.")
            return
        entity_id = self.tree.item(item, 'tags')
        if entity_id:
            eid = int(entity_id[0])
            entities = self.scene_data.get('entities', [])
            self.scene_data['entities'] = [e for e in entities if e.get('id') != eid]
            self._dirty = True
            self._refresh_tree()
            self._clear_inspector()
            self._set_status(f"엔티티 삭제 (id={eid})")

    def _add_component(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("안내", "컴포넌트를 추가할 엔티티를 선택해줘.")
            return

        item = sel[0]
        # 컴포넌트 항목이 선택된 경우 부모 엔티티로 올라감
        parent = self.tree.parent(item)
        entity_item = item if parent == '' else parent
        tags = self.tree.item(entity_item, 'tags')
        if not tags:
            return
        eid = int(tags[0])

        comp_type = simpledialog.askstring("컴포넌트 추가", "컴포넌트 타입:",
                                           initialvalue="Transform")
        if comp_type is None:
            return

        entity = next((e for e in self.scene_data.get('entities', [])
                       if e.get('id') == eid), None)
        if entity is None:
            return

        # 기본 필드 설정
        defaults = {
            'Transform': {'x': 0.0, 'y': 0.0, 'rotation': 0.0, 'scale_x': 1.0, 'scale_y': 1.0},
            'Sprite': {'texture': 'default.png', 'width': 64, 'height': 64},
            'Velocity': {'vx': 0.0, 'vy': 0.0},
            'Collider': {'width': 32.0, 'height': 32.0},
            'Health': {'hp': 100, 'max_hp': 100},
        }
        comp = {'type': comp_type}
        comp.update(defaults.get(comp_type, {}))
        entity.setdefault('components', []).append(comp)
        self._dirty = True
        self._refresh_tree()
        # 해당 엔티티 다시 선택
        for child in self.tree.get_children():
            if self.tree.item(child, 'tags') and int(self.tree.item(child, 'tags')[0]) == eid:
                self.tree.selection_set(child)
                self._on_select(None)
                break
        self._set_status(f"컴포넌트 추가: {comp_type}")

    def _delete_component(self):
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        parent = self.tree.parent(item)
        if parent == '':
            messagebox.showinfo("안내", "삭제할 컴포넌트를 선택해줘.")
            return

        # 부모 엔티티 id
        entity_tags = self.tree.item(parent, 'tags')
        if not entity_tags:
            return
        eid = int(entity_tags[0])

        # 컴포넌트 인덱스
        comp_tags = self.tree.item(item, 'tags')
        if not comp_tags:
            return
        try:
            comp_idx = int(comp_tags[0].split(':')[1])
        except (IndexError, ValueError):
            return

        entity = next((e for e in self.scene_data.get('entities', [])
                       if e.get('id') == eid), None)
        if entity and 0 <= comp_idx < len(entity.get('components', [])):
            del entity['components'][comp_idx]
            self._dirty = True
            self._refresh_tree()
            self._clear_inspector()
            self._set_status("컴포넌트 삭제됨")

    # ===== 트리 갱신 =====

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        if self.scene_data is None:
            return
        for entity in self.scene_data.get('entities', []):
            eid = entity.get('id', 0)
            name = entity.get('name', f'Entity{eid}')
            parent_item = self.tree.insert('', tk.END, text=name, values=('Entity',),
                                           tags=(str(eid),))
            for i, comp in enumerate(entity.get('components', [])):
                ctype = comp.get('type', '?')
                self.tree.insert(parent_item, tk.END, text=f'  {ctype}',
                                 values=(ctype,), tags=(f'{eid}:{i}',))
            self.tree.item(parent_item, open=True)

    # ===== 인스펙터 =====

    def _clear_inspector(self):
        for w in self.inspector.winfo_children():
            w.destroy()
        self._selected_entity = None

    def _on_select(self, event):
        sel = self.tree.selection()
        if not sel:
            self._clear_inspector()
            return
        item = sel[0]
        parent = self.tree.parent(item)

        if parent == '':
            # 엔티티 선택
            tags = self.tree.item(item, 'tags')
            if tags:
                eid = int(tags[0])
                entity = next((e for e in self.scene_data.get('entities', [])
                                if e.get('id') == eid), None)
                if entity:
                    self._show_entity_inspector(entity)
        else:
            # 컴포넌트 선택
            comp_tags = self.tree.item(item, 'tags')
            entity_tags = self.tree.item(parent, 'tags')
            if comp_tags and entity_tags:
                eid = int(entity_tags[0])
                try:
                    comp_idx = int(comp_tags[0].split(':')[1])
                except (IndexError, ValueError):
                    return
                entity = next((e for e in self.scene_data.get('entities', [])
                                if e.get('id') == eid), None)
                if entity and 0 <= comp_idx < len(entity.get('components', [])):
                    self._show_component_inspector(entity, comp_idx)

    def _on_double_click(self, event):
        """더블클릭 시 엔티티 이름 변경."""
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        if self.tree.parent(item) != '':
            return
        tags = self.tree.item(item, 'tags')
        if not tags:
            return
        eid = int(tags[0])
        entity = next((e for e in self.scene_data.get('entities', [])
                        if e.get('id') == eid), None)
        if entity is None:
            return
        new_name = simpledialog.askstring("이름 변경", "새 이름:", initialvalue=entity.get('name', ''))
        if new_name:
            entity['name'] = new_name
            self._dirty = True
            self._refresh_tree()

    def _show_entity_inspector(self, entity):
        self._clear_inspector()
        self._selected_entity = entity

        tk.Label(self.inspector, text=f"엔티티: {entity.get('name', '?')}",
                 font=('TkDefaultFont', 10, 'bold')).grid(row=0, column=0, columnspan=2,
                                                           sticky='w', padx=8, pady=(8, 4))
        tk.Label(self.inspector, text=f"ID: {entity.get('id', 0)}").grid(
            row=1, column=0, columnspan=2, sticky='w', padx=8)

        n_comps = len(entity.get('components', []))
        tk.Label(self.inspector, text=f"컴포넌트: {n_comps}개").grid(
            row=2, column=0, columnspan=2, sticky='w', padx=8, pady=(0, 8))

        tk.Button(self.inspector, text="+ 컴포넌트 추가",
                  command=self._add_component).grid(row=3, column=0, padx=8, pady=4, sticky='w')

    def _show_component_inspector(self, entity, comp_idx):
        self._clear_inspector()
        comp = entity['components'][comp_idx]

        tk.Label(self.inspector, text=f"컴포넌트: {comp.get('type', '?')}",
                 font=('TkDefaultFont', 10, 'bold')).grid(row=0, column=0, columnspan=2,
                                                           sticky='w', padx=8, pady=(8, 4))

        self._field_vars = {}
        row = 1
        for key, val in comp.items():
            if key == 'type':
                continue
            tk.Label(self.inspector, text=f'{key}:').grid(
                row=row, column=0, sticky='e', padx=(8, 4), pady=2)
            var = tk.StringVar(value=str(val))
            self._field_vars[key] = (var, type(val))
            entry = tk.Entry(self.inspector, textvariable=var, width=20)
            entry.grid(row=row, column=1, sticky='w', padx=(0, 8), pady=2)
            entry.bind('<Return>', lambda e, k=key, v=var, t=type(val), c=comp:
                       self._apply_field(c, k, v, t))
            entry.bind('<FocusOut>', lambda e, k=key, v=var, t=type(val), c=comp:
                       self._apply_field(c, k, v, t))
            row += 1

        tk.Button(self.inspector, text="적용", command=self._apply_all_fields).grid(
            row=row, column=0, columnspan=2, pady=8)

    def _apply_field(self, comp, key, var, val_type):
        try:
            raw = var.get()
            if val_type == int:
                comp[key] = int(raw)
            elif val_type == float:
                comp[key] = float(raw)
            elif val_type == bool:
                comp[key] = raw.lower() in ('true', '1', 'yes')
            else:
                comp[key] = raw
            self._dirty = True
        except ValueError:
            pass

    def _apply_all_fields(self):
        if not hasattr(self, '_field_vars'):
            return
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        parent = self.tree.parent(item)
        if parent == '':
            return
        comp_tags = self.tree.item(item, 'tags')
        entity_tags = self.tree.item(parent, 'tags')
        if not comp_tags or not entity_tags:
            return
        eid = int(entity_tags[0])
        try:
            comp_idx = int(comp_tags[0].split(':')[1])
        except (IndexError, ValueError):
            return
        entity = next((e for e in self.scene_data.get('entities', [])
                        if e.get('id') == eid), None)
        if entity is None or comp_idx >= len(entity.get('components', [])):
            return
        comp = entity['components'][comp_idx]
        for key, (var, val_type) in self._field_vars.items():
            self._apply_field(comp, key, var, val_type)
        self._set_status("변경 사항 적용됨")

    # ===== 유틸 =====

    def _set_status(self, msg):
        dirty_mark = ' *' if self._dirty else ''
        scene_name = (self.scene_data or {}).get('scene', '씬')
        self.statusvar.set(f"{scene_name}{dirty_mark} | {msg}")

    def _quit(self):
        if self._dirty and not messagebox.askyesno("종료", "저장되지 않은 변경 사항이 있어. 종료할까?"):
            return
        self.root.destroy()


def run_editor(scene_path=None):
    """씬 에디터를 실행한다."""
    root = tk.Tk()
    app = SceneEditor(root, scene_path=scene_path)
    root.protocol("WM_DELETE_WINDOW", app._quit)
    root.mainloop()
    return 0


def main(args):
    scene_path = args[0] if args else None
    try:
        return run_editor(scene_path)
    except tk.TclError as e:
        print(f"❌ GUI를 시작할 수 없어: {e}")
        print("  tkinter가 설치되어 있는지 확인해줘.")
        return 1
    except Exception as e:
        print(f"❌ 에디터 오류: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
