// Agent Audit Kit, local page. Plain DOM: every text from a run or a pack (transcripts, model
// output, hazard and case text) goes in as text nodes, never parsed as HTML.
"use strict";

(() => {
  const TOKEN = document.querySelector('meta[name="auditkit-token"]').getAttribute("content");
  const view = document.getElementById("view");
  const SVG = "http://www.w3.org/2000/svg";
  const POLL_MS = 1000;

  // ── DOM ────────────────────────────────────────────────────────────────────────────────────

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    setAttrs(el, attrs);
    put(el, children);
    return el;
  }

  function s(tag, attrs, ...children) {
    const el = document.createElementNS(SVG, tag);
    setAttrs(el, attrs);
    put(el, children);
    return el;
  }

  function setAttrs(el, attrs) {
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key.startsWith("on")) {
        if (typeof value === "function") el.addEventListener(key.slice(2), value); // never an inline handler
      } else if (key === "class") {
        el.setAttribute("class", value);
      } else if (key === "dataset") {
        Object.assign(el.dataset, value);
      } else {
        el.setAttribute(key, value === true ? "" : String(value));
      }
    }
  }

  function put(el, children) {
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : String(child));
    }
  }

  const enc = encodeURIComponent;

  // ── Words and numbers, as the report writes them ───────────────────────────────────────────

  function usd(value) {
    if (value === null || value === undefined) return "—";
    return value > 0 && value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
  }

  function pct(value) {
    return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
  }

  function words(verdict) {
    return verdict ? verdict.replace(/_/g, " ") : "—";
  }

  function when(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? iso : `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
  }

  function day(iso) {
    return iso ? when(iso).slice(0, 10) : "";
  }

  function plural(n, one, many) {
    return `${n} ${n === 1 ? one : many || `${one}s`}`;
  }

  function scalarSummary(outcome) {
    if (!outcome || typeof outcome !== "object") return "";
    return Object.entries(outcome)
      .filter(([, v]) => v === null || ["string", "number", "boolean"].includes(typeof v))
      .slice(0, 3)
      .map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`)
      .join(", ");
  }

  function json(value) {
    return JSON.stringify(value, null, 2);
  }

  // A recorded value as one line of text: lists of words joined, anything nested as JSON.
  function valueText(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (Array.isArray(value) && value.every((v) => typeof v !== "object" || v === null)) return value.length ? value.join(", ") : "—";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function recordTable(record, label) {
    return h(
      "div",
      { class: "table-box stacked trial" },
      h(
        "table",
        { "aria-label": label },
        h(
          "tbody",
          null,
          Object.entries(record).map(([key, value]) =>
            h("tr", null, h("th", { scope: "row" }, key.replace(/_/g, " ")), h("td", { class: "mono small" }, key === "command" && Array.isArray(value) ? value.join(" ") : valueText(value))),
          ),
        ),
      ),
    );
  }

  // The longest common prefix of the case ids, cut after a separator: "pack-" shown once, above.
  function commonPrefix(ids) {
    if (ids.length < 2) return "";
    let prefix = ids[0];
    for (const id of ids) while (!id.startsWith(prefix)) prefix = prefix.slice(0, -1);
    const cut = Math.max(prefix.lastIndexOf("-"), prefix.lastIndexOf("_"), prefix.lastIndexOf("."));
    prefix = cut >= 0 ? prefix.slice(0, cut + 1) : "";
    return ids.every((id) => id.length > prefix.length) ? prefix : "";
  }

  // ── Marks: shape and word, never colour alone ──────────────────────────────────────────────

  function icon(path, size, width) {
    return s(
      "svg",
      { "aria-hidden": "true", width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": width, "stroke-linecap": "round", "stroke-linejoin": "round" },
      s("path", { d: path }),
    );
  }

  const CHECK = "M5 12.5 10 17l9-10";
  const CROSS = "M6 6l12 12M18 6 6 18";

  function cellLabel(cell) {
    if (!cell) return "not tagged";
    if (cell.status === "excluded") {
      return cell.valid < cell.needed
        ? `excluded: ${plural(cell.valid, "valid trial")}, ${cell.needed} needed`
        : `excluded: ${cell.reason || "not counted"}`;
    }
    return cell.status;
  }

  function cellMark(cell) {
    const label = cellLabel(cell);
    if (!cell) return h("span", { class: "mark dot", role: "img", "aria-label": label, title: label });
    if (cell.status === "passed") {
      return h("span", { class: "mark pass", role: "img", "aria-label": label, title: label }, icon(CHECK, 13, 3));
    }
    if (cell.status === "failed") {
      return h("span", { class: "mark fail", role: "img", "aria-label": label, title: label }, icon(CROSS, 13, 3));
    }
    return h("span", { class: "mark excl", role: "img", "aria-label": label, title: label });
  }

  function verdictMark(verdict, size = 12) {
    const box = { "aria-hidden": "true", width: size, height: size, viewBox: "0 0 12 12" };
    if (verdict === "met") {
      return s("svg", { ...box, class: "v-met" }, s("circle", { cx: 6, cy: 6, r: 5, fill: "currentColor" }));
    }
    if (verdict === "not_met") {
      return s("svg", { ...box, class: "v-not_met" }, s("path", { d: "M6 1 11.5 11H0.5z", fill: "currentColor" }));
    }
    return s(
      "svg",
      { ...box, class: "v-nd" },
      s("circle", { cx: 6, cy: 6, r: 4.75, fill: "none", stroke: "currentColor", "stroke-width": 1.5 }),
    );
  }

  function verdictText(verdict) {
    return h("span", { class: "verdict" }, verdict ? verdictMark(verdict) : null, words(verdict));
  }

  function warnIcon() {
    return s(
      "svg",
      { "aria-hidden": "true", width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round" },
      s("path", { d: "M12 3 2 21h20L12 3z" }),
      s("path", { d: "M12 10v5" }),
      s("path", { d: "M12 18h.01" }),
    );
  }

  function notice(title, text) {
    return h(
      "p",
      { class: "notice" },
      warnIcon(),
      h("span", null, title ? h("span", { class: "notice-title" }, `${title}. `) : null, text),
    );
  }

  function pill(status, text) {
    return h("span", { class: `pill ${status}` }, text || status.replace(/_/g, " "));
  }

  function legend() {
    return h(
      "ul",
      { class: "legend", "aria-label": "Legend" },
      h("li", null, h("span", { class: "mark pass", "aria-hidden": "true" }, icon(CHECK, 9, 4)), "passed"),
      h("li", null, h("span", { class: "mark fail", "aria-hidden": "true" }, icon(CROSS, 9, 4)), "failed"),
      h("li", null, h("span", { class: "mark excl", "aria-hidden": "true" }), "excluded: too few valid trials"),
      h("li", null, h("span", { class: "mark dot", "aria-hidden": "true" }), "not tagged"),
    );
  }

  // ── API ────────────────────────────────────────────────────────────────────────────────────

  class ApiError extends Error {
    constructor(status, message) {
      super(message);
      this.status = status;
    }
  }

  async function api(path, options = {}) {
    const init = { method: options.method || "GET", headers: { "X-Auditkit-Token": TOKEN }, cache: "no-store", credentials: "same-origin", redirect: "error" };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch {
      throw new ApiError(0, "the local server does not answer: is eval --ui still running?");
    }
    const type = response.headers.get("Content-Type") || "";
    const data = type.startsWith("application/json") ? await response.json() : await response.text();
    if (!response.ok) throw new ApiError(response.status, data && data.error ? data.error : `HTTP ${response.status}`);
    return data;
  }

  // ── Router ─────────────────────────────────────────────────────────────────────────────────

  let generation = 0; // a newer route wins: an older render that resolves late writes nothing

  function hashParts() {
    try {
      return location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
    } catch {
      return [];
    }
  }

  function setTab(tab) {
    for (const link of document.querySelectorAll(".tabs a")) {
      if (link.dataset.tab === tab) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
  }

  function route() {
    const parts = hashParts();
    const tab = ["run", "parameters"].includes(parts[0]) ? parts[0] : "results";
    setTab(tab);
    const gen = ++generation;
    if (tab === "run") return showRun(gen);
    if (tab === "parameters") return showParameters(gen, parts[1]);
    return showResults(gen, parts[1], parts[2], parts[3]);
  }

  function stale(gen) {
    return gen !== generation;
  }

  function loading(text) {
    view.replaceChildren(h("p", { class: "pad muted" }, text));
  }

  function failed(error) {
    view.replaceChildren(h("div", { class: "pad" }, notice("Could not load", error.message)));
  }

  // ── Results ────────────────────────────────────────────────────────────────────────────────

  const runCache = new Map(); // run id → detail
  const selected = new Map(); // run id → hazard id
  const replays = new Map(); // run id → job, while a replay of it runs or just ended

  async function detail(runId, fresh) {
    if (fresh || !runCache.has(runId)) runCache.set(runId, await api(`/api/runs/${enc(runId)}`));
    return runCache.get(runId);
  }

  async function showResults(gen, runId, sub, caseId) {
    loading("Loading runs…");
    let runs;
    try {
      runs = await api("/api/runs");
    } catch (error) {
      if (!stale(gen)) failed(error);
      return;
    }
    if (stale(gen)) return;
    if (!runs.length) {
      view.replaceChildren(
        h(
          "div",
          { class: "main single" },
          h("h1", null, "No run yet"),
          h("p", { class: "muted" }, "A run folder appears here after a live run. Start one from Run: the estimate is free, the launch asks first."),
          h("p", null, h("a", { class: "button", href: "#/run" }, "New run")),
        ),
      );
      return;
    }
    const current = runId || (runs.find((r) => !r.error) || runs[0]).run_id;
    let run;
    try {
      run = await detail(current, !sub);
    } catch (error) {
      if (stale(gen)) return;
      view.replaceChildren(h("div", { class: "workbench" }, rail(runs, current), h("main", { class: "main" }, runPicker(runs, current), notice("Could not load this run", error.message))));
      return;
    }
    if (stale(gen)) return;
    if (sub === "case" && caseId) return renderCase(runs, run, caseId);
    if (sub === "report") return renderReport(gen, runs, run);
    renderRun(runs, run);
  }

  function railMeta(r) {
    if (r.error) return "unreadable";
    if (r.status !== "completed") return r.status.replace(/_/g, " ");
    if (!r.graded_at) return "not graded";
    return `${r.graded_by === "replay" ? "replayed" : "graded"} ${day(r.graded_at)}`;
  }

  function rail(runs, current) {
    return h(
      "aside",
      { class: "rail", "aria-label": "Runs" },
      h("h2", null, "RUNS"),
      h(
        "ul",
        null,
        runs.map((r) =>
          h(
            "li",
            null,
            h(
              "a",
              { href: `#/results/${enc(r.run_id)}`, "aria-current": r.run_id === current ? "true" : null },
              h("span", { class: "run-id" }, r.run_id),
              r.error ? null : h("span", { class: "run-meta" }, `${r.pack} · ${plural(r.cases, "case")} × ${r.repeats} · ${usd(r.spent_usd)}`),
              h("span", { class: "run-meta" }, railMeta(r)),
            ),
          ),
        ),
      ),
    );
  }

  function runPicker(runs, current) {
    const select = h(
      "select",
      { id: "run-picker", onchange: (e) => (location.hash = `#/results/${enc(e.target.value)}`) },
      runs.map((r) => h("option", { value: r.run_id, selected: r.run_id === current }, `${r.run_id} · ${railMeta(r)}`)),
    );
    return h("div", { class: "rail-select" }, h("label", { class: "label", for: "run-picker" }, "Run"), select);
  }

  function modelsFact(models) {
    const served = models.map((m) => (m.served.length ? m.served.join(", ") : `${m.requested} (requested)`));
    const unique = [...new Set(served)];
    if (unique.length === 1) return served.length > 1 ? `${unique[0]} ×${served.length}` : unique[0];
    return models.map((m, i) => `${m.role}: ${served[i]}`).join(" · ");
  }

  function facts(run) {
    const f = run.facts;
    const graded = f.graded_at ? `${f.graded_by || "graded"} · ${when(f.graded_at)}` : "not graded";
    const cell = (label, value, mono) => h("div", null, h("dt", null, label), h("dd", { class: mono ? "mono small" : null }, value));
    return h(
      "dl",
      { class: "facts" },
      cell("Trials", `${f.counted} of ${f.planned} counted`),
      cell("Cost", `${usd(f.spent_usd)} of a ${usd(f.budget_usd)} cap`),
      cell("Models", modelsFact(f.models), true),
      cell("Graded", graded),
    );
  }

  function titleRow(run, back) {
    const replay = replays.get(run.run_id);
    const busy = replay && replay.running;
    return h(
      "div",
      { class: "title-row" },
      h(
        "div",
        null,
        back,
        h("h1", null, `Run ${run.run_id}`),
        h("p", { class: "subtitle" }, `${run.pack.title} ${run.pack.version} · variant ${run.variant} · target ${run.target}`),
      ),
      h(
        "div",
        { class: "actions" },
        h("button", { type: "button", disabled: busy, onclick: () => startReplay(run.run_id) }, busy ? "Replaying (free)…" : "Replay (free)"),
        run.has_report ? h("a", { class: "link-44", href: `#/results/${enc(run.run_id)}/report` }, "report.md") : null,
      ),
    );
  }

  function replayStatus(run) {
    const job = replays.get(run.run_id);
    if (!job) return null;
    if (job.error) return notice("Replay not started", job.error);
    if (job.running) return h("p", { class: "status-line muted", role: "status" }, "Replay running: re-grading the saved transcripts from the snapshot, no model call.");
    if (job.exit_code === 0) return h("p", { class: "status-line muted", role: "status" }, `Replayed at ${when(job.finished_at)}: report written.`);
    return h(
      "div",
      { role: "status" },
      notice(`Replay ended with exit ${job.exit_code}`, job.exit_meaning || ""),
      h("pre", { class: "log" }, job.log.slice(-12).join("\n")),
    );
  }

  function renderRun(runs, run) {
    const hazards = run.hazards;
    let current = selected.get(run.run_id);
    if (!hazards.some((x) => x.id === current)) current = hazards.length ? hazards[0].id : null;
    const panel = h("aside", { class: "panel hazard-panel", "aria-label": "Selected hazard" });
    const matrix = matrixGrid(run, current, select);
    const list = matrixList(run);

    function select(hazardId) {
      selected.set(run.run_id, hazardId);
      for (const row of matrix.querySelectorAll(".m-row[data-hazard]")) {
        const on = row.dataset.hazard === hazardId;
        row.classList.toggle("selected", on);
        row.querySelector(".hazard-button").setAttribute("aria-pressed", on ? "true" : "false");
      }
      const hazard = hazards.find((x) => x.id === hazardId);
      panel.replaceChildren(...(hazard ? hazardDetail(run, hazard) : [h("p", { class: "muted" }, "No hazard in this run.")]));
    }

    const main = h(
      "main",
      { class: "main" },
      runPicker(runs, run.run_id),
      titleRow(run),
      facts(run),
      h("details", { class: "manifest" }, h("summary", null, "Manifest: what ran, as recorded before the first model call"), recordTable(run.manifest, "Manifest")),
      run.notices.map((n) => notice(n.title, n.text)),
      replayStatus(run),
      h("div", { class: "section-head" }, h("h2", null, "Hazards × cases"), legend()),
      hazards.length ? [matrix, list] : h("p", { class: "muted" }, "No hazard could be read from the run's snapshot."),
      h(
        "div",
        { class: "columns" },
        h(
          "section",
          null,
          h("h2", null, "Top findings"),
          run.findings.length
            ? h("ol", null, run.findings.map((f) => h("li", null, f.text, f.excerpt.map((line) => h("div", { class: "excerpt" }, line)))))
            : h("p", { class: "muted" }, run.has_report ? "None." : "No report yet."),
        ),
        h(
          "section",
          null,
          h("h2", null, "What this evidence cannot show"),
          run.limits.length ? h("ul", { class: "limits" }, run.limits.map((line) => h("li", null, line))) : h("p", { class: "muted" }, "No report yet."),
        ),
      ),
    );
    view.replaceChildren(h("div", { class: "workbench" }, rail(runs, run.run_id), main, panel));
    if (current) select(current);
    else panel.replaceChildren(h("p", { class: "muted" }, "No hazard in this run."));
  }

  function caseHref(run, caseId) {
    return `#/results/${enc(run.run_id)}/case/${enc(caseId)}`;
  }

  function matrixGrid(run, current, select) {
    const ids = run.case_ids;
    const prefix = commonPrefix(ids);
    // the box scrolls sideways past this width; the hazard column stays in view
    const columns = `minmax(160px, 2fr) repeat(${ids.length}, 112px) 60px 68px minmax(120px, 1.2fr)`;
    const width = 160 + ids.length * 112 + 60 + 68 + 120 + 10 * (ids.length + 3) + 28;
    const row = (cls, attrs, ...cells) => {
      const el = h("div", { class: cls, role: "row", ...attrs }, ...cells);
      el.style.gridTemplateColumns = columns;
      return el;
    };
    const head = row(
      "m-row m-head",
      null,
      h("div", { class: "m-hazard label", role: "columnheader" }, "Hazard"),
      ids.map((id) =>
        h(
          "div",
          { role: "columnheader" },
          h("a", { class: "case-head", href: caseHref(run, id), title: id }, prefix ? h("span", { class: "prefix" }, prefix) : null, h("span", { class: "cid" }, id.slice(prefix.length))),
        ),
      ),
      h("div", { class: "label", role: "columnheader" }, "Cases"),
      h("div", { class: "label", role: "columnheader" }, "Upper bound"),
      h("div", { class: "label", role: "columnheader" }, "Verdict"),
    );
    const rows = run.hazards.map((hz) =>
      row(
        `m-row${hz.id === current ? " selected" : ""}`,
        { dataset: { hazard: hz.id }, onclick: (e) => (e.target.closest("a") ? null : select(hz.id)) },
        h(
          "div",
          { class: "m-hazard", role: "rowheader" },
          h(
            "button",
            { type: "button", class: "hazard-button", "aria-pressed": hz.id === current ? "true" : "false" },
            h("span", null, h("span", { class: "hazard-id" }, hz.id), hz.title),
            h("span", { class: "hazard-sev" }, hz.severity),
          ),
        ),
        ids.map((id) => {
          const cell = hz.cells[id];
          return h("div", { class: "m-cell", role: "cell" }, cellMark(cell), cell && cell.status === "excluded" ? h("span", { class: "excl-count" }, `${cell.valid}/${cell.needed}`) : null);
        }),
        h("div", { class: "num", role: "cell" }, hz.cases_counted === null ? "—" : `${hz.cases_counted} / ${hz.cases_tagged}`),
        h("div", { class: "num", role: "cell" }, pct(hz.upper_bound)),
        h("div", { role: "cell" }, verdictText(hz.verdict)),
      ),
    );
    const inner = h("div", { role: "rowgroup" }, head, rows);
    inner.style.minWidth = `${width}px`;
    return h("div", { class: "matrix", role: "table", "aria-label": "Hazards × cases" }, inner);
  }

  // At 390: one row per hazard; a tap opens its detail in place.
  function matrixList(run) {
    const prefix = commonPrefix(run.case_ids);
    return h(
      "ul",
      { class: "mlist", "aria-label": "Hazards × cases" },
      run.hazards.map((hz) => {
        const detailBox = h("div", { class: "mlist-detail hidden" });
        const button = h(
          "button",
          {
            type: "button",
            class: "mlist-row",
            "aria-expanded": "false",
            onclick: () => {
              const open = button.getAttribute("aria-expanded") !== "true";
              button.setAttribute("aria-expanded", open ? "true" : "false");
              detailBox.classList.toggle("hidden", !open);
              detailBox.replaceChildren(...(open ? hazardDetail(run, hz) : []));
            },
          },
          h("span", { class: "hazard-id" }, hz.id),
          h("span", { class: "strong" }, hz.title),
          h(
            "span",
            { class: "line small muted" },
            h("span", null, [hz.severity, hz.cases_counted === null ? "—" : `${hz.cases_counted} / ${hz.cases_tagged} cases`, `bound ${pct(hz.upper_bound)}`].join(" · ")),
            verdictText(hz.verdict),
          ),
          h(
            "span",
            { class: "line" },
            run.case_ids.map((id) => h("span", { class: "mlist-cell" }, cellMark(hz.cells[id]), id.slice(prefix.length))),
          ),
        );
        return h("li", null, button, detailBox);
      }),
    );
  }

  function caseOutcome(run, caseId) {
    const trial = run.trials.find((t) => t.case_id === caseId && t.outcome);
    return trial ? scalarSummary(trial.outcome) : "";
  }

  function hazardDetail(run, hz) {
    const tagged = run.case_ids.filter((id) => hz.cells[id]);
    const reason = hz.reasons.join("; ");
    return [
      h("p", { class: "panel-kicker" }, `${hz.id} · ${hz.severity}`),
      h("h2", { class: "panel-title" }, hz.title),
      h(
        "div",
        { class: "verdict-box" },
        verdictText(hz.verdict),
        reason ? h("p", null, reason) : null,
        hz.deferred_reason ? h("p", null, `Deferred: ${hz.deferred_reason}`) : null,
        h("p", null, `Criterion, declared before the run: ${hz.criterion}.`),
      ),
      h(
        "dl",
        { class: "grid2" },
        h("div", null, h("dt", null, "Cases counted / tagged"), h("dd", { class: "num" }, hz.cases_counted === null ? "—" : `${hz.cases_counted} / ${hz.cases_tagged}`)),
        h("div", null, h("dt", null, "Failed"), h("dd", { class: "num" }, hz.cases_counted ? String(hz.cases_failed) : "—")),
        h("div", null, h("dt", null, "Observed rate"), h("dd", { class: "num" }, pct(hz.observed_rate))),
        h("div", null, h("dt", null, `Upper bound (${Math.round(hz.confidence * 100)}%)`), h("dd", { class: "num" }, pct(hz.upper_bound))),
      ),
      h("h3", null, "Controls"),
      h("ul", { class: "plain-list" }, hz.controls.map((c) => h("li", null, h("span", { class: "id" }, c.id), " ", c.requirement))),
      h("h3", null, "Checks"),
      hz.checks.length
        ? h("ul", { class: "plain-list tight" }, hz.checks.map((c) => h("li", null, c.id, h("span", { class: "muted" }, ` · ${c.method}`))))
        : h("p", { class: "muted plain-list" }, "No check is linked to this hazard."),
      h("h3", null, "Cases"),
      tagged.length
        ? tagged.map((id) => {
            const cell = hz.cells[id];
            const outcome = caseOutcome(run, id);
            return h(
              "div",
              { class: "panel-case" },
              h(
                "div",
                null,
                h("div", { class: "full" }, id),
                h("div", { class: "meta" }, pill(cell.status), `${cell.passed} of ${plural(cell.trials, "trial")} passed${outcome ? ` · ${outcome}` : ""}`),
              ),
              h("a", { class: "link-44", href: caseHref(run, id) }, "Open transcript"),
            );
          })
        : h("p", { class: "muted" }, "No case of this run is tagged with this hazard."),
    ].flat();
  }

  // ── A case: its trials, each check's result, the transcript ────────────────────────────────

  function renderCase(runs, run, caseId) {
    const back = h("a", { class: "back", href: `#/results/${enc(run.run_id)}` }, `← Run ${run.run_id}`);
    const info = run.cases.find((c) => c.id === caseId);
    const trials = run.trials.filter((t) => t.case_id === caseId).sort((a, b) => a.n - b.n);
    const main = h(
      "main",
      { class: "main" },
      back,
      h("h1", { class: "mono" }, caseId),
      info ? h("p", { class: "muted" }, info.title) : null,
      run.case_ids.includes(caseId) ? null : notice("Not in this run", "This case is not in the run's selection."),
      trials.length ? trials.map((t) => trialSection(run, t)) : h("p", { class: "muted trial" }, "No graded trial for this case: replay the run to grade it."),
    );
    const hazards = run.hazards.filter((hz) => hz.cells[caseId]);
    const panel = h(
      "aside",
      { class: "panel", "aria-label": "Hazards of this case" },
      h("p", { class: "panel-kicker" }, "Hazards of this case"),
      h("h2", { class: "panel-title" }, plural(hazards.length, "hazard")),
      hazards.map((hz) => {
        const cell = hz.cells[caseId];
        return h(
          "div",
          { class: "panel-case" },
          h(
            "div",
            null,
            h("div", null, h("span", { class: "id" }, hz.id), " ", hz.title),
            h("div", { class: "meta" }, pill(cell.status), cell.reason || `${cell.passed} of ${plural(cell.trials, "trial")} passed`),
          ),
        );
      }),
    );
    view.replaceChildren(h("div", { class: "workbench" }, rail(runs, run.run_id), main, panel));
  }

  function trialSection(run, trial) {
    const box = h("div");
    const toggle = h("button", { type: "button", "aria-expanded": "false" }, "Show transcript");
    let loaded = false;

    async function open(turn) {
      if (!loaded) {
        toggle.disabled = true;
        box.replaceChildren(h("p", { class: "muted" }, "Loading the transcript…"));
        try {
          const found = await api(`/api/runs/${enc(run.run_id)}/trials/${enc(trial.case_id)}/${trial.n}`);
          box.replaceChildren(transcriptView(found));
          loaded = true;
        } catch (error) {
          box.replaceChildren(notice("Could not load the transcript", error.message));
        }
        toggle.disabled = false;
      }
      box.classList.remove("hidden");
      toggle.setAttribute("aria-expanded", "true");
      toggle.textContent = "Hide transcript";
      if (turn !== undefined) {
        const target = box.querySelector(`[data-turn="${Number(turn)}"]`);
        if (target) {
          for (const el of box.querySelectorAll(".flash")) el.classList.remove("flash");
          target.classList.add("flash");
          target.scrollIntoView({ block: "center" });
        }
      }
    }

    toggle.addEventListener("click", () => {
      if (toggle.getAttribute("aria-expanded") === "true") {
        box.classList.add("hidden");
        toggle.setAttribute("aria-expanded", "false");
        toggle.textContent = "Show transcript";
      } else {
        open();
      }
    });

    const rows = trial.results.map((r) =>
      h(
        "tr",
        null,
        h("td", { class: "mono small", "data-label": "Check" }, r.check_id),
        h("td", { "data-label": "Result" }, pill(r.outcome)),
        h("td", { class: "small", "data-label": "Note" }, [r.note || "—", r.fault ? ` (fault: ${r.fault})` : ""]),
        h(
          "td",
          { class: "small", "data-label": "Turn" },
          r.evidence_turn === null ? "—" : h("button", { type: "button", class: "link", onclick: () => open(r.evidence_turn) }, `turn ${r.evidence_turn}`),
        ),
      ),
    );
    return h(
      "section",
      { class: "trial" },
      h(
        "div",
        { class: "trial-head" },
        h("h2", null, `Trial ${trial.n}`),
        pill("neutral", trial.status === "valid" ? "counted" : trial.status.replace(/_/g, " ")),
        h("span", { class: "small muted" }, [`attempt ${trial.attempt}`, trial.stop_reason ? ` · ${trial.stop_reason.replace(/_/g, " ")}` : "", trial.cost_usd !== null ? ` · ${usd(trial.cost_usd)}` : ""]),
      ),
      trial.reasons.length ? h("p", { class: "small muted" }, `Not counted: ${trial.reasons.join("; ")}`) : null,
      trial.outcome ? h("p", { class: "small" }, h("span", { class: "muted" }, "Final outcome: "), h("span", { class: "mono" }, JSON.stringify(trial.outcome))) : null,
      h(
        "div",
        { class: "table-box stacked trial" },
        h("table", null, h("thead", null, h("tr", null, h("th", null, "Check"), h("th", null, "Result"), h("th", null, "Note"), h("th", null, "Turn"))), h("tbody", null, rows)),
      ),
      h("p", { class: "trial" }, toggle),
      box,
    );
  }

  function transcriptView(found) {
    const t = found.trial;
    return h(
      "div",
      null,
      h(
        "p",
        { class: "small muted" },
        `${t.status} · ${t.stop_reason.replace(/_/g, " ")} · started ${when(t.started_at)} · ${usd(t.cost_usd)} · sha256 ${found.sha256.slice(0, 12)}`,
      ),
      t.error ? notice(`Error${t.fault ? ` (fault: ${t.fault})` : ""}`, t.error) : null,
      h(
        "ol",
        { class: "turns" },
        t.transcript.turns.map((turn) =>
          h(
            "li",
            { class: `turn ${turn.role}`, dataset: { turn: String(turn.index) } },
            h(
              "div",
              { class: "turn-head" },
              h("span", { class: "who" }, turn.role === "user" ? "Simulated user" : "Agent"),
              h("span", null, `turn ${turn.index}`),
              turn.source === "script" ? h("span", null, `scripted${turn.stimulus_id ? `: ${turn.stimulus_id}` : ""}`) : null,
              turn.end_conversation ? h("span", null, "ends the conversation") : null,
            ),
            h("p", { class: `turn-text${turn.text ? "" : " muted"}` }, turn.text || "(no text)"),
            turn.overridden_text ? h("p", { class: "small muted" }, `The simulator wrote, replaced by the script: ${turn.overridden_text}`) : null,
            turn.hidden ? h("details", null, h("summary", null, "Side channel (what the simulated user meant)"), h("pre", { class: "json" }, json(turn.hidden))) : null,
            turn.tool_calls.map((call) =>
              h(
                "div",
                { class: "tool" },
                h("div", { class: "small" }, h("span", { class: "muted" }, "Tool call "), h("span", { class: "mono" }, call.name), call.is_error ? h("span", { class: "muted" }, " · returned an error") : null),
                h("pre", { class: "json" }, json(call.arguments)),
                call.result === null || call.result === undefined ? null : h("details", null, h("summary", null, "Result"), h("pre", { class: "json" }, json(call.result))),
              ),
            ),
            turn.final_output ? h("div", { class: "tool" }, h("div", { class: "small muted" }, "Final output"), h("pre", { class: "json" }, json(turn.final_output))) : null,
          ),
        ),
      ),
    );
  }

  async function renderReport(gen, runs, run) {
    let text;
    try {
      text = await api(`/api/runs/${enc(run.run_id)}/report`);
    } catch (error) {
      text = null;
      if (stale(gen)) return;
      view.replaceChildren(h("div", { class: "workbench" }, rail(runs, run.run_id), h("main", { class: "main" }, notice("Could not load report.md", error.message))));
      return;
    }
    if (stale(gen)) return;
    view.replaceChildren(
      h(
        "div",
        { class: "workbench" },
        rail(runs, run.run_id),
        h(
          "main",
          { class: "main" },
          h("a", { class: "back", href: `#/results/${enc(run.run_id)}` }, `← Run ${run.run_id}`),
          h("h1", null, "report.md"),
          h("p", { class: "muted" }, "As written in the run folder, shown as text."),
          h("pre", { class: "log report-text" }, text),
        ),
      ),
    );
  }

  async function startReplay(runId) {
    replays.set(runId, { running: true, log: [] });
    rerender();
    try {
      const job = await api(`/api/runs/${enc(runId)}/replay`, { method: "POST", body: {} });
      replays.set(runId, job);
      watch(job.id, (update) => {
        replays.set(runId, update);
        if (update.running) return;
        runCache.delete(runId);
        rerender();
      });
    } catch (error) {
      replays.set(runId, { error: error.message, running: false });
      rerender();
    }
  }

  function rerender() {
    const parts = hashParts();
    if (!["run", "parameters"].includes(parts[0])) route();
  }

  // ── Jobs ───────────────────────────────────────────────────────────────────────────────────

  const watchers = new Map(); // job id → timer

  function watch(jobId, onUpdate) {
    if (watchers.has(jobId)) return;
    const tick = async () => {
      let job;
      try {
        job = await api(`/api/jobs/${jobId}`);
      } catch {
        watchers.set(jobId, setTimeout(tick, POLL_MS * 3));
        return;
      }
      onUpdate(job);
      if (job.running) watchers.set(jobId, setTimeout(tick, POLL_MS));
      else watchers.delete(jobId);
    };
    watchers.set(jobId, setTimeout(tick, POLL_MS));
  }

  // ── Run ────────────────────────────────────────────────────────────────────────────────────

  const form = { pack: null, variant: "default", target: null, all: true, cases: new Set(), repeats: "", maxTurns: "", budget: "" };
  const packCache = new Map();
  let plan = null; // the last estimate, valid for the form as it was
  let confirming = false;
  let liveJob = null;

  async function pack(name) {
    if (!packCache.has(name)) packCache.set(name, await api(`/api/packs/${enc(name)}`));
    return packCache.get(name);
  }

  async function showRun(gen) {
    loading("Loading packs…");
    let packs;
    let latest;
    try {
      [packs, latest] = await Promise.all([api("/api/packs"), api("/api/jobs")]);
    } catch (error) {
      if (!stale(gen)) failed(error);
      return;
    }
    if (stale(gen)) return;
    if (latest && latest.kind === "live") liveJob = latest;
    const usable = packs.filter((p) => !p.error);
    if (!usable.length) {
      view.replaceChildren(h("div", { class: "main single" }, h("h1", null, "New run"), notice("No pack", "The packs folder holds no pack that reads.")));
      return;
    }
    if (!usable.some((p) => p.name === form.pack)) form.pack = usable[0].name;
    let params;
    try {
      params = await pack(form.pack);
    } catch (error) {
      if (!stale(gen)) failed(error);
      return;
    }
    if (stale(gen)) return;
    renderRunForm(usable, params);
    if (liveJob && liveJob.running) followLive(liveJob.id);
  }

  function invalidate() {
    plan = null;
    confirming = false;
  }

  function numberField(id, label, key, hint, attrs) {
    const input = h("input", {
      id,
      type: "number",
      inputmode: "decimal",
      value: form[key],
      oninput: (e) => {
        form[key] = e.target.value;
        e.target.removeAttribute("aria-invalid");
        invalidate();
        renderPlan();
      },
      "aria-describedby": `${id}-hint`,
      ...attrs,
    });
    return h("div", { class: "field" }, h("label", { for: id }, label), input, h("span", { class: "hint", id: `${id}-hint` }, hint));
  }

  let planBox = null;
  let jobBox = null;
  let currentParams = null;

  function renderRunForm(packs, params) {
    currentParams = params;
    const variant = params.variants.find((v) => v.name === form.variant) || params.variants[0];
    form.variant = variant.name;
    const targets = params.targets.map((t) => t.name);
    if (!targets.includes(form.target)) form.target = params.defaults.target;
    for (const id of [...form.cases]) if (!variant.cases.some((c) => c.id === id)) form.cases.delete(id);
    if (form.budget === "" && params.budget.usd !== null) form.budget = String(params.budget.usd);

    const change = (key) => (e) => {
      form[key] = e.target.value;
      invalidate();
      if (key === "pack") {
        form.variant = "default";
        form.target = null;
        form.cases.clear();
        route();
        return;
      }
      renderRunForm(packs, params);
    };

    const picks = h(
      "div",
      { class: `case-picks${form.all ? " hidden" : ""}`, role: "group", "aria-label": "Cases to run" },
      variant.cases.map((c) =>
        h(
          "label",
          { class: "check" },
          h("input", {
            type: "checkbox",
            value: c.id,
            checked: form.cases.has(c.id),
            onchange: (e) => {
              if (e.target.checked) form.cases.add(c.id);
              else form.cases.delete(c.id);
              invalidate();
              renderPlan();
            },
          }),
          h("span", null, h("span", { class: "mono small" }, c.id), h("span", { class: "muted small" }, ` · ${c.title}`)),
        ),
      ),
    );
    const radio = (all, text) =>
      h(
        "label",
        { class: "check" },
        h("input", {
          type: "radio",
          name: "cases-mode",
          checked: form.all === all,
          onchange: () => {
            form.all = all;
            picks.classList.toggle("hidden", all);
            invalidate();
            renderPlan();
          },
        }),
        text,
      );

    const target = params.targets.find((t) => t.name === form.target);
    planBox = h("div", { "aria-live": "polite" });
    jobBox = h("div", { "aria-live": "polite" });
    const fields = h(
      "form",
      {
        class: "form",
        novalidate: true,
        onsubmit: (e) => {
          e.preventDefault();
          estimate();
        },
      },
      h(
        "div",
        { class: "field" },
        h("label", { for: "f-pack" }, "Pack"),
        h("select", { id: "f-pack", onchange: change("pack") }, packs.map((p) => h("option", { value: p.name, selected: p.name === form.pack }, `${p.name} · ${p.title}`))),
      ),
      h(
        "div",
        { class: "field" },
        h("label", { for: "f-variant" }, "Variant"),
        h("select", { id: "f-variant", onchange: change("variant") }, params.variants.map((v) => h("option", { value: v.name, selected: v.name === form.variant }, `${v.name} · ${plural(v.cases.length, "case")}`))),
      ),
      h(
        "div",
        { class: "field" },
        h("label", { for: "f-target" }, "Target"),
        h("select", { id: "f-target", onchange: change("target") }, params.targets.map((t) => h("option", { value: t.name, selected: t.name === form.target }, `${t.name} · ${t.kind.replace(/_/g, " ")}`))),
        target ? h("span", { class: "hint" }, target.models.map((m) => `${m.role} ${m.model}`).join(" · ")) : null,
      ),
      h("fieldset", { class: "field wide" }, h("legend", null, "Cases"), h("div", { class: "choices" }, radio(true, `All ${plural(variant.cases.length, "case")}`), radio(false, "Choose cases")), picks),
      numberField("f-repeats", "Repeats per case", "repeats", `default ${params.defaults.repeats}, the pack's`, { min: 1, max: 100, step: 1, placeholder: String(params.defaults.repeats) }),
      numberField("f-turns", "Max user turns per trial", "maxTurns", `default ${params.defaults.max_turns}, the pack's`, { min: 1, max: 200, step: 1, placeholder: String(params.defaults.max_turns) }),
      numberField("f-budget", "Budget cap (USD)", "budget", params.budget.usd === null ? params.budget.error : `default ${usd(params.budget.usd)} (${params.budget.source === "default" ? "default" : "EVAL_BUDGET_USD"})`, { min: 0.01, max: 1000, step: 0.01 }),
      h("div", { class: "field wide" }, h("div", { class: "actions" }, h("button", { type: "submit" }, "Estimate (free)"), h("span", { class: "hint" }, "Lint and cost estimate: no API call."))),
    );

    view.replaceChildren(
      h(
        "main",
        { class: "main single" },
        h("h1", null, "New run"),
        h("p", { class: "muted" }, "A live run calls the Anthropic API and costs money. The estimate is free; the launch names its cost and cap, and asks first."),
        fields,
        planBox,
        jobBox,
      ),
    );
    renderPlan();
    renderJob();
  }

  function selection() {
    const errors = [];
    const count = (raw, id, max) => {
      if (raw === "") return null;
      const n = Number(raw);
      if (!Number.isInteger(n) || n < 1 || n > max) {
        errors.push(id);
        return null;
      }
      return n;
    };
    const body = {
      pack: form.pack,
      variant: form.variant,
      target: form.target,
      cases: form.all ? null : [...form.cases],
      repeats: count(form.repeats, "f-repeats", 100),
      max_turns: count(form.maxTurns, "f-turns", 200),
      budget_usd: null,
    };
    if (form.budget !== "") {
      const b = Number(form.budget);
      if (!Number.isFinite(b) || b <= 0 || b > 1000) errors.push("f-budget");
      else body.budget_usd = b;
    }
    if (!form.all && !form.cases.size) errors.push("cases");
    return { body, errors };
  }

  async function estimate() {
    const { body, errors } = selection();
    for (const id of errors) {
      const input = document.getElementById(id);
      if (input) input.setAttribute("aria-invalid", "true");
    }
    if (errors.length) {
      plan = { error: errors.includes("cases") ? "Choose at least one case, or run them all." : "A highlighted field is out of range: repeats 1 to 100, turns 1 to 200, a cap above $0 and up to $1000." };
      renderPlan();
      return;
    }
    plan = { pending: true };
    renderPlan();
    try {
      plan = { body, result: await api("/api/estimate", { method: "POST", body }) };
    } catch (error) {
      plan = { error: error.message };
    }
    renderPlan();
  }

  function renderPlan() {
    if (!planBox) return;
    if (!plan) return planBox.replaceChildren();
    if (plan.pending) return planBox.replaceChildren(h("p", { class: "muted" }, "Estimating…"));
    if (plan.error) return planBox.replaceChildren(notice("No estimate", plan.error));
    const { lint, lint_errors: errors, estimate: e } = plan.result;
    const warnings = lint.length - errors;
    const issues = lint.length
      ? h(
          "div",
          { class: "table-box stacked trial" },
          h(
            "table",
            null,
            h("thead", null, h("tr", null, h("th", null, "Level"), h("th", null, "Code"), h("th", null, "Where"), h("th", null, "Message"))),
            h("tbody", null, lint.map((i) => h("tr", null, h("td", { "data-label": "Level" }, i.level), h("td", { class: "mono small", "data-label": "Code" }, i.code), h("td", { class: "mono small", "data-label": "Where" }, i.where), h("td", { class: "small", "data-label": "Message" }, i.message)))),
          ),
        )
      : null;
    const lintBox = h(
      "div",
      { class: "box" },
      h("h2", null, `Lint: ${plural(errors, "error")}, ${plural(warnings, "warning")}`),
      // warnings alone do not stop a run: folded; an error is shown open
      issues && !errors ? h("details", null, h("summary", null, `Show the ${plural(warnings, "warning")}`), issues) : issues,
      errors ? h("p", { class: "trial" }, "Lint errors: a live run would send nothing. Fix the pack, then estimate again.") : null,
    );
    if (!e) return planBox.replaceChildren(lintBox);
    const rows = e.roles.map((r) =>
      h(
        "tr",
        null,
        h("td", { "data-label": "Role" }, r.role),
        h("td", { class: "mono small", "data-label": "Model" }, r.model),
        h("td", { class: "small muted", "data-label": "From" }, r.source),
        h("td", { class: "num", "data-label": "Typical" }, usd(r.typical_usd)),
        h("td", { class: "num", "data-label": "Worst case" }, usd(r.worst_usd)),
      ),
    );
    const estimateBox = h(
      "div",
      { class: "box" },
      h("h2", null, "Estimate"),
      h("p", { class: "small muted trial" }, `${plural(e.case_ids.length, "case")} × ${plural(e.repeats, "repeat")} = ${plural(e.trials, "trial")}, target ${e.target}, up to ${e.worst_turns} user turns per trial (prices as of ${e.prices_as_of}; thinking tokens not counted).`),
      h(
        "div",
        { class: "table-box stacked trial" },
        h(
          "table",
          null,
          h("thead", null, h("tr", null, h("th", null, "Role"), h("th", null, "Model"), h("th", null, "From"), h("th", { class: "num" }, "Typical"), h("th", { class: "num" }, "Worst case"))),
          h(
            "tbody",
            null,
            rows,
            h("tr", { class: "total" }, h("td", { "data-label": "" }, "Total"), h("td"), h("td"), h("td", { class: "num", "data-label": "Typical" }, usd(e.typical_usd)), h("td", { class: "num", "data-label": "Worst case" }, usd(e.worst_usd))),
          ),
        ),
      ),
      h("p", { class: "trial" }, `Cap: ${usd(e.budget_usd)}. The run is refused if the typical estimate is above it, and stops before a call that would cross it.`),
      e.refused ? notice("A live run would be refused", `The typical estimate ${usd(e.typical_usd)} is above the ${usd(e.budget_usd)} cap: raise the cap or choose fewer trials.`) : null,
      e.refused || confirming ? null : h("p", { class: "trial" }, h("button", { type: "button", class: "primary", disabled: liveJob && liveJob.running, onclick: () => { confirming = true; renderPlan(); } }, "Launch live run…")),
    );
    planBox.replaceChildren(lintBox, estimateBox, confirming ? confirmBox(e) : null);
    if (confirming) {
      // the question, not the paid button: a second Enter must not launch
      const title = planBox.querySelector("#confirm-title");
      if (title) title.focus();
    }
  }

  function confirmBox(e) {
    const models = e.roles.map((r) => `${r.role} ${r.model}`).join(" · ");
    return h(
      "section",
      { class: "box confirm", "aria-labelledby": "confirm-title" },
      h("h2", { id: "confirm-title", tabindex: "-1" }, "Launch a live run?"),
      h("p", null, "This calls the Anthropic API and costs money. Completed trials are kept if the cap stops the run."),
      h(
        "dl",
        null,
        h("dt", null, "Trials"),
        h("dd", null, `${e.trials} (${plural(e.case_ids.length, "case")} × ${e.repeats})`),
        h("dt", null, "Models"),
        h("dd", { class: "mono small" }, models),
        h("dt", null, "Typical cost"),
        h("dd", { class: "num" }, usd(e.typical_usd)),
        h("dt", null, "Worst case"),
        h("dd", { class: "num" }, usd(e.worst_usd)),
        h("dt", null, "Cap"),
        h("dd", { class: "num" }, usd(e.budget_usd)),
      ),
      h(
        "div",
        { class: "actions" },
        h(
          "button",
          {
            type: "button",
            class: "primary",
            onclick: (event) => {
              event.currentTarget.disabled = true; // one click, one request: a double click sends one
              launch();
            },
          },
          `Launch: ${plural(e.trials, "trial")}, typical ${usd(e.typical_usd)}, cap ${usd(e.budget_usd)}`,
        ),
        h("button", { type: "button", onclick: () => { confirming = false; renderPlan(); } }, "Cancel"),
      ),
    );
  }

  async function launch() {
    const body = { ...plan.body, confirm: true };
    confirming = false;
    try {
      liveJob = await api("/api/runs", { method: "POST", body });
      plan = null;
      renderPlan();
      renderJob();
      followLive(liveJob.id);
    } catch (error) {
      renderPlan();
      planBox.append(notice("Not launched", error.message));
    }
  }

  function followLive(jobId) {
    watch(jobId, (job) => {
      liveJob = job;
      if (!job.running && job.run_id) runCache.delete(job.run_id);
      renderJob();
      if (!job.running) renderPlan();
    });
  }

  function renderJob() {
    if (!jobBox) return;
    const job = liveJob;
    if (!job) return jobBox.replaceChildren();
    const progress = job.planned ? `${job.done} of ${plural(job.planned, "trial")}` : `${plural(job.done, "trial")} done`;
    const state = job.running
      ? `Running${job.run_id ? ` · run ${job.run_id}` : ""} · ${progress} · ${usd(job.spent_usd)} spent`
      : `Ended · exit ${job.exit_code}${job.exit_meaning ? `: ${job.exit_meaning}` : ""}`;
    jobBox.replaceChildren(
      h(
        "section",
        { class: "box trial" },
        h("h2", null, `Live run, started ${when(job.started_at)}`),
        h("p", { class: "status-line trial", role: "status" }, state),
        !job.running && job.run_id ? h("p", null, h("a", { class: "button", href: `#/results/${enc(job.run_id)}` }, `Open the results of ${job.run_id}`)) : null,
        h("pre", { class: "log" }, job.log.slice(-400).join("\n") || " "),
      ),
    );
    const log = jobBox.querySelector(".log");
    if (log) log.scrollTop = log.scrollHeight;
  }

  // ── Parameters ─────────────────────────────────────────────────────────────────────────────

  async function showParameters(gen, name) {
    loading("Loading the pack…");
    let packs;
    try {
      packs = (await api("/api/packs")).filter((p) => !p.error);
    } catch (error) {
      if (!stale(gen)) failed(error);
      return;
    }
    if (stale(gen)) return;
    if (!packs.length) {
      view.replaceChildren(h("div", { class: "main single" }, h("h1", null, "Parameters"), notice("No pack", "The packs folder holds no pack that reads.")));
      return;
    }
    const chosen = packs.some((p) => p.name === name) ? name : form.pack && packs.some((p) => p.name === form.pack) ? form.pack : packs[0].name;
    let p;
    try {
      p = await pack(chosen);
    } catch (error) {
      if (!stale(gen)) failed(error);
      return;
    }
    if (stale(gen)) return;
    renderParameters(packs, p);
  }

  function table(headers, rows, cls) {
    return h(
      "div",
      { class: `table-box stacked trial ${cls || ""}` },
      h("table", null, h("thead", null, h("tr", null, headers.map((x) => h("th", null, x)))), h("tbody", null, rows)),
    );
  }

  function renderParameters(packs, p) {
    const target = p.targets.find((t) => t.name === p.defaults.target) || p.targets[0];
    const cell = (label, value) => h("div", null, h("dt", null, label), h("dd", null, value));
    view.replaceChildren(
      h(
        "main",
        { class: "main single" },
        h(
          "div",
          { class: "title-row" },
          h("div", null, h("h1", null, "Parameters"), h("p", { class: "subtitle" }, `${p.name} · ${p.title} ${p.version}`)),
          packs.length > 1
            ? h(
                "div",
                { class: "field" },
                h("label", { for: "p-pack" }, "Pack"),
                h("select", { id: "p-pack", onchange: (e) => (location.hash = `#/parameters/${enc(e.target.value)}`) }, packs.map((x) => h("option", { value: x.name, selected: x.name === p.name }, x.name))),
              )
            : null,
        ),
        h("p", null, p.intended_use),
        h("p", { class: "muted trial" }, `Out of scope: ${p.out_of_scope.join("; ")}.`),
        h("p", { class: "muted trial" }, p.disclaimer),
        h(
          "dl",
          { class: "facts trial" },
          cell("Default target", p.defaults.target),
          cell("Repeats per case", String(p.defaults.repeats)),
          cell("Max user turns", String(p.defaults.max_turns)),
          cell("Budget cap", p.budget.usd === null ? p.budget.error : `${usd(p.budget.usd)} (${p.budget.source})`),
        ),
        h("h2", { class: "trial" }, "Other settings"),
        recordTable({ depends_on: p.depends_on, ...p.settings }, "Other settings"),
        h("h2", { class: "trial" }, "Models"),
        h("p", { class: "small muted" }, `For target ${target ? target.name : "—"}. Set AGENT_MODEL, SIMULATOR_MODEL and EVAL_BUDGET_USD in .env; restart eval --ui to read a change.`),
        target
          ? table(
              ["Role", "Model", "From"],
              target.models.map((m) => h("tr", null, h("td", { "data-label": "Role" }, m.role), h("td", { class: "mono small", "data-label": "Model" }, m.model), h("td", { class: "small muted", "data-label": "From" }, m.source === "default" ? `default (${m.env_var} not set)` : m.env_var))),
            )
          : null,
        h("h2", { class: "trial" }, "Targets"),
        table(
          ["Target", "Kind", "Description", "Exposes", "Settings"],
          p.targets.map((t) =>
            h(
              "tr",
              null,
              h("td", { class: "mono small", "data-label": "Target" }, t.name),
              h("td", { class: "small", "data-label": "Kind" }, t.kind.replace(/_/g, " ")),
              h("td", { class: "small", "data-label": "Description" }, t.description),
              h("td", { class: "small muted", "data-label": "Exposes" }, t.exposes.join(", ")),
              h("td", { class: "small muted", "data-label": "Settings" }, `effort ${t.effort} · max tokens ${t.max_tokens}${t.tools.length ? ` · tools ${t.tools.join(", ")}` : ""}`),
            ),
          ),
        ),
        h("h2", { class: "trial" }, `Hazards (${p.hazards.length})`),
        h("p", { class: "small muted" }, `Severity scale: ${p.severity_scale.map((x) => `${x.rank} ${x.name}`).join(" · ")}.`),
        table(
          ["Hazard", "Severity", "Controls", "Acceptance criterion"],
          p.hazards.map((hz) =>
            h(
              "tr",
              null,
              h("td", { "data-label": "Hazard" }, h("span", { class: "id" }, hz.id), " ", hz.title, h("div", { class: "small muted" }, hz.failure_mode), hz.deferred_reason ? h("div", { class: "small muted" }, `Deferred: ${hz.deferred_reason}`) : null),
              h("td", { class: "small", "data-label": "Severity" }, hz.severity),
              h("td", { class: "small", "data-label": "Controls" }, hz.controls.map((c) => h("div", null, h("span", { class: "id" }, c.id), " ", c.requirement))),
              h("td", { class: "small", "data-label": "Criterion" }, hz.criterion),
            ),
          ),
        ),
        p.variants.map((v) => [
          h("h2", { class: "trial" }, `Cases · variant ${v.name} (${v.cases.length})`),
          v.excluded_hazards.length ? h("p", { class: "small muted" }, `Not applicable to this variant: ${v.excluded_hazards.join(", ")}.`) : null,
          table(
            ["Case", "Title", "Hazards"],
            v.cases.map((c) => h("tr", null, h("td", { class: "mono small", "data-label": "Case" }, c.id), h("td", { class: "small", "data-label": "Title" }, c.title), h("td", { class: "mono small", "data-label": "Hazards" }, c.hazards.join(", ")))),
          ),
        ]),
      ),
    );
  }

  window.addEventListener("hashchange", route);
  route();
})();
