"""将来の確定足だけで予想検証を行う集計関数。ネット接続不要。"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

JST = ZoneInfo('Asia/Tokyo')
RETENTION_DAYS = 120


def candle_id(index, asset_type):
    ts = pd.Timestamp(index)
    if asset_type in ('日本株', '低位株'):
        return ts.strftime('%Y-%m-%d')
    if ts.tzinfo is None:
        ts = ts.tz_localize('UTC')
    return ts.tz_convert(JST).isoformat(timespec='minutes')


def candle_closed_at(index, asset_type):
    ts = pd.Timestamp(index)
    if asset_type in ('日本株', '低位株'):
        # 東証の日足は取引終了 15:30 JST の後に確定とみなす。
        day = ts.date()
        return datetime(day.year, day.month, day.day, 15, 30, tzinfo=JST)
    if ts.tzinfo is None:
        ts = ts.tz_localize('UTC')
    return (ts + pd.Timedelta(hours=1)).tz_convert(JST).to_pydatetime()


def completed_history(frame, asset_type, now, publication_grace_minutes=15):
    """進行中の足と市場クローズ直後のデータを除外。"""
    if frame is None or frame.empty:
        return frame
    safe_before = now - timedelta(minutes=publication_grace_minutes)
    good = [candle_closed_at(idx, asset_type) <= safe_before for idx in frame.index]
    return frame.loc[good].copy()


def add_predictions(existing, snapshot):
    """同日の朝・夜でそれぞれ初回の予想のみ記録（再実行で書き換えない）。"""
    records = {r['id']: r for r in existing if isinstance(r, dict) and 'id' in r}
    for item in snapshot['items']:
        day = snapshot['generated_at'][:10]
        key = f"{day}|{snapshot['session']}|{item['ticker']}"
        if key in records:
            continue
        signal = item['signal']
        records[key] = {
            'id': key, 'date': day, 'session': snapshot['session'],
            'issued_at': snapshot['generated_at'], 'ticker': item['ticker'],
            'code': item['code'], 'name': item['name'],
            'category': item['category'], 'signal': signal, 'score': item['score'],
            'entry_price': item['raw_price'], 'currency': item['currency'],
            'base_candle': item['bar_key'],
            'status': 'PENDING' if signal != 'WAIT' else 'SKIPPED',
            'exit_price': None, 'exit_candle': None, 'change_pct': None,
        }
    return sorted(records.values(), key=lambda r: (r['issued_at'], r['ticker']), reverse=True)


def settle_predictions(records, histories, now):
    """発行後に確定した「次の」1本の足で判定。過去データの後付けを禁止。"""
    by_symbol = defaultdict(list)
    for record in records:
        if record.get('status') == 'PENDING':
            by_symbol[record['ticker']].append(record)

    for ticker, pending in by_symbol.items():
        hist = histories.get(ticker)
        if hist is None or hist.empty:
            continue
        cat = pending[0]['category']
        closed = completed_history(hist, cat, now)
        if closed is None or closed.empty:
            continue
        candles = []
        for idx, row in closed.iterrows():
            price = float(row['Close'])
            if price > 0 and pd.notna(price):
                candles.append((candle_id(idx, cat), candle_closed_at(idx, cat), price))

        for record in pending:
            issued = datetime.fromisoformat(record['issued_at'])
            base = record['base_candle']
            # 発表時点より新しい確定足だけを採用。
            following = next(((key, price) for key, close_at, price in candles
                              if key > base and close_at > issued), None)
            if following is None:
                continue
            exit_candle, exit_price = following
            entry = float(record['entry_price'])
            if entry <= 0:
                record['status'] = 'UNVERIFIABLE'
                continue
            ratio = exit_price / entry
            record['exit_candle'] = exit_candle
            record['exit_price'] = round(exit_price, 6)
            record['change_pct'] = round((ratio - 1) * 100, 3)
            # 株式分割・併合など、比較できない可能性がある変化は要確認にする。
            if cat in ('日本株', '低位株') and (ratio < 0.55 or ratio > 1.8):
                record['status'] = 'UNVERIFIABLE'
            elif abs(exit_price - entry) <= max(entry * 1e-9, 1e-10):
                record['status'] = 'FLAT'  # 同値は分母から除外
            elif (record['signal'] == 'BUY' and exit_price > entry) or (record['signal'] == 'SELL' and exit_price < entry):
                record['status'] = 'HIT'
            else:
                record['status'] = 'MISS'
    return records


def keep_recent(records, now, days=RETENTION_DAYS):
    since = (now.astimezone(JST).date() - timedelta(days=days)).isoformat()
    return [r for r in records if r.get('date', '') >= since]


def summary(records, now, days=30, category=None):
    cutoff = (now.astimezone(JST).date() - timedelta(days=days-1)).isoformat()
    chosen = [r for r in records if r['date'] >= cutoff and r['signal'] in ('BUY', 'SELL') and
              (category is None or r['category'] == category)]
    hits = sum(r['status'] == 'HIT' for r in chosen)
    misses = sum(r['status'] == 'MISS' for r in chosen)
    pending = sum(r['status'] == 'PENDING' for r in chosen)
    excluded = len(chosen) - hits - misses - pending
    checked = hits + misses
    return {'days': days, 'hit': hits, 'miss': misses, 'checked': checked,
            'pending': pending, 'excluded': excluded,
            'accuracy_pct': round(100 * hits / checked, 1) if checked else None}


def daily_summary(records, limit=12):
    result = []
    dates = sorted({r['date'] for r in records if r.get('signal') in ('BUY', 'SELL')}, reverse=True)
    for day in dates[:limit]:
        day_records = [r for r in records if r['date'] == day]
        eligible = [r for r in day_records if r['signal'] != 'WAIT']
        hits = sum(r['status'] == 'HIT' for r in eligible)
        misses = sum(r['status'] == 'MISS' for r in eligible)
        cnt = hits + misses
        result.append({'date': day, 'forecasts': len(eligible), 'hit': hits, 'miss': misses,
                       'pending': sum(r['status'] == 'PENDING' for r in eligible),
                       'accuracy_pct': round(hits / cnt * 100, 1) if cnt else None})
    return result
