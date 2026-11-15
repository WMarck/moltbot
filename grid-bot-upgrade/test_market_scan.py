import unittest

from market_scan import allow_new_buys, atr_pct, ema, market_regime, rank_symbols


def tick(sym, chg, vol=50e6, high=1.05, low=0.95, last=1.0, bid=0.9999, ask=1.0001):
    return {"symbol": sym, "priceChangePercent": str(chg), "quoteVolume": str(vol),
            "highPrice": str(high), "lowPrice": str(low), "lastPrice": str(last),
            "bidPrice": str(bid), "askPrice": str(ask)}


def klines(prices, spread=0.5):
    # [open_time, open, high, low, close, ...]
    return [[i, p, p + spread, p - spread, p] for i, p in enumerate(prices)]


UP = klines([100 + i for i in range(60)])
DOWN = klines([200 - i for i in range(60)])


class Scan(unittest.TestCase):
    def test_ema_tracks_trend(self):
        self.assertGreater(ema([1, 2, 3, 4, 5], 3)[-1], ema([1, 2, 3, 4, 5], 3)[0])

    def test_atr_zero_when_short(self):
        self.assertEqual(atr_pct(klines([1, 2, 3])), 0.0)

    def test_risk_off_on_broad_selloff_with_btc_down(self):
        t = [tick(f"A{i}USDT", -3) for i in range(40)]
        self.assertEqual(market_regime(t, DOWN).label, "RISK_OFF")

    def test_risk_on_when_broad_rally(self):
        t = [tick(f"A{i}USDT", 4) for i in range(40)]
        self.assertEqual(market_regime(t, UP).label, "RISK_ON")

    def test_neutral_when_mixed(self):
        t = [tick(f"A{i}USDT", 1 if i % 2 else -1) for i in range(40)]
        self.assertEqual(market_regime(t, UP).label, "NEUTRAL")

    def test_missing_data_never_blocks(self):
        r = market_regime([tick("AUSDT", -9)], DOWN)
        self.assertEqual(r.label, "NEUTRAL")
        self.assertTrue(allow_new_buys(r, False)[0])

    def test_stables_and_leveraged_ignored(self):
        t = [tick("USDCUSDT", -50), tick("BTCUPUSDT", -50)] + [tick(f"A{i}USDT", 2) for i in range(40)]
        self.assertEqual(market_regime(t, UP).universe, 40)

    def test_gate_blocks_buys_in_risk_off_only_for_buys(self):
        t = [tick(f"A{i}USDT", -3) for i in range(40)]
        ok, why = allow_new_buys(market_regime(t, DOWN), False)
        self.assertFalse(ok)
        self.assertIn("RISK_OFF", why)

    def test_gate_symbol_down_in_neutral_blocks_but_risk_on_allows(self):
        neutral = market_regime([tick(f"A{i}USDT", 1 if i % 2 else -1) for i in range(40)], UP)
        self.assertFalse(allow_new_buys(neutral, True)[0])
        risk_on = market_regime([tick(f"A{i}USDT", 4) for i in range(40)], UP)
        self.assertTrue(allow_new_buys(risk_on, True)[0])

    def test_rank_prefers_volatile_and_drops_low_range(self):
        t = [tick("MEMEUSDT", 5, high=1.14, low=0.95),   # faixa ~19%
             tick("BTCUSDT", 0.5, high=1.006, low=0.998),  # faixa < 1%
             tick("WIDEUSDT", 3, high=1.14, low=0.95, bid=0.9, ask=1.1)]  # spread alto
        r = rank_symbols(t, levels=8)
        self.assertEqual([x["symbol"] for x in r], ["MEMEUSDT"])
        self.assertGreater(r[0]["net_usdt_per_cycle"], 0)


if __name__ == "__main__":
    unittest.main()
