"""
Places real orders on Binance USD-M Futures via the official API:
  - market entry order
  - one stop-loss (closePosition, closes whatever remains)
  - N partial take-profit orders (reduceOnly, split evenly across TP1/TP2/TP3)
using each signal's own entry/SL/TP prices. No browser interaction here.

FIXED v5 (2026-09-26):
- v1: auto-detects Hedge Mode (dualSidePosition) vs One-way, sends
  positionSide on every order (fixes -4061), runtime -4061 retry fallback,
  side-aware has_open_position, True=handled / False=retry semantics.
- v2: partial take-profit orders at TP1/TP2/TP3.
- v5: position-mode detection is now LAZY — it makes ZERO Binance API calls
  at startup. Detection runs only when the first qualifying signal is about
  to be traded, so idle polling never contributes to rate-limit bans.
"""
import logging
from binance.client import Client
from binance.exceptions import BinanceAPIException

logger = logging.getLogger("cryptoflow_bot.executor")


class BinanceFuturesExecutor:
    def __init__(self, api_key: str, api_secret: str, testnet: bool, dry_run: bool):
        self.client = Client(api_key, api_secret, testnet=testnet)
        self.dry_run = dry_run
        self._step_size_cache: dict[str, float] = {}
        self._price_precision_cache: dict[str, int] = {}
        self._min_notional_cache: dict[str, float] = {}
        # None = not yet detected; True = Hedge (dual-side); False = One-way.
        # Lazy — detected on first real trade attempt (see _ensure_position_mode).
        self._dual_side: bool | None = None

    # ------------------------------------------------------------------
    # Position-mode handling (the -4061 fix) — LAZY
    # ------------------------------------------------------------------
    def _ensure_position_mode(self) -> None:
        """Detect dualSidePosition once, lazily, before the first real order.
        No-op in dry run or if already detected."""
        if self.dry_run or self._dual_side is not None:
            return
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
        """Attach positionSide when in hedge mode. signal_side is 'LONG'/'SHORT'."""
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

    def _split_into_portions(self, quantity: float, n: int, step_size: float) -> list[float]:
        """Evenly split quantity into n step-rounded portions; the last
        portion absorbs rounding error so their sum never exceeds quantity."""
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

    def place_trade(self, symbol: str, side: str, leverage: int, margin_usdt: float,
                    sl_price: float, take_profits: list[float] | None = None) -> bool:
        """
        side: "SHORT" or "LONG" (as parsed from the signal).
        take_profits: list of TP prices (TP1..TP3). Partial closes split
        evenly across the valid ones.
        Returns True if HANDLED (placed / dry-run / skipped / dead signal).
        Returns False only on a hard entry failure so main.py can retry.
        """
        # Lazy: first Binance call of the whole run happens here, only when a
        # qualifying signal actually exists. Zero calls while just polling.
        self._ensure_position_mode()

        entry_side = "SELL" if side == "SHORT" else "BUY"
        close_side = "BUY" if side == "SHORT" else "SELL"

        if self.has_open_position(symbol, signal_side=side):
            logger.info(
                "Skipping %s %s — matching position already open on Binance.",
                symbol, side,
            )
            return True

        mark_price = float(self.client.futures_mark_price(symbol=symbol)["markPrice"])

        # Sanity: price already past the stop → instant stop-out → refuse.
        if side == "SHORT" and mark_price >= sl_price:
            logger.warning(
                "Refusing %s SHORT — mark price %s already past SL %s.",
                symbol, mark_price, sl_price,
            )
            return True
        if side == "LONG" and mark_price <= sl_price:
            logger.warning(
                "Refusing %s LONG — mark price %s already past SL %s.",
                symbol, mark_price, sl_price,
            )
            return True

        step_size, price_precision, min_notional = self._get_symbol_filters(symbol)
        raw_qty = (margin_usdt * leverage) / mark_price
        quantity = self._round_step(raw_qty, step_size)
        sl_price_rounded = round(sl_price, price_precision)

        if quantity <= 0:
            logger.error("Computed zero quantity for %s — skipping.", symbol)
            return True

        # --- Validate / sanitize TP levels ---
        valid_tps: list[float] = []
        for tp in (take_profits or []):
            if tp is None:
                continue
            tp_r = round(tp, price_precision)
            # Direction sanity: SHORT profits when price falls → TP < mark.
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

        if self.dry_run:
            tp_desc = ", ".join(f"{p}" for p in valid_tps) if valid_tps else "none parsed"
            logger.info(
                "[DRY RUN] Would set %sx leverage, place %s %s %s (~%s USDT @ ~%s), "
                "SL STOP_MARKET at %s (%s to close), TPs at [%s] (partial reduce-only).%s",
                leverage, entry_side, quantity, symbol, margin_usdt, mark_price,
                sl_price_rounded, close_side, tp_desc,
                " [positionSide=%s]" % side if self._dual_side else "",
            )
            return True

        try:
            self.client.futures_change_leverage(
                symbol=symbol, leverage=leverage, recvWindow=20000,
            )
        except BinanceAPIException as e:
            logger.warning("Could not set leverage for %s: %s", symbol, e)

        # --- Entry (market) ---
        try:
            order = self._create_order_with_mode_fallback(
                signal_side=side,
                symbol=symbol,
                side=entry_side,
                type="MARKET",
                quantity=quantity,
                recvWindow=20000,
            )
            logger.info("Entry order placed for %s: %s", symbol, order)
        except BinanceAPIException as e:
            logger.error("Entry order FAILED for %s: %s", symbol, e)
            return False  # hard failure — retry next poll

        # --- Stop loss (closes whatever remains) ---
        try:
            sl_order = self._create_order_with_mode_fallback(
                signal_side=side,
                symbol=symbol,
                side=close_side,
                type="STOP_MARKET",
                stopPrice=sl_price_rounded,
                closePosition=True,
                workingType="MARK_PRICE",
                recvWindow=20000,
            )
            logger.info(
                "Stop-loss order placed for %s at %s: %s",
                symbol, sl_price_rounded, sl_order,
            )
        except BinanceAPIException as e:
            logger.error(
                "Stop-loss order FAILED for %s: %s — position is OPEN "
                "WITHOUT A STOP LOSS. Check Binance manually right now.",
                symbol, e,
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
                        stopPrice=tp_r,
                        quantity=qty_portion,
                        workingType="MARK_PRICE",
                        recvWindow=20000,
                    )
                    # Binance rejects reduceOnly when positionSide is also
                    # sent (Hedge Mode) — positionSide already makes it
                    # redundant there. One-way mode still needs it explicitly.
                    if not self._dual_side:
                        tp_kwargs["reduceOnly"] = True
                    tp_order = self._create_order_with_mode_fallback(
                        signal_side=side, **tp_kwargs
                    )
                    logger.info(
                        "Take-profit placed for %s at %s (qty %s, reduce-only): %s",
                        symbol, tp_r, qty_portion, tp_order,
                    )
                except BinanceAPIException as e:
                    logger.warning(
                        "Take-profit at %s FAILED for %s: %s — SL still active.",
                        tp_r, symbol, e,
                    )
        else:
            logger.info("No valid TP levels for %s — entry+SL only.", symbol)

        return True
