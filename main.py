import json
import logging
import os
import time
from pathlib import Path
from dotenv import load_dotenv
from scraper import CryptoFlowSignalsScraper, Filters
from executor import BinanceFuturesExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cryptoflow_bot.main")

SEEN_SIGNALS_FILE = Path(__file__).parent / "seen_signals.json"


def load_seen() -> set:
    if SEEN_SIGNALS_FILE.exists():
        return set(json.loads(SEEN_SIGNALS_FILE.read_text()))
    return set()


def save_seen(seen: set):
    SEEN_SIGNALS_FILE.write_text(json.dumps(list(seen)[-500:]))


def main():
    load_dotenv()
    scraper = CryptoFlowSignalsScraper(
        email=os.environ["CFS_EMAIL"],
        password=os.environ["CFS_PASSWORD"],
        login_url=os.environ["CFS_LOGIN_URL"],
        dashboard_url=os.environ["CFS_SIGNALS_URL"],
    )
    filters = Filters(
        min_confidence=float(os.environ.get("MIN_CONFIDENCE", "70")),
        skip_aged=os.environ.get("SKIP_AGED", "true").lower() == "true",
        skip_high_risk=os.environ.get("SKIP_HIGH_RISK", "false").lower() == "true",
        max_age_minutes=float(os.environ.get("MAX_SIGNAL_AGE_MINUTES", "25")),
        max_progress_pct=float(os.environ.get("MAX_PROGRESS_PCT", "10")),
    )
    leverage = int(os.environ.get("LEVERAGE", "4"))
    margin_usdt = float(os.environ.get("MARGIN_USDT", "24"))
    poll_seconds = int(os.environ.get("POLL_INTERVAL_SECONDS", "15"))

    # LIVE only — no testnet, no dry-run.
    executor = BinanceFuturesExecutor(
        api_key=os.environ["BINANCE_API_KEY"],
        api_secret=os.environ["BINANCE_API_SECRET"],
    )

    seen = load_seen()
    scraper.start()
    logger.info(
        "Started LIVE. min_confidence=%s skip_aged=%s skip_high_risk=%s "
        "max_age_minutes=%s max_progress_pct=%s leverage=%s margin=%s",
        filters.min_confidence, filters.skip_aged, filters.skip_high_risk,
        filters.max_age_minutes, filters.max_progress_pct, leverage, margin_usdt,
    )

    try:
        while True:
            try:
                qualifying, seen = scraper.get_new_qualifying_signals(filters, seen)
                save_seen(seen)
                for sig in qualifying:
                    tp_prices = [p for p in (sig.tp1_price, sig.tp2_price, sig.tp3_price)
                                 if p is not None]
                    logger.info(
                        "QUALIFIES: %s %s (confidence=%s%%, age=%sm, progress=%s%%) "
                        "leverage=%s margin=%s tps=%s",
                        sig.symbol, sig.side, sig.confidence, sig.age_minutes,
                        sig.progress_to_tp3_pct, leverage, margin_usdt,
                        tp_prices or "none-parsed",
                    )
                    # place_trade returns True only when HANDLED (placed /
                    # skipped duplicate). On False (hard failure) we do NOT
                    # mark seen, so it retries next poll.
                    try:
                        handled = executor.place_trade(
                            symbol=sig.symbol,
                            side=sig.side,
                            leverage=leverage,
                            margin_usdt=margin_usdt,
                            take_profits=tp_prices,
                        )
                    except Exception as e:
                        logger.error(
                            "Binance execution raised for %s: %s — "
                            "NOT marking as seen, will retry next poll.",
                            sig.symbol, e,
                        )
                        continue

                    if handled:
                        seen.add(sig.id)
                        save_seen(seen)
                    else:
                        logger.warning(
                            "Trade for %s %s failed — left unmarked so it "
                            "retries on the next poll.", sig.symbol, sig.side,
                        )
            except Exception as e:
                logger.error("Poll failed, will retry: %s", e)
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        logger.info("Stopping...")
    finally:
        scraper.stop()


if __name__ == "__main__":
    main()
