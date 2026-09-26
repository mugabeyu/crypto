"""
Places real orders on Binance USD-M Futures via the official API — entry
market order + a matching stop-loss order, using each signal's own
entry/SL prices. No browser interaction here at all; this only talks to
Binance directly.

FIXED (2026-09-26):
- Auto-detects Hedge Mode (dualSidePosition) vs One-way Mode at startup,
  and includes the required `positionSide` param in hedge mode. This is
  what caused APIError -4061 "Order's position side does not match user's
  setting."
- If detection is wrong/impossible, a -4061 rejection flips the assumption
  and retries the order once, so the bot adapts at runtime.
- has_open_position() is now side-aware in hedge mode.
- place_trade() now returns True for "handled" (placed / dry-run / skipped
  duplicate) and False only for a hard failure that should be retried.
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
        # None = not yet detected; True = Hedge (dual-side); False = One-way.
        self._dual_side: bool | None = None
        self._detect_position_mode()

    # ------------------------------------------------------------------
    # Position-mode handling (the -4061 fix)
    # ------------------------------------------------------------------
    def _detect_position_mode(self) -> None:
        """Query Binance for dualSidePosition and cache it. Safe to fail."""
        if self.dry_run:
            # Can't rely on the key in dry run; assume one-way but runtime
            # -4061 fallback below will correct it if needed.
            self._dual_side = False
            logger.info("DRY RUN: assuming one-way position mode until proven otherwise.")
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
    def _get_step_size_and_precision(self, symbol: str) -> tuple[float, int]:
        """Cached per-symbol quantity step size and price decimal precision."""
        if symbol in self._step_size_cache:
            return self._step_size_cache[symbol], self._price_precision_cache[symbol]
        info = self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                step_size = next(
                    float(f["stepSize"]) for f in s["filters"] if f["filterType"] == "LOT_SIZE"
                )
                price_precision = s["pricePrecision"]
                self._step_size_cache[symbol] = step_size
                self._price_precision_cache[symbol] = price_precision
                return step_size, price_precision
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
        """True if there's already a non-zero position for this symbol.
        In hedge mode, only the matching side counts (so an open LONG does
        not block a new SHORT, and vice versa)."""
        positions = self.client.futures_position_information(symbol=symbol, recvWindow=20000)
        for p in positions:
            amt = float(p.get("positionAmt", 0) or 0)
            if amt == 0:
                continue
            if self._dual_side and signal_side:
                if p.get("positionSide") == signal_side:
                    return True
            else:
                # One-way: any non-zero amount means the symbol is occupied.
                return True
        return False

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------
    def _create_order_with_mode_fallback(self, signal_side: str, **kwargs) -> dict:
        """futures_create_order with a one-shot -4061 hedge/one-way toggle."""
        try:
            return self.client.futures_create_order(
                **self._order_kwargs(signal_side, **kwargs)
            )
        except BinanceAPIException as e:
            if not self._is_position_side_mismatch(e):
                raise
            # Runtime correction: flip the assumption and retry exactly once.
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
                    sl_price: float) -> bool:
        """
        side: "SHORT" or "LONG" (as parsed from the signal).
        Returns True if the signal is HANDLED (order placed, dry-run logged,
        or skipped as a duplicate) — i.e. safe to mark seen.
        Returns False only on a hard failure so main.py can retry next poll.
        """
        entry_side = "SELL" if side == "SHORT" else "BUY"
        close_side = "BUY" if side == "SHORT" else "SELL"

        if self.has_open_position(symbol, signal_side=side):
            logger.info(
                "Skipping %s %s — matching position already open on Binance.",
                symbol, side,
            )
            return True  # handled — don't spam retries

        # Current mark price — safer than trusting the signal card's entry
        # price, which may be stale by the time we act on it.
        mark_price = float(self.client.futures_mark_price(symbol=symbol)["markPrice"])

        # Sanity check: if price already moved past the stop, placing this
        # would trigger an instant stop-out — refuse instead.
        if side == "SHORT" and mark_price >= sl_price:
            logger.warning(
                "Refusing %s SHORT — mark price %s is already past the "
                "stop-loss level %s.", symbol, mark_price, sl_price,
            )
            return True  # signal is dead — handled, don't retry
        if side == "LONG" and mark_price <= sl_price:
            logger.warning(
                "Refusing %s LONG — mark price %s is already past the "
                "stop-loss level %s.", symbol, mark_price, sl_price,
            )
            return True

        step_size, price_precision = self._get_step_size_and_precision(symbol)
        raw_qty = (margin_usdt * leverage) / mark_price
        quantity = self._round_step(raw_qty, step_size)
        sl_price_rounded = round(sl_price, price_precision)

        if quantity <= 0:
            logger.error("Computed zero quantity for %s — skipping.", symbol)
            return True  # will never be valid for this symbol — handled

        if self.dry_run:
            logger.info(
                "[DRY RUN] Would set %sx leverage, then place %s %s %s "
                "(~%s USDT margin @ ~%s), with a STOP_MARKET at %s (%s to close).%s",
                leverage, entry_side, quantity, symbol, margin_usdt, mark_price,
                sl_price_rounded, close_side,
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

        # --- Stop loss (closes the position) ---
        # In hedge mode, closePosition=True still requires positionSide,
        # which _order_kwargs attaches. closePosition orders must NOT send
        # quantity — we don't.
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
            # Entry succeeded — do NOT retry (would double the position).
        return True
