# AI Trading Bot — Project Plan

**Goal:** Let an AI make 1–3 stock/ETF trades per day on a $500–$1,000 account, aiming for aggressive-but-survivable growth. For-fun experiment; risk tolerance is moderate-high, but no single day should be able to wipe the account.

**Agreed parameters** (from kickoff Q&A, July 2026):

- Autonomy: human-approval mode for ~2 weeks, then full automation
- Instruments: stocks/ETFs only (options addendum at the end for later)
- Build: custom Python bot, built in this repo
- Budget: start on free data tiers, upgrade only if a measured need appears

---

## 1. Regulatory landscape (this just changed in our favor)

The Pattern Day Trader rule — which required $25k equity for 4+ day trades per 5 days in a margin account — **was eliminated effective June 4, 2026** (SEC approved FINRA's amendment to Rule 4210 on April 14, 2026; see [FINRA Regulatory Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10) and [Schwab's summary](https://www.schwab.com/learn/story/sec-approves-scrapping-25000-day-trader-minimum)). It's replaced by real-time intraday margin, and brokers have until October 2027 to phase it in, so **confirm your chosen broker has adopted it before relying on it**.

What this means for us:

- **Margin account (small):** unlimited day trades, intraday buying power based on real-time margin excess. We would use margin *only* for settlement convenience, not leverage.
- **Cash account (fallback):** never had PDT, but T+1 settlement + good-faith-violation rules mean each dollar can only be cycled about once per day. Workable at 1–3 trades/day if we split capital into 2–3 tranches.

Recommendation: open a **margin account but cap the bot's buying power at 1.0x equity** (no leverage). We get day-trading flexibility with none of the blowup mechanics.

## 2. Platform recommendation

**Primary: [Alpaca](https://alpaca.markets/)** — built specifically for API-first retail algo trading.

- $0 commissions, no account minimum, clean REST + WebSocket API, official Python SDK (`alpaca-py`)
- **Free paper-trading environment that is API-identical to live** — flip one URL + key pair to go live. This is the single biggest reason to pick it: our phased rollout costs nothing to test.
- Free real-time market data from IEX (~5–10% of consolidated volume — fine for liquid large-caps/ETFs); full SIP consolidated feed is ~$9/mo if we ever need it ([data plans](https://alpaca.markets/data))
- Rate limits: 200 req/min free, ample for 1–3 trades/day

**Alternatives considered:**

| Platform | Pros | Why not primary |
|---|---|---|
| Interactive Brokers | Best execution, 150+ order types, global markets | Complex API (TWS/gateway must run), overkill for this scale |
| Tradier | Simple REST API, good options support | $10/mo for commission-free tier; data less generous |
| QuantConnect / Composer | Hosted backtesting / no-code | Less control; we chose to code it ourselves |
| Robinhood / Schwab | Familiar | No official (or very limited) trading API |

## 3. System architecture

Plain Python, one repo (this one), run on a schedule. No servers needed initially — a scheduled run at your machine (or a $5 VPS / GitHub Actions cron later for reliability).

```
jerome/
├── bot/
│   ├── config.py          # tickers, risk limits, mode (paper/approve/live)
│   ├── data.py            # market data ingestion (Alpaca, yfinance, Finnhub)
│   ├── signals/
│   │   ├── momentum.py    # strategy modules, each emits scored candidates
│   │   ├── meanrev.py
│   │   └── llm_analyst.py # LLM layer: news/sentiment synthesis + veto
│   ├── risk.py            # position sizing, exposure caps, kill switch
│   ├── broker.py          # order placement/monitoring (alpaca-py)
│   ├── approve.py         # human-approval gate (phase 2)
│   └── journal.py         # every decision logged to SQLite + markdown
├── backtest/              # vectorized backtests on daily/minute bars
├── data/                  # cached bars, news, journal.db
└── dashboard.md           # auto-generated daily P&L + open positions
```

**Daily loop (runs ~9:00 AM ET pre-market, optional 12:30 PM check, 3:45 PM close-management):**

1. **Ingest** — pull overnight news, pre-market movers, daily bars for universe (~50 liquid large-caps + sector ETFs), current positions.
2. **Signal** — each strategy module scores candidates (0–100) with entry, stop, and target.
3. **LLM analyst** — an LLM (Claude via API) reads the top candidates + headlines and can veto (earnings tonight, binary FDA event, macro release) or adjust conviction. LLM is a *filter and synthesizer*, not the primary signal — research on LLM trading agents ([StockBench](https://stockbench.github.io/), [TradingAgents](https://github.com/tauricresearch/tradingagents)) shows they're decent at contextual judgment but backtests suffer look-ahead bias, so we don't trust an LLM to pick stocks unaided.
4. **Risk gate** — position sizing, exposure/loss limits (Section 5). Anything failing is dropped.
5. **Execute** — Phase 2: send proposals for your approval (see below). Phase 3: place bracket orders (entry + stop-loss + take-profit as one OCO unit — the account is never in a position without a live stop).
6. **Journal** — log signal inputs, LLM reasoning, fills, and outcomes. This becomes the dataset for judging what works.

**Approval mechanism (weeks 1–2 of live):** the morning run writes proposals to a file/message with ticker, side, size, entry, stop, target, and the reasoning. You reply approve/reject (this can be a Cowork scheduled task that pings you each morning, or as simple as the bot pausing for a y/n in terminal). Unapproved by 10:00 AM = skipped.

## 4. Strategy candidates

Constraint check: 1–3 trades/day on $500–1,000 rules out HFT, stat-arb, and market-making (need scale/speed) and pure buy-and-hold (too slow to be fun). The viable zone is **intraday-to-multi-day directional trades on liquid names**. Candidates, in recommended priority:

**A. Short-term momentum / breakout (primary).** Buy strength: stocks breaking out of consolidation on above-average volume, or gapping up on real news, riding 1–5 day continuation. Best fit for "grow fast" goal — winners can run 4–10R while stops cap losers at 1R. Lower win rate (~35–45%) but positive expectancy comes from the asymmetry. Signals: 20-day high breakout, relative volume > 2x, relative strength vs SPY, gap-and-go with pre-market volume confirmation.

**B. Mean reversion on ETFs/large-caps (stabilizer).** Buy short-term panic dips in things that reliably bounce (RSI(2) < 10 on SPY/QQQ/sector ETFs above their 200-day MA; exit on close above 5-day MA). High win rate (~65%+), small gains, well-documented edge. Smooths the equity curve while momentum provides the upside. Weakness: occasionally catches a falling knife — hard stops mandatory.

**C. LLM news/sentiment overlay (filter, not driver).** Daily headline synthesis per candidate: flag binary events, score sentiment direction/strength, veto trades into earnings. Adds the "AI" flavor and measurably helps avoid landmines; academic results on LLM-as-sole-trader are mixed, so it stays advisory.

**Skip for now:** shorting (borrow fees, unlimited-loss profile conflicts with survival constraint), penny stocks (spreads and manipulation eat small accounts), overnight-gap harvesting (edge is thin after 2023).

**Honest expectations:** a realistic *good* outcome is 2–5%/month with drawdowns of 10–20%; a great year might be +40–60%. Doubling $750 quickly requires either luck or risk that violates the "don't blow up" constraint. The bot's real product in month 1–3 is the *journal*: evidence about which signals actually pay.

## 5. Risk management (the part that keeps the experiment alive)

Tuned aggressive-but-survivable per your brief:

- **Risk per trade:** 3% of equity (vs. the textbook 1–2%). At $750 that's ~$22.50 risked per trade. 10 straight losses = −26%, painful but alive.
- **Max position size:** 40% of equity per name; max 2 concurrent positions in the same sector.
- **Hard stop on every position** via bracket/OCO order at the broker — not in bot logic, so a crashed bot can't leave a naked position.
- **Daily loss limit:** −6% of equity → bot flattens everything and stops until the next day.
- **Drawdown circuit breaker:** −20% from high-water mark → bot halts, we review the journal together before resuming.
- **No leverage** (buying power capped at equity), no shorting, no positions held through a stock's own earnings.
- **Kill switch:** one command/file-flag that cancels all orders and liquidates.

## 6. Data sources (running basis)

| Need | Source | Cost | Notes |
|---|---|---|---|
| Real-time quotes/bars for execution | Alpaca (IEX feed) | Free | Fine for liquid names; included with account |
| Daily/historical bars for signals + backtests | Alpaca historical API + `yfinance` | Free | yfinance is unofficial — backup only |
| News headlines | Alpaca News API (Benzinga-sourced) + [Finnhub](https://finnhub.io/) free tier (60 calls/min) | Free | Feeds the LLM analyst |
| Earnings calendar | Finnhub / Alpha Vantage free tier | Free | Critical for the earnings-veto rule |
| Macro calendar (Fed, CPI) | Scrape econ calendar or manual config | Free | Low frequency, low effort |
| LLM inference | Claude API (one analysis call/day over ~10 candidates) | ~$0.10–0.50/day | The only real running cost |
| Fundamentals (later) | SEC EDGAR | Free | Only if strategies evolve that way |

**Free vs. paid — when to upgrade:** The main free-tier weakness is the IEX feed (partial market view; quotes can be slightly stale on thinner names). Upgrade triggers: (1) journal shows slippage vs. expected fills > ~0.1% per trade — buy Alpaca's SIP plan (~$9/mo, i.e., ~1.2%/mo of a $750 account, so it must earn its keep); (2) we move to intraday entries needing minute-level breadth — consider Polygon/Databento paid tiers. **Pro of staying free:** zero drag on a small account. **Con:** slightly worse fills and 15–20 min delayed news on some sources; acceptable for daily-cadence trading, not for scalping — which is another reason the strategy design avoids scalping.

## 7. Phased rollout

### Phase 0 — Build & backtest (weeks 1–2)

**0.1 Accounts & keys**
- 0.1.1 Create Alpaca account (email signup; paper account is instant, no funding)
- 0.1.2 Generate paper API key/secret; store in `.env` (gitignored), never in code
- 0.1.3 Get free API keys: Finnhub, Alpha Vantage; add Claude API key for the LLM analyst
- 0.1.4 Smoke test: script that authenticates, pulls a quote, and places+cancels a paper order

**0.2 Repo scaffold**
- 0.2.1 Create the `bot/`, `backtest/`, `data/` structure from Section 3
- 0.2.2 `config.py` with mode flag (`backtest | paper | approve | live`), universe list, risk constants
- 0.2.3 Set up `journal.py` first — every later component logs through it from day one

**0.3 Data layer**
- 0.3.1 Historical daily bars downloader (Alpaca API, yfinance fallback) with local caching to `data/`
- 0.3.2 Pull 5 years of bars for the ~50-name universe + SPY/QQQ benchmarks
- 0.3.3 News fetcher (Alpaca News + Finnhub) and earnings-calendar fetcher
- 0.3.4 Data sanity checks: missing days, splits/dividends adjustment, stale quotes

**0.4 Signals**
- 0.4.1 Implement momentum module (20-day-high breakout, relative volume, RS vs SPY) → scored candidates with entry/stop/target
- 0.4.2 Implement mean-reversion module (RSI(2) on ETFs above 200-day MA)
- 0.4.3 Implement LLM analyst (headline synthesis, earnings/binary-event veto, conviction adjust)
- 0.4.4 Unit tests on canned data so signal changes are regression-checked

**0.5 Risk gate & backtest**
- 0.5.1 Implement `risk.py`: 3% risk sizing, 40% max position, sector cap, daily/drawdown limits
- 0.5.2 Build vectorized backtester over the cached bars (slippage 0.05%, $0 commissions)
- 0.5.3 Walk-forward test: tune on 2019–2023, validate on 2024–mid-2026 untouched
- 0.5.4 Produce report: expectancy, win rate, max drawdown, trades/day, equity curve

**Gate to Phase 1:** positive expectancy on the validation window, max drawdown < 25%, average 1–3 signals/day. If it fails, iterate on 0.4–0.5 — do not proceed on hope.

### Phase 1 — Paper trading, full auto (weeks 3–6)

**1.1 Execution plumbing**
- 1.1.1 `broker.py`: bracket (OCO) order placement, position/fill polling, cancel-all
- 1.1.2 Kill switch: file flag checked at loop start → cancel all + liquidate
- 1.1.3 Schedule the daily loop (9:00 AM / 12:30 PM / 3:45 PM ET runs) — local cron or Cowork scheduled task
- 1.1.4 Failure alerts: any unhandled exception or unfilled stop → notify you immediately

**1.2 Run & observe**
- 1.2.1 Bot trades the paper account autonomously every market day
- 1.2.2 Auto-generate `dashboard.md` daily: positions, P&L, decisions + reasoning
- 1.2.3 Weekly review (you + me): journal vs. backtest expectations, slippage measurement (feeds Section 6 upgrade triggers)
- 1.2.4 Fix operational bugs as found; strategy changes restart the 4-week clock only if major

**Gate to Phase 2:** ≥ 4 weeks, zero critical operational failures (missed stop, wrong-size order, crash leaving naked position), performance within reason of backtest (not necessarily profitable — regime matters — but behaving as designed).

### Phase 2 — Live, approval mode (~2 weeks)

**2.1 Go-live prep**
- 2.1.1 Complete Alpaca live-account application (identity, margin agreement); confirm they've adopted the post-PDT intraday margin rules
- 2.1.2 Fund $500–1,000 (ACH, 1–3 days); verify buying-power cap = 1.0x equity in config
- 2.1.3 Swap to live keys behind the `approve` mode flag; re-run smoke test with a 1-share order you approve manually

**2.2 Approval loop**
- 2.2.1 Morning run sends you proposals by ~9:45 AM ET: ticker, side, size, entry, stop, target, reasoning
- 2.2.2 You approve/reject each; no response by 10:00 AM = skipped
- 2.2.3 Bot executes approved trades as bracket orders and manages exits automatically (only *entries* need approval)
- 2.2.4 Log your reject reasons — if you keep vetoing a signal type, that's strategy feedback

**Gate to Phase 3:** ~2 weeks or ~10 approved trades, fills match expectations, settlement/margin mechanics behave, you're comfortable — the point of this phase is calibrating your trust.

### Phase 3 — Live, full automation

**3.1 Cutover**
- 3.1.1 Flip mode `approve → live`; approval gate off
- 3.1.2 Replace proposals with a daily digest (what it did, why, P&L, account state)
- 3.1.3 Keep kill switch + all Section 5 circuit breakers active permanently

**3.2 Ongoing cadence**
- 3.2.1 Daily: read the digest (~1 min); intervene only via kill switch
- 3.2.2 Weekly: strategy review from the journal — which signals are paying, slippage trend
- 3.2.3 Monthly: go/no-go decisions — SIP data upgrade ($9/mo trigger from Section 6), parameter tweaks, options addendum unlock (gate in the Addendum)
- 3.2.4 On −20% drawdown halt: full post-mortem together before any restart

## 8. Costs summary

Broker: $0 commissions, $0 minimum. Data: $0 to start. LLM: ~$3–15/mo. Hosting: $0 (local/scheduled) to ~$5/mo (VPS) when we want it running unattended. Total drag on the account: effectively just the LLM calls.

---

## Addendum: the case for options (future enhancement)

**Why options fit this experiment later:** On a $750 account, stock trades cap your upside — a 40% position that moves 3% makes ~$9. A defined-risk option position turns the same directional view into asymmetric payoff without margin-blowup risk, because **a bought option or debit spread can never lose more than you paid.**

**Instruments that fit the "risky but survivable" mandate:**

- **Debit spreads (bull call / bear put):** buy one option, sell a further-out one. Cost ~$30–100 per spread, max loss = cost, max gain typically 1.5–3x. The natural upgrade from stock trades — same signals, more octane.
- **Long calls/puts on liquid underlyings (SPY/QQQ, 2–4 weeks out, slightly OTM):** higher risk (theta decay, need to be right on timing), 3–10x payoff when right. Position sizing rule: an options premium *is* the stop — size it at the same 3% risk number.
- **Avoid:** selling naked options (unbounded loss — violates the survival rule), 0DTE lottery tickets (fun, but that's the "wipe out in a day" profile we excluded).

**How to do it on our stack:** Alpaca supports [options trading via the same API](https://alpaca.markets/options), commission-free, with options levels granted by application (Level 2 covers long options; Level 3 for spreads). The bot's architecture barely changes: same signal engine, `broker.py` gains an options order module, risk gate adds rules for max premium/day and days-to-expiry floors, and the LLM analyst adds an IV check (don't buy expensive volatility before earnings). Data need: an options chain endpoint (Alpaca provides indicative chains; free tier is adequate for liquid underlyings).

**Gate to unlock:** Phase 3 running ≥ 1 month with positive P&L and clean execution, then start with one debit spread/week alongside stock trades, paper-traded first.

---

*Not financial advice — this is an engineering plan for a personal experiment. Losses up to the full principal are a realistic outcome; sizing the account at money you're happy to lose is part of the design.*

**Sources:** [FINRA Notice 26-10 (PDT elimination)](https://www.finra.org/rules-guidance/notices/26-10) · [Schwab on the PDT change](https://www.schwab.com/learn/story/sec-approves-scrapping-25000-day-trader-minimum) · [Alpaca](https://alpaca.markets/) / [data plans](https://alpaca.markets/data) / [options](https://alpaca.markets/options) / [paper trading docs](https://docs.alpaca.markets/us/docs/paper-trading) · [BrokerChooser algo-broker rankings](https://brokerchooser.com/best-brokers/best-brokers-for-algo-trading) · [Finnhub](https://finnhub.io/) · [Alpha Vantage](https://www.alphavantage.co/) · [StockBench](https://stockbench.github.io/) · [TradingAgents](https://github.com/tauricresearch/tradingagents) · [Fidelity on cash-account violations](https://www.fidelity.com/learning-center/trading-investing/trading/avoiding-cash-trading-violations)
