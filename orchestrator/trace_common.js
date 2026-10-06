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

// An AIProver job cut short by a lost model server or an interrupted run ends
// with neither a proved lemma nor a recorded failure, or with every session
// cancelled; the lemma is attempted again after the run resumes. Such a job and its Lean checks are left out, so the later job takes
// its place, and the remaining steps are numbered consecutively.
function interruptedSteps(all) {
  const hidden = new Set();
  const jobs = all.filter(s => s.kind === "model_call" && s.backend === "aiprover");
  for (const job of jobs) {
    const next = jobs.find(j => j.index > job.index && j.lemma === job.lemma);
    const window = all.filter(s => s.index > job.index && (!next || s.index < next.index)
      && s.lemma === job.lemma);
    // A job whose sessions were all cancelled or never reached the model was
    // stopped by the interruption too, even if its failure was recorded.
    const unrun = (job.samples || []).length > 0
      && job.samples.every(x => x.status === "cancelled" || x.status === "infra");
    const ended = window.some(s => s.event === "lemma_proved"
      || (s.event === "solver_budget_exhausted" && !unrun));
    if (ended) continue;
    // Only after an interruption: a job still running in a live trace is kept.
    if (!all.some(s => s.index > job.index && s.event === "resumed")) continue;
    hidden.add(job.index);
    window.filter(s => s.kind === "lean_check").forEach(s => hidden.add(s.index));
  }
  return hidden;
}
const hiddenSteps = interruptedSteps(TRACE.steps);
const steps = TRACE.steps.filter(s => !hiddenSteps.has(s.index)).map((s, i) => ({ ...s, index: i }));
const calls = steps.filter(s => s.kind === "model_call");
const checks = steps.filter(s => s.kind === "lean_check");
const decisions = steps.filter(s => s.kind === "decision");
const eventsOf = name => decisions.filter(d => d.event === name);

// An AIProver call is one job of several samples, not one chat reply. Its Lean
// checks and its lemma_proved decision follow it in the trace and carry the
// sample index as `worker`, so they are attributed to the job by position: the
// job of a step is the last job on the same lemma recorded before it.
const isJob = step => step?.kind === "model_call" && step.backend === "aiprover";
const jobsByLemma = new Map();
for (const call of calls.filter(isJob)) {
  if (!jobsByLemma.has(call.lemma)) jobsByLemma.set(call.lemma, []);
  jobsByLemma.get(call.lemma).push(call);
}
const jobOfStep = new Map();
for (const step of steps) {
  if (step.lemma == null || step.kind === "model_call") continue;
  const job = (jobsByLemma.get(step.lemma) || []).filter(j => j.index < step.index).at(-1);
  if (job) jobOfStep.set(step.index, job);
}
const jobOf = step => (isJob(step) ? step : jobOfStep.get(step.index)) || null;
const jobNumber = job => (jobsByLemma.get(job.lemma) || []).indexOf(job) + 1;

// A chat solver's step belongs to the latest call on the same lemma, worker and
// round recorded at or before it (a later sketch repeats these keys).
const chatCallOf = step => step.kind === "model_call" ? step
  : calls.filter(c => c.lemma === step.lemma && c.worker === step.worker && c.round === step.round
    && c.index <= step.index).at(-1) || null;
const sameAttempt = (a, b) => {
  const jobA = jobOf(a), jobB = jobOf(b);
  if (jobA || jobB) return jobA === jobB;
  if (a.lemma !== b.lemma || a.worker !== b.worker || a.round !== b.round) return false;
  return chatCallOf(a) === chatCallOf(b);
};

// Independent sessions (samples) an AIProver job ran in parallel on its lemma.
const jobSamples = job => (job.samples || []).length || TRACE.config?.workers || 1;
// Largest number of AIProver jobs, and of their sessions, in flight at once.
// Sessions beyond the harness's max_parallel wait inside AIProver.
function peakInFlight() {
  const events = [];
  for (const jobs of jobsByLemma.values()) for (const job of jobs) {
    const start = job.elapsed_seconds - (job.seconds || 0);
    events.push([start, 1, jobSamples(job)], [job.elapsed_seconds, -1, -jobSamples(job)]);
  }
  events.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  let jobs = 0, sessions = 0, peakJobs = 0, peakSessions = 0;
  for (const [, dj, ds] of events) {
    jobs += dj; sessions += ds;
    peakJobs = Math.max(peakJobs, jobs); peakSessions = Math.max(peakSessions, sessions);
  }
  return { jobs: peakJobs, sessions: peakSessions };
}

// How each AIProver sample ended, from the harness's status of its Lean file.
// The status is that of the sample's final Lean file; the ending, when known,
// is why its session stopped (timeout, turn limit, server lost, ...).
const SAMPLE_TEXT = { verified: "verified", sorry: "sorry", error: "Lean error",
  empty: "no answer", infra: "server lost", unknown: "no status" };
function sampleText(sample) {
  const status = SAMPLE_TEXT[sample.status] || sample.status;
  const ending = sample.ending && sample.ending !== "finished" ? sample.ending : "";
  return ending && ending !== status ? `${status} · ${ending}` : status;
}
function jobSummary(job) {
  const samples = job.samples || [];
  if (!samples.length) return job.error ? "job failed" : "no answer";
  return samples.map(sampleText).join(", ");
}
// Sample `k` of an AIProver job: how it ended, its Lean check if its proof was
// extracted and checked, and when it finished (job start + its elapsed time).
function sampleOutcome(job, k) {
  const samples = job.samples || [];
  const sample = samples.find(s => s.sample === k) || samples[k] || null;
  const check = checks.find(c => jobOf(c) === job && c.worker === k) || null;
  const win = lemmaWins.get(job.lemma);
  const kept = !!win && jobOf(win) === job && win.worker === k;
  const late = !!win && !kept && job.index > win.index;
  const state = kept ? "win" : late ? "late" : check ? (check.ok ? "ok" : "fail") : "pending";
  const text = sample ? sampleText(sample) : job.error ? "job failed" : "no answer";
  const start = job.elapsed_seconds - (job.seconds || 0);
  const end = Math.min(job.elapsed_seconds, start + (sample?.elapsed_sec ?? job.seconds ?? 0));
  return { sample, check, state, text, start, end, turns: sample?.turns ?? null };
}
const sampleLanes = () => [...Array(Math.max(1, ...calls.filter(isJob).map(jobSamples))).keys()];
// Sessions of one job; jobs may run different numbers of sessions.
const jobLanes = job => [...Array(jobSamples(job)).keys()];
// Captain calls on a lemma the solvers kept failing on, and their decisions.
const handbackLemma = call => (/^handback\/(.+)$/.exec(call.label || "") || [])[1] || null;
const isHandback = step => step?.kind === "model_call" && handbackLemma(step) != null;
const captainCheck = step => step.kind === "lean_check" && (step.worker === "captain" || /_restated$/.test(step.label || ""));

// Who proved a lemma, for the lemma_proved decision `win`.
function winnerText(win) {
  if (win.worker === "captain") return "the captain, after a hand-back";
  const job = jobOf(win);
  return job ? `AIProver job ${jobNumber(job)}, sample ${win.worker}`
    : `solver ${win.worker}, round ${win.round}`;
}
// Why a lemma has a further attempt after `previous`: Lean errors for a chat
// solver; for AIProver a new job, after a replan (a new sketch) or else a
// retry of the same statement (a resumption does not change the attempt).
function transitionText(previous, next) {
  if (!isJob(next)) return solverOutcome(previous).state === "fail" ? "errors" : "";
  const between = decisions.filter(d => d.index > previous.index && d.index < next.index);
  if (between.some(d => d.event === "replan")) return "replan";
  return "retry";
}

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
// Display environments come first, so that inline math inside them (e.g. in
// \intertext) is rendered as part of the environment.
const MATH_SPAN = new RegExp([
  String.raw`\\begin\{(equation|align|gather|alignat|multline|eqnarray|prooftree)(\*?)\}([\s\S]+?)\\end\{\1\2\}`,
  String.raw`\$\$([\s\S]+?)\$\$`, String.raw`\\\[([\s\S]+?)\\\]`, String.raw`\$((?:[^$\n]|\n(?![ \t]*\n))+?)\$`,
].join("|"));
// KaTeX lacks these environments; each is rendered as the closest one it has.
const KATEX_ENVIRONMENT = { multline: "gather", eqnarray: "align" };

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

// Index just past the group that opens at `text[open]` ("{"), or -1.
function closingBrace(text, open) {
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    if (text[i] === "\\") { i++; continue; }
    if (text[i] === "{") depth++;
    else if (text[i] === "}" && --depth === 0) return i + 1;
  }
  return -1;
}

// A display environment; \intertext{...}, which KaTeX lacks, splits it into
// separate environments with the text between them.
function environmentHTML(name, star, body) {
  if (name === "prooftree") return `<pre class="tex"><code>${escapeHTML(body.trim())}</code></pre>`;
  const environment = (KATEX_ENVIRONMENT[name] || name) + star;
  const render = rows => rows.trim()
    ? texHTML(`\\begin{${environment}}${rows.replace(/\\label\{[^}]*\}/g, "")}\\end{${environment}}`, true)
    : "";
  let out = "", start = 0, at;
  while ((at = body.indexOf("\\intertext{", start)) !== -1) {
    const end = closingBrace(body, at + "\\intertext".length);
    if (end === -1) break;
    out += render(body.slice(start, at).replace(/\\\\\s*$/, ""))
      + `<span class="intertext">${mathHTML(body.slice(at + "\\intertext{".length, end - 1))}</span>`;
    start = end;
  }
  return out + render(body.slice(start));
}

function inlineCode(text) {
  return escapeHTML(text).replace(/`([^`\n]+)`/g, "<code>$1</code>");
}

// Text-mode LaTeX of an informal statement (already HTML-escaped), as HTML:
// emphasis, sectioning, lists, theorem environments and spacing commands.
const THEOREM_ENVIRONMENTS = "theorem|lemma|proposition|corollary|definition|remark|example|claim|conjecture";
function textModeHTML(html) {
  const replaceGroups = (text, command, wrap) => {
    let out = "", start = 0, at;
    while ((at = text.indexOf(command + "{", start)) !== -1) {
      const end = closingBrace(text, at + command.length);
      if (end === -1) break;
      out += text.slice(start, at) + wrap(textModeHTML(text.slice(at + command.length + 1, end - 1)));
      start = end;
    }
    return out + text.slice(start);
  };
  for (const [command, wrap] of [
    ["\\textbf", s => `<strong>${s}</strong>`], ["\\emph", s => `<em>${s}</em>`],
    ["\\textit", s => `<em>${s}</em>`], ["\\texttt", s => `<code>${s}</code>`],
    ["\\paragraph", s => `<strong>${s}</strong>`], ["\\subparagraph", s => `<strong>${s}</strong>`],
    ["\\marginnote", s => ` (${s})`],
  ]) html = replaceGroups(html, command, wrap);
  return html
    .replace(/\\label\{[^{}]*\}\s*/g, "")
    .replace(/\\(eq)?ref\{([^{}]*)\}/g, (_, eq, label) => eq ? `(${label})` : label)
    .replace(/\\textcolor\{[^{}]*\}\{([^{}]*)\}/g, "$1")
    .replace(/\{\\(it|em|sl)\s+([^{}]*)\}/g, "<em>$2</em>")
    .replace(/\{\\bf\s+([^{}]*)\}/g, "<strong>$1</strong>")
    .replace(/\\(begingroup|endgroup|allowdisplaybreaks|noindent|medskip|smallskip|bigskip|newline)\b\s*/g, "")
    .replace(/\s*\\begin\{(itemize|compactitem|enumerate|compactenum)\}\s*/g, "<ul>")
    .replace(/\s*\\end\{(itemize|compactitem|enumerate|compactenum)\}\s*/g, "</ul>")
    .replace(/\s*\\item\s*/g, "<li>")
    .replace(new RegExp(String.raw`\\begin\{(${THEOREM_ENVIRONMENTS})\}(?:\[([^\]]*)\])?\s*`, "g"),
             (_, name, note) => `<strong>${name[0].toUpperCase() + name.slice(1)}${note ? ` (${note})` : ""}.</strong> `)
    .replace(new RegExp(String.raw`\s*\\end\{(${THEOREM_ENVIRONMENTS})\}`, "g"), "")
    .replace(/\\begin\{proof\}\s*/g, "<em>Proof.</em> ")
    .replace(/\s*\\end\{proof\}/g, " ∎")
    .replace(/\\\\(\[[^\]]*\])?[ \t]*(\n?)/g, (_, skip, newline) => newline || "\n")
    .replace(/(\S)~(?=\S)/g, "$1&nbsp;");
}

// Prose containing LaTeX math (inline, display and align-like environments),
// text-mode LaTeX and `code`. Math and code are set aside while the text-mode
// commands, which may span them, are converted.
function mathHTML(text) {
  const pieces = [];
  const keep = html => `\u0000${pieces.push(html) - 1}\u0000`;
  // A fresh pattern per call: \intertext renders its text with a nested call.
  const span = new RegExp(MATH_SPAN.source, "g");
  let prose = "", last = 0, match;
  while ((match = span.exec(text))) {
    prose += text.slice(last, match.index);
    prose += keep(match[1] ? environmentHTML(match[1], match[2], match[3])
                           : texHTML(match[4] ?? match[5] ?? match[6], match[6] === undefined));
    last = span.lastIndex;
  }
  prose += text.slice(last);
  const html = escapeHTML(prose.replace(/`([^`\n]+)`/g, (_, code) => keep(`<code>${escapeHTML(code)}</code>`)));
  return textModeHTML(html).replace(/\u0000(\d+)\u0000/g, (_, index) => pieces[index]);
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
  lemma_handback: "Lemma handed back to the captain",
  final_verification: "Final verification",
  final_review: "Independent review",
  report: "LaTeX report",
};

function stepTitle(step) {
  if (step.kind === "model_call") {
    if (isJob(step)) return `AIProver job ${jobNumber(step)} · ${step.lemma}`;
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
      const label = state === "pending" && isJob(step) ? jobSummary(step)
        : { win: "proof kept", ok: "compiles", fail: "Lean errors",
            late: "after lemma proved", rejected: "rejected", pending: "no proof returned" }[state];
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
  if (step.event === "final_review") {
    return `<span class="pill ${step.verdict === "FAITHFUL" ? "ok" : "fail"}">${escapeHTML(step.verdict)}</span>`;
  }
  if (step.event === "report") return `<span class="pill ${step.pdf ? "ok" : "fail"}">${step.pdf ? "PDF" : "no PDF"}</span>`;
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

// Tabs of the step drawer: `panels` is a list of [id, name, html]. The tab
// last chosen stays selected when the next drawer has it.
function tabsHTML(panels) {
  const active = panels.some(([id]) => id === drawerTab) ? drawerTab : panels[0][0];
  return `<div class="tabs" role="tablist">${panels.map(([id, name]) =>
      `<button type="button" role="tab" data-tab="${id}" aria-selected="${active === id}">${name}</button>`).join("")}</div>
    ${panels.map(([id, , html]) => `<div data-panel="${id}" ${active === id ? "" : "hidden"}>${html}</div>`).join("")}`;
}

// Lean output, one message per `file:line:col: severity:` header.
function diagnosticsHTML(text) {
  if (!text) return `<p class="muted">No diagnostics: Lean reported nothing for this file.</p>`;
  const html = escapeHTML(text).replace(/^(\S+\.lean:\d+:\d+: )(error|warning)(:.*)$/gm,
    (_, where, severity, rest) => `${where}<span class="${severity}">${severity}${rest}</span>`);
  return `<pre class="diag">${html}</pre>`;
}
const diagnosticErrorLines = text => new Set([...String(text || "").matchAll(/\.lean:(\d+):\d+: error/g)]
  .map(m => Number(m[1])));
const flagPill = (value, yes, no) => value == null ? ""
  : `<span class="pill ${value ? "ok" : "fail"}">${value ? yes : no}</span>`;

// The harness's Lean check of a sample's whole final file.
function harnessCheckHTML(check) {
  if (!check || !Object.keys(check).length) {
    return `<p class="muted">The harness's check of this sample is not recorded.</p>`;
  }
  return `
    <div class="checkline"><span class="k">AIProver harness · Lean check of the final file</span>
      ${check.verdict ? `<span class="pill ${check.verdict === "PASS" ? "ok" : "fail"}">${escapeHTML(check.verdict)}</span>` : ""}
      ${flagPill(check.compiles, "compiles", "does not compile")}
      ${flagPill(check.complete, "no sorry", "contains sorry")}
      ${check.axioms_used ? `<span class="pill neutral">axioms: ${escapeHTML(check.axioms_used.join(", ") || "none")}</span>` : ""}</div>
    ${(check.problems || []).length ? `<ul class="problems">${check.problems.map(p => `<li>${escapeHTML(p)}</li>`).join("")}</ul>` : ""}
    ${diagnosticsHTML(check.diagnostics)}`;
}

// The pipeline's Lean check of the lemma proof extracted from a sample.
function extractedCheckHTML(outcome) {
  const { check, state } = outcome;
  if (!check) {
    return `<div class="checkline"><span class="k">Pipeline · Lean check of the extracted lemma proof</span>
      <span class="pill neutral">not run</span></div>
      <p class="muted">${state === "late" ? "The lemma was already proved when this sample finished."
        : "The final file holds no sorry-free proof of the lemma, so nothing was extracted to check."}</p>`;
  }
  const errors = check.errors || [];
  return `
    <div class="checkline"><span class="k">Pipeline · Lean check of the extracted lemma proof</span>
      <span class="pill ${check.ok ? "ok" : "fail"}">${check.ok ? (check.sorry_warning ? "compiles · sorry" : "compiles") : plural(errors.length, "error")}</span>
      ${state === "win" ? `<span class="pill ok">proof kept</span>` : ""} ${stepLink(check)}</div>
    ${errors.length ? diagnosticsHTML(errors.join("\n")) : ""}`;
}

// A Lean check of a chat model's code (captain or chat solver).
function chatCheckHTML(check) {
  if (!check) return `<p class="muted">Lean did not check this reply.</p>`;
  const errors = check.errors || [];
  return `<div class="checkline"><span class="k">Lean check ${stepLink(check)}</span>${stepResult(check)}</div>
    ${errors.length ? diagnosticsHTML(errors.join("\n")) : `<p class="muted">No errors.</p>`}
    <div class="tagblock"><span class="tagname">${escapeHTML(check.file || "")}</span>${leanHTML(check.source || "", errorLinesOf(errors), "tall")}</div>`;
}

// The model's reasoning before assistant turn `turn`, from the logging proxy.
const reasoningOfTurn = (sample, turn) => (sample.reasoning || []).filter(r => r.turn === turn);

function reasoningBlock(text, open = false) {
  return `<details class="fold reasoning" ${open ? "open" : ""}><summary><b>Reasoning</b>
    <span class="muted">${plural(text.split(/\s+/).filter(Boolean).length, "word")}</span></summary>
    <div class="body"><pre class="plain wrap">${escapeHTML(text)}</pre></div></details>`;
}

// One tool call: Lean written to a file is shown as Lean, other arguments as JSON.
function toolCallHTML(call) {
  let args = call.arguments || "";
  let lean = "";
  try {
    const parsed = JSON.parse(args);
    if (typeof parsed.content === "string" && /\.lean$/.test(parsed.file_path || parsed.path || "")) {
      lean = parsed.content;
      delete parsed.content;
    }
    args = JSON.stringify(parsed, null, 1);
  } catch { /* arguments cut at the trace limit stay as text */ }
  return `<div class="toolcall"><span class="pill neutral mono">${escapeHTML(call.name || "tool")}</span>
    <pre class="plain wrap">${escapeHTML(args)}</pre>${lean ? leanHTML(lean, new Set(), "tall") : ""}</div>`;
}

// The agent's session: its messages, tool calls, tool results and, where the
// proxy recorded it, its reasoning before each turn.
function sessionHTML(sample) {
  const session = sample.session || [];
  if (!session.length) return `<p class="muted">The session transcript is not recorded for this sample.</p>`;
  let turn = 0;
  return `<div class="session">${session.map(entry => {
    if (entry.role === "tool") {
      return `<details class="fold toolresult"><summary><span class="mono muted">result of ${escapeHTML(entry.name || "tool")}</span>
        <span class="muted">${plural(entry.text.split("\n").length, "line")}</span></summary>
        <div class="body">${/\.lean:\d+:\d+:/.test(entry.text) ? diagnosticsHTML(entry.text) : `<pre class="plain wrap">${escapeHTML(entry.text)}</pre>`}</div></details>`;
    }
    if (entry.role === "assistant") {
      turn += 1;
      const thoughts = reasoningOfTurn(sample, turn).map(r => reasoningBlock(r.reasoning || "")).join("");
      return `<div class="turn"><div class="turnhead"><span class="mono">turn ${turn}</span></div>${thoughts}
        ${entry.text ? `<div class="prose">${mathHTML(entry.text)}</div>` : ""}
        ${(entry.tool_calls || []).map(toolCallHTML).join("")}</div>`;
    }
    return `<details class="fold"><summary><b>${entry.injected ? "Harness message" : turn ? "User message" : "Task"}</b>
      <span class="muted">${plural(entry.text.split("\n").length, "line")}</span></summary>
      <div class="body"><pre class="plain wrap">${escapeHTML(entry.text)}</pre></div></details>`;
  }).join("")}</div>`;
}

function reasoningHTML(sample) {
  const records = sample.reasoning || [];
  if (!records.length) {
    return `<p class="muted">The model's reasoning is not recorded for this sample. The AIProver harness
      discards it; runs started after the reasoning proxy was installed record it per turn.</p>`;
  }
  return records.map(r => `<div class="turn"><div class="turnhead"><span class="mono">turn ${r.turn}</span>
    <span class="muted">${escapeHTML(r.time || "")}</span>${r.truncated ? ` <span class="pill fail">stopped at the reply token cap</span>` : ""}</div>${reasoningBlock(r.reasoning || "", true)}</div>`).join("");
}

// Drawer panels of an AIProver job, for one sample (a solver lane) or all.
function jobPanels(job, sampleIndex) {
  const lanes = sampleIndex == null ? jobLanes(job) : [sampleIndex];
  const per = render => lanes.map(k => {
    const outcome = sampleOutcome(job, k);
    const head = lanes.length > 1 ? `<h4 class="lanehead">solver ${k}</h4>` : "";
    return head + render(outcome.sample || {}, outcome, k);
  }).join("");
  return [
    ["response", "Reply", per(sample => sample.lean
      ? leanHTML(sample.lean, diagnosticErrorLines(sample.check?.diagnostics), "tall")
      : `<p class="muted">The final Lean file is not recorded.</p>`)],
    ["prompt", "Prompt", taggedHTML(job.prompt || "")],
    ["system", "System prompt", job.system_prompt
      ? markdownHTML(TRACE.system_prompts?.[job.system_prompt] || job.system_prompt)
      : `<p class="muted">AIProver composes its own system prompt inside the harness; the task above is
         what the pipeline sends.</p>`],
    ["lean", "Lean returns", per((sample, outcome) => harnessCheckHTML(sample.check) + extractedCheckHTML(outcome))],
    ["session", "Session", per(sample => sessionHTML(sample))],
    ["reasoning", "Reasoning", per(sample => reasoningHTML(sample))],
  ];
}

function stepDetail(step, sampleIndex = null) {
  const meta = [`<span>at <b>${formatClock(step.elapsed_seconds)}</b></span>`, `<span>stage <b>${escapeHTML(step.stage)}</b></span>`];
  let body = "";
  if (isJob(step)) {
    const job = step;
    meta.push(`<span>AIProver job <b>${jobNumber(job)}</b> on <b>${escapeHTML(job.lemma)}</b></span>`);
    if (sampleIndex != null) {
      const o = sampleOutcome(job, sampleIndex);
      meta.push(`<span>solver <b>${sampleIndex}</b></span>`,
        `<span>duration <b>${formatDuration(o.end - o.start)}</b></span>`,
        o.turns != null ? `<span>turns <b>${o.turns}</b></span>` : "",
        o.sample?.ending ? `<span>ended <b>${escapeHTML(o.sample.ending)}</b></span>` : "",
        `<span>result <b>${escapeHTML({ win: "proof kept", ok: "compiles", fail: "Lean errors" }[o.state] || o.text)}</b></span>`);
    } else {
      meta.push(`<span>duration <b>${formatDuration(job.seconds || 0)}</b></span>`, `<span>samples <b>${jobSummary(job)}</b></span>`);
    }
    const previous = (jobsByLemma.get(job.lemma) || [])[jobNumber(job) - 2];
    const note = `<p class="note">${previous ? `Started after a ${escapeHTML(transitionText(previous, job))} (previous job ${stepLink(previous)}).`
      : "First AIProver job on this lemma."} Each job runs ${plural(jobSamples(job), "independent session")}.</p>`;
    body = note + tabsHTML(jobPanels(job, sampleIndex));
  } else if (step.kind === "model_call") {
    meta.push(`<span>model <b>${escapeHTML(step.model)}</b></span>`,
      `<span>duration <b>${formatDuration(step.seconds || 0)}</b></span>`,
      `<span>output tokens <b>${(step.usage?.outputTokens ?? 0).toLocaleString()}</b></span>`,
      `<span>cost <b>${formatUSD(step.cost_usd)}</b></span>`);
    const check = checkOfCall.get(step.index);
    const note = step.role === "auditor"
      ? `<p class="note">The auditor's message contains only Lean code. Its system prompt is the auditor playbook.</p>` : "";
    const linked = check ? `<p class="note">The code in this reply was compiled in ${stepLink(check)}: ${stepResult(check)}</p>` : "";
    const panels = [
      ["response", "Reply", step.error ? `<pre class="plain errors">${escapeHTML(step.error)}</pre>` : taggedHTML(step.response || "")],
      ["prompt", "Prompt", taggedHTML(step.prompt || "")],
      ["system", "System prompt", markdownHTML(TRACE.system_prompts?.[step.system_prompt] || step.system_prompt || "")],
    ];
    if (check) panels.push(["lean", "Lean returns", chatCheckHTML(check)]);
    body = `${note}${linked}${tabsHTML(panels)}`;
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

function showStep(index, sampleIndex = null) {
  const step = steps[index];
  if (!step) return;
  const { head, body } = stepDetail(step, sampleIndex);
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
  const reviewed = outcome.status === "proved";
  document.getElementById("status").innerHTML = `<span class="pill ${reviewed ? "ok" : verified ? "warn" : outcome.status ? "fail" : "neutral"}">${reviewed ? "proved · verified · reviewed" : verified ? `verified · ${escapeHTML(outcome.status || "review pending")}` : escapeHTML(outcome.status || "in progress")}</span>`;
}
