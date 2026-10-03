// Utilities ------------------------------------------------------------------

const escapeHTML = value => String(value ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const formatClock = seconds => {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
const formatDuration = seconds => seconds >= 90
  ? `${(seconds / 60).toFixed(1)} min` : `${seconds.toFixed(1)} s`;
const formatUSD = value => `$${(value || 0).toFixed(value < 0.1 ? 3 : 2)}`;
const sum = values => values.reduce((a, b) => a + (b || 0), 0);
const plural = (n, word, many = word + "s") => `${n} ${n === 1 ? word : many}`;

// Derived views of the trace --------------------------------------------------

const steps = TRACE.steps;
const calls = steps.filter(s => s.kind === "model_call");
const checks = steps.filter(s => s.kind === "lean_check");
const decisions = steps.filter(s => s.kind === "decision");
const eventsOf = name => decisions.filter(d => d.event === name);
const sameAttempt = (a, b) =>
  a.lemma === b.lemma && a.worker === b.worker && a.round === b.round;

// Each model call that produced Lean code is paired with the check of that code.
const checkOfCall = new Map();
const callOfCheck = new Map();
for (const call of calls) {
  let check = null;
  if (call.lemma != null) {
    check = checks.find(k => k.lemma != null && sameAttempt(k, call));
  } else if (/^(formalize|sketch)/.test(call.label || "")) {
    check = checks.find(k => k.index > call.index && k.lemma == null
      && k.stage === call.stage && !callOfCheck.has(k.index));
  }
  if (check) {
    checkOfCall.set(call.index, check);
    callOfCheck.set(check.index, call);
  }
}
const rejections = eventsOf("solver_attempt_rejected");
const lemmaWins = new Map(eventsOf("lemma_proved").map(d => [d.lemma, d]));

// Outcome of one solver call, for colouring the timeline and step lists.
function solverOutcome(call) {
  const win = lemmaWins.get(call.lemma);
  const check = checkOfCall.get(call.index);
  const late = win && call.index > win.index;
  if (win && sameAttempt(win, call)) return { state: "win", late: false, check };
  if (late) return { state: "late", late: true, check };
  if (check) return { state: check.ok ? "ok" : "fail", late: false, check };
  if (rejections.some(r => sameAttempt(r, call))) return { state: "rejected", late: false };
  return { state: "pending", late: false };
}

const lemmaName = statement => (statement.match(/(?:theorem|lemma)\s+([^\s(:{\[]+)/) || [])[1];
const sketches = eventsOf("sketch_accepted");
const finalSketch = sketches[sketches.length - 1];
const lemmaOrder = [];
for (const sketch of sketches) {
  for (const statement of sketch.lemmas || []) {
    const name = lemmaName(statement);
    if (name && !lemmaOrder.includes(name)) lemmaOrder.push(name);
  }
}
for (const call of calls) {
  if (call.lemma != null && !lemmaOrder.includes(call.lemma)) lemmaOrder.push(call.lemma);
}

const roleOf = step => step.kind === "lean_check" ? "lean"
  : step.kind === "decision" ? "decision" : step.role;

// Rendering: Lean, math, prompts ----------------------------------------------

const LEAN_KEYWORDS = new Set(("theorem lemma def inductive structure namespace end open by fun "
  + "exact intro intros have show refine unfold match with if then else let in where calc "
  + "rfl simp rw apply constructor cases induction at import set_option noncomputable "
  + "instance class example abbrev private protected obtain rcases use omega decide "
  + "Type Prop").split(" "));
const LEAN_TOKEN = /(\/-[\s\S]*?-\/)|(--[^\n]*)|("(?:[^"\\\n]|\\.)*")|([A-Za-z_À-ɏ][\w'.À-ɏ]*)|([\s\S])/g;

// Lean source as numbered lines; `errorLines` is a set of 1-based line numbers.
function leanHTML(source, errorLines = new Set(), extraClass = "") {
  const lines = [[]];
  let match;
  LEAN_TOKEN.lastIndex = 0;
  while ((match = LEAN_TOKEN.exec(source))) {
    let cls = "";
    if (match[1] || match[2]) cls = "cm";
    else if (match[3]) cls = "st";
    else if (match[4]) cls = match[4] === "sorry" ? "sorry" : LEAN_KEYWORDS.has(match[4]) ? "kw" : "";
    const pieces = match[0].split("\n");
    pieces.forEach((piece, i) => {
      if (i > 0) lines.push([]);
      if (piece) lines[lines.length - 1].push(cls ? `<span class="${cls}">${escapeHTML(piece)}</span>` : escapeHTML(piece));
    });
  }
  while (lines.length > 1 && lines[lines.length - 1].length === 0) lines.pop();
  const body = lines.map((parts, i) => {
    const n = i + 1;
    return `<span class="line${errorLines.has(n) ? " err" : ""}" data-line="${n}"><span class="no">${n}</span>${parts.join("")}</span>`;
  }).join("");
  return `<pre class="lean ${extraClass}"><code>${body}</code></pre>`;
}

const errorLinesOf = errors => new Set((errors || []).flatMap(e =>
  [...String(e).matchAll(/\.lean:(\d+):\d+:/g)].map(m => Number(m[1]))));

const TEX_MACROS = {
  "\\PV": "\\mathsf{PV}", "\\eps": "\\varepsilon", "\\zo": "\\{0,1\\}",
  "\\TR": "\\mathrm{TR}", "\\ITR": "\\mathrm{ITR}", "\\M": "\\mathcal{M}",
  "\\FP": "\\mathsf{FP}", "\\calF": "\\mathcal{F}",
};
const MATH_SPAN = /\$\$([\s\S]+?)\$\$|\\begin\{equation\*?\}([\s\S]+?)\\end\{equation\*?\}|\\\[([\s\S]+?)\\\]|\$([^$\n]+?)\$/g;

function texHTML(tex, display) {
  if (!window.katex) return `<code>${escapeHTML(display ? tex : "$" + tex + "$")}</code>`;
  try {
    return katex.renderToString(tex.trim(), {
      displayMode: display, output: "mathml", throwOnError: false,
      macros: { ...TEX_MACROS },
    });
  } catch (error) {
    return `<code>${escapeHTML(tex)}</code>`;
  }
}

function inlineCode(text) {
  return escapeHTML(text).replace(/`([^`\n]+)`/g, "<code>$1</code>");
}

// Prose containing $...$ / $$...$$ / \begin{equation} math and `code`.
function mathHTML(text) {
  let out = "", last = 0, match;
  MATH_SPAN.lastIndex = 0;
  while ((match = MATH_SPAN.exec(text))) {
    out += inlineCode(text.slice(last, match.index));
    const display = match[4] === undefined;
    out += texHTML(match[1] ?? match[2] ?? match[3] ?? match[4], display);
    last = MATH_SPAN.lastIndex;
  }
  return out + inlineCode(text.slice(last));
}

// Minimal Markdown for the playbook-derived system prompts.
function markdownHTML(text) {
  const blocks = [];
  let list = null, para = [];
  const inline = s => inlineCode(s).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|\W)\*([^*\n]+)\*(?=\W|$)/g, "$1<em>$2</em>");
  const flushPara = () => { if (para.length) blocks.push(`<p>${inline(para.join(" "))}</p>`); para = []; };
  const flushList = () => { if (list) blocks.push(`<ul>${list.map(li => `<li>${inline(li)}</li>`).join("")}</ul>`); list = null; };
  for (const line of text.split("\n")) {
    const heading = line.match(/^(#{1,4})\s+(.*)/);
    const item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
    if (heading) { flushPara(); flushList(); blocks.push(`<h4>${inline(heading[2])}</h4>`); }
    else if (item) { flushPara(); (list ||= []).push(item[1]); }
    else if (!line.trim()) { flushPara(); flushList(); }
    else if (list) { list[list.length - 1] += " " + line.trim(); }
    else para.push(line.trim());
  }
  flushPara(); flushList();
  return `<div class="md">${blocks.join("")}</div>`;
}

const LEAN_TAGS = new Set(["definitions", "preamble", "statement", "lemmas", "main_proof",
  "proof", "helpers", "available_lemmas", "target_lemma", "previous_helpers",
  "previous_proof", "solution", "lean_code", "formal_statement", "previous_definitions",
  "previous_statement", "previous_lemmas", "previous_main_proof"]);
const PLAIN_TAGS = new Set(["lean_errors", "errors", "problems"]);

// A prompt or reply split into free text and <tag>...</tag> blocks.
function taggedHTML(text) {
  const parts = [];
  const pattern = /<([a-z_]+)>([\s\S]*?)<\/\1>/g;
  let last = 0, match;
  while ((match = pattern.exec(text))) {
    if (text.slice(last, match.index).trim()) parts.push({ prose: text.slice(last, match.index).trim() });
    parts.push({ tag: match[1], body: match[2].replace(/^\n+/, "").replace(/\s+$/, "") });
    last = pattern.lastIndex;
  }
  if (text.slice(last).trim()) parts.push({ prose: text.slice(last).trim() });
  return parts.map(part => {
    if (part.prose) return `<div class="prose">${mathHTML(part.prose)}</div>`;
    let body;
    if (!part.body) body = `<div class="muted">(empty)</div>`;
    else if (LEAN_TAGS.has(part.tag)) body = leanHTML(part.body);
    else if (PLAIN_TAGS.has(part.tag)) body = `<pre class="plain errors">${escapeHTML(part.body)}</pre>`;
    else body = `<div class="prose">${mathHTML(part.body)}</div>`;
    return `<div class="tagblock"><span class="tagname">&lt;${part.tag}&gt;</span>${body}</div>`;
  }).join("");
}

function templateHTML(text) {
  return `<pre class="plain">${escapeHTML(text).replace(/\{([a-z_]+)\}/g, '<span class="placeholder">{$1}</span>')}</pre>`;
}

// Step summaries ----------------------------------------------------------------

const EVENT_TEXT = {
  formalization_compiled: "Formalization compiles",
  formalization_rejected: "Formalization rejected",
  audit_verdict: "Audit verdict",
  sketch_accepted: "Sketch accepted",
  sketch_rejected: "Sketch rejected",
  lemma_proved: "Lemma proved",
  solver_attempt_rejected: "Attempt rejected before Lean",
  solver_budget_exhausted: "Solver chain exhausted its budget",
  replan: "Sketch revision requested",
  lemma_reused: "Proved lemma reused",
  final_verification: "Final verification",
};

function stepTitle(step) {
  if (step.kind === "model_call") {
    if (step.lemma != null) return `solver ${step.worker} · ${step.lemma} · round ${step.round}`;
    return `${step.role} · ${step.label}`;
  }
  if (step.kind === "lean_check") return `Lean check · ${step.file}`;
  return EVENT_TEXT[step.event] || step.event;
}

function stepResult(step) {
  if (step.kind === "model_call") {
    if (step.error) return `<span class="pill fail">error</span>`;
    if (step.lemma != null) {
      const { state } = solverOutcome(step);
      const label = { win: "proof kept", ok: "compiles", fail: "Lean errors",
        late: "after lemma proved", rejected: "rejected", pending: "no check" }[state];
      const tone = { win: "ok", ok: "ok", fail: "fail", late: "neutral", rejected: "warn", pending: "neutral" }[state];
      return `<span class="pill ${tone}">${label}</span>`;
    }
    return `<span class="muted mono">${formatDuration(step.seconds || 0)}</span>`;
  }
  if (step.kind === "lean_check") {
    if (!step.ok) return `<span class="pill fail">${plural((step.errors || []).length, "error")}</span>`;
    return step.sorry_warning
      ? `<span class="pill warn">compiles · sorry</span>`
      : `<span class="pill ok">compiles</span>`;
  }
  if (step.event === "audit_verdict") {
    return `<span class="pill ${step.verdict === "FAITHFUL" ? "ok" : "fail"}">${escapeHTML(step.verdict)}</span>`;
  }
  if (step.event === "final_verification") {
    return `<span class="pill ${step.checks?.verified ? "ok" : "fail"}">${step.checks?.verified ? "verified" : "not verified"}</span>`;
  }
  if (step.event === "lemma_proved") return `<span class="pill ok">${escapeHTML(step.lemma)}</span>`;
  return "";
}

function stepRow(step) {
  const role = roleOf(step);
  return `<button class="steprow" type="button" data-step="${step.index}">
    <span class="idx">#${step.index}</span>
    <span class="t">${formatClock(step.elapsed_seconds)}</span>
    <span class="dot ${role}" title="${role}"></span>
    <span class="what"><span class="lbl">${escapeHTML(stepTitle(step))}</span></span>
    <span>${stepResult(step)}</span>
  </button>`;
}
const stepList = list => list.length
  ? `<div class="steps">${list.map(stepRow).join("")}</div>`
  : `<p class="muted">No steps of this kind in this run.</p>`;
const stepLink = step => step ? `<button class="steplink" type="button" data-step="${step.index}">#${step.index}</button>` : "";

const cfg = TRACE.config || {};
const outcome = TRACE.outcome || {};
const problem = TRACE.problem || {};
const totalCost = sum(calls.map(c => c.cost_usd));
const callsByRole = role => calls.filter(c => c.role === role);

// The proposition itself, without the shared setting paragraph.
function propositionText() {
  const paragraphs = (problem.informal_statement || "").split(/\n\s*\n/);
  return (paragraphs.length > 1 ? paragraphs.slice(1) : paragraphs).join("\n\n");
}

function lemmaUses(text, self) {
  return lemmaOrder.filter(name => name !== self && new RegExp(`(^|[^\\w'.])${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![\\w'])`).test(text || ""));
}

// Step drawer --------------------------------------------------------------------------

let openStep = null;
let drawerOpen = false;
let drawerTab = "response";

function stepDetail(step) {
  const meta = [`<span>at <b>${formatClock(step.elapsed_seconds)}</b></span>`, `<span>stage <b>${escapeHTML(step.stage)}</b></span>`];
  let body = "";
  if (step.kind === "model_call") {
    meta.push(`<span>model <b>${escapeHTML(step.model)}</b></span>`,
      `<span>duration <b>${formatDuration(step.seconds || 0)}</b></span>`,
      `<span>output tokens <b>${(step.usage?.outputTokens ?? 0).toLocaleString()}</b></span>`,
      `<span>cost <b>${formatUSD(step.cost_usd)}</b></span>`);
    const check = checkOfCall.get(step.index);
    const note = step.role === "auditor"
      ? `<p class="note">The auditor's message contains only Lean code. Its system prompt is the auditor playbook.</p>` : "";
    const linked = check ? `<p class="note">The code in this reply was compiled in ${stepLink(check)}: ${stepResult(check)}</p>` : "";
    const tabs = [["response", "Reply"], ["prompt", "Prompt"], ["system", "System prompt"]];
    body = `${note}${linked}
      <div class="tabs" role="tablist">${tabs.map(([id, name]) => `<button type="button" role="tab" data-tab="${id}" aria-selected="${drawerTab === id}">${name}</button>`).join("")}</div>
      <div data-panel="response" ${drawerTab === "response" ? "" : "hidden"}>${step.error ? `<pre class="plain errors">${escapeHTML(step.error)}</pre>` : taggedHTML(step.response || "")}</div>
      <div data-panel="prompt" ${drawerTab === "prompt" ? "" : "hidden"}>${taggedHTML(step.prompt || "")}</div>
      <div data-panel="system" ${drawerTab === "system" ? "" : "hidden"}>${markdownHTML(TRACE.system_prompts?.[step.system_prompt] || step.system_prompt || "")}</div>`;
  } else if (step.kind === "lean_check") {
    meta.push(`<span>duration <b>${formatDuration(step.seconds || 0)}</b></span>`);
    const call = callOfCheck.get(step.index);
    const axioms = Object.entries(step.axioms || {});
    body = `
      ${call ? `<p class="note">Compiles the code from ${stepLink(call)} (${escapeHTML(stepTitle(call))}).</p>` : ""}
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">${stepResult(step)}
        ${step.sorry_warning ? `<span class="muted">The file still contains <code>sorry</code>${step.lemma != null ? " (earlier lemmas are included unproved)" : ""}.</span>` : ""}</div>
      ${(step.errors || []).length ? `<div class="tagblock"><span class="tagname">compiler errors</span><pre class="plain errors">${escapeHTML(step.errors.join("\n\n"))}</pre></div>` : ""}
      ${axioms.length ? `<p>Axioms: ${axioms.map(([name, list]) => `<code>${escapeHTML(name)}</code> → ${list.map(a => `<code>${escapeHTML(a)}</code>`).join(", ")}`).join("; ")}</p>` : ""}
      <div class="tagblock"><span class="tagname">${escapeHTML(step.file)}</span>${leanHTML(step.source || "", errorLinesOf(step.errors), "tall")}</div>`;
  } else {
    body = Object.entries(step).filter(([k]) => !["index", "kind", "stage", "elapsed_seconds", "event"].includes(k)).map(([key, value]) => {
      let inner;
      if (typeof value === "string" && (LEAN_TAGS.has(key) || key === "solution")) inner = value ? leanHTML(value, new Set(), "tall") : `<span class="muted">(empty)</span>`;
      else if (Array.isArray(value) && value.every(v => typeof v === "string") && key === "lemmas") inner = leanHTML(value.join("\n\n"));
      else if (typeof value === "string") inner = `<div class="prose">${mathHTML(value)}</div>`;
      else inner = `<pre class="plain">${escapeHTML(JSON.stringify(value, null, 1))}</pre>`;
      return `<div class="tagblock"><span class="tagname">${escapeHTML(key)}</span>${inner}</div>`;
    }).join("");
  }
  const role = roleOf(step);
  return {
    head: `<div class="row"><span class="dot ${role}"></span><span class="mono muted">#${step.index} of ${steps.length - 1}</span>
        <span class="spacer"></span>
        <button class="btn" type="button" data-drawer="prev" ${step.index === 0 ? "disabled" : ""} aria-label="Previous step">←</button>
        <button class="btn" type="button" data-drawer="next" ${step.index === steps.length - 1 ? "disabled" : ""} aria-label="Next step">→</button>
        <button class="btn" type="button" data-drawer="close">Close</button></div>
      <h3>${escapeHTML(stepTitle(step))}</h3>
      <div class="meta">${meta.join("")}</div>`,
    body,
  };
}

// Opens the drawer with arbitrary content; `index` is set for step records only.
function showDrawer(head, body, index = null) {
  openStep = index;
  drawerOpen = true;
  document.getElementById("drawer-head").innerHTML = head;
  const bodyEl = document.getElementById("drawer-body");
  bodyEl.innerHTML = body;
  bodyEl.scrollTop = 0;
  document.getElementById("drawer").hidden = false;
  document.getElementById("scrim").hidden = false;
  // Bring the first compiler error into view inside the Lean listing.
  const errorLine = bodyEl.querySelector(".line.err");
  if (errorLine) {
    const pre = errorLine.closest("pre");
    pre.scrollTop = Math.max(0, errorLine.offsetTop - pre.clientHeight / 3);
  }
  document.querySelector('#drawer [data-drawer="close"]').focus({ preventScroll: true });
}

// Drawer header for content that is not a single step.
function summaryHead(title, meta = "", dotClass = "") {
  return `<div class="row">${dotClass ? `<span class="dot ${dotClass}"></span>` : ""}
      <span class="spacer"></span><button class="btn" type="button" data-drawer="close">Close</button></div>
    <h3>${title}</h3>${meta ? `<div class="meta">${meta}</div>` : ""}`;
}

function showStep(index) {
  const step = steps[index];
  if (!step) return;
  const { head, body } = stepDetail(step);
  showDrawer(head, body, index);
}

function closeStep() {
  openStep = null;
  drawerOpen = false;
  document.getElementById("drawer").hidden = true;
  document.getElementById("scrim").hidden = true;
}

// Clicks on step links and drawer controls, and keys while the drawer is open.
// Page handlers should ignore keys when `drawerOpen` is true.
function installDrawerHandlers() {
  document.addEventListener("click", event => {
    if (event.target.id === "scrim") return closeStep();
    const target = event.target.closest("[data-step],[data-drawer],[data-tab]");
    if (!target) return;
    const d = target.dataset;
    if (d.step !== undefined) showStep(Number(d.step));
    else if (d.drawer === "close") closeStep();
    else if (d.drawer === "prev") showStep(openStep - 1);
    else if (d.drawer === "next") showStep(openStep + 1);
    else if (d.tab) {
      drawerTab = d.tab;
      document.querySelectorAll("#drawer [data-tab]").forEach(b => b.setAttribute("aria-selected", b.dataset.tab === d.tab));
      document.querySelectorAll("#drawer [data-panel]").forEach(p => { p.hidden = p.dataset.panel !== d.tab; });
    }
  });
  document.addEventListener("keydown", event => {
    if (!drawerOpen || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === "Escape") closeStep();
    else if (openStep !== null && event.key === "ArrowRight") showStep(Math.min(steps.length - 1, openStep + 1));
    else if (openStep !== null && event.key === "ArrowLeft") showStep(Math.max(0, openStep - 1));
  });
}

function renderHeader() {
  document.getElementById("thm").textContent = TRACE.theorem_name;
  document.getElementById("run").textContent = TRACE.run_id;
  document.getElementById("models").textContent = `${TRACE.models?.orchestrator} + ${TRACE.models?.worker}`;
  const verified = outcome.checks?.verified;
  document.getElementById("status").innerHTML = `<span class="pill ${verified ? "ok" : outcome.status ? "fail" : "neutral"}">${verified ? "proved · verified" : escapeHTML(outcome.status || "in progress")}</span>`;
}
