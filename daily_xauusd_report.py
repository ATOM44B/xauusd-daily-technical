#!/usr/bin/env python3
"""Daily XAUUSD technical day-trade report -> Telegram.

Runs in GitHub Actions on a cron schedule (00:00 UTC = 07:00 Asia/Bangkok).
Calls Claude with the server-side web_search tool to gather fresh gold data,
produces a full technical day-trade analysis (entry / TP1 / TP2 / SL), and
sends it to Telegram split into <4000-char chunks.

Required environment variables (set as GitHub Actions repository secrets):
  ANTHROPIC_API_KEY    - Anthropic API key (https://console.anthropic.com)
  TELEGRAM_BOT_TOKEN   - Telegram bot token (from BotFather)
  TELEGRAM_CHAT_ID     - Telegram chat / user ID to send to
Optional:
  CLAUDE_MODEL         - model id (default: claude-opus-4-8;
                         set to claude-sonnet-4-6 to cut cost ~3-5x)
"""

import datetime
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import anthropic

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
TELEGRAM_LIMIT = 4000  # stay safely under Telegram's 4096-char hard limit
BANGKOK = datetime.timezone(datetime.timedelta(hours=7))  # Thailand, no DST


def require_env(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing required environment variable: {name}")
    return val


BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
# TELEGRAM_CHAT_ID may be one id or several, comma/space-separated, e.g.
# "7897287575,-5170192508" to send to a private chat AND a group.
CHAT_IDS = [c for c in require_env("TELEGRAM_CHAT_ID").replace(",", " ").split() if c]
ANTHROPIC_API_KEY = require_env("ANTHROPIC_API_KEY")


def build_prompt(today_str):
    return f"""You are a professional markets technical analyst. Today is {today_str} \
(Asia/Bangkok time). Produce a TECHNICAL DAY-TRADE analysis of XAUUSD \
(spot gold vs US dollar) for TODAY's trading session.

STEP 1 - Use the web_search tool (run several searches) to gather CURRENT data:
- Latest XAUUSD spot price and today's intraday high/low range
- Key intraday support and resistance levels
- Daily moving averages (20/50/200 EMA) and whether price is above or below them
- RSI(14), MACD, and ATR readings
- Recent trend / chart pattern (bullish, bearish, range-bound)
- Any major scheduled catalyst today or tomorrow (FOMC, CPI, NFP, PCE, Fed speakers, geopolitics)
Prefer sources dated within the last 1-2 days. Never invent numbers - only use what you find.

STEP 2 - Write the analysis in PLAIN TEXT formatted for Telegram. No markdown tables, \
no asterisks for bold. Use UPPERCASE section headers and simple dashes/emoji. Be tight \
and actionable. Include these sections in order:

SNAPSHOT - current price, intraday range, overall bias (bullish/bearish/neutral), one-line technical signal.
TREND & STRUCTURE - EMA stack, higher-highs/lower-lows, short vs longer-term trend.
MOMENTUM - one line each for RSI, MACD, ATR.
KEY LEVELS - 3 resistances and 3 supports, each with the price and a one-word basis.
TRADE SETUP (the most important section) - a clear, actionable plan for today:
  Direction, Entry zone, Stop Loss (SL), Take Profit 1 (TP1), Take Profit 2 (TP2), \
and approximate Risk/Reward. If it is a pre-event or range-bound day, say so and give \
a primary plan PLUS a brief alternative / invalidation trigger.
RISKS & NOTES - event risk, what would invalidate the setup.

Rules: use real numbers from your searches; if sources conflict, say so and anchor to \
the live intraday range. End with the exact line: "Educational only - not financial advice." \
Keep the whole report under ~5000 characters."""


def generate_analysis(today_str):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=600.0)
    messages = [{"role": "user", "content": build_prompt(today_str)}]

    resp = None
    # The server-side web_search loop can return stop_reason="pause_turn"
    # if it hits its internal iteration cap; re-send to let it resume.
    for _ in range(6):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}],
            messages=messages,
        )
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break

    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"Empty analysis (stop_reason={resp.stop_reason})")
    return text


def split_message(text, limit=TELEGRAM_LIMIT):
    """Split text into <=limit-char chunks, preferring line boundaries."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single very long line: hard-split it
            chunks.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def send_telegram(text, chat_id):
    """Send one message to one chat. Returns True on success, False otherwise.

    A failure to one recipient (e.g. bot removed from a group) is logged but
    does not abort sends to the other recipients.
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"WARNING: Telegram send to {chat_id} failed ({e.code}): {body}",
              file=sys.stderr)
        return False
    except Exception as e:  # network issues shouldn't kill the other sends
        print(f"WARNING: Telegram send to {chat_id} failed: {e}", file=sys.stderr)
        return False


def main():
    today_str = datetime.datetime.now(BANGKOK).strftime("%Y-%m-%d (%A)")
    try:
        analysis = generate_analysis(today_str)
    except Exception as e:  # notify on Telegram so silent failures are visible
        for chat_id in CHAT_IDS:
            send_telegram(f"⚠️ XAUUSD daily report FAILED to generate "
                          f"({today_str}): {e}", chat_id)
        raise

    header = f"\U0001F4CA XAUUSD Daily Day-Trade Report — {today_str} — 07:00 ICT"
    chunks = split_message(f"{header}\n\n{analysis}")
    total = len(chunks)
    any_ok = False
    for chat_id in CHAT_IDS:
        for i, chunk in enumerate(chunks, 1):
            prefix = f"({i}/{total})\n" if total > 1 else ""
            any_ok |= send_telegram(prefix + chunk, chat_id)
            time.sleep(1)  # gentle pacing to avoid Telegram rate limits
    print(f"Sent {total} message(s) to {len(CHAT_IDS)} recipient(s) for {today_str}.")
    if not any_ok:
        sys.exit("All Telegram sends failed.")


if __name__ == "__main__":
    main()
