"""生成された JSON の健全性検証.

日次更新の自動コミット前に実行し、壊れたデータが公開されるのを防ぐ。
異常があれば非ゼロ終了する。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "index.json"
API = ROOT / "api" / "v1"

# 卸売価格として妥当な 1kg 単価の範囲（円）。これを外れたら集計を疑う。
SANE_MIN = 100.0
SANE_MAX = 2000.0

MIN_SESSIONS = 200


class ValidationError(Exception):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def validate_daily(series: list[dict]) -> None:
    check(len(series) >= MIN_SESSIONS, f"日次データが少なすぎます: {len(series)} 件")

    previous: date | None = None
    for entry in series:
        day = date.fromisoformat(entry["date"])
        check(previous is None or day > previous, f"日付が昇順でありません: {entry['date']}")
        previous = day

        for key in ("open", "high", "low", "close"):
            value = entry.get(key)
            check(isinstance(value, (int, float)), f"{entry['date']}: {key} が数値でありません")
            check(
                SANE_MIN <= value <= SANE_MAX,
                f"{entry['date']}: {key}={value} が妥当な範囲外です",
            )

        check(
            entry["low"] <= min(entry["open"], entry["close"])
            and entry["high"] >= max(entry["open"], entry["close"]),
            f"{entry['date']}: 四本値の整合が取れていません {entry}",
        )
        check(entry["samples"] >= 1, f"{entry['date']}: 建値件数が 0 です")


def validate_summary(summary: dict, series: list[dict], full_series: list[dict]) -> None:
    """summary の整合を検証する。

    data/index.json の daily は転送量を抑えるため直近 3 年に絞られている。
    全期間の最安値・最高値は api/v1/daily.json（全系列）と突き合わせる。
    """
    check(summary["asOf"] == series[-1]["date"], "summary.asOf が日次系列の末尾と一致しません")
    check(summary["price"] == series[-1]["close"], "summary.price が末尾終値と一致しません")

    low = summary["allTime"]["low"]
    high = summary["allTime"]["high"]
    closes = [e["close"] for e in full_series]
    check(low["price"] == min(closes), "過去最安値が全日次系列と一致しません")
    check(high["price"] == max(closes), "過去最高値が全日次系列と一致しません")
    check(low["price"] < high["price"], "最安値が最高値を下回っていません")
    check(
        summary["allTime"]["sessions"] == len(full_series),
        "収録営業日数が全日次系列と一致しません",
    )

    for key, window in summary["windows"].items():
        check(window["sessions"] >= 2, f"windows.{key}: 営業日数が不足しています")
        check(
            window["low"]["price"] <= window["high"]["price"],
            f"windows.{key}: 高値・安値が逆転しています",
        )


def validate_periods(payload: dict) -> None:
    for name in ("weekly", "monthly", "yearly", "fiscal"):
        series = payload.get(name)
        check(bool(series), f"{name} が空です")
        for entry in series:
            check(
                entry["low"] <= entry["close"] <= entry["high"],
                f"{name}/{entry['period']}: 四本値の整合が取れていません",
            )


def validate_api_files() -> None:
    required = [
        "index.json",
        "meta.json",
        "summary.json",
        "daily.json",
        "weekly.json",
        "monthly.json",
        "yearly.json",
        "fiscal.json",
        "retail.json",
        "seasonality.json",
        "markets.json",
        "lowest.json",
    ]
    for name in required:
        path = API / name
        check(path.exists(), f"api/v1/{name} が存在しません")
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"api/v1/{name} が不正な JSON です: {exc}") from exc


def main() -> int:
    try:
        check(DATA.exists(), "data/index.json が存在しません")
        payload = json.loads(DATA.read_text(encoding="utf-8"))

        validate_api_files()

        series = payload.get("daily") or []
        full_series = json.loads((API / "daily.json").read_text(encoding="utf-8"))["series"]

        validate_daily(series)
        validate_daily(full_series)
        validate_summary(payload["summary"], series, full_series)
        validate_periods(payload)

        check(bool(payload.get("seasonality")), "季節性データが空です")
        check(bool(payload.get("marketBreakdown")), "市場別データが空です")
        check(bool(payload.get("retail")), "小売価格データが空です")
    except (ValidationError, KeyError, ValueError) as exc:
        print(f"検証失敗: {exc}", file=sys.stderr)
        return 1

    summary = payload["summary"]
    print(
        f"検証成功: {summary['asOf']} 時点 {summary['price']} 円/kg / "
        f"{summary['allTime']['sessions']} 営業日 / "
        f"最安値 {summary['allTime']['low']['price']} 円 "
        f"({summary['allTime']['low']['date']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
