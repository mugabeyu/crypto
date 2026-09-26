"""
Watches the SHORT ONLY tab on the cryptoflowsignals.com signals page and
returns fresh, qualifying signals for main.py to execute directly on
Binance. This file ONLY reads the page — it never clicks Execute Trade,
Precision Trade, or any order-placement button.

FIXED v3 (2026-09-26):
- v1: domcontentloaded waits (no more 30s networkidle timeouts),
  throttled "still running" log, sane timeouts.
- v2: parsed TP1/TP2/TP3 via label regexes.
- v3: price parsing rewritten against the REAL card layout (5-column
  table: ENTRY | TP1 | TP2 | TP3 | SL, each price prefixed with $, and a
  separate "Current: $x" line below). We isolate the table segment
  (ENTRY .. before "Progress"/"Current") and map the $ prices by position
  — no guessing, no risk of grabbing a digit from "TP2" or the Current
  price. Falls back to label-based regex if <5 prices found.
"""
import logging
import re
from dataclasses import dataclass
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeoutError

logger = logging.getLogger("cryptoflow_bot.scraper")

HEADLESS = False
NAV_TIMEOUT_MS = 25000
ACTION_TIMEOUT_MS = 12000

SIGNAL_CARD_SELECTOR = 'div[id^="signal-row-"]'
SHORT_ONLY_TAB_SELECTOR = 'text="SHORT ONLY"'


@dataclass
class Filters:
    min_confidence: float
    skip_aged: bool
    skip_high_risk: bool
    max_age_minutes: float | None
    max_progress_pct: float | None


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
    tp1_price: float | None
    tp2_price: float | None
    tp3_price: float | None
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
        self._last_running_key: str | None = None

    def start(self):
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=HEADLESS)
        self._page = self._browser.new_page()
        self._page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        self._page.set_default_timeout(ACTION_TIMEOUT_MS)
        self._login()

    def stop(self):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def _login(self):
        page = self._page
        page.goto(self.login_url, wait_until="domcontentloaded")
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
        try:
            page.wait_for_url(lambda u: "auth" not in u.lower() and "login" not in u.lower(),
                              timeout=15000)
        except PWTimeoutError:
            pass
        if "auth" in page.url.lower() or "login" in page.url.lower():
            raise RuntimeError(
                "Still on the login page after submitting — check your "
                "email/password in .env, or whether the site has a "
                "CAPTCHA/2FA that blocks headless login."
            )
        logger.info("Logged in to cryptoflowsignals.com")

    def _goto_short_only_signals(self):
        page = self._page
        page.goto(self.dashboard_url, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(SIGNAL_CARD_SELECTOR, timeout=12000, state="attached")
        except PWTimeoutError:
            logger.warning("No signal cards appeared within timeout — continuing anyway.")
        tab = page.query_selector(SHORT_ONLY_TAB_SELECTOR)
        if tab:
            try:
                tab.click()
                page.wait_for_timeout(800)
            except Exception as e:
                logger.warning("Could not click SHORT ONLY tab: %s", e)
        else:
            logger.warning("Could not find the SHORT ONLY filter tab.")
        # Prices lazy-render after the tab loads. Wait for at least one card
        # to show a $ price before reading; if none appear, read anyway.
        try:
            page.wait_for_selector(
                SIGNAL_CARD_SELECTOR + ':has-text("$")', timeout=6000,
            )
        except PWTimeoutError:
            logger.debug("No card showed a $ price within 6s — reading anyway.")

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
        key = ",".join(sorted(running_symbols)) + f"|{completed_count}"
        if key == self._last_running_key:
            return
        self._last_running_key = key
        logger.info(
            "SHORT ONLY still running: %d (%s) — %d already in Today's Completed Wins.",
            len(running_symbols),
            ", ".join(running_symbols) if running_symbols else "none",
            completed_count,
        )

    def get_new_qualifying_signals(self, filters: Filters, seen: set) -> tuple[list[Signal], set]:
        self._goto_short_only_signals()
        self._log_running_signals()
        cards = self._get_cards()
        logger.info("Found %d card(s) under SHORT ONLY", len(cards))
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
                logger.warning("Could not parse a card, skipping: %r", text[:80])
                continue
            if sig.id in seen:
                continue
            if not sig.passes(filters):
                logger.info(
                    "Skipping %s %s — filtered out (confidence=%s, age=%sm, "
                    "progress=%s%%, aged=%s, high_risk=%s)",
                    sig.symbol, sig.side, sig.confidence, sig.age_minutes,
                    sig.progress_to_tp3_pct, sig.aged, sig.high_risk,
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


def _parse_prices(upper: str) -> tuple:
    """
    Real card layout (5 columns, each $-prefixed):
        ENTRY $x | TP1 $x | TP2 $x | TP3 $x | SL $x
    followed (below the table) by "Progress to TP3 …%" and "Current: $x".
    We isolate the table segment and map $ prices by position. If fewer
    than 5 found, fall back to label-anchored regexes.
    Returns (entry, tp1, tp2, tp3, sl).
    """
    table_m = re.search(r"ENTRY(.{0,600}?)(?:PROGRESS\s+TO|CURRENT\s*:|\Z)", upper, re.DOTALL)
    segment = table_m.group(1) if table_m else upper
    prices = [_to_float(p) for p in re.findall(r"\$\s*([\d.,]+)", segment)]

    entry = tp1 = tp2 = tp3 = sl = None

    if len(prices) >= 5:
        entry, tp1, tp2, tp3, sl = prices[:5]
    else:
        # Fallback: label-anchored, $ required. (?<!TO ) keeps "PROGRESS TO TP3"
        # from being mistaken for a TP label.
        def label_price(label_re: str) -> float | None:
            m = re.search(label_re + r"[^$]{0,30}?\$\s*([\d.,]+)", segment)
            return _to_float(m.group(1)) if m else None

        entry = label_price(r"ENTRY")
        tp1 = label_price(r"(?<!TO )(?:TP|TARGET)\s*1")
        tp2 = label_price(r"(?<!TO )(?:TP|TARGET)\s*2")
        tp3 = label_price(r"(?<!TO )(?:TP|TARGET)\s*3")
        sl = label_price(r"\bSL")

    return entry, tp1, tp2, tp3, sl


def _parse_card(text: str, dom_id: str | None = None) -> Signal | None:
    upper = text.upper()
    # Incomplete / not-yet-lazy-loaded card (no $ prices at all, e.g.
    # 'LDOUSDT\nSHORT\n+0.22%'). Skip silently — retried next poll.
    if "$" not in text:
        return None
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

    entry_price, tp1_price, tp2_price, tp3_price, sl_price = _parse_prices(upper)

    if sl_price is None:
        logger.warning(
            "Could not parse SL price for %s — parsed entry=%s tp1=%s tp2=%s tp3=%s. "
            "Card text: %r", symbol, entry_price, tp1_price, tp2_price, tp3_price, text[:200],
        )
    if tp1_price is None and tp2_price is None and tp3_price is None:
        logger.warning(
            "No TP prices parsed for %s — will trade entry+SL only. Card text: %r",
            symbol, text[:200],
        )

    entry_str = f"{entry_price}" if entry_price is not None else "?"
    sl_str = f"{sl_price}" if sl_price is not None else "?"
    sig_id = dom_id or f"{symbol}_{side}_{entry_str}_{sl_str}"

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
        tp1_price=tp1_price,
        tp2_price=tp2_price,
        tp3_price=tp3_price,
        raw_text=text,
    )
