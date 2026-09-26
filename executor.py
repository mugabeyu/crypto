"""
Places real orders on Binance USD-M Futures via the official API — entry
market order + a matching stop-loss order, using each signal's own
entry/SL prices. No browser interaction here at all; this only talks to
Binance directly.
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

    def has_open_position(self, symbol: str) -> bool:
        """True if there's already a non-zero position open for this symbol."""
        positions = self.client.futures_position_information(symbol=symbol, recvWindow=20000)
        return any(float(p["positionAmt"]) != 0 for p in positions)

    def place_trade(self, symbol: str, side: str, leverage: int, margin_usdt: float,
                     sl_price: float) -> bool:
        """
        side: "SHORT" or "LONG" (as parsed from the signal).
        Returns True if an order was placed (or would be, in dry-run),
        False if skipped as a duplicate.
        """
        entry_side = "SELL" if side == "SHORT" else "BUY"
        close_side = "BUY" if side == "SHORT" else "SELL"

        if self.has_open_position(symbol):
            logger.info(f"Skipping {symbol} {side} — already an open position on Binance.")
            return False

        # Current mark price — safer than trusting the signal card's entry
        # price, which may be stale by the time we act on it.
        mark_price = float(self.client.futures_mark_price(symbol=symbol)["markPrice"])

        # Sanity check: if price already moved past the stop, placing this
        # would trigger an instant stop-out — refuse instead.
        if side == "SHORT" and mark_price >= sl_price:
            logger.warning(
                f"Refusing {symbol} SHORT — mark price {mark_price} is already "
                f"past the stop-loss level {sl_price}."
            )
            return False
        if side == "LONG" and mark_price <= sl_price:
            logger.warning(
                f"Refusing {symbol} LONG — mark price {mark_price} is already "
                f"past the stop-loss level {sl_price}."
            )
            return False

        step_size, price_precision = self._get_step_size_and_precision(symbol)
        raw_qty = (margin_usdt * leverage) / mark_price
        quantity = self._round_step(raw_qty, step_size)
        sl_price_rounded = round(sl_price, price_precision)

        if quantity <= 0:
            logger.error(f"Computed zero quantity for {symbol} — skipping.")
            return False

        if self.dry_run:
            logger.info(
                f"[DRY RUN] Would set {leverage}x leverage, then place "
                f"{entry_side} {quantity} {symbol} (~{margin_usdt} USDT margin "
                f"@ ~{mark_price}), with a STOP_MARKET at {sl_price_rounded} "
                f"({close_side} to close)."
            )
            return True

        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage, recvWindow=20000)
        except BinanceAPIException as e:
            logger.warning(f"Could not set leverage for {symbol}: {e}")

        try:
            order = self.client.futures_create_order(
                symbol=symbol,
                side=entry_side,
                type="MARKET",
                quantity=quantity,
                recvWindow=20000,
            )
            logger.info(f"Entry order placed for {symbol}: {order}")
        except BinanceAPIException as e:
            logger.error(f"Entry order FAILED for {symbol}: {e}")
            return False

        try:
            sl_order = self.client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type="STOP_MARKET",
                stopPrice=sl_price_rounded,
                closePosition=True,
                workingType="MARK_PRICE",
                recvWindow=20000,
            )
            logger.info(f"Stop-loss order placed for {symbol} at {sl_price_rounded}: {sl_order}")
        except BinanceAPIException as e:
            logger.error(
                f"Stop-loss order FAILED for {symbol}: {e} — position is OPEN "
                f"WITHOUT A STOP LOSS. Check Binance manually right now."
            )

        return True
