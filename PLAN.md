# Graph-RAG per Codebase — Piano di progetto

## Obiettivo
Costruire uno strumento che analizza una codebase e genera una rappresentazione a grafo (sintattica + semantica) delle sue componenti, per recuperare in modo mirato il contesto rilevante da passare a un LLM quando deve modificare del codice — riducendo il rischio che "perda pezzi" o alluciní dipendenze.

Ispirato a un approccio descritto da un ingegnere del settore: un graph-RAG di codebase per limitare le allucinazioni di un'AI che lavora sul codice, mantenendo sempre visibili le dipendenze rilevanti.

## Repo di test
- **Fase iniziale**: [`tqdm`](https://github.com/tqdm/tqdm) — libreria Python piccola, ben strutturata, molto riconoscibile, con classi che si estendono per contesti diversi (notebook, terminale, pandas).
- **Fase avanzata (eventuale)**: repo di dimensione media con più moduli/chiamate incrociate (es. `httpie`) per rendere il retrieval multi-hop più interessante da mostrare.

## Struttura cartelle
```
graph-rag-codebase/
├── src/                 ← codice del progetto (parser, graph builder, ecc.)
├── test_repos/          ← repo clonati da analizzare (in .gitignore)
│   └── tqdm/
├── output/               ← JSON/grafi generati (in .gitignore o versionati come esempio)
├── README.md
└── requirements.txt
```

## Stack tecnico
| Componente | Tool |
|---|---|
| Parsing | `ast` nativo di Python (eventualmente tree-sitter in futuro per multi-linguaggio) |
| Grafo | `networkx` |
| Embedding | sentence-transformers (locale) o API embedding |
| Vector store | ChromaDB o FAISS |
| LLM | Claude API |
| CLI | Click o Typer |
| MCP | SDK ufficiale Python (`mcp.server.fastmcp`), transport stdio |
| Confronto framework (opzionale) | LangGraph o LangChain |

## Fasi e milestone

### Fase 1 — Parser
- Estrarre da ogni file: funzioni, classi, metodi, import, chiamate a funzione, ereditarietà
- Output: lista di **nodi** (id univoco, tipo, nome, file, riga, docstring, codice sorgente) e **archi** (tipo relazione: `calls`, `imports`, `inherits`, `defines`)
- **Milestone 1**: script che, dato un repo, produce un JSON con nodi e archi

### Fase 2 — Graph Builder
- Caricare nodi/archi in un grafo `networkx`
- Visualizzazione (matplotlib, o export per Gephi/D3.js)
- **Milestone 2**: CLI che genera e visualizza il grafo del repo di test

### Fase 3 — Layer semantico
- Embedding per nodo (docstring + firma + eventuale summary generato da LLM)
- Salvataggio in vector store leggero (ChromaDB/FAISS)
- **Milestone 3**: query testuale (es. "trova le funzioni che gestiscono l'autenticazione") → nodi più rilevanti per similarità

### Fase 4 — Retrieval ibrido
- Dato un task, trovare il nodo target (nome o similarità semantica)
- Espandere nel grafo: vicini diretti/indiretti (chi chiama, cosa chiama, ereditarietà)
- Filtrare/ordinare per rilevanza (distanza nel grafo + score semantico)
- **Milestone 4**: dato il nome di una funzione, stampare il "contesto minimo ma completo" (codice + dipendenze dirette/indirette fino a N livelli)

### Fase 5 — Integrazione con LLM
- Costruire un prompt che inietta il contesto recuperato + il task, chiamare Claude via API
- Confrontare risultato **con** e **senza** retrieval mirato su alcuni task di test, per dimostrare il valore del grafo

### Fase 6 — Esposizione come server MCP
- Impacchettare la logica di retrieval (Fase 4) come server MCP, usando l'SDK ufficiale Python (`mcp.server.fastmcp`)
- Esporre almeno un tool, es. `get_context(function_name)`, che ritorna il codice + dipendenze rilevanti dal grafo
- Transport: **stdio** (locale, sufficiente per uso personale/demo)
- Collegare il server a Claude Code tramite file di configurazione, così l'agente può chiamare il tool nativamente durante il lavoro sul codice
- **Milestone 6**: Claude Code, durante una sessione su un task reale, chiama autonomamente `get_context` per recuperare le dipendenze rilevanti

### Fase 7 — Confronto con framework per agenti (opzionale, per il README)
- Reimplementare il workflow di retrieval (Fase 4-5) usando **LangGraph** (o LangChain)
- Confrontare con l'implementazione manuale: righe di codice, leggibilità, flessibilità, tempo di sviluppo
- Documentare nel README quando ha senso costruire da zero vs usare un framework — dimostra padronanza di entrambi gli approcci, non solo scelta di uno

## Decisioni prese finora
- Iniziare con un solo linguaggio (Python) prima di generalizzare
- Usare `test_repos/` come cartella dedicata ai repo clonati, esclusa da git
- Non usare il progetto personale (tool colloqui) come caso di test: si preferisce un repo esterno riconoscibile
- Documentare le decisioni progressivamente (es. in un file `DECISIONS.md`), non solo a fine progetto

## Note per Claude Code
- Procedere per milestone, una alla volta, verificando insieme i risultati prima di passare alla fase successiva
- Ad ogni milestone, validare manualmente (aprendo il codice sorgente reale di `tqdm`) che quanto estratto/recuperato sia corretto