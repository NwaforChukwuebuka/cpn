from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import subprocess
import time
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, or_f
from aiogram.fsm.context import FSMContext
from aiogram.types import ErrorEvent
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

from marketplace_bot.btcpay import BTCPayClient, invoice_is_paid
from marketplace_bot.config import Settings, load_settings, _use_ngrok
from marketplace_bot.csv_export import build_profile_csv_bytes, persist_csv
from marketplace_bot.db import SupabaseRepo
from marketplace_bot.profiles import merge_profile, validate_profile_input, workflow_state_from_profile
from marketplace_bot.validators import (
    validate_city,
    validate_date_of_birth,
    validate_email,
    validate_first_name,
    validate_last_name,
    validate_phone,
    validate_state,
    validate_street,
    validate_zip,
)
from modules.full_workflow import (
    FileWorkflowCheckpointStore,
    FullWorkflowQueueService,
    WorkflowJobRecord,
    WorkflowJobRequest,
)

LOG = logging.getLogger("marketplace_bot")

# Downgrade aiogram network/connection errors to WARNING so they don't look fatal (we retry automatically)
def _is_network_error(record: logging.LogRecord) -> bool:
    msg = (record.getMessage() or "").lower()
    return (
        "network" in msg
        or "connection" in msg
        or "clientoserror" in msg
        or "aborted" in msg
        or "fetch updates" in msg
    )


class _NetworkErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno == logging.ERROR and _is_network_error(record):
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        return True


ROOT = Path(__file__).resolve().parent.parent

# Step numbers for progress (1–9: first name, last name, email, phone, street, city, state, zip, dob; country=US, no middle initial)
ORDER_STEP_TOTAL = 9

def _main_menu_keyboard(show_retry: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📋 Create order"), KeyboardButton(text="📦 My orders")],
        [KeyboardButton(text="❓ Help")],
    ]
    if show_retry:
        rows.insert(0, [KeyboardButton(text="🔄 Retry")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Choose an option or type a reply below…",
    )

def _cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Cancel")]],
        resize_keyboard=True,
    )


class OrderStates(StatesGroup):
    first_name = State()
    last_name = State()
    email = State()
    phone = State()
    street = State()
    city = State()
    state = State()
    zip = State()
    date_of_birth = State()


class RetryStates(StatesGroup):
    """States for retry flows (e.g. re-enter phone when previous was invalid)."""
    retry_phone = State()


class BotRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repo = SupabaseRepo(settings.supabase_url, settings.supabase_service_role_key)
        self.btcpay = BTCPayClient(settings.btcpay_url, settings.btcpay_api_key, settings.btcpay_store_id)
        self.bot = Bot(token=settings.telegram_bot_token)
        self.dp = Dispatcher()
        self.router = Router()
        self.dp.include_router(self.router)

        template_path = ROOT / "modules" / "profile_builder" / "profile_template.json"
        cap_steps_path = ROOT / "modules" / "capital_one" / "steps.json"
        fp_steps_path = ROOT / "modules" / "first_premier" / "steps.json"
        self.default_profile_template = json.loads(template_path.read_text(encoding="utf-8"))
        self.capital_one_steps = json.loads(cap_steps_path.read_text(encoding="utf-8"))
        self.first_premier_steps = json.loads(fp_steps_path.read_text(encoding="utf-8"))

        checkpoint_store = FileWorkflowCheckpointStore(ROOT / "data" / "workflow_checkpoints")
        self.workflow_queue = FullWorkflowQueueService(
            template=self.default_profile_template,
            capital_one_steps=self.capital_one_steps,
            first_premier_steps=self.first_premier_steps,
            concurrency_limit=settings.workflow_concurrency,
            adspower_api_base=settings.adspower_api_base,
            checkpoint_store=checkpoint_store,
            on_job_done=self.on_job_done,
            on_progress=self._on_workflow_progress,
        )

        self._setup_handlers()
        self.router.error()(self._handle_update_error)

    async def _handle_update_error(self, event: ErrorEvent) -> None:
        """Log errors during update processing so one bad update doesn't crash the bot."""
        exc = event.exception
        LOG.warning(
            "Update processing error (%s): %s. Update will be skipped.",
            type(exc).__name__,
            exc,
            exc_info=False,
        )

    def _setup_handlers(self) -> None:
        # Cancel and Help first so they work even inside order flow
        self.router.message.register(
            self.handle_cancel,
            or_f(Command("cancel"), F.text == "❌ Cancel", F.text == "Cancel"),
        )
        self.router.message.register(self.handle_start, Command("start"))
        self.router.message.register(
            self.handle_order,
            or_f(Command("order"), F.text == "📋 Create order", F.text == "Create order"),
        )
        self.router.message.register(
            self.handle_status,
            or_f(Command("status"), F.text == "📦 My orders", F.text == "My orders"),
        )
        self.router.message.register(
            self.handle_retry,
            or_f(Command("retry"), F.text == "🔄 Retry", F.text == "Retry"),
        )
        self.router.message.register(
            self.handle_help,
            or_f(Command("help"), F.text == "❓ Help", F.text == "Help"),
        )

        self.router.message.register(self.capture_first_name, OrderStates.first_name)
        self.router.message.register(self.capture_last_name, OrderStates.last_name)
        self.router.message.register(self.capture_email, OrderStates.email)
        self.router.message.register(self.capture_phone, OrderStates.phone)
        self.router.message.register(self.capture_street, OrderStates.street)
        self.router.message.register(self.capture_city, OrderStates.city)
        self.router.message.register(self.capture_state, OrderStates.state)
        self.router.message.register(self.capture_zip, OrderStates.zip)
        self.router.message.register(self.capture_dob, OrderStates.date_of_birth)
        self.router.message.register(self.capture_retry_phone, RetryStates.retry_phone)
        self.router.callback_query.register(self.handle_csv_download, F.data.startswith("csv:"))

    async def handle_start(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "👋 <b>Welcome to CPN Marketplace</b>\n\n"
            "Here you can order a <b>CPN package</b>: we run the full workflow and send you "
            "a valid CPN with details in a CSV file.\n\n"
            "• <b>Create order</b> — Enter your details and pay with BTC or LTC.\n"
            "• <b>My orders</b> — Check status of your latest order.\n"
            "• <b>Help</b> — Commands and support.\n\n"
            "Use the menu below or type /order to get started.",
            reply_markup=_main_menu_keyboard(),
            parse_mode="HTML",
        )

    async def handle_help(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "📖 <b>Commands</b>\n\n"
            "/start — Show this menu\n"
            "/order — Start a new CPN order\n"
            "/status — Check your latest order\n"
            "/retry — Retry a failed order (no new payment)\n"
            "/cancel — Cancel current order form\n\n"
            "You can also use the buttons below instead of typing commands.",
            reply_markup=_main_menu_keyboard(),
            parse_mode="HTML",
        )

    async def handle_cancel(self, message: Message, state: FSMContext) -> None:
        current = await state.get_state()
        if current is None:
            await message.answer("Nothing to cancel. Use the menu to start an order.", reply_markup=_main_menu_keyboard())
            return
        await state.clear()
        await message.answer(
            "Order form cancelled. Use <b>Create order</b> when you’re ready.",
            reply_markup=_main_menu_keyboard(),
            parse_mode="HTML",
        )

    async def handle_order(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(OrderStates.first_name)
        await state.update_data(address={})
        await message.answer(
            "📋 <b>New order</b>\n\n"
            "We’ll ask for your details in <b>9 short steps</b> (name, email, phone, address, date of birth). "
            "Then you’ll get a payment link (BTC or LTC). After payment we run the workflow and send you the CPN CSV.\n\n"
            "You can cancel anytime with the button below.\n\n"
            "━━━━━━━━━━━━━━\n"
            "Step <b>1/9</b> — <b>First name</b>\n"
            "Enter your legal first name:",
            reply_markup=_cancel_keyboard(),
            parse_mode="HTML",
        )

    async def handle_status(self, message: Message) -> None:
        user = await self.repo.get_user_by_telegram_id(message.from_user.id)  # type: ignore[arg-type]
        if not user:
            await message.answer(
                "📦 You don’t have any orders yet.\n\nUse <b>Create order</b> to place your first order.",
                reply_markup=_main_menu_keyboard(),
                parse_mode="HTML",
            )
            return
        orders = await self.repo.get_orders_for_user(user["id"], limit=10)
        if not orders:
            await message.answer(
                "📦 You don’t have any orders yet.\n\nUse <b>Create order</b> to place your first order.",
                reply_markup=_main_menu_keyboard(),
                parse_mode="HTML",
            )
            return
        status_emoji = {
            "pending_payment": "⏳",
            "paid": "✅",
            "processing": "🔄",
            "completed": "✅",
            "failed": "❌",
        }
        lines = ["📦 <b>Your orders</b>\n"]
        inline_buttons: list[list[InlineKeyboardButton]] = []
        is_failed = any(o["status"] == "failed" for o in orders)
        for order in orders:
            emoji = status_emoji.get(order["status"], "•")
            label = order["status"].replace("_", " ").title()
            cpns_paid = int(order.get("cpns_paid") or 1)
            cpns_delivered = int(order.get("cpns_delivered") or 0)
            lines.append(f"{emoji} <b>{order['id'][:8]}…</b> — {label}")
            if cpns_paid > 1:
                lines.append(f"   CPNs: {cpns_delivered}/{cpns_paid}")
            csv_path = order.get("result_csv_path")
            if order["status"] == "completed" and csv_path:
                inline_buttons.append([
                    InlineKeyboardButton(
                        text=f"📥 Download CSV ({order['id'][:8]}…)",
                        callback_data=f"csv:{order['id']}",
                    )
                ])
        body = "\n".join(lines)
        if is_failed:
            body += "\n\nYou can retry failed orders. Use <b>Retry</b> below."
        else:
            body += "\n\nNeed another? Use <b>Create order</b>."
        reply_markup: ReplyKeyboardMarkup | InlineKeyboardMarkup = _main_menu_keyboard(show_retry=is_failed)
        if inline_buttons:
            reply_markup = InlineKeyboardMarkup(inline_keyboard=inline_buttons)
        await message.answer(body, reply_markup=reply_markup, parse_mode="HTML")
        if inline_buttons:
            await message.answer(
                "Use the menu below for other actions.",
                reply_markup=_main_menu_keyboard(show_retry=is_failed),
                parse_mode="HTML",
            )

    async def handle_csv_download(self, callback: CallbackQuery) -> None:
        """Send CSV file when user clicks Download CSV button."""
        await callback.answer()
        order_id = (callback.data or "").removeprefix("csv:")
        if not order_id:
            return
        user = await self.repo.get_user_by_telegram_id(callback.from_user.id)  # type: ignore[arg-type]
        if not user:
            await callback.message.answer("You don't have any orders.")
            return
        order = await self.repo.get_order_by_id(order_id)
        if not order or str(order.get("user_id")) != str(user["id"]):
            await callback.message.answer("Order not found or access denied.")
            return
        csv_path = order.get("result_csv_path")
        if not csv_path or order.get("status") != "completed":
            await callback.message.answer("CSV not available for this order.")
            return
        path = ROOT / csv_path if not Path(csv_path).is_absolute() else Path(csv_path)
        if not path.exists():
            await callback.message.answer("CSV file no longer available.")
            return
        doc = BufferedInputFile(path.read_bytes(), filename=f"cpn_{order_id}.csv")
        await callback.message.answer_document(doc)

    async def handle_retry(self, message: Message, state: FSMContext) -> None:
        """Retry a failed paid order without requiring a new payment."""
        await state.clear()
        user = await self.repo.get_user_by_telegram_id(message.from_user.id)  # type: ignore[arg-type]
        if not user:
            await message.answer(
                "You don't have any orders. Use <b>Create order</b> to place one.",
                reply_markup=_main_menu_keyboard(),
                parse_mode="HTML",
            )
            return
        order = await self.repo.get_latest_order_for_user(user["id"])
        if not order:
            await message.answer(
                "You don't have any orders. Use <b>Create order</b> to place one.",
                reply_markup=_main_menu_keyboard(),
                parse_mode="HTML",
            )
            return
        if order["status"] != "failed":
            await message.answer(
                "Only failed orders can be retried. Your latest order is not in a failed state.\n\n"
                "Use <b>My orders</b> to check status.",
                reply_markup=_main_menu_keyboard(),
                parse_mode="HTML",
            )
            return
        # Failed orders have already been paid (workflow ran and then failed)
        order_id = order["id"]
        cpns_paid = int(order.get("cpns_paid") or 1)
        cpns_delivered = int(order.get("cpns_delivered") or 0)
        if cpns_delivered >= cpns_paid:
            await message.answer(
                "This order is already fulfilled. Use <b>Create order</b> for a new order.",
                reply_markup=_main_menu_keyboard(),
                parse_mode="HTML",
            )
            return

        # Check if failure was due to invalid phone — prompt for new number before retrying
        wf_job = await self.repo.get_latest_workflow_job_for_order(order_id)
        wf_error = (wf_job.get("error") or "") if wf_job else ""
        if self._is_invalid_phone_error(wf_error):
            await state.set_state(RetryStates.retry_phone)
            await state.update_data(retry_order_id=order_id, retry_order=order)
            await message.answer(
                "📱 <b>Invalid phone number</b>\n\n"
                "Your order failed because the phone number was not accepted. "
                "Please enter a <b>valid 10-digit US phone number</b> (e.g. 312-555-1234):",
                reply_markup=_cancel_keyboard(),
                parse_mode="HTML",
            )
            return

        await self._submit_retry_job(order, message)

    def _is_invalid_phone_error(self, error: str | None) -> bool:
        """True if the workflow error indicates an invalid phone number."""
        if not error:
            return False
        lower = error.lower()
        return (
            "valid 10-digit phone" in lower
            or "invalid phone" in lower
            or "invalid phone number" in lower
        )

    async def _submit_retry_job(self, order: dict, message: Message) -> None:
        """Submit the retry workflow job for the given order."""
        order_id = order["id"]
        await self.repo.mark_order_processing(order_id)
        workflow_state = workflow_state_from_profile(
            order["profile_snapshot"], self.settings.workflow_state
        )
        telegram_id = message.from_user.id
        request_obj = WorkflowJobRequest(
            user_id=str(order["user_id"]),
            state=workflow_state,
            stop_after=self.settings.workflow_stop_after,
            template=order["profile_snapshot"],
            metadata={"order_id": order_id, "telegram_id": telegram_id},
        )
        job_id = await self.workflow_queue.submit_job(request_obj)
        await self.repo.create_workflow_job(order_id, job_id)
        await message.answer(
            "🔄 <b>Retry started</b>\n\n"
            "We're processing your order again. You'll see progress updates here. "
            "No additional payment is required.",
            reply_markup=_main_menu_keyboard(),
            parse_mode="HTML",
        )

    async def capture_retry_phone(self, message: Message, state: FSMContext) -> None:
        """Capture new phone number when retrying after invalid phone error."""
        raw = (message.text or "").strip()
        ok, err = validate_phone(raw)
        if not ok:
            await message.answer(
                f"❌ {err}\n\n"
                "Please enter a valid 10-digit US phone number (e.g. 312-555-1234):",
                reply_markup=_cancel_keyboard(),
            )
            return
        data = await state.get_data()
        order = data.get("retry_order")
        order_id = data.get("retry_order_id")
        await state.clear()
        if not order or not order_id:
            await message.answer("Session expired. Use <b>Retry</b> again.", reply_markup=_main_menu_keyboard(), parse_mode="HTML")
            return

        profile = dict(order["profile_snapshot"])
        profile["phone"] = raw
        order["profile_snapshot"] = profile
        await self.repo.update_order_profile_snapshot(order_id, profile)

        await self._submit_retry_job(order, message)

    async def capture_first_name(self, message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        ok, err = validate_first_name(raw)
        if not ok:
            await message.answer(
                f"❌ {err}\n\nStep <b>1/{ORDER_STEP_TOTAL}</b> — <b>First name</b>\n"
                "Enter your legal first name (letters only, no numbers):",
                parse_mode="HTML",
            )
            return
        await state.update_data(first_name=raw)
        await state.set_state(OrderStates.last_name)
        await message.answer(
            f"Step <b>2/{ORDER_STEP_TOTAL}</b> — <b>Last name</b>\n"
            "Enter your legal last name:",
            parse_mode="HTML",
        )

    async def capture_last_name(self, message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        ok, err = validate_last_name(raw)
        if not ok:
            await message.answer(
                f"❌ {err}\n\nStep <b>2/{ORDER_STEP_TOTAL}</b> — <b>Last name</b>\n"
                "Enter your legal last name (letters only, no numbers):",
                parse_mode="HTML",
            )
            return
        await state.update_data(last_name=raw)
        await state.set_state(OrderStates.email)
        await message.answer(
            f"Step <b>3/{ORDER_STEP_TOTAL}</b> — <b>Email</b>\n"
            "A valid email we can use for the profile (e.g. name@example.com):",
            parse_mode="HTML",
        )

    async def capture_email(self, message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        ok, err = validate_email(raw)
        if not ok:
            await message.answer(
                f"❌ {err}\n\nStep <b>3/{ORDER_STEP_TOTAL}</b> — <b>Email</b>\n"
                "Enter a valid email (e.g. name@example.com):",
                parse_mode="HTML",
            )
            return
        await state.update_data(email=raw)
        await state.set_state(OrderStates.phone)
        await message.answer(
            f"Step <b>4/{ORDER_STEP_TOTAL}</b> — <b>Phone</b>\n"
            "Phone number (e.g. 312-555-1234):",
            parse_mode="HTML",
        )

    async def capture_phone(self, message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        ok, err = validate_phone(raw)
        if not ok:
            await message.answer(
                f"❌ {err}\n\nStep <b>4/{ORDER_STEP_TOTAL}</b> — <b>Phone</b>\n"
                "Enter a valid US phone with at least 10 digits (e.g. 312-555-1234):",
                parse_mode="HTML",
            )
            return
        await state.update_data(phone=raw)
        await state.set_state(OrderStates.street)
        await message.answer(
            f"Step <b>5/{ORDER_STEP_TOTAL}</b> — <b>Street address</b>\n"
            "Street address only (e.g. 123 Main St). Do not include city, state, or ZIP.",
            parse_mode="HTML",
        )

    async def capture_street(self, message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        ok, err = validate_street(raw)
        if not ok:
            await message.answer(
                f"❌ {err}\n\nStep <b>5/{ORDER_STEP_TOTAL}</b> — <b>Street address</b>\n"
                "Street address only (e.g. 123 Main St). Do not include city, state, or ZIP.",
                parse_mode="HTML",
            )
            return
        data = await state.get_data()
        address = dict(data.get("address") or {})
        address["street"] = raw
        await state.update_data(address=address)
        await state.set_state(OrderStates.city)
        await message.answer(
            f"Step <b>6/{ORDER_STEP_TOTAL}</b> — <b>City</b>\n"
            "City name:",
            parse_mode="HTML",
        )

    async def capture_city(self, message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        ok, err = validate_city(raw)
        if not ok:
            await message.answer(
                f"❌ {err}\n\nStep <b>6/{ORDER_STEP_TOTAL}</b> — <b>City</b>\n"
                "Enter city name (letters only, no numbers):",
                parse_mode="HTML",
            )
            return
        data = await state.get_data()
        address = dict(data.get("address") or {})
        address["city"] = raw
        await state.update_data(address=address)
        await state.set_state(OrderStates.state)
        await message.answer(
            f"Step <b>7/{ORDER_STEP_TOTAL}</b> — <b>State</b>\n"
            "2-letter state code (e.g. TX, FL, CA):",
            parse_mode="HTML",
        )

    async def capture_state(self, message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        ok, err = validate_state(raw)
        if not ok:
            await message.answer(
                f"❌ {err}\n\nStep <b>7/{ORDER_STEP_TOTAL}</b> — <b>State</b>\n"
                "Enter exactly 2 letters (e.g. TX, FL, CA):",
                parse_mode="HTML",
            )
            return
        data = await state.get_data()
        address = dict(data.get("address") or {})
        address["state"] = raw.upper()
        await state.update_data(address=address)
        await state.set_state(OrderStates.zip)
        await message.answer(
            f"Step <b>8/{ORDER_STEP_TOTAL}</b> — <b>ZIP code</b>\n"
            "ZIP code (5 digits):",
            parse_mode="HTML",
        )

    async def capture_zip(self, message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip().replace(" ", "")
        ok, err = validate_zip(raw)
        if not ok:
            await message.answer(
                f"❌ {err}\n\nStep <b>8/{ORDER_STEP_TOTAL}</b> — <b>ZIP code</b>\n"
                "Enter 5 digits (e.g. 77864):",
                parse_mode="HTML",
            )
            return
        data = await state.get_data()
        address = dict(data.get("address") or {})
        address["zip"] = raw
        address["country"] = "United States"
        await state.update_data(address=address)
        await state.set_state(OrderStates.date_of_birth)
        await message.answer(
            f"Step <b>9/{ORDER_STEP_TOTAL}</b> — <b>Date of birth</b>\n"
            "Format: MM/DD/YYYY (e.g. 01/15/1990):",
            parse_mode="HTML",
        )

    async def capture_dob(self, message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        ok, err = validate_date_of_birth(raw)
        if not ok:
            await message.answer(
                f"❌ {err}\n\nStep <b>9/{ORDER_STEP_TOTAL}</b> — <b>Date of birth</b>\n"
                "Use format MM/DD/YYYY (e.g. 01/15/1990):",
                parse_mode="HTML",
            )
            return
        await state.update_data(date_of_birth=raw)
        profile_input = await state.get_data()
        await state.clear()

        merged_profile = merge_profile(self.default_profile_template, profile_input)
        errors = validate_profile_input(merged_profile)
        if errors:
            await message.answer(
                "❌ <b>Please fix the following:</b>\n\n" + "\n".join(f"• {e}" for e in errors),
                reply_markup=_main_menu_keyboard(),
                parse_mode="HTML",
            )
            return

        creating = await message.answer(
            "⏳ Creating your order…" + ("" if self.settings.payment_enabled else " (payment disabled for testing)")
        )
        user = await self.repo.ensure_user(message.from_user.id, message.from_user.username)  # type: ignore[arg-type]
        order = await self.repo.create_order(
            user_id=user["id"],
            amount_usd=self.settings.order_amount_usd,
            profile_snapshot=merged_profile,
        )

        if not self.settings.payment_enabled:
            await self.repo.mark_order_paid(order["id"])
            await self.repo.mark_order_processing(order["id"])
            workflow_state = workflow_state_from_profile(
                order["profile_snapshot"], self.settings.workflow_state
            )
            telegram_id = message.from_user.id
            request_obj = WorkflowJobRequest(
                user_id=str(order["user_id"]),
                state=workflow_state,
                stop_after=self.settings.workflow_stop_after,
                template=order["profile_snapshot"],
                metadata={"order_id": order["id"], "telegram_id": telegram_id},
            )
            job_id = await self.workflow_queue.submit_job(request_obj)
            await self.repo.create_workflow_job(order["id"], job_id)
            await creating.edit_text(
                "✅ <b>Order submitted</b> (payment skipped for testing)\n\n"
                f"🆔 Order ID: <code>{order['id']}</code>\n\n"
                "We’re running the workflow now. We’ll notify you here when it’s done (or if it fails).\n\n"
                "Use <b>My orders</b> to check status.",
                parse_mode="HTML",
            )
            await message.answer(
                "Use <b>My orders</b> to check status, or the menu to start another order.",
                reply_markup=_main_menu_keyboard(),
                parse_mode="HTML",
            )
            return

        notification_url = f"{self.settings.webhook_public_base_url}/webhooks/btcpay"
        invoice = await self.btcpay.create_invoice(
            order_id=order["id"],
            amount_usd=self.settings.order_amount_usd,
            notification_url=notification_url,
        )
        invoice_id = invoice.get("id")
        checkout_url = invoice.get("checkoutLink") or invoice.get("url")
        if not invoice_id or not checkout_url:
            await creating.edit_text(
                "❌ Failed to create payment invoice. Please try again.",
            )
            return

        await self.repo.set_order_invoice(order["id"], invoice_id, checkout_url)
        pay_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Open payment page", url=checkout_url)],
        ])
        await creating.edit_text(
            "✅ <b>Order created</b>\n\n"
            f"🆔 Order ID: <code>{order['id']}</code>\n"
            f"💵 Amount: <b>${self.settings.order_amount_usd:.2f}</b> USD\n\n"
            "Pay with <b>BTC</b> or <b>LTC</b> using the button below. "
            "After payment we’ll run the workflow and send you the CPN CSV here.",
            reply_markup=pay_kb,
            parse_mode="HTML",
        )
        await message.answer(
            "Use <b>My orders</b> to check status, or the menu to start another order.",
            reply_markup=_main_menu_keyboard(),
            parse_mode="HTML",
        )

    async def _on_workflow_progress(self, record: WorkflowJobRecord, stage: str) -> None:
        """Send a progress update to the user so they see the workflow is running."""
        telegram_id = record.request.metadata.get("telegram_id")
        if telegram_id is None:
            return
        try:
            await self.bot.send_message(int(telegram_id), stage, parse_mode="HTML")
        except Exception as e:
            LOG.warning("Could not send progress to user %s: %s", telegram_id, e)

    async def on_job_done(self, record: WorkflowJobRecord) -> None:
        order_id = str(record.request.metadata.get("order_id", ""))
        telegram_id = record.request.metadata.get("telegram_id")
        started_at = _epoch_to_iso(record.started_at)
        completed_at = _epoch_to_iso(record.ended_at)
        await self.repo.update_workflow_job(
            job_id=record.job_id,
            status=record.status,
            result=record.result,
            error=record.error,
            started_at_iso=started_at,
            completed_at_iso=completed_at,
        )

        if not order_id:
            return

        if record.status == "succeeded" and record.result and record.result.get("profile"):
            profile = record.result["profile"]
            csv_bytes = build_profile_csv_bytes(profile)
            file_path = persist_csv(order_id, csv_bytes, ROOT / "data" / "workflow_csv")
            # Only complete if order still owes CPNs (prevents over-generation)
            updated = await self.repo.complete_order(order_id, str(file_path))
            if updated and telegram_id is not None:
                doc = BufferedInputFile(csv_bytes, filename=f"cpn_{order_id}.csv")
                await self.bot.send_message(
                    int(telegram_id),
                    "✅ <b>Your CPN is ready</b>\n\nYour workflow completed successfully. CSV with CPN and details is attached below.",
                    parse_mode="HTML",
                )
                await self.bot.send_document(int(telegram_id), doc)
        else:
            await self.repo.fail_order(order_id)
            if telegram_id is not None:
                base = "❌ <b>Something went wrong</b>\n\nWe couldn't complete your order.\n\n"
                if self._is_invalid_phone_error(record.error):
                    base += "Your order failed because the phone number was invalid. Use <b>Retry</b> and enter a new valid 10-digit phone number when prompted."
                else:
                    base += "You can retry from <b>My orders</b> without paying again."
                await self.bot.send_message(
                    int(telegram_id),
                    base,
                    parse_mode="HTML",
                    reply_markup=_main_menu_keyboard(show_retry=True),
                )

    async def handle_btcpay_webhook(self, request: web.Request) -> web.Response:
        body = await request.read()
        if self.settings.btcpay_webhook_secret:
            header_sig = request.headers.get("BTCPay-Sig") or request.headers.get("Btcpay-Sig")
            if not _verify_hmac(self.settings.btcpay_webhook_secret, body, header_sig):
                return web.Response(status=401, text="invalid signature")

        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return web.Response(status=400, text="invalid json")

        invoice_id = str(payload.get("invoiceId") or payload.get("id") or "").strip()
        if not invoice_id:
            return web.Response(status=400, text="missing invoice id")

        invoice = await self.btcpay.get_invoice(invoice_id)
        if not invoice_is_paid(invoice):
            return web.Response(status=200, text="invoice not paid")

        order = await self.repo.get_order_by_invoice(invoice_id)
        if not order:
            return web.Response(status=404, text="order not found")
        if order["status"] in {"processing", "completed"}:
            return web.Response(status=200, text="already processed")

        await self.repo.mark_order_paid(order["id"])
        await self.repo.mark_order_processing(order["id"])
        await self.repo.insert_payment(
            order_id=order["id"],
            invoice_id=invoice_id,
            status=str(invoice.get("status", "unknown")),
            amount=float(invoice.get("amount", 0)) if invoice.get("amount") is not None else None,
            currency=invoice.get("currency"),
            payload=payload,
            paid_at_iso=datetime.now(tz=timezone.utc).isoformat(),
        )

        user = await self.repo.get_user_by_id(order["user_id"])
        telegram_id = user["telegram_id"] if user else None
        # State for SSN/CPN workflow: from buyer's profile (address.state), fallback to env
        workflow_state = workflow_state_from_profile(
            order["profile_snapshot"], self.settings.workflow_state
        )
        request_obj = WorkflowJobRequest(
            user_id=str(order["user_id"]),
            state=workflow_state,
            stop_after=self.settings.workflow_stop_after,
            template=order["profile_snapshot"],
            metadata={"order_id": order["id"], "telegram_id": telegram_id},
        )
        job_id = await self.workflow_queue.submit_job(request_obj)
        await self.repo.create_workflow_job(order["id"], job_id)
        if telegram_id is not None:
            try:
                await self.bot.send_message(
                    int(telegram_id),
                    "✅ <b>Payment received.</b> We've started processing your order. "
                    "You'll see progress updates below so you know it's working.",
                    parse_mode="HTML",
                )
            except Exception as e:
                LOG.warning("Could not send payment-received message to user %s: %s", telegram_id, e)
        return web.Response(status=200, text="ok")

    async def _run_polling(self) -> None:
        """Run Telegram polling (used inside restart loop)."""
        await self.dp.start_polling(self.bot)

    async def run(self) -> None:
        await self.workflow_queue.start()
        app = web.Application()
        app.router.add_post("/webhooks/btcpay", self.handle_btcpay_webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host=self.settings.webhook_host, port=self.settings.webhook_port)
        await site.start()
        LOG.info("Webhook server started on %s:%s", self.settings.webhook_host, self.settings.webhook_port)

        restart_delay = 30
        while True:
            try:
                await self._run_polling()
            except asyncio.CancelledError:
                LOG.info("Polling cancelled (shutdown)")
                break
            except Exception as exc:
                err_type = type(exc).__name__
                err_msg = str(exc) or "unknown"
                if "network" in err_msg.lower() or "connection" in err_msg.lower() or "aborted" in err_msg.lower():
                    LOG.warning(
                        "Telegram connection error (%s): %s. Restarting polling in %s seconds.",
                        err_type,
                        err_msg,
                        restart_delay,
                    )
                else:
                    LOG.exception("Polling stopped with error (%s): %s. Restarting in %s seconds.", err_type, err_msg, restart_delay)
                await asyncio.sleep(restart_delay)
                continue
            else:
                LOG.warning("Polling exited normally; restarting in %s seconds.", restart_delay)
                await asyncio.sleep(restart_delay)
        await self.workflow_queue.shutdown()
        await runner.cleanup()
        await self.bot.session.close()


def _verify_hmac(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not signature_header:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    signature = signature_header.replace("sha256=", "").strip()
    return hmac.compare_digest(expected, signature)


def _epoch_to_iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _start_ngrok(port: int) -> tuple[subprocess.Popen | None, str | None]:
    """Start ngrok tunneling to the given port; return (process, public_url)."""
    token = (os.getenv("NGROK_AUTHTOKEN") or "").strip()
    if not token or token == "paste_your_authtoken_here":
        LOG.warning("USE_NGROK is set but NGROK_AUTHTOKEN is missing or placeholder; skipping ngrok")
        return None, None
    ngrok_exe = os.getenv("NGROK_PATH") or (r"C:\Program Files\ngrok\ngrok.exe" if os.name == "nt" else "ngrok")
    cmd = [ngrok_exe, "http", str(port)]
    reserved = (os.getenv("NGROK_URL") or "").strip()
    if reserved:
        cmd.insert(2, f"--url={reserved}")
    env = os.environ.copy()
    env["NGROK_AUTHTOKEN"] = token
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        LOG.warning("ngrok executable not found at %s; set NGROK_PATH in .env", ngrok_exe)
        return None, None
    # Poll local ngrok API for tunnel URL (up to 30s)
    api_url = "http://127.0.0.1:4040/api/tunnels"
    for _ in range(60):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(api_url, timeout=2) as r:
                data = json.loads(r.read().decode())
            tunnels = data.get("tunnels") or []
            if tunnels and isinstance(tunnels[0].get("public_url"), str):
                url = (tunnels[0]["public_url"] or "").rstrip("/")
                if url:
                    LOG.info("ngrok started: %s → localhost:%s", url, port)
                    return proc, url
        except Exception:
            pass
    LOG.warning("ngrok started but tunnel URL not available within 30s")
    proc.terminate()
    return None, None


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Treat Telegram network/connection errors as WARNING (we retry automatically)
    for _name in ("aiogram.dispatcher", "aiogram.event"):
        _log = logging.getLogger(_name)
        _log.addFilter(_NetworkErrorFilter())
    load_dotenv()
    settings = load_settings()
    ngrok_proc = None
    if settings.payment_enabled and _use_ngrok():
        ngrok_proc, public_url = _start_ngrok(settings.webhook_port)
        if public_url:
            settings = replace(settings, webhook_public_base_url=public_url)
            LOG.info("BTCPay webhook URL: %s/webhooks/btcpay", public_url)
        elif ngrok_proc:
            ngrok_proc.terminate()
            ngrok_proc = None
    try:
        runtime = BotRuntime(settings)
        await runtime.run()
    finally:
        if ngrok_proc is not None:
            ngrok_proc.terminate()
            ngrok_proc.wait(timeout=5)
            LOG.info("ngrok stopped")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
