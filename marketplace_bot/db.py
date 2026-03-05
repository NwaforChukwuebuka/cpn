from __future__ import annotations

import asyncio
from typing import Any

from supabase import Client, create_client


class SupabaseRepo:
    def __init__(self, url: str, key: str) -> None:
        self._client: Client = create_client(url, key)

    async def ensure_user(self, telegram_id: int, username: str | None) -> dict[str, Any]:
        def _op() -> dict[str, Any]:
            existing = (
                self._client.table("users")
                .select("*")
                .eq("telegram_id", telegram_id)
                .limit(1)
                .execute()
            )
            if existing.data:
                return existing.data[0]
            inserted = (
                self._client.table("users")
                .insert({"telegram_id": telegram_id, "username": username})
                .execute()
            )
            return inserted.data[0]

        return await asyncio.to_thread(_op)

    async def get_user_by_telegram_id(self, telegram_id: int) -> dict[str, Any] | None:
        def _op() -> dict[str, Any] | None:
            existing = (
                self._client.table("users")
                .select("*")
                .eq("telegram_id", telegram_id)
                .limit(1)
                .execute()
            )
            return existing.data[0] if existing.data else None

        return await asyncio.to_thread(_op)

    async def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        def _op() -> dict[str, Any] | None:
            existing = (
                self._client.table("users")
                .select("*")
                .eq("id", user_id)
                .limit(1)
                .execute()
            )
            return existing.data[0] if existing.data else None

        return await asyncio.to_thread(_op)

    async def create_order(
        self,
        *,
        user_id: str,
        amount_usd: float,
        profile_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        def _op() -> dict[str, Any]:
            resp = (
                self._client.table("orders")
                .insert(
                    {
                        "user_id": user_id,
                        "status": "pending_payment",
                        "amount_usd": amount_usd,
                        "currency": "USD",
                        "profile_snapshot": profile_snapshot,
                    }
                )
                .execute()
            )
            return resp.data[0]

        return await asyncio.to_thread(_op)

    async def set_order_invoice(self, order_id: str, invoice_id: str, checkout_url: str) -> None:
        def _op() -> None:
            (
                self._client.table("orders")
                .update(
                    {
                        "btcpay_invoice_id": invoice_id,
                        "btcpay_checkout_url": checkout_url,
                    }
                )
                .eq("id", order_id)
                .execute()
            )

        await asyncio.to_thread(_op)

    async def get_order_by_id(self, order_id: str) -> dict[str, Any] | None:
        def _op() -> dict[str, Any] | None:
            resp = (
                self._client.table("orders")
                .select("*")
                .eq("id", order_id)
                .limit(1)
                .execute()
            )
            return resp.data[0] if resp.data else None

        return await asyncio.to_thread(_op)

    async def get_order_by_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        def _op() -> dict[str, Any] | None:
            resp = (
                self._client.table("orders")
                .select("*")
                .eq("btcpay_invoice_id", invoice_id)
                .limit(1)
                .execute()
            )
            return resp.data[0] if resp.data else None

        return await asyncio.to_thread(_op)

    async def mark_order_paid(self, order_id: str) -> None:
        def _op() -> None:
            (
                self._client.table("orders")
                .update({"status": "paid"})
                .eq("id", order_id)
                .in_("status", ["pending_payment", "paid"])
                .execute()
            )

        await asyncio.to_thread(_op)

    async def mark_order_processing(self, order_id: str) -> None:
        """Set order status to processing (e.g. when starting or retrying workflow)."""
        def _op() -> None:
            (
                self._client.table("orders")
                .update({"status": "processing"})
                .eq("id", order_id)
                .execute()
            )

        await asyncio.to_thread(_op)

    async def complete_order(self, order_id: str, csv_path: str | None) -> bool:
        """
        Record one more delivered CPN and set status to completed when cpns_delivered >= cpns_paid.
        Returns True if the order was updated, False if already fulfilled (prevents over-generation).
        """
        def _op() -> bool:
            row = (
                self._client.table("orders")
                .select("cpns_paid, cpns_delivered, status")
                .eq("id", order_id)
                .limit(1)
                .execute()
            )
            if not row.data:
                return False
            o = row.data[0]
            cpns_paid = int(o.get("cpns_paid") or 1)
            cpns_delivered = int(o.get("cpns_delivered") or 0)
            if o.get("status") == "completed" or cpns_delivered >= cpns_paid:
                return False
            new_delivered = cpns_delivered + 1
            (
                self._client.table("orders")
                .update({
                    "result_csv_path": csv_path,
                    "cpns_delivered": new_delivered,
                    "status": "completed" if new_delivered >= cpns_paid else "processing",
                })
                .eq("id", order_id)
                .execute()
            )
            return True

        return await asyncio.to_thread(_op)

    async def fail_order(self, order_id: str) -> None:
        def _op() -> None:
            (
                self._client.table("orders")
                .update({"status": "failed"})
                .eq("id", order_id)
                .execute()
            )

        await asyncio.to_thread(_op)

    async def get_latest_order_for_user(self, user_id: str) -> dict[str, Any] | None:
        def _op() -> dict[str, Any] | None:
            resp = (
                self._client.table("orders")
                .select("*")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            return resp.data[0] if resp.data else None

        return await asyncio.to_thread(_op)

    async def insert_payment(
        self,
        *,
        order_id: str,
        invoice_id: str,
        status: str,
        amount: float | None,
        currency: str | None,
        payload: dict[str, Any],
        paid_at_iso: str | None,
    ) -> None:
        def _op() -> None:
            (
                self._client.table("payments")
                .insert(
                    {
                        "order_id": order_id,
                        "invoice_id": invoice_id,
                        "status": status,
                        "amount": amount,
                        "currency": currency,
                        "payload": payload,
                        "paid_at": paid_at_iso,
                    }
                )
                .execute()
            )

        await asyncio.to_thread(_op)

    async def create_workflow_job(self, order_id: str, job_id: str) -> None:
        def _op() -> None:
            (
                self._client.table("workflow_jobs")
                .insert({"order_id": order_id, "job_id": job_id, "status": "queued"})
                .execute()
            )

        await asyncio.to_thread(_op)

    async def update_workflow_job(
        self,
        *,
        job_id: str,
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
        started_at_iso: str | None,
        completed_at_iso: str | None,
    ) -> None:
        def _op() -> None:
            payload: dict[str, Any] = {"status": status, "result": result, "error": error}
            if started_at_iso:
                payload["started_at"] = started_at_iso
            if completed_at_iso:
                payload["completed_at"] = completed_at_iso
            (
                self._client.table("workflow_jobs")
                .update(payload)
                .eq("job_id", job_id)
                .execute()
            )

        await asyncio.to_thread(_op)
