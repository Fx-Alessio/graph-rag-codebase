# Graph-RAG for Codebases

When an LLM is asked to change a piece of code, it usually only sees what you hand it: a file, a function, maybe
a couple of lines of surrounding context. It has no real way of knowing what else in the project depends on that
code, or what would break if it changed. This project is an attempt to fix that for Python codebases: it builds a
static graph of a repository — who calls whom, who inherits from whom, who imports what — and uses that graph to
hand an LLM the *minimal but complete* context it needs before touching a function, instead of leaving it to guess.

The idea isn't new — a few engineers working on AI coding tools have described similar graph-RAG setups to keep an
agent from hallucinating dependencies it can't see. This is a from-scratch implementation of that idea, built to
actually measure whether it helps, not just to assume it does.

## A small example that makes the point

[`tqdm`](https://github.com/tqdm/tqdm), the progress-bar library, is the test repo here. Take `tqdm.write()`, the
method you call to print a message without breaking the progress bar. On its own it's nine unremarkable lines:

```python
@classmethod
def write(cls, s, file=None, end="\n", nolock=False):
    """Print a message via tqdm (without overlap with bars)."""
    fp = file if file is not None else sys.stdout
    ...
```

Nothing about those nine lines tells you that three other modules in the same package — the ones that send
progress updates to Slack, Discord and Telegram bots — all route through this exact method. Change its signature
without knowing that, and you've quietly broken three integrations. Ask this project for the context around
`tqdm.write`, and it tells you so up front, along with everything else that calls it or that it depends on:

```
$ python -m src.cli context tqdm.write --no-semantic

## What the target calls (dependencies)
### tqdm.std:tqdm.external_write_mode
### tqdm.std:tqdm.get_lock

## Who calls the target (blast radius of a change)
### tqdm.contrib.discord:DiscordIO.write
### tqdm.contrib.slack:SlackIO.write
### tqdm.contrib.telegram:TelegramIO.write
... (6 more callers / subclasses)
```

That's the whole pitch of the project in one example: turn "nine lines with no visible dependents" into "nine
lines plus the eleven things you'd otherwise break."

## How it fits together

The pipeline is a straight line: a parser walks the repo with Python's own `ast` module and writes out every
function, class and method as a *node*, and every call, import and inheritance relationship as an *edge*. That
graph gets loaded into `networkx` so it can be walked and queried, and each node also gets a semantic embedding
(via `sentence-transformers`, stored in ChromaDB) so you can find a starting point by *meaning* rather than by
exact name when you don't know what something is called. The retrieval step ties the two together: given a
target, it expands outward along the graph — dependencies, callers, base classes, overrides — ranks what it finds
by a mix of graph distance and semantic similarity, and assembles a context string within a size budget. On top of
that sits an evaluation harness that actually calls the Claude API with and without the retrieved context on a
couple of realistic tasks, to check whether any of this changes the answer for the better — and, finally, an MCP
server that exposes the whole thing as a tool an agent like Claude Code can call on its own mid-session, instead
of a human copying context into a chat window by hand.

Every one of those steps has sharp edges — static analysis can't know the type of a variable, semantic search on
a generic embedding model is noisy, a rename can hide behind a call on an object of unknown type — and the project
tries to be honest about them rather than pretend the graph is complete. The retrieved context explicitly lists
the calls it *couldn't* resolve, so an LLM (or a person) knows what it's not being told, not just what it is.

## Trying it

You'll need Python 3.12+ (the MCP SDK used in the last phase doesn't run on 3.9/3.10, which is what this started
on before that became a problem worth solving).

```bash
git clone https://github.com/Fx-Alessio/graph-rag-codebase
cd graph-rag-codebase
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

git clone https://github.com/tqdm/tqdm test_repo/tqdm   # the test repo, not checked into this one

python -m src.cli build test_repo/tqdm      # -> output/graph.json
python -m src.cli viz --open                # -> an interactive graph in the browser
python -m src.cli index                     # embeds every node (downloads a small model once)
python -m src.cli search "detect terminal width"
python -m src.cli context tqdm.update       # the command that matters most
```

`output/graph.json` and `output/graph.html` already ship in the repo, built from `tqdm`, so there's something to
look at even before running `build` yourself.

Comparing answers with and without retrieval (`python -m src.cli eval`) calls the real Claude API and needs an
`ANTHROPIC_API_KEY` in a local `.env` file (never committed); it costs a few cents in tokens. On the two test
tasks used here — propagating a new parameter across four subclasses, and renaming a method with callers hidden
behind untyped variables — the plan produced with retrieved context caught every point that needed touching,
where the same request without context caught half of them in one case and four out of five in the other.

Wiring this into Claude Code as an MCP server is a matter of copying `.mcp.json.example` to `.mcp.json`, pointing
it at your own checkout, and approving the server once (`claude mcp list` shows whether it's pending).

## Where things stand

The first six phases of the plan are done: the parser, the graph layer, the semantic index, the hybrid retrieval,
the with/without evaluation, and the MCP server. A seventh, optional phase — reimplementing the retrieval logic
with LangGraph and comparing it to the hand-written version — was left undone; it's a comparison of frameworks
more than a feature, and didn't seem worth the time yet. The full plan is in [`PLAN.md`](PLAN.md), and every
technical decision along the way — why static analysis over-approximates instead of under-approximating, why
ChromaDB over FAISS, why the ranking weights are what they are, what broke and how it got fixed — is written down
in [`DECISIONS.md`](DECISIONS.md) as it happened, rather than reconstructed after the fact.

The one caveat worth stating plainly: `tqdm` is a well-known public library, so a model asked to plan a change to
it "for free," without any retrieved context, still gets some things right just from having seen it during
training. That's part of why the baseline in the evaluation isn't hopeless. On a private codebase the model has
never seen, the gap in favor of retrieval would likely be wider — but that's an argument for testing this against
a repo of your own, not a claim made here.
