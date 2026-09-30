"""Fase 2 — Graph builder: carica nodi/archi di graph.json in un grafo networkx."""

import json
from pathlib import Path

import networkx as nx

EDGE_TYPES = ("defines", "imports", "inherits", "calls")


def load_graph(path: Path) -> nx.MultiDiGraph:
    """MultiDiGraph: tra due nodi possono esserci più relazioni (es. `calls` e `imports`).

    Ogni arco ha come chiave il tipo (`G[a][b]["calls"]`) e `type`/`lines` come attributi.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    G = nx.MultiDiGraph(repo=data["repo"], stats=data["stats"], unresolved=data["unresolved"])
    for n in data["nodes"]:
        G.add_node(n["id"], **{k: v for k, v in n.items() if k != "id"})
    for e in data["edges"]:
        G.add_edge(e["source"], e["target"], key=e["type"], type=e["type"], lines=e.get("lines", []))
    return G


def by_type(G: nx.MultiDiGraph, *types: str) -> nx.MultiDiGraph:
    """Vista del grafo con soli gli archi dei tipi indicati."""
    return nx.restricted_view(G, [], [(u, v, k) for u, v, k in G.edges(keys=True) if k not in types])


def summary(G: nx.MultiDiGraph, top: int = 8) -> dict:
    """Numeri utili per farsi un'idea del grafo (e per validarlo)."""
    calls = nx.DiGraph(by_type(G, "calls"))
    structural = nx.Graph(by_type(G, "calls", "inherits", "imports"))
    connected = nx.connected_components(structural)
    sizes = sorted((len(c) for c in connected), reverse=True)
    return {
        "nodes": G.number_of_nodes(),
        "edges": {t: sum(1 for _, _, k in G.edges(keys=True) if k == t) for t in EDGE_TYPES},
        "most_called": sorted(calls.in_degree(), key=lambda x: -x[1])[:top],
        "calls_most": sorted(calls.out_degree(), key=lambda x: -x[1])[:top],
        "components": sizes[:5],
        "isolated": sum(1 for s in sizes if s == 1),
        "unresolved": {k: sum(u["kind"] == k for u in G.graph["unresolved"]) for k in ("external", "dynamic", "import")},
    }
