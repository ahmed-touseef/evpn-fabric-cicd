"use strict";
// EVPN fabric status page. Reads status.json, which fabctl/status.py rewrites on the server
// every 30 s, and draws it. Every element is built with DOM calls: the page's Content Security
// Policy allows no inline code and no HTML strings.

const SVGNS = "http://www.w3.org/2000/svg";
const REFRESH_MS = 10000;
const STALE_S = 120;           // the collector writes every 30 s
const AGENT_SILENT_S = 300;    // the agent finishes a cycle every minute or so

let status = null;
let fetchFailed = false;

const $ = (id) => document.getElementById(id);

function h(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const kid of kids) if (kid != null && kid !== false) node.append(kid);
  return node;
}

function s(tag, attrs, text) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  if (text != null) node.textContent = text;
  return node;
}

function clearSvg(svg) {
  svg.querySelectorAll(":scope > :not(title)").forEach((n) => n.remove());
}

// ---- Time ----------------------------------------------------------------------------------------

const secondsSince = (iso) => (Date.now() - Date.parse(iso)) / 1000;

function ago(seconds) {
  if (!isFinite(seconds)) return "a long time";
  seconds = Math.max(0, Math.round(seconds));
  if (seconds < 90) return `${seconds} s`;
  if (seconds < 90 * 60) return `${Math.round(seconds / 60)} min`;
  if (seconds < 36 * 3600) return `${Math.round(seconds / 3600)} h`;
  return `${Math.round(seconds / 86400)} d`;
}

function span(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const d = Math.floor(seconds / 86400), hr = Math.floor((seconds % 86400) / 3600), min = Math.floor((seconds % 3600) / 60);
  if (d) return `${d} d ${hr} h`;
  if (hr) return `${hr} h ${min} min`;
  if (min) return `${min} min`;
  return `${seconds} s`;
}

const day = (iso) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
const clock = (iso) => new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
const when = (iso) => `${day(iso)}, ${clock(iso)}`;

// ---- Small helpers -------------------------------------------------------------------------------

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const byName = (list) => Object.fromEntries(list.map((x) => [x.name, x]));

function commitLink(sha) {
  if (!sha) return h("span", { class: "sha", text: "unknown" });
  const short = sha.slice(0, 7);
  if (!status.repository) return h("span", { class: "sha", text: short });
  return h("a", { class: "sha", href: `${status.repository}/commit/${sha}`, text: short });
}

function lastEvent(test) {
  return (status.agent.history || []).find(test);
}

// ---- Overall state -------------------------------------------------------------------------------

function overall() {
  const mark = $("overall-mark"), text = $("overall-text"), sub = $("overall-sub");
  if (!status) {
    mark.className = "mark warn";
    text.textContent = fetchFailed ? "Status not available" : "Loading the latest status";
    sub.textContent = fetchFailed ? "status.json could not be read. The collector on the server may be stopped." : "";
    $("overall").dataset.state = fetchFailed ? "warn" : "wait";
    live(fetchFailed ? "off" : "", fetchFailed ? "Offline" : "Connecting");
    return;
  }
  const age = Date.now() / 1000 - status.generated;
  const problems = status.problems.length;
  const game = status.gameday && status.gameday.running;
  const agentAge = status.agent.checked ? secondsSince(status.agent.checked) : Infinity;
  let state = "ok", line = "Fabric healthy";
  if (age > STALE_S || fetchFailed) {
    state = "warn"; line = `Status is ${ago(age)} old`;
  } else if (game) {
    state = "warn"; line = "Game day: failures injected on purpose";
  } else if (problems) {
    state = "bad"; line = `${plural(problems, "problem")} in the fabric`;
  } else if (agentAge > AGENT_SILENT_S) {
    state = "warn"; line = `Deploy agent silent for ${ago(agentAge)}`;
  }
  mark.className = `mark ${state}`;
  text.textContent = line;
  $("overall").dataset.state = state;
  live(age > STALE_S || fetchFailed ? "off" : "on", age > STALE_S || fetchFailed ? `Stale, ${ago(age)} old` : "Live");

  const parts = [];
  if (status.running) {
    let run = `Running ${status.running.sha.slice(0, 7)}`;
    if (status.behind) run += `, main is ${plural(status.behind, "commit")} ahead`;
    else if (status.main) run += ", the same commit as main";
    parts.push(run + ".");
  }
  if (game) parts.push("The fabric breaks and repairs itself for about four minutes.");
  else if (status.since) parts.push(`${problems ? "Broken" : "Healthy"} for ${span(secondsSince(status.since))}.`);
  parts.push(`Updated ${ago(age)} ago.`);
  sub.textContent = parts.join(" ");
}

function live(state, text) {
  $("live").className = `live ${state}`;
  $("live-text").textContent = text;
}

// ---- The fabric in numbers -----------------------------------------------------------------------

function stats() {
  const list = $("stats");
  list.replaceChildren();
  const sum = (key) => status.routers.reduce((acc, r) => {
    if (r[key]) { acc.up += r[key].up; acc.want += r[key].want; }
    return acc;
  }, { up: 0, want: 0 });
  const tile = (label, have, total, sub, state) => {
    const ratio = total ? Math.min(1, have / total) : 0;
    const value = h("span", { class: "stat-value" }, String(have), total != null ? h("small", { text: ` / ${total}` }) : null);
    let meter = null;
    if (total) {
      const fill = h("i");
      fill.style.width = `${(ratio * 100).toFixed(1)}%`;     // CSSOM, which the CSP allows; a style attribute it would block
      meter = h("span", { class: "meter" }, fill);
    }
    list.append(h("li", { class: `stat ${state || (total && have < total ? "bad" : "")}` },
      h("span", { class: "stat-label", text: label }), value, h("span", { class: "stat-sub", text: sub }), meter));
  };
  const healthy = status.routers.filter((r) => r.answers && !r.problems.length).length;
  tile("Routers healthy", healthy, status.routers.length, "answering, no problems");
  const bgp = sum("sessions"), bfd = sum("bfd");
  tile("BGP sessions", bgp.up, bgp.want, "established, as the intent wants");
  tile("BFD peers", bfd.up, bfd.want, `every ${status.fabric.bfd.interval_ms} ms, ${status.fabric.bfd.multiplier} missed`);
  const up = status.cables.filter((c) => c.state === "up").length;
  tile("Cables up", up, status.cables.length, "fabric, servers and ISP");
  const right = status.reach.filter((p) => p.allowed === p.answers).length;
  tile("Reachability", right, status.reach.length, "server pairs as the intent wants");
  const g = status.gameday;
  if (!g) {
    list.append(h("li", { class: "stat" }, h("span", { class: "stat-label", text: "Last game day" }),
      h("span", { class: "stat-value", text: "None" }), h("span", { class: "stat-sub", text: "first run tonight, 03:30 UTC" })));
  } else if (g.running) {
    list.append(h("li", { class: "stat warn" }, h("span", { class: "stat-label", text: "Game day" }),
      h("span", { class: "stat-value", text: "Running" }), h("span", { class: "stat-sub", text: `started ${ago(secondsSince(g.started))} ago` })));
  } else {
    const m = g.measured || {};
    const worst = [m.spine_cut_worst_outage_s, m.border_down_worst_outage_s, ...Object.values(m.frozen_spine_noticed_s || {})]
      .filter((v) => v != null);
    const sub = worst.length && m.budget_s != null ? `worst ${Math.max(...worst).toFixed(2)} s, budget ${m.budget_s} s` : `${ago(secondsSince(g.time))} ago`;
    list.append(h("li", { class: `stat ${g.passed ? "" : "bad"}` }, h("span", { class: "stat-label", text: "Last game day" }),
      h("span", { class: "stat-value", text: g.passed ? "Passed" : "Failed" }), h("span", { class: "stat-sub", text: sub })));
  }
}

// ---- Map -----------------------------------------------------------------------------------------

const MAP = { width: 1000, spineY: 40, leafY: 186, serverY: 312, boxW: 96, boxH: 34, serverGap: 62, spineGap: 300 };

function layout() {
  const roles = (role) => status.routers.filter((r) => r.role === role).map((r) => r.name);
  const spines = roles("spine"), leaves = roles("leaf"), borders = roles("border");
  const at = {};
  const left = 90, right = MAP.width - 90, gap = 0.5;            // half a slot between leaves and borders
  const slot = (right - left) / Math.max(1, leaves.length + borders.length - 1 + gap);
  leaves.forEach((n, i) => { at[n] = [left + i * slot, MAP.leafY]; });
  borders.forEach((n, i) => { at[n] = [left + (leaves.length + gap + i) * slot, MAP.leafY]; });
  spines.forEach((n, i) => { at[n] = [MAP.width / 2 + (i - (spines.length - 1) / 2) * MAP.spineGap, MAP.spineY]; });
  for (const leaf of leaves) {
    const mine = status.servers.filter((x) => x.leaf === leaf);
    mine.forEach((x, j) => { at[x.name] = [at[leaf][0] + (j - (mine.length - 1) / 2) * MAP.serverGap, MAP.serverY]; });
  }
  const bx = borders.map((b) => at[b][0]);
  at.isp = [bx.reduce((a, b) => a + b, 0) / Math.max(1, bx.length), MAP.serverY];
  return at;
}

function lostServers() {
  const lost = new Set();
  for (const x of status.servers) {
    const mine = status.reach.filter((p) => p.from === x.name && p.allowed);
    if (mine.length && mine.every((p) => !p.answers)) lost.add(x.name);
  }
  return lost;
}

function drawMap() {
  const svg = $("map");
  clearSvg(svg);
  const at = layout();
  const routers = byName(status.routers), servers = byName(status.servers);
  const top = (n) => [at[n][0], at[n][1] - (servers[n] ? 8 : MAP.boxH / 2)];
  const bottom = (n) => [at[n][0], at[n][1] + MAP.boxH / 2];
  const cables = s("g"), flows = s("g", { "aria-hidden": "true" }), nodes = s("g");

  for (const c of status.cables) {
    let a, b;
    if (c.kind === "fabric") { a = top(c.a); b = bottom(c.b); }             // leaf or border up to a spine
    else { a = bottom(c.a); b = top(c.b); }                                 // down to a server or the ISP
    if (!at[c.b] || !at[c.a]) continue;
    let cls = { up: "", down: "down", "no session": "nosession", unknown: "unknown" }[c.state] ?? "unknown";
    if (c.kind === "server" && c.state === "up") cls = servers[c.b].tenant;
    const line = s("line", { class: `cable ${cls}`, x1: a[0], y1: a[1], x2: b[0], y2: b[1] });
    line.append(s("title", {}, `${c.a} ${c.a_port} to ${c.b} ${c.b_port}: ${c.state}`));
    cables.append(line);
    if (c.state === "up") flows.append(s("line", { class: "flow", x1: a[0], y1: a[1], x2: b[0], y2: b[1] }));
  }

  if (routers.isp) {                                                      // the ISP's way out
    const [x, y] = at.isp;
    cables.append(s("line", { class: "cable uplink", x1: x, y1: y + MAP.boxH / 2, x2: x, y2: y + 52 }));
    nodes.append(s("text", { class: "name label-bg", x, y: y + 68, "text-anchor": "middle" }, "internet"));
    nodes.append(s("text", { class: "asn label-bg", x, y: y + 82, "text-anchor": "middle" }, status.internet));
  }

  for (const r of status.routers) {
    if (!at[r.name]) continue;
    const [x, y] = at[r.name];
    const cls = !r.answers ? "silent" : r.problems.length ? "bad" : "";
    const g = s("g", { class: `router ${r.role} ${cls}` });
    g.append(s("rect", { class: "box", x: x - MAP.boxW / 2, y: y - MAP.boxH / 2, width: MAP.boxW, height: MAP.boxH, rx: 8 }));
    g.append(s("circle", { class: "dot", cx: x - MAP.boxW / 2 + 14, cy: y, r: 3.5 }));
    g.append(s("text", { class: "name", x: x + 6, y: y + 4.5, "text-anchor": "middle" }, r.name));
    const asnY = r.role === "spine" ? y - MAP.boxH / 2 - 7 : y + MAP.boxH / 2 + 14;
    g.append(s("text", { class: "asn label-bg", x, y: asnY, "text-anchor": "middle" }, `AS${r.asn}`));
    const facts = [`${r.name}, ${r.role}, AS${r.asn}`];
    if (r.loopback) facts.push(`loopback ${r.loopback}`);
    if (r.sessions) facts.push(`BGP ${r.sessions.up} of ${r.sessions.want}`);
    if (r.bfd) facts.push(`BFD ${r.bfd.up} of ${r.bfd.want}`);
    facts.push(r.answers ? plural(r.problems.length, "problem") : "does not answer");
    g.append(s("title", {}, facts.join(", ")));
    nodes.append(g);
  }

  const lost = lostServers();
  for (const x of status.servers) {
    const [cx, cy] = at[x.name];
    const g = s("g", { class: `server ${x.tenant}${lost.has(x.name) ? " lost" : ""}` });
    g.append(s("rect", { x: cx - 8, y: cy - 8, width: 16, height: 16, rx: 4 }));
    g.append(s("text", { class: "name label-bg", x: cx, y: cy + 24, "text-anchor": "middle" }, x.name));
    g.append(s("text", { class: "asn label-bg", x: cx, y: cy + 38, "text-anchor": "middle" }, `vlan ${x.vlan}`));
    g.append(s("title", {}, `${x.name}, ${x.address}, tenant ${x.tenant}, VLAN ${x.vlan}, on ${x.leaf}` +
                            (lost.has(x.name) ? ": reaches nothing it should" : "")));
    nodes.append(g);
  }
  const bfd = status.fabric.bfd;
  $("map-facts").replaceChildren(...[
    ["underlay", `eBGP unnumbered, BFD ${bfd.interval_ms} ms x ${bfd.multiplier}`],
    ["overlay", "EVPN, VXLAN, symmetric IRB"],
    ["gateway", "anycast .1 on every leaf"]].map(([k, v]) => h("li", {}, h("b", { text: k }), v)));
  svg.append(cables, flows, nodes);

  const broken = status.cables.filter((c) => c.state !== "up");
  $("map-note").textContent = broken.length
    ? `${plural(status.cables.length - broken.length, "cable")} up. Not up: ` +
      broken.map((c) => `${c.a} ${c.a_port} to ${c.b} ${c.b_port} (${c.state})`).join(", ") + "."
    : `All ${status.cables.length} cables up and every BGP session over them established. Every leaf and border has a path through each spine.`;
}

// ---- What runs now -------------------------------------------------------------------------------

function facts() {
  const dl = $("facts");
  dl.replaceChildren();
  const row = (term, ...value) => dl.append(h("dt", { text: term }), h("dd", {}, ...value));
  const sub = (text) => h("span", { class: "sub", text });
  const agent = status.agent;

  if (status.running) {
    const deployed = lastEvent((e) => e.event === "deployed" && status.running.sha.startsWith(e.commit));
    row("Running", commitLink(status.running.sha), ` ${status.running.subject}`,
        sub(deployed ? `deployed ${when(deployed.time)}, ${deployed.plan === "reload" ?
          `${plural(deployed.routers.length, "router")} reloaded, canary ${deployed.canary}` :
          deployed.plan === "rebuild" ? "lab rebuilt" : "no router config changed"}`
          : `committed ${when(status.running.date)}`));
  }

  if (!status.main) {
    row("main", "not fetched yet");
  } else if (!status.behind) {
    row("main", "the same commit", sub("the agent fetches main from GitHub every minute"));
  } else {
    const sha = status.main.sha;
    let why = "the next agent cycle checks it";
    if (agent.refused === sha) {
      const refused = lastEvent((e) => (e.event === "refused" || e.event === "rolled back") && sha.startsWith(e.commit));
      why = refused ? `refused: ${refused.reason || "it failed on this lab and was rolled back"}` : "refused";
    } else if (agent.waiting === sha) {
      why = "waiting for its checks to finish on GitHub";
    }
    row("main", commitLink(sha), ` ${plural(status.behind, "commit")} ahead`, sub(why));
  }

  const age = agent.checked ? secondsSince(agent.checked) : Infinity;
  row("Agent", h("span", { class: age > AGENT_SILENT_S ? "state-warn" : "", text: agent.checked ? `last cycle ${ago(age)} ago` : "no cycle yet" }),
      sub("every minute it compares all routers with Git and undoes any difference"));

  const repair = lastEvent((e) => e.event === "drift healed" || e.event === "drift not healed");
  row("Last repair", repair ? `${repair.router}, ${when(repair.time)}` : "none so far",
      repair && repair.detail ? sub(repair.detail.map((l) => l.replace(/\s+/g, " ").trim()).slice(-1)[0]) : null);

  const gates = h("span", { class: "gates" });
  status.required_checks.forEach((name, i) => {
    if (i) gates.append(h("i", { text: "then" }));
    gates.append(h("span", { text: name }));
  });
  row("Gates", gates, sub("all three green on GitHub for the exact commit, or it never runs here"));
}

// ---- Game day ------------------------------------------------------------------------------------

function gameday() {
  const g = status.gameday, result = $("game-result"), chart = $("game-chart"), failures = $("game-failures");
  clearSvg(chart);
  failures.replaceChildren();
  result.replaceChildren();
  $("game-budget").textContent = "";
  chart.hidden = true;
  if (!g) {
    result.textContent = "No game day yet: the first one runs tonight at 03:30 UTC.";
    return;
  }
  if (g.running) {
    result.append(h("span", { class: "state-warn", text: "Running now" }), `, started ${ago(secondsSince(g.started))} ago on ${g.commit}.`);
    return;
  }
  result.append(h("span", { class: g.passed ? "state-ok" : "state-bad", text: g.passed ? "Passed" : "Failed" }),
                ` ${when(g.time)} on ${g.commit}: ${g.summary}.`);
  for (const line of g.failures || []) failures.append(h("li", { text: line }));

  const m = g.measured || {};
  if (m.budget_s == null) return;
  const rows = [];
  if (m.frozen_spine_noticed_s) {
    rows.push({ group: "A frozen spine was noticed by BFD after" });
    for (const [node, t] of Object.entries(m.frozen_spine_noticed_s)) rows.push({ label: node, value: t });
  }
  rows.push({ group: "Longest gap in pings (every 50 ms) while" });
  if (m.spine_cut_worst_outage_s != null) rows.push({ label: "a spine lost every cable", value: m.spine_cut_worst_outage_s });
  if (m.border_down_worst_outage_s != null) rows.push({ label: "a border went down", value: m.border_down_worst_outage_s });

  const W = 440, labelW = 158, barMax = 200, rowH = 22, topPad = 24;
  const values = rows.filter((r) => r.value != null).map((r) => r.value);
  const scale = Math.max(m.budget_s * 1.2, ...values) || 1;
  const xOf = (v) => labelW + (Math.min(v, scale) / scale) * barMax;
  chart.setAttribute("viewBox", `0 0 ${W} ${topPad + rows.length * rowH + 4}`);
  let y = topPad;
  for (const r of rows) {
    if (r.group) {
      chart.append(s("text", { class: "group", x: 0, y: y + 14 }, r.group));
    } else {
      chart.append(s("text", { class: "label", x: 10, y: y + 14 }, r.label));
      chart.append(s("rect", { class: "track", x: labelW, y: y + 5, width: barMax, height: 11, rx: 1 }));
      const over = r.value == null || r.value > m.budget_s;
      const width = r.value == null ? barMax : Math.max(2, xOf(r.value) - labelW);
      chart.append(s("rect", { class: `bar${over ? " over" : ""}`, x: labelW, y: y + 5, width, height: 11, rx: 1 }));
      chart.append(s("text", { class: "value", x: labelW + barMax + 8, y: y + 14 }, r.value == null ? "never" : `${r.value.toFixed(2)} s`));
    }
    y += rowH;
  }
  const bx = xOf(m.budget_s);
  chart.append(s("line", { class: "budget", x1: bx, x2: bx, y1: 14, y2: y }));
  chart.append(s("text", { class: "budget-label", x: bx, y: 10, "text-anchor": "middle" }, `budget ${m.budget_s} s`));
  chart.hidden = false;
  const bfd = status.fabric.bfd;
  $("game-budget").textContent = `The budget comes from the intent: BFD every ${bfd.interval_ms} ms, ` +
    `${bfd.multiplier} missed to declare a neighbour dead, plus ${(m.budget_s - bfd.interval_ms * bfd.multiplier / 1000).toFixed(1)} s ` +
    "for BGP to withdraw its paths and every switch to install the rest.";
}

// ---- Who reaches whom ----------------------------------------------------------------------------

function reach() {
  const table = $("reach");
  table.replaceChildren();
  const order = status.tenants.map((t) => t.name);
  const hosts = [...status.servers].sort((a, b) => order.indexOf(a.tenant) - order.indexOf(b.tenant) || a.name.localeCompare(b.name));
  const targets = [...hosts.map((x) => ({ name: x.name, key: x.name, tenant: x.tenant })),
                   { name: "internet", key: status.internet, tenant: "none" }];
  const pairs = new Map(status.reach.map((p) => [`${p.from}>${p.to}`, p]));

  const head = h("tr", {}, h("th", { class: "corner", scope: "col", text: "from \\ to" }));
  for (const t of targets) head.append(h("th", { scope: "col", text: t.name }, h("span", { class: `tenant ${t.tenant}` })));
  const body = h("tbody");
  for (const src of hosts) {
    const tr = h("tr", {}, h("th", { scope: "row" }, h("span", { class: `tenant ${src.tenant}` }), src.name));
    for (const t of targets) {
      const p = pairs.get(`${src.name}>${t.key}`);
      let cls = "self", label = "itself";
      if (p) {
        const right = p.allowed === p.answers;
        cls = !right ? "bad" : p.answers ? "yes" : "no";
        label = `${src.name} to ${t.name}: ${p.answers ? "answers" : "no answer"}` +
                (right ? (p.allowed ? ", as the intent wants" : ", the tenants are isolated") :
                         (p.allowed ? ", but the intent says it must" : ", but the intent forbids it"));
      }
      tr.append(h("td", {}, h("span", { class: `cell ${cls}`, role: "img", "aria-label": label, title: label })));
    }
    body.append(tr);
  }
  table.append(h("thead", {}, head), body);
}

// ---- Routers -------------------------------------------------------------------------------------

function routers() {
  const body = $("routers").tBodies[0];
  body.replaceChildren();
  for (const r of status.routers) {
    const count = (x) => (x ? `${x.up}/${x.want}` : "");
    const state = !r.answers ? h("span", { class: "state-bad", text: "no answer" })
      : r.problems.length ? h("span", { class: "state-bad", text: plural(r.problems.length, "problem") })
      : h("span", { class: "state-ok", text: "ok" });
    body.append(h("tr", {},
      h("td", { text: r.name }), h("td", { class: "ip", text: r.asn }), h("td", { class: "ip", text: r.loopback || "" }),
      h("td", { class: "num", text: count(r.sessions) }), h("td", { class: "num", text: count(r.bfd) }), h("td", {}, state)));
  }
  const list = $("problems");
  list.replaceChildren();
  const shown = status.problems.slice(0, 40);
  for (const line of shown) {
    const [who, ...rest] = line.split(" ");
    list.append(h("li", {}, h("b", { text: who }), ` ${rest.join(" ")}`));
  }
  if (status.problems.length > shown.length) list.append(h("li", { text: `and ${status.problems.length - shown.length} more` }));
}

// ---- Deploys and repairs -------------------------------------------------------------------------

function describe(e) {
  const c = e.commit ? commitLink(e.commit) : null;
  switch (e.event) {
    case "deployed":
      return ["ok", "Deployed", c, e.plan === "reload"
        ? `: ${plural(e.routers.length, "router")} reloaded one by one, canary ${e.canary}, ${e.seconds} s`
        : e.plan === "rebuild" ? `: lab rebuilt and tested, ${e.seconds} s` : `: ${e.reason || "no router config changed"}`];
    case "rolled back":
      return ["bad", "Rolled back", c, `: failed on ${(e.routers || []).join(", ") || "the lab"}, back on ${e.previous} in ${e.seconds} s`];
    case "rollback failed":
      return ["bad", "Rollback failed", c, ""];
    case "refused":
      return ["warn", "Refused", c, `: ${e.reason}`];
    case "waiting":
      return ["wait", "Waiting", c, ": checks still running on GitHub"];
    case "drift healed":
      return ["warn", "Drift repaired", null, ` on ${e.router}: a change typed by hand, undone from Git`];
    case "drift not healed":
      return ["bad", "Drift not repaired", null, ` on ${e.router}`];
    case "files restored":
      return ["warn", "Files restored", null, ": a file edited by hand on the server, put back from Git"];
    case "rebuilt":
      return ["ok", "Lab rebuilt", c, `: ${e.reason}`];
    case "rebuild failed":
      return ["bad", "Rebuild failed", c, `: ${e.reason}`];
    case "game day":
      return [e.passed ? "ok" : "bad", e.passed ? "Game day passed" : "Game day failed", null, `: ${e.tests}`];
    case "game day skipped":
      return ["warn", "Game day skipped", null, `: ${e.reason}`];
    default:
      return ["wait", e.event, c, ""];
  }
}

function events() {
  const list = $("events");
  list.replaceChildren();
  const history = status.agent.history || [];
  if (!history.length) {
    list.append(h("li", { class: "none" }, h("span", { class: "empty", text: "Nothing yet." })));
    return;
  }
  for (const e of history) {
    const [cls, what, link, rest] = describe(e);
    const text = h("span", {}, h("span", { class: `what ${cls}`, text: what }), link ? " " : "", link, rest);
    const detail = (e.detail || []).slice(0, 4).map((l) => l.replace(/\s+/g, " ").trim()).join(" | ");
    if (detail && e.event !== "deployed") text.append(h("span", { class: "detail", text: detail }));
    list.append(h("li", { class: cls }, h("time", { datetime: e.time }, day(e.time), h("br"), clock(e.time)), text));
  }
}

// ---- Internet edge -------------------------------------------------------------------------------

function edge() {
  const borders = status.routers.filter((r) => r.role === "border").map((r) => r.name);
  const received = new Map(status.edge.received.map((r) => [r.prefix, r.from]));
  const table = $("received");
  table.replaceChildren();
  const head = h("tr", {}, h("th", { scope: "col", text: "Announced to the ISP" }), h("th", { scope: "col", text: "Tenant" }));
  for (const b of borders) head.append(h("th", { scope: "col", text: `from ${b}` }));
  const body = h("tbody");
  const announce = Object.entries(status.edge.announce);
  for (const [tenant, prefix] of announce) {
    const from = received.get(prefix) || [];
    const tr = h("tr", {}, h("td", { class: "ip", text: prefix }), h("td", { text: tenant }));
    for (const b of borders) {
      tr.append(h("td", {}, from.includes(b) ? h("span", { class: "state-ok", text: "received" })
                                             : h("span", { class: "state-bad", text: "missing" })));
    }
    body.append(tr);
  }
  const wanted = new Set(announce.map(([, p]) => p));
  for (const [prefix, from] of received) {
    if (wanted.has(prefix)) continue;
    const tr = h("tr", {}, h("td", { class: "ip", text: prefix }), h("td", { class: "state-bad", text: "leak" }));
    for (const b of borders) tr.append(h("td", { text: from.includes(b) ? "received" : "" }));
    body.append(tr);
  }
  table.append(h("thead", {}, head), body);

  const dbody = $("defaults").tBodies[0];
  dbody.replaceChildren();
  for (const d of status.edge.default_routes) {
    const all = d.via.length === borders.length;
    const via = d.via.length ? d.via.join(" and ") : "none";
    dbody.append(h("tr", {}, h("td", { text: d.leaf }), h("td", { text: d.tenant }),
      h("td", {}, h("span", { class: all ? "state-ok" : d.via.length ? "state-warn" : "state-bad", text: via }),
                  all ? " (ECMP)" : "")));
  }
}

// ---- Refresh -------------------------------------------------------------------------------------

function draw() {
  overall();
  if (!status) return;
  for (const part of [stats, drawMap, facts, gameday, reach, routers, events, edge]) {
    try {
      part();
    } catch (error) {
      console.error(part.name, error);         // one broken panel must not blank the others
    }
  }
}

async function refresh() {
  try {
    const response = await fetch("status.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    status = await response.json();
    fetchFailed = false;
  } catch (error) {
    fetchFailed = true;
  }
  draw();
}

refresh();
setInterval(refresh, REFRESH_MS);
