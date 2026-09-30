"""Fase 6 — Server MCP: espone il retrieval (Fase 4) come tool per un agente come Claude Code.

Avvio:  .venv/bin/python -m src.mcp_server [--graph output/graph.json] [--db output/chroma]
Transport: stdio (pensato per uso locale, configurato come server MCP di un client come Claude Code).

Grafo e modello di embedding si caricano una sola volta all'avvio (non ad ogni chiamata, a differenza della CLI):
un client MCP tiene il processo vivo per tutta la sessione, quindi ha senso pagare il costo di caricamento una
volta sola. Vedi `noteXme/08-mcp.md`.
"""

import argparse
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .graph import load_graph
from .retrieval import build_context, find_nodes
from .semantic import DEFAULT_MODEL, _model

server = MCPServer(
    name="graph-rag-codebase",
    instructions=(
        "Fornisce il contesto minimo ma completo (codice + dipendenze dirette/indirette, tramite grafo statico "
        "del codice + similarità semantica) per lavorare su una funzione, un metodo o una classe di un repo Python "
        "già analizzato. Usa `get_context` prima di modificare del codice, per non perdere dipendenze rilevanti "
        "o chiamanti che una lettura del solo file non rivelerebbe. Usa `search_code` quando non conosci il nome "
        "esatto del punto da modificare."
    ),
)

# Stato caricato una volta all'avvio (vedi main()).
STATE = {"graph": None, "graph_path": None, "db": None, "model_name": DEFAULT_MODEL}


def _graph():
    if STATE["graph"] is None:
        raise RuntimeError("Grafo non caricato: avviare il server con `python -m src.mcp_server --graph ...`.")
    return STATE["graph"]


@server.tool()
def get_context(name: str, depth: int = 2, callers_depth: int = 1, task: str = "", max_chars: int = 12000) -> str:
    """Contesto minimo ma completo per modificare `name` (funzione/metodo/classe) senza perdere dipendenze.

    Espande il grafo staticamente costruito del repo (chiamanti, dipendenze, ereditarietà, override) e lo
    combina con la similarità semantica. Include anche i punti che il grafo NON può vedere (chiamate dinamiche,
    chiamanti su variabili di tipo ignoto): leggerli prima di assumere che il contesto sia esaustivo.

    Args:
        name: id, qualname (es. "tqdm.update") o suffisso del nodo target. Se ambiguo, l'errore elenca i candidati.
        depth: livelli di dipendenze da includere (cosa il target chiama, transitivamente).
        callers_depth: livelli di chiamanti da includere (chi chiama il target).
        task: descrizione del task in corso, se disponibile: orienta il ranking semantico verso ciò che serve
            per QUEL task invece che verso il solo significato del target.
        max_chars: budget massimo del testo restituito; oltre, i nodi meno rilevanti appaiono solo come firma.
    """
    G = _graph()
    hits = find_nodes(G, name)
    if not hits:
        return (f"Nessun nodo trovato per '{name}'. Usa `search_code` per cercare per significato, "
                f"oppure `list_nodes` per un elenco.")
    if len(hits) > 1:
        return "Nome ambiguo, specifica meglio uno di questi:\n" + "\n".join(f"- {h}" for h in hits[:20])
    if G.nodes[hits[0]]["type"] == "module":
        return f"'{hits[0]}' è un modulo: scegli una funzione, un metodo o una classe al suo interno."
    text, _ = build_context(G, hits[0], depth, callers_depth, query=task or None, max_chars=max_chars,
                            db=STATE["db"], model_name=STATE["model_name"])
    return text


@server.tool()
def search_code(query: str, k: int = 5, node_type: str = "") -> str:
    """Cerca funzioni/metodi/classi per significato (embedding), quando non conosci il nome esatto.

    Args:
        query: descrizione in inglese di cosa cerchi (es. "detect terminal width").
        k: quanti risultati.
        node_type: se dato, filtra su "function", "method", "class" o "module".
    """
    from .semantic import search
    types = [node_type] if node_type else None
    try:
        results = search(query, k, types, STATE["db"], STATE["model_name"])
    except Exception as e:
        return f"Indice semantico non disponibile ({e}). Costruiscilo con `python -m src.cli index`."
    if not results:
        return "Nessun risultato."
    return "\n".join(f"{r['score']:.2f}  {r['type']:8s} {r['id']}  ({r['file']}:{r['line']})" for r in results)


@server.tool()
def list_nodes(module_prefix: str = "", node_type: str = "") -> str:
    """Elenca i nodi del repo indicizzato, filtrando per modulo e/o tipo (utile per orientarsi o disambiguare).

    Args:
        module_prefix: solo i nodi il cui modulo inizia con questo prefisso (es. "tqdm.contrib").
        node_type: "module", "class", "function" o "method".
    """
    G = _graph()
    hits = [n for n, d in G.nodes(data=True)
            if (not module_prefix or d["module"].startswith(module_prefix))
            and (not node_type or d["type"] == node_type)]
    hits.sort()
    if len(hits) > 200:
        return f"{len(hits)} nodi (troppi per elencarli, restringi il filtro). Primi 50:\n" + "\n".join(hits[:50])
    return "\n".join(hits) if hits else "Nessun nodo corrisponde ai filtri."


def main():
    ap = argparse.ArgumentParser(description="Server MCP per il retrieval del grafo-codice (Fase 6).")
    ap.add_argument("--graph", type=Path, default=Path("output/graph.json"))
    ap.add_argument("--db", type=Path, default=Path("output/chroma"))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    STATE["graph"] = load_graph(args.graph)
    STATE["graph_path"] = args.graph
    STATE["db"] = args.db
    STATE["model_name"] = args.model
    try:
        _model(args.model)   # pre-carica il modello di embedding: la prima query non deve aspettare
    except Exception:
        pass   # l'indice/modello potrebbero non esserci ancora: get_context funziona comunque (solo grafo)

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
