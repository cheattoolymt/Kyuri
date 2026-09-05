/*
 * 胡瓜価格、胡瓜食いたい — アプリ本体
 *
 * data/index.json を読み込み、相場ボード・チャート・各期間の統計表を描画する。
 */

import { PriceChart } from './chart.js';

const DATA_URL = new URL('../../data/index.json', import.meta.url).href;

const RANGE_DEFS = [
  { key: '1w', label: '1週', days: 12, granularity: 'daily' },
  { key: '1m', label: '1月', days: 34, granularity: 'daily' },
  { key: '3m', label: '3月', days: 95, granularity: 'daily' },
  { key: '6m', label: '6月', days: 190, granularity: 'daily' },
  { key: '1y', label: '1年', days: 370, granularity: 'daily' },
  { key: '3y', label: '3年', days: 1105, granularity: 'weekly' },
  { key: 'all', label: '全体', days: null, granularity: 'monthly' },
];

/** 期間別テーブルに出す窓（1 日窓は起点と終点が同一なので持たない） */
const WINDOW_LABELS = {
  '1w': '1週間',
  '1m': '1か月',
  '3m': '3か月',
  '6m': '6か月',
  '1y': '1年',
  '3y': '3年',
  '5y': '5年',
};

/** ティッカーに出す「〜前比」の基準点 */
const REFERENCE_LABELS = {
  '1d': '前営業日',
  '1w': '1週間',
  '1m': '1か月',
  '1y': '1年',
};

const MONTH_NAMES = [
  '1月', '2月', '3月', '4月', '5月', '6月',
  '7月', '8月', '9月', '10月', '11月', '12月',
];

const state = {
  data: null,
  chart: null,
  range: '1y',
  mode: 'candle',
  showMA: true,
  showVolume: true,
  showRetail: false,
};

/* ---------------- 書式ユーティリティ ---------------- */

const yen = (value, digits = 0) =>
  value == null
    ? '—'
    : Number(value).toLocaleString('ja-JP', {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      });

const signed = (value, digits = 1) => {
  if (value == null) return '—';
  const sign = value > 0 ? '+' : '';
  return `${sign}${Number(value).toFixed(digits)}`;
};

const pctClass = (value) =>
  value == null ? 'flat' : value > 0 ? 'up' : value < 0 ? 'down' : 'flat';

const formatDate = (iso) => {
  if (!iso) return '—';
  const [y, m, d] = iso.split('-');
  return `${y}年${Number(m)}月${Number(d)}日`;
};

const shortDate = (iso) => (iso ? iso.replace(/-/g, '/') : '—');

/** 1 本（約 100g）あたりの参考価格。kg 単価から換算する。 */
const perPiece = (pricePerKg) => (pricePerKg == null ? null : pricePerKg * 0.1);

function el(id) {
  return document.getElementById(id);
}

function setText(id, text) {
  const node = el(id);
  if (node) node.textContent = text;
}

function setHTML(id, html) {
  const node = el(id);
  if (node) node.innerHTML = html;
}

/* ---------------- データ読み込み ---------------- */

async function loadData() {
  const response = await fetch(`${DATA_URL}?v=${Date.now()}`, { cache: 'no-store' });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  const payload = await response.json();
  if (!payload?.daily?.length) {
    throw new Error('日次データが空です');
  }
  return payload;
}

/* ---------------- 相場ボード ---------------- */

function renderQuote(data) {
  const s = data.summary;

  setText('quote-price', yen(s.price, 1));
  setText('quote-asof', `${formatDate(s.asOf)} の卸売価格（東京都中央卸売市場・速報値）`);

  const changeNode = el('quote-change');
  if (changeNode) {
    changeNode.className = `quote-change ${pctClass(s.changePct)}`;
    changeNode.textContent =
      s.change == null
        ? '前営業日比 —'
        : `${signed(s.change)} 円 (${signed(s.changePct, 2)}%)`;
  }

  const badges = [`建値 ${s.samples ?? '—'} 件`];
  if (s.volumeKg) badges.push(`卸売数量 ${yen(s.volumeKg)} kg`);
  badges.push(`1本(約100g) 約 ${yen(perPiece(s.price), 1)} 円`);
  if (data.meta?.generatedAt) {
    badges.push(`更新 ${data.meta.generatedAt.slice(0, 16).replace('T', ' ')}`);
  }
  setHTML('quote-badges', badges.map((b) => `<span class="badge">${b}</span>`).join(''));

  const ref1d = s.reference?.['1d'];
  const stats = [
    ['始値（前日終値）', yen(s.open, 1), '円/kg'],
    ['当日 高値水準', yen(s.dayHigh, 1), '高値建値の中央値'],
    ['当日 安値水準', yen(s.dayLow, 1), '安値建値の中央値'],
    ['前営業日終値', yen(s.previousClose, 1), ref1d ? shortDate(ref1d.date) : ''],
    ['年間高値', yen(s.yearRange?.high, 1), '直近1年'],
    ['年間安値', yen(s.yearRange?.low, 1), '直近1年'],
    ['年間平均', yen(s.yearRange?.mean, 1), '直近1年'],
    ['過去最安値', yen(s.allTime?.low?.price, 1), shortDate(s.allTime?.low?.date)],
    ['過去最高値', yen(s.allTime?.high?.price, 1), shortDate(s.allTime?.high?.date)],
    ['収録期間', `${s.allTime?.sessions ?? '—'} 営業日`, `${shortDate(s.allTime?.from)} 〜`],
  ];

  setHTML(
    'quote-stats',
    stats
      .map(
        ([label, value, sub]) => `
        <div class="stat">
          <div class="stat-label">${label}</div>
          <div class="stat-value tabular">${value}</div>
          <div class="stat-sub">${sub ?? ''}</div>
        </div>`
      )
      .join('')
  );
}

/* ---------------- ティッカー ---------------- */

function renderTicker(data) {
  const s = data.summary;
  const items = [['胡瓜 現在値', `${yen(s.price, 1)} 円/kg`]];

  for (const [key, label] of Object.entries(REFERENCE_LABELS)) {
    const ref = s.reference?.[key];
    if (!ref || ref.changePct == null) continue;
    items.push([`${label}前比`, `${signed(ref.changePct, 2)}%`]);
  }
  if (s.allTime?.low) {
    items.push(['最安値', `${yen(s.allTime.low.price, 1)} 円 (${shortDate(s.allTime.low.date)})`]);
  }
  if (s.allTime?.high) {
    items.push(['最高値', `${yen(s.allTime.high.price, 1)} 円 (${shortDate(s.allTime.high.date)})`]);
  }
  const retail = data.retail?.[data.retail.length - 1];
  if (retail) {
    items.push(['小売価格', `${yen(retail.price)} 円/kg (${shortDate(retail.date)})`]);
  }
  if (s.volumeKg) {
    items.push(['卸売数量', `${yen(s.volumeKg)} kg`]);
  }

  const markup = items
    .map(([label, value]) => `<span class="ticker-item">${label}<strong>${value}</strong></span>`)
    .join('');
  // シームレスループのため 2 周分を並べる
  setHTML('ticker-track', markup + markup);
}

/* ---------------- チャート ---------------- */

function seriesForRange(data, rangeKey) {
  const def = RANGE_DEFS.find((r) => r.key === rangeKey) ?? RANGE_DEFS[4];
  const source =
    def.granularity === 'daily'
      ? data.daily
      : def.granularity === 'weekly'
      ? data.weekly
      : data.monthly;

  const normalized = source.map((entry) => ({
    date: entry.date ?? entry.to,
    open: entry.open,
    high: entry.high,
    low: entry.low,
    close: entry.close,
    volumeKg: entry.volumeKg,
    samples: entry.samples ?? entry.sessions,
  }));

  if (!def.days) return normalized;

  const last = normalized[normalized.length - 1];
  if (!last) return normalized;
  const cutoff = new Date(`${last.date}T00:00:00Z`);
  cutoff.setUTCDate(cutoff.getUTCDate() - def.days);
  const cutoffISO = cutoff.toISOString().slice(0, 10);
  const filtered = normalized.filter((e) => e.date >= cutoffISO);
  return filtered.length >= 2 ? filtered : normalized.slice(-2);
}

function renderChart() {
  const { data, chart, range } = state;
  if (!chart) return;

  const series = seriesForRange(data, range);
  chart.setData(series, data.retail ?? []);
  chart.setOptions({
    mode: state.mode,
    showMA: state.showMA,
    showVolume: state.showVolume,
    showOverlay: state.showRetail,
  });

  const def = RANGE_DEFS.find((r) => r.key === range);
  const granularityLabel =
    def.granularity === 'daily' ? '日足' : def.granularity === 'weekly' ? '週足' : '月足';
  setText(
    'chart-caption',
    `${granularityLabel} ${series.length} 本 / ${shortDate(series[0]?.date)} 〜 ${shortDate(
      series[series.length - 1]?.date
    )}`
  );
}

/* ---------------- 期間別の表 ---------------- */

function renderWindows(data) {
  const windows = data.summary.windows ?? {};
  const rows = Object.entries(WINDOW_LABELS)
    .filter(([key]) => windows[key])
    .map(([key, label]) => {
      const w = windows[key];
      return `
        <tr>
          <td>${label}</td>
          <td class="num">${shortDate(w.from)}</td>
          <td class="num">${yen(w.open, 1)}</td>
          <td class="num">${yen(w.close, 1)}</td>
          <td class="num ${pctClass(w.changePct)}">${signed(w.changePct, 2)}%</td>
          <td class="num">${yen(w.mean, 1)}</td>
          <td class="num up">${yen(w.high.price, 1)}<span class="stat-sub"> ${shortDate(
        w.high.date
      )}</span></td>
          <td class="num down">${yen(w.low.price, 1)}<span class="stat-sub"> ${shortDate(
        w.low.date
      )}</span></td>
          <td class="num">${w.sessions}</td>
        </tr>`;
    })
    .join('');

  const all = data.summary.allTime;
  const allRow = `
    <tr>
      <td>全期間</td>
      <td class="num">${shortDate(all.from)}</td>
      <td class="num">—</td>
      <td class="num">${yen(data.summary.price, 1)}</td>
      <td class="num">—</td>
      <td class="num">—</td>
      <td class="num up">${yen(all.high.price, 1)}<span class="stat-sub"> ${shortDate(
    all.high.date
  )}</span></td>
      <td class="num down">${yen(all.low.price, 1)}<span class="stat-sub"> ${shortDate(
    all.low.date
  )}</span></td>
      <td class="num">${all.sessions}</td>
    </tr>`;

  setHTML('windows-body', rows + allRow);
}

/* ---------------- 年次・年度の表 ---------------- */

function renderYearly(data) {
  const rows = (data.yearly ?? []).slice().reverse();
  if (!rows.length) return;
  setHTML(
    'yearly-body',
    rows
      .map(
        (r) => `
      <tr>
        <td>${r.period} 年</td>
        <td class="num">${yen(r.open, 1)}</td>
        <td class="num">${yen(r.close, 1)}</td>
        <td class="num">${yen(r.mean, 1)}</td>
        <td class="num up">${yen(r.high, 1)}</td>
        <td class="num down">${yen(r.low, 1)}</td>
        <td class="num">${r.sessions}</td>
        <td class="num">${r.volumeKg ? yen(r.volumeKg) : '—'}</td>
      </tr>`
      )
      .join('')
  );
}

/* ---------------- 季節性 ---------------- */

function renderSeasonality(data) {
  const series = data.seasonality ?? [];
  if (!series.length) return;

  const values = series.map((s) => s.mean);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const cheapest = series.find((s) => s.mean === min);

  setHTML(
    'season-grid',
    series
      .map((s) => {
        const ratio = max === min ? 0.5 : (s.mean - min) / (max - min);
        const best = s === cheapest ? ' season-best' : '';
        return `
          <div class="season-cell${best}" title="${MONTH_NAMES[s.month - 1]}: 平均 ${yen(
          s.mean,
          1
        )} 円/kg（${s.samples} 営業日 / 範囲 ${yen(s.min, 1)}〜${yen(s.max, 1)}）">
            <div class="season-month">${MONTH_NAMES[s.month - 1]}</div>
            <div class="season-value tabular">${yen(s.mean)}</div>
            <div class="season-bar"><span style="width:${Math.round(ratio * 100)}%"></span></div>
          </div>`;
      })
      .join('')
  );

  setText(
    'season-note',
    `収録期間の月別平均。最も安いのは ${MONTH_NAMES[cheapest.month - 1]}（平均 ${yen(
      cheapest.mean,
      1
    )} 円/kg）、最も高いのは ${
      MONTH_NAMES[series.find((s) => s.mean === max).month - 1]
    }（平均 ${yen(max, 1)} 円/kg）。`
  );
}

/* ---------------- 市場別 ---------------- */

function renderMarkets(data) {
  const rows = data.marketBreakdown ?? [];
  if (!rows.length) {
    setHTML('markets-body', '<tr><td colspan="6">データがありません</td></tr>');
    return;
  }
  setHTML(
    'markets-body',
    rows
      .map(
        (r, i) => `
      <tr>
        <td>${i + 1}. ${r.label}</td>
        <td class="num">${yen(r.median, 1)}</td>
        <td class="num">${yen(r.mean, 1)}</td>
        <td class="num down">${yen(r.min, 1)}</td>
        <td class="num up">${yen(r.max, 1)}</td>
        <td class="num">${r.sessions}</td>
      </tr>`
      )
      .join('')
  );
}

/* ---------------- 小売価格 ---------------- */

function renderRetail(data) {
  const series = data.retail ?? [];
  if (!series.length) {
    setHTML('retail-body', '<tr><td colspan="4">データがありません</td></tr>');
    return;
  }

  const recent = series.slice(-18).reverse();
  setHTML(
    'retail-body',
    recent
      .map((entry, i) => {
        const prev = recent[i + 1];
        const change = prev ? ((entry.price - prev.price) / prev.price) * 100 : null;
        return `
        <tr>
          <td class="num">${shortDate(entry.date)}</td>
          <td class="num">${yen(entry.price)}</td>
          <td class="num ${pctClass(change)}">${change == null ? '—' : `${signed(change, 1)}%`}</td>
          <td class="num">${yen(perPiece(entry.price), 1)}</td>
        </tr>`;
      })
      .join('')
  );

  const latest = series[series.length - 1];
  const wholesale = data.summary.price;
  const markup = wholesale ? ((latest.price / wholesale - 1) * 100).toFixed(0) : null;
  setText(
    'retail-note',
    `農林水産省 食品価格動向調査（野菜）の全国平均小売価格。最新は ${shortDate(
      latest.date
    )} の ${yen(latest.price)} 円/kg で、卸売価格 ${yen(
      wholesale,
      1
    )} 円/kg に対して約 ${markup}% 高い。差は流通・小分け・小売のコストにあたる。`
  );
}

/* ---------------- 「食いたい」判定 ---------------- */

function renderVerdict(data) {
  const s = data.summary;
  const price = s.price;
  const yearMean = s.yearRange?.mean;
  const month = Number(s.asOf.slice(5, 7));
  const seasonal = (data.seasonality ?? []).find((x) => x.month === month);

  const reasons = [];
  let score = 50;

  if (yearMean) {
    const gap = ((price - yearMean) / yearMean) * 100;
    score -= gap * 1.2;
    reasons.push(
      `直近1年の平均 ${yen(yearMean, 1)} 円/kg に対して ${signed(gap, 1)}%（${
        gap < 0 ? '平均より安い' : '平均より高い'
      }）。`
    );
  }

  const weekRef = s.reference?.['1w'];
  if (weekRef?.changePct != null) {
    score -= weekRef.changePct * 0.5;
    reasons.push(
      `1週間前（${shortDate(weekRef.date)} ${yen(weekRef.close, 1)} 円）比 ${signed(
        weekRef.changePct,
        1
      )}%。`
    );
  }

  if (seasonal) {
    const gap = ((price - seasonal.mean) / seasonal.mean) * 100;
    score -= gap * 0.8;
    reasons.push(
      `${MONTH_NAMES[month - 1]}の平年平均 ${yen(seasonal.mean, 1)} 円/kg に対して ${signed(
        gap,
        1
      )}%。`
    );
  }

  if (s.allTime?.low) {
    const gap = ((price - s.allTime.low.price) / s.allTime.low.price) * 100;
    reasons.push(
      `過去最安値 ${yen(s.allTime.low.price, 1)} 円/kg（${shortDate(
        s.allTime.low.date
      )}）からは +${gap.toFixed(0)}%。`
    );
  }

  score = Math.max(0, Math.min(100, Math.round(score)));

  const verdicts = [
    [72, '買い時。胡瓜を食え', 'down'],
    [55, 'やや安い。食っていい', 'down'],
    [42, '平常値。好きにしろ', 'flat'],
    [25, 'やや高い。数日待て', 'up'],
    [0, '高値圏。今日は諦めろ', 'up'],
  ];
  const [, title, cls] = verdicts.find(([threshold]) => score >= threshold);

  setHTML('verdict-title', `<span class="${cls}">${title}</span>`);
  setText('verdict-score', `割安度スコア ${score} / 100（100 に近いほど安い）`);
  setHTML('verdict-reasons', reasons.map((r) => `<li>${r}</li>`).join(''));
}

/* ---------------- 出典・状態 ---------------- */

function renderMeta(data) {
  const meta = data.meta ?? {};
  setHTML(
    'source-list',
    (meta.sources ?? [])
      .map(
        (src) => `
      <li>
        <a href="${src.url}" target="_blank" rel="noopener">${src.name}</a>
        （${src.publisher} / ${src.license}）${src.note ? `<br>${src.note}` : ''}
      </li>`
      )
      .join('')
  );

  const coverage = meta.coverage ?? {};
  const parts = [];
  if (coverage.daily) {
    parts.push(
      `卸売 ${coverage.daily.from} 〜 ${coverage.daily.to}（${coverage.daily.sessions} 営業日）`
    );
  }
  if (coverage.retail) {
    parts.push(
      `小売 ${coverage.retail.from} 〜 ${coverage.retail.to}（${coverage.retail.points} 週）`
    );
  }
  if (meta.generatedAt) parts.push(`生成 ${meta.generatedAt}`);
  setText('status-line', parts.join(' / '));
  setText('header-asof', `${data.summary.asOf}　${yen(data.summary.price, 1)} 円/kg`);
}

/* ---------------- 操作系 ---------------- */

function buildRangeButtons() {
  const container = el('range-buttons');
  if (!container) return;

  container.innerHTML = RANGE_DEFS.map(
    (def) =>
      `<button type="button" data-range="${def.key}" aria-pressed="${
        def.key === state.range
      }">${def.label}</button>`
  ).join('');

  container.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-range]');
    if (!button) return;
    state.range = button.dataset.range;
    for (const node of container.querySelectorAll('button')) {
      node.setAttribute('aria-pressed', String(node === button));
    }
    renderChart();
  });
}

function bindModeButtons() {
  const container = el('mode-buttons');
  if (!container) return;
  container.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-mode]');
    if (!button) return;
    state.mode = button.dataset.mode;
    for (const node of container.querySelectorAll('button')) {
      node.setAttribute('aria-pressed', String(node === button));
    }
    renderChart();
  });
}

function bindToggles() {
  const map = {
    'toggle-ma': 'showMA',
    'toggle-volume': 'showVolume',
    'toggle-retail': 'showRetail',
  };
  for (const [id, key] of Object.entries(map)) {
    const node = el(id);
    if (!node) continue;
    node.checked = state[key];
    node.addEventListener('change', () => {
      state[key] = node.checked;
      renderChart();
    });
  }
}

/* ---------------- 起動 ---------------- */

async function main() {
  const banner = el('error-banner');
  try {
    const data = await loadData();
    state.data = data;

    renderTicker(data);
    renderQuote(data);
    renderVerdict(data);
    renderWindows(data);
    renderYearly(data);
    renderSeasonality(data);
    renderMarkets(data);
    renderRetail(data);
    renderMeta(data);

    state.chart = new PriceChart(el('chart'), el('chart-readout'));
    buildRangeButtons();
    bindModeButtons();
    bindToggles();
    renderChart();

    if (banner) banner.hidden = true;
  } catch (error) {
    console.error(error);
    if (banner) {
      banner.hidden = false;
      banner.textContent = `データの読み込みに失敗しました（${error.message}）。時間をおいて再読み込みしてください。`;
    }
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', main);
} else {
  main();
}
