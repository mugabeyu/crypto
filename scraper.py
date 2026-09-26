"""
Watches the SHORT ONLY tab on the cryptoflowsignals.com signals page and
returns fresh, qualifying signals for main.py to execute directly on
Binance. This file ONLY reads the page — it never clicks Execute Trade,
Precision Trade, or any order-placement button. Execution now happens
via executor.py's direct Binance API calls, which is far more reliable
than automating the site's UI.

*** ONE THING STILL NEEDS VERIFYING ***
SIGNAL_CARD_SELECTOR and SHORT_ONLY_TAB_SELECTOR below were confirmed
against your real DevTools screenshots earlier and are working (per
your logs). Login field selectors are also confirmed working. Nothing
else here needs guesswork anymore.
"""

import logging
import re
from dataclasses import dataclass
from playwright.sync_api import sync_playwright, Page

logger = logging.getLogger("cryptoflow_bot.scraper")

HEADLESS = False

# Confirmed working: each signal card has a unique id like
# "signal-row-2045a1f4-90ea-4215-9de7-b643c30ec5a".
SIGNAL_CARD_SELECTOR = 'div[id^="signal-row-"]'

# Confirmed working: exact-text match avoids colliding with the same
# words inside a card's own tag row.
SHORT_ONLY_TAB_SELECTOR = 'text="SHORT ONLY"'


@dataclass
class Filters:
    min_confidence: float
    skip_aged: bool
    skip_high_risk: bool
    max_age_minutes: float | None
    max_progress_pct: float | None  # None = no limit on "Progress to TP3"


@dataclass
class Signal:
    id: str
    symbol: str
    side: str            # "LONG" or "SHORT"
    confidence: float | None
    elite: bool
    aged: bool
    high_risk: bool
    age_minutes: float | None
    progress_to_tp3_pct: float | None
    entry_price: float | None
    sl_price: float | None
    raw_text: str

    def passes(self, f: Filters) -> bool:
        if self.confidence is None or self.confidence < f.min_confidence:
            return False
        if f.skip_aged and self.aged:
            return False
        if f.skip_high_risk and self.high_risk:
            return False
        if f.max_age_minutes is not None:
            if self.age_minutes is None or self.age_minutes > f.max_age_minutes:
                return False
        if f.max_progress_pct is not None:
            if self.progress_to_tp3_pct is None or abs(self.progress_to_tp3_pct) > f.max_progress_pct:
                return False
        return True


class CryptoFlowSignalsScraper:
    def __init__(self, email: str, password: str, login_url: str, dashboard_url: str):
        self.email = email
        self.password = password
        self.login_url = login_url
        self.dashboard_url = dashboard_url
        self._playwright = None
        self._browser = None
        self._page: Page | None = None

    def start(self):
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=HEADLESS)
        self._page = self._browser.new_page()
        self._login()

    def stop(self):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def _login(self):
        page = self._page
        page.goto(self.login_url)

        # Confirmed working against the real /auth page.
        EMAIL_FIELD_SELECTOR = "input[placeholder='you@example.com']"
        PASSWORD_FIELD_SELECTOR = "input[type='password']"

        page.fill(EMAIL_FIELD_SELECTOR, self.email)
        page.fill(PASSWORD_FIELD_SELECTOR, self.password)

        submit = page.query_selector("button[type='submit']")
        if submit is None:
            matches = page.query_selector_all('text="Sign In"')
            if matches:
                submit = matches[-1]
        if submit is None:
            raise RuntimeError("Could not find the Sign In submit button.")
        submit.click()
        page.wait_for_load_state("networkidle")

        if "auth" in page.url.lower() or "login" in page.url.lower():
            raise RuntimeError(
                "Still on the login page after submitting — check your "
                "email/password in .env, or whether the site has a "
                "CAPTCHA/2FA that blocks headless login."
            )
        logger.info("Logged in to cryptoflowsignals.com")

    def _goto_short_only_signals(self):
        page = self._page
        page.goto(self.dashboard_url)
        page.wait_for_load_state("networkidle")

        tab = page.query_selector(SHORT_ONLY_TAB_SELECTOR)
        if tab:
            tab.click()
            page.wait_for_timeout(500)
        else:
            logger.warning("Could not find the SHORT ONLY filter tab.")

    def _get_cards(self):
        return self._page.query_selector_all(SIGNAL_CARD_SELECTOR)

    def _log_running_signals(self):
        cards = self._get_cards()
        running_symbols = []
        completed_count = 0

        for card in cards:
            text = card.inner_text().strip()
            if not text:
                continue
            upper = text.upper()
            if "COMPLETED WIN" in upper:
                completed_count += 1
                continue
            symbol_match = re.search(r"\b([A-Z0-9]{1,15}(?:USDT|BUSD|USD))\b", upper)
            running_symbols.append(symbol_match.group(1) if symbol_match else "UNKNOWN")

        logger.info(
            f"SHORT ONLY still running: {len(running_symbols)} "
            f"({', '.join(running_symbols) if running_symbols else 'none'}) — "
            f"{completed_count} already in Today's Completed Wins on this page."
        )

    def get_new_qualifying_signals(self, filters: Filters, seen: set) -> tuple[list[Signal], set]:
        """
        Navigates to SHORT ONLY signals and returns every new signal that
        passes `filters`. Any new signal seen but filtered out is marked
        seen so it isn't re-logged every poll. Qualifying signals are
        NOT marked seen here — main.py marks them after a real execution
        attempt, so a failed attempt can retry next poll.
        """
        self._goto_short_only_signals()
        self._log_running_signals()

        cards = self._get_cards()
        logger.info(f"Found {len(cards)} card(s) under SHORT ONLY")

        qualifying = []
        for card in cards:
            text = card.inner_text().strip()
            if not text:
                continue
            if "COMPLETED WIN" in text.upper():
                continue
            dom_id = card.get_attribute("id") or None
            sig = _parse_card(text, dom_id)
            if sig is None:
                logger.warning(f"Could not parse a card, skipping: {text[:80]!r}")
                continue
            if sig.id in seen:
                continue
            if not sig.passes(filters):
                logger.info(
                    f"Skipping {sig.symbol} {sig.side} — filtered out "
                    f"(confidence={sig.confidence}, age={sig.age_minutes}m, "
                    f"progress={sig.progress_to_tp3_pct}%, aged={sig.aged}, "
                    f"high_risk={sig.high_risk})"
                )
                seen.add(sig.id)
                continue
            qualifying.append(sig)

        return qualifying, seen


def _to_float(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _parse_age_minutes(upper_text: str) -> float | None:
    m = re.search(r"(\d+)\s*H\s*(\d+)\s*M\s*AGO", upper_text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"\b(\d+)\s*M\s*AGO", upper_text)
    if m:
        return int(m.group(1))
    return None


def _parse_card(text: str, dom_id: str | None = None) -> Signal | None:
    upper = text.upper()

    symbol_match = re.search(r"\b([A-Z0-9]{1,15}(?:USDT|BUSD|USD))\b", upper)
    if not symbol_match:
        return None
    symbol = symbol_match.group(1)

    if "SHORT" in upper and "LONG" not in upper:
        side = "SHORT"
    elif "LONG" in upper and "SHORT" not in upper:
        side = "LONG"
    else:
        return None

    conf_match = re.search(r"AI CONFIDENCE\D{0,20}?(\d{1,3})\s*%", upper, re.DOTALL)
    if not conf_match:
        conf_match = re.search(r"ELITE\s*(\d{1,3})\s*%", upper)
    confidence = float(conf_match.group(1)) if conf_match else None

    elite = "ELITE" in upper
    aged = "AGED" in upper or "AGING" in upper or "STALE" in upper
    high_risk = "HIGH RISK" in upper
    age_minutes = _parse_age_minutes(upper)

    progress_match = re.search(r"PROGRESS TO TP3\D{0,10}?([+-]?\d+(?:\.\d+)?)\s*%", upper)
    progress_to_tp3_pct = float(progress_match.group(1)) if progress_match else None

    entry_match = re.search(r"ENTRY\D{0,10}?\$?([\d.,]+)", upper)
    sl_match = re.search(r"\bSL\D{0,10}?\$?([\d.,]+)", upper)
    entry_str = entry_match.group(1) if entry_match else None
    sl_str = sl_match.group(1) if sl_match else None
    entry_price = _to_float(entry_str)
    sl_price = _to_float(sl_str)

    sig_id = dom_id or f"{symbol}_{side}_{entry_str or '?'}_{sl_str or '?'}"

    return Signal(
        id=sig_id,
        symbol=symbol,
        side=side,
        confidence=confidence,
        elite=elite,
        aged=aged,
        high_risk=high_risk,
        age_minutes=age_minutes,
        progress_to_tp3_pct=progress_to_tp3_pct,
        entry_price=entry_price,
        sl_price=sl_price,
        raw_text=text,
    )
