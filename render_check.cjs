/* Render the topology page's real script against live API data with a stubbed DOM,
   then inspect the generated SVG. Catches template bugs (NaN, undefined, missing nodes)
   that a syntax check cannot. */
const fs = require("fs");
const vm = require("vm");

const API = "http://127.0.0.1:6060/api/dashboard/topology?scope=last_24_hours";

(async () => {
  const res = await fetch(API, {cache: "no-store"});
  if (!res.ok) { console.log("API HTTP " + res.status); process.exit(1); }
  const DATA = await res.json();
  console.log(`live payload: ${DATA.nodes.length} nodes, ${DATA.edges.length} edges, ` +
              `${DATA.summary.requests} requests, ${DATA.latency.cells.length} latency cells`);

  const code = fs.readFileSync("page_script.js", "utf8");
  const els = {};
  function el(id) {
    if (els[id]) return els[id];
    const e = {id, textContent: "", hidden: false, style: {}, dataset: {},
      classList: {toggle() {}, add() {}, remove() {}},
      setAttribute() {}, getAttribute: () => null, addEventListener() {},
      querySelectorAll: () => [], setPointerCapture() {},
      getBoundingClientRect: () => ({left: 0, top: 0, width: 1090, height: 800})};
    let html = "";
    Object.defineProperty(e, "innerHTML", {get: () => html, set: (v) => { html = String(v); }});
    els[id] = e;
    return e;
  }

  const sandbox = {
    console, JSON, Math, Date, encodeURIComponent, String, Number, Array, Object, Set, Promise, Error,
    setTimeout, clearTimeout,
    fetch: async () => ({ok: true, json: async () => DATA}),
    document: {getElementById: el, querySelector: () => null, body: {classList: {toggle() {}}}},
    window: {matchMedia: () => ({matches: false}), addEventListener() {}},
    localStorage: {getItem: () => null, setItem() {}, removeItem() {}},
    CSS: {escape: (s) => s},
    setInterval: () => 0,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);

  await new Promise((r) => setTimeout(r, 400));

  const svg = els.graph.innerHTML;
  const count = (re) => (svg.match(re) || []).length;
  const checks = [
    ["column headers rendered", count(/class="colhead"/g) === 4],
    ["column counts rendered", count(/class="colcount"/g) === 4],
    ["every node drawn", count(/class="node/g) === DATA.nodes.length],
    ["every edge drawn", count(/class="edge /g) === DATA.edges.length],
    ["flow overlay present", count(/class="flow"/g) > 0],
    ["tier gate badges present", count(/class="gate"/g) > 0],
    ["animation durations set", count(/animation-duration:/g) > 0],
    ["no NaN in SVG", !/NaN/.test(svg)],
    ["no undefined in SVG", !/undefined/.test(svg)],
    ["no empty stroke colour", !/stroke=""/.test(svg)],
    ["no negative or NaN coords", !/(x|y|width|height)="-[0-9]/.test(svg) && !/width="NaN"/.test(svg)],
    ["stamp text built", /refreshed \d+s ago/.test(els.stamp.textContent)],
    ["governance strip built", /configured tiers enforce a data-governance gate/.test(els.govstrip.textContent)],
    ["chips built", /requests/.test(els.chips.innerHTML)],
  ];

  let bad = 0;
  for (const [name, ok] of checks) {
    console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}`);
    if (!ok) bad++;
  }
  console.log(`\nnodes=${DATA.nodes.length} edges=${DATA.edges.length} flow paths=${count(/class="flow"/g)} badges=${count(/class="gate"/g)}`);
  if (bad) {
    const kinds = {};
    DATA.nodes.forEach((n) => { kinds[n.kind] = (kinds[n.kind] || 0) + 1; });
    console.log("\nnode kinds in payload:", JSON.stringify(kinds));
    const drawn = [...svg.matchAll(/class="node[^"]*" data-id="([^"]+)"/g)].map((m) => m[1]);
    console.log(`drawn nodes=${drawn.length} of ${DATA.nodes.length}`);
    console.log("NOT drawn:", DATA.nodes.filter((n) => !drawn.includes(n.id)).map((n) => `${n.kind}:${n.label}`).join(", "));
    const drawnEdges = count(/class="edge /g);
    console.log(`drawn edges=${drawnEdges} of ${DATA.edges.length}`);
    const ids = new Set(drawn);
    const skipped = DATA.edges.filter((e) => !ids.has(e.from) || !ids.has(e.to));
    console.log("edges skipped (endpoint missing):", skipped.length,
                skipped.slice(0, 6).map((e) => `${e.from}->${e.to}`).join(" | "));
    const m = svg.match(/.{0,90}(NaN|undefined).{0,90}/);
    if (m) console.log("sample of offending markup:\n  " + m[0]);
    console.log(`VERDICT: ${bad} check(s) failed`);
    process.exit(1);
  }
  console.log("VERDICT: all render checks passed");
})();
