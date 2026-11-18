// Search page: query box, trie autocomplete, results, pagination, click logging, AI answer panel.
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var input = $("q"), form = $("searchForm"), list = $("suggest");
  var state = { q: "", page: 1, queryId: null, ai: false, suggestions: [], active: -1, reqSeq: 0 };
  var reduced = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var ICON_SEARCH = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="m20 20-4.2-4.2"/></svg>';
  var ICON_DOC = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>';
  var ICON_TERM = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h10M4 17h6"/></svg>';

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function animateIn(nodes) {
    if (reduced || !window.gsap || !nodes.length) return;
    window.gsap.from(nodes, { y: 10, opacity: 0, duration: 0.35, stagger: 0.035, ease: "power2.out", clearProps: "all" });
  }
  function showError(msg) {
    var el = $("error");
    el.textContent = msg || "";
    el.hidden = !msg;
  }
  async function getJSON(url, opts) {
    var res = await fetch(url, opts);
    var data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON */ }
    if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
    return data;
  }

  // ---------------------------------------------------------------- autocomplete
  var suggestTimer = null, suggestSeq = 0;
  function closeSuggest() {
    clearTimeout(suggestTimer); suggestSeq++; // drop any in-flight suggestion request
    list.hidden = true; list.innerHTML = ""; state.suggestions = []; state.active = -1;
    input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant");
  }
  function renderSuggest(prefix, items) {
    state.suggestions = items; state.active = -1;
    if (!items.length || document.activeElement !== input) return closeSuggest();
    var p = prefix.toLowerCase();
    list.innerHTML = items.map(function (s, i) {
      var text = s.text, html;
      html = text.toLowerCase().indexOf(p) === 0 ? "<b>" + esc(text.slice(0, p.length)) + "</b>" + esc(text.slice(p.length)) : esc(text);
      var icon = s.kind === "title" ? ICON_DOC : s.kind === "term" ? ICON_TERM : ICON_SEARCH;
      var meta = s.kind === "query" ? Math.round(s.weight) + " searches" : s.kind;
      return '<li role="option" id="sg-' + i + '" data-i="' + i + '" aria-selected="false">' + icon +
        '<span class="s-text">' + html + '</span><span class="s-meta">' + esc(meta) + "</span></li>";
    }).join("");
    list.hidden = false; input.setAttribute("aria-expanded", "true");
  }
  function setActive(i) {
    var n = state.suggestions.length;
    if (!n) return;
    state.active = (i + n) % n;
    Array.prototype.forEach.call(list.children, function (li, j) {
      li.setAttribute("aria-selected", j === state.active ? "true" : "false");
    });
    input.setAttribute("aria-activedescendant", "sg-" + state.active);
  }
  input.addEventListener("input", function () {
    clearTimeout(suggestTimer);
    var prefix = input.value;
    if (!prefix.trim()) return closeSuggest();
    var seq = ++suggestSeq;
    suggestTimer = setTimeout(async function () {
      try {
        var data = await getJSON("/api/suggest?k=8&q=" + encodeURIComponent(prefix));
        if (seq === suggestSeq && input.value === prefix) renderSuggest(prefix.trim(), data.suggestions);
      } catch (e) { closeSuggest(); }
    }, 90);
  });
  input.addEventListener("keydown", function (e) {
    if (list.hidden) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(state.active + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(state.active - 1); }
    else if (e.key === "Escape") { closeSuggest(); }
    else if (e.key === "Enter" && state.active >= 0) {
      e.preventDefault(); input.value = state.suggestions[state.active].text; closeSuggest(); run(input.value, 1);
    }
  });
  list.addEventListener("mousedown", function (e) {
    var li = e.target.closest("li");
    if (!li) return;
    e.preventDefault();
    input.value = state.suggestions[+li.dataset.i].text; closeSuggest(); run(input.value, 1);
  });
  input.addEventListener("blur", function () { setTimeout(closeSuggest, 120); });

  // ---------------------------------------------------------------- search
  form.addEventListener("submit", function (e) { e.preventDefault(); closeSuggest(); run(input.value, 1); });
  $("examples").addEventListener("click", function (e) {
    var chip = e.target.closest(".chip");
    if (chip) { input.value = chip.dataset.q; run(chip.dataset.q, 1); }
  });
  $("aiBtn").addEventListener("click", function () {
    state.ai = !state.ai;
    this.setAttribute("aria-pressed", String(state.ai));
    if (state.ai && state.q) loadAnswer(state.q); else $("answer").hidden = true;
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "/" && document.activeElement !== input && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) {
      e.preventDefault(); input.focus(); input.select();
    }
  });
  window.addEventListener("popstate", function () { fromUrl(false); });

  async function run(q, page, push) {
    q = (q || "").trim();
    if (!q) return;
    showError("");
    var seq = ++state.reqSeq;
    try {
      var data = await getJSON("/api/search?q=" + encodeURIComponent(q) + "&page=" + page);
      if (seq !== state.reqSeq) return;
      var changed = q !== state.q;
      state.q = q; state.page = page; state.queryId = data.query_id;
      if (push !== false) history.pushState(null, "", "/?q=" + encodeURIComponent(q) + (page > 1 ? "&page=" + page : ""));
      document.title = q + " - Sift";
      render(data);
      if (state.ai && (changed || $("answer").hidden)) loadAnswer(q);
    } catch (e) { showError(e.message); }
  }

  function render(d) {
    document.body.classList.add("has-results");
    $("resultsShell").hidden = false;
    $("resultsCount").textContent = d.total.toLocaleString() + (d.total === 1 ? " result" : " results");
    $("took").textContent = d.took_ms.toFixed(2) + " ms";
    $("cacheBadge").hidden = !d.cache_hit;
    var chips = [];
    d.parsed.phrases.forEach(function (p) { chips.push('<span class="badge info">"' + esc(p) + '"</span>'); });
    d.parsed.excluded.forEach(function (t) { chips.push('<span class="badge warn">-' + esc(t) + "</span>"); });
    $("parsed").innerHTML = chips.join("");

    var box = $("results");
    if (!d.results.length) {
      box.innerHTML = '<div class="card empty">' + ICON_SEARCH + "<h3>No documents match</h3>" +
        "<p>Try fewer words, drop a quoted phrase, or check the spelling. Zero-result queries show up on the analytics page.</p></div>";
      $("pager").innerHTML = "";
      return;
    }
    // title_html and snippet are escaped server-side; only <mark> tags are added there.
    box.innerHTML = d.results.map(function (r) {
      return '<article class="card result" id="r-' + r.doc_id + '">' +
        '<span class="r-url">' + esc(r.url) + "</span>" +
        '<h3><a href="' + esc(r.url) + '" target="_blank" rel="noopener noreferrer" data-doc="' + r.doc_id + '" data-rank="' + r.rank + '">' + r.title_html + "</a></h3>" +
        "<p>" + r.snippet + "</p>" +
        '<div class="r-foot"><span class="badge neutral num">#' + r.rank + '</span><span class="score">bm25 ' + r.score.toFixed(3) + "</span></div></article>";
    }).join("");
    renderPager(d.page, d.pages);
    animateIn(box.querySelectorAll(".result"));
    if (!reduced) window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function renderPager(page, pages) {
    var nav = $("pager");
    if (pages <= 1) { nav.innerHTML = ""; return; }
    var nums = [], i;
    for (i = 1; i <= pages; i++) if (i === 1 || i === pages || Math.abs(i - page) <= 1) nums.push(i);
    var html = '<button type="button" data-p="' + (page - 1) + '"' + (page === 1 ? " disabled" : "") + ' aria-label="Previous page">&lsaquo;</button>';
    var prev = 0;
    nums.forEach(function (n) {
      if (n - prev > 1) html += '<span class="gap">&hellip;</span>';
      html += '<button type="button" data-p="' + n + '"' + (n === page ? ' aria-current="page"' : "") + ">" + n + "</button>";
      prev = n;
    });
    html += '<button type="button" data-p="' + (page + 1) + '"' + (page === pages ? " disabled" : "") + ' aria-label="Next page">&rsaquo;</button>';
    nav.innerHTML = html;
  }
  $("pager").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-p]");
    if (b && !b.disabled) run(state.q, +b.dataset.p);
  });

  // ---------------------------------------------------------------- click logging (feeds CTR)
  $("results").addEventListener("click", function (e) {
    var a = e.target.closest("a[data-doc]");
    if (!a || !state.queryId) return;
    var body = JSON.stringify({ query_id: state.queryId, doc_id: +a.dataset.doc, rank: +a.dataset.rank });
    var blob = new Blob([body], { type: "application/json" });
    if (!(navigator.sendBeacon && navigator.sendBeacon("/api/click", blob))) {
      fetch("/api/click", { method: "POST", headers: { "Content-Type": "application/json" }, body: body, keepalive: true }).catch(function () {});
    }
  });

  // ---------------------------------------------------------------- AI answer
  async function loadAnswer(q) {
    var card = $("answer"), body = $("answerBody");
    card.hidden = false;
    $("answerMode").textContent = "thinking";
    body.innerHTML = '<div class="shimmer" style="width:92%"></div><div class="shimmer" style="width:78%"></div><div class="shimmer" style="width:60%"></div>';
    try {
      var a = await getJSON("/api/answer", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ q: q }) });
      if (q !== state.q) return;
      $("answerMode").textContent = a.mode === "claude" ? "Claude - " + a.model : "extractive";
      var text = esc(a.answer).replace(/\[(\d+)\]/g, function (m, n) {
        return '<button type="button" class="cite" data-n="' + n + '" title="Jump to source ' + n + '">' + n + "</button>";
      });
      var srcs = a.citations.map(function (c) {
        return '<a href="' + esc(c.url) + '" target="_blank" rel="noopener noreferrer" class="' + (c.used ? "" : "unused") + '">[' + c.n + "] " + esc(c.title) + "</a>";
      }).join("");
      body.innerHTML = '<p class="answer-text">' + text + '</p><div class="answer-sources">' + srcs + "</div>" +
        (a.note ? '<div class="answer-note">' + esc(a.note) + "</div>" : "");
      body._citations = a.citations;
      animateIn([card]);
    } catch (e) {
      $("answerMode").textContent = "error";
      body.innerHTML = '<p class="answer-note">' + esc(e.message) + "</p>";
    }
  }
  $("answerBody").addEventListener("click", function (e) {
    var b = e.target.closest(".cite");
    if (!b || !this._citations) return;
    var c = this._citations.filter(function (x) { return String(x.n) === b.dataset.n; })[0];
    var el = c && $("r-" + c.doc_id);
    if (!el) { if (c) window.open(c.url, "_blank", "noopener"); return; }
    el.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "center" });
    el.classList.add("flash");
    setTimeout(function () { el.classList.remove("flash"); }, 1200);
  });

  // ---------------------------------------------------------------- boot
  async function loadStats() {
    try {
      var s = await getJSON("/api/stats");
      $("indexStats").innerHTML = "<span><b>" + s.documents.toLocaleString() + "</b> documents</span>" +
        "<span><b>" + s.terms.toLocaleString() + "</b> terms</span>" +
        "<span><b>" + s.postings.toLocaleString() + "</b> postings</span>" +
        "<span><b>" + s.suggestions.toLocaleString() + "</b> autocomplete entries</span>";
      $("modeText").textContent = s.ai_mode === "claude" ? "Claude answers" : "extractive answers";
      if (s.ai_mode === "claude") $("modeBadge").className = "badge";
    } catch (e) { /* stats are decorative */ }
  }
  function fromUrl(push) {
    var p = new URLSearchParams(location.search);
    var q = p.get("q");
    if (q) { input.value = q; run(q, Math.max(1, parseInt(p.get("page"), 10) || 1), push); }
    else {
      document.body.classList.remove("has-results"); $("resultsShell").hidden = true; state.q = ""; input.value = "";
    }
  }
  loadStats();
  fromUrl(false);
  if (!reduced && window.gsap) {
    window.gsap.from(".hero h1, .hero p, .search-box, .examples", { y: 14, opacity: 0, duration: 0.5, stagger: 0.06, ease: "power2.out", clearProps: "all" });
  }
})();
