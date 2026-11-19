import asyncio
import html
import json
import re
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from .browser import BrowserManager
from .config import settings
from .state import AccountState
from .telegram import TelegramBot

# Force real-time logs on Railway Deploy Logs
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist_time() -> str:
    return datetime.now(IST).strftime("%I:%M:%S %p")


def now_ist_full() -> str:
    return datetime.now(IST).strftime("%I:%M:%S %p, %d %b %Y IST")


def esc(val) -> str:
    return html.escape(str(val if val is not None else ""))


def log_step(slot_id: str, msg: str):
    now = datetime.now(IST).strftime("%H:%M:%S")
    print(f"[{now}] [{slot_id}] {msg}", flush=True)


class AccountController:

    VALID_DENOMS = ("1000", "2000", "3000", "4000", "5000")
    VALID_ABORT_PCTS = (5, 10, 15, 20, 25, 30, 35, 40, 45, 50)

    def __init__(
        self,
        browser: BrowserManager,
        telegram: TelegramBot,
    ):
        self.browser = browser
        self.telegram = telegram

        self.accounts: Dict[str, AccountState] = {}

        self.screenshots_dir = Path(settings.screenshots_dir)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

        # Persistent Target Denomination, Quantity & Surge Abort Limit
        self.settings_file = Path(settings.sessions_dir) / "swarm_target_config.json"
        self.target_denom: str = "5000"
        self.target_qty: int = 1
        self.abort_limit_pct: Optional[int] = None  # None = OFF, or 5..50
        self._load_target_config()

        self.stock_event = asyncio.Event()
        self.sniper_tasks: Dict[str, asyncio.Task] = {}

        # Interactive /start wizard state per chat_id
        self.wizard_state: Dict[str, Dict] = {}

        # Anti-spam cooldown for repeated abort alerts per slot
        self._last_abort_alert: Dict[str, float] = {}

        self._create_slots()
        log_step(
            "SYSTEM",
            f"Controller initialized | Target: Rs.{self.target_denom} | Qty: {self.target_qty} | Abort: {self._abort_label()}",
        )

    # =========================================================
    # PERSISTENT CONFIG (DENOM, QTY, ABORT LIMIT)
    # =========================================================

    def _abort_label(self) -> str:
        return f"≤ {self.abort_limit_pct}%" if self.abort_limit_pct is not None else "OFF (No Limit)"

    def _load_target_config(self):
        try:
            if self.settings_file.exists():
                data = json.loads(self.settings_file.read_text(encoding="utf-8"))
                denom = str(data.get("target_denom", "5000")).strip()
                qty = int(data.get("target_qty", 1))
                abort_raw = data.get("abort_limit_pct", None)
                if denom in self.VALID_DENOMS:
                    self.target_denom = denom
                if qty in (1, 2):
                    self.target_qty = qty
                if abort_raw is None:
                    self.abort_limit_pct = None
                else:
                    abort_int = int(abort_raw)
                    if abort_int in self.VALID_ABORT_PCTS:
                        self.abort_limit_pct = abort_int
        except Exception as e:
            log_step("CONFIG", f"Target config load note: {e}")

    def _save_target_config(self):
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            self.settings_file.write_text(
                json.dumps(
                    {
                        "target_denom": self.target_denom,
                        "target_qty": self.target_qty,
                        "abort_limit_pct": self.abort_limit_pct,
                    }
                ),
                encoding="utf-8",
            )
        except Exception as e:
            log_step("CONFIG", f"Target config save note: {e}")

    # =========================================================
    # SLOT MANAGEMENT & AUTO SESSION RESTORE
    # =========================================================

    def _create_slots(self):
        service_number = settings.current_service_id
        for account_number in range(1, settings.accounts_per_service + 1):
            slot_id = f"S{service_number}-A{account_number}"
            state = AccountState(slot_id=slot_id)

            try:
                session_dir = self.browser.sessions.session_path(slot_id)
                storage_file = session_dir / "storage_state.json"
                if storage_file.exists() and storage_file.stat().st_size > 10:
                    saved_phone = self.browser.load_saved_phone(slot_id)
                    state.logged_in = True
                    state.active = True
                    state.phone_number = saved_phone or "Saved Session"
                    state.current_page = "✅ Session Restored (Auto-Starting...)"
                    log_step(slot_id, f"Restored saved session (ID: {state.phone_number})")
            except Exception as e:
                log_step(slot_id, f"Session restore note: {e}")

            self.accounts[slot_id] = state

    def _ensure_snipers_running(self) -> int:
        started = 0
        for slot_id, acc in self.accounts.items():
            if acc.logged_in:
                task = self.sniper_tasks.get(slot_id)
                if task is None or task.done():
                    self.sniper_tasks[slot_id] = asyncio.create_task(self._sniper_worker(slot_id))
                    started += 1
        return started

    def get_account(self, slot_id: str) -> AccountState:
        account = self.accounts.get(slot_id.upper())
        if account is None:
            raise ValueError(f"Unknown slot: {slot_id}")
        return account

    def all_accounts(self) -> List[AccountState]:
        return list(self.accounts.values())

    # =========================================================
    # COMMAND & INTERACTIVE HANDLER REGISTRATION
    # =========================================================

    def connect_commands(self):
        self.telegram.register_command("start", self.handle_start)
        self.telegram.register_command("status", self.handle_status)
        self.telegram.register_command("denom", self.handle_set_denom)
        self.telegram.register_command("target", self.handle_set_denom)
        self.telegram.register_command("code", self.handle_set_denom)
        self.telegram.register_command("qty", self.handle_set_qty)
        self.telegram.register_command("abort", self.handle_set_abort)
        self.telegram.register_command("login", self.handle_login)
        self.telegram.register_command("otp", self.handle_otp)
        self.telegram.register_command("cookie", self.handle_cookie_import)
        self.telegram.register_command("popup", self.handle_force_popup)
        self.telegram.register_command("logout", self.handle_logout)
        self.telegram.register_command("screen", self.handle_screen)
        self.telegram.register_command("refresh", self.handle_refresh)
        self.telegram.register_command("help", self.handle_help)
        self.telegram.register_command("sniper", self.handle_sniper_mode)
        self.telegram.register_command("stop_sniper", self.handle_stop_sniper)
        self.telegram.register_command("trigger_buy", self.handle_trigger_buy)
        self.telegram.register_command("heal", self.handle_heal)

        # Smart Interactive Button Wizard + Direct Input Handlers
        self.telegram.register_callback_query(self.handle_callback_query)
        self.telegram.register_text_handler(self.handle_wizard_text)
        self.telegram.register_document_handler(self.handle_wizard_document)

    # =========================================================
    # SMART INTERACTIVE MENU BUILDERS (/start)
    # =========================================================

    def _main_menu_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        logged_in = sum(1 for a in self.all_accounts() if a.logged_in)
        snipers_on = sum(
            1
            for sid, a in self.accounts.items()
            if a.logged_in and (t := self.sniper_tasks.get(sid)) is not None and not t.done()
        )
        face_val = int(self.target_denom)
        if self.abort_limit_pct is not None:
            max_per = face_val * (1.0 + self.abort_limit_pct / 100.0)
            abort_str = f"≤ {self.abort_limit_pct}% (Max ₹{max_per:,.0f}/code)"
        else:
            abort_str = "OFF (No Price Cap)"

        text = (
            "🤖 <b>ROOTER SWARM ULTRA PRO</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>Target Code:</b> <code>Rs.{esc(self.target_denom)} Wallet Code</code>\n"
            f"📦 <b>Quantity:</b> <code>{self.target_qty}</code> "
            f"({'Direct Fast Buy' if self.target_qty == 1 else 'Double → 1 Fallback'})\n"
            f"🛡️ <b>Abort Limit:</b> <code>{esc(abort_str)}</code>\n"
            f"🔐 <b>Accounts:</b> <code>{logged_in}/{settings.total_slots}</code> | "
            f"🔫 <b>Snipers:</b> <code>{snipers_on}/{settings.total_slots}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "👇 Niche se category choose karo:"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔐 Login", callback_data="m:login"),
                    InlineKeyboardButton("🚪 Logout", callback_data="m:logout"),
                ],
                [
                    InlineKeyboardButton("🖥 Screen", callback_data="m:screen"),
                    InlineKeyboardButton("🔄 Refresh", callback_data="m:refresh"),
                ],
                [
                    InlineKeyboardButton("🎯 Target & Qty", callback_data="m:target"),
                    InlineKeyboardButton("🛡️ Abort Limit", callback_data="m:abort"),
                ],
                [
                    InlineKeyboardButton("🔫 Sniper Control", callback_data="m:sniper"),
                    InlineKeyboardButton("📊 Live Status", callback_data="m:status"),
                ],
                [
                    InlineKeyboardButton("⚡ Trigger Buy", callback_data="m:trigger"),
                    InlineKeyboardButton("🧹 Heal All", callback_data="m:heal"),
                ],
            ]
        )
        return text, keyboard

    def _slot_picker_keyboard(self, action_prefix: str, back_target: str = "m:home") -> InlineKeyboardMarkup:
        rows: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []
        for acc in self.all_accounts():
            icon = "✅" if acc.logged_in else "⚪"
            row.append(
                InlineKeyboardButton(
                    f"{icon} {acc.slot_id}",
                    callback_data=f"{action_prefix}:{acc.slot_id}",
                )
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("🔙 Back", callback_data=back_target)])
        return InlineKeyboardMarkup(rows)

    def _target_menu_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        text = (
            "🎯 <b>TARGET DENOMINATION & QUANTITY</b>\n\n"
            f"• Active Code: <code>Rs.{esc(self.target_denom)} Wallet Code</code>\n"
            f"• Active Quantity: <code>{self.target_qty}</code>\n\n"
            "👇 Tap karke instantly change karo:"
        )
        denom_row1 = [
            InlineKeyboardButton(
                f"{'✅ ' if self.target_denom == d else ''}Rs.{d}",
                callback_data=f"m:set_denom:{d}",
            )
            for d in ("1000", "2000", "3000")
        ]
        denom_row2 = [
            InlineKeyboardButton(
                f"{'✅ ' if self.target_denom == d else ''}Rs.{d}",
                callback_data=f"m:set_denom:{d}",
            )
            for d in ("4000", "5000")
        ]
        qty_row = [
            InlineKeyboardButton(
                f"{'✅ ' if self.target_qty == 1 else ''}Qty 1 (Fast)",
                callback_data="m:set_qty:1",
            ),
            InlineKeyboardButton(
                f"{'✅ ' if self.target_qty == 2 else ''}Qty 2 (Double)",
                callback_data="m:set_qty:2",
            ),
        ]
        kb = InlineKeyboardMarkup(
            [
                denom_row1,
                denom_row2,
                qty_row,
                [InlineKeyboardButton("🔙 Main Menu", callback_data="m:home")],
            ]
        )
        return text, kb

    def _abort_menu_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        face_val = int(self.target_denom)
        if self.abort_limit_pct is not None:
            max_per = face_val * (1.0 + self.abort_limit_pct / 100.0)
            curr_desc = f"<b>{self.abort_limit_pct}%</b> (Rs.{face_val} max ₹{max_per:,.2f}/code)"
        else:
            curr_desc = "<b>OFF</b> (Kisi bhi price par buy karega)"

        text = (
            "🛡️ <b>PRICE SURGE ABORT LIMIT</b>\n\n"
            f"• Current Limit: {curr_desc}\n"
            "• Agar Rooter fees/surge mila kar per-code price is % se upar gaya toh bot buy <b>ABORT</b> kar dega!\n\n"
            "👇 Apni max limit select karo:"
        )
        row1 = [
            InlineKeyboardButton(
                f"{'✅ ' if self.abort_limit_pct == p else ''}{p}%",
                callback_data=f"m:set_abort:{p}",
            )
            for p in (5, 10, 15, 20, 25)
        ]
        row2 = [
            InlineKeyboardButton(
                f"{'✅ ' if self.abort_limit_pct == p else ''}{p}%",
                callback_data=f"m:set_abort:{p}",
            )
            for p in (30, 35, 40, 45, 50)
        ]
        row3 = [
            InlineKeyboardButton(
                f"{'✅ ' if self.abort_limit_pct is None else ''}❌ OFF (No Limit)",
                callback_data="m:set_abort:off",
            )
        ]
        kb = InlineKeyboardMarkup(
            [
                row1,
                row2,
                row3,
                [InlineKeyboardButton("🔙 Main Menu", callback_data="m:home")],
            ]
        )
        return text, kb

    def _sniper_menu_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        text = (
            "🔫 <b>SNIPER & POPUP CONTROL</b>\n\n"
            "Sabhi accounts ko ek sath Start/Stop karo ya kisi ek slot ko toggle karo:"
        )
        rows: List[List[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton("▶️ Start All", callback_data="m:sniper_all_on"),
                InlineKeyboardButton("⏸️ Stop All", callback_data="m:sniper_all_off"),
            ]
        ]
        slot_row: List[InlineKeyboardButton] = []
        for acc in self.all_accounts():
            t = self.sniper_tasks.get(acc.slot_id)
            running = bool(t and not t.done())
            badge = "🟢 ON" if running else ("⏸️ OFF" if acc.logged_in else "⚪")
            slot_row.append(
                InlineKeyboardButton(
                    f"{acc.slot_id} {badge}",
                    callback_data=f"m:sniper_toggle:{acc.slot_id}",
                )
            )
            if len(slot_row) == 2:
                rows.append(slot_row)
                slot_row = []
        if slot_row:
            rows.append(slot_row)

        rows.append([InlineKeyboardButton("📍 Force Dismiss Popup", callback_data="m:popup")])
        rows.append([InlineKeyboardButton("🔙 Main Menu", callback_data="m:home")])
        return text, InlineKeyboardMarkup(rows)

    def _build_status_html(self) -> str:
        qty_desc = "1 (Direct Fast Buy)" if self.target_qty == 1 else "2 (Auto Fallback → 1)"
        lines = [
            "📊 <b>SWARM ACCOUNT STATUS</b>",
            f"🎯 <b>Target:</b> <code>Rs.{esc(self.target_denom)} Wallet Code</code>",
            f"📦 <b>Qty:</b> <code>{esc(qty_desc)}</code>",
            f"🛡️ <b>Abort Limit:</b> <code>{esc(self._abort_label())}</code>",
            "",
        ]
        for account in self.all_accounts():
            phone = account.phone_number if account.phone_number else "Saved Session"
            login_state = "✅ LOGGED IN" if account.logged_in else "❌ EMPTY"
            task = self.sniper_tasks.get(account.slot_id)
            sniper_state = "🔫 ON" if (task and not task.done()) else "⏸️ OFF"
            balance = f"₹{account.wallet_balance:,.2f}" if account.wallet_balance is not None else "-"

            lines.append(f"🆔 <b>{esc(account.slot_id)}</b> | {login_state} | {sniper_state}")
            if account.logged_in:
                lines.append(f"   📱 ID: <code>{esc(phone)}</code> | 💰 Bal: <code>{esc(balance)}</code>")
            lines.append(f"   📍 {esc(account.current_page)}")
            if account.last_error:
                lines.append(f"   ⚠️ {esc(account.last_error)}")
            lines.append("")
        return "\n".join(lines)

    async def _safe_edit(
        self,
        query,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ):
        try:
            await query.edit_message_text(
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            await self.telegram.send_message(
                text=text,
                chat_id=str(query.message.chat_id),
                parse_mode="HTML",
                reply_markup=reply_markup,
            )

    # =========================================================
    # INTERACTIVE CALLBACK QUERY ROUTER
    # =========================================================

    async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query or not query.data:
            return
        await query.answer()

        chat_id = str(query.message.chat_id)
        data = query.data

        if data in ("m:home", "m:cancel"):
            self.wizard_state.pop(chat_id, None)
            self._ensure_snipers_running()
            text, kb = self._main_menu_view()
            return await self._safe_edit(query, text, kb)

        if data == "m:login":
            self.wizard_state.pop(chat_id, None)
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🍪 Cookie / Token Login", callback_data="m:login_cookie")],
                    [InlineKeyboardButton("📱 Mobile Number (OTP)", callback_data="m:login_phone")],
                    [InlineKeyboardButton("🔙 Main Menu", callback_data="m:home")],
                ]
            )
            return await self._safe_edit(
                query,
                "🔐 <b>LOGIN CATEGORY</b>\n\nLogin kis tarike se karna hai?",
                kb,
            )

        if data == "m:login_cookie":
            return await self._safe_edit(
                query,
                "🍪 <b>COOKIE / TOKEN LOGIN</b>\n\nKis slot mein login karna hai? Choose karo:",
                self._slot_picker_keyboard("m:pick_cookie", back_target="m:login"),
            )

        if data == "m:login_phone":
            return await self._safe_edit(
                query,
                "📱 <b>MOBILE OTP LOGIN</b>\n\nKis slot mein login karna hai? Choose karo:",
                self._slot_picker_keyboard("m:pick_phone", back_target="m:login"),
            )

        if data.startswith("m:pick_cookie:"):
            slot_id = data.split(":")[-1].upper()
            self.wizard_state[chat_id] = {
                "action": "cookie_input",
                "slot_id": slot_id,
                "prompt_msg_id": query.message.message_id,
            }
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="m:cancel")]])
            return await self._safe_edit(
                query,
                f"🍪 <b>{esc(slot_id)} — Cookie / Token Login</b>\n\n"
                "Ab apna <code>user_auth</code> token paste karke bhejo\n"
                "<b>YA</b> apni <code>.json</code> cookie file direct send karo:",
                kb,
            )

        if data.startswith("m:pick_phone:"):
            slot_id = data.split(":")[-1].upper()
            acc = self.get_account(slot_id)
            if acc.logged_in:
                kb = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton(f"🚪 Logout {slot_id} First", callback_data=f"m:do_logout:{slot_id}")],
                        [InlineKeyboardButton("🔙 Back", callback_data="m:login_phone")],
                    ]
                )
                return await self._safe_edit(
                    query,
                    f"⚠️ <b>{esc(slot_id)}</b> pehle se logged in hai (<code>{esc(acc.phone_number)}</code>).\n"
                    "Naya number dalne ke liye pehle logout karo:",
                    kb,
                )
            self.wizard_state[chat_id] = {
                "action": "phone_number",
                "slot_id": slot_id,
                "prompt_msg_id": query.message.message_id,
            }
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="m:cancel")]])
            return await self._safe_edit(
                query,
                f"📱 <b>{esc(slot_id)} — Mobile Number Login</b>\n\n"
                "Ab apna <b>10-digit mobile number</b> type karke bhejo (jaise <code>9876543210</code>):",
                kb,
            )

        if data == "m:logout":
            return await self._safe_edit(
                query,
                "🚪 <b>LOGOUT SLOT</b>\n\nKis slot ko logout karna hai?",
                self._slot_picker_keyboard("m:do_logout", back_target="m:home"),
            )

        if data.startswith("m:do_logout:"):
            slot_id = data.split(":")[-1].upper()
            await self._safe_edit(query, f"⏳ Logging out <b>{esc(slot_id)}</b>...")
            ok, res_html = await self._logout_slot_core(slot_id)
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🚪 Logout Another", callback_data="m:logout")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="m:home")],
                ]
            )
            return await self._safe_edit(query, res_html, kb)

        if data == "m:screen":
            return await self._safe_edit(
                query,
                "🖥 <b>LIVE SCREENSHOT</b>\n\nKis slot ki screen dekhni hai?",
                self._slot_picker_keyboard("m:do_screen", back_target="m:home"),
            )

        if data.startswith("m:do_screen:"):
            slot_id = data.split(":")[-1].upper()
            await self._safe_edit(query, f"📸 Capturing live screenshot for <b>{esc(slot_id)}</b>...")
            ok, msg_html = await self._screen_slot_core(slot_id, chat_id)
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🖥 Another Screen", callback_data="m:screen")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="m:home")],
                ]
            )
            if ok:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                return
            return await self._safe_edit(query, msg_html, kb)

        if data == "m:refresh":
            return await self._safe_edit(
                query,
                "🔄 <b>HARD REFRESH SLOT</b>\n\nKis slot ko hard refresh karna hai?",
                self._slot_picker_keyboard("m:do_refresh", back_target="m:home"),
            )

        if data.startswith("m:do_refresh:"):
            slot_id = data.split(":")[-1].upper()
            await self._safe_edit(query, f"🔄 Hard refreshing <b>{esc(slot_id)}</b>...")
            ok, msg_html = await self._refresh_slot_core(slot_id)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="m:home")]])
            return await self._safe_edit(query, msg_html, kb)

        if data == "m:popup":
            return await self._safe_edit(
                query,
                "📍 <b>FORCE DISMISS LOCATION POPUP</b>\n\nSlot select karo:",
                self._slot_picker_keyboard("m:do_popup", back_target="m:sniper"),
            )

        if data.startswith("m:do_popup:"):
            slot_id = data.split(":")[-1].upper()
            ok, msg_html = await self._popup_slot_core(slot_id)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="m:sniper")]])
            return await self._safe_edit(query, msg_html, kb)

        if data == "m:target":
            text, kb = self._target_menu_view()
            return await self._safe_edit(query, text, kb)

        if data.startswith("m:set_denom:"):
            denom = data.split(":")[-1].strip()
            if denom in self.VALID_DENOMS:
                self.target_denom = denom
                self._save_target_config()
                self._ensure_snipers_running()
            text, kb = self._target_menu_view()
            return await self._safe_edit(query, text, kb)

        if data.startswith("m:set_qty:"):
            qty = int(data.split(":")[-1].strip())
            if qty in (1, 2):
                self.target_qty = qty
                self._save_target_config()
                self._ensure_snipers_running()
            text, kb = self._target_menu_view()
            return await self._safe_edit(query, text, kb)

        if data == "m:abort":
            text, kb = self._abort_menu_view()
            return await self._safe_edit(query, text, kb)

        if data.startswith("m:set_abort:"):
            val = data.split(":")[-1].strip().lower()
            if val == "off":
                self.abort_limit_pct = None
            elif val.isdigit() and int(val) in self.VALID_ABORT_PCTS:
                self.abort_limit_pct = int(val)
            self._save_target_config()
            self._last_abort_alert.clear()
            text, kb = self._abort_menu_view()
            return await self._safe_edit(query, text, kb)

        if data == "m:sniper":
            text, kb = self._sniper_menu_view()
            return await self._safe_edit(query, text, kb)

        if data == "m:sniper_all_on":
            self._ensure_snipers_running()
            text, kb = self._sniper_menu_view()
            return await self._safe_edit(query, text, kb)

        if data == "m:sniper_all_off":
            for sid, task in list(self.sniper_tasks.items()):
                if task and not task.done():
                    task.cancel()
                self.sniper_tasks.pop(sid, None)
            await asyncio.sleep(0.1)
            text, kb = self._sniper_menu_view()
            return await self._safe_edit(query, text, kb)

        if data.startswith("m:sniper_toggle:"):
            slot_id = data.split(":")[-1].upper()
            acc = self.get_account(slot_id)
            task = self.sniper_tasks.get(slot_id)
            if task and not task.done():
                task.cancel()
                self.sniper_tasks.pop(slot_id, None)
                await asyncio.sleep(0.1)
            elif acc.logged_in:
                self.sniper_tasks[slot_id] = asyncio.create_task(self._sniper_worker(slot_id))
            text, kb = self._sniper_menu_view()
            return await self._safe_edit(query, text, kb)

        if data == "m:status":
            self._ensure_snipers_running()
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Refresh Status", callback_data="m:status")],
                    [InlineKeyboardButton("🔙 Main Menu", callback_data="m:home")],
                ]
            )
            return await self._safe_edit(query, self._build_status_html(), kb)

        if data == "m:trigger":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="m:home")]])
            await self._safe_edit(
                query,
                f"🚨 <b>SWARM TRIGGER SENT!</b>\n"
                f"Saare accounts abhi <code>Rs.{esc(self.target_denom)} x {self.target_qty}</code> "
                f"(Abort: <code>{esc(self._abort_label())}</code>) par attack kar rahe hain!",
                kb,
            )
            asyncio.create_task(self.trigger_stock_buy())
            return

        if data == "m:heal":
            await self._safe_edit(query, "🧹 Healing browser & cleaning RAM for all active slots...")
            healed = await self._heal_all_core()
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="m:home")]])
            return await self._safe_edit(
                query,
                f"✅ RAM cleaned & <b>{healed}</b> slots refreshed! Saare snipers active hain.",
                kb,
            )

    # =========================================================
    # INTERACTIVE WIZARD TEXT & DOCUMENT HANDLERS
    # =========================================================

    async def handle_wizard_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.text:
            return
        chat_id = str(update.message.chat_id)
        state = self.wizard_state.get(chat_id)
        if not state:
            return

        action = state.get("action")
        slot_id = state.get("slot_id")
        prompt_msg_id = state.get("prompt_msg_id")
        user_text = update.message.text.strip()

        # Remove old prompt message to keep chat super clean
        if prompt_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
            except Exception:
                pass

        if action == "phone_number":
            phone = re.sub(r"[^\d]", "", user_text)
            if len(phone) != 10:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="m:cancel")]])
                sent = await update.message.reply_text(
                    "❌ Valid <b>10-digit</b> mobile number bhejo bhai (jaise <code>9876543210</code>):",
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                state["prompt_msg_id"] = sent.message_id
                return

            self.wizard_state.pop(chat_id, None)
            wait_msg = await update.message.reply_text(
                f"🔍 Opening product page & sending OTP for <b>{esc(slot_id)}</b> (<code>{esc(phone)}</code>)...",
                parse_mode="HTML",
            )
            ok, res_html = await self._phone_login_core(slot_id, phone, chat_id)
            try:
                await wait_msg.delete()
            except Exception:
                pass

            if ok:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="m:cancel")]])
                sent = await self.telegram.send_message(
                    text=(
                        f"📩 <b>OTP Sent for {esc(slot_id)}</b> (<code>{esc(phone)}</code>)!\n\n"
                        "Ab sirf <b>OTP digits</b> bhejo (jaise <code>123456</code>):"
                    ),
                    chat_id=chat_id,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                self.wizard_state[chat_id] = {
                    "action": "phone_otp",
                    "slot_id": slot_id,
                    "prompt_msg_id": sent.message_id,
                }
            else:
                kb = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🔁 Retry Login", callback_data=f"m:pick_phone:{slot_id}")],
                        [InlineKeyboardButton("🏠 Main Menu", callback_data="m:home")],
                    ]
                )
                await self.telegram.send_message(
                    text=res_html, chat_id=chat_id, parse_mode="HTML", reply_markup=kb
                )
            return

        if action == "phone_otp":
            otp = re.sub(r"[^\d]", "", user_text)
            if len(otp) not in (4, 6, 8):
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="m:cancel")]])
                sent = await update.message.reply_text(
                    "❌ OTP 4, 6 ya 8 digit ka number hona chahiye! Dobara bhejo:",
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                state["prompt_msg_id"] = sent.message_id
                return

            self.wizard_state.pop(chat_id, None)
            wait_msg = await update.message.reply_text(
                f"⏳ Verifying OTP <code>{esc(otp)}</code> for <b>{esc(slot_id)}</b>...",
                parse_mode="HTML",
            )
            ok, res_html = await self._otp_verify_core(slot_id, otp)
            try:
                await wait_msg.delete()
            except Exception:
                pass

            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="m:home")]])
            await self.telegram.send_message(
                text=res_html, chat_id=chat_id, parse_mode="HTML", reply_markup=kb
            )
            return

        if action == "cookie_input":
            self.wizard_state.pop(chat_id, None)
            wait_msg = await update.message.reply_text(
                f"⏳ Injecting isolated device session into <b>{esc(slot_id)}</b>...",
                parse_mode="HTML",
            )
            ok, res_html = await self._cookie_import_core(slot_id, user_text)
            try:
                await wait_msg.delete()
            except Exception:
                pass

            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="m:home")]])
            await self.telegram.send_message(
                text=res_html, chat_id=chat_id, parse_mode="HTML", reply_markup=kb
            )
            return

    async def handle_wizard_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if not update.message or not update.message.document:
            return False
        chat_id = str(update.message.chat_id)
        state = self.wizard_state.get(chat_id)
        if not state or state.get("action") != "cookie_input":
            return False

        slot_id = state.get("slot_id")
        prompt_msg_id = state.get("prompt_msg_id")
        self.wizard_state.pop(chat_id, None)

        if prompt_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
            except Exception:
                pass

        wait_msg = await update.message.reply_text(
            f"⏳ Reading cookie file & injecting into <b>{esc(slot_id)}</b>...",
            parse_mode="HTML",
        )
        try:
            tg_file = await context.bot.get_file(update.message.document.file_id)
            file_bytes = await tg_file.download_as_bytearray()
            raw_input = file_bytes.decode("utf-8").strip()
            ok, res_html = await self._cookie_import_core(slot_id, raw_input)
        except Exception as exc:
            res_html = f"❌ Cookie file read failed: <code>{esc(exc)}</code>"

        try:
            await wait_msg.delete()
        except Exception:
            pass

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="m:home")]])
        await self.telegram.send_message(
            text=res_html, chat_id=chat_id, parse_mode="HTML", reply_markup=kb
        )
        return True

    # =========================================================
    # SLASH COMMANDS (STILL 100% SUPPORTED)
    # =========================================================

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.message.chat_id)
        self.wizard_state.pop(chat_id, None)
        self._ensure_snipers_running()
        text, kb = self._main_menu_view()
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

    async def handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📖 <b>ROOTER SWARM ULTRA PRO</b>\n\n"
            "✨ <b>Easy Mode:</b> Bas <code>/start</code> bhejo—saare options buttons se mil jayenge!\n\n"
            "⌨️ <b>Direct Shortcut Commands:</b>\n"
            "• <code>/denom 2000</code> — Target code (1000..5000)\n"
            "• <code>/qty 1</code> ya <code>/qty 2</code> — Quantity pre-feed\n"
            "• <code>/abort 15</code> ya <code>/abort off</code> — Max price surge limit (5%..50%)\n"
            "• <code>/status</code> — Live accounts & settings\n"
            "• <code>/trigger_buy</code> — Wake all accounts NOW\n"
            "• <code>/screen S1-A1</code> — Live screenshot\n"
            "• <code>/heal</code> — Reset RAM & tabs",
            parse_mode="HTML",
        )

    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._ensure_snipers_running()
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Refresh Status", callback_data="m:status")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="m:home")],
            ]
        )
        await update.message.reply_text(
            self._build_status_html(), parse_mode="HTML", reply_markup=kb
        )

    async def handle_set_denom(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            text, kb = self._target_menu_view()
            return await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

        raw_val = re.sub(r"[^\d]", "", context.args[0].strip())
        if raw_val not in self.VALID_DENOMS:
            return await update.message.reply_text(
                "❌ Valid denomination daal bhai: <code>1000</code>, <code>2000</code>, <code>3000</code>, <code>4000</code>, ya <code>5000</code>",
                parse_mode="HTML",
            )

        self.target_denom = raw_val
        self._save_target_config()
        self._ensure_snipers_running()
        await update.message.reply_text(
            f"🎯 Target Denomination Locked: <b>Rs.{esc(self.target_denom)} Wallet Code</b>!\n"
            f"📦 Pre-Fed Quantity: <b>{self.target_qty}</b>\n"
            f"🛡️ Abort Limit: <b>{esc(self._abort_label())}</b>",
            parse_mode="HTML",
        )

    async def handle_set_qty(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args or context.args[0].strip() not in ("1", "2"):
            text, kb = self._target_menu_view()
            return await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

        self.target_qty = int(context.args[0].strip())
        self._save_target_config()
        self._ensure_snipers_running()
        await update.message.reply_text(
            f"📦 Pre-Fed Quantity set to <b>{self.target_qty}</b> for <b>Rs.{esc(self.target_denom)}</b>!",
            parse_mode="HTML",
        )

    async def handle_set_abort(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            text, kb = self._abort_menu_view()
            return await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

        raw = context.args[0].strip().lower().replace("%", "")
        if raw in ("off", "none", "0", "disable"):
            self.abort_limit_pct = None
        elif raw.isdigit() and int(raw) in self.VALID_ABORT_PCTS:
            self.abort_limit_pct = int(raw)
        else:
            return await update.message.reply_text(
                "❌ 5% se 50% (5 ke multiple) ya <code>off</code> bhejo! Example: <code>/abort 15</code>",
                parse_mode="HTML",
            )

        self._save_target_config()
        self._last_abort_alert.clear()
        await update.message.reply_text(
            f"🛡️ Price Surge Abort Limit set to: <b>{esc(self._abort_label())}</b>",
            parse_mode="HTML",
        )

    async def handle_heal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🧹 Healing browser & cleaning RAM for all active slots...")
        healed = await self._heal_all_core()
        await update.message.reply_text(
            f"✅ RAM cleaned & <b>{healed}</b> slots refreshed! All snipers running.",
            parse_mode="HTML",
        )

    async def _heal_all_core(self) -> int:
        healed = 0
        for slot_id, acc in self.accounts.items():
            if acc.logged_in:
                try:
                    await self.browser.save_session(slot_id, acc.phone_number)
                    await self.browser.reset_slot_page(slot_id)
                    acc.update(current_page="✨ Healed & Fresh", last_error=None)
                    healed += 1
                except Exception:
                    pass
        self._ensure_snipers_running()
        return healed

    # =========================================================
    # CORE ACTION HELPERS (SHARED BY BUTTON WIZARD & SLASH CMDS)
    # =========================================================

    def _normalize_cookie_for_playwright(self, c: dict) -> dict:
        exp = c.get("expirationDate") or c.get("expires") or 1893456000
        same_site_raw = str(c.get("sameSite") or "Lax").lower()
        if "strict" in same_site_raw:
            same_site = "Strict"
        elif "none" in same_site_raw or "no_restriction" in same_site_raw:
            same_site = "None"
        else:
            same_site = "Lax"

        return {
            "name": str(c["name"]),
            "value": str(c["value"]),
            "domain": str(c.get("domain", ".rooter.gg")),
            "path": str(c.get("path", "/")),
            "expires": float(exp),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", True if same_site == "None" else False)),
            "sameSite": same_site,
        }

    async def _cookie_import_core(self, slot_id: str, raw_input: str) -> Tuple[bool, str]:
        try:
            slot_id = slot_id.upper()
            account = self.get_account(slot_id)

            old_task = self.sniper_tasks.pop(slot_id, None)
            if old_task and not old_task.done():
                old_task.cancel()

            exp = 1893456000
            playwright_cookies = list(self.browser.get_indian_region_cookies())
            user_label = "Token-Auth"
            extracted_device_id = str(uuid.uuid4())
            decoded_user_auth_str = ""

            if raw_input.startswith("["):
                parsed_list = json.loads(raw_input)
                has_client_id = False
                for item in parsed_list:
                    if isinstance(item, dict) and "name" in item and "value" in item:
                        if item["name"] == "client_id":
                            has_client_id = True
                        playwright_cookies.append(self._normalize_cookie_for_playwright(item))
                        if item["name"] == "user_auth":
                            try:
                                dec = urllib.parse.unquote(item["value"])
                                decoded_user_auth_str = dec
                                data = json.loads(dec)
                                if data.get("userId"):
                                    user_label = f"UID:{data['userId']}"
                                if data.get("deviceId"):
                                    extracted_device_id = str(data["deviceId"])
                            except Exception:
                                pass
                if not has_client_id:
                    playwright_cookies.append({
                        "name": "client_id",
                        "value": extracted_device_id,
                        "domain": ".rooter.gg",
                        "path": "/",
                        "expires": exp,
                        "httpOnly": False,
                        "secure": True,
                        "sameSite": "Lax",
                    })
            else:
                token_value = raw_input.strip().strip('"').strip("'")
                if token_value.startswith("{"):
                    decoded_user_auth_str = token_value
                    try:
                        data = json.loads(token_value)
                        if data.get("userId"):
                            user_label = f"UID:{data['userId']}"
                        if data.get("deviceId"):
                            extracted_device_id = str(data["deviceId"])
                    except Exception:
                        pass
                    token_value = urllib.parse.quote(token_value, safe="")
                else:
                    try:
                        dec = urllib.parse.unquote(token_value)
                        decoded_user_auth_str = dec
                        data = json.loads(dec)
                        if data.get("userId"):
                            user_label = f"UID:{data['userId']}"
                        if data.get("deviceId"):
                            extracted_device_id = str(data["deviceId"])
                    except Exception:
                        pass

                playwright_cookies.extend([
                    {
                        "name": "user_auth",
                        "value": token_value,
                        "domain": ".rooter.gg",
                        "path": "/",
                        "expires": exp,
                        "httpOnly": False,
                        "secure": True,
                        "sameSite": "Lax",
                    },
                    {
                        "name": "client_id",
                        "value": extracted_device_id,
                        "domain": ".rooter.gg",
                        "path": "/",
                        "expires": exp,
                        "httpOnly": False,
                        "secure": True,
                        "sameSite": "Lax",
                    },
                ])

            await self.browser.close_slot(slot_id)

            origins_data = [
                {
                    "origin": "https://shop.rooter.gg",
                    "localStorage": [
                        {"name": "COUNTRY", "value": "IN"},
                        {"name": "COUNTRY_LOCALE", "value": "en-IN"},
                        {"name": "LANGUAGE", "value": "en"},
                        {"name": "client_id", "value": extracted_device_id},
                        {"name": "deviceId", "value": extracted_device_id},
                    ],
                }
            ]
            if decoded_user_auth_str:
                origins_data[0]["localStorage"].append(
                    {"name": "user_auth", "value": decoded_user_auth_str}
                )

            session_dir = self.browser.sessions.session_path(slot_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            storage_file = session_dir / "storage_state.json"
            storage_file.write_text(json.dumps({"cookies": playwright_cookies, "origins": origins_data}))

            page = await self.browser.open_product_page(slot_id)
            await self._destroy_location_popup(page, slot_id)
            await asyncio.sleep(0.8)
            balance_txt = await self._update_live_balance(page, account)
            await self.browser.save_session(slot_id, user_label)

            account.update(
                phone_number=user_label,
                logged_in=True,
                active=True,
                current_page="✅ Token Active & Verified",
                last_error=None,
            )
            log_step(slot_id, f"Cookie session live-loaded ({user_label}) | {balance_txt}")

            self.sniper_tasks[slot_id] = asyncio.create_task(self._sniper_worker(slot_id))

            return True, (
                f"✅ <b>Login Successful!</b> ({esc(slot_id)})\n"
                f"👤 Account: <code>{esc(user_label)}</code>\n"
                f"📱 Device: <code>{esc(extracted_device_id[:12])}...</code>\n"
                f"💰 Wallet: <code>{esc(balance_txt)}</code>\n"
                f"🎯 Target: <code>Rs.{esc(self.target_denom)} x {self.target_qty}</code>\n"
                "🔫 Swarm Sniper Auto-Started!"
            )
        except Exception as exc:
            log_step("COOKIE_ERR", f"Failed: {exc}")
            return False, f"❌ Cookie import failed ({esc(slot_id)}): <code>{esc(exc)}</code>"

    async def handle_cookie_import(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        slot_id = ""
        raw_input = ""
        if update.message and update.message.document:
            if context.args:
                slot_id = context.args[0].upper()
            elif update.message.caption:
                for part in update.message.caption.upper().split():
                    if "-A" in part:
                        slot_id = part
                        break
            if not slot_id:
                return await update.message.reply_text("❌ Caption mein <code>/cookie S1-A1</code> likh kar file bhej bhai!", parse_mode="HTML")
            tg_file = await context.bot.get_file(update.message.document.file_id)
            file_bytes = await tg_file.download_as_bytearray()
            raw_input = file_bytes.decode("utf-8").strip()
        else:
            if not context.args or len(context.args) < 2:
                return await update.message.reply_text(
                    "❌ Format: <code>/cookie S1-A1 &lt;user_auth_value&gt;</code>\nYa <code>/start</code> se button dabao!",
                    parse_mode="HTML",
                )
            slot_id = context.args[0].upper()
            raw_input = " ".join(context.args[1:]).strip()

        await update.message.reply_text(f"⏳ Injecting isolated session into <b>{esc(slot_id)}</b>...", parse_mode="HTML")
        ok, res_html = await self._cookie_import_core(slot_id, raw_input)
        await update.message.reply_text(res_html, parse_mode="HTML")

    async def _popup_slot_core(self, slot_id: str) -> Tuple[bool, str]:
        try:
            slot_id = slot_id.upper()
            page = await self.browser.get_page(slot_id)
            if not page.url or page.url == "about:blank":
                return False, f"❌ <b>{esc(slot_id)}</b> ka browser page active nahi hai."
            await self._destroy_location_popup(page, slot_id)
            await self.browser.save_session(slot_id, self.get_account(slot_id).phone_number)
            return True, f"✅ Location popup dismissed & Indian region locked for <b>{esc(slot_id)}</b>!"
        except Exception as exc:
            return False, f"❌ Force popup failed: <code>{esc(exc)}</code>"

    async def handle_force_popup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            return await update.message.reply_text("❌ Format: <code>/popup S1-A1</code>", parse_mode="HTML")
        _, res_html = await self._popup_slot_core(context.args[0].upper())
        await update.message.reply_text(res_html, parse_mode="HTML")

    async def _screen_slot_core(self, slot_id: str, chat_id: Optional[str] = None) -> Tuple[bool, str]:
        try:
            slot_id = slot_id.upper()
            account = self.get_account(slot_id)
            page = await self.browser.get_page(slot_id)
            if not page.url or page.url == "about:blank":
                page = await self.browser.open_product_page(slot_id)
                await asyncio.sleep(0.8)

            await self._destroy_location_popup(page, slot_id)
            screenshot_path = self.screenshots_dir / f"{slot_id}_{int(time.time())}.png"
            await page.screenshot(path=str(screenshot_path), full_page=False)
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🖥 Another Screen", callback_data="m:screen")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="m:home")],
                ]
            )
            await self.telegram.send_photo(
                photo_path=str(screenshot_path),
                caption=(
                    f"🖥 <b>{esc(slot_id)}</b> | Target: <code>Rs.{esc(self.target_denom)} x {self.target_qty}</code>\n"
                    f"🛡️ Abort: <code>{esc(self._abort_label())}</code>\n"
                    f"📍 State: {esc(account.current_page)}"
                ),
                chat_id=chat_id,
                parse_mode="HTML",
                reply_markup=kb,
            )
            if screenshot_path.exists():
                screenshot_path.unlink()
            return True, "✅ Screenshot sent!"
        except Exception as exc:
            try:
                self.get_account(slot_id).update(last_error=str(exc))
            except Exception:
                pass
            return False, f"❌ Screenshot failed ({esc(slot_id)}): <code>{esc(exc)}</code>"

    async def handle_screen(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            return await update.message.reply_text("❌ Format: <code>/screen S1-A1</code>", parse_mode="HTML")
        ok, res_html = await self._screen_slot_core(context.args[0].upper(), str(update.message.chat_id))
        if not ok:
            await update.message.reply_text(res_html, parse_mode="HTML")

    async def _refresh_slot_core(self, slot_id: str) -> Tuple[bool, str]:
        try:
            slot_id = slot_id.upper()
            account = self.get_account(slot_id)
            page = await self.browser.reset_slot_page(slot_id)
            cb = int(time.time())
            fresh_url = (
                f"{settings.product_url}?cb={cb}"
                if "?" not in settings.product_url
                else f"{settings.product_url}&cb={cb}"
            )
            await page.goto(fresh_url, wait_until="domcontentloaded", timeout=40000)
            await asyncio.sleep(1.0)
            await self._destroy_location_popup(page, slot_id)
            await self.browser.save_session(slot_id, account.phone_number)

            account.update(logged_in=True, active=True, current_page="🔄 Manually Refreshed", last_error=None)
            task = self.sniper_tasks.get(slot_id)
            if task is None or task.done():
                self.sniper_tasks[slot_id] = asyncio.create_task(self._sniper_worker(slot_id))
            return True, (
                f"🔄 Hard Refreshed <b>{esc(slot_id)}</b>!\n"
                f"🎯 Target: <code>Rs.{esc(self.target_denom)} x {self.target_qty}</code> | 🔫 Sniper Active!"
            )
        except Exception as exc:
            try:
                self.get_account(slot_id).update(last_error=str(exc))
            except Exception:
                pass
            return False, f"❌ Refresh failed ({esc(slot_id)}): <code>{esc(exc)}</code>"

    async def handle_refresh(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            return await update.message.reply_text("❌ Format: <code>/refresh S1-A1</code>", parse_mode="HTML")
        _, res_html = await self._refresh_slot_core(context.args[0].upper())
        await update.message.reply_text(res_html, parse_mode="HTML")

    async def _logout_slot_core(self, slot_id: str) -> Tuple[bool, str]:
        try:
            slot_id = slot_id.upper()
            account = self.get_account(slot_id)

            task = self.sniper_tasks.pop(slot_id, None)
            if task and not task.done():
                task.cancel()

            await self.browser.close_slot(slot_id)
            self.browser.sessions.delete_session(slot_id)

            try:
                session_dir = self.browser.sessions.session_path(slot_id)
                meta_file = session_dir / "meta.json"
                if meta_file.exists():
                    meta_file.unlink()
            except Exception:
                pass

            account.update(
                phone_number=None,
                logged_in=False,
                active=False,
                current_page="unknown",
                last_error=None,
                wallet_balance=None,
            )
            log_step(slot_id, "Logged out and RAM freed.")
            return True, f"🚪 Successfully logged out & RAM freed for <b>{esc(slot_id)}</b>. Slot is now fresh!"
        except Exception as exc:
            return False, f"❌ Logout failed ({esc(slot_id)}): <code>{esc(exc)}</code>"

    async def handle_logout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            return await update.message.reply_text("❌ Format: <code>/logout S1-A1</code>", parse_mode="HTML")
        _, res_html = await self._logout_slot_core(context.args[0].upper())
        await update.message.reply_text(res_html, parse_mode="HTML")

    # =========================================================
    # LOCATION POPUP & BALANCE HELPERS
    # =========================================================

    async def _destroy_location_popup(self, page, slot_id: str = "SLOT"):
        try:
            await self.browser.handle_location_popup(page, slot_id)
        except Exception:
            pass

        try:
            await page.evaluate(r"""() => {
                const allEls = Array.from(document.querySelectorAll('button, p, span, div, a'));
                for (const el of allEls) {
                    const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                    if (txt === 'no, stay on this site' || txt === 'stay on this site') {
                        el.click();
                    }
                }
                for (const div of document.querySelectorAll('div, section')) {
                    if (div.innerText && div.innerText.includes('Different Location Detected')) {
                        const modal = div.closest('div[role="dialog"]') || div.closest('.fixed');
                        if (modal) modal.remove();
                    }
                }
            }""")
        except Exception:
            pass

        try:
            curr_url = (page.url or "").lower()
            if (
                curr_url
                and "playstation" not in curr_url
                and "order" not in curr_url
                and "about:blank" not in curr_url
            ):
                log_step(slot_id, f"Redirected to {curr_url}! Returning to PlayStation product page...")
                await page.goto(settings.product_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(0.8)
                await self.browser.handle_location_popup(page, slot_id)
        except Exception as e:
            log_step(slot_id, f"URL check note: {e}")

    async def _update_live_balance(self, page, account: AccountState) -> str:
        wallet_balance_text = "Balance info not found"
        try:
            balance_locator = page.locator("text=/Wallet Balance/i >> visible=true").first
            if await balance_locator.is_visible(timeout=900):
                wallet_balance_text = await balance_locator.inner_text()
                nums = re.findall(r"\d+(?:\.\d+)?", wallet_balance_text.replace(",", ""))
                if nums:
                    account.update(wallet_balance=float(nums[0]))
        except Exception:
            pass
        return wallet_balance_text

    # =========================================================
    # HUMAN LOGIN & OTP HELPERS (PRESERVED WORKING LOGIC)
    # =========================================================

    async def _find_mobile_input(self, page):
        selectors = [
            'input[type="tel"]',
            'input[inputmode="tel"]',
            'input[name="phone"]',
            'input[placeholder*="mobile" i]',
            'input[placeholder*="number" i]',
            'input[placeholder*="phone" i]',
            'input[placeholder*="enter" i]',
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = await locator.count()
                for index in range(count):
                    item = locator.nth(index)
                    if await item.is_visible(timeout=300):
                        box = await item.bounding_box()
                        if box and box["y"] < 85:
                            continue
                        ph = (await item.get_attribute("placeholder") or "").lower()
                        if "search" in ph or "bgmi" in ph or "valorant" in ph or "coupon" in ph:
                            continue
                        return item
            except Exception:
                pass
        return None

    async def _open_login_popup(self, page, slot_id: str = "SLOT"):
        await self._destroy_location_popup(page, slot_id)
        await asyncio.sleep(0.7)

        mobile_input = await self._find_mobile_input(page)
        if mobile_input is not None:
            return mobile_input

        try:
            icon_info = await page.evaluate(r"""() => {
                const svgs = Array.from(document.querySelectorAll('svg'));
                const topRight = [];
                for (const svg of svgs) {
                    const r = svg.getBoundingClientRect();
                    if (r.top >= 5 && r.top <= 75 && r.left > 800 && r.width >= 14 && r.width <= 60) {
                        topRight.push({
                            x: r.left + r.width / 2,
                            y: r.top + r.height / 2,
                            left: r.left
                        });
                    }
                }
                if (topRight.length === 0) return null;
                topRight.sort((a, b) => b.left - a.left);
                return topRight[0];
            }""")

            if icon_info and "x" in icon_info and "y" in icon_info:
                px, py = icon_info["x"], icon_info["y"]
                log_step(slot_id, f"Clicking top-right profile icon at ({px:.0f}, {py:.0f})")
                await page.mouse.move(px, py, steps=6)
                await page.mouse.click(px, py)
                await asyncio.sleep(1.8)

                mobile_input = await self._find_mobile_input(page)
                if mobile_input is not None:
                    return mobile_input
        except Exception as e:
            log_step(slot_id, f"SVG icon click note: {e}")

        login_texts = [
            page.get_by_text("Login / Sign Up", exact=False),
            page.get_by_text("Login / Signup", exact=False),
            page.get_by_text("Sign In", exact=False),
            page.locator("button:has-text('Login')"),
            page.locator("a:has-text('Login')"),
            page.get_by_text("Login", exact=True),
        ]
        for candidate in login_texts:
            try:
                if await candidate.count() > 0 and await candidate.first.is_visible(timeout=400):
                    await candidate.first.click(force=True, timeout=2000)
                    await asyncio.sleep(1.6)
                    mobile_input = await self._find_mobile_input(page)
                    if mobile_input is not None:
                        return mobile_input
            except Exception:
                pass

        for coord_x in [1208, 1143, 1225]:
            try:
                await page.mouse.move(coord_x, 36, steps=5)
                await page.mouse.click(coord_x, 36)
                await asyncio.sleep(1.4)
                mobile_input = await self._find_mobile_input(page)
                if mobile_input is not None:
                    return mobile_input
            except Exception:
                pass

        extra_triggers = [
            page.locator("button:has-text('Login to')"),
            page.get_by_text("Apply Coupons", exact=False),
        ]
        for trig in extra_triggers:
            try:
                if await trig.count() > 0 and await trig.first.is_visible(timeout=300):
                    await trig.first.click(force=True, timeout=1500)
                    await asyncio.sleep(1.6)
                    mobile_input = await self._find_mobile_input(page)
                    if mobile_input is not None:
                        return mobile_input
            except Exception:
                pass

        return None

    async def _find_send_otp_button(self, page):
        candidates = [
            page.locator("button:has-text('Continue')"),
            page.locator("button:has-text('CONTINUE')"),
            page.locator("button:has-text('Send OTP')"),
            page.locator("button:has-text('Get OTP')"),
        ]
        for locator in candidates:
            try:
                count = await locator.count()
                for i in range(count):
                    btn = locator.nth(i)
                    if await btn.is_visible(timeout=300):
                        txt = (await btn.inner_text() or "").strip().lower()
                        if "switch" in txt or "local" in txt or "stay" in txt or "yes" in txt:
                            continue
                        return btn
            except Exception:
                pass
        return None

    async def _find_otp_inputs(self, page, phone_number: str = ""):
        try:
            inputs = page.locator("input:visible")
            count = await inputs.count()
            valid_boxes = []

            for i in range(count):
                el = inputs.nth(i)
                box = await el.bounding_box()
                if not box or box["y"] < 85:
                    continue

                ph = (await el.get_attribute("placeholder") or "").lower()
                if "search" in ph or "bgmi" in ph or "valorant" in ph or "mobile" in ph or "phone" in ph:
                    continue

                val = (await el.input_value() or "").strip()
                if phone_number and phone_number in val:
                    continue

                valid_boxes.append(el)

            if len(valid_boxes) >= 4:
                return valid_boxes[:8]

            if len(valid_boxes) == 1:
                verify_btn = await self._find_verify_button(page)
                if verify_btn is not None:
                    return valid_boxes
        except Exception:
            pass

        return []

    async def _find_verify_button(self, page):
        candidates = [
            page.locator("button:has-text('Verify')"),
            page.locator("button:has-text('VERIFY')"),
            page.get_by_role("button", name="Verify Now", exact=False),
        ]
        for locator in candidates:
            try:
                count = await locator.count()
                for i in range(count):
                    btn = locator.nth(i)
                    if await btn.is_visible(timeout=300):
                        return btn
            except Exception:
                pass
        return None

    async def _phone_login_core(self, slot_id: str, phone_number: str, chat_id: Optional[str] = None) -> Tuple[bool, str]:
        try:
            slot_id = slot_id.upper()
            account = self.get_account(slot_id)
            if account.logged_in:
                return False, f"⚠️ <b>{esc(slot_id)}</b> pehle se logged in hai. Pehle logout karo."
            if not phone_number.isdigit() or len(phone_number) != 10:
                return False, "❌ Valid 10-digit mobile number daal bhai."

            old_task = self.sniper_tasks.pop(slot_id, None)
            if old_task and not old_task.done():
                old_task.cancel()

            log_step(slot_id, f"Starting human login flow for phone {phone_number}...")
            page = await self.browser.open_product_page(slot_id)
            await asyncio.sleep(1.2)
            await self._destroy_location_popup(page, slot_id)

            account.update(phone_number=phone_number, active=True, current_page="🔐 Opening Login Popup", last_error=None)

            mobile_input = await self._open_login_popup(page, slot_id)
            if mobile_input is None:
                screenshot_path = self.screenshots_dir / f"{slot_id}_login_error.png"
                await page.screenshot(path=str(screenshot_path), full_page=False)
                await self.telegram.send_photo(photo_path=str(screenshot_path), caption="❌ Login popup nahi khula.", chat_id=chat_id)
                if screenshot_path.exists():
                    screenshot_path.unlink()
                raise RuntimeError("Login popup not opened.")

            await mobile_input.click(force=True)
            await asyncio.sleep(0.3)
            await mobile_input.fill("")
            await page.keyboard.type(phone_number, delay=60)
            await asyncio.sleep(0.7)

            send_otp_button = await self._find_send_otp_button(page)
            if send_otp_button is None:
                error_ss = self.screenshots_dir / f"{slot_id}_continue_btn_err.png"
                await page.screenshot(path=str(error_ss), full_page=False)
                await self.telegram.send_photo(photo_path=str(error_ss), caption="❌ Continue button nahi mila.", chat_id=chat_id)
                if error_ss.exists():
                    error_ss.unlink()
                raise RuntimeError("Continue button not found.")

            await send_otp_button.evaluate(
                "(el) => { el.removeAttribute('disabled'); el.removeAttribute('data-disabled'); el.disabled = false; }"
            )
            await send_otp_button.click(force=True, timeout=3000)

            otp_ready = False
            for _ in range(35):
                await asyncio.sleep(0.45)
                if await self._find_otp_inputs(page, phone_number=phone_number):
                    otp_ready = True
                    break

            if not otp_ready:
                error_ss = self.screenshots_dir / f"{slot_id}_otp_popup_err.png"
                await page.screenshot(path=str(error_ss), full_page=False)
                await self.telegram.send_photo(photo_path=str(error_ss), caption="❌ OTP box nahi aaya.", chat_id=chat_id)
                if error_ss.exists():
                    error_ss.unlink()
                raise RuntimeError("OTP popup did not appear.")

            log_step(slot_id, "SUCCESS: OTP screen is open!")
            account.update(current_page="📩 OTP Sent. Waiting...", last_error=None)
            return True, f"📩 OTP sent for <b>{esc(slot_id)}</b>.\nAb bhej: <code>/otp {esc(slot_id)} 123456</code>"
        except Exception as exc:
            log_step(slot_id, f"Login failed: {exc}")
            try:
                self.get_account(slot_id).update(last_error=str(exc), active=False)
            except Exception:
                pass
            return False, f"❌ Login failed ({esc(slot_id)}): <code>{esc(exc)}</code>"

    async def _otp_verify_core(self, slot_id: str, otp: str) -> Tuple[bool, str]:
        try:
            slot_id = slot_id.upper()
            account = self.get_account(slot_id)
            if len(otp) not in (4, 6, 8) or not otp.isdigit():
                return False, "❌ OTP 4, 6 ya 8 digit ka number hona chahiye."

            page = await self.browser.get_page(slot_id)
            otp_inputs = []
            for _ in range(20):
                otp_inputs = await self._find_otp_inputs(page, phone_number=account.phone_number or "")
                if otp_inputs:
                    break
                await asyncio.sleep(0.45)

            if not otp_inputs:
                raise RuntimeError("OTP fields page par nahi mile.")

            if len(otp_inputs) == 1:
                await otp_inputs[0].click(force=True)
                await asyncio.sleep(0.2)
                await otp_inputs[0].fill("")
                await page.keyboard.type(otp, delay=60)
            else:
                boxes = otp_inputs[: len(otp)]
                for i, digit in enumerate(otp):
                    if i < len(boxes):
                        await boxes[i].click(force=True)
                        await page.keyboard.press(digit)
                        await asyncio.sleep(0.07)

            await asyncio.sleep(0.7)
            verify_button = await self._find_verify_button(page)
            if verify_button is None:
                raise RuntimeError("Verify button nahi mila.")

            await verify_button.evaluate(
                "(el) => { el.removeAttribute('disabled'); el.removeAttribute('data-disabled'); el.disabled = false; }"
            )
            await verify_button.click(force=True, timeout=3000)

            ctx = await self.browser.get_context(slot_id)
            for _ in range(20):
                await asyncio.sleep(0.55)
                try:
                    cookies = await ctx.cookies()
                    cookie_auth = next((c["value"] for c in cookies if c.get("name") == "user_auth" and c.get("value")), None)
                    ls_auth = await page.evaluate(
                        "() => { try { return localStorage.getItem('user_auth') || sessionStorage.getItem('user_auth'); } catch(e) { return null; } }"
                    )
                    if cookie_auth or ls_auth:
                        break
                except Exception:
                    pass

            await asyncio.sleep(2.5)

            try:
                cookies = await ctx.cookies()
                persistent_cookies = []
                for c in cookies:
                    c_copy = dict(c)
                    if not c_copy.get("expires") or c_copy.get("expires", -1) < 0:
                        c_copy["expires"] = 1893456000
                    persistent_cookies.append(c_copy)
                if persistent_cookies:
                    await ctx.add_cookies(persistent_cookies)

                ls_auth = await page.evaluate(
                    "() => { try { return localStorage.getItem('user_auth') || sessionStorage.getItem('user_auth'); } catch(e) { return null; } }"
                )
                if ls_auth:
                    await page.evaluate(
                        "(val) => { try { localStorage.setItem('user_auth', val); } catch(e) {} }",
                        ls_auth,
                    )
                    await ctx.add_cookies([{
                        "name": "user_auth",
                        "value": urllib.parse.quote(ls_auth, safe="") if ls_auth.startswith("{") else ls_auth,
                        "domain": ".rooter.gg",
                        "path": "/",
                        "expires": 1893456000,
                        "httpOnly": False,
                        "secure": True,
                        "sameSite": "Lax",
                    }])
            except Exception as sync_err:
                log_step(slot_id, f"Cookie sync note: {sync_err}")

            if "playstation-gift-cards" not in (page.url or "").lower():
                try:
                    await page.goto(settings.product_url, wait_until="domcontentloaded", timeout=25000)
                    await asyncio.sleep(1.2)
                except Exception:
                    pass

            await self._destroy_location_popup(page, slot_id)
            balance_txt = await self._update_live_balance(page, account)
            await self.browser.save_session(slot_id, account.phone_number)

            account.update(logged_in=True, active=True, current_page="✅ Logged In & Saved", last_error=None)
            log_step(slot_id, f"Login successful & saved! ({balance_txt})")

            task = self.sniper_tasks.get(slot_id)
            if task is None or task.done():
                self.sniper_tasks[slot_id] = asyncio.create_task(self._sniper_worker(slot_id))

            return True, (
                f"✅ <b>Login Successful & Session Saved!</b> ({esc(slot_id)})\n"
                f"📱 Phone: <code>{esc(account.phone_number)}</code>\n"
                f"💰 Wallet: <code>{esc(balance_txt)}</code>\n"
                f"🎯 Target: <code>Rs.{esc(self.target_denom)} x {self.target_qty}</code>\n"
                "🔫 Sniper AUTO-STARTED!"
            )
        except Exception as exc:
            log_step(slot_id, f"OTP failed: {exc}")
            try:
                self.get_account(slot_id).update(logged_in=False, last_error=str(exc))
            except Exception:
                pass
            return False, f"❌ OTP failed ({esc(slot_id)}): <code>{esc(exc)}</code>"

    async def handle_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args or len(context.args) < 2:
            return await update.message.reply_text("❌ Format: <code>/login S1-A1 9876543210</code>", parse_mode="HTML")
        slot_id, phone = context.args[0].upper(), context.args[1].strip()
        await update.message.reply_text(f"🔍 Opening product page for <b>{esc(slot_id)}</b>...", parse_mode="HTML")
        _, res_html = await self._phone_login_core(slot_id, phone, str(update.message.chat_id))
        await update.message.reply_text(res_html, parse_mode="HTML")

    async def handle_otp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args or len(context.args) < 2:
            return await update.message.reply_text("❌ Format: <code>/otp S1-A1 123456</code>", parse_mode="HTML")
        slot_id, otp = context.args[0].upper(), context.args[1].strip()
        _, res_html = await self._otp_verify_core(slot_id, otp)
        await update.message.reply_text(res_html, parse_mode="HTML")

    # =========================================================
    # 🎯 EXACT CARD SELECTOR, VERIFIED QUANTITY & MILLISECOND ABORT ENGINE
    # =========================================================

    async def _find_and_select_denomination(self, page, denom: str) -> Tuple[str, int]:
        """
        Finds the exact 'Rs.<denom> Wallet Code' card under Select Options, clicks it with a real
        Playwright mouse click, and STRICTLY VERIFIES that the '- <qty> +' stepper pill moved
        onto THAT exact denomination card.
        Returns: (status, live_qty_on_card)
          status in ('selected', 'oos', 'not_found')
        """
        target_label = f"Rs.{denom} Wallet Code"

        async def _inspect_card():
            return await page.evaluate(
                r"""(label) => {
                    const lowerLabel = label.toLowerCase();
                    const allEls = Array.from(document.querySelectorAll('div, button, span, p'));
                    let labelEl = null;

                    for (const el of allEls) {
                        const t = (el.innerText || '').trim().toLowerCase();
                        if (t === lowerLabel && el.offsetParent !== null) {
                            labelEl = el;
                            break;
                        }
                    }
                    if (!labelEl) {
                        for (const el of allEls) {
                            const t = (el.innerText || '').trim().toLowerCase();
                            if (t.startsWith(lowerLabel + '\n') && t.length < 90 && el.offsetParent !== null) {
                                labelEl = el;
                                break;
                            }
                        }
                    }
                    if (!labelEl) return { status: 'waiting' };

                    // Climb up to the exact single denomination card container
                    // (must contain price or 'out of stock', and must NOT contain any other 'rs.XXXX wallet code')
                    let card = labelEl;
                    let cur = labelEl;
                    for (let depth = 0; depth < 6 && cur && cur.parentElement; depth++) {
                        const parentTxt = (cur.parentElement.innerText || '').toLowerCase();
                        const otherMatches = parentTxt.match(/rs\.\d{4}\s*wallet code/g) || [];
                        if (otherMatches.length > 1) break;
                        cur = cur.parentElement;
                        if (parentTxt.includes('out of stock') || parentTxt.includes('₹')) {
                            card = cur;
                        }
                    }

                    const cardTxt = (card.innerText || '').toLowerCase();
                    if (cardTxt.includes('out of stock')) {
                        return { status: 'oos' };
                    }
                    if (!cardTxt.includes('₹') && !cardTxt.includes('rs.')) {
                        return { status: 'waiting' };
                    }

                    const rect = card.getBoundingClientRect();

                    // Check if the '- <qty> +' pill is currently attached to THIS exact card
                    // (either inside card DOM or positioned on top-right edge of this card)
                    const checkStepperInside = (rootEl) => {
                        const lines = (rootEl.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
                        const hasPlus = lines.includes('+');
                        const hasMinus = lines.includes('-') || lines.includes('−');
                        if (hasPlus || hasMinus) {
                            for (const ln of lines) {
                                if (/^\d+$/.test(ln)) {
                                    const n = parseInt(ln, 10);
                                    if (n >= 1 && n <= 10) return n;
                                }
                            }
                        }
                        return null;
                    };

                    let stepperQty = checkStepperInside(card);

                    // Also check floating sibling pill positioned over this card's top-right corner
                    if (stepperQty === null) {
                        const candidates = Array.from(document.querySelectorAll('div, span'));
                        for (const c of candidates) {
                            if (c.offsetParent === null) continue;
                            const cr = c.getBoundingClientRect();
                            if (cr.width < 40 || cr.width > 160 || cr.height < 18 || cr.height > 55) continue;
                            // Must overlap this card's bounding box horizontally and near top edge
                            const overlapsX = cr.left >= rect.left - 15 && cr.right <= rect.right + 20;
                            const overlapsY = cr.top >= rect.top - 25 && cr.bottom <= rect.bottom + 15;
                            if (!overlapsX || !overlapsY) continue;
                            const q = checkStepperInside(c);
                            if (q !== null) {
                                stepperQty = q;
                                break;
                            }
                        }
                    }

                    // Check visual blue active border as secondary verification
                    const style = window.getComputedStyle(card);
                    const borderColor = (style.borderColor || '').toLowerCase();
                    const isBlueBorder =
                        borderColor.includes('rgb(59') ||
                        borderColor.includes('rgb(37') ||
                        borderColor.includes('rgb(29') ||
                        borderColor.includes('rgb(0, 1') ||
                        (card.className || '').toLowerCase().includes('blue') ||
                        (card.className || '').toLowerCase().includes('active') ||
                        (card.className || '').toLowerCase().includes('selected');

                    return {
                        status: 'in_stock',
                        selected: stepperQty !== null || isBlueBorder,
                        qty: stepperQty !== null ? stepperQty : 1,
                        cx: rect.left + Math.min(rect.width * 0.35, 90),
                        cy: rect.top + rect.height * 0.65,
                    };
                }""",
                target_label,
            )

        # Wait up to 2.5s for card grid to hydrate
        info = None
        for _ in range(35):
            info = await _inspect_card()
            if info and info.get("status") in ("oos", "in_stock"):
                break
            await asyncio.sleep(0.07)

        if not info or info.get("status") == "waiting":
            return "not_found", 1
        if info.get("status") == "oos":
            return "oos", 1

        if info.get("selected"):
            return "selected", int(info.get("qty") or 1)

        # Click exact target card center (lower-left area so we never accidentally hit a pill)
        for attempt in range(3):
            cx, cy = info["cx"], info["cy"]
            await page.mouse.click(cx, cy)

            # Verify the '- 1 +' stepper actually moved onto our target card!
            for _ in range(16):
                await asyncio.sleep(0.05)
                check = await _inspect_card()
                if check and check.get("status") == "oos":
                    return "oos", 1
                if check and check.get("selected"):
                    return "selected", int(check.get("qty") or 1)

            # Refresh coordinates before retry
            info = await _inspect_card()
            if not info or info.get("status") != "in_stock":
                return "not_found", 1

        return "not_found", 1

    async def _set_and_verify_quantity(self, page, denom: str, desired_qty: int) -> Tuple[bool, int]:
        """
        Reads the exact live quantity inside the selected 'Rs.<denom>' card's '- <qty> +' pill.
        - If desired_qty == live_qty: does NOTHING (0ms!).
        - If desired_qty != live_qty: clicks the exact '+' or '-' button inside THAT card's pill
          using a real Playwright mouse click and STRICTLY VERIFIES that the displayed number changed!
        """
        target_label = f"Rs.{denom} Wallet Code"

        async def _locate_stepper():
            return await page.evaluate(
                r"""(label) => {
                    const lowerLabel = label.toLowerCase();
                    const allEls = Array.from(document.querySelectorAll('div, button, span, p'));
                    let labelEl = null;
                    for (const el of allEls) {
                        const t = (el.innerText || '').trim().toLowerCase();
                        if ((t === lowerLabel || t.startsWith(lowerLabel + '\n')) && t.length < 90 && el.offsetParent !== null) {
                            labelEl = el;
                            break;
                        }
                    }
                    if (!labelEl) return null;

                    let card = labelEl;
                    let cur = labelEl;
                    for (let depth = 0; depth < 6 && cur && cur.parentElement; depth++) {
                        const pt = (cur.parentElement.innerText || '').toLowerCase();
                        if ((pt.match(/rs\.\d{4}\s*wallet code/g) || []).length > 1) break;
                        cur = cur.parentElement;
                        if (pt.includes('₹')) card = cur;
                    }
                    const rect = card.getBoundingClientRect();

                    // Find the exact '- <qty> +' pill overlapping this card's top-right area
                    let pill = null;
                    let qtyVal = null;
                    const boxes = Array.from(document.querySelectorAll('div, span'));
                    for (const b of boxes) {
                        if (b.offsetParent === null) continue;
                        const br = b.getBoundingClientRect();
                        if (br.width < 40 || br.width > 170 || br.height < 18 || br.height > 55) continue;
                        const overlapsX = br.left >= rect.left - 15 && br.right <= rect.right + 20;
                        const overlapsY = br.top >= rect.top - 25 && br.bottom <= rect.bottom + 15;
                        if (!overlapsX || !overlapsY) continue;

                        const txt = (b.innerText || '').trim();
                        const lines = txt.split(/\s+/).filter(Boolean);
                        if (lines.includes('+') || lines.includes('-') || lines.includes('−')) {
                            for (const ln of lines) {
                                if (/^\d+$/.test(ln)) {
                                    const n = parseInt(ln, 10);
                                    if (n >= 1 && n <= 10) {
                                        pill = b;
                                        qtyVal = n;
                                        break;
                                    }
                                }
                            }
                        }
                        if (pill) break;
                    }

                    if (!pill) return null;
                    const pr = pill.getBoundingClientRect();

                    // Find exact '+' and '-' child clickable elements inside the pill
                    let plusBox = null;
                    let minusBox = null;
                    const kids = Array.from(pill.querySelectorAll('button, span, div, svg'));
                    for (const k of kids) {
                        const kt = (k.innerText || '').trim();
                        const kr = k.getBoundingClientRect();
                        if (kr.width <= 0 || kr.height <= 0) continue;
                        if (kt === '+') {
                            plusBox = { x: kr.left + kr.width / 2, y: kr.top + kr.height / 2 };
                        } else if (kt === '-' || kt === '−') {
                            minusBox = { x: kr.left + kr.width / 2, y: kr.top + kr.height / 2 };
                        }
                    }

                    // Geometric fallback on left/right edges of the pill if buttons are SVG-only
                    if (!minusBox) {
                        minusBox = { x: pr.left + pr.width * 0.18, y: pr.top + pr.height / 2 };
                    }
                    if (!plusBox) {
                        plusBox = { x: pr.left + pr.width * 0.82, y: pr.top + pr.height / 2 };
                    }

                    return { qty: qtyVal, plus: plusBox, minus: minusBox };
                }""",
                target_label,
            )

        stepper = await _locate_stepper()
        if not stepper:
            return (desired_qty == 1), 1

        current_qty = int(stepper["qty"])
        if current_qty == desired_qty:
            return True, current_qty

        for _ in range(3):
            btn = stepper["plus"] if desired_qty > current_qty else stepper["minus"]
            await page.mouse.click(btn["x"], btn["y"])

            # Poll every 40ms up to 800ms to confirm the counter actually updated!
            for _ in range(20):
                await asyncio.sleep(0.04)
                updated = await _locate_stepper()
                if updated and int(updated["qty"]) == desired_qty:
                    return True, desired_qty
                if updated:
                    current_qty = int(updated["qty"])
                    stepper = updated

        return (current_qty == desired_qty), current_qty

    async def _extract_payable_from_dom(self, page, face_value: int, qty: int) -> Optional[float]:
        """Fast <15ms DOM parser that reads exact Total Payable or Base + Fees from Rooter's UI."""
        base_total = float(face_value * qty)
        result = await page.evaluate(
            r"""(baseTotal) => {
                const body = (document.body && document.body.innerText) ? document.body.innerText : '';
                const lines = body.split('\n').map(l => l.trim()).filter(Boolean);

                const parseRupee = (str) => {
                    if (!str) return null;
                    const m = str.match(/₹\s*([\d,]+(?:\.\d{1,2})?)/);
                    if (!m) return null;
                    return parseFloat(m[1].replace(/,/g, ''));
                };

                const findValueNear = (pattern) => {
                    for (let i = 0; i < lines.length; i++) {
                        if (pattern.test(lines[i])) {
                            const same = parseRupee(lines[i]);
                            if (same !== null) return same;
                            if (i + 1 < lines.length) {
                                const nxt = parseRupee(lines[i + 1]);
                                if (nxt !== null) return nxt;
                            }
                        }
                    }
                    return null;
                };

                // 1. Explicit Total Payable / Paid Amount / Amount Payable / To Pay
                const explicitPayable = findValueNear(
                    /^(?:total\s*payable|payable\s*amount|amount\s*payable|grand\s*total|to\s*pay|net\s*payable|paid\s*amount|total\s*amount)/i
                );
                if (explicitPayable !== null && explicitPayable >= baseTotal * 0.5) {
                    return explicitPayable;
                }

                // 2. Explicit 'Total Fees' (e.g. ₹156 in Rooter's Bill Details)
                const totalFees = findValueNear(/^total\s*fees/i);
                if (totalFees !== null && totalFees >= 0 && totalFees <= baseTotal) {
                    return baseTotal + totalFees;
                }

                // 3. Individual 'Platform Fees' + 'Surge Fees' + 'Convenience Fees'
                const platformFee = findValueNear(/^platform\s*fees?/i);
                const surgeFee = findValueNear(/^surge\s*fees?/i);
                const convFee = findValueNear(/^convenience\s*fees?/i);
                if (platformFee !== null || surgeFee !== null || convFee !== null) {
                    return baseTotal + (platformFee || 0) + (surgeFee || 0) + (convFee || 0);
                }

                return null;
            }""",
            base_total,
        )
        return float(result) if result is not None else None

    async def _check_price_abort_limit(
        self, page, slot_id: str, denom: str, qty: int, peek_bill: bool = True
    ) -> Tuple[bool, Optional[Dict]]:
        """
        Millisecond surge-price protector.
        Returns: (allowed_to_buy, price_info_dict)
        If self.abort_limit_pct is None (OFF), returns (True, None) in 0ms!
        """
        if self.abort_limit_pct is None:
            return True, None

        face_value = int(denom)
        payable = await self._extract_payable_from_dom(page, face_value, qty)

        # If bill isn't expanded yet on product page, peek 'View Detailed Bill' in ~150ms
        opened_bill = False
        if payable is None and peek_bill:
            clicked_bill = await page.evaluate(r"""() => {
                const els = Array.from(document.querySelectorAll('button, div, span, p, a'));
                for (const el of els) {
                    const t = (el.innerText || '').trim().toLowerCase();
                    if (t.includes('view detailed bill') && t.length < 40 && el.offsetParent !== null) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""")
            if clicked_bill:
                opened_bill = True
                for _ in range(14):
                    await asyncio.sleep(0.04)
                    payable = await self._extract_payable_from_dom(page, face_value, qty)
                    if payable is not None:
                        break

        # If bill opened a modal covering the Pay button, close it cleanly so Pay stays clickable
        if opened_bill:
            pay_still_visible = False
            try:
                pay_still_visible = await page.locator("button >> text='Pay via wallet' >> visible=true").first.is_visible(timeout=200)
            except Exception:
                pass
            if not pay_still_visible:
                try:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.1)
                except Exception:
                    pass

        if payable is None:
            # Bill wasn't available here yet; we'll re-verify inside Delivery Details modal before final pay!
            return True, None

        per_code_cost = payable / max(1, qty)
        extra_pct = ((per_code_cost - face_value) / face_value) * 100.0
        max_per_code = face_value * (1.0 + self.abort_limit_pct / 100.0)

        info = {
            "face_value": face_value,
            "qty": qty,
            "total_payable": payable,
            "per_code_cost": per_code_cost,
            "extra_pct": extra_pct,
            "limit_pct": self.abort_limit_pct,
            "max_per_code": max_per_code,
        }

        if extra_pct > (self.abort_limit_pct + 0.001):
            log_step(
                slot_id,
                f"🛑 ABORTED! Rs.{denom}x{qty} payable=₹{payable:.2f} (₹{per_code_cost:.2f}/code = +{extra_pct:.2f}% > {self.abort_limit_pct}% limit)",
            )
            return False, info

        log_step(
            slot_id,
            f"✅ PRICE OK! Rs.{denom}x{qty} payable=₹{payable:.2f} (₹{per_code_cost:.2f}/code = +{extra_pct:.2f}% ≤ {self.abort_limit_pct}%)",
        )
        return True, info

    async def _verify_order_page_and_extract_details(
        self, page, timeout_sec: float = 16.0
    ) -> Tuple[bool, Dict]:
        """
        Strictly verifies that the browser reached Rooter's Payment / Order Confirmation page
        (#MO_..., 'items delivered successfully', 'Coupon Codes' -> 'Code #01') and extracts:
          - order_id
          - codes (list)
          - actual_qty (int or None)
          - actual_product (str or None)
          - paid_amount (str or None)
        """
        deadline = time.monotonic() + timeout_sec
        reached = False
        latest: Dict = {
            "order_id": "Confirmed",
            "codes": [],
            "actual_qty": None,
            "actual_product": None,
            "paid_amount": None,
        }

        while time.monotonic() < deadline:
            try:
                data = await page.evaluate(r"""() => {
                    const body = (document.body && document.body.innerText) ? document.body.innerText : '';
                    const lower = body.toLowerCase();
                    const url = (window.location.href || '').toLowerCase();

                    // Dismiss floating promo/sale popup ('X') on right side for a crisp screenshot
                    const closeEls = Array.from(document.querySelectorAll('button, span, div, svg'));
                    for (const el of closeEls) {
                        const t = (el.innerText || '').trim();
                        if (t === 'X' || t === '✕' || t === '×') {
                            const r = el.getBoundingClientRect();
                            if (r.left > 700 && r.top > 300) {
                                try { el.click(); } catch (e) {}
                            }
                        }
                    }

                    const orderMatch = body.match(/#?(MO_\d+_\d+)/i);
                    const hasOrderMarker = Boolean(
                        orderMatch ||
                        lower.includes('items delivered successfully') ||
                        lower.includes('item purchased') ||
                        lower.includes('coupon codes') ||
                        url.includes('/order')
                    );

                    if (!hasOrderMarker) {
                        return { reached: false };
                    }

                    const lines = body.split('\n').map(l => l.trim()).filter(Boolean);
                    const codes = [];

                    // Method 1: Exact Rooter 'Code #01' / 'Code #02' next-line parser
                    for (let i = 0; i < lines.length; i++) {
                        if (/^code\s*#?\s*\d+/i.test(lines[i])) {
                            for (let j = i + 1; j < Math.min(i + 4, lines.length); j++) {
                                const cand = lines[j];
                                if (
                                    cand.length >= 8 &&
                                    cand.length <= 32 &&
                                    !/^(copy|redeem|write a|how to|code|coupon|product|delivery)/i.test(cand) &&
                                    /^[A-Z0-9-]+$/i.test(cand)
                                ) {
                                    codes.push(cand);
                                    break;
                                }
                            }
                        }
                    }

                    // Method 2: Standard PSN 12-char hyphenated format (XXXX-XXXX-XXXX)
                    const psnMatches = body.match(/\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\b/g) || [];
                    for (const m of psnMatches) {
                        codes.push(m);
                    }

                    // Extract real purchased quantity & denomination from order details
                    let actualQty = null;
                    let actualProduct = null;
                    const itemMatch = body.match(/(\d+)\s*[xX]\s*([^\n₹]+Wallet Code)/i);
                    if (itemMatch) {
                        actualQty = parseInt(itemMatch[1], 10);
                        actualProduct = itemMatch[2].trim();
                    } else {
                        const purMatch = body.match(/(\d+)\s*items?\s*purchased/i);
                        if (purMatch) actualQty = parseInt(purMatch[1], 10);
                    }

                    // Extract Paid Amount
                    let paidAmount = null;
                    for (let i = 0; i < lines.length; i++) {
                        if (/^paid\s*amount/i.test(lines[i])) {
                            const mSame = lines[i].match(/₹\s*[\d,]+(?:\.\d{1,2})?/);
                            if (mSame) { paidAmount = mSame[0]; break; }
                            if (i + 1 < lines.length) {
                                const mNxt = lines[i + 1].match(/₹\s*[\d,]+(?:\.\d{1,2})?/);
                                if (mNxt) { paidAmount = mNxt[0]; break; }
                            }
                        }
                    }
                    if (!paidAmount) {
                        for (let i = 0; i < Math.min(8, lines.length); i++) {
                            const mTop = lines[i].match(/^₹\s*[\d,]+(?:\.\d{1,2})?$/);
                            if (mTop) { paidAmount = mTop[0]; break; }
                        }
                    }

                    return {
                        reached: true,
                        order_id: orderMatch ? '#' + orderMatch[1].replace(/^#/, '') : 'Confirmed',
                        codes: Array.from(new Set(codes)),
                        actual_qty: actualQty,
                        actual_product: actualProduct,
                        paid_amount: paidAmount,
                    };
                }""")

                if data and data.get("reached"):
                    reached = True
                    latest = data
                    if data.get("codes"):
                        await asyncio.sleep(0.35)
                        return True, latest
            except Exception:
                pass

            await asyncio.sleep(0.3)

        return reached, latest

    # =========================================================
    # 🎯 HIGH-SPEED SWARM SNIPER WORKER (TUNED FOR 8vCPU / 8GB)
    # =========================================================

    async def _sniper_worker(self, slot_id: str):
        account = self.get_account(slot_id)

        try:
            slot_num = int(slot_id.split("-A")[-1])
            await asyncio.sleep((slot_num - 1) * 2.0)
        except Exception:
            pass

        log_step(
            slot_id,
            f"Swarm Sniper started! Target: Rs.{self.target_denom} x {self.target_qty} | Abort: {self._abort_label()}",
        )
        await self.telegram.send_message(
            f"🎯 <b>Swarm Sniper ACTIVATED</b> for <b>{esc(slot_id)}</b>\n"
            f"• Target: <code>Rs.{esc(self.target_denom)}</code> | Qty: <code>{self.target_qty}</code> | Abort: <code>{esc(self._abort_label())}</code>",
            parse_mode="HTML",
        )

        cycle_count = 0

        while account.logged_in:
            try:
                target_denom = self.target_denom
                prefed_qty = self.target_qty

                cycle_count += 1
                if cycle_count % 60 == 0:
                    try:
                        await self.browser.save_session(slot_id, account.phone_number)
                        await self.browser.reset_slot_page(slot_id)
                    except Exception:
                        pass

                last_time = now_ist_time()
                account.update(
                    current_page=f"⏳ [Wait 18s | Rs.{target_denom}x{prefed_qty}] ({last_time})",
                    last_error=None,
                )

                try:
                    await asyncio.wait_for(self.stock_event.wait(), timeout=18.0)
                    account.update(current_page=f"⚡ [Swarm Triggered! Attacking Rs.{target_denom}!]")
                except asyncio.TimeoutError:
                    pass

                if not account.logged_in:
                    break

                # Always pick up latest settings changed via /start buttons or commands
                target_denom = self.target_denom
                prefed_qty = self.target_qty

                page = await self.browser.get_page(slot_id)

                account.update(current_page="🔄 [Refreshing Product Page]")
                cache_buster = int(time.time() * 1000)
                fresh_url = (
                    f"{settings.product_url}?cb={cache_buster}"
                    if "?" not in settings.product_url
                    else f"{settings.product_url}&cb={cache_buster}"
                )

                await page.goto(fresh_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(0.35)
                await self._destroy_location_popup(page, slot_id)

                content = await page.content()
                if "Application error: a client-side exception" in content:
                    account.update(last_error="Website White Screen - Auto Retrying")
                    await asyncio.sleep(2.0)
                    continue

                # STRICT DENOMINATION SELECTION & VERIFICATION (Never buys default Rs.1000 by mistake!)
                account.update(current_page=f"🎯 [Selecting Rs.{target_denom}]")
                sel_status, live_card_qty = await self._find_and_select_denomination(page, target_denom)

                if sel_status == "oos":
                    account.update(current_page=f"😴 [Rs.{target_denom} Out of Stock] ({now_ist_time()})")
                    await asyncio.sleep(0.8)
                    continue

                if sel_status != "selected":
                    account.update(
                        current_page=f"⚠️ [Rs.{target_denom} Not Confirmed - Retrying]",
                        last_error=f"Could not verify Rs.{target_denom} selection",
                    )
                    await asyncio.sleep(1.0)
                    continue

                await asyncio.sleep(0.2)
                await self._update_live_balance(page, account)

                pay_btn_1 = page.locator("button >> text='Pay via wallet' >> visible=true").first
                is_in_stock = False
                try:
                    is_in_stock = await pay_btn_1.is_visible(timeout=1400)
                except Exception:
                    pass

                if not is_in_stock:
                    account.update(current_page=f"😴 [Rs.{target_denom} OOS / Low Bal] ({now_ist_time()})")
                    await asyncio.sleep(0.8)
                    continue

                log_step(slot_id, f"🚨 Rs.{target_denom} IN STOCK! Launching buy (Qty: {prefed_qty}, Abort: {self._abort_label()})")
                account.update(current_page=f"🚨 [BUYING Rs.{target_denom} x {prefed_qty}!]")

                if not self.stock_event.is_set():
                    self.stock_event.set()
                    asyncio.create_task(self._clear_stock_event_delayed())

                asyncio.create_task(
                    self.telegram.send_message(
                        f"🚨 <b>Rs.{esc(target_denom)} STOCK DETECTED in {esc(slot_id)}!</b>\n"
                        f"⚡ Waking all accounts | Target: <code>Rs.{esc(target_denom)}</code> | "
                        f"Qty: <code>{prefed_qty}</code> | Abort: <code>{esc(self._abort_label())}</code>",
                        parse_mode="HTML",
                    )
                )

                # If pre-fed Qty is 1 -> only [1]. If Qty 2 -> [2, 1] (tries 2 first, falls back to 1 on low balance).
                qty_attempts = [1] if prefed_qty == 1 else [2, 1]
                purchase_completed = False
                aborted_due_to_surge = False

                for attempt_qty in qty_attempts:
                    try:
                        # Ensure we are on product page with exact target denomination selected
                        if "playstation-gift-cards" not in (page.url or "").lower():
                            await page.goto(fresh_url, wait_until="domcontentloaded", timeout=25000)
                            await asyncio.sleep(0.35)
                            await self._destroy_location_popup(page, slot_id)
                            re_sel, _ = await self._find_and_select_denomination(page, target_denom)
                            if re_sel != "selected":
                                continue

                        # STRICT QUANTITY SELECTION & VERIFICATION
                        account.update(current_page=f"🔢 [Setting Qty → {attempt_qty}]")
                        qty_ok, verified_qty = await self._set_and_verify_quantity(
                            page, target_denom, desired_qty=attempt_qty
                        )
                        if not qty_ok:
                            log_step(slot_id, f"Could not verify Qty {attempt_qty} (still {verified_qty})")
                            if attempt_qty == 2:
                                # Could not bump to 2 (maybe stock/limit is 1); fall back cleanly to 1
                                continue
                            else:
                                continue

                        # Check wallet sufficiency dynamically via 'Pay via wallet' visibility
                        pay_btn_1 = page.locator("button >> text='Pay via wallet' >> visible=true").first
                        can_pay_wallet = False
                        try:
                            can_pay_wallet = await pay_btn_1.is_visible(timeout=1200)
                        except Exception:
                            pass

                        if not can_pay_wallet:
                            if attempt_qty == 2:
                                log_step(slot_id, "Wallet insufficient for Qty 2, switching to Qty 1 immediately...")
                                asyncio.create_task(
                                    self.telegram.send_message(
                                        f"⚠️ <b>{esc(slot_id)}</b>: Qty 2 ke liye wallet balance kam hai, turant Qty 1 try kar raha hu!",
                                        parse_mode="HTML",
                                    )
                                )
                                continue
                            else:
                                account.update(current_page="🛑 [Stopped - Insufficient Balance for Qty 1]")
                                asyncio.create_task(
                                    self.telegram.send_message(
                                        f"🛑 <b>{esc(slot_id)}</b>: <code>Rs.{esc(target_denom)} x 1</code> ke liye wallet balance kam hai.",
                                        parse_mode="HTML",
                                    )
                                )
                                break

                        # MILLISECOND PRICE SURGE ABORT CHECK (Pass 1: Product Page / View Detailed Bill)
                        price_ok, price_info = await self._check_price_abort_limit(
                            page, slot_id, target_denom, verified_qty, peek_bill=True
                        )
                        if not price_ok and price_info:
                            aborted_due_to_surge = True
                            account.update(
                                current_page=(
                                    f"🛑 [ABORTED +{price_info['extra_pct']:.1f}% > {price_info['limit_pct']}%]"
                                )
                            )
                            now_ts = time.monotonic()
                            if now_ts - self._last_abort_alert.get(slot_id, 0.0) > 60.0:
                                self._last_abort_alert[slot_id] = now_ts
                                asyncio.create_task(
                                    self.telegram.send_message(
                                        f"🛡️ <b>BUY ABORTED — HIGH PRICE SURGE!</b> ({esc(slot_id)})\n\n"
                                        f"🎮 <b>Code:</b> <code>Rs.{price_info['face_value']} x {price_info['qty']}</code>\n"
                                        f"💸 <b>Total Payable:</b> <code>₹{price_info['total_payable']:,.2f}</code>\n"
                                        f"📊 <b>Per Code Cost:</b> <code>₹{price_info['per_code_cost']:,.2f}</code> "
                                        f"(<b>+{price_info['extra_pct']:.2f}%</b>)\n"
                                        f"🛑 <b>Your Abort Limit:</b> <code>≤ {price_info['limit_pct']}%</code> "
                                        f"(Max <code>₹{price_info['max_per_code']:,.2f}/code</code>)\n\n"
                                        "Buy skip kar diya gaya! Limit badhani ho toh <code>/start</code> → <b>Abort Limit</b> dabao.",
                                        parse_mode="HTML",
                                    )
                                )
                            break

                        # STEP 1: Click First 'Pay via wallet'
                        account.update(current_page=f"💳 [Clicking Pay via Wallet (Qty {verified_qty})]")
                        await pay_btn_1.click(force=True, timeout=2500)
                        await pay_btn_1.evaluate("(el) => el.click()")

                        # STEP 2: Wait for 'Delivery Details' Confirmation Modal
                        account.update(current_page="📝 [Confirming Delivery Popup]")
                        await page.locator("text=Delivery Details >> visible=true").wait_for(
                            state="visible", timeout=5000
                        )
                        await asyncio.sleep(0.2)

                        # MILLISECOND PRICE SURGE ABORT CHECK (Pass 2: Double-Check Inside Delivery Modal!)
                        if self.abort_limit_pct is not None and price_info is None:
                            modal_ok, modal_info = await self._check_price_abort_limit(
                                page, slot_id, target_denom, verified_qty, peek_bill=False
                            )
                            if not modal_ok and modal_info:
                                aborted_due_to_surge = True
                                try:
                                    await page.keyboard.press("Escape")
                                except Exception:
                                    pass
                                account.update(
                                    current_page=(
                                        f"🛑 [ABORTED +{modal_info['extra_pct']:.1f}% > {modal_info['limit_pct']}%]"
                                    )
                                )
                                now_ts = time.monotonic()
                                if now_ts - self._last_abort_alert.get(slot_id, 0.0) > 60.0:
                                    self._last_abort_alert[slot_id] = now_ts
                                    asyncio.create_task(
                                        self.telegram.send_message(
                                            f"🛡️ <b>BUY ABORTED IN CHECKOUT — HIGH PRICE!</b> ({esc(slot_id)})\n\n"
                                            f"🎮 <b>Code:</b> <code>Rs.{modal_info['face_value']} x {modal_info['qty']}</code>\n"
                                            f"💸 <b>Total Payable:</b> <code>₹{modal_info['total_payable']:,.2f}</code>\n"
                                            f"📊 <b>Per Code Cost:</b> <code>₹{modal_info['per_code_cost']:,.2f}</code> "
                                            f"(<b>+{modal_info['extra_pct']:.2f}%</b>)\n"
                                            f"🛑 <b>Your Abort Limit:</b> <code>≤ {modal_info['limit_pct']}%</code>",
                                            parse_mode="HTML",
                                        )
                                    )
                                break

                        # STEP 3: Click Final 'Pay via wallet'
                        account.update(current_page="💳 [Clicking Final Pay via Wallet]")
                        pay_btn_2 = page.locator("button >> text='Pay via wallet' >> visible=true").last
                        await pay_btn_2.wait_for(state="visible", timeout=4000)
                        await pay_btn_2.click(force=True, timeout=2500)
                        await pay_btn_2.evaluate("(el) => el.click()")

                        # STEP 4: STRICT PAYMENT PAGE VERIFICATION & CODE EXTRACTION (Zero Fake Success!)
                        account.update(current_page="⏳ [Verifying Payment Page & Extracting Code...]")
                        verified, details = await self._verify_order_page_and_extract_details(
                            page, timeout_sec=16.0
                        )

                        if not verified:
                            log_step(slot_id, f"Order page not reached on Qty {verified_qty}, retrying cleanly...")
                            account.update(last_error=f"Order page not reached (Qty {verified_qty}), retrying...")
                            continue

                        # VERIFIED ON ORDER PAGE! Send Payment Page Screenshot + Copyable Codes in 1 HTML Message!
                        purchase_completed = True
                        order_id = details.get("order_id") or "Confirmed"
                        codes = details.get("codes") or []
                        final_qty = details.get("actual_qty") or (len(codes) if codes else verified_qty)
                        final_prod = details.get("actual_product") or f"Rs.{target_denom} Wallet Code"
                        paid_str = details.get("paid_amount") or (
                            f"₹{price_info['total_payable']:,.2f}" if price_info else "Paid via Wallet"
                        )
                        ist_now = now_ist_full()

                        account.update(
                            current_page=f"✅ [BOUGHT Rs.{target_denom}x{final_qty} | {order_id}]",
                            last_order_id=order_id,
                            last_error=None,
                        )
                        log_step(slot_id, f"✅ VERIFIED ORDER {order_id} | Qty={final_qty} | Paid={paid_str} | Codes={codes}")

                        if codes:
                            codes_text = "\n".join(
                                f"🎟 <b>Code #{idx:02d}:</b> <code>{esc(code)}</code>"
                                for idx, code in enumerate(codes, start=1)
                            )
                        else:
                            codes_text = "🎟 <b>Code:</b> Screenshot mein <i>Coupon Codes</i> section dekho!"

                        success_caption = (
                            "✅✅ <b>SWARM SUCCESS: CODE PURCHASED!</b>\n\n"
                            f"🆔 <b>Order ID:</b> <code>{esc(order_id)}</code>\n"
                            f"🎮 <b>Product:</b> {esc(final_prod)}\n"
                            f"📦 <b>Quantity:</b> <code>{esc(final_qty)}</code>\n"
                            f"💰 <b>Paid Amount:</b> <code>{esc(paid_str)}</code>\n"
                            f"👤 <b>Slot:</b> {esc(slot_id)} (<code>{esc(account.phone_number or 'Active')}</code>)\n\n"
                            f"{codes_text}\n\n"
                            f"🕒 <b>Time:</b> {esc(ist_now)}"
                        )

                        shot_path = self.screenshots_dir / f"{slot_id}_order_success_{int(time.time())}.png"
                        try:
                            await page.screenshot(path=str(shot_path), full_page=False)
                            await self.telegram.send_photo(
                                photo_path=str(shot_path),
                                caption=success_caption,
                                parse_mode="HTML",
                            )
                        except Exception as ss_err:
                            log_step(slot_id, f"Photo send fallback note: {ss_err}")
                            await self.telegram.send_message(success_caption, parse_mode="HTML")
                        finally:
                            if shot_path.exists():
                                try:
                                    shot_path.unlink()
                                except Exception:
                                    pass

                        await self.browser.save_session(slot_id, account.phone_number)
                        break

                    except Exception as flow_err:
                        log_step(slot_id, f"Attempt Qty {attempt_qty} note: {flow_err}")
                        account.update(last_error=f"Retry (Qty {attempt_qty}): {str(flow_err)[:40]}")
                        continue

                if purchase_completed:
                    await asyncio.sleep(6.0)
                elif aborted_due_to_surge:
                    await asyncio.sleep(8.0)
                else:
                    await asyncio.sleep(1.2)

            except asyncio.CancelledError:
                account.update(current_page="⏸️ Sniper Paused")
                break
            except Exception as loop_err:
                err_msg = str(loop_err)
                log_step(slot_id, f"Sniper loop error: {err_msg[:60]}")
                account.update(
                    current_page="🛠️ [Auto-Healing Tab...]",
                    last_error=f"Self-Healed: {err_msg[:45]}",
                )
                try:
                    await self.browser.reset_slot_page(slot_id)
                except Exception:
                    pass
                await asyncio.sleep(2.0)

        self.sniper_tasks.pop(slot_id, None)

    async def _clear_stock_event_delayed(self):
        await asyncio.sleep(3.0)
        self.stock_event.clear()

    # =========================================================
    # SNIPER START/STOP & MANUAL TRIGGER COMMANDS
    # =========================================================

    async def handle_sniper_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            args = context.args
            target = args[0].lower() if args else "all"

            if target == "all":
                started = self._ensure_snipers_running()
                await update.message.reply_text(
                    f"🔫 Swarm Sniper active/started! ({started} newly launched)\n"
                    f"🎯 Target: <code>Rs.{esc(self.target_denom)} x {self.target_qty}</code> | "
                    f"🛡️ Abort: <code>{esc(self._abort_label())}</code>",
                    parse_mode="HTML",
                )
            else:
                slot_id = target.upper()
                acc = self.get_account(slot_id)
                if not acc.logged_in:
                    return await update.message.reply_text(f"❌ {slot_id} is not logged in!")
                task = self.sniper_tasks.get(slot_id)
                if task is None or task.done():
                    self.sniper_tasks[slot_id] = asyncio.create_task(self._sniper_worker(slot_id))
                    await update.message.reply_text(
                        f"🔫 Sniper started for <b>{esc(slot_id)}</b>", parse_mode="HTML"
                    )
                else:
                    await update.message.reply_text(f"✅ Sniper is already running on {slot_id}")
        except Exception as exc:
            await update.message.reply_text(f"❌ Error: {exc}")

    async def handle_stop_sniper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            args = context.args
            target = args[0].lower() if args else "all"

            if target == "all":
                stopped = 0
                for slot_id, task in list(self.sniper_tasks.items()):
                    if task and not task.done():
                        task.cancel()
                        stopped += 1
                    self.sniper_tasks.pop(slot_id, None)
                await update.message.reply_text(f"⏸️ Paused sniper on {stopped} accounts.")
            else:
                slot_id = target.upper()
                task = self.sniper_tasks.pop(slot_id, None)
                if task and not task.done():
                    task.cancel()
                    await update.message.reply_text(f"⏸️ Sniper paused for {slot_id}")
                else:
                    await update.message.reply_text(f"ℹ️ Sniper was not running on {slot_id}")
        except Exception as exc:
            await update.message.reply_text(f"❌ Error: {exc}")

    async def trigger_stock_buy(self):
        self.stock_event.set()
        await asyncio.sleep(3.0)
        self.stock_event.clear()

    async def handle_trigger_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"🚨 <b>SWARM TRIGGER SENT!</b>\nWaking up ALL accounts to attack "
            f"<code>Rs.{esc(self.target_denom)} x {self.target_qty}</code> "
            f"(Abort: <code>{esc(self._abort_label())}</code>) NOW!",
            parse_mode="HTML",
        )
        await self.trigger_stock_buy()
