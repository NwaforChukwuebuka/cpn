# cpn

## Telegram Marketplace Bot

The Telegram app lives in `marketplace_bot/` and integrates:

- Order/profile intake over Telegram (`/order`).
- BTCPay invoice creation for BTC/LTC.
- BTCPay webhook confirmation and queue submission.
- Supabase Postgres persistence for users/orders/payments/workflow jobs.
- Workflow completion callback that sends the resulting CPN CSV in Telegram.

### Setup

1. Copy `.env.example` to `.env` and fill all variables.
2. Run Supabase migration: `supabase/migrations/202603040001_marketplace_bot.sql`.
3. Install dependencies: `pip install -r requirements.txt`.
4. Start the bot: `python -m marketplace_bot.app`.

**BTCPay webhooks (public URL):** Either set `WEBHOOK_PUBLIC_BASE_URL` in `.env` to your public URL, or set `USE_NGROK=true` and `NGROK_AUTHTOKEN` (and optionally `NGROK_URL` for a reserved domain). With `USE_NGROK=true`, the bot starts ngrok automatically and uses the tunnel URL for webhooks—no separate terminal needed.

The bot starts Telegram polling and an HTTP webhook listener on `WEBHOOK_HOST:WEBHOOK_PORT`.

### Fillers (Capital One, First Premier)

On a new machine (e.g. after cloning the repo), create a profile so the fillers can run:

1. Copy the example profile:  
   `copy data\profile.example.json data\profile.json` (Windows) or  
   `cp data/profile.example.json data/profile.json` (Linux/macOS).
2. Edit `data/profile.json` with your real data (do not commit this file; it is gitignored).
3. Run a filler, e.g.:  
   `python -m modules.first_premier.run_filler` or  
   `python -m modules.capital_one.run_filler`.
