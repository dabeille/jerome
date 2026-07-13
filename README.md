# jerome — AI trading bot

An experiment: 1–3 stock/ETF trades/day on a small account, quant signals +
LLM veto layer, via Alpaca. Full plan: [trading-bot-plan.md](trading-bot-plan.md).

## Layout

```
bot/            trading logic (config, data, signals/, risk, broker, journal)
backtest/       daily-bar backtest engine (shares code with the live bot)
scripts/        smoke_test.py — verify keys/connectivity (paper only)
data/           cached bars + journal.db (gitignored)
KILL            touch this file to cancel all orders, liquidate, and halt
dashboard.md    regenerated each run (gitignored)
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env   # then fill in your keys
python -m scripts.smoke_test             # verifies auth, data, order round-trip
```

Daily runs (times are **ET** — see Pi notes on timezone):

```bash
python -m bot.main morning   # ~9:00 ET
python -m bot.main midday    # ~12:30 ET (optional)
python -m bot.main close     # ~15:45 ET
```

## Raspberry Pi 4 notes

**Use 64-bit Raspberry Pi OS (Lite is ideal).** This is the one hard
requirement: on aarch64, every dependency (numpy, pandas, pydantic-core
inside alpaca-py) installs as a prebuilt wheel. On 32-bit OS you'd be
compiling Rust/C and some builds fail outright. Check with:
`uname -m` → must say `aarch64`.

Other Pi specifics, all already accounted for in the code:

- **No compile-heavy deps.** No TA-Lib (C build), no vectorbt/numba (flaky
  on ARM). Indicators are plain pandas; the backtester is a simple loop —
  at this data size (~60 symbols, daily bars) a Pi 4 runs it in seconds.
- **CSV cache, not parquet** — avoids pyarrow.
- **SD card wear:** SQLite journal writes are tiny (a few KB/day). Fine.
  If you're paranoid, mount `data/` on a USB SSD.
- **Clock:** market-hours logic depends on correct time. Verify NTP:
  `timedatectl` → "System clock synchronized: yes". Keep the Pi's timezone
  as whatever it is and schedule in ET explicitly (below) so DST never
  silently shifts your runs.
- **Memory:** 2 GB model is plenty; don't run a desktop environment.

Cron (`crontab -e`) — `CRON_TZ` keeps schedules in market time year-round;
`flock` prevents overlapping runs if one hangs:

```cron
CRON_TZ=America/New_York
0  9  * * 1-5  cd /home/pi/jerome && flock -n /tmp/bot.lock .venv/bin/python -m bot.main morning >> data/cron.log 2>&1
30 12 * * 1-5  cd /home/pi/jerome && flock -n /tmp/bot.lock .venv/bin/python -m bot.main midday  >> data/cron.log 2>&1
45 15 * * 1-5  cd /home/pi/jerome && flock -n /tmp/bot.lock .venv/bin/python -m bot.main close   >> data/cron.log 2>&1
```

(Market holidays: the bot should no-op via the broker calendar — TODO step
1.1.3 adds an `is_market_open` check at the top of `run()`.)

## Security

Threat model: a box on your home network holding API keys that can trade
(but **not withdraw**) your money, running unattended.

**Keys**
- Alpaca API keys can trade and read the account but cannot change bank
  links or withdraw to arbitrary accounts. The realistic damage from a
  leaked key is churning/liquidating your positions — bad, but bounded.
  Rotate immediately (Alpaca dashboard) if you ever suspect exposure.
- `.env` is gitignored and must be `chmod 600`. `config.py` warns at every
  run if permissions are loose.
- Paper and live keys are separate variables; live keys don't exist
  anywhere until Phase 2. Never put keys in code, logs, or the journal.
- The Claude/Finnhub keys are low-stakes (spend-capped, free tier) but get
  the same treatment.

**The Pi itself**
- Start from a fresh 64-bit Raspberry Pi OS Lite image; change the default
  password immediately, or better: SSH keys only —
  in `/etc/ssh/sshd_config` set `PasswordAuthentication no`.
- **No inbound exposure:** don't port-forward anything to the Pi, disable
  UPnP on your router for it. The bot only needs *outbound* HTTPS.
  Optionally `sudo apt install ufw && sudo ufw default deny incoming &&
  sudo ufw allow from <your-LAN-subnet> to any port 22 && sudo ufw enable`.
- Auto security updates: `sudo apt install unattended-upgrades`.
- Run the bot as a dedicated non-sudo user; the repo and `.env` owned by it.
- Don't co-host other services (media servers, etc.) on this Pi — every
  extra service is attack surface next to your trading keys.
- Read `dashboard.md` by SSHing in, not by exposing a web server.
- Physical: anyone with the SD card has the keys. Home setting = acceptable
  risk given keys can't withdraw; rotate keys if the Pi is ever
  lost/stolen/resold, and wipe the card before disposal.

**Supply chain**
- `requirements.txt` is version-bounded and minimal (6 packages). Install
  only from it; watch for typosquatting if adding packages by hand.
- Update deliberately (`pip list --outdated`, read changelogs), not
  automatically — an auto-updated broken dep during market hours is its
  own risk.

**Operational**
- The kill switch is a file (`touch KILL`) — works over any SSH session
  even if Python is wedged; the next scheduled run flattens and halts, or
  run `python -m bot.main` manually to flatten immediately.
- Every position is entered as a bracket order, so stops live at the
  broker. A dead Pi (power cut, SD failure) cannot leave you without a
  stop — the worst case of a dead Pi is missed *entries*, which costs
  nothing.
- Alerts on failure (step 1.1.4) so silence never means "fine".
