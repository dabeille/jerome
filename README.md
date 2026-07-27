# jerome — AI trading bot

An experiment: 1–3 stock/ETF trades/day on a small account, quant signals with an LLM veto layer, via Alpaca.

## Layout

```
bot/            trading logic (config, data, signals/, risk, broker, alerts, journal)
backtest/       daily-bar backtest engine (shares code with the live bot)
scripts/        smoke_test, heartbeat_check, snapshot_chains, refresh_universe, ...
data/           cached bars + journal.db (gitignored)
data/public/    dashboard.html — the only directory the LAN server exposes
KILL            touch this file to cancel all orders, liquidate, and halt
dashboard.md    regenerated each run (gitignored)
```

## Setup

**Python 3.10+ recommended** (3.9 works — the code carries
`from __future__ import annotations` for it — but 3.9 is EOL). Heads-up for
macOS: bare `python3` may be the Xcode Command Line Tools 3.9 build, which
also triggers a harmless `NotOpenSSLWarning` from urllib3 because it links
LibreSSL. Prefer a Homebrew Python: `brew install python@3.12`, then build
the venv with `python3.12`. Raspberry Pi OS Bookworm ships 3.11 — fine as-is.

```bash
python3.12 -m venv .venv && source .venv/bin/activate   # or python3 if it's ≥3.10
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
`flock` prevents overlapping runs if one hangs, and `timeout` guarantees a hung
run *releases* that lock (without it, one wedged process blocks every later
session, including the one meant to act on the kill switch). `mkdir -p run`
first:

```cron
CRON_TZ=America/New_York
0  9  * * 1-5  cd /home/pi/jerome && flock -n run/bot.lock   timeout 900 .venv/bin/python -m bot.main morning         >> data/cron.log 2>&1
30 12 * * 1-5  cd /home/pi/jerome && flock -n run/bot.lock   timeout 900 .venv/bin/python -m bot.main midday          >> data/cron.log 2>&1
45 15 * * 1-5  cd /home/pi/jerome && flock -n run/bot.lock   timeout 900 .venv/bin/python -m bot.main close           >> data/cron.log 2>&1
0  10 * * 1-5  cd /home/pi/jerome && flock -n run/hb.lock    timeout 300 .venv/bin/python -m scripts.heartbeat_check  >> data/cron.log 2>&1
15 16 * * 1-5  cd /home/pi/jerome && flock -n run/chain.lock timeout 900 .venv/bin/python -m scripts.snapshot_chains  >> data/cron.log 2>&1
0  8  * * 0    cd /home/pi/jerome && flock -n run/uni.lock   timeout 900 .venv/bin/python -m scripts.refresh_universe >> data/cron.log 2>&1
```

Locks live in `run/`, not `/tmp`: `/tmp` is world-writable, so any other local
process can pre-create the lock file with permissions that make every run fail
silently.

Market holidays: the three session runs no-op automatically — `run()` checks
Alpaca's trading calendar (`broker.is_trading_day()`) right after the kill
switch and exits early on non-trading days. Half-days need no special handling.

The extra cron lines: `heartbeat_check` (10:00 ET) pages you if the morning run
never journaled an equity snapshot on a trading day — i.e. cron itself failed;
`snapshot_chains` (16:15 ET, after the close) banks EOD option-chain snapshots;
`refresh_universe` (Sunday) rebuilds the dynamic tier.

## LAN dashboard

Each run also renders `data/public/dashboard.html` (account header, an inline-SVG
equity sparkline, open positions, the last signal-funnel row, recent decisions).
Serve it read-only from the Pi over the LAN with a static file server — no
dynamic code touches the DB, and **only `data/public/` is exposed, never `data/`
itself** (so `journal.db` and cached data stay unreachable):

`python -m http.server` is not a production server, and it runs as the user that
owns `.env` — so the unit assumes it will be the weak link and takes away
everything it could leak. The `--directory` is absolute (a relative path follows
`WorkingDirectory`; if that drifts you serve the repo root, `.env` included) and
the sandbox makes the key file unreadable to this process even if the server
itself is compromised:

```ini
# /etc/systemd/system/bot-dashboard.service
[Unit]
Description=Bot LAN dashboard (static)
After=network.target
[Service]
User=pi
WorkingDirectory=/home/pi/jerome
ExecStart=/usr/bin/python3 -m http.server 8080 --directory /home/pi/jerome/data/public --bind 0.0.0.0
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadOnlyPaths=/home/pi/jerome/data/public
InaccessiblePaths=/home/pi/jerome/.env
PrivateDevices=true
RestrictAddressFamilies=AF_INET AF_INET6
[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now bot-dashboard
sudo ufw allow from <your-LAN-subnet> to any port 8080   # LAN only, same pattern as port 22
```

Then browse `http://<pi-lan-ip>:8080/dashboard.html` from any device on your
network. The page auto-refreshes every 5 minutes. Sanity check: `curl
http://<pi-lan-ip>:8080/journal.db` must 404 — only `data/public/` is served.

Note this puts account equity and open positions on the LAN in cleartext with no
authentication. If you don't need the dashboard on a phone, bind `127.0.0.1`,
skip the ufw rule entirely, and tunnel over the SSH you already have:
`ssh -L 8080:localhost:8080 pi@jerome.local`.

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
  the same treatment. Concretely: API keys go in **headers, never query
  params** — `requests` puts the full URL in every exception message it
  raises, and cron appends stderr to `data/cron.log`, so a `?token=` form
  writes the key to a plaintext file on every transient 4xx.

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
- The one deliberate inbound exception is the **LAN dashboard** (above): a
  read-only static file server bound to the LAN and firewalled with `ufw` to
  your subnet only. It serves `data/public/` exclusively — never `data/`, so
  `journal.db` and cached data stay off the wire. No dynamic code, no DB
  access, no writes: a viewer can read the rendered HTML and nothing else. For
  anything not on the dashboard, read the journal by SSHing in. It's also
  systemd-sandboxed so that a bug in `http.server` — which is not a hardened
  server — can't reach `.env`, and it's unauthenticated, so anyone on your LAN
  can read your equity and positions. Prefer the SSH-tunnel variant if that
  matters to you.
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
- The kill switch is a file (`touch KILL`) — settable over any SSH session,
  and the first thing `run()` checks. The next scheduled run flattens and
  halts; **if you mean it, don't wait for cron** — run `python -m bot.main`
  manually to flatten immediately. The scheduled path needs cron to actually
  start a run, which the `flock`/`timeout` pairing above protects but can't
  make instantaneous (a wedged run holds the lock until `timeout` fires). The
  manual run takes no lock.
- Every position is entered as a bracket order, so stops live at the
  broker. A dead Pi (power cut, SD failure) cannot leave you without a
  stop — the worst case of a dead Pi is missed *entries*, which costs
  nothing.
- Alerts on failure (step 1.1.4) so silence never means "fine".
