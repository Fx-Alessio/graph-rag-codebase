"""Genera una pagina HTML interattiva (vis-network) a partire da output/graph.json."""

import argparse
import json
import webbrowser
from pathlib import Path

TEMPLATE = r"""<!doctype html>
<html lang="it"><head><meta charset="utf-8"><title>Graph — __REPO__</title>
<script src="https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
  :root { --bg:#fff; --fg:#1f2328; --mut:#656d76; --line:#d0d7de; --panel:#f6f8fa; }
  @media (prefers-color-scheme: dark) { :root { --bg:#0d1117; --fg:#e6edf3; --mut:#8b949e; --line:#30363d; --panel:#161b22; } }
  * { box-sizing:border-box } body { margin:0; font:14px system-ui,sans-serif; background:var(--bg); color:var(--fg); display:flex; height:100vh }
  #graph { flex:1; min-width:0 } aside { width:380px; border-left:1px solid var(--line); background:var(--panel); overflow:auto; padding:14px }
  h1 { font-size:15px; margin:0 0 4px } .mut { color:var(--mut); font-size:12px }
  fieldset { border:1px solid var(--line); border-radius:6px; margin:10px 0; padding:6px 10px } legend { font-size:12px; color:var(--mut) }
  label { display:inline-flex; align-items:center; gap:5px; margin-right:12px; cursor:pointer } .sw { width:10px; height:10px; border-radius:50% }
  input[type=search] { width:100%; padding:6px 8px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg) }
  pre { background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:8px; overflow:auto; max-height:320px; font-size:12px }
  a { color:#0969da; cursor:pointer } @media (prefers-color-scheme: dark) { a { color:#58a6ff } } li { margin:2px 0 }
  ul { padding-left:18px; margin:4px 0 }
</style></head><body>
<div id="graph"></div>
<aside>
  <h1>__REPO__</h1><div class="mut" id="stats"></div>
  <input type="search" id="q" placeholder="Cerca nodo (es. tqdm.update)…" style="margin-top:10px">
  <fieldset><legend>Nodi</legend><span id="ntypes"></span></fieldset>
  <fieldset><legend>Archi</legend><span id="etypes"></span></fieldset>
  <div id="detail" class="mut">Clicca un nodo per vedere codice e vicini. Doppio click: isola il vicinato.</div>
</aside>
<script>
const G = __DATA__;
const NCOL = {module:"#8250df", class:"#0969da", function:"#1a7f37", method:"#bf8700"};
const ECOL = {calls:"#8c959f", inherits:"#cf222e", imports:"#0969da", defines:"#d0d7de"};
const deg = {}; G.edges.forEach(e => { deg[e.source]=(deg[e.source]||0)+1; deg[e.target]=(deg[e.target]||0)+1; });
const byId = Object.fromEntries(G.nodes.map(n => [n.id, n]));
const nodes = new vis.DataSet(G.nodes.map(n => ({
  id:n.id, label:n.type==="module" ? n.qualname : n.name, type:n.type, color:NCOL[n.type],
  shape:n.type==="module" ? "box" : n.type==="class" ? "diamond" : "dot",
  size:8 + Math.min(22, 2.5*Math.sqrt(deg[n.id]||1)), font:{size:11, color:getComputedStyle(document.body).color}
})));
const edges = new vis.DataSet(G.edges.map((e,i) => ({
  id:i, from:e.source, to:e.target, etype:e.type, arrows:"to", color:{color:ECOL[e.type], opacity:e.type==="defines"?.5:.8},
  dashes:e.type==="imports", width:e.type==="inherits"?2.5:1, smooth:false
})));
const showN = {module:true, class:true, function:true, method:true};
const showE = {calls:true, inherits:true, imports:false, defines:false};
const nview = new vis.DataView(nodes, {filter:n => showN[n.type]});
const eview = new vis.DataView(edges, {filter:e => showE[e.etype]});
const net = new vis.Network(document.getElementById("graph"), {nodes:nview, edges:eview}, {
  physics:{solver:"forceAtlas2Based", forceAtlas2Based:{gravitationalConstant:-60, springLength:90}, stabilization:{iterations:250}},
  interaction:{hover:true, tooltipDelay:150}, nodes:{borderWidth:1}
});
net.once("stabilizationIterationsDone", () => net.setOptions({physics:false}));

function box(host, obj, key) {
  Object.keys(obj).forEach(k => {
    const l = document.createElement("label");
    l.innerHTML = `<input type="checkbox" ${obj[k]?"checked":""}><span class="sw" style="background:${(key==="n"?NCOL:ECOL)[k]}"></span>${k}`;
    l.firstChild.onchange = ev => { obj[k] = ev.target.checked; (key==="n"?nview:eview).refresh(); };
    document.getElementById(host).appendChild(l);
  });
}
box("ntypes", showN, "n"); box("etypes", showE, "e");
document.getElementById("stats").textContent = Object.entries(G.stats.nodes).map(([k,v]) => v+" "+k).join(" · ");

const esc = s => s.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
function neigh(id, dir) {
  return G.edges.filter(e => (dir==="out" ? e.source : e.target) === id && e.type !== "defines")
    .map(e => `<li>${e.type} → <a data-id="${dir==="out"?e.target:e.source}">${esc(dir==="out"?e.target:e.source)}</a></li>`).join("") || "<li class=mut>nessuno</li>";
}
function select(id) {
  const n = byId[id]; if (!n) return;
  const code = n.type === "module" ? "(modulo: codice omesso)" : n.code;
  document.getElementById("detail").innerHTML =
    `<b>${esc(n.id)}</b><div class="mut">${n.type} · ${n.file}:${n.line}-${n.end_line}</div>` +
    (n.docstring ? `<p>${esc(n.docstring.split("\n\n")[0])}</p>` : "") +
    `<b>Chiama / importa / eredita</b><ul>${neigh(id,"out")}</ul><b>Usato da</b><ul>${neigh(id,"in")}</ul><pre>${esc(code)}</pre>`;
  document.querySelectorAll("#detail a").forEach(a => a.onclick = () => focus(a.dataset.id));
}
function focus(id) { net.selectNodes([id]); net.focus(id, {scale:1.1, animation:true}); select(id); }
net.on("click", p => p.nodes.length && select(p.nodes[0]));
net.on("doubleClick", p => {
  if (!p.nodes.length) return;
  const keep = new Set([p.nodes[0]]);
  G.edges.forEach(e => { if (e.source===p.nodes[0]) keep.add(e.target); if (e.target===p.nodes[0]) keep.add(e.source); });
  nodes.update(G.nodes.map(n => ({id:n.id, hidden:!keep.has(n.id)})));
  document.getElementById("detail").insertAdjacentHTML("afterbegin", `<p><a id="reset">← mostra tutto</a></p>`);
  document.getElementById("reset").onclick = () => { nodes.update(G.nodes.map(n => ({id:n.id, hidden:false}))); select(p.nodes[0]); };
});
document.getElementById("q").oninput = ev => {
  const s = ev.target.value.toLowerCase(); if (s.length < 2) return;
  const hit = G.nodes.find(n => n.id.toLowerCase().includes(s)); if (hit) focus(hit.id);
};
</script></body></html>
"""


def render(graph: dict) -> str:
    slim = {"nodes": graph["nodes"], "edges": graph["edges"], "stats": graph["stats"]}
    data = json.dumps(slim, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("__REPO__", graph["repo"]).replace("__DATA__", data)


def main():
    ap = argparse.ArgumentParser(description="Visualizza graph.json come pagina HTML interattiva.")
    ap.add_argument("graph", type=Path, nargs="?", default=Path("output/graph.json"))
    ap.add_argument("-o", "--output", type=Path, default=Path("output/graph.html"))
    ap.add_argument("--open", action="store_true", help="apre la pagina nel browser")
    args = ap.parse_args()

    args.output.write_text(render(json.loads(args.graph.read_text(encoding="utf-8"))), encoding="utf-8")
    print(f"-> {args.output}")
    if args.open:
        webbrowser.open(args.output.resolve().as_uri())


if __name__ == "__main__":
    main()
