"""CLI del progetto. Uso:  python -m src.cli --help"""

import json
import webbrowser
from pathlib import Path

import click

from .graph import load_graph, summary
from .parser import DEFAULT_EXCLUDE, build_graph
from .retrieval import build_context, find_nodes
from .semantic import DEFAULT_DB, DEFAULT_MODEL, build_index, search
from .visualize import render

GRAPH = click.Path(exists=True, dir_okay=False, path_type=Path)
DEFAULT_GRAPH = Path("output/graph.json")
DEFAULT_HTML = Path("output/graph.html")


@click.group()
def cli():
    """Graph-RAG per codebase: estrae, ispeziona e visualizza il grafo di un repo Python."""


@cli.command()
@click.argument("repo", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("-o", "--output", type=Path, default=DEFAULT_GRAPH, show_default=True)
@click.option("--exclude", default=",".join(sorted(DEFAULT_EXCLUDE)), show_default=True,
              help="Directory top-level da escludere, separate da virgola.")
def build(repo, output, exclude):
    """Analizza REPO e scrive nodi e archi in un JSON."""
    graph = build_graph(repo.resolve(), set(filter(None, exclude.split(","))))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")
    click.echo(f"{sum(graph['stats']['nodes'].values())} nodi, {sum(graph['stats']['edges'].values())} archi -> {output}")


@cli.command()
@click.argument("graph", type=GRAPH, default=DEFAULT_GRAPH)
def stats(graph):
    """Statistiche sul grafo (networkx): hub, componenti, chiamate non risolte."""
    s = summary(load_graph(graph))
    click.echo(f"Nodi: {s['nodes']}   Archi: {s['edges']}")
    click.echo(f"Non risolti: {s['unresolved']}")
    click.echo(f"Componenti connesse (dimensioni): {s['components']}   nodi isolati: {s['isolated']}")
    click.echo("\nPiù chiamati:")
    for n, d in s["most_called"]:
        click.echo(f"  {d:3d}  {n}")
    click.echo("\nChe chiamano di più:")
    for n, d in s["calls_most"]:
        click.echo(f"  {d:3d}  {n}")


@cli.command()
@click.argument("graph", type=GRAPH, default=DEFAULT_GRAPH)
@click.option("-o", "--output", type=Path, default=DEFAULT_HTML, show_default=True)
@click.option("--open", "open_", is_flag=True, help="Apre la pagina nel browser.")
def viz(graph, output, open_):
    """Genera la pagina HTML interattiva del grafo."""
    output.write_text(render(json.loads(graph.read_text(encoding="utf-8"))), encoding="utf-8")
    click.echo(f"-> {output}")
    if open_:
        webbrowser.open(output.resolve().as_uri())


@cli.command()
@click.argument("graph", type=GRAPH, default=DEFAULT_GRAPH)
@click.option("--db", type=Path, default=DEFAULT_DB, show_default=True)
@click.option("--model", default=DEFAULT_MODEL, show_default=True)
def index(graph, db, model):
    """Calcola un embedding per ogni nodo e lo salva in ChromaDB."""
    n = build_index(graph, db, model)
    click.echo(f"{n} nodi indicizzati -> {db}")


@cli.command("search")
@click.argument("query")
@click.option("-k", type=int, default=5, show_default=True, help="Quanti risultati.")
@click.option("-t", "--type", "types", multiple=True,
              type=click.Choice(["module", "class", "function", "method"]), help="Filtra per tipo (ripetibile).")
@click.option("--db", type=Path, default=DEFAULT_DB, show_default=True)
@click.option("--model", default=DEFAULT_MODEL, show_default=True)
def ask(query, k, types, db, model):
    """Ricerca semantica: i nodi più simili a QUERY (Milestone 3)."""
    for r in search(query, k, types, db, model):
        click.echo(f"{r['score']:.3f}  {r['type']:8s} {r['id']}   ({r['file']}:{r['line']})")


@cli.command()
@click.argument("name")
@click.option("--graph", "graph_path", type=GRAPH, default=DEFAULT_GRAPH, show_default=True)
@click.option("-d", "--depth", type=int, default=2, show_default=True, help="Livelli di dipendenze (cosa chiama il target).")
@click.option("--callers-depth", type=int, default=1, show_default=True, help="Livelli di chiamanti.")
@click.option("-q", "--query", help="Descrizione del task: guida il ranking semantico invece del solo target.")
@click.option("--max-chars", type=int, default=12000, show_default=True, help="Budget di caratteri per il codice completo.")
@click.option("--no-semantic", is_flag=True, help="Ranking basato solo sul grafo.")
@click.option("--db", type=Path, default=DEFAULT_DB, show_default=True)
def context(name, graph_path, depth, callers_depth, query, max_chars, no_semantic, db):
    """Contesto minimo ma completo per lavorare su NAME (Milestone 4).

    NAME può essere l'id completo, il qualname (tqdm.update) o un suffisso.
    """
    G = load_graph(graph_path)
    hits = find_nodes(G, name)
    if not hits:
        raise click.ClickException(f"nessun nodo per '{name}' (prova `search` per cercare per significato)")
    if len(hits) > 1:
        raise click.ClickException("nome ambiguo, specifica meglio:\n  " + "\n  ".join(hits[:15]))
    if G.nodes[hits[0]]["type"] == "module":
        raise click.ClickException("il target è un modulo: scegli una funzione, un metodo o una classe")
    text, _ = build_context(G, hits[0], depth, callers_depth, query, max_chars, not no_semantic, db)
    click.echo(text)


@cli.command()
@click.option("--graph", "graph_path", type=GRAPH, default=DEFAULT_GRAPH, show_default=True)
@click.option("--db", type=Path, default=DEFAULT_DB, show_default=True)
@click.option("--model", "llm_model", default=None, help="Modello Claude (default: $ANTHROPIC_MODEL o claude-sonnet-5).")
@click.option("--no-semantic", is_flag=True, help="Ranking del contesto basato solo sul grafo.")
def eval(graph_path, db, llm_model, no_semantic):
    """Confronta le risposte di Claude con e senza il contesto recuperato (Fase 5, Milestone 5).

    Richiede ANTHROPIC_API_KEY (in .env o nell'ambiente) e fa chiamate reali all'API.
    """
    from .eval import DEFAULT_LLM_MODEL, format_report, run_all
    results = run_all(graph_path, db, llm_model or DEFAULT_LLM_MODEL, not no_semantic)
    report = format_report(results)
    out = Path("output/eval")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(report, encoding="utf-8")
    (out / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    click.echo(report)
    click.echo(f"-> dettagli in {out}/")


@cli.command()
@click.argument("repo", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.pass_context
def show(ctx, repo):
    """Scorciatoia: build + viz --open (Milestone 2)."""
    ctx.invoke(build, repo=repo, output=DEFAULT_GRAPH, exclude=",".join(sorted(DEFAULT_EXCLUDE)))
    ctx.invoke(viz, graph=DEFAULT_GRAPH, output=DEFAULT_HTML, open_=True)


if __name__ == "__main__":
    cli()
