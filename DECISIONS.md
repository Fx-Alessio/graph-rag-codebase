# Decisioni di progetto

## Fase 1 — Parser

- **Solo `ast` della stdlib**: nessuna dipendenza esterna per il parser (Python 3.9 in locale).
- **ID dei nodi**: `modulo:qualname` (es. `tqdm.std:tqdm.update`); i moduli hanno come id il nome dotted (`tqdm.std`).
- **Nodi**: `module`, `class`, `function`, `method`. Le funzioni annidate non sono nodi: le loro chiamate sono attribuite alla funzione che le contiene.
- **Archi**: `defines`, `imports`, `inherits`, `calls`; un solo arco per (source, target, type), con la lista delle righe in `lines`.
- **Risoluzione multi-target (over-approssimazione)**: import condizionali (`try/except ImportError`) e alias di modulo (`tqdm = tqdm_notebook`) possono risolvere a più nodi. Si emettono tutti gli archi possibili: per il retrieval è meglio un vicino in più che uno in meno ("perdere pezzi" è il rischio da evitare).
- **Alias di modulo `X = Y`** (Name = Name/dotted a livello di modulo) sono seguiti nella risoluzione: in tqdm sono ovunque (`tqdm`, `trange`).
- **Import di costanti/variabili** (es. `from .utils import CUR_OS`): non sono nodi, l'arco `imports` punta al modulo che li espone.
- **Cosa non risolviamo** (finisce in `unresolved`, con `kind`):
  - `external`: chiamate a librerie fuori dal repo;
  - `dynamic`: metodi su oggetti di tipo ignoto, es. `self._ema_dn(...)` dove `_ema_dn` è un attributo assegnato in `__init__` (servirebbe inferenza di tipo degli attributi).
  - I builtin (`len`, `range`, …) sono scartati.
- **Chiamata a classe** (`tqdm(...)`): l'arco punta al nodo classe, non a `__init__`.
- **Directory escluse di default**: `tests`, `benchmarks`, `examples`, `docs`, `build`, `dist` (configurabile con `--exclude`).
- **Nota**: il repo di test è in `test_repo/` (singolare), non `test_repos/` come nel piano; `.gitignore` copre entrambi.

## Fase 2 — Graph builder e CLI

- **Ambiente**: venv in `.venv/` (ignorato da git), dipendenze in `requirements.txt` (`networkx`, `click`).
- **Il parser resta senza dipendenze** (solo stdlib): `networkx` entra solo dopo, in `src/graph.py`.
- **`networkx.MultiDiGraph`**: più relazioni di tipo diverso tra la stessa coppia di nodi; il tipo è la chiave dell'arco.
- **CLI con Click** (`python -m src.cli`): `build`, `stats`, `viz`, `show`. Lanciata come modulo perché `src` è un package
  con import relativi (evita anche il conflitto con il modulo `parser` della stdlib di Python 3.9).
- **Visualizzazione**: pagina HTML autonoma con vis-network (da CDN) invece di matplotlib: ~280 nodi non sono leggibili in un'immagine statica.
- **Note personali** in `noteXme/` (ignorata da git): spiegazioni discorsive e glossario.

## Fase 3 — Layer semantico

- **Embedding locale** con `sentence-transformers` (`all-MiniLM-L6-v2`): gratuito, offline, nessun dato del codice esce dalla macchina. Limite: modello generico inglese, non specifico per codice.
- **ChromaDB** (persistente in `output/chroma/`, spazio coseno) invece di FAISS: salva anche metadati e supporta filtri (per tipo) senza codice in più.
- **Testo del nodo** = tipo + qualname + nome spezzato in parole + firma (da `ast`) + docstring (max 1200 caratteri). Il corpo del codice non è incluso: rumoroso e oltre il limite di ~256 token del modello.
- **Tutti i nodi sono indicizzati**, moduli inclusi; il filtro per tipo è a query time (`-t`).
- **Indice ricostruito da zero** ad ogni `index` (283 nodi, pochi secondi): niente aggiornamento incrementale per ora.
- **Query in inglese**: il modello e le docstring di tqdm sono in inglese.
- **Non rimandiamo** il summary generato da LLM (previsto nel piano): prima si misura se serve. Su 6 query di prova, 5 hanno il nodo giusto al primo posto.

## Fase 4 — Retrieval ibrido

- **Parser: fallback sulle sottoclassi** per `self.x` non trovato nelle basi (template method): la classe base chiama, la sottoclasse definisce. Sovra-approssimazione coerente con la Fase 1.
- **Parser: arco verso `__init__`** quando si istanzia una classe (oltre all'arco verso la classe).
- **Espansione**: callee (default 2 livelli), caller (1 livello), classe contenitrice, basi, sottoclassi, override/overridden_by. Le dipendenze pesano più dei chiamanti, che pesano più della struttura di classe.
- **Rilevanza = 0.7 × grafo + 0.3 × semantica**, con grafo = peso della relazione / distanza. Pesi iniziali non tarati; da misurare in Fase 5.
- **Semantica opzionale**: se l'indice non c'è il ranking usa solo il grafo (`--no-semantic` per forzarlo). Con `-q` la semantica si confronta con la descrizione del task, non con il target.
- **Budget in caratteri** (`--max-chars`, default 12000 ≈ 3000 token): codice completo per i più rilevanti, firma + docstring per gli altri; classi e moduli sempre compatti; troncamento a 120 righe per nodo.
- **Onestà sui punti ciechi**: il contesto include le chiamate non risolte del target e una sezione di "possibili chiamanti" (stesso nome, oggetto di tipo ignoto). Alto richiamo, precisione bassa, dichiarata non verificata.
- **Non inclusi (per ora)**: override nelle sottoclassi dei metodi *chiamati* dal target; chiamanti di una classe (istanziazioni) quando il target è la classe; moduli come target.
- **Differita**: pulizia del rumore nel testo embeddato (Fase 3).

## Fase 5 — Confronto con/senza retrieval (Claude API)

- **Chiave in `.env`** (ignorato da git), letta con `python-dotenv`. Mai committata; comparsa una volta in chiaro in chat → rigenerata.
- **Metodo**: per ogni task, chiedere un *piano di modifica* (non codice eseguibile) in due condizioni (solo target vs target + contesto) e misurare per parola chiave quanti punti di un ground truth scritto a mano compaiono nella risposta. Più economico di validare codice generato, misura la stessa cosa che interessa.
- **`thinking={"type": "disabled"}` esplicito**: senza, `claude-sonnet-5` può consumare l'intero `max_tokens` in "thinking" e restituire una risposta vuota, senza errori evidenti (solo `stop_reason: "max_tokens"`).
- **Limite dichiarato**: tqdm è una libreria nota, il baseline ne beneficia (conoscenza pregressa del modello). Il confronto resta valido (il contesto vince comunque nettamente) ma il divario su codice privato sarebbe probabilmente maggiore.
- Dettagli in `noteXme/07-eval.md`.

## Fase 6 — Server MCP

- **Serve Python 3.10+** per l'SDK `mcp`: il venv è stato ricreato su Python 3.12 (Homebrew), non più 3.9. Tutte le dipendenze reinstallate.
- **`mcp.server.mcpserver.MCPServer`**, non `mcp.server.fastmcp.FastMCP` (nome del piano): rinominata nella versione 2.x dell'SDK installata. Stessa API (`@server.tool()`).
- **Tre tool**: `get_context` (Fase 4), `search_code` (Fase 3), `list_nodes` (elenco/disambiguazione). Tutti di sola lettura.
- **Stato caricato una volta all'avvio**, non ad ogni chiamata: corretto anche un bug in `semantic._model()` che ricaricava il modello di embedding ad ogni chiamata (cache a livello di modulo).
- **Transport stdio**, configurato in `.mcp.json` con percorsi assoluti al Python del venv (non `${workspaceFolder}`, non garantito supportato).
- **Verificato con un client MCP di prova** (script temporaneo) prima di fidarsi della configurazione con Claude Code: tutti e tre i tool rispondono correttamente, inclusa la disambiguazione su nomi ambigui.
- Dettagli in `noteXme/08-mcp.md`.
