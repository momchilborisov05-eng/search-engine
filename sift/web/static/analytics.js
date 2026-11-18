// Analytics dashboard: KPI tiles, Chart.js charts, top / zero-result query tables, cache stats.
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var reduced = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var days = 7, charts = {}, last = null, firstLoad = true;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function pct(x) { return (x * 100).toFixed(1) + "%"; }
  function ms(x) { return x < 10 ? x.toFixed(2) : x.toFixed(1); }
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function ago(ts) {
    var s = Date.now() / 1000 - ts;
    if (s < 3600) return Math.max(1, Math.round(s / 60)) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }

  function countUp(el, to, fmt) {
    if (reduced || !window.gsap || !firstLoad) { el.textContent = fmt(to); return; }
    var o = { v: 0 };
    window.gsap.to(o, { v: to, duration: 0.9, ease: "power2.out", onUpdate: function () { el.textContent = fmt(o.v); } });
  }

  function renderKpis(d) {
    countUp($("kSearches"), d.searches, function (v) { return Math.round(v).toLocaleString(); });
    $("kUnique").textContent = d.unique_queries + " unique queries";
    countUp($("kCtr"), d.ctr, pct);
    $("kClicks").textContent = d.clicks + " clicks" + (d.mean_click_rank ? ", mean rank " + d.mean_click_rank.toFixed(1) : "");
    countUp($("kZero"), d.zero_result_rate, pct);
    $("kP50").textContent = ms(d.latency_ms.p50) + " ms";
    $("kP50u").textContent = "uncached " + ms(d.latency_ms.p50_uncached) + " ms";
    $("kP95").textContent = ms(d.latency_ms.p95) + " ms";
    $("kP95u").textContent = "uncached " + ms(d.latency_ms.p95_uncached) + " ms";
    countUp($("kCache"), d.cache_hit_rate_logged, pct);
    $("kCacheSub").textContent = "of logged searches";
    $("demoNote").innerHTML = d.demo_searches
      ? '<span class="badge warn"><span class="dot"></span>' + d.demo_searches + " simulated demo searches included</span>"
      : "";
  }

  function renderTables(d) {
    var maxCount = d.top_queries.reduce(function (m, r) { return Math.max(m, r.count); }, 1);
    $("topBody").innerHTML = d.top_queries.length ? d.top_queries.map(function (r) {
      return '<tr><td><a class="q" href="/?q=' + encodeURIComponent(r.query) + '">' + esc(r.query) + "</a></td>" +
        '<td class="n"><span class="bar"><i style="width:' + Math.round((r.count / maxCount) * 100) + '%"></i></span>' + r.count + "</td>" +
        '<td class="n">' + r.avg_results.toFixed(0) + "</td><td>" + pct(r.ctr) + "</td></tr>";
    }).join("") : '<tr><td colspan="4" class="muted">No searches in this range yet.</td></tr>';

    $("zeroBody").innerHTML = d.zero_result_queries.length ? d.zero_result_queries.map(function (r) {
      return "<tr><td>" + esc(r.query) + '</td><td class="n">' + r.count + '</td><td class="n muted">' + ago(r.last_ts) + "</td></tr>";
    }).join("") : '<tr><td colspan="3" class="muted">Every query found something.</td></tr>';

    var c = d.cache;
    $("cacheGrid").innerHTML = [
      ["Hit rate (live)", pct(c.hit_rate)], ["Entries", c.size + " / " + c.capacity],
      ["Hits / misses", c.hits + " / " + c.misses], ["Evictions", c.evictions],
    ].map(function (x) { return '<div><div class="label">' + x[0] + '</div><div class="v">' + x[1] + "</div></div>"; }).join("");
  }

  function renderCharts(d) {
    if (!window.Chart) return;
    var text2 = css("--text-2"), grid = css("--border"), accent = css("--accent"), warn = css("--warning");
    Chart.defaults.font.family = css("--font") || "Inter, sans-serif";
    Chart.defaults.color = text2;
    var base = {
      responsive: true, maintainAspectRatio: false, animation: reduced ? false : { duration: 600 },
      plugins: { legend: { display: false }, tooltip: { mode: "index", intersect: false } },
      scales: { x: { grid: { display: false } }, y: { beginAtZero: true, grid: { color: grid }, ticks: { precision: 0 } } },
    };
    Object.keys(charts).forEach(function (k) { charts[k].destroy(); });
    charts.daily = new Chart($("dailyChart"), {
      type: "bar",
      data: {
        labels: d.daily.map(function (r) { return r.day.slice(5); }),
        datasets: [
          { label: "With results", data: d.daily.map(function (r) { return r.searches - r.zero; }), backgroundColor: accent, borderRadius: 6, stack: "s" },
          { label: "Zero results", data: d.daily.map(function (r) { return r.zero; }), backgroundColor: warn, borderRadius: 6, stack: "s" },
        ],
      },
      options: Object.assign({}, base, { scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, beginAtZero: true, grid: { color: grid }, ticks: { precision: 0 } } } }),
    });
    charts.latency = new Chart($("latencyChart"), {
      type: "bar",
      data: { labels: d.latency_histogram.labels, datasets: [{ label: "Searches", data: d.latency_histogram.counts, backgroundColor: accent, borderRadius: 6 }] },
      options: base,
    });
  }

  async function load() {
    try {
      var res = await fetch("/api/analytics?days=" + days);
      var d = await res.json();
      if (!res.ok) throw new Error(d.error || "HTTP " + res.status);
      $("error").hidden = true;
      last = d;
      renderKpis(d); renderTables(d); renderCharts(d);
      if (firstLoad && !reduced && window.gsap) {
        window.gsap.from(".kpi, .panel", { y: 12, opacity: 0, duration: 0.45, stagger: 0.04, ease: "power2.out", clearProps: "all" });
      }
      firstLoad = false;
    } catch (e) {
      $("error").textContent = "Could not load analytics: " + e.message;
      $("error").hidden = false;
    }
  }

  $("range").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-days]");
    if (!b) return;
    days = +b.dataset.days;
    Array.prototype.forEach.call(this.children, function (x) { x.setAttribute("aria-pressed", String(x === b)); });
    load();
  });
  $("refresh").addEventListener("click", load);
  document.addEventListener("themechange", function () { if (last) renderCharts(last); });
  load();
})();
