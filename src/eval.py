"""Fase 5 — Confronto con e senza retrieval: alcuni task di test su tqdm.

Per ogni task si chiede a Claude un piano di modifica (cosa cambiare, dove, perché) in due condizioni:
- baseline: solo il codice del target (come se l'LLM avesse aperto solo quella funzione, senza esplorare il repo);
- context: target + il contesto costruito da `retrieval.build_context` (Fase 4).

Si misura, per ciascun task, quanti dei punti "nascosti" (dipendenze/chiamanti che il target da solo non rivela)
compaiono nella risposta. Non è una revisione di codice automatica: è una misura di richiamo (recall) per parola
chiave, a bassa precisione ma sufficiente a mostrare se il contesto recuperato cambia la risposta.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .graph import load_graph
from .retrieval import build_context

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DEFAULT_LLM_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
MAX_TOKENS = 4096   # con "thinking" disabilitato basta per liste + codice; con thinking abilitato il modello
                    # può consumare l'intero budget "pensando" senza lasciare spazio alla risposta (visto in pratica)

SYSTEM = (
    "Sei un ingegnere che deve pianificare una modifica in una codebase Python reale (tqdm). "
    "Ricevi il codice del punto da modificare (target) ed eventualmente altro codice di contesto. "
    "Rispondi in questo formato:\n"
    "1. Elenco puntato di OGNI file/funzione/metodo della codebase che va toccato o anche solo verificato "
    "(non solo il target), con il motivo. Se non conosci il nome esatto di qualcosa che sospetti debba esistere, "
    "dillo comunque (es. 'andrebbe controllata ogni sottoclasse che ridefinisce display()').\n"
    "2. Il codice della modifica al target.\n"
    "Sii concreto: l'obiettivo è non dimenticare nessun punto della codebase da aggiornare."
)


@dataclass
class Task:
    id: str
    target: str                # qualname/suffisso passato a find_nodes
    instruction: str
    ground_truth: list = field(default_factory=list)   # [{"must_mention": [...], "label": "..."}]
    depth: int = 2
    callers_depth: int = 1


TASKS = [
    Task(
        id="close-final-message",
        target="tqdm.std:tqdm.close",
        instruction=(
            "Aggiungi un parametro opzionale `final_message: str = None` al metodo `close()` di `tqdm`: se "
            "specificato, deve essere mostrato come ultima riga (al posto del normale display) prima di terminare "
            "la progress bar. La modifica deve restare coerente in tutte le sottoclassi che ridefiniscono `close()` "
            "(es. le varianti per notebook, GUI, Tkinter, rich): elenca esplicitamente cosa va verificato o "
            "aggiornato in ciascuna."
        ),
        ground_truth=[
            {"must_mention": ["tqdm_notebook"], "label": "tqdm.notebook:tqdm_notebook.close"},
            {"must_mention": ["tqdm_gui"], "label": "tqdm.gui:tqdm_gui.close"},
            {"must_mention": ["tqdm_tk"], "label": "tqdm.tk:tqdm_tk.close"},
            {"must_mention": ["tqdm_rich"], "label": "tqdm.rich:tqdm_rich.close"},
        ],
    ),
    Task(
        id="rename-update-to-advance",
        target="tqdm.std:tqdm.update",
        instruction=(
            "Rinomina il metodo `update()` in `advance()`, mantenendo `update()` come alias deprecato che richiama "
            "`advance()` per compatibilità. Elenca esplicitamente OGNI punto della codebase (oltre al metodo "
            "stesso) che chiama `.update()` su un'istanza di `tqdm` e che va quindi verificato."
        ),
        ground_truth=[
            {"must_mention": ["cli.py"], "label": "tqdm.cli:main (chiama .update() su una variabile)"},
            {"must_mention": ["dask"], "label": "tqdm.dask (chiama .update() su una variabile)"},
            {"must_mention": ["keras"], "label": "tqdm.keras (chiama .update() su una variabile)"},
            {"must_mention": ["concurrent"], "label": "tqdm.contrib.concurrent (chiama .update() su una variabile)"},
            {"must_mention": ["notebook"], "label": "tqdm.notebook:tqdm_notebook.update (override via super().update)"},
        ],
    ),
]


def _call(client, model, user_text):
    resp = client.messages.create(
        model=model, max_tokens=MAX_TOKENS, system=SYSTEM, thinking={"type": "disabled"},
        messages=[{"role": "user", "content": user_text}],
    )
    text = "\n".join(b.text for b in resp.content if b.type == "text")
    return text, resp.usage.input_tokens, resp.usage.output_tokens


def _score(text, ground_truth):
    low = text.lower()
    hits = [g for g in ground_truth if all(s.lower() in low for s in g["must_mention"])]
    return hits


def run_task(G, task: Task, model, graph_path, db, semantic=True, save_dir: Path = None):
    import anthropic
    client = anthropic.Anthropic()

    target_node = None
    from .retrieval import find_nodes
    hits = find_nodes(G, task.target)
    if len(hits) != 1:
        raise ValueError(f"target ambiguo o non trovato per '{task.target}': {hits}")
    target_node = hits[0]
    t = G.nodes[target_node]

    baseline_prompt = (
        f"Task: {task.instruction}\n\nCodice del target ({target_node}):\n```python\n{t['code']}\n```"
    )
    context_text, _ = build_context(G, target_node, task.depth, task.callers_depth, query=task.instruction,
                                     semantic=semantic, db=db)
    context_prompt = f"Task: {task.instruction}\n\n{context_text}"

    result = {"task": task.id, "target": target_node, "conditions": {}}
    for cond, prompt in (("baseline", baseline_prompt), ("context", context_prompt)):
        text, itok, otok = _call(client, model, prompt)
        hits = _score(text, task.ground_truth)
        result["conditions"][cond] = {
            "prompt_chars": len(prompt), "input_tokens": itok, "output_tokens": otok,
            "found": [h["label"] for h in hits], "found_n": len(hits), "total": len(task.ground_truth),
            "response": text,
        }
        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)
            (save_dir / f"{task.id}__{cond}.md").write_text(
                f"# {task.id} — {cond}\n\n## Prompt\n\n{prompt}\n\n## Risposta\n\n{text}\n", encoding="utf-8")
    return result


def run_all(graph_path=Path("output/graph.json"), db=Path("output/chroma"), model=DEFAULT_LLM_MODEL,
            semantic=True, save_dir=Path("output/eval"), tasks=None):
    G = load_graph(graph_path)
    return [run_task(G, t, model, graph_path, db, semantic, save_dir) for t in (tasks or TASKS)]


def format_report(results) -> str:
    lines = ["# Fase 5 — con vs senza retrieval", ""]
    for r in results:
        lines.append(f"## {r['task']}  (target: `{r['target']}`)")
        lines.append("")
        lines.append("| condizione | punti trovati | prompt (car.) | token input/output |")
        lines.append("|---|---|---|---|")
        for cond in ("baseline", "context"):
            c = r["conditions"][cond]
            lines.append(f"| {cond} | {c['found_n']}/{c['total']} | {c['prompt_chars']} | "
                        f"{c['input_tokens']}/{c['output_tokens']} |")
        lines.append("")
        base_found, ctx_found = set(r["conditions"]["baseline"]["found"]), set(r["conditions"]["context"]["found"])
        only_ctx = ctx_found - base_found
        if only_ctx:
            lines.append("**Trovati solo con il contesto:**")
            lines += [f"- {x}" for x in sorted(only_ctx)]
        missed = {g["label"] for g in next(t for t in TASKS if t.id == r["task"]).ground_truth} - ctx_found
        if missed:
            lines.append("**Non trovati nemmeno con il contesto:**")
            lines += [f"- {x}" for x in sorted(missed)]
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    results = run_all()
    report = format_report(results)
    Path("output/eval").mkdir(parents=True, exist_ok=True)
    Path("output/eval/report.md").write_text(report, encoding="utf-8")
    Path("output/eval/results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(report)
