"""データ取得層.

外部データ源へのアクセスをここに集約する。すべての取得処理は
リトライ・タイムアウト・明示的な失敗を伴い、上位層は例外のみを見る。

データ源
--------
1. 東京都中央卸売市場「東京都卸売市場日報（販売結果・価格・青果）」
   - 東京都オープンデータカタログ (CKAN) から年度別リソース URL を自動発見
   - 実データは CSV（UTF-8 BOM）。年度単位のファイル。
   - ライセンス: CC BY（東京都中央卸売市場）
   - 併せて東京都オープンデータ API（service.api.metro.tokyo.lg.jp）も利用可。
     CORS 対応済みのため、フロントエンドからの直接照会にも使える。

2. 農林水産省「食品価格動向調査（野菜）」の小売価格（全国平均・週次）
   - cultivationdata.net が提供する再配布 Web API を利用（JSON / CORS 対応）
   - 元データ: 農林水産省 食品価格動向調査（野菜）
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

LOG = logging.getLogger("sources")

USER_AGENT = "kyuri-price-index/1.0 (+https://github.com/cheattoolymt/Kyuri)"

CKAN_BASE = "https://catalog.data.metro.tokyo.lg.jp/api/3/action"
TOKYO_API_BASE = "https://service.api.metro.tokyo.lg.jp/api"
RETAIL_API = "https://api.cultivationdata.net/yasai_kakaku"

# 卸売市場日報データセット（青果・販売結果価格）の年度別カタログ ID。
# 東京都は年度ごとに別データセットを作るため、CKAN 検索で自動発見しつつ
# 既知 ID をフォールバックとして持つ。
KNOWN_DAILY_REPORT_DATASETS = [
    "t000013d0000000003",  # 2020年度
    "t000013d0000000004",  # 2021年度
    "t000013d0000000005",  # 2022年度
    "t000013d0000000012",  # 2023年度
    "t000013d2000000032",  # 2024年度
    "t000013d2000000033",  # 2025年度
]

FRESH_PRICE_RESOURCE_MARKER = "result_price_fresh.csv"
FRESH_QUANTITY_RESOURCE_MARKER = "result_quantity_fresh.csv"


class SourceError(RuntimeError):
    """データ源からの取得に失敗した場合に送出される。"""


@dataclass(frozen=True)
class WholesaleResource:
    """年度単位の卸売市場日報リソース（価格・数量）。"""

    dataset_id: str
    fiscal_year: int
    title: str
    price_csv_url: str
    quantity_csv_url: str | None
    last_modified: str | None


def _request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
    retries: int = 4,
    backoff: float = 2.0,
) -> bytes:
    """リトライ付き HTTP 取得。恒久的な失敗のみ SourceError を送出する。"""
    merged = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, identity"}
    if headers:
        merged.update(headers)

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=merged)
            with urllib.request.urlopen(req, timeout=timeout) as res:
                payload = res.read()
                if res.headers.get("Content-Encoding") == "gzip":
                    import gzip

                    payload = gzip.decompress(payload)
                return payload
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            status = getattr(exc, "code", None)
            if status is not None and 400 <= status < 500 and status not in (408, 429):
                raise SourceError(f"{url} -> HTTP {status}") from exc
            if attempt == retries:
                break
            wait = backoff ** attempt
            LOG.warning("取得失敗 (%s/%s) %s: %s / %.1fs 後に再試行", attempt, retries, url, exc, wait)
            time.sleep(wait)

    raise SourceError(f"{url} の取得に失敗しました: {last_error}")


def _get_json(url: str, **kwargs: Any) -> Any:
    raw = _request(url, **kwargs)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError(f"{url} のレスポンスが JSON として解釈できません: {exc}") from exc


def _fiscal_year_from_title(title: str) -> int | None:
    """「東京都卸売市場日報（2025年度）」から 2025 を取り出す。"""
    import re

    m = re.search(r"(\d{4})\s*年度", title)
    return int(m.group(1)) if m else None


def discover_wholesale_resources() -> list[WholesaleResource]:
    """卸売市場日報（青果・価格）の年度別 CSV リソースを列挙する。

    CKAN の全文検索でデータセットを発見し、失敗した場合は既知 ID に退避する。
    新年度データセットが追加された場合も自動的に取り込まれる。
    """
    datasets: dict[str, dict[str, Any]] = {}

    try:
        query = urllib.parse.urlencode(
            {"q": "東京都卸売市場日報", "rows": 100, "sort": "metadata_modified desc"}
        )
        payload = _get_json(f"{CKAN_BASE}/package_search?{query}", timeout=60, retries=3)
        for pkg in payload.get("result", {}).get("results", []):
            if "卸売市場日報" in pkg.get("title", ""):
                datasets[pkg["name"]] = pkg
    except SourceError as exc:
        LOG.warning("CKAN 検索に失敗したため既知 ID を使用します: %s", exc)

    for dataset_id in KNOWN_DAILY_REPORT_DATASETS:
        if dataset_id in datasets:
            continue
        try:
            payload = _get_json(
                f"{CKAN_BASE}/package_show?id={dataset_id}", timeout=60, retries=3
            )
            if payload.get("success"):
                datasets[dataset_id] = payload["result"]
        except SourceError as exc:
            LOG.warning("データセット %s を取得できません: %s", dataset_id, exc)

    resources: list[WholesaleResource] = []
    for dataset_id, pkg in datasets.items():
        fiscal_year = _fiscal_year_from_title(pkg.get("title", ""))
        if fiscal_year is None:
            continue

        price_url: str | None = None
        quantity_url: str | None = None
        last_modified: str | None = None
        for res in pkg.get("resources", []):
            url = res.get("url", "")
            if FRESH_PRICE_RESOURCE_MARKER in url:
                price_url = url
                last_modified = res.get("last_modified")
            elif FRESH_QUANTITY_RESOURCE_MARKER in url:
                quantity_url = url

        if not price_url:
            continue

        resources.append(
            WholesaleResource(
                dataset_id=dataset_id,
                fiscal_year=fiscal_year,
                title=pkg.get("title", ""),
                price_csv_url=price_url,
                quantity_csv_url=quantity_url,
                last_modified=last_modified,
            )
        )

    resources.sort(key=lambda r: r.fiscal_year)
    if not resources:
        raise SourceError("卸売市場日報（青果・価格）のリソースを発見できませんでした")
    return resources


def _iter_filtered_csv(
    csv_url: str, item_name: str, required: set[str]
) -> Iterator[dict[str, str]]:
    """年度 CSV をダウンロードし、対象品名の行のみを返す。

    CSV は 30MB 前後だがカラム構成は安定している。メモリ上で
    ストリーム処理し、対象品名以外は即座に捨てる。
    """
    raw = _request(csv_url, timeout=600, retries=4)
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    missing = required - set(reader.fieldnames or [])
    if missing:
        raise SourceError(f"{csv_url} のカラム構成が想定外です（欠落: {sorted(missing)}）")

    for row in reader:
        if row.get("品名") == item_name:
            yield row


PRICE_COLUMNS = {
    "日付",
    "品名",
    "単位",
    "高値(円)",
    "中値(円)",
    "安値(円)",
    "市場",
    "産地",
    "販売方法",
}
QUANTITY_COLUMNS = {"日付", "品名", "市場", "販売方法", "卸売数量(kg)"}


def iter_wholesale_price_rows(csv_url: str, item_name: str) -> Iterator[dict[str, str]]:
    """卸売価格 CSV から対象品名の行を返す。"""
    return _iter_filtered_csv(csv_url, item_name, PRICE_COLUMNS)


def iter_wholesale_quantity_rows(csv_url: str, item_name: str) -> Iterator[dict[str, str]]:
    """卸売数量 CSV から対象品名の行を返す。"""
    return _iter_filtered_csv(csv_url, item_name, QUANTITY_COLUMNS)


def fetch_wholesale_via_api(api_path: str, item_name: str, *, limit: int = 1000) -> list[dict[str, str]]:
    """東京都オープンデータ API 経由で対象品名の行を取得する（CSV 取得の代替）。

    api_path は "t000013d2000000033-<hash>-0" 形式の API ID。
    """
    url = f"{TOKYO_API_BASE}/{api_path}/json"
    body = json.dumps(
        {
            "searchCondition": {
                "stringAndSearch": [
                    {"column": "品名", "relationship": "eq", "condition": item_name}
                ]
            }
        }
    ).encode("utf-8")

    hits: list[dict[str, str]] = []
    offset = 0
    while True:
        page = _get_json(
            f"{url}?limit={limit}&offset={offset}",
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=120,
            retries=3,
        )
        chunk = page.get("hits", [])
        hits.extend(chunk)
        total = page.get("total", 0)
        offset += len(chunk)
        if not chunk or offset >= total:
            break
    return hits


def fetch_retail_series() -> dict[str, dict[str, Any]]:
    """農林水産省 食品価格動向調査（野菜）の全期間データを取得する。

    戻り値: {"2026/8/24": {"Kyuuri": 816, ...}, ...}
    """
    payload = _get_json(f"{RETAIL_API}?div=all", timeout=120, retries=4)
    if not isinstance(payload, dict) or not payload:
        raise SourceError("小売価格 API のレスポンスが空です")
    return payload
