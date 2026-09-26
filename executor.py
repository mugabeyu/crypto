"""
Places real LIVE orders on Binance USD-M Futures via the official API:
  - market entry order
  - N partial take-profit orders (reduceOnly, split evenly across TP1/TP2/TP3)
No stop-loss (per user request), no testnet, no dry-run: every qualifying
signal is executed live. Hedge Mode (positionSide) is auto-detected.
"""
import logging
from binance.client import Client
from binance.exceptions import BinanceAPIException

logger = logging.getLogger("cryptoflow_bot.executor")


class BinanceFuturesExecutor:
    def __init__(self, api_key: str, api_secret: str):
        # LIVE only — no testnet flag.
        self.client = Client(api_key, api_secret)
        self._step_size_cache: dict[str, float] = {}
        self._price_precision_cache: dict[str, int] = {}
        self._min_notional_cache: dict[str, float] = {}
        # None = not yet detected; True = Hedge (dual-side); False = One-way.
        self._dual_side: bool | None = None
        self._detect_position_mode()

    # ------------------------------------------------------------------
    # Position-mode handling (the -4061 fix)
    # ------------------------------------------------------------------
    def _detect_position_mode(self) -> None:
        try:
            info = self.client.futures_get_position_mode()
            self._dual_side = bool(info.get("dualSidePosition", False))
            logger.info(
                "Binance position mode detected: %s.",
                "HEDGE (dual-side) — will send positionSide on every order."
                if self._dual_side
                else "ONE-WAY — positionSide omitted (correct for this mode).",
            )
        except Exception as e:
            logger.warning(
                "Could not detect position mode (%s) — assuming one-way. "
                "If your account is in Hedge Mode, the first order will get "
                "-4061 and the bot will auto-correct and retry.",
                e,
            )
            self._dual_side = False

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
    def _get_symbol_filters(self, symbol: str) -> tuple[float, int, float]:
        """Cached (step_size, price_precision, min_notional)."""
        if symbol in self._step_size_cache:
            return (self._step_size_cache[symbol],
                    self._price_precision_cache[symbol],
                    self._min_notional_cache[symbol])
        info = self.client.futures_exchange_info()
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
        """Evenly split into n step-rounded portions; last absorbs rounding
        error so sum never exceeds quantity."""
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
    def has_open_position(self, symbol: str, signal_side: str | None = None) -> bool:
        positions = self.client.futures_position_information(symbol=symbol, recvWindow=20000)
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
    def _create_order_with_mode_fallback(self, signal_side: str, **kwargs) -> dict:
        try:
            return self.client.futures_create_order(
                **self._order_kwargs(signal_side, **kwargs)
            )
        except BinanceAPIException as e:
            if not self._is_position_side_mismatch(e):
                raise
            old = self._dual_side
            self._dual_side = not self._dual_side
            logger.warning(
                "Got -4061 position-side mismatch — flipped mode assumption "
                "%s -> %s and retrying.", old, self._dual_side,
            )
            return self.client.futures_create_order(
                **self._order_kwargs(signal_side, **kwargs)
            )

    def place_trade(self, symbol: str, side: str, leverage: int, margin_usdt: float,
                    take_profits: list[float] | None = None) -> bool:
        """
        LIVE market entry + partial reduce-only take-profits at TP1/TP2/TP3.
        No stop-loss (per config).
        Returns True if HANDLED (placed / skipped duplicate). Returns False
        only on a hard entry failure so main.py can retry next poll.
        """
        entry_side = "SELL" if side == "SHORT" else "BUY"
        close_side = "BUY" if side == "SHORT" else "SELL"

        if self.has_open_position(symbol, signal_side=side):
            logger.info(
                "Skipping %s %s — matching position already open on Binance.",
                symbol, side,
            )
            return True

        mark_price = float(self.client.futures_mark_price(symbol=symbol)["markPrice"])
        step_size, price_precision, min_notional = self._get_symbol_filters(symbol)
        raw_qty = (margin_usdt * leverage) / mark_price
        quantity = self._round_step(raw_qty, step_size)

        if quantity <= 0:
            logger.error("Computed zero quantity for %s — skipping.", symbol)
            return True

        # Validate TP direction: SHORT profits when price falls → TP < mark.
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

        try:
            self.client.futures_change_leverage(
                symbol=symbol, leverage=leverage, recvWindow=20000,
            )
        except BinanceAPIException as e:
            logger.warning("Could not set leverage for %s: %s", symbol, e)

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
            logger.info(
                "LIVE entry placed for %s %s: qty=%s @ ~%s — %s",
                symbol, side, quantity, mark_price, order,
            )
        except BinanceAPIException as e:
            logger.error("Entry order FAILED for %s: %s", symbol, e)
            return False  # hard failure — retry next poll

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
                    tp_order = self._create_order_with_mode_fallback(
                        signal_side=side,
                        symbol=symbol,
                        side=close_side,
                        type="TAKE_PROFIT_MARKET",
                        stopPrice=tp_r,
                        quantity=qty_portion,
                        reduceOnly=True,
                        workingType="MARK_PRICE",
                        recvWindow=20000,
                    )
                    logger.info(
                        "Take-profit placed for %s at %s (qty %s, reduce-only): %s",
                        symbol, tp_r, qty_portion, tp_order,
                    )
                except BinanceAPIException as e:
                    logger.warning(
                        "Take-profit at %s FAILED for %s: %s", tp_r, symbol, e,
                    )
        else:
            logger.info("No valid TP levels for %s — entry only.", symbol)

        # NOTE: No stop-loss is placed by design.
        return True
