# Telegram Marketplace Bot Architecture

This project now includes a modular async scaffold in `marketplace_bot/` for a Telegram marketplace.

## Design Goals

- Modular feature boundaries (users, products, orders, payments).
- Concurrent multi-user handling without shared-state conflicts.
- Async-first request handling for high parallel throughput.
- Easy scaling from in-memory services to external infrastructure.

## Module Layout

- `marketplace_bot/core/contracts.py`: module interface contract.
- `marketplace_bot/core/module_manager.py`: module lifecycle + router wiring.
- `marketplace_bot/core/session_store.py`: per-user async locks for session safety.
- `marketplace_bot/core/services.py`: concurrency-safe domain services.
- `marketplace_bot/modules/*.py`: feature modules with isolated handlers.
- `marketplace_bot/app.py`: runtime composition and polling startup.

## Concurrency Model

- Telegram updates are processed asynchronously by `aiogram`.
- Shared mutable state is protected with `asyncio.Lock` in services.
- User-scoped critical flows (`/order`, `/pay`) run under per-user locks:
  - One user cannot race their own order/payment commands.
  - Different users still execute concurrently.
- Payment processing is idempotent in `PaymentService` to avoid double charge behavior on retries.

## Scaling Path

Current scaffold is in-memory for speed of development. To scale:

1. Replace in-memory services with DB-backed repositories (PostgreSQL).
2. Use a distributed lock/session store (Redis) for multi-instance bot workers.
3. Add task queue for slow operations (Celery/RQ/Arq).
4. Keep module APIs unchanged so scaling stays an infra concern, not a handler rewrite.

## Run

1. Add `.env` with:
   - `TELEGRAM_BOT_TOKEN=<your token>`
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Start bot:
   - `python -m marketplace_bot.app`

