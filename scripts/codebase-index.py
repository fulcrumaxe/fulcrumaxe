#!/usr/bin/env python3
"""Index Python and TypeScript symbol trees; write .autonomous-team/codebase-index.json."""
from __future__ import annotations
import argparse, ast, json, re
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).parent.parent
PY_ROOTS     = [REPO / "backend", REPO / "scripts", REPO / "hooks"]
TS_ROOT      = REPO / "tui" / "src"
DEFAULT_OUT  = REPO / ".autonomous-team" / "codebase-index.json"


class _Visitor(ast.NodeVisitor):
    def __init__(self):
        self.classes, self.functions, self.constants = [], [], []

    def visit_ClassDef(self, node):
        bases = [ast.unparse(b) for b in node.bases]
        is_dc = any(
            (isinstance(d, ast.Name) and d.id == "dataclass") or
            (isinstance(d, ast.Attribute) and d.attr == "dataclass")
            for d in node.decorator_list)
        entry = {"name": node.name, "bases": bases}
        if is_dc:
            entry["is_dataclass"] = True
            fields = []
            for s in node.body:
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
                    f = {"name": s.target.id, "type": ast.unparse(s.annotation)}
                    if s.value is not None:
                        try: f["default"] = ast.literal_eval(s.value)
                        except Exception: f["default"] = ast.unparse(s.value)
                    fields.append(f)
            entry["fields"] = fields
        if any("Enum" in b for b in bases):
            entry["members"] = [t.id for s in node.body if isinstance(s, ast.Assign)
                                 for t in s.targets if isinstance(t, ast.Name)]
        self.classes.append(entry)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        if node.col_offset != 0: return
        params = []
        for a in node.args.args:
            p = {"name": a.arg}
            if a.annotation: p["type"] = ast.unparse(a.annotation)
            params.append(p)
        fn = {"name": node.name, "params": params}
        if node.returns: fn["returns"] = ast.unparse(node.returns)
        self.functions.append(fn)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Assign(self, node):
        if node.col_offset != 0: return
        try: val = ast.literal_eval(node.value)
        except Exception: return
        if isinstance(val, (str, int, float, list, bool)):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    self.constants.append({"name": t.id, "value": val})


def _parse_py(path: Path) -> dict:
    try: tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return {"parse_error": True, "classes": [], "functions": [], "constants": []}
    v = _Visitor(); v.visit(tree)
    return {"classes": v.classes, "functions": v.functions, "constants": v.constants}

def collect_python(root: Path) -> dict[str, dict]:
    if not root.exists():
        return {}
    return {str(p.relative_to(REPO)): _parse_py(p)
            for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts}


def _parse_ts(path: Path) -> dict:
    src = path.read_text(encoding="utf-8", errors="replace")
    interfaces = []
    for m in re.finditer(r"export\s+interface\s+(\w+)[^{]*\{([^}]*)\}", src, re.DOTALL):
        fields = [{"name": fm.group(1), "type": fm.group(2).strip()}
                  for fm in re.finditer(r"(\w+)\??:\s*([^;\n]+)", m.group(2))]
        interfaces.append({"name": m.group(1), "fields": fields})
    type_aliases = [{"name": m.group(1), "definition": m.group(2).strip()}
                    for m in re.finditer(r"export\s+type\s+(\w+)\s*=\s*([^;]+);", src)]
    functions = [{"name": m.group(1), "params": m.group(2).strip(),
                  **({"returns": m.group(3).strip()} if m.group(3) else {})}
                 for m in re.finditer(r"export\s+(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)(?:\s*:\s*([^{;\n]+))?", src)]
    functions += [{"name": m.group(1), "params": m.group(2).strip()}
                  for m in re.finditer(r"export\s+const\s+(\w+)\s*(?::[^=]+)?\s*=\s*(?:async\s*)?\(([^)]*)\)", src)]
    return {"interfaces": interfaces, "type_aliases": type_aliases, "functions": functions}

def collect_typescript(root: Path) -> dict[str, dict]:
    return {str(p.relative_to(REPO)): _parse_ts(p)
            for p in sorted(root.rglob("*.ts")) + sorted(root.rglob("*.tsx"))}

def main():
    ap = argparse.ArgumentParser(description="Index codebase symbols")
    ap.add_argument("--output", default=str(DEFAULT_OUT))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    py = {}
    for root in PY_ROOTS:
        py.update(collect_python(root))
    ts = collect_typescript(TS_ROOT) if TS_ROOT.exists() else {}

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python": py,
        "typescript": ts,
    }, indent=2), encoding="utf-8")

    if not args.quiet:
        print(f"Indexed {len(py)} Python files, {len(ts)} TypeScript files")

if __name__ == "__main__":
    main()
