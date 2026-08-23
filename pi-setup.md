# Raspberry Pi setup & Phase-1 kickoff runbook

A follow-along guide to take a bare Raspberry Pi to a running, hardened,
unattended paper-trading bot with alerts, a LAN dashboard, and EOD chain
snapshots. Work top to bottom; each section ends with a check you can verify
before moving on.

> Conventions: the bot lives at `/home/pi/jerome`, runs as the `pi` user, and
> uses a venv at `/home/pi/jerome/.venv`. Adjust paths if you pick a different
> user or directory (and update the cron/systemd blocks to match).

---

## 0. What you need

- Raspberry Pi 4 (2 GB is plenty) + a good power supply.
- A 32 GB+ SD card (or, better, a USB SSD — the journal and chain snapshots do
  daily small writes; an SSD outlives an SD card).
- Your laptop, on the same LAN as the Pi.
- An Alpaca **paper** account with API key + secret.
- Free API keys: Finnhub, Alpha Vantage, Anthropic (Claude).
- A [Resend](https://resend.com) account (free tier) for email alerts.

You do **not** need: a monitor/keyboard for the Pi (headless setup below), a
domain name, port forwarding, or any paid data feed.

---

## 1. Flash Raspberry Pi OS Lite (64-bit)

64-bit matters — `requirements.txt` relies on aarch64 wheels; 32-bit would try
to compile some packages.

1. Install **Raspberry Pi Imager** on your laptop.
2. Choose device: Raspberry Pi 4. OS: *Raspberry Pi OS Lite (64-bit)* (no
   desktop — a desktop is wasted attack surface next to trading keys).
3. Click the gear / "Edit settings" **before** writing, and set:
   - **Hostname:** `jerome` (so you can `ssh pi@jerome.local`).
   - **Enable SSH** → *Use public-key authentication only*. Paste your laptop's
     public key (`cat ~/.ssh/id_ed25519.pub`; if you have none, run
     `ssh-keygen -t ed25519` first). Password auth stays off from minute one.
   - **Username/password:** username `pi`, and a strong password (used only for
     `sudo`, not for SSH login).
   - **Locale / timezone:** set your real local timezone. The bot schedules in
     ET explicitly regardless, so this only affects log readability.
4. Write the card, put it in the Pi, power on. Give it ~60 seconds.

**Check:** from your laptop, `ssh pi@jerome.local`. You should land in a shell
with no password prompt (key auth). If `jerome.local` doesn't resolve, find the
Pi's IP from your router's DHCP table and use that.

---

## 2. OS hardening

Run these on the Pi over SSH.

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3-venv python3-pip ufw unattended-upgrades tzdata
# flock ships in util-linux, which is already on every Debian-based system —
# no separate package to install
```

**Confirm SSH is key-only** (the Imager should have set this; verify):

```bash
sudo grep -E 'PasswordAuthentication|PubkeyAuthentication' /etc/ssh/sshd_config
# want: PasswordAuthentication no   /   PubkeyAuthentication yes
# if you had to change it:  sudo systemctl restart ssh
```

**Firewall — default-deny inbound, allow SSH + the dashboard from your LAN
only.** Find your LAN subnet first (e.g. `ip route` shows something like
`192.168.1.0/24`):

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 192.168.1.0/24 to any port 22     # SSH, LAN only
sudo ufw allow from 192.168.1.0/24 to any port 8080   # dashboard, LAN only (section 8)
sudo ufw enable
sudo ufw status verbose
```

**Automatic security updates:**

```bash
sudo dpkg-reconfigure -plow unattended-upgrades   # choose "Yes"
```

**Clock & timezone sanity** (market-hours logic depends on correct time):

```bash
timedatectl        # want: "System clock synchronized: yes"
python3 -c "from zoneinfo import ZoneInfo; from datetime import datetime; print(datetime.now(ZoneInfo('America/New_York')))"
# must print a valid ET timestamp — proves tzdata resolves America/New_York
```

**Check:** `sudo ufw status` shows only ports 22 and 8080, both scoped to your
LAN subnet; `timedatectl` shows synchronized.

---

## 3. Clone the repo & build the venv

```bash
cd /home/pi
git clone https://github.com/dabeille/jerome.git
cd jerome
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Raspberry Pi OS Bookworm ships Python 3.11 — fine. Installation pulls only
prebuilt aarch64 wheels, so nothing compiles.

**Check:** `.venv/bin/python -c "import alpaca, pandas, requests, anthropic; print('deps OK')"`.

---

## 4. Secrets (`.env`)

```bash
cp .env.example .env
nano .env        # fill in every key
chmod 600 .env   # readable only by you — config.py warns at every run if looser
```

Fill in:

| Variable | Value |
|---|---|
| `BOT_MODE` | `paper` (stays paper for all of Phase 1) |
| `ALPACA_PAPER_KEY_ID` / `ALPACA_PAPER_SECRET` | from Alpaca paper dashboard |
| `FINNHUB_KEY`, `ALPHA_VANTAGE_KEY` | free-tier keys |
| `ANTHROPIC_API_KEY` | Claude key for the LLM analyst |
| `RESEND_API_KEY` | from Resend (section 5) |
| `ALERT_EMAIL_TO` | the inbox where alerts should land |

Leave `ALPACA_LIVE_*` blank — live keys don't exist until Phase 2. Leave
`ALERT_EMAIL_FROM` commented out to use Resend's shared `onboarding@resend.dev`
sender (delivers to the account owner with no domain setup).

**Check:** `.venv/bin/python -c "from bot import config; config.validate()"`
prints no "Missing Alpaca keys" or permission warnings. (A "no dynamic universe
file" note is expected here — section 7 fixes it.)

---

## 5. Resend alerts

1. Create a Resend account, verify your email.
2. **API Keys → Create** → copy the key into `RESEND_API_KEY` in `.env`.
3. With the free shared sender, Resend only delivers to the **account owner's**
   email, so make `ALERT_EMAIL_TO` that same address. (Add and verify your own
   domain later if you want to send elsewhere.)
4. Send a live test:

```bash
.venv/bin/python -c "from bot import alerts; print('sent:', alerts.alert('jerome test alert', 'If you see this, alerting works.'))"
```

**Check:** prints `sent: True` and the email arrives (check spam once, then
allowlist the sender). If it prints `sent: False` with an "unconfigured" note,
the key/recipient aren't loaded — re-check `.env`. Alerting failing never
crashes the bot, but you want it working before you rely on silence meaning
"fine".

---

## 6. Smoke test & seed data

**Broker/data/order round-trip (paper):**

```bash
.venv/bin/python -m scripts.smoke_test
```

Expect three green lines: auth OK, data OK, order round-trip OK.

**Seed the bar cache** (signals and the chain-snapshot spot anchor read it):

```bash
.venv/bin/python -m scripts.fetch_history          # ~5y daily bars, whole universe
```

This takes a few minutes on a Pi and prints a per-symbol tally. A few
`MISSING` names are tolerable; a mostly-failed run means a data/key problem —
fix before continuing.

**Build the dynamic universe once:**

```bash
.venv/bin/python -m scripts.refresh_universe
```

**Check:** `smoke_test` passes; `ls data/bars | wc -l` shows dozens of CSVs;
`data/dynamic_universe.json` exists.

---

## 7. Dry-run the sessions by hand

Do these manually before trusting cron.

**A weekday run** (any sequence — during or outside market hours is fine for a
paper smoke):

```bash
.venv/bin/python -m bot.main morning
```

You should see it fetch signals, run the gate, and (if anything qualifies)
submit paper brackets. Then inspect the journal and funnel:

```bash
.venv/bin/python - <<'PY'
from bot import journal
print("last funnel:", journal.last_funnel())
for row in journal.recent_decision_rows(10):
    print(row)
PY
```

**A weekend / holiday no-op** — prove the trading-day guard works. On a
Saturday/Sunday (or any market holiday), run:

```bash
.venv/bin/python -m bot.main morning
# expect: "morning: not a trading day (broker calendar) — no-op."
```

If today is a weekday and you want to force-verify the no-op logic without
waiting, you can trust the unit test (`tests/test_main.py::
test_non_trading_day_no_ops_before_signals`) instead.

**Check:** the weekday run journals an equity row + a funnel row; the
weekend run prints the no-op line and journals nothing.

---

## 8. LAN dashboard (systemd + firewall)

The bot writes `data/public/dashboard.html` on every run. Serve **only** that
directory, read-only, over the LAN.

`python -m http.server` is explicitly not a production server, and this one runs
as `pi` — the user that owns `.env`. So the unit below assumes it *will* be the
weak link and takes away everything it could leak: an absolute `--directory`
(a relative path silently follows `WorkingDirectory` — if that ever drifts you
serve the repo root, `.env` included), plus systemd sandboxing that makes the
key file unreadable to this process even if the server itself is compromised.

```bash
sudo tee /etc/systemd/system/bot-dashboard.service >/dev/null <<'UNIT'
[Unit]
Description=Jerome LAN dashboard (static)
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/jerome
ExecStart=/usr/bin/python3 -m http.server 8080 --directory /home/pi/jerome/data/public --bind 0.0.0.0
Restart=on-failure

# Sandbox: this process should be able to read exactly one directory.
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
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now bot-dashboard
sudo systemctl status bot-dashboard --no-pager
```

The ufw rule from section 2 already allows port 8080 from your LAN subnet.

**Check, from another device on the LAN:**

- `http://jerome.local:8080/dashboard.html` (or `http://<pi-ip>:8080/…`) renders
  the account header, equity sparkline, positions, funnel, and decisions.
- `curl -so /dev/null -w '%{http_code}\n' http://<pi-ip>:8080/journal.db`
  returns **404** — the DB and the rest of `data/` are *not* served, only
  `data/public/`. If this returns 200, stop and fix the `--directory` path.
- The sandbox actually bit:
  `sudo -u pi systemd-run --pipe --property=InaccessiblePaths=/home/pi/jerome/.env cat /home/pi/jerome/.env`
  fails. (Belt and braces: `systemctl show bot-dashboard -p InaccessiblePaths`
  should echo the `.env` path back.)

> **Tighter option, no extra port.** Everything above still puts your account
> equity and open positions on the LAN in cleartext, readable by any device on
> it, with no authentication. Since SSH is already key-only, you can instead
> set `--bind 127.0.0.1`, drop the ufw rule for 8080 entirely, and reach the
> dashboard through a tunnel from your laptop:
>
> ```bash
> ssh -L 8080:localhost:8080 pi@jerome.local   # then browse localhost:8080
> ```
>
> That gets you authentication and encryption for free and removes the only
> inbound port besides SSH. Recommended if you don't need the dashboard on a
> phone or tablet.

---

## 9. Install cron

First make the lock directory (locks live in the repo, not `/tmp` — see the
note below):

```bash
mkdir -p /home/pi/jerome/run
```

`crontab -e` (as the `pi` user, not root) and paste:

```cron
CRON_TZ=America/New_York
0  9  * * 1-5  cd /home/pi/jerome && flock -n run/bot.lock   timeout 900 .venv/bin/python -m bot.main morning         >> data/cron.log 2>&1
30 12 * * 1-5  cd /home/pi/jerome && flock -n run/bot.lock   timeout 900 .venv/bin/python -m bot.main midday          >> data/cron.log 2>&1
45 15 * * 1-5  cd /home/pi/jerome && flock -n run/bot.lock   timeout 900 .venv/bin/python -m bot.main close           >> data/cron.log 2>&1
0  10 * * 1-5  cd /home/pi/jerome && flock -n run/hb.lock    timeout 300 .venv/bin/python -m scripts.heartbeat_check  >> data/cron.log 2>&1
15 16 * * 1-5  cd /home/pi/jerome && flock -n run/chain.lock timeout 900 .venv/bin/python -m scripts.snapshot_chains  >> data/cron.log 2>&1
0  8  * * 0    cd /home/pi/jerome && flock -n run/uni.lock   timeout 900 .venv/bin/python -m scripts.refresh_universe >> data/cron.log 2>&1
```

Two details that are load-bearing, not style:

- **`timeout 900` is what keeps the kill switch working.** The three sessions
  share one lock, and `flock -n` skips the run outright if it's held. So a run
  that wedges — a hung TLS socket to Alpaca, say — would hold that lock forever
  and every later session would exit before reaching the kill-switch check,
  including the one you're counting on after `touch KILL`. `timeout` guarantees
  a hung run dies and releases the lock. Without it, "the next scheduled run
  flattens" is not true.
- **Locks live in `run/`, not `/tmp`.** `/tmp` is world-writable: any other
  local process can pre-create `/tmp/bot.lock` with permissions `pi` can't
  open, and every run then fails silently. A repo-local path also survives
  reboots consistently.

What each line does:

- **09:00 / 12:30 / 15:45 ET** — the three trading sessions. `flock` on a shared
  lock prevents overlap if one hangs. On non-trading days each self-no-ops via
  the broker calendar.
- **10:00 ET heartbeat** — pages you if the 09:00 run never journaled an equity
  snapshot on a trading day (i.e. cron itself failed).
- **16:15 ET chain snapshot** — after the close, outside any trading session, so
  a chain failure can never touch the trading path.
- **Sunday 08:00 — universe refresh** — rebuilds the dynamic tier weekly.

`CRON_TZ=America/New_York` keeps every schedule in market time across DST.

**Check:** `crontab -l` shows all six lines. After the next scheduled run,
`tail -f data/cron.log` shows output. Manually trigger the heartbeat now to
confirm the path works: `.venv/bin/python -m scripts.heartbeat_check`.

---

## 10. Chain snapshots — verify once

Run it by hand once (any time — it's outside market hours safe):

```bash
.venv/bin/python -m scripts.snapshot_chains --symbols SPY
ls -lh data/chains/SPY/
.venv/bin/python - <<'PY'
import gzip, json, glob
f = sorted(glob.glob("data/chains/SPY/*.json.gz"))[-1]
d = json.load(gzip.open(f, "rt"))
print(f, "->", d["contract_count"], "contracts; spot", d["spot"])
print("sample:", d["contracts"][0] if d["contracts"] else "(empty)")
PY
```

**Check:** a `YYYY-MM-DD.json.gz` file exists, parses, has a sane contract
count and spot, and a second run of the same command prints `skip` (idempotent).

---

## 11. Day-to-day operations

- **Kill switch:** `touch /home/pi/jerome/KILL` from any SSH session. The next
  scheduled run cancels all orders, liquidates, and halts — it's the first
  thing `run()` checks, before anything touches the broker.
  **Don't wait for it if you mean it:** run `.venv/bin/python -m bot.main`
  manually to flatten immediately. The scheduled path depends on cron actually
  starting a run, which the `flock`/`timeout` pairing in section 9 protects but
  cannot absolutely guarantee (a wedged run still holds the lock until `timeout`
  fires — up to 15 minutes). The manual run takes no lock and is instant.
  Delete the file to re-arm.
- **Drawdown halt & resume:** on a −20%-from-high-water-mark breach the bot
  flattens and halts. Recovery is deliberately manual: review the journal, then
  `.venv/bin/python -m bot.main resume` and type `RESUME` (cron can never trip
  this). It rebases the high-water mark to current equity.
- **Unprotected positions & re-arming:** an `UNPROTECTED` alert means an open
  position has no live broker-side stop. Either flatten (`touch KILL`) or
  re-arm with `.venv/bin/python -m bot.main reattach-stops`, which attaches a
  GTC stop/target OCO at the levels from that position's original entry. A
  position with no journalled entry levels is reported, not guessed at — re-arm
  that one by hand in the Alpaca UI.
- **Alerts mean act; silence means fine.** The heartbeat guarantees silence is
  real — if the morning run dies, you get paged by 10:00 ET. Watch for
  `UNPROTECTED` and `CRASH` subjects especially. Alert subjects lead with the
  dollars at stake; a `[STILL UNPROTECTED — run N]` prefix means you have
  already been told N times.
- **Daily glance:** open the LAN dashboard. Equity curve, open positions, the
  last funnel row (how many signals surfaced vs. filled), recent decisions.
- **Log hygiene:** `data/cron.log` grows slowly; rotate occasionally
  (`: > data/cron.log` after archiving, or add a `logrotate` rule).

## 12. Updating the Pi

Every code change reaches the Pi by hand — there is no deploy pipeline, and the
bot deliberately never updates itself (an auto-updated dependency during market
hours is its own risk). The failure mode that matters here is silent: a Pi still
running last month's code produces a perfectly clean-looking week of evidence
about the wrong software. The week-1 review opened by proving the Pi had
actually pulled, because nothing in the journal says so on its own.

### When

**Weekends and holidays.** The three sessions and the heartbeat only fire
Mon–Fri, so a Saturday update collides with nothing; Sunday collides only with
the 08:00 ET universe refresh. Update mid-week and you get one session on the
old code and the next on the new one, with no marker in the journal saying
which — or a run that starts mid-pull and imports a half-updated tree.

If you must update on a weekday, do it **between** sessions (roughly 10:15–12:15
or 13:00–15:30 ET), never inside one, and check nothing is running first.

### Update

```bash
ssh pi@jerome.local
cd /home/pi/jerome

# 1. Nothing may be mid-run. Both of these must come back empty.
pgrep -af 'bot\.main|scripts\.' ; ls run/

# 2. Local state should be clean. data/, .env and KILL are gitignored, so
#    anything listed here is an edit made on the Pi and forgotten about.
git status --short

# 3. See what you are about to take, then take it.
git fetch origin && git log --oneline HEAD..origin/main
git pull --ff-only origin main

# 4. Dependencies — only if requirements.txt moved in that range.
git diff --name-only HEAD@{1} HEAD -- requirements.txt
.venv/bin/pip install -r requirements.txt        # only if the line above printed

# 5. New settings. .env is gitignored and a pull never touches it, so diff the
#    variable names against the example and add anything new by hand.
diff <(grep -oE '^[A-Z_]+' .env.example | sort) <(grep -oE '^[A-Z_]+' .env | sort)
```

Step 5 is not bookkeeping. `ENABLED_STRATEGIES=meanrev` — the setting that
benched momentum for the entire Phase-1 window — arrived as a line in
`.env.example` that had to be copied across by hand. A pull cannot add it, the
bot does not warn when it is missing, and the default quietly runs both
strategies instead.

### Verify

```bash
# The code is what you think it is, and it imports.
git log -1 --format='%h %ad %s' --date=iso
.venv/bin/python -c "import bot.main, bot.broker, bot.risk, bot.journal; print('imports OK')"
.venv/bin/python -c "from bot import config; config.validate()"

# Read-only broker check — exercises the live API path without placing anything.
.venv/bin/python -c "from bot import broker; print('buying power', broker.available_buying_power())"
```

The full suite is the strongest check available, and it is offline and pure
Python — one extra package, no wheels to build:

```bash
.venv/bin/pip install -r requirements-dev.txt    # once; pytest only
.venv/bin/python -m pytest -q
```

**Do not use `scripts.smoke_test` as a post-update check.** It places a real
(far-from-market, immediately cancelled) SPY order, which lands in the account's
order history and then shows up as a phantom entry in the next weekly Alpaca
pull. It is a first-install tool, not a deploy check.

**The dashboard needs nothing.** `bot-dashboard` serves static files and runs no
bot code; the next session re-renders `data/public/dashboard.html`.

### Confirm it actually took

`git` says what is on disk. Only the journal says what *ran*. After the first
session on the new code, check the funnel row carries whatever the change was
supposed to add:

```bash
sqlite3 data/journal.db "select ts,run,payload from funnel order by ts desc limit 3"
```

For the 2026-08-23 update that means `vetoed_earnings` / `vetoed_llm` appearing
on a run that had vetoes, and `llm_failed` staying absent. Until a real run
shows it, treat the deploy as unverified.

### Rolling back

```bash
git log --oneline -5
git reset --hard <sha>          # the previous known-good commit
touch KILL                      # if you are rolling back because it traded wrong
```

Two things `git reset` will not undo: a `.env` edit from step 5, and anything
already written to `journal.db`. **Never delete or truncate the journal** to get
back to a clean state — per plan §1.1.6 an empty journal makes both the −6% and
−20% breakers return `False`, silently disarming them. That is a much worse
failure than the one being rolled back.

## 13. Weekly review (plan 1.2.3)

Once a week, spend ten minutes on:

- **Live funnel vs. backtest funnel shape** — are signals dying at the same
  stages (mostly `slots_full`) as the backtest predicted?
- **Fill slippage vs. signal entry prices** — if realized entries drift > ~0.1%
  from intended, that's the §6 trigger to consider Alpaca's paid SIP feed.
- **meanrev vs. momentum split** — the validation run flagged meanrev at −0.91R
  out-of-sample; this is the evidence that feeds the deferred retune decision.
- **Running tally toward the Phase-2 gate** — expectancy and drawdown over the
  live sample.

## 14. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Missing Alpaca keys for mode 'paper'` | `.env` not filled or not loaded; check `chmod 600 .env` and the `ALPACA_PAPER_*` names. |
| `config.py` warns `.env is readable by group/others` | `chmod 600 .env`. |
| Alert test prints `sent: False (unconfigured…)` | `RESEND_API_KEY` or `ALERT_EMAIL_TO` empty in `.env`. |
| Alert `sent: False` with an HTTP error | free Resend sender only delivers to the account owner — set `ALERT_EMAIL_TO` to that address. |
| Sessions run on a weekend | check `CRON_TZ` line is present and the broker calendar call works (`is_trading_day()`); a weekend run should print the no-op line, not trade. |
| Dashboard unreachable from laptop | ufw rule subnet wrong (`ufw status`), or `bot-dashboard` not running (`systemctl status bot-dashboard`). |
| `curl …/journal.db` returns 200 | the server is exposing too much — `--directory` is missing, relative, or wrong. Must be the absolute `/home/pi/jerome/data/public`. Fix immediately. |
| Dashboard 403s or the unit won't start after the sandbox change | a `ProtectSystem=strict` path violation. `journalctl -u bot-dashboard -n 50` names the path; add it to `ReadOnlyPaths` only if it's genuinely needed. |
| A session produced no output at all in `cron.log` | `flock` skipped it because a previous run still holds `run/bot.lock`. `ls -l run/`, then `pgrep -af bot.main`. With `timeout` in place this self-clears within 15 min; if it doesn't, kill the process by hand and `touch KILL`. |
| Silence, but you're not sure it's healthy | the 10:00 heartbeat only proves the **morning** run journaled equity. A midday/close run that never fired is not alerted on. `sqlite3 data/journal.db "select ts,note from equity order by ts desc limit 5"`. |
| `ZoneInfo('America/New_York')` errors | `sudo apt install tzdata`. |
| A week of evidence looks clean but doesn't match the code you shipped | the Pi never pulled. `ssh pi@jerome.local 'cd jerome && git log -1'` — compare against `main`. Nothing in the journal records which commit ran, so check this *first*, before reading any of the week's numbers (section 12). |
| Chain snapshot: `no cached bars for X` | run `scripts.fetch_history` first (section 6). |

## 15. Phase-1 start

When sections 1–10 all check out and cron is installed:

1. Log the **Phase-1 start date** in `docs/backtest-findings.md` — it starts the
   4-week Phase-2 evaluation clock.
2. Let it run. Resist the urge to tinker mid-week; the point of Phase 1 is an
   honest live sample to compare against the backtest.
3. Do the weekly review (section 13). The month-1 product is the *journal*:
   evidence about which signals actually pay.
