"""Varredura de mercado para o grid bot (somente biblioteca padrao, sem ordens).

Ideia: antes de qualquer compra, olhar o mercado inteiro (nao so o par do bot).
Tres pecas, todas funcoes puras (faceis de testar, sem chamar a Binance aqui):

  1. market_regime()  -> RISK_ON / NEUTRAL / RISK_OFF  (amplitude + BTC + volatilidade)
  2. rank_symbols()   -> quais pares valem a pena para grid (volatil o bastante p/ cobrir taxas)
  3. allow_new_buys() -> porta unica que o bot consulta antes de abrir nova compra

Uso no bot (esboco, a cada ~5 min e nao a cada 20 s, para poupar limite de API):
    tickers = client.get_ticker()                      # 1 chamada, todos os pares (24h)
    btc_1h  = client.get_klines(symbol="BTCUSDT", interval="1h", limit=60)
    regime  = market_regime(tickers, btc_1h)
    if not allow_new_buys(regime, symbol_trend_down): ...  # nao abrir nova compra
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

# Taxa spot padrao da Binance por lado (0,075% se pagar taxa em BNB).
FEE_PER_SIDE = 0.001

# Pares alavancados/estaveis que distorcem a leitura de amplitude.
_EXCLUDED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
_STABLES = {"USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "USDPUSDT", "EURUSDT", "BUSDUSDT"}


def ema(values: list[float], period: int) -> list[float]:
    """EMA classica (alpha = 2/(period+1)), semeada com o primeiro valor."""
    if not values:
        return []
    alpha = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def closes(klines: list[list]) -> list[float]:
    """Formato de get_klines da Binance: indice 4 = preco de fechamento."""
    return [float(k[4]) for k in klines]


def atr_pct(klines: list[list], period: int = 14) -> float:
    """ATR medio em % do preco (volatilidade tipica por vela)."""
    if len(klines) < period + 1:
        return 0.0
    trs = []
    for prev, cur in zip(klines, klines[1:]):
        high, low, prev_close = float(cur[2]), float(cur[3]), float(prev[4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    last = float(klines[-1][4])
    return (sum(trs[-period:]) / period) / last * 100 if last else 0.0


def _usdt_universe(tickers: list[dict], min_quote_volume: float) -> list[dict]:
    out = []
    for t in tickers:
        s = t.get("symbol", "")
        if not s.endswith("USDT") or s in _STABLES or s.endswith(_EXCLUDED_SUFFIXES):
            continue
        if float(t.get("quoteVolume", 0)) < min_quote_volume:
            continue
        out.append(t)
    return out


@dataclass(frozen=True)
class Regime:
    label: str            # RISK_ON | NEUTRAL | RISK_OFF
    breadth_up: float     # fracao de pares com variacao 24h > 0
    median_change: float  # mediana da variacao 24h (%)
    btc_trend_down: bool  # EMA9 < EMA21 no 1h do BTC
    btc_atr_pct: float    # volatilidade do BTC (% por vela de 1h)
    universe: int         # quantos pares entraram na leitura
    reason: str


def market_regime(
    tickers: list[dict],
    btc_1h: list[list],
    min_quote_volume: float = 5_000_000,
    min_universe: int = 30,
) -> Regime:
    """Le o mercado todo. Sem dados suficientes, devolve NEUTRAL (nunca bloqueia por falta de dado)."""
    uni = _usdt_universe(tickers, min_quote_volume)
    c = closes(btc_1h)
    if len(uni) < min_universe or len(c) < 22:
        return Regime("NEUTRAL", 0.5, 0.0, False, 0.0, len(uni), "dados insuficientes")

    changes = [float(t["priceChangePercent"]) for t in uni]
    breadth = sum(1 for x in changes if x > 0) / len(changes)
    med = median(changes)
    btc_down = ema(c, 9)[-1] < ema(c, 21)[-1]
    vol = atr_pct(btc_1h)

    # Regras simples e explicaveis: queda ampla + BTC em baixa = ficar fora.
    if breadth < 0.30 and btc_down:
        label, why = "RISK_OFF", f"so {breadth:.0%} dos pares sobem e BTC em baixa"
    elif breadth < 0.20:
        label, why = "RISK_OFF", f"so {breadth:.0%} dos pares sobem (queda ampla)"
    elif breadth > 0.60 and not btc_down:
        label, why = "RISK_ON", f"{breadth:.0%} dos pares sobem e BTC em alta"
    else:
        label, why = "NEUTRAL", f"amplitude {breadth:.0%}, BTC {'em baixa' if btc_down else 'em alta'}"
    return Regime(label, breadth, med, btc_down, vol, len(uni), why)


def rank_symbols(
    tickers: list[dict],
    order_size_usdt: float = 6.0,
    levels: int = 8,
    fee_per_side: float = FEE_PER_SIDE,
    min_quote_volume: float = 20_000_000,
    max_spread_pct: float = 0.05,
    top: int = 5,
) -> list[dict]:
    """Escolhe pares para grid. Automatiza a conta feita a mao (DOGE/SHIB vs BTC/SOL).

    Espaco medio entre niveis ~ faixa_24h / levels. So serve se espacamento > 3x o custo de
    ida e volta (2 x taxa), senao a taxa come o lucro do ciclo.
    """
    scored = []
    for t in _usdt_universe(tickers, min_quote_volume):
        high, low, last = float(t["highPrice"]), float(t["lowPrice"]), float(t["lastPrice"])
        bid, ask = float(t.get("bidPrice", 0)), float(t.get("askPrice", 0))
        if last <= 0 or low <= 0:
            continue
        range_pct = (high - low) / last * 100
        spacing_pct = range_pct / levels
        spread_pct = (ask - bid) / last * 100 if ask and bid else 0.0
        round_trip_cost_pct = 2 * fee_per_side * 100
        if spread_pct > max_spread_pct or spacing_pct < 3 * round_trip_cost_pct:
            continue
        net_per_cycle = order_size_usdt * (spacing_pct - round_trip_cost_pct) / 100
        scored.append(
            {
                "symbol": t["symbol"],
                "range_24h_pct": round(range_pct, 2),
                "spacing_pct": round(spacing_pct, 3),
                "spread_pct": round(spread_pct, 4),
                "net_usdt_per_cycle": round(net_per_cycle, 4),
            }
        )
    scored.sort(key=lambda r: r["net_usdt_per_cycle"], reverse=True)
    return scored[:top]


def allow_new_buys(regime: Regime, symbol_trend_down: bool) -> tuple[bool, str]:
    """Porta unica: devolve (pode_comprar, motivo). Vendas e retentativas NUNCA passam por aqui."""
    if regime.label == "RISK_OFF":
        return False, f"mercado em RISK_OFF: {regime.reason}"
    if symbol_trend_down and regime.label != "RISK_ON":
        return False, "tendencia de queda no par e mercado sem forca"
    return True, regime.reason
