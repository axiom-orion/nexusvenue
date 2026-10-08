(function () {
  "use strict";
  const D = window.NEXUS_DATA;
  const $ = (id) => document.getElementById(id);
  if (!D) {
    document.querySelector("main").insertAdjacentHTML("afterbegin",
      '<div class="wrap" style="padding:48px 22px"><p>The page data (data.js) failed to load. Reload to try again.</p></div>');
    return;
  }
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
  const esc = (s) => (s == null ? "—" : String(s)).replace(/[&<>"]/g, (c) => ESC[c]);
  const money = (n) => "$" + Math.round(n).toLocaleString("en-US");
  const short = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  const PROP = Object.fromEntries(D.properties.map((p) => [p.code, p]));
  const r = D.resolution, ev = D.eval;

  /* ---------- pipeline diagram + hero stats ---------- */
  const STAGES = [
    ["Extract", "messy multi-CRM"], ["Resolve", "fuzzy entity ER"], ["Graph", "Neo4j load"],
    ["Retrieve", "vector + traversal"], ["Advise", "cited blueprint"], ["Judge", "cross-family eval"],
  ];
  $("pipeline").innerHTML = STAGES.map((s, i) =>
    `<div class="pstage"><div class="pnum">0${i + 1}</div><div class="pt">${s[0]}</div><div class="pd">${s[1]}</div></div>` +
    (i < STAGES.length - 1 ? '<div class="parrow" aria-hidden="true">→</div>' : "")
  ).join("");

  const stat = (num, lbl) => `<div class="stat"><div class="num">${esc(num)}</div><div class="lbl">${esc(lbl)}</div></div>`;
  $("hero-stats").innerHTML = [
    stat(D.counts.beo_history, "bookings in the graph"),
    stat(`${r.accounts_source}→${r.accounts_canonical}`, "account rows resolved"),
    stat(ev.macro_recall.toFixed(2), `macro recall@${ev.k} (offline hash)`),
    stat("3", "judge model families"),
  ].join("");

  /* ---------- the mess ---------- */
  $("properties").innerHTML = D.properties.map((p) =>
    `<div class="card prop-card"><h3>${esc(p.name)}</h3><div class="code">${esc(p.code)}</div><div class="city">${esc(p.city)}</div></div>`
  ).join("");

  const variantList = (items) => items.map((v) =>
    `<div class="variant"><span class="chip prop">${esc(v.property)}</span><span class="chip alias">${esc(v.name)}</span></div>`).join("");

  $("dirty-accounts").innerHTML = D.dirty.accounts.map((a) =>
    `<div class="merge-row">
      <div class="merge-variants stack">${variantList(a.variants)}</div>
      <div class="merge-arrow">resolve →</div>
      <div class="canon">${esc(a.canonical)} <span style="color:var(--muted);font-weight:400">· ${esc(a.industry)}</span>
        <span class="provenance">source_ids: ${a.source_ids.map(esc).join(", ")}</span></div>
    </div>`).join("");

  $("dirty-contacts").innerHTML = D.dirty.contacts.map((c) =>
    `<div class="merge-row">
      <div class="merge-variants stack">${variantList(c.spellings.map((s) => ({ property: s.property, name: s.name })))}</div>
      <div class="merge-arrow">same email →</div>
      <div class="canon">${esc(c.canonical)}<span class="provenance">${esc(c.email)} · ${esc(c.title)}</span></div>
    </div>`).join("");

  /* ---------- entity resolution ---------- */
  $("resolve-stats").innerHTML = [
    stat(`${r.accounts_source} → ${r.accounts_canonical}`, "account rows → canonical"),
    stat(r.accounts_merged, "duplicate rows merged away"),
    stat(`${r.contacts_source} → ${r.contacts_canonical}`, "contact rows → canonical"),
    stat(`${r.pairwise_precision.toFixed(2)} / ${r.pairwise_recall.toFixed(2)}`, "pairwise precision / recall"),
  ].join("");

  $("merge-clusters").innerHTML = r.clusters.map((c) =>
    `<div class="merge-row">
      <div class="merge-variants">${c.aliases.map((a) => `<span class="chip alias">${esc(a)}</span>`).join("")}</div>
      <div class="merge-arrow">→</div>
      <div class="canon">${esc(c.canonical)}</div>
    </div>`).join("");

  $("er-misses").innerHTML =
    `<p class="fine" style="margin:0 0 6px">Scored against the generator's ground truth: <b>${r.false_merges} false merges</b>, so precision comes first.
     Recall is the work ahead. ${r.miss_count} of ${r.truth_companies} companies still have a variant the ${r.fuzz_threshold}-point fuzzy
     threshold can't connect (abbreviations, short forms, added words). Those stay separate until a person confirms them.</p>` +
    r.misses.map((m) =>
      `<div class="merge-row">
        <div class="merge-variants">${m.groups.map((g) => `<span class="chip alias">${g.map(esc).join(" · ")}</span>`)
          .join('<span class="chip neq" title="treated as different companies">≠</span>')}</div>
        <div class="merge-arrow">should be →</div>
        <div class="canon">${esc(m.canonical)}</div>
      </div>`).join("");

  /* ---------- the graph, drawn from a real account ---------- */
  function renderGraph() {
    const g = D.graph_example;
    const hasAgency = Boolean(g.agency);
    const box = (x, y, w, title, sub, color) =>
      `<g><rect x="${x}" y="${y}" width="${w}" height="46" rx="8" fill="var(--surface-3)" stroke="${color}" stroke-width="1.5"/>
       <text x="${x + w / 2}" y="${y + 20}" text-anchor="middle" fill="var(--text)" font-size="12" font-weight="600">${esc(short(title, Math.floor(w / 7.4)))}</text>
       <text x="${x + w / 2}" y="${y + 36}" text-anchor="middle" fill="var(--muted)" font-size="9.5">${esc(short(sub, Math.floor(w / 5.6)))}</text></g>`;
    const line = (x1, y1, x2, y2) => `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="var(--border)" stroke-width="1.5"/>`;
    const label = (x, y, t) =>
      `<g><rect x="${x - t.length * 3.1}" y="${y - 9}" width="${t.length * 6.2}" height="14" rx="4" fill="var(--surface)"/>
       <text x="${x}" y="${y + 2}" text-anchor="middle" fill="var(--faint)" font-size="9">${t}</text></g>`;
    const pY = hasAgency ? 176 : 130;            // planner box top
    const [beoA, beoB] = g.beos;
    const city = (code) => (PROP[code] ? PROP[code].city.split(",")[0] : "");
    const beoLabel = (b) => [b.event_type, `BEO · ${money(b.total_revenue)}`];
    const parts = [];
    if (hasAgency) {
      parts.push(line(190, 59, 290, 145), label(240, 100, "REPRESENTS"));
      parts.push(line(105, pY, 105, 82), label(105, 129, "EMPLOYED_BY"));
      parts.push(box(20, 36, 170, g.agency, "Agency", "var(--green)"));
    }
    parts.push(box(20, pY, 170, g.planner || "Planner", "Planner", "var(--blue)"));
    parts.push(box(290, 134, 170, g.account, "Account · canonical", "var(--cyan)"));
    parts.push(`<text x="375" y="204" text-anchor="middle" fill="var(--muted)" font-size="9.5">${esc("aliases: " + g.aliases.slice(0, 2).join(" · ") + (g.aliases.length > 2 ? ` +${g.aliases.length - 2}` : ""))}</text>`);
    parts.push(`<text x="375" y="220" text-anchor="middle" fill="var(--faint)" font-size="9.5">${esc(g.properties.join(" · "))} · ${esc(money(g.lifetime_revenue))} lifetime</text>`);
    // BEO A is the one this planner booked: BOOKED_BY runs from the BEO to the planner
    parts.push(`<path d="M 560 80 C 500 330, 190 330, 120 ${pY + 46}" fill="none" stroke="var(--border)" stroke-width="1.5"/>`, label(352, 290, "BOOKED_BY"));
    if (beoA) {
      parts.push(line(460, 148, 560, 63), label(510, 100, "EXECUTED"));
      parts.push(box(560, 40, 150, ...beoLabel(beoA), "var(--violet)"));
      parts.push(line(710, 63, 780, 63), label(745, 80, "AT_PROPERTY"));
      parts.push(box(780, 40, 70, beoA.property, city(beoA.property), "var(--amber)"));
    }
    if (beoB) {
      parts.push(line(460, 168, 560, 273), label(510, 224, "EXECUTED"));
      parts.push(box(560, 250, 150, ...beoLabel(beoB), "var(--violet)"));
      parts.push(line(710, 273, 780, 273), label(745, 290, "AT_PROPERTY"));
      parts.push(box(780, 250, 70, beoB.property, city(beoB.property), "var(--amber)"));
    }
    $("graph-svg").innerHTML = `<svg viewBox="0 0 860 360" role="img"
      aria-label="${esc(g.account)} as a graph: account, planner, agency, two bookings and their properties">${parts.join("")}</svg>`;
  }
  renderGraph();

  $("graph-model").innerHTML =
    `<div style="font-size:13px;color:var(--muted);margin-bottom:8px">${D.graph_model.nodes.length} node labels</div>` +
    D.graph_model.nodes.map((n) => `<span class="chip">${esc(n)}</span>`).join("") +
    `<div style="font-size:13px;color:var(--muted);margin:14px 0 8px">Relationships</div>` +
    D.graph_model.relationships.map((e) => `<span class="chip" style="color:var(--cyan)">${esc(e)}</span>`).join("");

  /* ---------- SQL vs Cypher ---------- */
  const highlight = (code, dialect) => {
    let s = esc(code);
    s = s.replace(/(^|\n)(\s*--[^\n]*)/g, (m, a, b) => a + '<span class="cm">' + b + "</span>");
    const kws = dialect === "sql"
      ? /\b(SELECT|FROM|JOIN|ON|WHERE|AND|IN|GROUP BY|SUM|ORDER BY|DESC|LIMIT)\b/g
      : /\b(MATCH|OPTIONAL MATCH|WITH|WHERE|AND|IN|RETURN|AS|collect|DISTINCT|count|sum|round|size|ORDER BY|DESC|LIMIT)\b/g;
    return s.replace(kws, '<span class="kw">$1</span>');
  };
  const isMoney = (c) => c.includes("revenue") || c.includes("spend");
  let sqlActive = 0;
  function renderSql() {
    $("sql-tabs").innerHTML = D.sql_cypher.map((q, i) =>
      `<button type="button" role="tab" aria-selected="${i === sqlActive}" class="tab ${i === sqlActive ? "active" : ""}" data-i="${i}">${esc(q.name)}</button>`).join("");
    const q = D.sql_cypher[sqlActive];
    const rows = q.results.map((row) => "<tr>" + q.columns.map((c) => {
      let v = row[c];
      if (Array.isArray(v)) v = v.join(", ");
      if (isMoney(c)) return `<td class="num"><span class="money">${money(v)}</span></td>`;
      if (typeof v === "number") return `<td class="num">${v}</td>`;
      return `<td>${esc(v)}</td>`;
    }).join("") + "</tr>").join("");
    $("sql-panel").innerHTML = `
      <div class="q-line">${esc(q.question)}</div>
      <div class="grid2">
        <div><div class="code-label sql">SQL — legacy relational <span class="tag bad">blind to variants</span></div><pre class="code-block">${highlight(q.sql, "sql")}</pre></div>
        <div><div class="code-label cypher">Cypher — graph <span class="tag good">one path pattern</span></div><pre class="code-block">${highlight(q.cypher, "cypher")}</pre></div>
      </div>
      <div class="takeaway"><b>Why it matters:</b> ${esc(q.takeaway)}</div>
      <div style="margin-top:16px"><div class="code-label cypher">Results over this dataset (${q.results.length})</div>
        <div class="tbl-wrap"><table><thead><tr>${q.columns.map((c) => `<th${isMoney(c) ? ' class="num"' : ""}>${esc(c)}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div></div>`;
  }
  $("sql-tabs").addEventListener("click", (e) => {
    const b = e.target.closest(".tab");
    if (!b) return;
    sqlActive = +b.dataset.i;
    renderSql();
    const active = $("sql-tabs").querySelector(".tab.active");
    if (active) active.focus();
  });
  renderSql();

  /* ---------- hybrid GraphRAG ---------- */
  const LABELS = {
    botanical_mocktail: "Botanical mocktails", vip_transport: "VIP transport",
    led_wall: "LED wall / broadcast", sustainability: "Zero-waste ESG",
    kosher: "Kosher gala", wellness: "Wellness incentive",
  };
  let rfpActive = 0;

  function renderBlueprint(b) {
    $("blueprint").innerHTML = `
      <span class="bp-badge">representative sample · not model output</span>
      <h4>Executive summary</h4><div class="exec">${esc(b.executive_summary)}</div>
      <h4>Win-probability factors</h4><ul>${b.win_probability_factors.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>
      <h4>Recommended packages</h4>
      ${b.recommended_packages.map((p) => `<div class="pkg"><div class="pn">${esc(p.name)}</div>
        <div class="pdesc">${esc(p.description)}</div>
        <div class="price">${esc(p.pricing_guidance)}</div>
        <div style="margin-top:6px">${p.supporting_beo_ids.map((id) => `<span class="chip id">${esc(id)}</span>`).join("")}</div></div>`).join("")}
      <h4>Historical evidence</h4>
      ${b.historical_evidence.map((e) => `<div class="ev"><span class="chip id">${esc(e.beo_id)}</span><div style="margin-top:5px;color:var(--muted)">${esc(e.insight)}</div></div>`).join("")}
      <h4>Key contacts</h4><ul>${b.key_contacts.map((c) => `<li><b>${esc(c.name)}</b> <span style="color:var(--faint)">(${esc(c.role)})</span> — ${esc(c.why)}</li>`).join("")}</ul>
      <h4>Operational risks</h4><ul>${b.operational_risks.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      <h4>Next steps</h4><ul>${b.next_steps.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
  }

  function renderRfp() {
    $("rfp-picker").innerHTML = D.rfps.map((g, i) =>
      `<button type="button" class="rfp-btn ${i === rfpActive ? "active" : ""}" aria-pressed="${i === rfpActive}" data-i="${i}">${esc(LABELS[g.key] || g.key)}</button>`).join("");
    const g = D.rfps[rfpActive];
    $("rfp-query").innerHTML = `<span class="qk">incoming RFP · ${esc(g.key)}</span>${esc(g.query)}`;

    const gold = new Set((D._gold && D._gold[g.key]) || []);
    const hits = g.context.similar_past_events;
    const top = Math.max(...hits.map((h) => h.score), 0.0001);
    $("retrieval-hits").innerHTML = hits.map((h) =>
      `<div class="hit ${gold.has(h.beo_id) ? "relevant" : ""}">
        <div class="hit-top">
          <span class="chip id">${esc(h.beo_id)}</span>${gold.has(h.beo_id) ? '<span class="gold-mark">✓ gold</span>' : ""}
          <div class="score-bar"><div class="score-fill" style="width:${(h.score / top * 100).toFixed(0)}%"></div></div>
          <span class="score-val">${h.score.toFixed(4)}</span>
        </div>
        <div class="notes">“${esc(h.ops_notes)}”</div>
        <div class="meta"><span><b>${esc(h.event_type)}</b></span><span>${esc(h.account)}</span><span>${esc(h.property)}</span><span>${h.attendees} pax</span><span>${money(h.total_revenue)}</span>${h.agency ? `<span>via ${esc(h.agency)}</span>` : ""}</div>
      </div>`).join("");

    $("portfolios").innerHTML = g.context.account_portfolios.map((p) =>
      `<div class="mini-card"><div class="mt">${esc(p.account)}</div>
        <div class="mrow">${p.events} events · <span style="color:var(--green)">${money(p.lifetime_revenue)}</span> lifetime · avg F&amp;B ${money(p.avg_fb_spend)}</div>
        <div class="mrow">${p.properties.map((x) => `<span class="chip prop">${esc(x.replace("NexusVenue ", ""))}</span>`).join("")}</div>
        ${p.aliases && p.aliases.length > 1 ? `<div class="mrow" style="color:var(--faint)">aliases: ${p.aliases.map(esc).join(", ")}</div>` : ""}
      </div>`).join("");

    $("agencies").innerHTML = g.context.agency_relationships.map((a) =>
      `<div class="mini-card"><div class="mt">${esc(a.agency)}</div>
        <div class="mrow">represents ${a.represented_accounts.length} accounts · ${a.total_events} events · <span style="color:var(--green)">${money(a.total_revenue)}</span></div>
        <div class="mrow" style="color:var(--faint)">${a.represented_accounts.slice(0, 4).map(esc).join(", ")}${a.represented_accounts.length > 4 ? "…" : ""}</div>
      </div>`).join("") || `<div class="mini-card"><div class="mrow">No agency-intermediated bookings in this hit set.</div></div>`;

    renderBlueprint(g.blueprint);
  }
  $("rfp-picker").addEventListener("click", (e) => {
    const b = e.target.closest(".rfp-btn");
    if (!b) return;
    rfpActive = +b.dataset.i;
    renderRfp();
    const active = $("rfp-picker").querySelector(".rfp-btn.active");
    if (active) active.focus({ preventScroll: true });
    $("rfp-query").scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  renderRfp();

  /* ---------- judge ---------- */
  const J = D.judge;
  $("judge-rubric").innerHTML = J.rubric.map((x) =>
    `<div class="rubric-card"><div class="rk">${esc(x.rule)}</div><div class="rn">${esc(x.name)}</div><div class="rd">${esc(x.detail)}</div></div>`).join("");
  $("judge-panel").innerHTML =
    `<div class="finding"><span class="qk">a real disagreement</span>${esc(J.finding)}</div>
     <div class="panel-grid">${J.panel.map((p) =>
      `<div class="panel-card"><div class="pf">${esc(p.family)} judge</div>
        <dl><dt>Caught it</dt><dd>${p.caught ? "yes" : "no"}</dd>
            <dt>Judged</dt><dd>${p.material ? "material" : "immaterial"}</dd></dl>
        <span class="verdict-pill ${p.passed ? "pass" : "fail"}">${p.passed ? "PASSED" : "FAILED"}</span></div>`).join("")}</div>
     <p class="fine">All three families found the discrepancy; they split on whether it matters. That is why a single judge's pass/fail is fragile at the rubric boundary, and why cross-family agreement is worth measuring. ${esc(J.source)}</p>`;

  /* ---------- eval ---------- */
  const pk = "precision@" + ev.k, rk = "recall@" + ev.k;
  const anyTie = ev.queries.some((q) => q.tie_at_cutoff);
  $("eval-table").innerHTML =
    `<thead><tr><th>query</th><th class="num">relevant</th><th class="num">retrieved</th><th class="num">tp</th><th class="num">${pk}</th><th class="num">${rk}</th></tr></thead>
     <tbody>${ev.queries.map((q) =>
      `<tr><td>${esc(q.query_key)}${q.tie_at_cutoff ? '<span class="dagger" title="Score tie at the cutoff: which event makes the top 6 is arbitrary">†</span>' : ""}</td>
        <td class="num">${q.relevant}</td><td class="num">${q.retrieved}</td><td class="num">${q.true_positives}</td>
        <td class="num">${q[pk].toFixed(3)}</td><td class="num">${q[rk].toFixed(3)}</td></tr>`).join("")}</tbody>`;
  $("eval-macro").innerHTML = [
    ["macro " + pk, ev.macro_precision.toFixed(3)],
    ["macro " + rk, ev.macro_recall.toFixed(3)],
    ["backend", `${D.meta.embed_backend} · ${D.meta.embed_dim}d`],
  ].map((m) => `<div class="m"><div class="v">${esc(m[1])}</div><div class="k">${esc(m[0])}</div></div>`).join("");
  const m = ev.measured;
  $("eval-note").innerHTML =
    (anyTie ? "† Events tie on score at the cutoff, so which one makes the top " + ev.k +
      " is arbitrary. This export breaks ties by BEO id; the live index breaks them arbitrarily, so a live run can land a hit or two either way. " : "") +
    (m ? `The README's live run on Neo4j measured hash <b>P@${ev.k} ${m.hash.precision.toFixed(2)} / R@${ev.k} ${m.hash.recall.toFixed(2)}</b> and, with Gemini embeddings, <b>P@${ev.k} ${m.gemini.precision.toFixed(2)} / R@${ev.k} ${m.gemini.recall.toFixed(2)}</b>.` : "");

  /* ---------- incremental sync ---------- */
  $("delta-timeline").innerHTML = D.delta.changes.map((c) =>
    `<div class="tl-item"><div class="tp">${esc(c.path)}</div><div class="td">${esc(c.detail)}</div></div>`).join("");
  $("delta-report").textContent = D.delta.report.join("\n");
  $("delta-idem").innerHTML = `<b>Idempotent.</b> ${esc(D.delta.idempotent)}`;

  /* ---------- footer ---------- */
  $("stack").innerHTML = `<b>Stack</b><br>
    Python 3.11 · Neo4j 5 (vector indexes)<br>
    Anthropic Claude: structured outputs<br>
    Gemini embeddings (1536-dim)<br>
    Cross-family judges: Gemini + xAI Grok<br>
    RapidFuzz · Pydantic v2 · Click<br><br>
    <b>Data provenance</b><br>
    <span style="color:var(--faint)">${esc(D.meta.note)}</span>`;
  $("foot-note").textContent = "NexusVenue · GraphRAG sales intelligence · " + D.meta.dataset;
})();
