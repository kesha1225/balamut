(() => {
  "use strict";

  const DATA = JSON.parse(document.getElementById("balamut-data").textContent);
  const FILES = DATA.files;
  const TESTS = DATA.tests;
  const STATUSES = ["survived", "no_coverage", "timeout", "killed"];
  const SEVERITY = { survived: 4, timeout: 3, no_coverage: 2, killed: 1 };
  const LABEL = { killed: "Killed", survived: "Survived", timeout: "Timeout", no_coverage: "No coverage" };
  const REASON = {
    hang: "a covering test hung on the mutant and passed on the original in the same time budget",
    hang_unconfirmed: "a test hung, but the mutated code never ran or the hung test is unknown",
    original_hang: "the same test hangs on the original code too",
    original_failed: "the killing test fails on the original code too, so an unstable test proves nothing",
  };
  const COLD = {
    type_only: "a type annotation, never evaluated at run time",
    schema_default: "a class-level default (a schema field), set once at import time",
    import_runtime: "module or class level code that runs at import time, before any test body",
    unattributed: "module or class level code that no test body executes",
  };
  const MAX_DIFF_LINES = 8;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const summaryView = document.getElementById("summary");
  const fileView = document.getElementById("file-view");
  const tooltip = document.getElementById("tooltip");

  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (value) => String(value).replace(/[&<>"']/g, (c) => ESCAPES[c]);
  const fmt = (n) => n.toLocaleString("en-US");
  const score = (c) => (c.killed + c.survived ? ((c.killed / (c.killed + c.survived)) * 100).toFixed(1) + "%" : "—");

  function countsOf(mutants) {
    const counts = { killed: 0, survived: 0, timeout: 0, no_coverage: 0 };
    for (const m of mutants) counts[m.status] += 1;
    return counts;
  }

  // ---- tooltip: one element for every [data-tip] mark and for the mosaic ----

  function placeTip(x, y) {
    const pad = 14;
    const box = tooltip.getBoundingClientRect();
    let left = x + pad;
    let top = y + pad;
    if (left + box.width > window.innerWidth - 8) left = x - box.width - pad;
    if (top + box.height > window.innerHeight - 8) top = y - box.height - pad;
    tooltip.style.left = Math.max(8, left) + "px";
    tooltip.style.top = Math.max(8, top) + "px";
  }
  function showTipText(text, x, y) {
    tooltip.textContent = text;
    tooltip.hidden = false;
    placeTip(x, y);
  }
  function hideTip() {
    tooltip.hidden = true;
  }
  const tipTarget = (node) => (node instanceof Element ? node.closest("[data-tip]") : null);
  document.addEventListener("pointerover", (ev) => {
    const target = tipTarget(ev.target);
    if (target) showTipText(target.getAttribute("data-tip"), ev.clientX, ev.clientY);
  });
  document.addEventListener("pointermove", (ev) => {
    if (!tooltip.hidden && tipTarget(ev.target)) placeTip(ev.clientX, ev.clientY);
  });
  document.addEventListener("pointerout", (ev) => {
    if (tipTarget(ev.target)) hideTip();
  });
  document.addEventListener("focusin", (ev) => {
    const target = tipTarget(ev.target);
    if (!target) return;
    const box = target.getBoundingClientRect();
    showTipText(target.getAttribute("data-tip"), box.left + box.width / 2, box.bottom);
  });
  document.addEventListener("focusout", (ev) => {
    if (tipTarget(ev.target)) hideTip();
  });

  // ---- the mosaic: every mutant as one square, in file and line order ----

  const canvas = document.getElementById("mosaic");
  const cells = [];
  FILES.forEach((file, fileIndex) => file.mutants.forEach((_m, mutantIndex) => cells.push([fileIndex, mutantIndex])));
  let grid = { pitch: 0, cols: 1, size: 0, colors: {} };
  let hovered = -1;

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function drawCell(ctx, index, lifted) {
    const [fileIndex, mutantIndex] = cells[index];
    const m = FILES[fileIndex].mutants[mutantIndex];
    const x = (index % grid.cols) * grid.pitch;
    const y = Math.floor(index / grid.cols) * grid.pitch;
    ctx.clearRect(x, y, grid.pitch, grid.pitch);
    ctx.fillStyle = grid.colors[m.status];
    ctx.fillRect(x, y, grid.size, grid.size);
    if (lifted) {
      ctx.strokeStyle = grid.colors.ink;
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x + 0.75, y + 0.75, grid.size - 1.5, grid.size - 1.5);
    }
  }
  function drawMosaic() {
    if (!canvas) return;
    if (!cells.length) {
      canvas.style.height = "0px";
      return;
    }
    const width = canvas.clientWidth;
    if (!width) return;
    const pitch = Math.max(4, Math.min(16, Math.floor(Math.sqrt((width * 150) / cells.length))));
    const cols = Math.max(1, Math.floor(width / pitch));
    const height = Math.ceil(cells.length / cols) * pitch;
    const ratio = window.devicePixelRatio || 1;
    canvas.style.height = height + "px";
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    grid = {
      pitch,
      cols,
      size: pitch - (pitch >= 7 ? 2 : 1),
      colors: {
        killed: token("--killed-tile"),
        survived: token("--survived"),
        no_coverage: token("--no_coverage"),
        timeout: token("--timeout"),
        ink: token("--ink"),
      },
    };
    for (let index = 0; index < cells.length; index += 1) drawCell(ctx, index, false);
    hovered = -1;
  }
  function cellAt(ev) {
    const box = canvas.getBoundingClientRect();
    const col = Math.floor((ev.clientX - box.left) / grid.pitch);
    const row = Math.floor((ev.clientY - box.top) / grid.pitch);
    if (col < 0 || col >= grid.cols || row < 0) return -1;
    const index = row * grid.cols + col;
    return index < cells.length ? index : -1;
  }
  function lift(index) {
    if (index === hovered) return;
    const ctx = canvas.getContext("2d");
    if (hovered >= 0) drawCell(ctx, hovered, false);
    if (index >= 0) drawCell(ctx, index, true);
    hovered = index;
  }
  if (canvas) {
    canvas.addEventListener("pointermove", (ev) => {
      const index = cellAt(ev);
      lift(index);
      if (index < 0) {
        hideTip();
        return;
      }
      const [fileIndex, mutantIndex] = cells[index];
      const file = FILES[fileIndex];
      const m = file.mutants[mutantIndex];
      tooltip.innerHTML =
        `<b>${LABEL[m.status]}</b> · ${esc(m.operator)}<br>${esc(file.path)}:${m.line}`;
      tooltip.hidden = false;
      placeTip(ev.clientX, ev.clientY);
    });
    canvas.addEventListener("pointerleave", () => {
      lift(-1);
      hideTip();
    });
    canvas.addEventListener("click", (ev) => {
      const index = cellAt(ev);
      if (index < 0) return;
      const [fileIndex, mutantIndex] = cells[index];
      hideTip();
      location.hash = `f${fileIndex}-l${FILES[fileIndex].mutants[mutantIndex].line}`;
    });
    let frame = 0;
    const redraw = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(drawMosaic);
    };
    new ResizeObserver(redraw).observe(canvas);
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);
    new MutationObserver(redraw).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    drawMosaic();
  }

  // ---- the files table: sort by any column, filter by path ----

  const table = document.getElementById("files");
  const filterInput = document.getElementById("file-filter");
  let sortKey = "survived";
  let sortDir = -1;
  const DEFAULT_DIR = { path: 1, score: 1 };

  function sortRows() {
    if (!table) return;
    const body = table.tBodies[0];
    const rows = Array.from(body.rows);
    const byPath = (a, b) => a.dataset.path.localeCompare(b.dataset.path);
    rows.sort((a, b) => {
      if (sortKey === "path") return sortDir * byPath(a, b);
      const left = parseFloat(a.dataset[sortKey]);
      const right = parseFloat(b.dataset[sortKey]);
      const leftMissing = Number.isNaN(left);
      const rightMissing = Number.isNaN(right);
      if (leftMissing || rightMissing) return leftMissing === rightMissing ? byPath(a, b) : leftMissing ? 1 : -1;
      return sortDir * (left - right) || byPath(a, b);
    });
    body.append(...rows);
    for (const button of table.querySelectorAll("th button[data-sort]")) {
      const state = button.dataset.sort === sortKey ? (sortDir > 0 ? "ascending" : "descending") : "none";
      button.parentElement.setAttribute("aria-sort", state);
    }
  }
  if (table) {
    table.querySelector("thead").addEventListener("click", (ev) => {
      const button = ev.target instanceof Element ? ev.target.closest("button[data-sort]") : null;
      if (!button) return;
      const key = button.dataset.sort;
      sortDir = key === sortKey ? -sortDir : DEFAULT_DIR[key] || -1;
      sortKey = key;
      sortRows();
    });
    sortRows();
  }
  if (filterInput && table) {
    filterInput.addEventListener("input", () => {
      const needle = filterInput.value.trim().toLowerCase();
      for (const row of table.tBodies[0].rows) row.hidden = needle !== "" && !row.dataset.path.toLowerCase().includes(needle);
    });
  }

  // ---- the file view ----

  const filters = { survived: true, no_coverage: true, timeout: true, killed: true };
  let current = -1;
  let byLine = new Map();
  let plainCache = new Map();
  let summaryScroll = 0;
  const scratch = document.createElement("div");

  function segmentsHtml(counts, label) {
    const total = counts.killed + counts.survived + counts.timeout + counts.no_coverage;
    const order = ["killed", "survived", "timeout", "no_coverage"];
    const parts = order
      .filter((key) => counts[key])
      .map((key) => {
        const share = Math.round((counts[key] / total) * 100);
        const tip = `${LABEL[key]}: ${fmt(counts[key])} of ${fmt(total)} (${share}%)`;
        return `<span class="seg-part v-${key}" style="flex-grow:${counts[key]}" data-tip="${esc(tip)}"></span>`;
      })
      .join("");
    const aria = order.map((key) => `${LABEL[key]} ${counts[key]}`).join(", ");
    return `<span class="seg" role="img" aria-label="${esc(label)}: ${esc(aria)}">${parts}</span>`;
  }

  function plainLine(n) {
    if (!plainCache.has(n)) {
      scratch.innerHTML = FILES[current].lines[n - 1] || "";
      plainCache.set(n, scratch.textContent);
    }
    return plainCache.get(n);
  }

  function commonIndent(lines) {
    let indent = Infinity;
    for (const line of lines) {
      if (!line.trim()) continue;
      indent = Math.min(indent, line.length - line.trimStart().length);
    }
    return Number.isFinite(indent) ? indent : 0;
  }

  function markLines(lines, startCol, endCol, indent) {
    const last = lines.length - 1;
    return lines.map((text, i) => {
      const start = i === 0 ? startCol : 0;
      const end = i === last ? endCol : text.length;
      const shown = text.slice(indent);
      const from = Math.max(0, start - indent);
      const to = Math.max(from, end - indent);
      const marked = to > from ? `<mark>${esc(shown.slice(from, to))}</mark>` : "";
      return esc(shown.slice(0, from)) + marked + esc(shown.slice(to));
    });
  }

  function diffRows(lines, cls, sign) {
    const row = (html) => `<div class="${cls}"><span class="sign" aria-hidden="true">${sign}</span>${html}</div>`;
    if (lines.length <= MAX_DIFF_LINES) return lines.map(row).join("");
    const head = lines.slice(0, 4).map(row).join("");
    const tail = lines.slice(-3).map(row).join("");
    return `${head}<div class="gap">    … ${lines.length - 7} more lines</div>${tail}`;
  }

  function diffHtml(m) {
    const before = [];
    for (let n = m.line; n <= m.end_line; n += 1) before.push(plainLine(n));
    const head = before[0].slice(0, m.col);
    const tail = before[before.length - 1].slice(m.end_col);
    const after = (head + m.mutated + tail).split("\n");
    const indent = commonIndent([...before, ...after]);
    const removed = markLines(before, m.col, m.end_col, indent);
    const added = markLines(after, head.length, after[after.length - 1].length - tail.length, indent);
    return (
      `<pre class="diff" aria-label="Original and mutated code">` +
      `${diffRows(removed, "del", "−")}${diffRows(added, "add", "+")}</pre>`
    );
  }

  function testList(refs, total) {
    const items = refs.map((ref) => `<li><code>${esc(TESTS[ref])}</code></li>`).join("");
    const more = total > refs.length ? `<li>and ${fmt(total - refs.length)} more</li>` : "";
    return `<ul>${items}${more}</ul>`;
  }

  function testsHtml(m) {
    switch (m.status) {
      case "killed": {
        const how = m.reason === "hang" ? ` It hung: ${REASON.hang}.` : "";
        if (!m.killed_by.length) return `Caught by a covering test.${how}`;
        return `Caught by ${m.killed_by.map((ref) => `<code>${esc(TESTS[ref])}</code>`).join(", ")}.${how}`;
      }
      case "survived": {
        const n = m.covering_total;
        return `All ${fmt(n)} covering ${n === 1 ? "test" : "tests"} passed with this fault in place. ` +
          `Strengthen ${n === 1 ? "it" : "one of them"}:${testList(m.covering, n)}`;
      }
      case "timeout":
        return `Unresolved: ${esc(REASON[m.reason] || "a covering test hung")}.${testList(m.covering, m.covering_total)}`;
      default:
        if (m.cold) return `Not reached by a test body: ${esc(COLD[m.cold] || COLD.unattributed)}.`;
        return "No test runs this line. Add a test that takes this path.";
    }
  }

  function mutantHtml(m) {
    const tags = [];
    if (m.bucket) tags.push(`<span class="tag${m.bucket === "high" ? " high" : ""}">${esc(m.bucket)} priority</span>`);
    if (m.kind) tags.push(`<span class="tag">${esc(m.kind)}</span>`);
    for (const tag of m.semantic) tags.push(`<span class="tag sem">${esc(tag)}</span>`);
    return (
      `<article class="mut v-${m.status}"><div class="mut-head">` +
      `<span class="verdict"><span class="dot v-${m.status}" aria-hidden="true"></span>${LABEL[m.status]}</span>` +
      `<code class="op">${esc(m.operator)}</code>${tags.join("")}</div>` +
      `${diffHtml(m)}<div class="mut-tests">${testsHtml(m)}</div></article>`
    );
  }

  function lineLabel(n, mutants) {
    const counts = countsOf(mutants);
    const parts = STATUSES.filter((key) => counts[key]).map((key) => `${counts[key]} ${LABEL[key].toLowerCase()}`);
    return `Line ${n}: ${mutants.length} ${mutants.length === 1 ? "mutant" : "mutants"} (${parts.join(", ")})`;
  }

  function renderCode() {
    const file = FILES[current];
    byLine = new Map();
    file.mutants.forEach((m) => {
      if (!filters[m.status]) return;
      if (!byLine.has(m.line)) byLine.set(m.line, []);
      byLine.get(m.line).push(m);
    });
    const parts = [];
    for (let i = 0; i < file.lines.length; i += 1) {
      const n = i + 1;
      const mutants = byLine.get(n);
      const code = `<code class="src">${file.lines[i]}</code>`;
      if (!mutants) {
        parts.push(`<div class="ln" id="L${n}"><span class="gut"><span class="num">${n}</span></span>${code}</div>`);
        continue;
      }
      mutants.sort((a, b) => SEVERITY[b.status] - SEVERITY[a.status] || a.col - b.col);
      const worst = mutants[0].status;
      parts.push(
        `<div class="ln has-${worst}" id="L${n}"><button type="button" class="gut" data-line="${n}" ` +
          `aria-expanded="false" aria-label="${esc(lineLabel(n, mutants))}">` +
          `<span class="badge v-${worst}">${mutants.length}</span><span class="num">${n}</span></button>${code}</div>`
      );
    }
    document.getElementById("code").innerHTML = parts.join("");
  }

  function renderFile(index) {
    current = index;
    plainCache = new Map();
    const file = FILES[index];
    const counts = countsOf(file.mutants);
    const slash = file.path.lastIndexOf("/");
    const dir = slash >= 0 ? file.path.slice(0, slash + 1) : "";
    const chips = STATUSES.map(
      (key) =>
        `<button type="button" class="chip" data-filter="${key}" aria-pressed="${filters[key]}">` +
        `<span class="dot v-${key}" aria-hidden="true"></span>${LABEL[key]} ${fmt(counts[key])}</button>`
    ).join("");
    const next = counts.survived
      ? `<button type="button" class="chip primary" id="next-survivor" title="Shortcut: n">Next survivor</button>`
      : "";
    const total = file.mutants.length;
    fileView.innerHTML =
      `<div class="fv-bar"><div class="wrap fv-bar-inner"><a class="fv-back" href="#">← All files</a>` +
      `<div class="fv-path"><span class="dir">${esc(dir)}</span>${esc(file.path.slice(slash + 1))}</div>` +
      `<div class="fv-tools">${chips}${next}</div></div></div>` +
      `<div class="wrap"><div class="fv-head"><div class="fv-stats">` +
      `<span><b>${fmt(total)}</b> ${total === 1 ? "mutant" : "mutants"}</span>` +
      `<span><b>${fmt(counts.survived)}</b> survived</span>` +
      `<span><b>${fmt(counts.no_coverage)}</b> without coverage</span>` +
      `<span><b>${fmt(file.lines.length)}</b> lines</span></div>` +
      `<div class="fv-score" title="Mutation score of this file">${score(counts)}</div>` +
      `${segmentsHtml(counts, file.path)}</div>` +
      `<div class="code"><div class="code-inner" id="code"></div></div></div>`;
    renderCode();
  }

  function togglePanel(n, open) {
    const line = document.getElementById("L" + n);
    const button = line && line.querySelector("button.gut");
    if (!button) return;
    const next = line.nextElementSibling;
    const isOpen = next !== null && next.classList.contains("muts");
    if (isOpen && open !== true) {
      next.remove();
      button.setAttribute("aria-expanded", "false");
      return;
    }
    if (isOpen) return;
    line.insertAdjacentHTML("afterend", `<div class="muts">${byLine.get(n).map(mutantHtml).join("")}</div>`);
    button.setAttribute("aria-expanded", "true");
  }

  function focusLine(n, open) {
    const line = document.getElementById("L" + n);
    if (!line) return;
    if (open) togglePanel(n, true);
    const bar = fileView.querySelector(".fv-bar");
    const offset = (bar ? bar.getBoundingClientRect().height : 0) + 24;
    const top = line.getBoundingClientRect().top + window.scrollY - offset;
    window.scrollTo({ top: Math.max(0, top), behavior: reduceMotion.matches ? "auto" : "smooth" });
    line.classList.add("flash");
    window.setTimeout(() => line.classList.remove("flash"), 1400);
    const button = line.querySelector("button.gut");
    if (button) button.focus({ preventScroll: true });
  }

  function nextSurvivor() {
    const bar = fileView.querySelector(".fv-bar");
    const floor = bar ? bar.getBoundingClientRect().bottom + 30 : 0;
    const lines = Array.from(document.querySelectorAll("#code .ln.has-survived"));
    if (!lines.length) return;
    const target = lines.find((line) => line.getBoundingClientRect().top > floor) || lines[0];
    focusLine(Number(target.id.slice(1)), true);
  }

  fileView.addEventListener("click", (ev) => {
    const target = ev.target instanceof Element ? ev.target : null;
    if (!target) return;
    const gutter = target.closest("button.gut");
    if (gutter) {
      togglePanel(Number(gutter.dataset.line));
      return;
    }
    const chip = target.closest("button[data-filter]");
    if (chip) {
      const key = chip.dataset.filter;
      filters[key] = !filters[key];
      chip.setAttribute("aria-pressed", String(filters[key]));
      renderCode();
      return;
    }
    if (target.closest("#next-survivor")) nextSurvivor();
  });

  document.addEventListener("keydown", (ev) => {
    if (fileView.hidden || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    const typing = ev.target instanceof Element && ev.target.closest("input, textarea, select");
    if (typing) return;
    if (ev.key === "n") {
      ev.preventDefault();
      nextSurvivor();
    } else if (ev.key === "Escape") {
      location.hash = "";
    }
  });

  function openFile(index, line) {
    if (fileView.hidden) summaryScroll = window.scrollY;
    if (current !== index) renderFile(index);
    hideTip();
    summaryView.hidden = true;
    fileView.hidden = false;
    if (line) requestAnimationFrame(() => focusLine(line, true));
    else window.scrollTo(0, 0);
  }

  function closeFile() {
    if (fileView.hidden) return;
    fileView.hidden = true;
    summaryView.hidden = false;
    requestAnimationFrame(() => window.scrollTo(0, summaryScroll));
  }

  function route() {
    const match = /^#f(\d+)(?:-l(\d+))?$/.exec(location.hash);
    if (match && FILES[Number(match[1])]) openFile(Number(match[1]), match[2] ? Number(match[2]) : 0);
    else closeFile();
  }
  window.addEventListener("hashchange", route);
  route();
})();
