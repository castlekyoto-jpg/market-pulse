"""本番の相場を使わない、予想検証の単体テスト。"""
import sys
import unittest
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from performance import add_predictions, settle_predictions, summary, completed_history, daily_summary

JST=ZoneInfo('Asia/Tokyo')


def record(date='2026-11-09', session='morning', signal='BUY', ticker='7203.T', category='日本株', price=100):
    snap={'session':session,'generated_at':f'{date}T07:07:00+09:00','items':[{
        'ticker':ticker,'code':'7203','name':'テスト株','category':category,'signal':signal,
        'score':4 if signal=='BUY' else -4, 'raw_price':price,'currency':'円', 'bar_key':'2026-11-06'
    }]}
    return add_predictions([],snap)[0]

class Tests(unittest.TestCase):
    def test_add_no_overwrite(self):
        old=[record()]
        snap={'session':'morning','generated_at':'2026-11-09T08:00:00+09:00','items':[{'ticker':'7203.T','code':'7203','name':'変更後','category':'日本株','signal':'SELL','score':-5,'raw_price':120,'currency':'円','bar_key':'2026-11-09'}]}
        new=add_predictions(old,snap)
        self.assertEqual(len(new),1)
        self.assertEqual(new[0]['signal'],'BUY')
        self.assertEqual(new[0]['entry_price'],100)

    def test_no_future_peeking_on_same_day(self):
        one=record()
        hist=pd.DataFrame({'Close':[100,110]},index=pd.to_datetime(['2026-11-06','2026-11-09']))
        before=datetime(2026,11,9,15,35,tzinfo=JST)
        self.assertEqual(settle_predictions([one], {'7203.T':hist},before)[0]['status'],'PENDING')
        # 当日15:30の足は予想後に確定：前日の足を基準に答え合わせ
        after=datetime(2026,11,9,16,00,tzinfo=JST)
        r=settle_predictions([one],{'7203.T':hist},after)[0]
        self.assertEqual(r['status'],'HIT')
        self.assertEqual(r['exit_price'],110)

    def test_losses_and_denominator(self):
        hit=record(); hit['status']='HIT'
        miss=record(signal='SELL',ticker='6758.T');miss['status']='MISS'
        pending=record(ticker='8306.T');pending['status']='PENDING'
        skipped=record(signal='WAIT',ticker='9432.T'); skipped['status']='SKIPPED'
        now=datetime(2026,11,10,8,tzinfo=JST)
        result=summary([hit,miss,pending,skipped],now,7)
        self.assertEqual((result['hit'],result['miss'],result['pending'],result['checked'],result['accuracy_pct']),(1,1,1,2,50.0))
        self.assertEqual(len(daily_summary([hit,miss,pending,skipped])),1)

    def test_sell_correct(self):
        rec=record(signal='SELL')
        hist=pd.DataFrame({'Close':[100,90]},index=pd.to_datetime(['2026-11-06','2026-11-09']))
        later=datetime(2026,11,9,18,tzinfo=JST)
        self.assertEqual(settle_predictions([rec],{'7203.T':hist},later)[0]['status'],'HIT')

    def test_intraday_not_settle_until_closed(self):
        one=record(ticker='USDJPY=X',category='FX',price=150)
        one['base_candle']='2026-11-09T06:00+09:00'
        idx=pd.to_datetime(['2026-11-08T21:00:00Z','2026-11-08T22:00:00Z'])
        hist=pd.DataFrame({'Close':[150,151]},index=idx)
        early=datetime(2026,11,9,7,20,tzinfo=JST)
        self.assertEqual(len(completed_history(hist,'FX',early)),1)
        self.assertEqual(settle_predictions([one],{'USDJPY=X':hist},early)[0]['status'],'PENDING')
        later=datetime(2026,11,9,8,20,tzinfo=JST)
        self.assertEqual(settle_predictions([one],{'USDJPY=X':hist},later)[0]['status'],'HIT')

if __name__=='__main__':unittest.main()
