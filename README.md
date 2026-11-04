# Zero Waste Bag watcher

Pings your phone when a Deliveroo HOP Zero Waste Bag is listed and in stock, and logs every drop in `sightings.csv`.

It checks every 5 minutes from 7am to 11pm Dubai time. It only reads the public store page; it never logs in, adds to basket or orders.

## Setup

It's watching Deliveroo HOP Motor City, and the HOP link and ntfy channel are written near the top of `zero_waste_watch.py`. All you need is the **ntfy** app (free, App Store or Google Play) subscribed to the channel named there.

To test it any time, go to **Actions › Zero Waste Bag watcher › Run workflow** and pick `test-push` (your phone should ping) or `check` (shows what it can see on Deliveroo right now).

Because this repo is public, anyone who finds it can see the channel name and could send you pings. To hide it, add it as a secret instead (**Settings › Secrets and variables › Actions**, named `NTFY_TOPIC`), which overrides the one in the script, then delete it from the script. The same works for `HOP_URLS`.

## What you'll get

One ping when a bag appears, showing its price; nothing while it stays listed; a new ping if one comes back. No pings from 11pm to 7am, but a bag still listed at 7am gets announced then.

If you get a ping saying the watcher **can't see Deliveroo**, Deliveroo is blocking GitHub's servers. Run it on a laptop instead (see below).

## Is it fast enough?

After a few days, run the workflow with `report`. It shows which hours bags have dropped, on how many days, how quickly they sold out, and warns you if bags are selling out between checks.

## Public or private

Public repos get unlimited free GitHub Actions minutes, which is what lets it check every 5 minutes. The trade-off is that anyone can see the code, the log and which HOP store it watches.

If you make the repo private, change the `cron` line in `.github/workflows/watch.yml` to `"7,27,47 3-18 * * *"` (every 20 minutes). Private repos get 2,000 free minutes a month, and 5 minute checks would use them up in about ten days.

## Running it on a laptop instead

Download `zero_waste_watch.py` and, on your laptop only (never in this repo), paste your HOP link into `HOP_URLS` and your channel name into `NTFY_TOPIC` near the top. Then in Terminal:

```
python3 zero_waste_watch.py --check
python3 zero_waste_watch.py --test-push
CHECK_EVERY_MINUTES=5 python3 zero_waste_watch.py
```

Leave the last one running; it checks every 5 minutes while the laptop is awake. On Windows, type `py` instead of `python3` and set `CHECK_EVERY_MINUTES` in the script. To avoid double pings, switch the GitHub one off first: **Actions › Zero Waste Bag watcher › ⋯ › Disable workflow**.

## Good to know

* It catches anything with "Zero Waste" in the name, so the AED 25 and AED 50 bags are both covered.
* If `check` says there's no bag while you can see one in the app, run `--check` on a laptop: it saves the page as `last_page.html`, which shows exactly what the watcher saw.
