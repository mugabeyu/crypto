"""
Places real LIVE orders on Binance USD-M Futures via the official API:
  - market entry order
  - N partial take-profit orders (reduceOnly, split evenly across TP1/TP2/TP3)
No stop-loss (per user request), no testnet, no dry-run.

BINANCE ALGO ORDER MIGRATION (2025-12-09):
Binance moved conditional order types (STOP_MARKET, TAKE_PROFIT_MARKET,
etc.) to a separate endpoint (/fapi/v1/algoOrder) on 2025-12-09. The old
/fapi/v1/order endpoint now hard-rejects them with -4120 ("Order type
not supported for this endpoint. Please use the Algo Order API
endpoints instead."). Take-profit orders here go through
futures_create_algo_order() instead of futures_create_order() — this
requires python-binance >=1.0.36 (pin in requirements.txt), and that
endpoint renamed the price parameter from stopPrice to triggerPrice.
The regular MARKET entry order is unaffected — only conditional/trigger
order types moved.

RATE-LIMIT PROTECTION (circuit breaker):
- Position-mode detection is LAZY — zero Binance calls at startup, only when
  a qualifying signal is about to be traded.
- Every Binance call goes through _call(), which on -1003 ("IP banned")
  parses the ban-until timestamp from the error message and sets a cooldown.
- While in cooldown, place_trade() returns immediately (no Binance calls)
  so the bot can never hammer Binance and extend a ban. Backoff doubles on
  each ban (60s -> 120s -> 240s ... cap 1h) and resets on any success.
- This protects ALL callers (main.py loop, manual_trade.py, etc.) because
  the breaker lives in the executor itself.
"""
import logging
import re
import time
from binance.client import Client
from binance.exceptions import BinanceAPIException

logger = logging.getLogger("cryptoflow_bot.executor")

_BAN_UNTIL_RE = re.compile(r"banned until (\d+)")


class BinanceFuturesExecutor:
    def __init__(self, api_key: str, api_secret: str):
        # LIVE only — no testnet flag.
        self.client = Client(api_key, api_secret)
        self._step_size_cache: dict[str, float] = {}
        self._price_precision_cache: dict[str, int] = {}
        self._min_notional_cache: dict[str, float] = {}
        # None = not yet detected; True = Hedge (dual-side); False = One-way.
        # LAZY — detected only when a real trade is about to be placed.
        self._dual_side: bool | None = None
        # Circuit-breaker state.
        self._cooldown_until: float = 0.0
        self._backoff_seconds: float = 60.0

    # ------------------------------------------------------------------
    # Rate-limit circuit breaker
    # ------------------------------------------------------------------
    def _in_cooldown(self) -> bool:
        return time.time() < self._cooldown_until

    def _apply_ban_cooldown(self, error_msg: str) -> None:
        """On -1003, set cooldown to max(backoff, ban-until). Doubles backoff."""
        now = time.time()
        cooldown_until = now + self._backoff_seconds
        m = _BAN_UNTIL_RE.search(error_msg or "")
        if m:
            try:
                ban_until_s = int(m.group(1)) / 1000.0
                if ban_until_s > cooldown_until:
                    cooldown_until = ban_until_s
            except ValueError:
                pass
        self._cooldown_until = cooldown_until
        wait = int(cooldown_until - now)
        logger.warning(
            "Binance -1003 rate-limit ban — entering cooldown for ~%ds (until "
            "epoch %.0f). No Binance calls will be made until then. "
            "Next backoff: %ds.",
            max(wait, 0), cooldown_until, min(self._backoff_seconds * 2, 3600),
        )
        self._backoff_seconds = min(self._backoff_seconds * 2, 3600.0)

    def _call(self, method_name: str, *args, **kwargs):
        """Call a Binance client method. Returns None on -1003 (cooldown set);
        resets backoff on success; re-raises any other API error."""
        try:
            result = getattr(self.client, method_name)(*args, **kwargs)
            self._backoff_seconds = 60.0  # any success resets the backoff
            return result
        except BinanceAPIException as e:
            if getattr(e, "code", None) == -1003:
                self._apply_ban_cooldown(str(e))
                return None
            raise

    # ------------------------------------------------------------------
    # Position-mode handling (the -4061 fix) — LAZY
    # ------------------------------------------------------------------
    def _ensure_position_mode(self) -> bool:
        """Detect dualSidePosition once, lazily. Returns False if banned."""
        if self._dual_side is not None:
            return True
        info = self._call("futures_get_position_mode")
        if info is None:
            return False  # rate-limited — cooldown set, skip this signal
        self._dual_side = bool(info.get("dualSidePosition", False))
        logger.info(
            "Binance position mode detected: %s.",
            "HEDGE (dual-side) — will send positionSide on every order."
            if self._dual_side
            else "ONE-WAY — positionSide omitted (correct for this mode).",
        )
        return True

    def _order_kwargs(self, signal_side: str, **base) -> dict:
        if self._dual_side:
            base["positionSide"] = signal_side
        return base

    @staticmethod
    def _is_position_side_mismatch(exc: BinanceAPIException) -> bool:
        return getattr(exc, "code", None) == -4061

    # ------------------------------------------------------------------
    # Symbol metadata / rounding
    # ------------------------------------------------------------------
    def _get_symbol_filters(self, symbol: str):
        """Cached (step_size, price_precision, min_notional). None if banned."""
        if symbol in self._step_size_cache:
            return (self._step_size_cache[symbol],
                    self._price_precision_cache[symbol],
                    self._min_notional_cache[symbol])
        info = self._call("futures_exchange_info")
        if info is None:
            return None
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                step_size = next(
                    float(f["stepSize"]) for f in s["filters"] if f["filterType"] == "LOT_SIZE"
                )
                price_precision = s["pricePrecision"]
                min_notional = next(
                    (float(f.get("notional", 5.0)) for f in s["filters"]
                     if f["filterType"] == "MIN_NOTIONAL"),
                    5.0,
                )
                self._step_size_cache[symbol] = step_size
                self._price_precision_cache[symbol] = price_precision
                self._min_notional_cache[symbol] = min_notional
                return step_size, price_precision, min_notional
        raise RuntimeError(f"Symbol {symbol} not found in futures_exchange_info().")

    def _round_step(self, quantity: float, step_size: float) -> float:
        if step_size >= 1:
            return float(int(quantity / step_size) * step_size)
        precision = len(str(step_size).rstrip("0").split(".")[-1])
        factor = 10 ** precision
        return int(quantity * factor) / factor

    def _split_into_portions(self, quantity: float, n: int, step_size: float) -> list[float]:
        portions = []
        remaining = quantity
        for i in range(n):
            if i < n - 1:
                p = self._round_step(quantity / n, step_size)
            else:
                p = self._round_step(remaining, step_size)
            p = max(p, 0.0)
            portions.append(p)
            remaining -= p
        return portions

    # ------------------------------------------------------------------
    # Position checks
    # ------------------------------------------------------------------
    def has_open_position(self, symbol: str, signal_side: str | None = None):
        """True/False, or None if rate-limited."""
        positions = self._call("futures_position_information", symbol=symbol, recvWindow=20000)
        if positions is None:
            return None
        for p in positions:
            amt = float(p.get("positionAmt", 0) or 0)
            if amt == 0:
                continue
            if self._dual_side and signal_side:
                if p.get("positionSide") == signal_side:
                    return True
            else:
                return True
        return False

    # ------------------------------------------------------------------
    # Order placement with -4061 fallback
    # ------------------------------------------------------------------
    def _create_order_with_mode_fallback(self, signal_side: str, **kwargs):
        """Returns order dict, None if rate-limited (-1003)."""
        try:
            result = self._call("futures_create_order",
                                **self._order_kwargs(signal_side, **kwargs))
            return result  # dict or None
        except BinanceAPIException as e:
            if not self._is_position_side_mismatch(e):
                raise
            old = self._dual_side
            self._dual_side = not self._dual_side
            logger.warning(
                "Got -4061 position-side mismatch — flipped mode assumption "
                "%s -> %s and retrying.", old, self._dual_side,
            )
            return self._call("futures_create_order",
                              **self._order_kwargs(signal_side, **kwargs))

    def _create_algo_order_with_mode_fallback(self, signal_side: str, **kwargs):
        """
        Same -4061 fallback pattern as _create_order_with_mode_fallback,
        but for CONDITIONAL order types (STOP_MARKET, TAKE_PROFIT_MARKET,
        etc.), which Binance moved to a separate endpoint on 2025-12-09.
        The old /fapi/v1/order endpoint now hard-rejects these with -4120;
        python-binance >=1.0.36 exposes the new endpoint as
        futures_create_algo_order(), which also renamed stopPrice ->
        triggerPrice. Returns order dict, None if rate-limited (-1003).
        """
        try:
            result = self._call("futures_create_algo_order",
                                **self._order_kwargs(signal_side, **kwargs))
            return result
        except BinanceAPIException as e:
            if not self._is_position_side_mismatch(e):
                raise
            old = self._dual_side
            self._dual_side = not self._dual_side
            logger.warning(
                "Got -4061 position-side mismatch on algo order — flipped "
                "mode assumption %s -> %s and retrying.", old, self._dual_side,
            )
            return self._call("futures_create_algo_order",
                              **self._order_kwargs(signal_side, **kwargs))

    def place_trade(self, symbol: str, side: str, leverage: int, margin_usdt: float,
                    take_profits: list[float] | None = None) -> bool:
        """
        LIVE market entry + partial reduce-only take-profits at TP1/TP2/TP3.
        No stop-loss (per config).
        Returns True if HANDLED (placed / skipped duplicate). Returns False on
        hard failure OR rate-limit cooldown — main.py retries next poll.
        """
        # Circuit breaker: skip Binance entirely while cooldown is active.
        if self._in_cooldown():
            logger.warning(
                "Binance cooldown active until epoch %.0f — skipping %s %s "
                "execution attempt (will retry later).",
                self._cooldown_until, symbol, side,
            )
            return False

        if not self._ensure_position_mode():
            return False  # rate-limited during detection

        entry_side = "SELL" if side == "SHORT" else "BUY"
        close_side = "BUY" if side == "SHORT" else "SELL"

        already_open = self.has_open_position(symbol, signal_side=side)
        if already_open is None:
            return False  # rate-limited
        if already_open:
            logger.info(
                "Skipping %s %s — matching position already open on Binance.",
                symbol, side,
            )
            return True

        mark = self._call("futures_mark_price", symbol=symbol)
        if mark is None:
            return False
        mark_price = float(mark["markPrice"])

        filters = self._get_symbol_filters(symbol)
        if filters is None:
            return False
        step_size, price_precision, min_notional = filters

        raw_qty = (margin_usdt * leverage) / mark_price
        quantity = self._round_step(raw_qty, step_size)
        if quantity <= 0:
            logger.error("Computed zero quantity for %s — skipping.", symbol)
            return True

        # Validate TP direction: SHORT profits when price falls -> TP < mark.
        valid_tps: list[float] = []
        for tp in (take_profits or []):
            if tp is None:
                continue
            tp_r = round(tp, price_precision)
            if side == "SHORT" and tp_r >= mark_price:
                logger.warning(
                    "Skipping TP %s for %s SHORT — at/above mark %s (misparse?).",
                    tp_r, symbol, mark_price,
                )
                continue
            if side == "LONG" and tp_r <= mark_price:
                logger.warning(
                    "Skipping TP %s for %s LONG — at/below mark %s (misparse?).",
                    tp_r, symbol, mark_price,
                )
                continue
            valid_tps.append(tp_r)

        self._call("futures_change_leverage", symbol=symbol, leverage=leverage, recvWindow=20000)

        # --- LIVE entry (market) ---
        try:
            order = self._create_order_with_mode_fallback(
                signal_side=side,
                symbol=symbol,
                side=entry_side,
                type="MARKET",
                quantity=quantity,
                recvWindow=20000,
            )
        except BinanceAPIException as e:
            logger.error("Entry order FAILED for %s: %s", symbol, e)
            return False
        if order is None:
            return False  # rate-limited
        logger.info(
            "LIVE entry placed for %s %s: qty=%s @ ~%s — %s",
            symbol, side, quantity, mark_price, order,
        )

        # --- Take profits (partial reduce-only closes) ---
        if valid_tps:
            portions = self._split_into_portions(quantity, len(valid_tps), step_size)
            for tp_r, qty_portion in zip(valid_tps, portions):
                if qty_portion <= 0:
                    continue
                if qty_portion * tp_r < min_notional:
                    logger.warning(
                        "Skipping TP %s portion %s for %s — below min notional %s.",
                        tp_r, qty_portion, symbol, min_notional,
                    )
                    continue
                try:
                    tp_kwargs = dict(
                        symbol=symbol,
                        side=close_side,
                        type="TAKE_PROFIT_MARKET",
                        triggerPrice=tp_r,  # new algo endpoint renamed stopPrice -> triggerPrice
                        quantity=qty_portion,
                        workingType="MARK_PRICE",
                        recvWindow=20000,
                    )
                    # Binance rejects reduceOnly when positionSide is also
                    # sent (Hedge Mode) — positionSide alone already makes
                    # it a reduce-only order there. One-way mode still
                    # needs reduceOnly explicit.
                    if not self._dual_side:
                        tp_kwargs["reduceOnly"] = True
                    # Conditional order types (TAKE_PROFIT_MARKET included)
                    # moved to Binance's separate Algo Order endpoint on
                    # 2025-12-09 — the old /fapi/v1/order endpoint now
                    # hard-rejects them with -4120.
                    tp_order = self._create_algo_order_with_mode_fallback(
                        signal_side=side, **tp_kwargs
                    )
                except BinanceAPIException as e:
                    logger.warning("Take-profit at %s FAILED for %s: %s", tp_r, symbol, e)
                    continue
                if tp_order is None:
                    logger.warning("Take-profit at %s skipped — rate-limited.", tp_r)
                    continue
                logger.info(
                    "Take-profit placed for %s at %s (qty %s, reduce-only): %s",
                    symbol, tp_r, qty_portion, tp_order,
                )
        else:
            logger.info("No valid TP levels for %s — entry only.", symbol)

        # NOTE: No stop-loss is placed by design.
        return True
