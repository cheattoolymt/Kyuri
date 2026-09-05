/*
 * 相場チャート描画エンジン（依存ライブラリなし・Canvas 2D）
 *
 * ローソク足 / ラインの切り替え、移動平均、出来高サブチャート、
 * クロスヘアと読み取り表示に対応する。
 */

const PALETTE = {
  bg: '#131a1f',
  grid: '#1f2a31',
  gridStrong: '#26323b',
  axis: '#647684',
  text: '#90a4ae',
  textStrong: '#e6edf1',
  up: '#ef5350',
  down: '#26a69a',
  doji: '#8e9ba5',
  line: '#8bc34a',
  lineFill: 'rgba(139, 195, 74, 0.12)',
  ma25: '#42a5f5',
  ma75: '#ab47bc',
  volume: 'rgba(96, 125, 139, 0.55)',
  crosshair: 'rgba(230, 237, 241, 0.35)',
  retail: '#ffb74d',
};

const PADDING = { top: 16, right: 68, bottom: 26, left: 6 };
const VOLUME_RATIO = 0.2;

/** 価格軸の目盛りを人間が読みやすい間隔で刻む。 */
function niceTicks(min, max, count = 6) {
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) {
    return [min];
  }
  const rawStep = (max - min) / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const normalized = rawStep / magnitude;
  let step;
  if (normalized <= 1) step = 1;
  else if (normalized <= 2) step = 2;
  else if (normalized <= 2.5) step = 2.5;
  else if (normalized <= 5) step = 5;
  else step = 10;
  step *= magnitude;

  const ticks = [];
  const start = Math.ceil(min / step) * step;
  for (let value = start; value <= max + step * 0.001; value += step) {
    ticks.push(Math.round(value * 100) / 100);
  }
  return ticks;
}

/** 単純移動平均。データが足りない区間は null を返す。 */
function movingAverage(values, window) {
  const out = new Array(values.length).fill(null);
  let sum = 0;
  let filled = 0;
  for (let i = 0; i < values.length; i += 1) {
    const value = values[i];
    if (value == null) {
      continue;
    }
    sum += value;
    filled += 1;
    if (filled > window) {
      // 単純化のため window 個前を引く（null 混在時は近似）
      const drop = values[i - window];
      if (drop != null) {
        sum -= drop;
        filled -= 1;
      }
    }
    if (filled >= window) {
      out[i] = sum / filled;
    }
  }
  return out;
}

export class PriceChart {
  constructor(canvas, readout) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.readout = readout;
    this.data = [];
    this.overlay = [];
    this.mode = 'candle';
    this.showMA = true;
    this.showVolume = true;
    this.showOverlay = false;
    this.hoverIndex = null;
    this.layout = null;

    this._onResize = this._onResize.bind(this);
    this._onMove = this._onMove.bind(this);
    this._onLeave = this._onLeave.bind(this);

    window.addEventListener('resize', this._onResize);
    canvas.addEventListener('mousemove', this._onMove);
    canvas.addEventListener('mouseleave', this._onLeave);
    canvas.addEventListener('touchstart', this._onMove, { passive: true });
    canvas.addEventListener('touchmove', this._onMove, { passive: true });
  }

  /**
   * @param {Array<{date:string,open:number,high:number,low:number,close:number,volumeKg?:number}>} data
   * @param {Array<{date:string,price:number}>} overlay 小売価格など副系列
   */
  setData(data, overlay = []) {
    this.data = Array.isArray(data) ? data : [];
    this.overlay = Array.isArray(overlay) ? overlay : [];
    this.hoverIndex = null;
    this.render();
  }

  setOptions(options = {}) {
    if (options.mode) this.mode = options.mode;
    if (typeof options.showMA === 'boolean') this.showMA = options.showMA;
    if (typeof options.showVolume === 'boolean') this.showVolume = options.showVolume;
    if (typeof options.showOverlay === 'boolean') this.showOverlay = options.showOverlay;
    this.render();
  }

  destroy() {
    window.removeEventListener('resize', this._onResize);
    this.canvas.removeEventListener('mousemove', this._onMove);
    this.canvas.removeEventListener('mouseleave', this._onLeave);
  }

  _onResize() {
    if (this._resizeTimer) clearTimeout(this._resizeTimer);
    this._resizeTimer = setTimeout(() => this.render(), 90);
  }

  _onMove(event) {
    if (!this.layout || !this.data.length) return;
    const rect = this.canvas.getBoundingClientRect();
    const point = event.touches ? event.touches[0] : event;
    const x = point.clientX - rect.left;
    const { plotLeft, slotWidth } = this.layout;
    const index = Math.round((x - plotLeft - slotWidth / 2) / slotWidth);
    const clamped = Math.max(0, Math.min(this.data.length - 1, index));
    if (clamped !== this.hoverIndex) {
      this.hoverIndex = clamped;
      this.render();
    }
  }

  _onLeave() {
    this.hoverIndex = null;
    this.render();
  }

  _updateReadout(index) {
    if (!this.readout) return;
    const point = this.data[index] ?? this.data[this.data.length - 1];
    if (!point) {
      this.readout.textContent = '';
      return;
    }
    const change = point.close - point.open;
    const cls = change > 0 ? 'up' : change < 0 ? 'down' : 'flat';
    const sign = change > 0 ? '+' : '';
    const parts = [
      `<span class="ro-date">${point.date}</span>`,
      `始 ${point.open?.toFixed(1) ?? '-'}`,
      `高 ${point.high?.toFixed(1) ?? '-'}`,
      `安 ${point.low?.toFixed(1) ?? '-'}`,
      `終 ${point.close?.toFixed(1) ?? '-'}`,
      `<span class="${cls}">${sign}${change.toFixed(1)} 円</span>`,
    ];
    if (point.volumeKg) {
      parts.push(`数量 ${Math.round(point.volumeKg).toLocaleString('ja-JP')} kg`);
    }
    if (point.samples) {
      parts.push(`建値 ${point.samples} 件`);
    }
    this.readout.innerHTML = parts.join('&nbsp;&nbsp;');
  }

  render() {
    const { canvas, ctx, data } = this;
    const cssWidth = canvas.clientWidth || canvas.parentElement?.clientWidth || 800;
    const cssHeight = canvas.clientHeight || 400;
    const dpr = window.devicePixelRatio || 1;

    canvas.width = Math.round(cssWidth * dpr);
    canvas.height = Math.round(cssHeight * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);
    ctx.fillStyle = PALETTE.bg;
    ctx.fillRect(0, 0, cssWidth, cssHeight);

    if (!data.length) {
      ctx.fillStyle = PALETTE.text;
      ctx.font = '13px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('この期間のデータがありません', cssWidth / 2, cssHeight / 2);
      this.layout = null;
      return;
    }

    const useVolume = this.showVolume && data.some((d) => d.volumeKg);
    const plotLeft = PADDING.left;
    const plotRight = cssWidth - PADDING.right;
    const plotWidth = Math.max(10, plotRight - plotLeft);
    const totalHeight = cssHeight - PADDING.top - PADDING.bottom;
    const volumeHeight = useVolume ? totalHeight * VOLUME_RATIO : 0;
    const priceHeight = totalHeight - volumeHeight - (useVolume ? 8 : 0);
    const plotTop = PADDING.top;
    const priceBottom = plotTop + priceHeight;
    const volumeTop = priceBottom + 8;
    const volumeBottom = volumeTop + volumeHeight;

    const closes = data.map((d) => d.close);
    const highs = data.map((d) => (this.mode === 'candle' ? d.high : d.close));
    const lows = data.map((d) => (this.mode === 'candle' ? d.low : d.close));

    const ma25 = this.showMA ? movingAverage(closes, 25) : [];
    const ma75 = this.showMA ? movingAverage(closes, 75) : [];

    let overlayPoints = [];
    if (this.showOverlay && this.overlay.length) {
      const firstDate = data[0].date;
      const lastDate = data[data.length - 1].date;
      const inRange = this.overlay.filter((p) => p.date >= firstDate && p.date <= lastDate);

      // 副系列（小売価格・週次）は主系列と粒度が異なるため、
      // 主系列の各足に対して、その足の期間に入る副系列の平均を割り当てる。
      // これにより月足でも階段状にならず滑らかな比較線になる。
      const buckets = data.map(() => []);
      let cursor = 0;
      for (const point of inRange) {
        while (cursor < data.length - 1 && data[cursor].date < point.date) {
          cursor += 1;
        }
        buckets[cursor].push(point.price);
      }
      overlayPoints = buckets
        .map((prices, index) =>
          prices.length
            ? { index, price: prices.reduce((a, b) => a + b, 0) / prices.length }
            : null
        )
        .filter(Boolean);
    }

    let min = Math.min(...lows);
    let max = Math.max(...highs);
    if (this.showMA) {
      for (const series of [ma25, ma75]) {
        for (const v of series) {
          if (v == null) continue;
          min = Math.min(min, v);
          max = Math.max(max, v);
        }
      }
    }
    for (const p of overlayPoints) {
      min = Math.min(min, p.price);
      max = Math.max(max, p.price);
    }

    const span = max - min || Math.max(1, max * 0.1);
    min -= span * 0.06;
    max += span * 0.06;
    min = Math.max(0, min);

    const slotWidth = plotWidth / data.length;
    const priceToY = (price) => priceBottom - ((price - min) / (max - min)) * priceHeight;
    const indexToX = (index) => plotLeft + index * slotWidth + slotWidth / 2;

    this.layout = { plotLeft, plotRight, slotWidth, priceToY, indexToX };

    // --- グリッドと価格軸 ---
    const ticks = niceTicks(min, max, 6);
    ctx.font = '11px "SFMono-Regular", "DejaVu Sans Mono", monospace';
    ctx.textBaseline = 'middle';
    for (const tick of ticks) {
      const y = priceToY(tick);
      if (y < plotTop - 1 || y > priceBottom + 1) continue;
      ctx.strokeStyle = PALETTE.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(plotLeft, Math.round(y) + 0.5);
      ctx.lineTo(plotRight, Math.round(y) + 0.5);
      ctx.stroke();
      ctx.fillStyle = PALETTE.text;
      ctx.textAlign = 'left';
      ctx.fillText(tick.toLocaleString('ja-JP'), plotRight + 8, y);
    }

    // --- 時間軸ラベル ---
    const labelCount = Math.max(2, Math.min(8, Math.floor(plotWidth / 90)));
    const labelStep = Math.max(1, Math.floor(data.length / labelCount));
    const axisBottom = useVolume ? volumeBottom : priceBottom;
    ctx.textBaseline = 'top';
    for (let i = 0; i < data.length; i += labelStep) {
      const x = indexToX(i);
      ctx.strokeStyle = PALETTE.grid;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, plotTop);
      ctx.lineTo(Math.round(x) + 0.5, axisBottom);
      ctx.stroke();

      const label = data[i].date.slice(2).replace(/-/g, '/');
      const halfWidth = ctx.measureText(label).width / 2;
      // 端のラベルが枠外にはみ出して切れないよう、寄せ方を切り替える
      ctx.textAlign = x - halfWidth < plotLeft ? 'left' : 'center';
      ctx.fillStyle = PALETTE.text;
      ctx.fillText(label, ctx.textAlign === 'left' ? plotLeft : x, axisBottom + 6);
    }
    ctx.textAlign = 'center';

    // --- 出来高 ---
    if (useVolume) {
      const maxVolume = Math.max(...data.map((d) => d.volumeKg || 0)) || 1;
      const barWidth = Math.max(1, slotWidth * 0.68);
      for (let i = 0; i < data.length; i += 1) {
        const volume = data[i].volumeKg;
        if (!volume) continue;
        const height = (volume / maxVolume) * volumeHeight;
        const x = indexToX(i) - barWidth / 2;
        const rising = data[i].close >= data[i].open;
        ctx.fillStyle = rising
          ? 'rgba(239, 83, 80, 0.38)'
          : 'rgba(38, 166, 154, 0.38)';
        ctx.fillRect(x, volumeBottom - height, barWidth, height);
      }
      ctx.strokeStyle = PALETTE.gridStrong;
      ctx.beginPath();
      ctx.moveTo(plotLeft, Math.round(volumeBottom) + 0.5);
      ctx.lineTo(plotRight, Math.round(volumeBottom) + 0.5);
      ctx.stroke();

      ctx.fillStyle = PALETTE.axis;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      ctx.fillText('卸売数量 (kg)', plotLeft + 4, volumeTop + 2);
    }

    // --- 本体 ---
    if (this.mode === 'candle') {
      const bodyWidth = Math.max(1, Math.min(11, slotWidth * 0.66));
      for (let i = 0; i < data.length; i += 1) {
        const d = data[i];
        const x = indexToX(i);
        const px = Math.round(x) + 0.5;
        // 始値は前営業日の終値なので、前日と同値の日は実体を持たない。
        // その場合は同値足（ドージ）として横線で示す。
        const flat = d.close === d.open;
        const rising = d.close > d.open;
        const color = flat ? PALETTE.doji : rising ? PALETTE.up : PALETTE.down;
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 1;

        ctx.beginPath();
        ctx.moveTo(px, priceToY(d.high));
        ctx.lineTo(px, priceToY(d.low));
        ctx.stroke();

        const yOpen = priceToY(d.open);
        const yClose = priceToY(d.close);

        if (flat) {
          const y = Math.round(yClose) + 0.5;
          ctx.beginPath();
          ctx.moveTo(x - bodyWidth / 2, y);
          ctx.lineTo(x + bodyWidth / 2, y);
          ctx.lineWidth = 1.6;
          ctx.stroke();
          continue;
        }

        const top = Math.min(yOpen, yClose);
        const height = Math.max(1, Math.abs(yClose - yOpen));
        if (bodyWidth <= 1.5) {
          ctx.fillRect(Math.round(x), top, 1, height);
        } else {
          ctx.fillRect(x - bodyWidth / 2, top, bodyWidth, height);
        }
      }
    } else {
      ctx.beginPath();
      for (let i = 0; i < data.length; i += 1) {
        const x = indexToX(i);
        const y = priceToY(data[i].close);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = PALETTE.line;
      ctx.lineWidth = 1.8;
      ctx.lineJoin = 'round';
      ctx.stroke();

      ctx.lineTo(indexToX(data.length - 1), priceBottom);
      ctx.lineTo(indexToX(0), priceBottom);
      ctx.closePath();
      ctx.fillStyle = PALETTE.lineFill;
      ctx.fill();
    }

    // --- 移動平均 ---
    const drawSeries = (series, color) => {
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < series.length; i += 1) {
        const value = series[i];
        if (value == null) {
          started = false;
          continue;
        }
        const x = indexToX(i);
        const y = priceToY(value);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.3;
      ctx.stroke();
    };

    if (this.showMA) {
      if (data.length > 25) drawSeries(ma25, PALETTE.ma25);
      if (data.length > 75) drawSeries(ma75, PALETTE.ma75);
    }

    if (overlayPoints.length > 1) {
      ctx.beginPath();
      overlayPoints.forEach((p, i) => {
        const x = indexToX(p.index);
        const y = priceToY(p.price);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = PALETTE.retail;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // --- 凡例 ---
    const legend = [];
    if (this.showMA && data.length > 25) legend.push(['25日移動平均', PALETTE.ma25]);
    if (this.showMA && data.length > 75) legend.push(['75日移動平均', PALETTE.ma75]);
    if (overlayPoints.length > 1) legend.push(['小売価格（全国平均）', PALETTE.retail]);
    if (legend.length) {
      let lx = plotLeft + 6;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      for (const [label, color] of legend) {
        ctx.fillStyle = color;
        ctx.fillRect(lx, plotTop + 5, 14, 2);
        ctx.fillStyle = PALETTE.text;
        ctx.fillText(label, lx + 19, plotTop);
        lx += ctx.measureText(label).width + 42;
      }
    }

    // --- クロスヘア ---
    const hoverIndex = this.hoverIndex;
    if (hoverIndex != null && data[hoverIndex]) {
      const x = indexToX(hoverIndex);
      const y = priceToY(data[hoverIndex].close);
      ctx.strokeStyle = PALETTE.crosshair;
      ctx.setLineDash([3, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, plotTop);
      ctx.lineTo(Math.round(x) + 0.5, useVolume ? volumeBottom : priceBottom);
      ctx.moveTo(plotLeft, Math.round(y) + 0.5);
      ctx.lineTo(plotRight, Math.round(y) + 0.5);
      ctx.stroke();
      ctx.setLineDash([]);

      const label = data[hoverIndex].close.toFixed(1);
      ctx.font = '11px "SFMono-Regular", "DejaVu Sans Mono", monospace';
      const width = ctx.measureText(label).width + 12;
      ctx.fillStyle = '#263238';
      ctx.fillRect(plotRight + 4, y - 9, width, 18);
      ctx.fillStyle = PALETTE.textStrong;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillText(label, plotRight + 10, y);
    }

    this._updateReadout(hoverIndex ?? data.length - 1);
  }
}
