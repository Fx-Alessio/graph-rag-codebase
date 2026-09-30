"""Fase 4 — Retrieval ibrido: dato un target, il contesto minimo ma completo.

1. Trova il nodo target (id / qualname / suffisso).
2. Espande sul grafo: chi il target chiama (callee), chi lo chiama (caller), classe che lo contiene,
   basi/sottoclassi, override.
3. Ordina per rilevanza = grafo (distanza + tipo di relazione) combinato con la similarità semantica.
4. Compone un testo entro un budget di caratteri: codice completo per i più rilevanti, firma per gli altri.
"""

from collections import deque
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from .semantic import DEFAULT_DB, DEFAULT_MODEL, signature

# Peso della relazione nel punteggio di grafo: cosa serve di più a chi modifica il target.
REL_WEIGHT = {"callee": 1.0, "caller": 0.8, "class": 0.7, "base": 0.7, "overrides": 0.7,
              "overridden_by": 0.6, "subclass": 0.5}
GRAPH_W, SEM_W = 0.7, 0.3          # rilevanza = 0.7*grafo + 0.3*semantica
MAX_CODE_LINES = 120               # oltre, il codice di un singolo nodo viene troncato


@dataclass
class Item:
    id: str
    relation: str
    depth: int
    graph_score: float
    sem_score: float = 0.0
    full: bool = False

    @property
    def score(self):
        return GRAPH_W * self.graph_score + SEM_W * self.sem_score


# --------------------------------------------------------------------------- #
# 1. Trovare il target
# --------------------------------------------------------------------------- #

def find_nodes(G: nx.MultiDiGraph, name: str) -> list:
    """Candidati per `name`, dal match più preciso al più lasco (si ferma al primo livello che trova qualcosa)."""
    if name in G:
        return [name]
    data = G.nodes(data=True)
    tiers = [
        lambda n, d: d["qualname"] == name,
        lambda n, d: n.endswith(":" + name) or n.endswith("." + name),
        lambda n, d: d["name"] == name,
    ]
    for tier in tiers:
        hits = sorted(n for n, d in data if tier(n, d))
        if hits:
            return hits
    return []


# --------------------------------------------------------------------------- #
# 2. Espansione sul grafo
# --------------------------------------------------------------------------- #

def _bfs(G, start, edge_type, forward, depth):
    """{nodo: distanza} lungo archi di un tipo, in avanti (out) o indietro (in), fino a `depth`."""
    seen, queue = {start: 0}, deque([start])
    while queue:
        cur = queue.popleft()
        if seen[cur] >= depth:
            continue
        edges = G.out_edges(cur, keys=True) if forward else G.in_edges(cur, keys=True)
        for u, v, k in edges:
            nxt = v if forward else u
            if k == edge_type and nxt not in seen:
                seen[nxt] = seen[cur] + 1
                queue.append(nxt)
    seen.pop(start)
    return seen


def _parent_class(G, method):
    for u, _, k in G.in_edges(method, keys=True):
        if k == "defines" and G.nodes[u]["type"] == "class":
            return u
    return None


def expand(G, target, depth=2, callers_depth=1) -> dict:
    """{id: Item} dei nodi collegati al target. Se un nodo compare in più modi si tiene la relazione migliore."""
    found = {}

    def add(nid, relation, d):
        item = Item(nid, relation, d, REL_WEIGHT[relation] / d)
        if nid not in found or item.graph_score > found[nid].graph_score:
            found[nid] = item

    for nid, d in _bfs(G, target, "calls", True, depth).items():
        add(nid, "callee", d)
    for nid, d in _bfs(G, target, "calls", False, callers_depth).items():
        add(nid, "caller", d)

    kind = G.nodes[target]["type"]
    cls = _parent_class(G, target) if kind == "method" else target if kind == "class" else None
    if kind == "method" and cls:
        add(cls, "class", 1)
    if cls:
        for nid, d in _bfs(G, cls, "inherits", True, depth).items():
            add(nid, "base", d)
        for nid, d in _bfs(G, cls, "inherits", False, 1).items():
            add(nid, "subclass", d)
    if kind == "method" and cls:
        name = G.nodes[target]["name"]
        for forward, rel in ((True, "overrides"), (False, "overridden_by")):
            for c in _bfs(G, cls, "inherits", forward, 10):   # antenati (out) o discendenti (in)
                m = next((v for _, v, k in G.out_edges(c, keys=True)
                          if k == "defines" and G.nodes[v]["name"] == name), None)
                if m:
                    add(m, rel, 1)
    found.pop(target, None)
    return found


# --------------------------------------------------------------------------- #
# 3. Punteggio semantico
# --------------------------------------------------------------------------- #

def add_semantic_scores(items, target, query=None, db: Path = DEFAULT_DB, model_name: str = DEFAULT_MODEL):
    """Similarità coseno di ogni candidato con il target (o con `query`, se data: retrieval guidato dal task).

    Se l'indice non esiste il punteggio semantico resta 0 e il ranking usa solo il grafo.
    """
    import numpy as np
    from .semantic import _collection, _model
    try:
        col = _collection(db)
    except Exception:
        return False
    ids = list(items) + [target]
    got = col.get(ids=ids, include=["embeddings"])
    emb = dict(zip(got["ids"], np.asarray(got["embeddings"])))
    ref = _model(model_name).encode([query], normalize_embeddings=True)[0] if query else emb.get(target)
    if ref is None:
        return False
    for nid, item in items.items():
        if nid in emb:
            item.sem_score = max(0.0, float(np.dot(ref, emb[nid])))
    return True


# --------------------------------------------------------------------------- #
# 4. Composizione del contesto
# --------------------------------------------------------------------------- #

def _header(n):
    return f"{n['file']}:{n['line']}-{n['end_line']}"


def _compact(n):
    doc = (n.get("docstring") or "").strip().split("\n")[0]
    return "\n".join(x for x in (signature(n) or n["qualname"], f'    """{doc}"""' if doc else "") if x)


def _code(n):
    lines = n["code"].split("\n")
    if len(lines) > MAX_CODE_LINES:
        return "\n".join(lines[:MAX_CODE_LINES]) + f"\n    # … (+{len(lines) - MAX_CODE_LINES} righe omesse)"
    return n["code"]


def _body(n, full):
    # classi e moduli sono troppo grandi per essere utili interi: sempre in forma compatta
    if full and n["type"] in ("function", "method"):
        return _code(n)
    return _compact(n)


def build_context(G, target, depth=2, callers_depth=1, query=None, max_chars=12000,
                  semantic=True, db: Path = DEFAULT_DB, model_name: str = DEFAULT_MODEL):
    """Ritorna (testo, elenco Item ordinato) con il contesto per lavorare su `target`."""
    items = expand(G, target, depth, callers_depth)
    used_semantic = add_semantic_scores(items, target, query, db, model_name) if semantic and items else False
    ranked = sorted(items.values(), key=lambda i: -i.score)

    budget = max_chars - len(_code(G.nodes[target]))
    for it in ranked:  # codice completo finché c'è budget, poi solo firma + prima riga di docstring
        n = G.nodes[it.id]
        cost = len(_body(n, True))
        if n["type"] in ("function", "method") and cost <= budget:
            it.full, budget = True, budget - cost

    t = G.nodes[target]
    out = [f"# Target: {target}  [{t['type']}]  ({_header(t)})", "```python", _code(t), "```", ""]
    sections = [("callee", "Cosa chiama il target (dipendenze)"), ("caller", "Chi chiama il target (impatto di una modifica)"),
                ("class", "Classe che lo contiene"), ("base", "Classi base"), ("overrides", "Metodo che il target ridefinisce"),
                ("overridden_by", "Ridefinizioni nelle sottoclassi"), ("subclass", "Sottoclassi")]
    for rel, title in sections:
        group = [i for i in ranked if i.relation == rel]
        if not group:
            continue
        out.append(f"## {title}")
        for it in group:
            n = G.nodes[it.id]
            out += [f"### {it.id}  [{n['type']}, distanza {it.depth}, rilevanza {it.score:.2f}]  ({_header(n)})",
                    "```python", _body(n, it.full), "```", ""]

    # Chiamanti che il grafo non può vedere: chiamate su variabili (`t.update()`) con lo stesso nome del target.
    # Non verificati (`d.update()` può essere un dict): sezione separata, solo elenco.
    known = {i.id for i in ranked if i.relation == "caller"}
    maybe = sorted({u["source"] for u in G.graph["unresolved"]
                    if u["kind"] == "dynamic" and u["name"].split(".")[-1] == t["name"] and "." in u["name"]
                    and not (u["name"].count(".") == 1 and u["name"].startswith(("self.", "cls.", "super()")))
                    and u["source"] not in known | {target}})
    if maybe:
        out.append("## Possibili chiamanti non verificati (stesso nome, oggetto di tipo ignoto)")
        out += [f"- {m}  ({_header(G.nodes[m])})" for m in maybe[:15]]
        out += [f"- … altri {len(maybe) - 15}"] if len(maybe) > 15 else []
        out.append("")

    unresolved = {}
    for u in G.graph["unresolved"]:
        if u["source"] == target and u["kind"] != "import":
            unresolved.setdefault(u["name"], u["kind"])
    if unresolved:
        out.append("## Chiamate non risolte dal grafo (potrebbero nascondere dipendenze)")
        out += [f"- `{name}` ({kind})" for name, kind in sorted(unresolved.items())] + [""]
    if not used_semantic and semantic:
        out.append("_Nota: indice semantico non disponibile, ranking basato solo sul grafo._")
    return "\n".join(out), ranked
