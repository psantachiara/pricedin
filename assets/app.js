/* Priced In: Visualizing the Entanglements of AI News and Market Valuations.
   Reads data/config.json, data/events.json, data/prices.json and
   data/thumbnails.json, and draws everything with D3 v7. */

(function () {
  'use strict';

  const parseDate = d3.utcParse('%Y-%m-%d');
  const fmtDate = d3.utcFormat('%b %-d, %Y');
  const fmtDay = d3.utcFormat('%b %-d');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const FULL_STEM = 0.08; // an 8-point move draws a full-length stem

  const $ = (sel) => document.querySelector(sel);

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k === 'style') node.setAttribute('style', v);
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children) if (c != null) node.append(c);
    return node;
  }

  function fmtPct(v, digits = 1) {
    if (v == null || !isFinite(v)) return '–';
    const sign = v > 0 ? '+' : v < 0 ? '−' : '';
    return sign + Math.abs(v * 100).toFixed(digits) + '%';
  }
  function fmtTrillions(v) {
    if (v == null || !isFinite(v)) return '–';
    return '$' + v.toFixed(v >= 10 ? 1 : 2) + 'T';
  }
  const signClass = (v) => (v == null ? '' : v >= 0 ? 'up-text' : 'down-text');

  function hostOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return ''; }
  }

  async function loadJSON(path, optional) {
    try {
      const res = await fetch(path, { cache: 'no-cache' });
      if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
      return await res.json();
    } catch (err) {
      if (optional) return null;
      throw err;
    }
  }

  function showMessage(title, html) {
    const box = $('#stage-message');
    box.hidden = false;
    box.innerHTML = '';
    const p = el('p');
    p.innerHTML = html;
    box.append(el('h2', { text: title }), p);
    $('#chart').style.display = 'none';
    for (const sel of ['.range-row', '#overview', '.legend', '.chips-row', '.controls', '.ranking', '#detail']) {
      const n = $(sel);
      if (n) n.style.display = 'none';
    }
  }

  async function main() {
    let config, events;
    try {
      [config, events] = await Promise.all([loadJSON('data/config.json'), loadJSON('data/events.json')]);
    } catch (err) {
      console.error(err);
      showMessage('The data files did not load',
        'Serve the site over HTTP, from GitHub Pages or with <code>python -m http.server</code> in the project folder. ' +
        'Opening <code>index.html</code> straight from disk blocks the data requests.');
      return;
    }
    const [prices, thumbs] = await Promise.all([
      loadJSON('data/prices.json', true),
      loadJSON('data/thumbnails.json', true),
    ]);
    if (!prices || !Array.isArray(prices.dates) || prices.dates.length < 2) {
      showMessage('No price data yet',
        'Run the <strong>Update data</strong> workflow from the Actions tab of the repository, or run ' +
        '<code>python scripts/update_data.py</code> locally. The timeline appears once <code>data/prices.json</code> exists.');
      return;
    }
    init(config, events, prices, thumbs || {});
  }

  function init(config, rawEvents, prices, thumbs) {
    // ------------------------------------------------------------------
    // Data
    // ------------------------------------------------------------------
    const dates = prices.dates.map(parseDate);
    const N = dates.length;
    const close = prices.close || {};
    const shares = prices.shares || {};

    const tickers = config.tickers.filter((t) => Array.isArray(close[t.symbol]));
    const tickerBy = new Map(tickers.map((t) => [t.symbol, t]));
    const benchmarks = config.benchmarks.filter((b) => Array.isArray(close[b.symbol]));
    const orgs = config.orgs;
    const orgBy = new Map(orgs.map((o) => [o.id, o]));
    const cats = config.categories;
    const catBy = new Map(cats.map((c) => [c.id, c]));
    const catColor = (id) => {
      const c = catBy.get(id);
      return c ? `var(--cat-${c.id}, ${c.color})` : 'var(--muted)';
    };

    const events = rawEvents
      .map((e, k) => {
        const date = parseDate(e.date);
        if (!date) return null;
        let t = d3.bisectLeft(dates, date);
        if (e.afterClose && t < N && +dates[t] === +date) t += 1;
        if (t >= N) return null; // announced after the last price we have
        return {
          ...e,
          id: e.id || `event-${k}`,
          date,
          t,
          tickers: (e.tickers || []).filter((s) => tickerBy.has(s)),
        };
      })
      .filter(Boolean)
      .sort((a, b) => a.date - b.date || a.t - b.t);
    const eventBy = new Map(events.map((e) => [e.id, e]));

    const pickBench = (view) => {
      const want = config.defaultBenchmark && config.defaultBenchmark[view];
      return benchmarks.some((b) => b.symbol === want) ? want : benchmarks[0] && benchmarks[0].symbol;
    };

    const state = {
      view: 'detailed',
      measure: 'change',
      weighting: 'cap',
      benchmark: pickBench('detailed'),
      window: 3,
      hidden: new Set(),
      categories: new Set(cats.map((c) => c.id)),
      domain: [dates[0], dates[N - 1]],
      pinned: null, // { key, ids, t }
      hover: null,
      animated: false,
    };

    // ------------------------------------------------------------------
    // Series maths
    // ------------------------------------------------------------------
    const memo = new Map();
    const cached = (key, fn) => {
      if (!memo.has(key)) memo.set(key, fn());
      return memo.get(key);
    };
    const members = () => tickers.map((t) => t.symbol).filter((s) => !state.hidden.has(s));
    const weighting = () => (state.measure === 'value' ? 'cap' : state.weighting);

    function capOf(sym) {
      return cached('cap:' + sym, () => {
        const n = shares[sym];
        const c = close[sym];
        if (!n || !c) return null;
        return c.map((v) => (v == null ? null : v * n));
      });
    }

    // Chain-linked index: each day's return uses only companies with
    // prices on both days, so a missing print never creates a jump.
    function aggIndex(syms, how) {
      return cached(`idx:${how}:${syms.join(',')}`, () => {
        const src = how === 'cap' ? syms.map(capOf).filter(Boolean) : syms.map((s) => close[s]);
        const out = new Array(N).fill(null);
        if (!src.length) return out;
        let level = null;
        for (let i = 0; i < N; i++) {
          if (level == null) {
            if (src.some((a) => a[i] != null)) { level = 1; out[i] = 1; }
            continue;
          }
          let r = 0;
          if (how === 'cap') {
            let num = 0, den = 0;
            for (const a of src) if (a[i] != null && a[i - 1] != null) { num += a[i]; den += a[i - 1]; }
            r = den > 0 ? num / den - 1 : 0;
          } else {
            let sum = 0, n = 0;
            for (const a of src) if (a[i] != null && a[i - 1] != null) { sum += a[i] / a[i - 1] - 1; n++; }
            r = n ? sum / n : 0;
          }
          level *= 1 + r;
          out[i] = level;
        }
        return out;
      });
    }

    function aggValue(syms) {
      return cached('val:' + syms.join(','), () => {
        const caps = syms.map(capOf).filter(Boolean);
        if (!caps.length) return null;
        return dates.map((_, i) => {
          let s = 0, any = false;
          for (const a of caps) if (a[i] != null) { s += a[i]; any = true; }
          return any ? s : null;
        });
      });
    }

    const tickerRaw = (sym) => (state.measure === 'value' ? capOf(sym) : close[sym]);
    const aggRaw = () => (state.measure === 'value' ? aggValue(members()) : aggIndex(members(), weighting()));
    const benchRaw = () => close[state.benchmark];

    function transform(raw, i0) {
      if (!raw) return null;
      if (state.measure === 'value') return raw.map((v) => (v == null ? null : v / 1e12));
      const bench = benchRaw();
      let b0 = -1;
      for (let i = i0; i < N; i++) {
        if (raw[i] != null && (state.measure === 'change' || (bench && bench[i] != null))) { b0 = i; break; }
      }
      if (b0 < 0) return raw.map(() => null);
      if (state.measure === 'change') return raw.map((v) => (v == null ? null : v / raw[b0] - 1));
      return raw.map((v, i) =>
        v == null || bench[i] == null ? null : v / raw[b0] / (bench[i] / bench[b0]) - 1);
    }

    function reaction(raw, t) {
      if (!raw) return null;
      const base = t - 1;
      const end = Math.min(t + state.window - 1, N - 1);
      if (base < 0) return null;
      const a = raw[base], z = raw[end];
      if (a == null || z == null) return null;
      const r = z / a - 1;
      const b = benchRaw();
      const br = b && b[base] != null && b[end] != null ? b[end] / b[base] - 1 : null;
      return { raw: r, bench: br, abn: br == null ? null : r - br, base, end };
    }

    function scoreForIds(ids, t) {
      if (state.view === 'total') {
        const R = reaction(aggIndex(members(), weighting()), t);
        return R ? R.abn : null;
      }
      const syms = new Set();
      for (const id of ids) for (const s of eventBy.get(id).tickers) syms.add(s);
      const vals = [...syms].map((s) => reaction(close[s], t)).filter((R) => R && R.abn != null).map((R) => R.abn);
      return vals.length ? d3.mean(vals) : null;
    }

    const visibleEvents = () => events.filter((e) => state.categories.has(e.category));

    function domainIdx() {
      let i0 = d3.bisectLeft(dates, state.domain[0]);
      let i1 = d3.bisectRight(dates, state.domain[1]) - 1;
      i0 = Math.max(0, Math.min(N - 1, i0));
      i1 = Math.max(i0, Math.min(N - 1, i1));
      return [i0, i1];
    }

    const valueFmt = (v) => (state.measure === 'value' ? fmtTrillions(v) : fmtPct(v));
    const benchName = () => state.benchmark;

    // ------------------------------------------------------------------
    // Controls
    // ------------------------------------------------------------------
    const benchSelect = $('#benchmark');
    for (const b of benchmarks) benchSelect.append(el('option', { value: b.symbol, text: b.name }));
    benchSelect.addEventListener('change', () => { state.benchmark = benchSelect.value; update(); });

    document.querySelectorAll('.seg[data-key]').forEach((seg) => {
      const key = seg.dataset.key;
      seg.addEventListener('click', (ev) => {
        const btn = ev.target.closest('button');
        if (!btn || btn.disabled) return;
        const value = key === 'window' ? +btn.dataset.value : btn.dataset.value;
        if (state[key] === value) return;
        state[key] = value;
        if (key === 'view') {
          state.benchmark = pickBench(value);
          state.pinned = null;
        }
        update();
      });
    });

    function syncControls() {
      document.querySelectorAll('.seg[data-key]').forEach((seg) => {
        const key = seg.dataset.key;
        seg.querySelectorAll('button').forEach((b) => {
          const v = key === 'window' ? +b.dataset.value : b.dataset.value;
          const on = key === 'weighting' ? v === weighting() : state[key] === v;
          b.setAttribute('aria-pressed', String(on));
          if (key === 'weighting') b.disabled = state.measure === 'value' && v === 'equal';
        });
      });
      $('#weighting-control').hidden = state.view !== 'total';
      benchSelect.value = state.benchmark;
      $('#chips-label').textContent = state.view === 'total' ? 'Included in the total' : 'Companies';
      $('#legend-text').textContent = state.view === 'total'
        ? `Stems rise when the industry total beat ${benchName()} after an announcement and drop when it lagged. A full-length stem means 8 points or more.`
        : `Stems rise when the tagged stocks beat ${benchName()} after an announcement and drop when they lagged. A full-length stem means 8 points or more.`;
    }

    // Company chips double as a live readout.
    const chipWrap = $('#tickers');
    const chipVals = new Map();
    for (const t of tickers) {
      const val = el('span', { class: 'val' });
      chipVals.set(t.symbol, val);
      const chip = el('button', {
        type: 'button', class: 'chip', 'data-sym': t.symbol, title: t.name,
        style: `--c:${t.color}`,
        onclick: () => {
          if (state.hidden.has(t.symbol)) state.hidden.delete(t.symbol);
          else if (members().length > 1) state.hidden.add(t.symbol);
          update();
        },
        onmouseenter: () => emphasiseLine(t.symbol),
        onmouseleave: () => emphasiseLine(null),
      }, el('span', { class: 'dot' }), el('span', { class: 'sym', text: t.symbol }), val);
      chipWrap.append(chip);
    }

    // Announcement type toggles.
    const catWrap = $('#categories');
    for (const c of cats) {
      catWrap.append(el('button', {
        type: 'button', class: 'cat', 'data-cat': c.id, style: `--c:${catColor(c.id)}`,
        onclick: () => {
          if (state.categories.has(c.id)) {
            if (state.categories.size > 1) state.categories.delete(c.id);
          } else state.categories.add(c.id);
          if (state.pinned && !state.pinned.ids.some((id) => state.categories.has(eventBy.get(id).category))) state.pinned = null;
          update();
        },
      }, el('span', { class: 'dot' }), el('span', { text: c.name })));
    }

    function syncChips() {
      chipWrap.querySelectorAll('.chip').forEach((b) =>
        b.setAttribute('aria-pressed', String(!state.hidden.has(b.dataset.sym))));
      catWrap.querySelectorAll('.cat').forEach((b) =>
        b.setAttribute('aria-pressed', String(state.categories.has(b.dataset.cat))));
    }

    // ------------------------------------------------------------------
    // Main chart
    // ------------------------------------------------------------------
    const stage = $('#stage');
    const svg = d3.select('#chart');
    const tooltip = $('#tooltip');
    let geo = null;       // geometry of the last render
    let current = null;   // transformed series of the last render

    function render() {
      const W = Math.max(300, Math.floor(stage.clientWidth));
      const mobile = W < 640;
      const M = { l: mobile ? 70 : 104, r: mobile ? 8 : 14, t: 12 };
      const chartH = mobile ? 230 : 330;
      const top = M.t;
      const bottom = top + chartH;
      const vis = visibleEvents();
      const lanes = state.view === 'detailed'
        ? orgs.filter((o) => vis.some((e) => e.org === o.id))
        : [{ id: '__all', name: 'All news' }];
      const laneH = state.view === 'detailed' ? 40 : 76;
      const lanesTop = bottom + 50;
      const H = lanesTop + Math.max(1, lanes.length) * laneH + 6;

      svg.attr('width', W).attr('height', H).attr('viewBox', `0 0 ${W} ${H}`);
      svg.selectAll('*').remove();

      const [i0, i1] = domainIdx();
      const x = d3.scaleUtc().domain(state.domain).range([M.l, W - M.r]);

      // Series ---------------------------------------------------------
      const list = [];
      if (state.view === 'detailed') {
        for (const s of members()) {
          list.push({ key: s, label: s, color: tickerBy.get(s).color, raw: tickerRaw(s), cls: 'series' });
        }
      } else {
        list.push({ key: 'AGG', label: 'Industry total', color: 'var(--ink)', raw: aggRaw(), cls: 'series aggregate' });
      }
      if (state.measure === 'change' && benchRaw()) {
        list.push({ key: 'BENCH', label: state.benchmark, color: 'var(--muted)', raw: benchRaw(), cls: 'series benchmark' });
      }
      const series = list.filter((s) => s.raw).map((s) => ({ ...s, values: transform(s.raw, i0) }));

      let lo = Infinity, hi = -Infinity;
      for (const s of series) for (let i = i0; i <= i1; i++) {
        const v = s.values[i];
        if (v != null) { if (v < lo) lo = v; if (v > hi) hi = v; }
      }
      if (!isFinite(lo)) { lo = 0; hi = 1; }
      if (state.measure !== 'value') { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
      if (hi - lo < 1e-9) { hi += 0.01; lo -= 0.01; }
      const y = d3.scaleLinear().domain([lo, hi]).nice(mobile ? 4 : 6).range([bottom, top]);

      geo = { W, M, top, bottom, chartH, x, y, i0, i1, lanesTop, laneH, lanes, laneBottom: H - 6 };
      current = series;

      svg.append('defs').append('clipPath').attr('id', 'plot-clip')
        .append('rect').attr('x', M.l).attr('y', top - 6).attr('width', W - M.r - M.l).attr('height', chartH + 12);

      // Grid + y axis ----------------------------------------------------
      const yTicks = y.ticks(mobile ? 4 : 6);
      const step = yTicks.length > 1 ? Math.abs(yTicks[1] - yTicks[0]) : 0.1;
      const yLabel = state.measure === 'value'
        ? (d) => '$' + d3.format(step < 1 ? '.1f' : ',.0f')(d) + 'T'
        : (d) => (d === 0 ? '0%' : fmtPct(d, Math.max(0, d3.precisionFixed(step * 100))));
      const gy = svg.append('g').attr('class', 'y-axis');
      gy.selectAll('line').data(yTicks).join('line')
        .attr('class', (d) => (state.measure !== 'value' && d === 0 ? 'zero' : 'gridline'))
        .attr('x1', M.l).attr('x2', W - M.r).attr('y1', (d) => y(d)).attr('y2', (d) => y(d));
      gy.selectAll('text').data(yTicks).join('text')
        .attr('x', M.l - 8).attr('y', (d) => y(d)).attr('dy', '0.32em').attr('text-anchor', 'end')
        .text(yLabel);

      svg.append('g').attr('class', 'x-axis').attr('transform', `translate(0,${bottom})`)
        .call(d3.axisBottom(x).ticks(mobile ? 3 : 8).tickSizeOuter(0).tickSize(5).tickPadding(6));

      // Layers ---------------------------------------------------------
      svg.append('g').attr('class', 'band-layer');

      const line = d3.line()
        .defined((v) => v != null)
        .x((v, i) => x(dates[i]))
        .y((v) => y(v));
      svg.append('g').attr('class', 'lines').attr('clip-path', 'url(#plot-clip)')
        .selectAll('path').data(series, (d) => d.key).join('path')
        .attr('class', (d) => d.cls)
        .style('stroke', (d) => d.color)
        .attr('d', (d) => line(d.values));

      svg.append('g').attr('class', 'cursor');

      svg.append('rect').attr('class', 'overlay')
        .attr('x', M.l).attr('y', top).attr('width', W - M.r - M.l).attr('height', chartH)
        .on('pointermove', (ev) => {
          const [mx] = d3.pointer(ev);
          const i = d3.bisector((d) => d).center(dates, x.invert(mx));
          setCursor(Math.max(geo.i0, Math.min(geo.i1, i)));
        })
        .on('pointerleave', () => setCursor(null));

      // Lanes ------------------------------------------------------------
      const gl = svg.append('g').attr('class', 'lanes');
      gl.append('text').attr('class', 'lanes-heading')
        .attr('x', M.l).attr('y', lanesTop - 8)
        .text(state.view === 'detailed' ? 'Announcements by company' : 'All announcements, grouped by day');

      lanes.forEach((lane, k) => {
        const cy = lanesTop + k * laneH + laneH / 2;
        gl.append('line').attr('class', 'lane-rule')
          .attr('x1', M.l).attr('x2', W - M.r).attr('y1', cy).attr('y2', cy);
        gl.append('text').attr('class', 'lane-label')
          .attr('x', M.l - 10).attr('y', cy).attr('dy', '0.32em').attr('text-anchor', 'end')
          .text(lane.name);
      });

      const inRange = (t) => t >= i0 && t <= i1;
      let items;
      if (state.view === 'detailed') {
        const laneIndex = new Map(lanes.map((l, k) => [l.id, k]));
        items = vis.filter((e) => inRange(e.t) && laneIndex.has(e.org)).map((e) => ({
          key: e.id,
          ids: [e.id],
          t: e.t,
          cx: x(dates[e.t]),
          cy: lanesTop + laneIndex.get(e.org) * laneH + laneH / 2,
          color: catColor(e.category),
          count: 1,
        }));
        // Nudge same-lane markers that would sit on top of each other.
        const seen = new Map();
        for (const it of items) {
          const k = `${Math.round(it.cx / 6)}:${it.cy}`;
          const n = seen.get(k) || 0;
          it.cx += n * 7;
          seen.set(k, n + 1);
        }
      } else {
        items = [];
        const byT = d3.groups(vis.filter((e) => inRange(e.t)), (e) => e.t).sort((a, b) => a[0] - b[0]);
        for (const [t, evs] of byT) {
          const px = x(dates[t]);
          const last = items[items.length - 1];
          if (last && px - last.cx < 12) last.evs.push(...evs);
          else items.push({ t, cx: px, cy: lanesTop + laneH / 2, evs: [...evs] });
        }
        for (const it of items) {
          it.ids = it.evs.map((e) => e.id);
          it.key = 'c:' + it.ids.join('|');
          it.count = it.ids.length;
          const kinds = new Set(it.evs.map((e) => e.category));
          it.color = kinds.size === 1 ? catColor(it.evs[0].category) : 'var(--ink)';
        }
      }
      for (const it of items) {
        const evs = it.ids.map((id) => eventBy.get(id));
        it.score = scoreForIds(it.ids, it.t);
        it.label = evs.length === 1
          ? `${evs[0].title}, ${fmtDate(evs[0].date)}. Move ${fmtPct(it.score)} against ${benchName()}.`
          : `${evs.length} announcements around ${fmtDate(dates[it.t])}. Move ${fmtPct(it.score)} against ${benchName()}.`;
      }

      const stemMax = laneH / 2 - (state.view === 'detailed' ? 3 : 8);
      // Square-root scale so everyday 1-3 point moves stay visible.
      const stemLen = (v) => (v == null ? 0 : Math.sqrt(Math.min(1, Math.abs(v) / FULL_STEM)) * stemMax);
      const stemY = (d) => (d.score == null ? 0 : (d.score >= 0 ? -1 : 1) * stemLen(d.score));

      const gm = gl.selectAll('g.marker').data(items, (d) => d.key).join('g')
        .attr('class', (d) => 'marker' + (d.score == null ? '' : d.score >= 0 ? ' up' : ' down'))
        .attr('transform', (d) => `translate(${d.cx},${d.cy})`)
        .attr('tabindex', 0)
        .attr('role', 'button')
        .attr('aria-label', (d) => d.label);

      const stems = gm.append('line').attr('class', 'stem').attr('x1', 0).attr('x2', 0).attr('y1', 0);
      if (!state.animated && !reduceMotion) {
        stems.attr('y2', 0).transition().delay((d) => 150 + (d.cx - M.l) * 0.9).duration(420)
          .ease(d3.easeBackOut.overshoot(1.6)).attr('y2', stemY);
      } else {
        stems.attr('y2', stemY);
      }
      state.animated = true;

      gm.append('circle').attr('class', 'dot')
        .attr('r', (d) => (d.count > 1 ? 6 + Math.min(4, Math.sqrt(d.count - 1) * 1.7) : 4.5))
        .style('fill', (d) => d.color);
      gm.filter((d) => d.count > 1).append('text').attr('class', 'count')
        .attr('text-anchor', 'middle').attr('dy', '0.35em').text((d) => d.count);
      gm.append('circle').attr('class', 'hit').attr('r', 11);

      gm.on('pointerenter', (ev, d) => { state.hover = d; highlight(); showTooltip(d); })
        .on('pointerleave', () => { state.hover = null; highlight(); hideTooltip(); })
        .on('focus', (ev, d) => { state.hover = d; highlight(); showTooltip(d); })
        .on('blur', () => { state.hover = null; highlight(); hideTooltip(); })
        .on('click', (ev, d) => pin(d))
        .on('keydown', (ev, d) => {
          if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); pin(d); }
          if (ev.key === 'Escape') { unpin(); }
        });

      highlight();
    }

    // Hover / selection ----------------------------------------------------
    const active = () => state.hover || state.pinned;

    function taggedOf(sel) {
      const out = new Set();
      if (sel) for (const id of sel.ids) for (const s of eventBy.get(id).tickers) out.add(s);
      return out;
    }

    function highlight() {
      const a = active();
      const tagged = taggedOf(a);
      const dimming = state.view === 'detailed' && a && tagged.size > 0;
      svg.selectAll('.lines path')
        .classed('dim', (d) => dimming && d.key !== 'BENCH' && !tagged.has(d.key))
        .classed('lit', (d) => dimming && tagged.has(d.key));
      const pinnedIds = new Set(state.pinned ? state.pinned.ids : []);
      const activeIds = new Set(a ? a.ids : []);
      svg.selectAll('g.marker')
        .classed('active', (d) => d.ids.some((id) => activeIds.has(id)))
        .classed('pinned', (d) => d.ids.some((id) => pinnedIds.has(id)));
      chipWrap.querySelectorAll('.chip').forEach((c) =>
        c.classList.toggle('tagged', !!a && tagged.has(c.dataset.sym)));
      drawBand(a);
    }

    function emphasiseLine(sym) {
      if (active()) return;
      svg.selectAll('.lines path')
        .classed('dim', (d) => sym != null && state.view === 'detailed' && d.key !== sym && d.key !== 'BENCH')
        .classed('lit', (d) => sym != null && d.key === sym);
    }

    function drawBand(a) {
      const g = svg.select('.band-layer');
      g.selectAll('*').remove();
      if (!a || !geo) return;
      const base = Math.max(0, a.t - 1);
      const end = Math.min(N - 1, a.t + state.window - 1);
      const x0 = geo.x(dates[base]);
      const x1 = geo.x(dates[end]);
      const xt = geo.x(dates[a.t]);
      const left = Math.max(geo.M.l, x0);
      const right = Math.min(geo.W - geo.M.r, x1);
      if (right > left) {
        g.append('rect').attr('class', 'band')
          .attr('x', left).attr('width', right - left).attr('y', geo.top).attr('height', geo.chartH);
      }
      if (xt >= geo.M.l && xt <= geo.W - geo.M.r) {
        g.append('line').attr('class', 'guide')
          .attr('x1', xt).attr('x2', xt).attr('y1', geo.top).attr('y2', geo.laneBottom);
      }
    }

    function pin(d) {
      const same = state.pinned && state.pinned.ids.join('|') === d.ids.join('|');
      state.pinned = same ? null : { key: d.key, ids: d.ids.slice(), t: d.t };
      highlight();
      renderDetail();
      renderRanking();
    }
    function unpin() {
      state.pinned = null;
      highlight();
      renderDetail();
      renderRanking();
    }

    // Cursor readout ---------------------------------------------------------
    function setCursor(i) {
      const g = svg.select('.cursor');
      g.selectAll('*').remove();
      if (i != null && geo) {
        const cx = geo.x(dates[i]);
        g.append('line').attr('x1', cx).attr('x2', cx).attr('y1', geo.top).attr('y2', geo.bottom);
        for (const s of current) {
          const v = s.values[i];
          if (v == null) continue;
          g.append('circle').attr('cx', cx).attr('cy', geo.y(v)).attr('r', 3.5).style('fill', s.color);
        }
      }
      updateReadout(i);
    }

    function updateReadout(i) {
      if (!geo) return;
      const idx = i == null ? geo.i1 : i;
      $('#readout-date').textContent = fmtDate(dates[idx]);

      for (const t of tickers) {
        const vals = transform(tickerRaw(t.symbol), geo.i0);
        chipVals.get(t.symbol).textContent = vals ? valueFmt(vals[idx]) : '–';
      }

      const box = $('#series-readout');
      box.innerHTML = '';
      for (const s of current) {
        if (s.key !== 'AGG' && s.key !== 'BENCH') continue;
        box.append(el('span', { class: 'item' },
          el('span', { class: 'swatch-line' + (s.key === 'BENCH' ? ' dashed' : ''), style: `color:${s.color}` }),
          el('span', { class: 'key', text: s.key === 'BENCH' ? `${state.benchmark}` : s.label }),
          el('span', { class: 'val', text: valueFmt(s.values[idx]) })));
      }
      if (state.measure === 'relative') {
        box.append(el('span', { class: 'item' }, el('span', { class: 'key', text: `Measured against ${state.benchmark}; 0% means it kept pace` })));
      }
    }

    // Tooltip ----------------------------------------------------------------
    function thumbURL(e) {
      return e.thumbnail || (thumbs[e.url] && thumbs[e.url].image) || null;
    }

    function thumbEl(e, extra = '') {
      const wrap = el('div', { class: `thumb ${extra}`.trim(), style: `--tint:${catColor(e.category)}` });
      const fallback = () => {
        wrap.innerHTML = '';
        wrap.classList.add('is-fallback');
        const label = (e.source || hostOf(e.url) || '?').trim();
        wrap.append(el('span', { text: label.charAt(0).toUpperCase(), 'aria-hidden': 'true' }));
      };
      const url = thumbURL(e);
      if (url) {
        const img = el('img', { alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer' });
        img.addEventListener('error', fallback, { once: true });
        img.src = url;
        wrap.append(img);
      } else fallback();
      return wrap;
    }

    function showTooltip(d) {
      const evs = d.ids.map((id) => eventBy.get(id));
      tooltip.innerHTML = '';
      if (evs.length === 1) {
        const e = evs[0];
        tooltip.append(thumbEl(e),
          el('p', { class: 'tt-title', text: e.title }),
          el('p', { class: 'tt-meta', text: `${e.source || hostOf(e.url)}, ${fmtDate(e.date)}` }));
      } else {
        const list = el('ul', { class: 'tt-list' });
        evs.slice(0, 4).forEach((e) => list.append(el('li', { text: e.title })));
        if (evs.length > 4) list.append(el('li', { class: 'more', text: `and ${evs.length - 4} more` }));
        tooltip.append(el('p', { class: 'tt-title', text: `${evs.length} announcements` }), list);
      }
      const who = state.view === 'total' ? 'Industry total' : 'Tagged stocks';
      const days = state.window === 1 ? '1 day' : `${state.window} days`;
      tooltip.append(el('p', { class: 'tt-move' },
        el('span', { text: `${who} vs ${benchName()}, ${days}` }),
        el('b', { class: signClass(d.score), text: fmtPct(d.score) })));

      tooltip.hidden = false;
      const tw = tooltip.offsetWidth;
      const th = tooltip.offsetHeight;
      let left = d.cx + 16;
      if (left + tw > geo.W - 4) left = d.cx - tw - 16;
      left = Math.max(4, left);
      let top = d.cy - th - 12;
      if (top < 4) top = d.cy + 16;
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${top}px`;
    }
    function hideTooltip() { tooltip.hidden = true; }

    // ------------------------------------------------------------------
    // Detail panel
    // ------------------------------------------------------------------
    const detail = $('#detail');

    function moveTable(sel) {
      const rows = [];
      if (state.view === 'total') {
        rows.push({ label: 'Industry total', color: 'var(--ink)', R: reaction(aggIndex(members(), weighting()), sel.t) });
      }
      for (const s of taggedOf(sel)) {
        rows.push({ label: s, color: tickerBy.get(s).color, R: reaction(close[s], sel.t), title: tickerBy.get(s).name });
      }
      const base = Math.max(0, sel.t - 1);
      const end = Math.min(N - 1, sel.t + state.window - 1);
      const table = el('table', { class: 'move-table' },
        el('caption', { text: `From the close on ${fmtDay(dates[base])} to the close on ${fmtDate(dates[end])}.` }));
      table.append(el('thead', {}, el('tr', {},
        el('th', { scope: 'col', text: 'Stock' }),
        el('th', { scope: 'col', class: 'num', text: 'Price' }),
        el('th', { scope: 'col', class: 'num', text: `vs ${benchName()}` }))));
      const tb = el('tbody');
      for (const r of rows) {
        tb.append(el('tr', {},
          el('td', {}, el('span', { class: 'sym', title: r.title || null }, el('i', { style: `--c:${r.color}` }), r.label)),
          el('td', { class: `num ${signClass(r.R && r.R.raw)}`, text: fmtPct(r.R && r.R.raw) }),
          el('td', { class: `num ${signClass(r.R && r.R.abn)}`, text: fmtPct(r.R && r.R.abn) })));
      }
      if (rows.length) table.append(tb);
      return rows.length ? table : el('p', { class: 'none', text: 'No listed stocks are tagged for this announcement.' });
    }

    function renderDetail() {
      detail.innerHTML = '';
      const sel = state.pinned;
      if (!sel) {
        const p = el('p', { class: 'detail-empty' });
        p.innerHTML = 'Select an announcement on the timeline, or a date in the table below, to see how the stocks moved around it.';
        detail.append(p);
        return;
      }
      const evs = sel.ids.map((id) => eventBy.get(id)).filter(Boolean);
      if (evs.length === 1) {
        const e = evs[0];
        const org = orgBy.get(e.org);
        const cat = catBy.get(e.category);
        detail.append(
          el('a', { href: e.url, target: '_blank', rel: 'noopener', tabindex: '-1', 'aria-hidden': 'true' }, thumbEl(e)),
          el('h2', {}, el('a', { href: e.url, target: '_blank', rel: 'noopener', text: e.title })),
          el('p', { class: 'meta', text: `${e.source || hostOf(e.url)}, ${fmtDate(e.date)}${e.afterClose ? ', after the market closed' : ''}` }),
          el('p', { class: 'meta' },
            org ? el('span', { class: 'tag', text: org.name }) : null,
            cat ? el('span', { class: 'tag', style: `--tint:${catColor(cat.id)}`, text: cat.name }) : null));
      } else {
        const first = evs[0].date, last = evs[evs.length - 1].date;
        detail.append(
          el('h2', { text: `${evs.length} announcements` }),
          el('p', { class: 'meta', text: +first === +last ? fmtDate(first) : `${fmtDay(first)} to ${fmtDate(last)}` }));
        const ul = el('ul', { class: 'cluster-list' });
        for (const e of evs) {
          ul.append(el('li', {}, thumbEl(e, 'small'),
            el('div', {}, el('a', { href: e.url, target: '_blank', rel: 'noopener', text: e.title }),
              el('span', { class: 'src', text: `${e.source || hostOf(e.url)}, ${fmtDay(e.date)}` }))));
        }
        detail.append(ul);
      }
      detail.append(moveTable(sel));
      detail.append(el('button', { type: 'button', class: 'text-button', text: 'Clear selection', onclick: unpin }));
    }

    // ------------------------------------------------------------------
    // Ranking table
    // ------------------------------------------------------------------
    function renderRanking() {
      const [i0, i1] = domainIdx();
      const vis = visibleEvents().filter((e) => e.t >= i0 && e.t <= i1);
      let rows;
      if (state.view === 'total') {
        rows = d3.groups(vis, (e) => e.t).map(([t, evs]) => ({ t, evs, ids: evs.map((e) => e.id) }));
      } else {
        rows = vis.map((e) => ({ t: e.t, evs: [e], ids: [e.id] }));
      }
      for (const r of rows) r.score = scoreForIds(r.ids, r.t);
      rows = rows.filter((r) => r.score != null)
        .sort((a, b) => Math.abs(b.score) - Math.abs(a.score))
        .slice(0, 12);

      const days = state.window === 1 ? 'one trading day' : `${state.window} trading days`;
      $('#ranking-caption').textContent = state.view === 'total'
        ? `How the industry total moved against ${benchName()} over ${days} after each day with announcements, in the selected date range.`
        : `Average move of each announcement’s tagged stocks against ${benchName()} over ${days}, in the selected date range.`;

      const tbody = $('#ranking tbody');
      tbody.innerHTML = '';
      if (!rows.length) {
        tbody.append(el('tr', {}, el('td', { colspan: 4, class: 'more', text: 'No announcements in this range. Widen the date range or turn on more announcement types.' })));
        return;
      }
      const maxAbs = Math.max(...rows.map((r) => Math.abs(r.score)), 0.01);
      const pinnedKey = state.pinned ? state.pinned.ids.join('|') : null;
      for (const r of rows) {
        const e = r.evs[0];
        const syms = new Set();
        r.evs.forEach((ev) => ev.tickers.forEach((s) => syms.add(s)));
        const w = Math.round((Math.abs(r.score) / maxAbs) * 45);
        const tr = el('tr', { class: r.ids.join('|') === pinnedKey ? 'is-pinned' : null },
          el('td', {}, el('button', {
            type: 'button', class: 'date-button', text: fmtDate(dates[r.t]),
            'aria-label': `Show ${fmtDate(dates[r.t])} on the timeline`,
            onclick: () => focusOn(r),
          })),
          el('td', {},
            el('a', { href: e.url, target: '_blank', rel: 'noopener', text: e.title }),
            r.evs.length > 1 ? el('span', { class: 'more', text: ` and ${r.evs.length - 1} more` }) : null),
          el('td', { class: 'sym-list', text: [...syms].join(', ') || '–' }),
          el('td', { class: 'num' }, el('span', { class: 'movebar' },
            el('span', { class: 'track', 'aria-hidden': 'true' },
              el('span', { class: `fill ${r.score >= 0 ? 'up' : 'down'}`, style: `width:${w}px` })),
            el('span', { class: `v ${signClass(r.score)}`, text: fmtPct(r.score) }))));
        tbody.append(tr);
      }
    }

    function focusOn(r) {
      state.pinned = { key: r.ids.join('|'), ids: r.ids, t: r.t };
      // Make sure the date is visible on the chart.
      const d = dates[r.t];
      if (d < state.domain[0] || d > state.domain[1]) setDomain([dates[0], dates[N - 1]]);
      update();
      document.getElementById('stage').scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' });
    }

    // ------------------------------------------------------------------
    // Overview + brush
    // ------------------------------------------------------------------
    const osvg = d3.select('#overview');
    let brushCtl = null;

    function renderOverview() {
      if (!geo) return;
      const { W, M } = geo;
      const h = 54;
      osvg.attr('width', W).attr('height', h).attr('viewBox', `0 0 ${W} ${h}`);
      osvg.selectAll('*').remove();
      const xo = d3.scaleUtc().domain([dates[0], dates[N - 1]]).range([M.l, W - M.r]);
      const ref = aggIndex(tickers.map((t) => t.symbol), 'equal');
      const ext = d3.extent(ref.filter((v) => v != null));
      const yo = d3.scaleLinear().domain(ext).range([h - 12, 6]);
      const area = d3.area().defined((v) => v != null).x((v, i) => xo(dates[i])).y0(h - 12).y1((v) => yo(v));
      const ln = d3.line().defined((v) => v != null).x((v, i) => xo(dates[i])).y((v) => yo(v));
      osvg.append('path').attr('class', 'ov-area').attr('d', area(ref));
      osvg.append('path').attr('class', 'ov-line').attr('d', ln(ref));
      osvg.append('g').selectAll('line').data(events).join('line').attr('class', 'ov-tick')
        .attr('x1', (e) => xo(dates[e.t])).attr('x2', (e) => xo(dates[e.t]))
        .attr('y1', h - 9).attr('y2', h - 2);

      const brush = d3.brushX()
        .extent([[M.l, 1], [W - M.r, h - 1]])
        .on('brush end', (ev) => {
          if (!ev.sourceEvent) return;
          if (!ev.selection) {
            state.domain = [dates[0], dates[N - 1]];
            gb.call(brush.move, [xo(dates[0]), xo(dates[N - 1])]);
          } else {
            const [a, b] = ev.selection.map(xo.invert);
            // Keep at least two weeks visible.
            if (b - a < 14 * 864e5) return;
            state.domain = [a, b];
          }
          syncPresets();
          scheduleUpdate();
        });
      const gb = osvg.append('g').attr('class', 'brush').call(brush);
      gb.call(brush.move, state.domain.map(xo));
      brushCtl = { brush, gb, xo };
    }

    function setDomain(domain) {
      state.domain = domain;
      if (brushCtl) brushCtl.gb.call(brushCtl.brush.move, domain.map(brushCtl.xo));
      syncPresets();
    }

    const presetBox = $('#presets');
    presetBox.addEventListener('click', (ev) => {
      const b = ev.target.closest('button');
      if (!b) return;
      const months = +b.dataset.months;
      const end = dates[N - 1];
      const start = months ? d3.max([dates[0], d3.utcMonth.offset(end, -months)]) : dates[0];
      setDomain([start, end]);
      update();
    });
    function syncPresets() {
      const end = dates[N - 1];
      presetBox.querySelectorAll('button').forEach((b) => {
        const months = +b.dataset.months;
        const start = months ? d3.max([dates[0], d3.utcMonth.offset(end, -months)]) : dates[0];
        const on = Math.abs(state.domain[0] - start) < 2 * 864e5 && Math.abs(state.domain[1] - end) < 2 * 864e5;
        b.setAttribute('aria-pressed', String(on));
      });
    }

    // ------------------------------------------------------------------
    // Update cycle
    // ------------------------------------------------------------------
    let frame = null;
    function scheduleUpdate() {
      if (frame) return;
      frame = requestAnimationFrame(() => { frame = null; update(); });
    }

    function update() {
      syncControls();
      syncChips();
      hideTooltip();
      state.hover = null;
      render();
      updateReadout(null);
      renderDetail();
      renderRanking();
    }

    let lastWidth = 0;
    new ResizeObserver(() => {
      const w = Math.floor(stage.clientWidth);
      if (w === lastWidth) return;
      lastWidth = w;
      render();
      renderOverview();
      updateReadout(null);
    }).observe(stage);

    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && state.pinned) unpin();
    });

    const status = $('#data-status');
    const updated = prices.updated ? `, fetched ${prices.updated.replace('T', ' at ').replace('Z', ' UTC')}` : '';
    status.textContent = `Prices from ${fmtDate(dates[0])} through ${fmtDate(dates[N - 1])}${updated}. ${events.length} announcements.`;

    update();
    renderOverview();
    syncPresets();
  }

  main();
})();
