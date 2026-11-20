# JB Hi-Fi Pokémon TCG monitor

Checks JB Hi-Fi every 15 minutes, one second past each quarter hour (hh:00:01, hh:15:01, hh:30:01, hh:45:01), using GitHub Actions, for Pokémon TCG products **sold by JB Hi-Fi itself** (marketplace sellers are excluded) and posts to Discord:

- **Alerts**: a clean embed when a product is **newly listed** or comes **back in stock** (price, status, per-customer limit, release date for pre-orders, online/in-store, set, image).
- **Live board**: one message listing everything currently available, grouped into Pre-orders / In stock / Limited stock. It's edited in place every run, so you pin it once and it stays current.

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

```bash
python monitor.py --dry-run                                   # no Discord, no state written
DISCORD_WEBHOOK_URL=... python monitor.py --test-alert        # send one sample alert
```

## Caveats

- GitHub's scheduled runs are best-effort. The 7-minute head start covers normal delays, but if GitHub is more than 7 minutes behind, that check happens late, and occasionally GitHub drops a scheduled run entirely. For guaranteed timing, run `monitor.py --at-quarter` from cron on a VPS or Raspberry Pi.
- GitHub disables scheduled workflows in public repos after 60 days with no repository activity. If alerts stop, check the Actions tab and re-enable the workflow.
- This relies on JB's unofficial frontend API. If they change the API key or request format, the run fails, and the failure shows up in the Actions tab (GitHub emails you on failed runs).
