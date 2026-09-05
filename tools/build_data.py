"""胡瓜価格インデックスのデータ構築スクリプト.

東京都中央卸売市場の日報（青果・販売結果価格／数量）と農林水産省の
食品価格動向調査（小売）を取得し、静的サイトが読む JSON 群を生成する。

使い方
------
    python3 tools/build_data.py                # 全年度を取得して再構築
    python3 tools/build_data.py --incremental  # 直近年度のみ再取得（日次更新用）
    python3 tools/build_data.py --offline      # キャッシュのみ使用（ネット不可時）

設計方針
--------
* 生の CSV は cache/ に年度単位で保存し、再取得を避ける。
  これにより日次更新時のネットワーク負荷を最小化し、外部障害時にも
  直前の状態でビルドを完走できる（安定性重視）。
* 生成物はすべて data/ 配下および api/v1/ 配下に出力する。
* 価格は「1kg あたりの円」に正規化する。日報の価格は単位（kg/箱）
  当たりの金額なので、単位で除算する。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sources  # noqa: E402

LOG = logging.getLogger("build")

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"
DATA_DIR = ROOT / "data"
API_DIR = ROOT / "api" / "v1"

ITEM_NAME = "きゅうり"
JST = timezone(timedelta(hours=9))

# 卸売価格の外れ値除去に用いる 1kg 単価の許容範囲（円）。
# 実データの分布では 1 パーセンタイルが約 86 円、99.9 パーセンタイルが
# 約 1,296 円。一方で単位表記の誤り由来と思われる極端値（数万円）が
# ごく少数混じるため、両側を機械的に切り落とす。
PRICE_FLOOR = 80.0
PRICE_CEIL = 3000.0

MARKET_LABELS = {
    "豊洲": "豊洲市場",
    "大田": "大田市場",
    "豊島": "豊島市場",
    "板橋": "板橋市場",
    "葛西": "葛西市場",
    "北足立": "北足立市場",
    "多摩NT": "多摩ニュータウン市場",
    "世田谷": "世田谷市場",
    "淀橋": "淀橋市場",
    "食肉": "食肉市場",
}


# ---------------------------------------------------------------------------
# 小さなユーティリティ
# ---------------------------------------------------------------------------


def parse_number(raw: str | None) -> float | None:
    """CSV 中の数値文字列を float に変換する。空欄・記号は None。"""
    if raw is None:
        return None
    text = raw.strip().replace(",", "")
    if not text or text in {"-", "−", "―", "－", "\u2015"}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def round2(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def iso_week_key(d: date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def fiscal_year_of(d: date) -> int:
    """日本の年度（4月始まり）。"""
    return d.year if d.month >= 4 else d.year - 1


def write_json(path: Path, payload: Any, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":") if compact else (",", ": "),
        indent=None if compact else 2,
    )
    path.write_text(text + "\n", encoding="utf-8")
    LOG.info("書き出し %s (%.1f KB)", path.relative_to(ROOT), len(text) / 1024)


# ---------------------------------------------------------------------------
# 卸売価格の集計
# ---------------------------------------------------------------------------


class DailyBucket:
    """1 日分の卸売相場の観測値をためる入れ物。"""

    __slots__ = ("highs", "mids", "lows", "per_market", "auction")

    def __init__(self) -> None:
        self.highs: list[float] = []
        self.mids: list[float] = []
        self.lows: list[float] = []
        self.per_market: dict[str, list[float]] = defaultdict(list)
        self.auction: list[float] = []


def collect_price_rows(rows: Iterable[dict[str, str]]) -> dict[str, DailyBucket]:
    """日報の価格行を日付ごとに集計する。

    価格は単位（1 箱あたりの kg 数）で割って 1kg 単価に正規化する。
    「せり・入札」は小口・端物の建値が混ざり相対取引と水準が乖離するため、
    代表値には相対・商物分離・第三者販売を用い、せり値は別枠に保持する。
    """
    buckets: dict[str, DailyBucket] = defaultdict(DailyBucket)

    for row in rows:
        day = (row.get("日付") or "").strip()
        if len(day) != 10:
            continue

        unit = parse_number(row.get("単位"))
        if not unit or unit <= 0:
            continue

        market = (row.get("市場") or "").strip()
        method = (row.get("販売方法") or "").strip()
        bucket = buckets[day]

        values: dict[str, float] = {}
        for column, key in (("高値(円)", "high"), ("中値(円)", "mid"), ("安値(円)", "low")):
            raw = parse_number(row.get(column))
            if raw is None:
                continue
            per_kg = raw / unit
            if not (PRICE_FLOOR <= per_kg <= PRICE_CEIL):
                continue
            values[key] = per_kg

        if not values:
            continue

        if method == "せり・入札":
            bucket.auction.extend(values.values())
            continue

        if "high" in values:
            bucket.highs.append(values["high"])
        if "mid" in values:
            bucket.mids.append(values["mid"])
        if "low" in values:
            bucket.lows.append(values["low"])

        if market:
            center = values.get("mid")
            if center is None:
                center = statistics.mean(values.values())
            bucket.per_market[market].append(center)

    return buckets


def build_daily_series(
    buckets: dict[str, DailyBucket], volumes: dict[str, float]
) -> list[dict[str, Any]]:
    """日次の四本値・代表値・市場別内訳を組み立てる。

    日報は 1 日分の集計値であり時刻情報を持たないため、四本値は
    「その日の建値分布」を表すものとして次のように定義する。

      終値 = 代表建値（中値）の中央値。指数としての現在値。
      始値 = 前営業日の終値。連続した系列として扱う。
      高値 = 高値建値の中央値。その日の上側の代表水準。
      安値 = 安値建値の中央値。その日の下側の代表水準。

    中央値を用いるのは、市場・産地ごとに 20 件前後ある建値の中に
    端物・小口由来の飛び値が混じるためで、単純な最大／最小では
    値幅が実勢から乖離する。分布の両端は intradayHigh / intradayLow
    として別途保持する。
    """
    series: list[dict[str, Any]] = []
    previous_close: float | None = None

    for day in sorted(buckets):
        bucket = buckets[day]
        pool = bucket.mids or bucket.lows or bucket.highs or bucket.auction
        if not pool:
            continue

        close_value = statistics.median(pool)
        open_value = previous_close if previous_close is not None else close_value

        high = statistics.median(bucket.highs) if bucket.highs else close_value
        low = statistics.median(bucket.lows) if bucket.lows else close_value

        high = max(high, open_value, close_value)
        low = min(low, open_value, close_value)

        observed = bucket.highs + bucket.mids + bucket.lows or pool

        markets = {
            market: round2(statistics.median(vals))
            for market, vals in sorted(bucket.per_market.items())
            if vals
        }

        entry: dict[str, Any] = {
            "date": day,
            "open": round2(open_value),
            "high": round2(high),
            "low": round2(low),
            "close": round2(close_value),
            "mean": round2(statistics.mean(pool)),
            "intradayHigh": round2(max(observed)),
            "intradayLow": round2(min(observed)),
            "samples": len(pool),
            "markets": markets,
        }
        if bucket.auction:
            entry["auctionMedian"] = round2(statistics.median(bucket.auction))
        volume = volumes.get(day)
        if volume:
            entry["volumeKg"] = round(volume)

        series.append(entry)
        previous_close = close_value

    return series


def collect_quantity_rows(rows: Iterable[dict[str, str]]) -> dict[str, float]:
    """日別の卸売数量（kg）を合計する。

    「第三者販売」と「商物分離」は相対・せりと一部重複するため、
    合計には含めない（東京都の注記に従う）。
    """
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        day = (row.get("日付") or "").strip()
        if len(day) != 10:
            continue
        method = (row.get("販売方法") or "").strip()
        if method in {"第三者販売", "商物分離"}:
            continue
        qty = parse_number(row.get("卸売数量(kg)"))
        if qty and qty > 0:
            totals[day] += qty
    return dict(totals)


# ---------------------------------------------------------------------------
# 統計量の算出
# ---------------------------------------------------------------------------


def pct_change(current: float, previous: float | None) -> float | None:
    if previous is None or previous == 0:
        return None
    return round((current - previous) / previous * 100, 2)


def value_on_or_before(series: list[dict[str, Any]], target: date) -> dict[str, Any] | None:
    """指定日以前で最も近い営業日のレコードを返す。"""
    chosen = None
    for entry in series:
        if date.fromisoformat(entry["date"]) <= target:
            chosen = entry
        else:
            break
    return chosen


def window_stats(series: list[dict[str, Any]], since: date) -> dict[str, Any] | None:
    """指定日以降の区間統計。高値・安値は終値ベースで判定する。

    高値・安値に日内レンジ（high/low）を使うと単発の飛び値に引っ張られる
    ため、期間の代表的な高値・安値としては終値の最大／最小を採る。
    """
    subset = [e for e in series if date.fromisoformat(e["date"]) >= since]
    if not subset:
        return None
    closes = [e["close"] for e in subset]
    highest = max(subset, key=lambda e: e["close"])
    lowest = min(subset, key=lambda e: e["close"])
    return {
        "from": subset[0]["date"],
        "to": subset[-1]["date"],
        "sessions": len(subset),
        "open": subset[0]["open"],
        "close": subset[-1]["close"],
        "mean": round2(statistics.mean(closes)),
        "median": round2(statistics.median(closes)),
        "high": {"date": highest["date"], "price": highest["close"]},
        "low": {"date": lowest["date"], "price": lowest["close"]},
        "intradayHigh": max(e["high"] for e in subset),
        "intradayLow": min(e["low"] for e in subset),
        "changePct": pct_change(subset[-1]["close"], subset[0]["open"]),
    }


def build_summary(series: list[dict[str, Any]]) -> dict[str, Any]:
    """株価アプリ風のサマリー（現在値・変化率・各期間の最安値など）。"""
    latest = series[-1]
    latest_date = date.fromisoformat(latest["date"])
    previous = series[-2] if len(series) >= 2 else None

    all_time_low = min(series, key=lambda e: e["close"])
    all_time_high = max(series, key=lambda e: e["close"])

    windows: dict[str, Any] = {}
    for key, days in (
        ("1w", 7),
        ("1m", 30),
        ("3m", 91),
        ("6m", 182),
        ("1y", 365),
        ("3y", 365 * 3),
        ("5y", 365 * 5),
    ):
        stats = window_stats(series, latest_date - timedelta(days=days))
        # 収録期間より長い窓は重複するので、直前の窓と同一なら採用しない
        if stats and stats["sessions"] >= 2:
            windows[key] = stats

    reference: dict[str, Any] = {}
    for key, days in (("1d", 1), ("1w", 7), ("1m", 30), ("1y", 365)):
        past = value_on_or_before(series, latest_date - timedelta(days=days))
        if past and past["date"] != latest["date"]:
            reference[key] = {
                "date": past["date"],
                "close": past["close"],
                "changePct": pct_change(latest["close"], past["close"]),
                "changeAbs": round2(latest["close"] - past["close"]),
            }

    year_series = [
        e for e in series if date.fromisoformat(e["date"]) >= latest_date - timedelta(days=365)
    ]
    closes_1y = [e["close"] for e in year_series]

    return {
        "asOf": latest["date"],
        "price": latest["close"],
        "median": latest["close"],
        "open": latest["open"],
        "dayHigh": latest["high"],
        "dayLow": latest["low"],
        "samples": latest["samples"],
        "volumeKg": latest.get("volumeKg"),
        "previousClose": previous["close"] if previous else None,
        "change": round2(latest["close"] - previous["close"]) if previous else None,
        "changePct": pct_change(latest["close"], previous["close"]) if previous else None,
        "reference": reference,
        "windows": windows,
        "yearRange": {
            "low": min(closes_1y) if closes_1y else None,
            "high": max(closes_1y) if closes_1y else None,
            "mean": round2(statistics.mean(closes_1y)) if closes_1y else None,
        },
        "allTime": {
            "low": {"date": all_time_low["date"], "price": all_time_low["close"]},
            "high": {"date": all_time_high["date"], "price": all_time_high["close"]},
            "from": series[0]["date"],
            "to": series[-1]["date"],
            "sessions": len(series),
        },
        "markets": latest.get("markets", {}),
    }


def aggregate_period(series: list[dict[str, Any]], keyfunc) -> list[dict[str, Any]]:
    """日次系列を週次・月次・年次に畳み込む（四本値を維持）。"""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in series:
        groups[keyfunc(date.fromisoformat(entry["date"]))].append(entry)

    out: list[dict[str, Any]] = []
    for key in sorted(groups):
        chunk = groups[key]
        closes = [e["close"] for e in chunk]
        volume = sum(e.get("volumeKg") or 0 for e in chunk)
        out.append(
            {
                "period": key,
                "from": chunk[0]["date"],
                "to": chunk[-1]["date"],
                "open": chunk[0]["open"],
                "high": max(e["high"] for e in chunk),
                "low": min(e["low"] for e in chunk),
                "close": chunk[-1]["close"],
                "mean": round2(statistics.mean(closes)),
                "sessions": len(chunk),
                "volumeKg": round(volume) if volume else None,
            }
        )
    return out


def build_seasonality(series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """月別の平年値（全期間平均）。旬の目安として使う。"""
    groups: dict[int, list[float]] = defaultdict(list)
    for entry in series:
        groups[date.fromisoformat(entry["date"]).month].append(entry["close"])

    return [
        {
            "month": month,
            "mean": round2(statistics.mean(groups[month])),
            "median": round2(statistics.median(groups[month])),
            "min": min(groups[month]),
            "max": max(groups[month]),
            "samples": len(groups[month]),
        }
        for month in range(1, 13)
        if groups.get(month)
    ]


def build_market_breakdown(series: list[dict[str, Any]], days: int = 90) -> list[dict[str, Any]]:
    """直近 N 日の市場別平均。どこが安いかを見るための表。"""
    if not series:
        return []
    since = date.fromisoformat(series[-1]["date"]) - timedelta(days=days)
    pools: dict[str, list[float]] = defaultdict(list)
    for entry in series:
        if date.fromisoformat(entry["date"]) < since:
            continue
        for market, price in (entry.get("markets") or {}).items():
            if price:
                pools[market].append(price)

    rows = [
        {
            "market": market,
            "label": MARKET_LABELS.get(market, market),
            "mean": round2(statistics.mean(prices)),
            "median": round2(statistics.median(prices)),
            "min": min(prices),
            "max": max(prices),
            "sessions": len(prices),
        }
        for market, prices in pools.items()
        if len(prices) >= 5
    ]
    rows.sort(key=lambda r: r["median"])
    return rows


# ---------------------------------------------------------------------------
# 小売価格
# ---------------------------------------------------------------------------


def build_retail_series(payload: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """食品価格動向調査（野菜）の全国平均小売価格からきゅうりを抽出。"""
    out: list[dict[str, Any]] = []
    for raw_date, values in payload.items():
        try:
            parts = [int(p) for p in raw_date.split("/")]
            day = date(parts[0], parts[1], parts[2])
        except (ValueError, IndexError):
            continue
        price = parse_number(str(values.get("Kyuuri", "")))
        if price is None:
            continue
        out.append({"date": day.isoformat(), "price": round2(price)})
    out.sort(key=lambda e: e["date"])
    return out


# ---------------------------------------------------------------------------
# キャッシュ付き取得
# ---------------------------------------------------------------------------


def cache_path(name: str) -> Path:
    return CACHE_DIR / name


def load_cached_rows(name: str) -> list[dict[str, str]] | None:
    path = cache_path(name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        LOG.warning("キャッシュ %s が読めません: %s", name, exc)
        return None


def store_cached_rows(name: str, rows: list[dict[str, str]]) -> None:
    path = cache_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def fetch_year_rows(
    resource: sources.WholesaleResource,
    *,
    kind: str,
    force: bool,
    offline: bool,
) -> list[dict[str, str]]:
    """年度・種別ごとの行をキャッシュ経由で取得する。

    失敗時はキャッシュにフォールバックし、ビルド全体を落とさない。
    """
    name = f"{kind}-{resource.fiscal_year}.json"
    cached = load_cached_rows(name)

    if offline or (cached is not None and not force):
        if cached is not None:
            LOG.info("キャッシュ使用 %s (%s 行)", name, len(cached))
            return cached
        if offline:
            LOG.warning("オフライン指定だがキャッシュ %s がありません", name)
            return []

    url = resource.price_csv_url if kind == "price" else resource.quantity_csv_url
    if not url:
        return cached or []

    iterator = (
        sources.iter_wholesale_price_rows
        if kind == "price"
        else sources.iter_wholesale_quantity_rows
    )

    try:
        LOG.info("取得中 %s (%s年度)", kind, resource.fiscal_year)
        rows = list(iterator(url, ITEM_NAME))
    except sources.SourceError as exc:
        LOG.error("%s の取得に失敗: %s", url, exc)
        if cached is not None:
            LOG.warning("キャッシュ %s にフォールバックします", name)
            return cached
        return []

    if not rows:
        LOG.warning("%s から対象行が得られませんでした", url)
        return cached or []

    store_cached_rows(name, rows)
    return rows


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def build(*, incremental: bool, offline: bool) -> dict[str, Any]:
    today = datetime.now(JST).date()
    current_fy = fiscal_year_of(today)

    if offline:
        resources = []
        for path in sorted(CACHE_DIR.glob("price-*.json")):
            year = int(path.stem.split("-")[1])
            resources.append(
                sources.WholesaleResource(
                    dataset_id="cached",
                    fiscal_year=year,
                    title=f"cached {year}",
                    price_csv_url="",
                    quantity_csv_url="",
                    last_modified=None,
                )
            )
        if not resources:
            raise SystemExit("オフラインビルドに必要なキャッシュがありません")
    else:
        resources = sources.discover_wholesale_resources()

    LOG.info(
        "対象年度: %s", ", ".join(str(r.fiscal_year) for r in resources) or "(なし)"
    )

    price_rows: list[dict[str, str]] = []
    quantity_rows: list[dict[str, str]] = []
    source_years: list[dict[str, Any]] = []

    for resource in resources:
        # 増分モードでは進行中の年度（および直前年度）だけ再取得する。
        force = (not incremental) or resource.fiscal_year >= current_fy - 1
        rows = fetch_year_rows(resource, kind="price", force=force, offline=offline)
        price_rows.extend(rows)
        if resource.quantity_csv_url or offline:
            quantity_rows.extend(
                fetch_year_rows(resource, kind="quantity", force=force, offline=offline)
            )
        if rows:
            source_years.append(
                {
                    "fiscalYear": resource.fiscal_year,
                    "datasetId": resource.dataset_id,
                    "rows": len(rows),
                    "lastModified": resource.last_modified,
                }
            )

    if not price_rows:
        raise SystemExit("卸売価格データを 1 件も取得できませんでした")

    LOG.info("卸売価格 %s 行 / 数量 %s 行", len(price_rows), len(quantity_rows))

    buckets = collect_price_rows(price_rows)
    volumes = collect_quantity_rows(quantity_rows)
    daily = build_daily_series(buckets, volumes)
    if not daily:
        raise SystemExit("日次系列を構築できませんでした")

    LOG.info("日次系列 %s 営業日 (%s 〜 %s)", len(daily), daily[0]["date"], daily[-1]["date"])

    retail: list[dict[str, Any]] = []
    try:
        if not offline:
            retail = build_retail_series(sources.fetch_retail_series())
            write_json(CACHE_DIR / "retail.json", retail, compact=True)
        else:
            cached = load_cached_rows("retail.json")
            retail = cached or []
    except sources.SourceError as exc:
        LOG.error("小売価格の取得に失敗: %s", exc)
        cached = load_cached_rows("retail.json")
        retail = cached or []

    summary = build_summary(daily)
    weekly = aggregate_period(daily, iso_week_key)
    monthly = aggregate_period(daily, lambda d: f"{d.year}-{d.month:02d}")
    yearly = aggregate_period(daily, lambda d: str(d.year))
    fiscal = aggregate_period(daily, lambda d: f"FY{fiscal_year_of(d)}")

    generated_at = datetime.now(JST).isoformat(timespec="seconds")

    meta = {
        "generatedAt": generated_at,
        "item": ITEM_NAME,
        "unit": "JPY/kg",
        "priceBasis": "東京都中央卸売市場 卸売価格（相対・商物分離・第三者販売の中値の中央値）",
        "timezone": "Asia/Tokyo",
        "sources": [
            {
                "id": "tokyo-wholesale",
                "name": "東京都中央卸売市場 東京都卸売市場日報（販売結果・価格／数量・青果）",
                "publisher": "東京都中央卸売市場",
                "license": "CC BY 4.0",
                "url": "https://catalog.data.metro.tokyo.lg.jp/dataset/t000013d2000000033",
                "note": "速報値。年度単位の CSV。1kg 単価に正規化して集計。",
                "years": source_years,
            },
            {
                "id": "maff-retail",
                "name": "食品価格動向調査（野菜）小売価格 全国平均",
                "publisher": "農林水産省",
                "license": "政府標準利用規約",
                "url": "https://www.maff.go.jp/j/zyukyu/anpo/kouri/k_yasai/h22index.html",
                "note": "cultivationdata.net の再配布 API 経由で取得（週次）。",
                "points": len(retail),
            },
        ],
        "coverage": {
            "daily": {"from": daily[0]["date"], "to": daily[-1]["date"], "sessions": len(daily)},
            "retail": (
                {"from": retail[0]["date"], "to": retail[-1]["date"], "points": len(retail)}
                if retail
                else None
            ),
        },
    }

    payloads: dict[str, Any] = {
        "meta": meta,
        "summary": summary,
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
        "yearly": yearly,
        "fiscal": fiscal,
        "retail": retail,
        "seasonality": build_seasonality(daily),
        "marketBreakdown": build_market_breakdown(daily),
    }

    return payloads


def emit(payloads: dict[str, Any]) -> None:
    meta = payloads["meta"]
    summary = payloads["summary"]
    daily = payloads["daily"]

    # サイト本体が読む一括ファイル（軽量化のため直近 3 年に絞る）
    cutoff = (date.fromisoformat(daily[-1]["date"]) - timedelta(days=365 * 3)).isoformat()
    recent_daily = [e for e in daily if e["date"] >= cutoff]

    write_json(
        DATA_DIR / "index.json",
        {
            "meta": meta,
            "summary": summary,
            "seasonality": payloads["seasonality"],
            "marketBreakdown": payloads["marketBreakdown"],
            "daily": recent_daily,
            "weekly": payloads["weekly"],
            "monthly": payloads["monthly"],
            "yearly": payloads["yearly"],
            "fiscal": payloads["fiscal"],
            "retail": payloads["retail"],
        },
        compact=True,
    )

    # 公開 JSON API
    api_files = {
        "meta.json": meta,
        "summary.json": {"meta": meta, "summary": summary},
        "daily.json": {"meta": meta, "count": len(daily), "series": daily},
        "weekly.json": {"meta": meta, "count": len(payloads["weekly"]), "series": payloads["weekly"]},
        "monthly.json": {
            "meta": meta,
            "count": len(payloads["monthly"]),
            "series": payloads["monthly"],
        },
        "yearly.json": {"meta": meta, "count": len(payloads["yearly"]), "series": payloads["yearly"]},
        "fiscal.json": {"meta": meta, "count": len(payloads["fiscal"]), "series": payloads["fiscal"]},
        "retail.json": {"meta": meta, "count": len(payloads["retail"]), "series": payloads["retail"]},
        "seasonality.json": {"meta": meta, "series": payloads["seasonality"]},
        "markets.json": {"meta": meta, "series": payloads["marketBreakdown"]},
        "lowest.json": {
            "meta": meta,
            "allTimeLow": summary["allTime"]["low"],
            "allTimeHigh": summary["allTime"]["high"],
            "windows": {
                key: {"low": win["low"], "high": win["high"]}
                for key, win in summary["windows"].items()
            },
        },
    }
    for name, payload in api_files.items():
        write_json(API_DIR / name, payload, compact=True)

    # 年別の日次データ（大きな系列を分割配信）
    by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in daily:
        by_year[entry["date"][:4]].append(entry)
    for year, rows in by_year.items():
        write_json(
            API_DIR / "daily" / f"{year}.json",
            {"meta": meta, "year": int(year), "count": len(rows), "series": rows},
            compact=True,
        )

    write_json(
        API_DIR / "index.json",
        {
            "meta": meta,
            "endpoints": sorted(
                [f"api/v1/{name}" for name in api_files]
                + [f"api/v1/daily/{year}.json" for year in sorted(by_year)]
            ),
        },
        compact=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="胡瓜価格データの構築")
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="進行中の年度のみ再取得する（日次更新向け）",
    )
    parser.add_argument(
        "--offline", action="store_true", help="ネットワークを使わずキャッシュのみで構築"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    payloads = build(incremental=args.incremental, offline=args.offline)
    emit(payloads)

    summary = payloads["summary"]
    LOG.info(
        "完了: %s 時点 %.1f 円/kg（前営業日比 %s%%）/ 最安値 %s 円 (%s)",
        summary["asOf"],
        summary["price"],
        summary["changePct"],
        summary["allTime"]["low"]["price"],
        summary["allTime"]["low"]["date"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
