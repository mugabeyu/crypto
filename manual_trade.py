"""
Manually trigger a single trade through the exact same Binance execution
path the automated bot uses (leverage, Hedge Mode handling, stop loss,
take profits) — for when YOU decide the symbol/side/levels yourself,
rather than waiting on a parsed signal from cryptoflowsignals.com.

Usage:
    python manual_trade.py SYMBOL SHORT --sl 0.0186 --tp 0.0175 0.0170 0.0165
    python manual_trade.py SYMBOL LONG  --sl 1.20    --tp 1.35

Respects the same .env settings as main.py: BINANCE_API_KEY/SECRET,
BINANCE_TESTNET, DRY_RUN, LEVERAGE, MARGIN_USDT — override any of them
per-call with --leverage / --margin / --live.
"""

import argparse
import logging
import os

from dotenv import load_dotenv

from executor import BinanceFuturesExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cryptoflow_bot.manual_trade")


def main():
    parser = argparse.ArgumentParser(description="Manually execute one trade on Binance Futures.")
    parser.add_argument("symbol", help="e.g. QUSDT")
    parser.add_argument("side", choices=["LONG", "SHORT"])
    parser.add_argument("--sl", type=float, required=True, help="Stop-loss price (required)")
    parser.add_argument("--tp", type=float, nargs="*", default=[], help="One or more take-profit prices")
    parser.add_argument("--leverage", type=int, default=None, help="Override .env LEVERAGE")
    parser.add_argument("--margin", type=float, default=None, help="Override .env MARGIN_USDT")
    parser.add_argument(
        "--live", action="store_true",
        help="Actually place the order. Without this flag, always dry-runs "
             "regardless of your .env DRY_RUN setting — manual trades default "
             "to safe-by-default.",
    )
    args = parser.parse_args()

    load_dotenv()

    leverage = args.leverage if args.leverage is not None else int(os.environ.get("LEVERAGE", "4"))
    margin_usdt = args.margin if args.margin is not None else float(os.environ.get("MARGIN_USDT", "24"))
    dry_run = not args.live  # manual trades are dry-run unless --live is explicit

    executor = BinanceFuturesExecutor(
        api_key=os.environ["BINANCE_API_KEY"],
        api_secret=os.environ["BINANCE_API_SECRET"],
        testnet=os.environ.get("BINANCE_TESTNET", "true").lower() == "true",
        dry_run=dry_run,
    )

    logger.info(
        "Manual trade: %s %s leverage=%s margin=%s sl=%s tp=%s dry_run=%s",
        args.symbol, args.side, leverage, margin_usdt, args.sl, args.tp, dry_run,
    )
    if dry_run:
        logger.info("(Add --live to actually place this order.)")

    executor.place_trade(
        symbol=args.symbol,
        side=args.side,
        leverage=leverage,
        margin_usdt=margin_usdt,
        sl_price=args.sl,
        take_profits=args.tp,
    )


if __name__ == "__main__":
    main()
