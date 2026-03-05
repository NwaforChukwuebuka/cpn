from __future__ import annotations

from typing import Any

import httpx


class BTCPayClient:
    def __init__(self, base_url: str, api_key: str, store_id: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"token {api_key}"}
        self._store_id = store_id

    async def create_invoice(
        self,
        *,
        order_id: str,
        amount_usd: float,
        notification_url: str,
    ) -> dict[str, Any]:
        body = {
            "amount": f"{amount_usd:.2f}",
            "currency": "USD",
            "checkout": {
                "speedPolicy": "MediumSpeed",
                "paymentMethods": ["BTC", "LTC"],
            },
            "metadata": {"orderId": order_id},
            "notificationURL": notification_url,
        }
        url = f"{self._base_url}/api/v1/stores/{self._store_id}/invoices"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=self._headers, json=body)
            resp.raise_for_status()
            return resp.json()

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        url = f"{self._base_url}/api/v1/stores/{self._store_id}/invoices/{invoice_id}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=self._headers)
            resp.raise_for_status()
            return resp.json()


def invoice_is_paid(invoice: dict[str, Any]) -> bool:
    status = str(invoice.get("status", "")).lower()
    return status in {"settled", "processing", "complete", "confirmed"}
