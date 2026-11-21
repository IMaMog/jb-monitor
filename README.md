# JB Hi-Fi Pokémon TCG monitor

Checks JB Hi-Fi every 15 minutes, one second past each quarter hour (hh:00:01, hh:15:01, hh:30:01, hh:45:01), using GitHub Actions, for Pokémon TCG products **sold by JB Hi-Fi itself** (marketplace sellers are excluded) and posts to Discord:

- **Alerts**: a clean embed when a product is **newly listed** or comes **back in stock** (price, status, per-customer limit, release date for pre-orders, online/in-store, set, image).
- **Live board**: a card with an image for every product currently available, ordered Pre-orders → In stock → Limited stock and coloured by status. It's edited in place every run, so you pin it once and it stays current. Discord fits 9 cards (plus the header) in a message. If the count crosses 9, 19, and so on, the board is reposted as more or fewer messages, and you'd need to re-pin. A dedicated board channel avoids needing a pin at all.

By default it tracks sealed card product only (boosters, blisters, display boxes, decks, collections). Set `INCLUDE_ACCESSORIES=true` to also track sleeves, binders, playmats and deck boxes.

## Setup

1. **Create the Discord webhooks.** Go to Channel settings → Integrations → Webhooks → New Webhook → Copy URL.
   - Use one webhook for alerts (for example `#jb-alerts`).
   - Optionally use a second webhook in its own channel for the board (for example `#jb-stock`) so it never gets buried.

2. **Create a GitHub repo and push this folder.** Make the repo **public**. Each run waits on the runner for up to ~7 minutes to hit its exact time, which adds up to roughly 20,000 runner-minutes a month. That's free on public repos but far beyond the 2,000 minutes private repos get. Your webhook URLs are stored as secrets and are never visible in a public repo.

   ```bash
   git add .
   git commit -m "JB Hi-Fi Pokemon monitor"
   git remote add origin https://github.com/<you>/jb-monitor.git
   git push -u origin main
   ```

3. **Add the settings** under repo → Settings → Secrets and variables → Actions.

   | Kind | Name | Value |
   |---|---|---|
   | Secret | `DISCORD_WEBHOOK_URL` | Alerts webhook URL (required) |
   | Secret | `DISCORD_BOARD_WEBHOOK_URL` | Board webhook URL (optional; defaults to the alerts webhook) |
   | Variable | `DISCORD_PING` | Who to ping on alerts: `<@YOUR_USER_ID>` or `<@&ROLE_ID>` (optional) |
   | Variable | `INCLUDE_ACCESSORIES` | `true` to include accessories (optional) |

   To get your user ID, turn on Developer Mode in Discord (Settings → Advanced), then right-click your name → Copy User ID.

4. **Start it.** Go to the Actions tab → *JB Hi-Fi Pokemon monitor* → Run workflow.
   - The first run records what's currently listed **without** alerting, then posts the board.
   - **Pin the board message** (hover → ⋯ → Pin). Webhooks can't pin, but the message ID never changes, so the pin keeps working.
   - To preview what an alert looks like, run the workflow again with **Send a sample alert** ticked.

## How it works

- **Timing:** GitHub's cron works to the minute and often starts runs late, so the workflow is scheduled 7 minutes early (`:53`, `:08`, `:23`, `:38`). `monitor.py --at-quarter` then sleeps until exactly the next `:00:01`/`:15:01`/`:30:01`/`:45:01` before calling JB. Each slot has its own cron line, so a run GitHub starts late still knows which slot it belongs to. If it's already past that slot, it checks immediately instead of skipping. Manual runs from the Actions tab check right away. Each run logs the exact time it called JB, so you can see the timing in the run output.

- `monitor.py` calls JB's own search API (the one jbhifi.com.au uses) with a server-side filter for trading cards, the Pokémon franchise, and `isMarketplace = false`. It then keeps product group 1186 (cards), plus 1106 if accessories are on.
- `state.json` remembers every product seen and its status. The workflow commits it back only when something actually changed.
- An alert fires when a SKU appears for the first time while purchasable, or goes from sold out/missing back to purchasable. Items that drop out of search results (JB hides sold-out items) are marked sold out, so they alert again when they return.
- If the API ever returns nothing, the run is skipped instead of marking everything sold out.

## Running locally

Copy `.env.example` to `.env` and fill in a webhook. Use a **separate test channel** while trialling, so it doesn't double up with the GitHub monitor.

```bash
python monitor.py --dry-run          # fetch + print, no Discord, no state written
python monitor.py --test-alert       # send one sample alert
```

## Near-live loop mode (trial)

```bash
python monitor.py --loop 15 --state state.local.json
```

- Polls every ~15 s (randomised ±20% so it isn't perfectly regular), sends alerts the moment something changes, and refreshes the board when something changes or once a minute.
- `--state state.local.json` keeps the trial's state separate from GitHub's `state.json`. The first poll records what's listed without alerting.
- On errors or 429s, it backs off (2×, 4×… the interval, up to 5 minutes), then carries on.
- Logs go to `logs/` (git-ignored):
  - `polls.csv`: every request, with success/failure, response time and product count. Use it to spot rate limiting.
  - `latency.csv`: for every alert, how many seconds after JB's `published_at` (new listings) or `updated_at` (restocks) it was detected. This is the real-world delay, including any lag in JB's search index.
- Stop with Ctrl-C. Intervals under 5 s are refused.

If the trial looks good, run it permanently on an always-on machine and turn off the GitHub schedule, so alerts don't arrive twice.

## Target AU monitor

The same script also watches Target's [Pokémon Cards category](https://www.target.com.au/c/toys/trading-card-games/pokemon-cards/W1852642) with `--retailer target`. It sends the same alerts and runs its own board, and keeps its state in `state_target.json`. The workflow is `.github/workflows/target-monitor.yml`, which runs on the same quarter-hour slots.

- **Source:** the category page is a Next.js page with the full product list embedded as JSON (`__NEXT_DATA__`). There's no API key and no login, just one normal page request per check (more only if the category grows past 48 products).
- **Cards only:** the category also contains plush, sleeves and portfolios. A product counts as a card product if its title says Pokémon plus "TCG" or "Trading Card Game", and doesn't contain an accessory word (sleeves, deck protector, portfolio, binder, toploader…). "Sleeved Booster" still counts as cards. Set `TARGET_INCLUDE_ACCESSORIES=true` to also track the Pokémon accessories.
- **Status:** a product is *In stock* if any variation is in stock, and *Coming soon* if Target marks it that way (no alert yet). When a Coming soon product goes live you get a **Now available** alert. Target removes sold-out items from the category, so a product that disappears is marked sold out and alerts again when it comes back.
- Target doesn't publish per-customer limits, release dates or set names in the listing, so those fields are left off its alerts.

**Setup:** add these in the same Actions settings, then run *Target Pokemon monitor* once from the Actions tab to seed it and post the board.

| Kind | Name | Value |
|---|---|---|
| Secret | `TARGET_DISCORD_WEBHOOK_URL` | Target alerts webhook (optional; falls back to `DISCORD_WEBHOOK_URL`) |
| Secret | `TARGET_DISCORD_BOARD_WEBHOOK_URL` | Target board webhook (optional; falls back to the Target alerts webhook) |
| Variable | `TARGET_INCLUDE_ACCESSORIES` | `true` to include Pokémon accessories (optional) |

`DISCORD_PING` is shared. Locally:

```bash
python monitor.py --retailer target --dry-run
python monitor.py --retailer target --loop 30 --state state_target.local.json
```

## Caveats

- Target sits behind Akamai. A normal request gets through today, but if Akamai starts blocking GitHub's runner IPs, the run fails with "no `__NEXT_DATA__`" or an HTTP 403/429. If that happens, run it from a home machine instead.

- GitHub's scheduled runs are best-effort. The 7-minute head start covers normal delays, but if GitHub is more than 7 minutes behind, that check happens late, and occasionally GitHub drops a scheduled run entirely. For guaranteed timing, run `monitor.py --at-quarter` from cron on a VPS or Raspberry Pi.
- GitHub disables scheduled workflows in public repos after 60 days with no repository activity. If alerts stop, check the Actions tab and re-enable the workflow.
- This relies on JB's unofficial frontend API. If they change the API key or request format, the run fails, and the failure shows up in the Actions tab (GitHub emails you on failed runs).
