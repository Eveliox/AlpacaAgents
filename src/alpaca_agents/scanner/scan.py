"""Layer 1 orchestration: filters -> signals -> structures -> Trade Idea JSON.

Emits the exact idea shape rules.evaluate() validates. Extra keys (`playbook`,
`exit_plan`, `session`) are informational for logging/dashboard and are ignored
by the rules engine. Nothing here talks to a broker or an LLM.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from .contracts import DTE_MAX, DTE_MIN, OptionQuote, select_debit_spread, select_long
from .indicators import Bar, atr
from .signals import Signal, Skip, breakout_signal, oversold_bounce_signal, trend_signal

PLAYBOOKS = ("trend_directional", "trend_debit_spread", "oversold_bounce", "breakout_continuation")
# Owner-drafted single idea (python -m alpaca_agents.manual). Not a scanner playbook: it never
# appears in shadow scans, but it needs the same approval marker and --enable-playbook as any other.
MANUAL_PLAYBOOK = "manual"
INDEX_ETFS = frozenset({"SPY", "QQQ", "IWM", "DIA"})
# Funds with no earnings report. Explicit allowlist: an unknown symbol is a stock until proven
# otherwise and needs a verified earnings date. Leveraged/inverse products are deliberately absent.
NO_EARNINGS_ETFS = INDEX_ETFS | frozenset({
    "TLT", "IEF", "HYG", "LQD", "GLD", "SLV", "USO", "GDX", "EEM", "EFA", "EWZ", "EWJ", "FXI", "KWEB",
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLP", "XLU", "XLB", "XLY", "XLRE", "XLC", "KRE", "XBI", "SMH", "ARKK", "VXX",
})


def earnings_exempt(symbol: str) -> bool:
    return symbol in NO_EARNINGS_ETFS
IV_RANK_PREFER_SPREAD = 50.0
CENT = Decimal("0.01")


@dataclass(frozen=True)
class SymbolSnapshot:
    symbol: str
    bars: tuple                 # ascending Bar objects, last = most recent completed session
    chain: tuple                # OptionQuote objects
    next_earnings: date | None  # None = unknown unless explicitly inapplicable for a supported ETF
    iv_rank: float | None = None
    earnings_not_applicable: bool = False
    valuation_day: date | None = None  # Quote/DTE date; bars may end on the preceding session.


@dataclass(frozen=True)
class ScanConfig:
    universe: frozenset = frozenset({"SPY", "QQQ", "IWM"})
    enabled_playbooks: frozenset = frozenset()      # nothing proposes until backtested + enabled
    fee_per_contract: Decimal = Decimal("0.65")     # placeholder estimate, not a verified broker fee
    max_risk: Decimal = Decimal("100")
    max_open_positions: int = 2


@dataclass
class ScanResult:
    proposals: list = field(default_factory=list)   # enabled playbooks, portfolio-filtered
    shadow: list = field(default_factory=list)      # every candidate idea, for per-playbook stats
    skipped: list = field(default_factory=list)     # {symbol, playbook, reason}


def correlation_bucket(symbol: str) -> str:
    return "index_etf" if symbol in INDEX_ETFS else symbol


def _cents(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _exit_plan(playbook: str, structure) -> dict:
    if structure.strategy.endswith("spread"):
        plan = {"target_pct_max_profit": 50, "debit_stop_pct": 50, "time_stop_dte": 14,
                "max_profit": str(structure.max_profit * 100)}
    else:
        plan = {"premium_stop_pct": 50, "time_stop_dte": 21}
    rules = {"trend_directional": "close back through EMA20 against position",
             "trend_debit_spread": "close back through EMA20 against position",
             "oversold_bounce": "close below recent swing low",
             "breakout_continuation": "close back inside prior range",
             MANUAL_PLAYBOOK: "none: fixed underlying stop/target only"}
    plan["underlying_stop_rule"] = rules[playbook]
    if playbook == "oversold_bounce":
        plan["time_stop_sessions"] = 10
    return plan


def _build_idea(symbol, playbook, signal: Signal, structure, config: ScanConfig, as_of: date, preferred: bool):
    entry, stop, target = _cents(signal.entry), _cents(signal.stop), _cents(signal.target)
    if not ((signal.direction == "long" and stop < entry < target) or (signal.direction == "short" and target < entry < stop)):
        return f"levels collapsed after cent rounding"
    rr = (abs(target - entry) / abs(entry - stop)).quantize(CENT, rounding=ROUND_HALF_UP)
    if rr < 1:
        return f"reward:risk {rr} below 1:1 after rounding"
    fees = config.fee_per_contract * len(structure.legs)
    cost = structure.limit_debit * 100
    if cost + fees > config.max_risk:
        return f"max loss {cost + fees} exceeds {config.max_risk} premium cap"
    score = 40 * signal.strength + 30 * min(float(rr), 2.0) / 2.0 + 20 + (10 if preferred else 0)
    legs = []
    for i, quote in enumerate(structure.legs):
        legs.append({"symbol": symbol, "expiration": quote.expiration.isoformat(), "strike": str(quote.strike),
                     "right": quote.right, "side": "buy" if i == 0 else "sell", "ratio": 1, "multiplier": 100})
    return {
        "symbol": symbol, "action": "open", "holding_style": "swing", "strategy": structure.strategy,
        "direction": signal.direction, "entry_trigger": str(entry), "stop": str(stop), "target": str(target),
        "reward_risk": str(rr), "quantity": 1, "limit_debit": str(structure.limit_debit),
        "est_contract_cost": str(cost), "estimated_fees": str(fees), "score": round(score),
        "thesis": signal.thesis, "legs": legs,
        "playbook": playbook, "exit_plan": _exit_plan(playbook, structure), "session": as_of.isoformat(),
    }


MANUAL_STOP_ATR, MANUAL_TARGET_ATR = 1.0, 1.5


def build_manual_idea(snap: SymbolSnapshot, right: str, config: ScanConfig, *, as_of: date, contract_day: date, note: str):
    """Owner-requested idea with NO signal: levels are fixed ATR14 multiples from the last close.

    Goes through the same _build_idea as the scanner so the premium cap, cent rounding and
    reward:risk floor apply before Moon ever sees it. Returns an idea dict or a refusal string.
    """
    if right not in ("call", "put"):
        return "right must be call or put"
    if not isinstance(note, str) or not note.strip():
        return "a note explaining why is required"
    structure = select_long(snap.chain, right, contract_day)
    if structure is None:
        return f"no liquid {DTE_MIN}-{DTE_MAX} DTE {right} near 0.40 delta"
    bars = snap.bars
    if not bars or bars[-1].day != as_of:
        return "bars not current for session"
    close, band = bars[-1].close, atr(bars, 14)
    if band <= 0:
        return "ATR14 not positive"
    direction = "long" if right == "call" else "short"
    sign = 1 if direction == "long" else -1
    signal = Signal(MANUAL_PLAYBOOK, direction, close, close - sign * MANUAL_STOP_ATR * band, close + sign * MANUAL_TARGET_ATR * band, 0.5,
                    f"MANUAL (no signal): {note.strip()[:200]}; stop {MANUAL_STOP_ATR}xATR14, target {MANUAL_TARGET_ATR}xATR14 from close {close:.2f}")
    idea = _build_idea(snap.symbol, MANUAL_PLAYBOOK, signal, structure, config, as_of, False)
    if isinstance(idea, dict):
        idea["valuation_day"] = contract_day.isoformat()
    return idea


def _candidates(snap: SymbolSnapshot, config: ScanConfig, as_of: date, skipped: list):
    """All ideas one symbol could produce, across playbooks."""
    right_for = {"long": "call", "short": "put"}
    out = []

    def note(playbook, reason):
        skipped.append({"symbol": snap.symbol, "playbook": playbook, "reason": reason})

    def attach(playbook, signal, structure, preferred):
        if structure is None:
            return note(playbook, "no liquid contract in 30-45 DTE / delta band (or spread quality gate failed)")
        if snap.next_earnings is not None and snap.next_earnings <= structure.expiration:
            return note(playbook, f"earnings {snap.next_earnings} before expiration {structure.expiration}")
        idea = _build_idea(snap.symbol, playbook, signal, structure, config, as_of, preferred)
        if isinstance(idea, str):
            return note(playbook, idea)
        idea["valuation_day"] = contract_day.isoformat()
        out.append(idea)

    contract_day = snap.valuation_day if snap.valuation_day is not None else as_of
    trend = trend_signal(snap.bars)
    if isinstance(trend, Skip):
        note("trend_directional", trend.reason)
        note("trend_debit_spread", trend.reason)
    else:
        right = right_for[trend.direction]
        prefer_spread = snap.iv_rank is None or snap.iv_rank >= IV_RANK_PREFER_SPREAD
        attach("trend_directional", trend, select_long(snap.chain, right, contract_day), not prefer_spread)
        attach("trend_debit_spread", trend, select_debit_spread(snap.chain, right, contract_day, config.max_risk), prefer_spread)

    bounce = oversold_bounce_signal(snap.bars)
    if isinstance(bounce, Skip):
        note("oversold_bounce", bounce.reason)
    else:
        attach("oversold_bounce", bounce, select_long(snap.chain, "call", contract_day), True)

    breakout = breakout_signal(snap.bars)
    if isinstance(breakout, Skip):
        note("breakout_continuation", breakout.reason)
    else:
        attach("breakout_continuation", breakout, select_debit_spread(snap.chain, "call", contract_day, config.max_risk), True)
    return out


def scan(snapshots, config: ScanConfig, *, as_of: date, open_symbols=frozenset()) -> ScanResult:
    """open_symbols: symbols with an open position or pending idea (from the executor's state)."""
    unknown = set(config.enabled_playbooks) - set(PLAYBOOKS)
    if unknown:
        raise ValueError(f"unknown playbooks enabled: {sorted(unknown)}")
    result = ScanResult()
    per_symbol_best = []
    for snap in snapshots:
        sym = snap.symbol
        if sym not in config.universe:
            result.skipped.append({"symbol": sym, "playbook": "*", "reason": "outside liquid universe"})
            continue
        if sym in open_symbols:
            result.skipped.append({"symbol": sym, "playbook": "*", "reason": "one open idea per symbol"})
            continue
        if (type(snap.earnings_not_applicable) is not bool
                or (snap.earnings_not_applicable and (not earnings_exempt(sym) or snap.next_earnings is not None))):
            result.skipped.append({"symbol": sym, "playbook": "*", "reason": "invalid earnings exemption"})
            continue
        if snap.next_earnings is None and not snap.earnings_not_applicable:
            result.skipped.append({"symbol": sym, "playbook": "*", "reason": "earnings date unknown; fail closed"})
            continue
        if ((snap.next_earnings is not None and type(snap.next_earnings) is not date)
                or (snap.valuation_day is not None and (type(snap.valuation_day) is not date or snap.valuation_day < as_of))):
            result.skipped.append({"symbol": sym, "playbook": "*", "reason": "invalid snapshot dates"})
            continue
        if not snap.bars or snap.bars[-1].day != as_of:
            result.skipped.append({"symbol": sym, "playbook": "*", "reason": "bars not current for session"})
            continue
        try:
            candidates = _candidates(snap, config, as_of, result.skipped)
        except ValueError as exc:
            result.skipped.append({"symbol": sym, "playbook": "*", "reason": f"bad market data: {exc}"})
            continue
        result.shadow.extend(candidates)
        enabled = [c for c in candidates if c["playbook"] in config.enabled_playbooks]
        for c in candidates:
            if c["playbook"] not in config.enabled_playbooks:
                result.skipped.append({"symbol": sym, "playbook": c["playbook"], "reason": "playbook disabled (shadow only until backtested)"})
        if enabled:
            best = max(enabled, key=lambda c: (c["score"], c["playbook"]))
            per_symbol_best.append(best)
            for c in enabled:
                if c is not best:
                    result.skipped.append({"symbol": sym, "playbook": c["playbook"], "reason": f"lower score than {best['playbook']} on same symbol"})

    held_buckets = {correlation_bucket(s) for s in open_symbols}
    slots = max(0, config.max_open_positions - len(open_symbols))
    for idea in sorted(per_symbol_best, key=lambda c: (-c["score"], c["symbol"])):
        bucket = correlation_bucket(idea["symbol"])
        if bucket in held_buckets:
            result.skipped.append({"symbol": idea["symbol"], "playbook": idea["playbook"], "reason": f"correlated with held/proposed {bucket} exposure"})
            continue
        if len(result.proposals) >= slots:
            result.skipped.append({"symbol": idea["symbol"], "playbook": idea["playbook"], "reason": "no free position slot"})
            continue
        held_buckets.add(bucket)
        result.proposals.append(idea)
    return result
