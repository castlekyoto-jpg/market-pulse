#!/usr/bin/env python3
"""朝・夜のFX/日本株テクニカル判定を data.json に書き出します。売買執行はしません。"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from performance import (add_predictions, settle_predictions, keep_recent, summary, daily_summary, completed_history, candle_id)

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data.json"
HISTORY = ROOT / "history.json"
WATCHLIST = ROOT / "watchlist.csv"
JST = ZoneInfo("Asia/Tokyo")
LOW_PRICE_LIMIT = 500.0   # 円以下を低位株として抽出
LOW_LIQUIDITY_LIMIT = 15_000_000  # 20日平均売買代金15百万円未満は買い判定抑制
JPX_XLSX = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
FX = [
    ("USDJPY=X", "米ドル／円", "FX", "円"),
    ("EURJPY=X", "ユーロ／円", "FX", "円"),
]
# Yahoo Finance の XAUUSD=X は 404 になるため最初から照会しない。
# GC=F はCOMEX金先物で、XAUUSD現物やXMのGOLDとは別の商品です。
GOLD_SOURCE = ("GC=F", "GOLD / COMEX金先物（参考）", "GOLD", "USD")


def finite_float(value):
    try:
        n = float(value)
        if math.isfinite(n):
            return n
    except (ValueError, TypeError):
        pass
    return None


def round_price(value, currency):
    if value is None:
        return None
    return round(float(value), 3 if currency != "円" else 2)


def read_watchlist():
    result = []
    with WATCHLIST.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = str(row["code"]).strip()
            if not code or code.startswith("#"):
                continue
            if not code.isalnum():
                print(f"注意: 不正な証券コードはスキップ: {code}")
                continue
            result.append((f"{code}.T", str(row.get("name", "")).strip() or code, code))
    return result


def get_jpx_names():
    """名前変更などに追従。JPXの取得失敗時はCSV名を利用。"""
    try:
        res = requests.get(JPX_XLSX, timeout=18, headers={"User-Agent": "Mozilla/5.0"})
        res.raise_for_status()
        table = pd.read_excel(io.BytesIO(res.content), dtype={"コード": "string"}, engine="openpyxl")
        if not {"コード", "銘柄名"}.issubset(table.columns):
            return {}
        result = {}
        for _, line in table.iterrows():
            code = str(line["コード"]).strip()
            if code.endswith(".0"):
                code = code[:-2]
            name = str(line["銘柄名"]).strip()
            if code and name and name != "nan":
                result[code] = name
        return result
    except Exception as exc:
        print(f"JPX上場企業名の更新に失敗（CSV名で継続）: {exc}")
        return {}


def fetch_batch(symbols, period="6mo", interval="1d"):
    try:
        data = yf.download(
            tickers=symbols, period=period, interval=interval,
            group_by="ticker", auto_adjust=True,
            threads=4, progress=False, timeout=25,
            multi_level_index=True,
        )
        return data if data is not None else pd.DataFrame()
    except Exception as exc:
        print(f"価格取得に失敗 {symbols}: {exc}")
        return pd.DataFrame()


def extract_ticker(data, symbol):
    if data is None or data.empty:
        return pd.DataFrame()
    try:
        if isinstance(data.columns, pd.MultiIndex):
            if symbol in data.columns.get_level_values(0):
                part = data[symbol].copy()
            elif symbol in data.columns.get_level_values(1):
                part = data.xs(symbol, axis=1, level=1).copy()
            else:
                return pd.DataFrame()
        else:
            part = data.copy()
        if not {"Open", "High", "Low", "Close"}.issubset(part.columns):
            return pd.DataFrame()
        part = part.dropna(subset=["High", "Low", "Close"]).copy().sort_index()
        if "Volume" not in part.columns:
            part["Volume"] = float("nan")
        return part
    except Exception as exc:
        print(f"{symbol} 分割データの処理失敗: {exc}")
        return pd.DataFrame()


def calculate_signal(history, symbol, name, category, currency, code=None):
    if history is None or len(history) < 65:
        return None
    close = pd.to_numeric(history["Close"], errors="coerce")
    high = pd.to_numeric(history["High"], errors="coerce")
    low = pd.to_numeric(history["Low"], errors="coerce")
    volume = pd.to_numeric(history["Volume"], errors="coerce")
    if close.isna().tail(55).any() or (close.tail(65) <= 0).any():
        return None
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_sig = macd.ewm(span=9, adjust=False).mean()
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    down = -delta.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / down.replace(0, float("nan")))
    if down.iloc[-1] == 0:
        rsi.iloc[-1] = 100.0 if up.iloc[-1] > 0 else 50.0
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14, adjust=False).mean()

    price = finite_float(close.iloc[-1]); p5 = finite_float(close.iloc[-6]); atr_now = finite_float(atr.iloc[-1])
    e20 = finite_float(ema20.iloc[-1]); e50 = finite_float(ema50.iloc[-1])
    mh = finite_float((macd-macd_sig).iloc[-1]); rsi_now = finite_float(rsi.iloc[-1])
    if any(x is None for x in (price, p5, atr_now, e20, e50, mh, rsi_now)):
        return None

    if category == "日本株" and price <= LOW_PRICE_LIMIT:
        category = "低位株"
    score = 0
    reasons = []
    if price > e20:
        score += 1; reasons.append("価格が20期間移動平均より上")
    else:
        score -= 1; reasons.append("価格が20期間移動平均より下")
    if e20 > e50:
        score += 1; reasons.append("20期間線が50期間線より上")
    else:
        score -= 1; reasons.append("20期間線が50期間線より下")
    if mh > 0:
        score += 1; reasons.append("MACDが上向き")
    else:
        score -= 1; reasons.append("MACDが下向き")
    if 55 <= rsi_now < 70:
        score += 1; reasons.append("RSIが上昇優勢")
    elif rsi_now >= 75:
        score -= 1; reasons.append("RSIが買われ過ぎ圏")
    elif rsi_now < 45:
        score -= 1; reasons.append("RSIが弱気圏")
    if price > p5:
        score += 1; reasons.append("直近5期間で上昇")
    else:
        score -= 1; reasons.append("直近5期間で下落")

    signal = "BUY" if score >= 3 else "SELL" if score <= -3 else "WAIT"
    warnings = []
    vol_ratio = None
    avg_turnover = None
    if category in ("日本株", "低位株"):
        daily_turnover = (close * volume).replace([float("inf"), -float("inf")], float("nan"))
        avg_turnover = finite_float(daily_turnover.tail(20).mean())
        if avg_turnover is None:
            warnings.append("出来高の確認ができません")
            if signal == "BUY": signal = "WAIT"
        elif avg_turnover < LOW_LIQUIDITY_LIMIT:
            warnings.append("売買代金が少なく、流動性に注意")
            if signal == "BUY": signal = "WAIT"
        avg_vol = finite_float(volume.tail(20).mean())
        this_vol = finite_float(volume.iloc[-1])
        if avg_vol and this_vol is not None:
            vol_ratio = round(this_vol/avg_vol, 2)
    atr_pct = atr_now / price * 100
    limit = 8.0 if category in ("日本株", "低位株") else 3.0
    if atr_pct > limit:
        warnings.append("値動きが大きく、リスク高め")
        if signal == "BUY": signal = "WAIT"

    dt = history.index[-1]
    if hasattr(dt, "to_pydatetime"):
        dt = dt.to_pydatetime()
    bar_date = dt.strftime("%Y-%m-%d")
    # 株価は日足。FX/GOLDの1時間足は、直近足の時刻も表示。
    bar_at = dt.astimezone(JST).strftime("%Y-%m-%d %H:%M") if dt.tzinfo is not None else bar_date

    # 為替レートは最低3桁まで保持します（日本株は2桁）。
    price_digits = 3 if category == "FX" else 2 if currency == "円" else 3
    stop = target = None
    if signal == "BUY":
        stop = price - 1.5*atr_now; target = price + 2.5*atr_now
    elif signal == "SELL":
        stop = price + 1.5*atr_now; target = price - 2.5*atr_now

    return {
        "ticker": symbol, "code": code or symbol, "name": name, "category": category,
        "currency": currency, "timeframe": "日足" if category in ("日本株", "低位株") else "1時間足",
        "price": round(price, price_digits), "raw_price": round(price, 8),
        "bar_key": candle_id(history.index[-1], category),
        "previous_change_pct": round((price / float(close.iloc[-2]) - 1)*100, 2) if close.iloc[-2] else None,
        "signal": signal, "score": int(score), "rsi": round(rsi_now, 1),
        "atr_pct": round(atr_pct, 2), "stop": round(stop, price_digits) if stop is not None else None,
        "target": round(target, price_digits) if target is not None else None, "volume_ratio": vol_ratio,
        "average_turnover": round(avg_turnover) if avg_turnover is not None else None,
        "price_at": bar_at, "reasons": reasons, "warnings": warnings,
    }


def build_snapshot(session, previous_predictions=None, now=None):
    now = now or datetime.now(JST)
    items = []; failed = []; histories = {}
    jpx_names = get_jpx_names()
    watchlist = read_watchlist()
    for j in range(0, len(watchlist), 20):
        subset = watchlist[j:j+20]
        raw = fetch_batch([x[0] for x in subset])
        for symbol, csv_name, code in subset:
            name = jpx_names.get(code, csv_name)
            hist = completed_history(extract_ticker(raw, symbol), "日本株", now)
            if not hist.empty: histories[symbol] = hist
            sig = calculate_signal(hist, symbol, name, "日本株", "円", code)
            if sig:
                items.append(sig)
            else:
                failed.append(symbol)
        if j + 20 < len(watchlist):
            time.sleep(0.6)
    # FXとCOMEX金先物を同じダウンロードで取得。未対応の XAUUSD=X は照会しない。
    symbols = [x[0] for x in FX] + [GOLD_SOURCE[0]]
    fxdata = fetch_batch(symbols, period="60d", interval="1h")
    for symbol, name, category, currency in FX:
        hist = completed_history(extract_ticker(fxdata, symbol), category, now)
        if not hist.empty: histories[symbol] = hist
        sig = calculate_signal(hist, symbol, name, category, currency)
        if sig: items.append(sig)
        else: failed.append(symbol)

    gold_hist = completed_history(extract_ticker(fxdata, GOLD_SOURCE[0]), "GOLD", now)
    if not gold_hist.empty: histories[GOLD_SOURCE[0]] = gold_hist
    gold = calculate_signal(gold_hist, *GOLD_SOURCE)
    if gold:
        gold["warnings"].insert(0, "COMEX金先物の参考価格です。XAUUSD現物・XM GOLDとは異なります。")
        items.append(gold)
    else:
        failed.append("GOLD (GC=F)")

    # 旧版が XAUUSD=X で記録した予想は、現物データを取得できないため
    # 先物の価格で答え合わせしない（異なる商品の比較は誤った的中率になります）。
    if previous_predictions:
        for prediction in previous_predictions:
            if prediction.get("ticker") == "XAUUSD=X" and prediction.get("status") == "PENDING":
                prediction["status"] = "UNVERIFIABLE"
                prediction["unverifiable_reason"] = "Yahoo Financeで現物の価格が取得できないため判定不能"

    # 低位株は500円以下のみ採用。CSVで監視対象を追加できます。
    items.sort(key=lambda x: (0 if x["signal"] == "BUY" else 1 if x["signal"] == "WAIT" else 2,
                              -abs(x["score"]), x["category"], x["ticker"]))
    snapshot = {
        "session": session, "generated_at": now.isoformat(timespec="seconds"),
        "timezone": "Asia/Tokyo", "symbols_checked": len(watchlist) + len(FX) + 1,
        "count": len(items), "failed": failed,
        "items": items,
    }
    return snapshot, histories


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", choices=["auto", "morning", "night"], default="auto")
    args = parser.parse_args()
    now = datetime.now(JST)
    session = args.session
    if session == "auto":
        session = "morning" if now.hour < 13 else "night"
    existing = {}
    if OUTPUT.exists():
        try:
            existing = json.loads(OUTPUT.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    old_history = {}
    if HISTORY.exists():
        try:
            old_history = json.loads(HISTORY.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            pass
    previous_records = old_history.get('predictions', existing.get('predictions', []))
    snapshot, histories = build_snapshot(session, previous_records, now=now)
    print(f"{session}: {snapshot['count']}/{snapshot['symbols_checked']} 件取得、失敗 {len(snapshot['failed'])} 件")
    if not snapshot['items']:
        raise SystemExit("取得0件のため、既存の結果と的中履歴は変更せず保持します。")

    # 古い記録を精算してから、本日新規予想を登録。二重登録はしない。
    records = settle_predictions(previous_records, histories, now)
    # 土日は価格が動いていないことが多いため、手動起動時に週末の予想を新規登録しない。
    # サイトの最新スナップショットは作成する（画面の動作確認はできる）。
    if now.weekday() < 5:
        records = add_predictions(records, snapshot)
    else:
        print("土日はデータ表示のみ更新し、新しい的中率の予想履歴は登録しません。")
    records = keep_recent(records, now)
    snapshots = existing.get('snapshots', {})
    snapshots[session] = snapshot
    counts = {str(days): summary(records, now, days) for days in (7, 30, 90)}
    by_category = {cat: summary(records, now, 30, cat)
                   for cat in ('GOLD', 'FX', '日本株', '低位株')}
    output = {
        "version": 2, "latest": session,
        "updated_at": snapshot['generated_at'],
        "strategy": "EMA20/50 + MACD + RSI14 + 5期間騰落。次の確定足の方向で検証（デモ分析）。",
        "low_price_limit_jpy": LOW_PRICE_LIMIT,
        "monitor_count": len(read_watchlist()),
        "performance": {"periods": counts, "by_category": by_category,
                        "daily": daily_summary(records, limit=14)},
        "snapshots": snapshots,
        "recent_predictions": [r for r in records if r['date'] >=
                               (now.date() - timedelta(days=7)).isoformat()],
    }
    tmp = OUTPUT.with_name(OUTPUT.name + '.tmp')
    tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    tmp.replace(OUTPUT)
    htmp = HISTORY.with_name(HISTORY.name + '.tmp')
    htmp.write_text(json.dumps({'version': 1, 'predictions': records}, ensure_ascii=False,
                               separators=(',', ':'), allow_nan=False) + '\n', encoding='utf-8')
    htmp.replace(HISTORY)
    print(f"保存: {OUTPUT}, {HISTORY} / 履歴: {len(records)} 件")
    if snapshot['failed']:
        print("欠損銘柄:", ", ".join(snapshot['failed']))


if __name__ == "__main__":
    main()
