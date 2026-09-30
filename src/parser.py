"""Fase 1 — Parser: estrae nodi e archi da una codebase Python usando `ast`.

Nodi: module, class, function, method
Archi: defines, imports, inherits, calls

La risoluzione di chiamate/ereditarietà è statica e "best effort": ciò che non si
riesce a risolvere finisce in `unresolved`, classificato come `external`
(libreria fuori dal repo) o `dynamic` (metodo su oggetto di tipo ignoto).
"""

import argparse
import ast
import builtins
import json
import sys
from collections import defaultdict
from pathlib import Path

BUILTINS = set(dir(builtins))
DEFAULT_EXCLUDE = {"tests", "benchmarks", "examples", "docs", "build", "dist"}


# --------------------------------------------------------------------------- #
# Estrazione per singolo file
# --------------------------------------------------------------------------- #

def module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def dotted(node):
    """Ricostruisce 'a.b.c' da una catena Name/Attribute; 'super().x' per super()."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
          and node.func.id == "super"):
        parts.append("super()")
    else:
        return None
    return ".".join(reversed(parts))


class FileExtractor:
    def __init__(self, path: Path, root: Path):
        self.path = path
        self.rel = str(path.relative_to(root))
        self.module = module_name(path, root)
        self.is_package = path.name == "__init__.py"
        self.source = path.read_text(encoding="utf-8")
        self.lines = self.source.splitlines()
        self.tree = ast.parse(self.source, filename=str(path))
        self.nodes = []
        self.defines = []           # (parent_id, child_id)
        self.imports = defaultdict(list)   # alias -> [dotted target] (più valori: import condizionali)
        self.local_alias = defaultdict(list)  # `X = Y` a livello di modulo -> X: [Y]
        self.import_edges = []      # (target_dotted, line)
        self.calls = {}             # node_id -> [(dotted_call, line)]
        self.bases = {}             # class_id -> [dotted base]
        self.class_of = {}          # method_id -> class_id

    # -- helpers ----------------------------------------------------------- #
    def _code(self, node):
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        return "\n".join(self.lines[start - 1:node.end_lineno])

    def _add(self, node, qualname, ntype, parent_id):
        nid = f"{self.module}:{qualname}"
        self.nodes.append({
            "id": nid, "type": ntype, "name": node.name, "qualname": qualname,
            "module": self.module, "file": self.rel,
            "line": node.lineno, "end_line": node.end_lineno,
            "docstring": ast.get_docstring(node),
            "code": self._code(node),
        })
        self.defines.append((parent_id, nid))
        return nid

    def _resolve_relative(self, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        base = self.module.split(".") if self.module else []
        if not self.is_package:
            base = base[:-1]
        base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
        return ".".join(base + ([node.module] if node.module else []))

    # -- visita ------------------------------------------------------------ #
    def run(self):
        self.nodes.append({
            "id": self.module, "type": "module", "name": self.module.split(".")[-1],
            "qualname": self.module, "module": self.module, "file": self.rel,
            "line": 1, "end_line": len(self.lines),
            "docstring": ast.get_docstring(self.tree), "code": self.source,
        })
        self._collect_imports()
        self._visit_body(self.tree.body, prefix="", parent_id=self.module, cls_id=None)
        return self

    def _collect_imports(self):
        # Tutti gli import (anche dentro funzioni / try-except): alias a livello di modulo.
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self._add_import(a.asname, a.name)
                    else:
                        self._add_import(a.name.split(".")[0], a.name.split(".")[0])
                    self.import_edges.append((a.name, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                base = self._resolve_relative(node)
                for a in node.names:
                    if a.name == "*":
                        self.import_edges.append((base, node.lineno))
                        continue
                    self._add_import(a.asname or a.name, f"{base}.{a.name}" if base else a.name)
                    self.import_edges.append((f"{base}.{a.name}" if base else a.name, node.lineno))

    def _add_import(self, alias, target):
        if target not in self.imports[alias]:
            self.imports[alias].append(target)

    def _visit_body(self, body, prefix, parent_id, cls_id):
        for node in body:
            if (isinstance(node, ast.Assign) and not prefix and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name) and dotted(node.value)):
                self.local_alias[node.targets[0].id].append(dotted(node.value))
            elif isinstance(node, ast.ClassDef):
                qn = prefix + node.name
                cid = self._add(node, qn, "class", parent_id)
                self.bases[cid] = [b for b in map(dotted, node.bases) if b]
                self._visit_body(node.body, qn + ".", cid, cid)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qn = prefix + node.name
                ntype = "method" if cls_id else "function"
                fid = self._add(node, qn, ntype, parent_id)
                if cls_id:
                    self.class_of[fid] = cls_id
                self.calls[fid] = self._collect_calls(node)
            elif hasattr(node, "body"):
                # if/try/with/for a livello di modulo o classe: definizioni condizionali
                for field in ("body", "orelse", "finalbody"):
                    self._visit_body(getattr(node, field, []) or [], prefix, parent_id, cls_id)
                for h in getattr(node, "handlers", []):
                    self._visit_body(h.body, prefix, parent_id, cls_id)

    def _collect_calls(self, fn):
        calls = []
        for n in ast.walk(fn):  # include funzioni annidate/lambda: attribuite al parent
            if isinstance(n, ast.Call):
                name = dotted(n.func)
                if name:
                    calls.append((name, n.lineno))
        return calls


# --------------------------------------------------------------------------- #
# Risoluzione cross-file
# --------------------------------------------------------------------------- #

class Resolver:
    def __init__(self, files):
        self.files = {f.module: f for f in files}
        self.nodes = {n["id"]: n for f in files for n in f.nodes}
        self.members = defaultdict(dict)   # parent_id -> {name: child_id}
        for f in files:
            for parent, child in f.defines:
                self.members[parent][self.nodes[child]["name"]] = child
        self.class_of = {k: v for f in files for k, v in f.class_of.items()}
        self.class_module = {n["id"]: n["module"] for n in self.nodes.values() if n["type"] == "class"}
        self._bases_cache = {}

    # Simbolo top-level di un modulo: definito lì, alias `X = Y`, oppure re-export via import.
    # Ritorna un insieme di id (più valori con definizioni/import condizionali).
    def lookup_symbol(self, f, name, seen=None):
        seen = seen if seen is not None else set()
        if (f.module, name) in seen:
            return set()
        seen.add((f.module, name))
        out = set()
        if name in self.members.get(f.module, {}):
            out.add(self.members[f.module][name])
        for a in f.local_alias.get(name, []):
            out |= self.resolve_name(a, f, None, seen)
        for t in f.imports.get(name, []):
            out |= self.resolve_dotted(t, seen)
        return out

    def step(self, cur, part, seen):
        """Un passo di risoluzione 'cur.part' partendo da un nodo già risolto."""
        node = self.nodes[cur]
        if node["type"] == "module":
            out = self.lookup_symbol(self.files[cur], part, seen)
            sub = f"{cur}.{part}" if cur else part
            if not out and sub in self.nodes:
                out.add(sub)
            return out
        if node["type"] == "class":
            m = self.find_method(cur, part)
            return {m} if m else set()
        return set()

    # Risolve un nome dotted ('tqdm.std.tqdm') in id di nodi interni, seguendo alias e re-export.
    def resolve_dotted(self, target, seen=None):
        seen = seen if seen is not None else set()
        if target in self.nodes:
            return {target}
        parts = target.split(".")
        for i in range(len(parts) - 1, 0, -1):
            mod = ".".join(parts[:i])
            if mod not in self.files:
                continue
            cur = {mod}
            for part in parts[i:]:
                cur = set().union(*[self.step(c, part, seen) for c in cur]) if cur else set()
            return cur
        return set()

    def is_external(self, dotted_target):
        return dotted_target.split(".")[0] not in {m.split(".")[0] for m in self.files}

    def base_ids(self, class_id):
        """Basi risolte + basi esterne di una classe."""
        f = self.files[self.nodes[class_id]["module"]]
        resolved, external = [], []
        for b in f.bases.get(class_id, []):
            r = {x for x in self.resolve_name(b, f, class_id) if self.nodes[x]["type"] == "class"}
            if r:
                resolved.extend(sorted(r))
            else:
                external.append(b)
        return resolved, external

    def mro(self, class_id):
        if class_id in self._bases_cache:
            return self._bases_cache[class_id]
        order, queue = [], [class_id]
        while queue:
            c = queue.pop(0)
            if c in order:
                continue
            order.append(c)
            queue.extend(self.base_ids(c)[0])
        self._bases_cache[class_id] = order
        return order

    def descendants(self, class_id):
        """Tutte le sottoclassi (dirette e indirette) di una classe."""
        if not hasattr(self, "_subclasses"):
            self._subclasses = defaultdict(set)
            for c, n in self.nodes.items():
                if n["type"] == "class":
                    for b in self.base_ids(c)[0]:
                        self._subclasses[b].add(c)
        out, queue = set(), [class_id]
        while queue:
            for sub in self._subclasses.get(queue.pop(), ()):
                if sub not in out:
                    out.add(sub)
                    queue.append(sub)
        return out

    def find_in_descendants(self, class_id, name):
        """Definizioni di `name` nelle sottoclassi (per i template method: la base chiama, la sottoclasse definisce)."""
        return {m for d in self.descendants(class_id) if (m := self.members.get(d, {}).get(name))}

    def find_method(self, class_id, name):
        for c in self.mro(class_id):
            m = self.members.get(c, {}).get(name)
            if m:
                return m
        return None

    # Risolve un nome (eventualmente dotted) nel contesto di un file. Ritorna un insieme di id.
    def resolve_name(self, name, f, class_id=None, seen=None):
        seen = seen if seen is not None else set()
        first, *rest = name.split(".")
        if first == "super()" and class_id and rest:
            for base in self.base_ids(class_id)[0]:
                m = self.find_method(base, rest[0])
                if m:
                    return {m}
            return set()
        if first in ("self", "cls") and class_id and len(rest) == 1:
            m = self.find_method(class_id, rest[0])
            return {m} if m else self.find_in_descendants(class_id, rest[0])
        cur = self.lookup_symbol(f, first, seen)
        for part in rest:
            cur = set().union(*[self.step(c, part, seen) for c in cur]) if cur else set()
        return cur

    def classify_unresolved(self, name, f):
        first = name.split(".")[0]
        if first in BUILTINS and first not in f.imports:
            return "builtin"
        if first in f.imports and all(self.is_external(t) for t in f.imports[first]):
            return "external"
        return "dynamic"


# --------------------------------------------------------------------------- #
# Costruzione del grafo
# --------------------------------------------------------------------------- #

def iter_py_files(root: Path, exclude):
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).parts
        if any(part.startswith(".") for part in rel) or rel[0] in exclude:
            continue
        yield p


def build_graph(root: Path, exclude=DEFAULT_EXCLUDE):
    files, errors = [], []
    for p in iter_py_files(root, exclude):
        try:
            files.append(FileExtractor(p, root).run())
        except (SyntaxError, UnicodeDecodeError) as e:
            errors.append({"file": str(p.relative_to(root)), "error": str(e)})

    r = Resolver(files)
    edges = {}          # (src, tgt, type) -> edge dict
    unresolved = []
    external_imports = defaultdict(list)

    def add_edge(src, tgt, etype, line=None):
        e = edges.setdefault((src, tgt, etype), {"source": src, "target": tgt, "type": etype})
        if line is not None:
            e.setdefault("lines", []).append(line)

    for f in files:
        for parent, child in f.defines:
            add_edge(parent, child, "defines")

        for target, line in f.import_edges:
            resolved = r.resolve_dotted(target)
            if not resolved:
                # nome non-nodo (costante/variabile): l'arco va al modulo che lo espone
                mod = next((".".join(target.split(".")[:i]) for i in range(len(target.split(".")) - 1, 0, -1)
                            if ".".join(target.split(".")[:i]) in r.files), None)
                resolved = {mod} if mod else set()
            for t in resolved:
                add_edge(f.module, t, "imports", line)
            if resolved:
                continue
            if r.is_external(target):
                external_imports[f.module].append(target.split(".")[0])
            else:
                unresolved.append({"source": f.module, "name": target, "kind": "import", "line": line})

        for cid, bases in f.bases.items():
            resolved_bases, external_bases = r.base_ids(cid)
            for b in resolved_bases:
                add_edge(cid, b, "inherits")
            if external_bases:
                r.nodes[cid]["external_bases"] = external_bases

        for src, calls in f.calls.items():
            cls = r.class_of.get(src)
            for name, line in calls:
                tgts = r.resolve_name(name, f, cls)
                for tgt in sorted(tgts - {src}):
                    add_edge(src, tgt, "calls", line)
                    if r.nodes[tgt]["type"] == "class":   # istanziare una classe esegue il suo __init__
                        init = r.find_method(tgt, "__init__")
                        if init and init != src:
                            add_edge(src, init, "calls", line)
                if not tgts:
                    kind = r.classify_unresolved(name, f)
                    if kind != "builtin":
                        unresolved.append({"source": src, "name": name, "kind": kind, "line": line})

    nodes = list(r.nodes.values())
    for n in nodes:
        if n["type"] == "module":
            n["external_imports"] = sorted(set(external_imports.get(n["id"], [])))
    stats = {
        "nodes": {t: sum(n["type"] == t for n in nodes) for t in ("module", "class", "function", "method")},
        "edges": {t: sum(k[2] == t for k in edges) for t in ("defines", "imports", "inherits", "calls")},
        "unresolved": {k: sum(u["kind"] == k for u in unresolved) for k in ("external", "dynamic", "import")},
        "parse_errors": len(errors),
    }
    return {"repo": root.name, "nodes": nodes, "edges": list(edges.values()),
            "unresolved": unresolved, "errors": errors, "stats": stats}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Estrae il grafo (nodi + archi) di una codebase Python.")
    ap.add_argument("repo", type=Path, help="root del repo da analizzare")
    ap.add_argument("-o", "--output", type=Path, default=Path("output/graph.json"))
    ap.add_argument("--exclude", default=",".join(sorted(DEFAULT_EXCLUDE)),
                    help="directory top-level da escludere, separate da virgola")
    args = ap.parse_args(argv)

    graph = build_graph(args.repo.resolve(), set(filter(None, args.exclude.split(","))))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(graph["stats"], indent=2))
    print(f"-> {args.output}")


if __name__ == "__main__":
    sys.exit(main())
