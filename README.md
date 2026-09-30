# Graph-RAG per Codebase

Uno strumento che analizza una codebase Python e ne costruisce un grafo (chiamate, ereditarietà, import), per
recuperare — dato il nome di una funzione — il contesto minimo ma completo da passare a un LLM che deve
modificarla: le sue dipendenze, chi la chiama, le sottoclassi che la ridefiniscono. L'obiettivo è ridurre il
rischio che un'AI "perda pezzi" o rompa qualcosa di collegato che non vedeva.

Ispirato a un approccio di graph-RAG su codice per limitare le allucinazioni di un'AI che lavora su codice
esistente, mantenendo sempre visibili le dipendenze rilevanti.

## Perché

Quando un LLM riceve solo il file (o la funzione) da modificare, non ha modo di sapere cosa altro nel progetto
dipende da quel pezzo di codice. Il rischio è modificare una funzione senza accorgersi che, per esempio, altri tre
moduli la chiamano o che una sottoclasse la ridefinisce. Questo progetto costruisce staticamente la mappa di quelle
relazioni e la usa per comporre un contesto mirato, invece di affidarsi alla sola finestra di codice che l'LLM ha
sotto gli occhi.

## Esempio concreto

Il repo di test è [`tqdm`](https://github.com/tqdm/tqdm). Il metodo `tqdm.write()` è 9 righe; guardandolo da solo
non si direbbe che ha 11 punti collegati nel resto del progetto:

```
$ python -m src.cli context tqdm.write --no-semantic

## Cosa chiama il target (dipendenze)
### tqdm.std:tqdm.external_write_mode
### tqdm.std:tqdm.get_lock

## Chi chiama il target (impatto di una modifica)
### tqdm.contrib.discord:DiscordIO.write
### tqdm.contrib.slack:SlackIO.write
### tqdm.contrib.telegram:TelegramIO.write
... (altri 6 chiamanti/sottoclassi)
```

Modificare la firma di `write()` senza saperlo romperebbe silenziosamente le integrazioni con Slack, Discord e
Telegram. Questo è esattamente il tipo di dipendenza che il progetto vuole rendere visibile prima che venga rotta.

## Come funziona, in breve

```
repo Python ──► parser (ast)  ──► graph.json ──► networkx / embedding ──► retrieval ──► contesto per l'LLM
```

1. **Parser** (`src/parser.py`): legge il codice con `ast` e produce nodi (moduli, classi, funzioni, metodi) e archi
   (`defines`, `imports`, `inherits`, `calls`), risolvendo staticamente alias, re-export e import condizionali.
2. **Grafo** (`src/graph.py`): carica il risultato in `networkx` per interrogarlo (hub, componenti, espansione).
3. **Layer semantico** (`src/semantic.py`): un embedding per nodo (`sentence-transformers`, locale) in ChromaDB,
   per trovare un punto di partenza per significato quando non si conosce il nome esatto.
4. **Retrieval ibrido** (`src/retrieval.py`): dato un nodo target, espande il grafo (dipendenze, chiamanti,
   ereditarietà, override) e ordina per rilevanza = grafo + similarità semantica, entro un budget di caratteri.
5. **Valutazione** (`src/eval.py`): confronto con/senza il contesto recuperato su task di test reali, usando
   l'API di Claude.
6. **Server MCP** (`src/mcp_server.py`): espone il retrieval come tool (`get_context`, `search_code`,
   `list_nodes`) a un agente come Claude Code, così può richiederlo da solo durante una sessione di lavoro.

## Quickstart

Richiede **Python 3.12+** (l'SDK MCP della Fase 6 non funziona con 3.9/3.10).

```bash
git clone https://github.com/Fx-Alessio/graph-rag-codebase
cd graph-rag-codebase
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# repo di test (escluso dal repository, va clonato a parte)
git clone https://github.com/tqdm/tqdm test_repo/tqdm

python -m src.cli build test_repo/tqdm      # -> output/graph.json
python -m src.cli viz --open                # -> output/graph.html (grafo interattivo)
python -m src.cli index                     # embedding dei nodi (scarica un modello ~90 MB la prima volta)
python -m src.cli search "detect terminal width"
python -m src.cli context tqdm.update       # il comando centrale del progetto
```

`output/graph.json` e `output/graph.html` sono già inclusi nel repository (generati su `tqdm`), quindi si può
guardare il grafo anche senza rifare `build`.

### Confronto con/senza retrieval (Fase 5)

Richiede una `ANTHROPIC_API_KEY` in un file `.env` nella radice del progetto (mai committato) e fa chiamate reali
all'API, quindi ha un piccolo costo:

```bash
echo "ANTHROPIC_API_KEY=..." > .env
python -m src.cli eval
```

Su due task di test (aggiungere un parametro che deve propagarsi a 4 sottoclassi; rinominare un metodo con
chiamanti nascosti dietro variabili di tipo ignoto), il piano di modifica con il contesto recuperato trova il
100% dei punti da toccare in entrambi i casi, contro il 50% e l'80% senza contesto. Dettagli e limiti del metodo
in `output/eval/` dopo l'esecuzione.

### Server MCP (Fase 6)

Per usarlo da Claude Code: copia `.mcp.json.example` in `.mcp.json` e sostituisci i percorsi con quelli assoluti
del tuo checkout, poi approva il server la prima volta che apri una sessione `claude` in questa cartella
(`claude mcp list` per controllarne lo stato).

## Stato del progetto

| Fase | Contenuto | Stato |
|---|---|---|
| 1 | Parser (AST → nodi/archi) | ✅ |
| 2 | Grafo (`networkx`) + CLI | ✅ |
| 3 | Layer semantico (embedding + ChromaDB) | ✅ |
| 4 | Retrieval ibrido | ✅ |
| 5 | Confronto con/senza retrieval (Claude API) | ✅ |
| 6 | Server MCP | ✅ |
| 7 | Confronto con LangGraph (opzionale) | non fatto |

Il piano di progetto è in [`PLAN.md`](PLAN.md); le decisioni tecniche, fase per fase, in [`DECISIONS.md`](DECISIONS.md).

## Limiti noti

- **Analisi statica**: chiamate su variabili di tipo ignoto (`t.update()`) o su attributi assegnati in `__init__`
  non sono risolte automaticamente; il contesto le segnala comunque in una sezione dedicata, non verificata.
- **Ricerca semantica imprecisa**: il modello di embedding è generico (non specifico per codice); serve come punto
  di partenza approssimativo, non come risposta definitiva — per questo si combina con l'espansione sul grafo.
- **Un solo linguaggio**: solo Python per ora, come da piano.
- **Valutazione su una libreria nota**: `tqdm` è pubblica e probabilmente nota al modello usato in Fase 5, quindi
  il confronto "senza contesto" beneficia in parte di conoscenza pregressa; su codice privato il divario sarebbe
  presumibilmente maggiore.

## Struttura del repository

```
graph-rag-codebase/
├── src/                  # parser, grafo, semantica, retrieval, eval, server MCP
├── output/               # graph.json e graph.html d'esempio (già generati su tqdm)
├── test_repo/            # repo clonati da analizzare (escluso da git)
├── PLAN.md               # piano di progetto
├── DECISIONS.md          # decisioni tecniche, fase per fase
├── requirements.txt
└── .mcp.json.example     # configurazione MCP di esempio per Claude Code
```
