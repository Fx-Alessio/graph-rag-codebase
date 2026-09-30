"""Fase 3 — Layer semantico: embedding per nodo + ricerca per similarità (ChromaDB)."""

import ast
import json
import re
import textwrap
from pathlib import Path

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_DB = Path("output/chroma")
COLLECTION = "nodes"
MAX_DOC_CHARS = 1200   # il modello tronca comunque intorno ai 256 token


def signature(node: dict) -> str:
    """Firma di funzione/metodo/classe ricavata dal sorgente del nodo (senza corpo)."""
    if node["type"] == "module":
        return ""
    try:
        d = ast.parse(textwrap.dedent(node["code"])).body[0]
        if isinstance(d, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in d.bases)
            return f"class {d.name}({bases})" if bases else f"class {d.name}"
        ret = f" -> {ast.unparse(d.returns)}" if d.returns else ""
        return f"def {d.name}({ast.unparse(d.args)}){ret}"
    except (SyntaxError, IndexError):
        return ""


def split_identifier(name: str) -> str:
    """`format_meter` / `TqdmCallback` -> 'format meter' / 'Tqdm Callback': aiuta il matching semantico."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|_+", " ", name).strip()


def node_text(node: dict) -> str:
    """Testo da embeddare: tipo, nome (anche spezzato in parole), firma, docstring."""
    parts = [f"{node['type']} {node['qualname']} ({split_identifier(node['name'])})"]
    if sig := signature(node):
        parts.append(sig)
    if node.get("docstring"):
        parts.append(node["docstring"][:MAX_DOC_CHARS])
    return "\n".join(parts)


_MODEL_CACHE = {}   # nome -> istanza già caricata: nella CLI conta poco (un comando alla volta), ma nel
                    # server MCP persistente evita di ricaricare il modello ad ogni chiamata di tool


def _model(name: str):
    if name not in _MODEL_CACHE:
        from sentence_transformers import SentenceTransformer  # import lento: solo quando serve
        _MODEL_CACHE[name] = SentenceTransformer(name)
    return _MODEL_CACHE[name]


def _collection(db: Path, create: bool = False):
    import chromadb
    client = chromadb.PersistentClient(path=str(db))
    if create:
        try:
            client.delete_collection(COLLECTION)
        except Exception:
            pass
        return client.create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})
    return client.get_collection(COLLECTION)


def build_index(graph_path: Path, db: Path = DEFAULT_DB, model_name: str = DEFAULT_MODEL) -> int:
    """(Ri)costruisce l'indice: un embedding per nodo. Ritorna il numero di nodi indicizzati."""
    nodes = json.loads(Path(graph_path).read_text(encoding="utf-8"))["nodes"]
    texts = [node_text(n) for n in nodes]
    vectors = _model(model_name).encode(texts, batch_size=64, normalize_embeddings=True, show_progress_bar=True)
    col = _collection(db, create=True)
    step = 500
    for i in range(0, len(nodes), step):
        chunk = nodes[i:i + step]
        col.add(
            ids=[n["id"] for n in chunk],
            embeddings=vectors[i:i + step].tolist(),
            documents=texts[i:i + step],
            metadatas=[{"type": n["type"], "module": n["module"], "file": n["file"], "line": n["line"]} for n in chunk],
        )
    return len(nodes)


def search(query: str, k: int = 5, types: list = None, db: Path = DEFAULT_DB,
           model_name: str = DEFAULT_MODEL) -> list:
    """Nodi più simili alla query: lista di dict {id, score, type, file, line}. score = similarità coseno."""
    col = _collection(db)
    vec = _model(model_name).encode([query], normalize_embeddings=True).tolist()
    where = {"type": {"$in": list(types)}} if types else None
    res = col.query(query_embeddings=vec, n_results=k, where=where)
    return [
        {"id": i, "score": 1 - dist, **meta}
        for i, dist, meta in zip(res["ids"][0], res["distances"][0], res["metadatas"][0])
    ]
