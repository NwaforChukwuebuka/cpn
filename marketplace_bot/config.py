from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    supabase_url: str
    supabase_service_role_key: str
    payment_enabled: bool
    btcpay_url: str
    btcpay_api_key: str
    btcpay_store_id: str
    btcpay_webhook_secret: str | None
    webhook_host: str
    webhook_port: int
    webhook_public_base_url: str
    order_amount_usd: float
    workflow_state: str
    workflow_stop_after: str
    adspower_api_base: str
    workflow_concurrency: int


def _required(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _payment_enabled() -> bool:
    return os.getenv("PAYMENT_ENABLED", "true").strip().lower() in ("true", "1", "yes")


def load_settings() -> Settings:
    pay = _payment_enabled()
    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        supabase_url=_required("SUPABASE_URL"),
        supabase_service_role_key=_required("SUPABASE_SERVICE_ROLE_KEY"),
        payment_enabled=pay,
        btcpay_url=_required("BTCPAY_URL") if pay else "",
        btcpay_api_key=_required("BTCPAY_API_KEY") if pay else "",
        btcpay_store_id=_required("BTCPAY_STORE_ID") if pay else "",
        btcpay_webhook_secret=os.getenv("BTCPAY_WEBHOOK_SECRET", "").strip() or None,
        webhook_host=os.getenv("WEBHOOK_HOST", "0.0.0.0"),
        webhook_port=int(os.getenv("WEBHOOK_PORT", "8080")),
        webhook_public_base_url=(_required("WEBHOOK_PUBLIC_BASE_URL").rstrip("/") if pay else "https://localhost"),
        order_amount_usd=float(os.getenv("ORDER_AMOUNT_USD", "150")),
        workflow_state=os.getenv("WORKFLOW_STATE", "Florida"),
        workflow_stop_after=os.getenv("WORKFLOW_STOP_AFTER", "first_premier"),
        adspower_api_base=os.getenv("ADSPOWER_API_BASE", "http://127.0.0.1:50325"),
        workflow_concurrency=int(os.getenv("WORKFLOW_CONCURRENCY", "3")),
    )
