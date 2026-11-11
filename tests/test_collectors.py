from __future__ import annotations

import datetime as dt

from jobs.collectors.sina_daily import parse_daily_kline
from jobs.collectors.sina_quote import parse_sina_response

NF_BODY = (
    'hq_str_nf_SR0="白糖连续,150000,5185.000,5207.000,5161.000,5195.000,5194.000,'
    "5195.000,5195.000,5188.000,5209.000,93,109,648578.000,366349,郑,白糖,"
    '2026-11-10,1,0,0,0,0";\n'
    'hq_str_hf_OIL="89.797,,89.800,89.810,92.840,89.660,16:51:17,92.570,92.450,'
    '0,2,2,2026-11-10,布伦特原油,74587";\n'
    'hq_str_hf_XX99="";\n'  # 空行应忽略
    'hq_str_nf_XX00="幽灵品种,150000,1,1,1,1,1,1,1,1,1,1,1,1,1,郑,鬼,2026-11-10,1";\n'
    'hq_str_hf_XX98="1,,1,1,1,1,16:51:17,1,1,0,2,2,2026-11-10,未知外盘,1";\n'
)

NOW = dt.datetime(2026, 11, 10, 17, 0, tzinfo=dt.UTC)


def test_parse_domestic_quote() -> None:
    items = {item.external_id: item for item in parse_sina_response(NF_BODY, now=NOW)}
    assert "SR0" in items
    sr = items["SR0"]
    assert sr.source == "sina"
    assert sr.ts == dt.datetime(2026, 11, 10, 7, 0, tzinfo=dt.UTC)  # 15:00 北京 → 07:00 UTC
    assert sr.payload["market"] == "D"
    assert sr.payload["variety_code"] == "SR"
    assert sr.payload["price"] == "5195.000"
    assert sr.payload["prev_close"] == "5209.000"  # 昨结算
    assert sr.payload["open"] == "5185.000"
    assert sr.payload["high"] == "5207.000"
    assert sr.payload["low"] == "5161.000"
    assert sr.payload["volume"] == "366349"
    assert sr.payload["oi"] == "648578.000"


def test_parse_foreign_quote() -> None:
    items = {item.external_id: item for item in parse_sina_response(NF_BODY, now=NOW)}
    oil = items["OIL"]
    assert oil.ts == dt.datetime(2026, 11, 10, 16, 51, 17, tzinfo=dt.UTC)  # 伦敦 16:51 (GMT)
    assert oil.payload["market"] == "F"
    assert oil.payload["variety_code"] is None
    assert oil.payload["price"] == "89.797"
    assert oil.payload["prev_close"] == "92.570"  # 昨结算
    assert oil.payload["high"] == "92.840"
    assert oil.payload["oi"] == "0"


def test_parse_ignores_unknown_and_empty() -> None:
    items = {item.external_id for item in parse_sina_response(NF_BODY, now=NOW)}
    assert items == {"SR0", "OIL"}


def test_parse_rejects_future_timestamps() -> None:
    past = NOW - dt.timedelta(days=2)
    items = parse_sina_response(NF_BODY, now=past)
    by_id = {item.external_id: item for item in items}
    assert "SR0" not in by_id  # 国内行情时间超前 → 直接丢弃
    assert by_id["OIL"].ts == past  # 外盘闭市最后价 → 回退为轮询时刻


def test_parse_daily_kline() -> None:
    body = (
        "/*<script>location.href='//finance.sina.com.cn';</script>*/\n"
        'var _tSR0=([{"d":"2026-11-07","o":5962.000,"h":5981.000,"l":5890.000,'
        '"c":5902.000,"v":363971,"p":648578,"s":5964.000},'
        '{"d":"bad","o":1,"h":1,"l":1,"c":1}]);'
    )
    items = parse_daily_kline(body)
    assert len(items) == 1
    assert items[0].external_id == "2026-11-07"
    assert items[0].payload["close"] == 5902.000
    assert items[0].payload["settle"] == 5964.000
    assert items[0].payload["oi"] == 648578


def test_parse_daily_kline_invalid_body() -> None:
    import pytest

    with pytest.raises(ValueError, match="JSON"):
        parse_daily_kline("not a jsonp response")
