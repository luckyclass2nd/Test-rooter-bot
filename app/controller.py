import asyncio
import json
import re
import sys
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from telegram import Update
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


def log_step(slot_id: str, msg: str):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] [{slot_id}] {msg}", flush=True)


class AccountController:

    VALID_DENOMS = ("1000", "2000", "3000", "4000", "5000")

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

        # Pre-feedable Target Denomination & Quantity (Persisted on disk)
        self.settings_file = Path(settings.sessions_dir) / "swarm_target_config.json"
        self.target_denom: str = "5000"
        self.target_qty: int = 1
        self._load_target_config()

        self.stock_event = asyncio.Event()
        self.sniper_tasks: Dict[str, asyncio.Task] = {}

        self._create_slots()
        log_step(
            "SYSTEM",
            f"Controller initialized | Target: Rs.{self.target_denom} | Pre-Fed Qty: {self.target_qty}",
        )

    # =========================================================
    # PRE-FED TARGET DENOMINATION & QUANTITY PERSISTENCE
    # =========================================================

    def _load_target_config(self):
        try:
            if self.settings_file.exists():
                data = json.loads(self.settings_file.read_text(encoding="utf-8"))
                denom = str(data.get("target_denom", "5000")).strip()
                qty = int(data.get("target_qty", 1))
                if denom in self.VALID_DENOMS:
                    self.target_denom = denom
                if qty in (1, 2):
                    self.target_qty = qty
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
        account = self.accounts.get(slot_id)
        if account is None:
            raise ValueError(f"Unknown slot: {slot_id}")
        return account

    def all_accounts(self):
        return list(self.accounts.values())

    # =========================================================
    # COMMAND CONNECTION
    # =========================================================

    def connect_commands(self):
        self.telegram.register_command("start", self.handle_start)
        self.telegram.register_command("status", self.handle_status)
        self.telegram.register_command("denom", self.handle_set_denom)
        self.telegram.register_command("target", self.handle_set_denom)
        self.telegram.register_command("code", self.handle_set_denom)
        self.telegram.register_command("qty", self.handle_set_qty)
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

    # =========================================================
    # NEW: PRE-FEED DENOMINATION (/denom) & QUANTITY (/qty)
    # =========================================================

    async def handle_set_denom(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            return await update.message.reply_text(
                f"🎯 Current Target Code: Rs.{self.target_denom} Wallet Code\n"
                f"📦 Current Target Quantity: {self.target_qty}\n\n"
                "Change karne ke liye bhej:\n"
                "`/denom 1000`\n"
                "`/denom 2000`\n"
                "`/denom 3000`\n"
                "`/denom 4000`\n"
                "`/denom 5000`"
            )

        raw_val = re.sub(r"[^\d]", "", context.args[0].strip())
        if raw_val not in self.VALID_DENOMS:
            return await update.message.reply_text(
                "❌ Valid denomination daal bhai: `1000`, `2000`, `3000`, `4000`, ya `5000`\n"
                "Example: `/denom 2000`"
            )

        self.target_denom = raw_val
        self._save_target_config()
        self._ensure_snipers_running()

        qty_mode_str = (
            "1 (Direct Fast Buy - No '+' Click)"
            if self.target_qty == 1
            else "2 (Clicks '+' for Qty 2, Smart Fallback to Qty 1)"
        )
        await update.message.reply_text(
            f"🎯 Target Denomination Locked: **Rs.{self.target_denom} Wallet Code**!\n"
            f"📦 Pre-Fed Quantity: **{qty_mode_str}**\n"
            f"🔫 Saare accounts ab Rs.{self.target_denom} ka code snipe karenge!"
        )

    async def handle_set_qty(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args or context.args[0].strip() not in ("1", "2"):
            return await update.message.reply_text(
                f"📦 Current Pre-Fed Quantity: **{self.target_qty}**\n\n"
                "Change karne ke liye bhej:\n"
                "• `/qty 1` -> Quantity nahi chhedega, seedha ultra-fast 1 code buy karega!\n"
                "• `/qty 2` -> '+' daba kar 2 code buy karega (agar 2 ka balance kam hua toh 1 lega)."
            )

        self.target_qty = int(context.args[0].strip())
        self._save_target_config()
        self._ensure_snipers_running()

        if self.target_qty == 1:
            msg = (
                f"⚡ **Pre-Fed Quantity Set to 1!**\n"
                f"🎯 Target: **Rs.{self.target_denom} Wallet Code x 1**\n"
                "🚀 Ab bot '+' button bilkul nahi chhedega—stock dikhte hi seedha `Pay via wallet` thokega!"
            )
        else:
            msg = (
                f"➕ **Pre-Fed Quantity Set to 2!**\n"
                f"🎯 Target: **Rs.{self.target_denom} Wallet Code x 2**\n"
                "🚀 Stock dikhte hi '+' daba kar Qty 2 buy karega (aur balance kam hone par turant Qty 1 lega)!"
            )
        await update.message.reply_text(msg)

    # =========================================================
    # START / HELP / STATUS / HEAL
    # =========================================================

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        auto_started = self._ensure_snipers_running()
        extra_msg = f"\n🚀 Auto-resumed {auto_started} saved sessions!" if auto_started > 0 else ""
        await update.message.reply_text(
            "🤖 Rooter Auto Buyer (Argon Swarm Ultra Pro)\n\n"
            f"🎯 Target Denomination: Rs.{self.target_denom} Wallet Code\n"
            f"📦 Pre-Fed Quantity: {self.target_qty}\n"
            f"🎰 Total Slots: {settings.total_slots} (Service {settings.current_service_id})\n"
            f"{extra_msg}\n\n"
            "Use /denom 2000 to change target code & /qty 1 or /qty 2 to pre-feed quantity."
        )

    async def handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📖 ARGON SWARM ULTRA PRO COMMANDS\n\n"
            "🎯 **Pre-Feed Target & Quantity:**\n"
            "/denom 2000 - Set target code (1000/2000/3000/4000/5000)\n"
            "/qty 1 - Set Qty 1 (No '+' click, direct fast buy)\n"
            "/qty 2 - Set Qty 2 (Clicks '+', smart fallback to 1)\n\n"
            "🔐 **Account & Sniper Controls:**\n"
            "/status - Live status, target, balance & stage\n"
            "/cookie S1-A1 <token> - Instant Token Login\n"
            "/login S1-A1 9876543210 - Human Login with Phone\n"
            "/otp S1-A1 123456 - Submit OTP\n"
            "/popup S1-A1 - Force dismiss location popup\n"
            "/screen S1-A1 - Live page screenshot\n"
            "/refresh S1-A1 - Hard refresh & start sniper\n"
            "/trigger_buy - Wake up ALL accounts & attack stock NOW\n"
            "/sniper all - Start sniper on all logged-in slots\n"
            "/stop_sniper all - Pause sniper without logging out\n"
            "/heal - Clean RAM & restart browser tabs safely\n"
            "/logout S1-A1 - Logout & delete saved session"
        )

    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._ensure_snipers_running()
        qty_desc = "1 (Direct Fast Buy)" if self.target_qty == 1 else "2 (Auto Fallback -> 1)"
        lines = [
            "📊 SWARM ACCOUNT STATUS",
            f"🎯 Target Code: Rs.{self.target_denom} Wallet Code",
            f"📦 Pre-Fed Qty: {qty_desc}",
            "",
        ]

        for account in self.all_accounts():
            phone = account.phone_number if account.phone_number else "Saved Session"
            login_state = "✅ LOGGED IN" if account.logged_in else "❌ NOT LOGGED IN"
            task = self.sniper_tasks.get(account.slot_id)
            sniper_state = "🔫 SNIPER: ON" if (task and not task.done()) else "🔫 SNIPER: OFF"
            balance = f"₹{account.wallet_balance:.2f}" if account.wallet_balance is not None else "-"

            lines.append(f"🆔 {account.slot_id} | {login_state} | {sniper_state}")
            if account.logged_in:
                lines.append(f"  📱 ID/Num: {phone} | Bal: {balance}")

            lines.append(f"  📍 State: {account.current_page}")

            if account.last_error:
                lines.append(f"  ⚠️ Note: {account.last_error}")

            lines.append("")

        await update.message.reply_text("\n".join(lines))

    async def handle_heal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🧹 Healing browser & cleaning RAM for all active slots...")
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
        await update.message.reply_text(f"✅ RAM cleaned & {healed} slots refreshed! All snipers running.")

    # =========================================================
    # INSTANT COOKIE / SHORT TOKEN IMPORT (UNIQUE DEVICE ISOLATION)
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

    async def handle_cookie_import(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
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
                    return await update.message.reply_text("❌ Caption mein `/cookie S1-A1` likh kar file bhej bhai!")

                tg_file = await context.bot.get_file(update.message.document.file_id)
                file_bytes = await tg_file.download_as_bytearray()
                raw_input = file_bytes.decode("utf-8").strip()
            else:
                if not context.args or len(context.args) < 2:
                    return await update.message.reply_text(
                        "❌ Format:\n`/cookie S1-A1 <user_auth_value>`\n"
                        "Ya fir `.json` file attach karke caption mein `/cookie S1-A1` likh!"
                    )
                slot_id = context.args[0].upper()
                raw_input = " ".join(context.args[1:]).strip()

            account = self.get_account(slot_id)
            await update.message.reply_text(f"⏳ Injecting isolated device session into {slot_id}...")

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
            await asyncio.sleep(1.0)
            balance_txt = await self._update_live_balance(page, account)
            await self.browser.save_session(slot_id, user_label)

            account.update(
                phone_number=user_label,
                logged_in=True,
                active=True,
                current_page="✅ Token Active & Verified",
                last_error=None,
            )
            log_step(slot_id, f"Cookie session live-loaded ({user_label} | DevID: {extracted_device_id[:8]}) | {balance_txt}")

            self.sniper_tasks[slot_id] = asyncio.create_task(self._sniper_worker(slot_id))

            await update.message.reply_text(
                f"✅ Success! {slot_id} Live Logged-In ({user_label})!\n"
                f"📱 Isolated Device ID: `{extracted_device_id[:12]}...`\n"
                f"💰 Wallet: {balance_txt}\n"
                f"🎯 Target: Rs.{self.target_denom} x {self.target_qty}\n"
                f"🔫 Swarm Sniper Auto-Started!"
            )
        except Exception as exc:
            log_step("COOKIE_ERR", f"Failed: {exc}")
            await update.message.reply_text(f"❌ Cookie import failed: {exc}")

    # =========================================================
    # MANUAL & AUTO LOCATION POPUP HANDLER
    # =========================================================

    async def handle_force_popup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            if not context.args:
                return await update.message.reply_text("❌ Format:\n/popup S1-A1")
            slot_id = context.args[0].upper()

            page = await self.browser.get_page(slot_id)
            if not page.url or page.url == "about:blank":
                return await update.message.reply_text(f"❌ {slot_id} ka browser page active nahi hai.")

            await self._destroy_location_popup(page, slot_id)
            await self.browser.save_session(slot_id, self.get_account(slot_id).phone_number)

            await update.message.reply_text(
                f"✅ 'No, stay on this site' clicked & Indian region locked for {slot_id}!\n"
                f"Ab `/screen {slot_id}` check kar."
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ Force popup failed: {exc}")

    async def _destroy_location_popup(self, page, slot_id: str = "SLOT"):
        try:
            await self.browser.handle_location_popup(page, slot_id)
        except Exception:
            pass

        try:
            await page.evaluate("""() => {
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
                await page.goto(settings.product_url, wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(1.0)
                await self.browser.handle_location_popup(page, slot_id)
        except Exception as e:
            log_step(slot_id, f"URL check note: {e}")

    async def _update_live_balance(self, page, account: AccountState) -> str:
        wallet_balance_text = "Balance info not found"
        try:
            balance_locator = page.locator("text=/Wallet Balance/i >> visible=true").first
            if await balance_locator.is_visible(timeout=1200):
                wallet_balance_text = await balance_locator.inner_text()
                nums = re.findall(r"\d+(?:\.\d+)?", wallet_balance_text.replace(",", ""))
                if nums:
                    account.update(wallet_balance=float(nums[0]))
        except Exception:
            pass
        return wallet_balance_text

    # =========================================================
    # HUMAN LOGIN & OTP HELPERS
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
        await asyncio.sleep(0.8)

        mobile_input = await self._find_mobile_input(page)
        if mobile_input is not None:
            return mobile_input

        try:
            icon_info = await page.evaluate("""() => {
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
                await asyncio.sleep(2.0)

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
                    await asyncio.sleep(1.8)
                    mobile_input = await self._find_mobile_input(page)
                    if mobile_input is not None:
                        return mobile_input
            except Exception:
                pass

        for coord_x in [1208, 1143, 1225]:
            try:
                await page.mouse.move(coord_x, 36, steps=5)
                await page.mouse.click(coord_x, 36)
                await asyncio.sleep(1.5)
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
                    await asyncio.sleep(1.8)
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

    # =========================================================
    # LOGIN & OTP & SCREEN & LOGOUT
    # =========================================================

    async def handle_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        slot_id = "UNKNOWN"
        try:
            args = context.args
            if not args or len(args) < 2:
                return await update.message.reply_text("❌ Format:\n/login S1-A1 9876543210")
            slot_id, phone_number = args[0].upper(), args[1].strip()

            account = self.get_account(slot_id)
            if account.logged_in:
                return await update.message.reply_text(
                    f"⚠️ {slot_id} pehle se logged in hai. Naya login karne ke liye pehle `/logout {slot_id}` bhej."
                )
            if not phone_number.isdigit() or len(phone_number) != 10:
                return await update.message.reply_text("❌ Valid 10-digit mobile number daal bhai.")

            old_task = self.sniper_tasks.pop(slot_id, None)
            if old_task and not old_task.done():
                old_task.cancel()

            log_step(slot_id, f"Starting human login flow for phone {phone_number}...")
            await update.message.reply_text(f"🔍 Opening product page for {slot_id} (Stealth Mode)...")

            page = await self.browser.open_product_page(slot_id)
            await asyncio.sleep(1.5)
            await self._destroy_location_popup(page, slot_id)

            account.update(phone_number=phone_number, active=True, current_page="🔐 Opening Login Popup", last_error=None)

            mobile_input = await self._open_login_popup(page, slot_id)
            if mobile_input is None:
                log_step(slot_id, "ERROR: Login popup did not open!")
                screenshot_path = self.screenshots_dir / f"{slot_id}_login_error.png"
                await page.screenshot(path=str(screenshot_path), full_page=False)
                await self.telegram.send_photo(photo_path=str(screenshot_path), caption="❌ Login popup nahi khula.")
                if screenshot_path.exists():
                    screenshot_path.unlink()
                raise RuntimeError("Login popup not opened.")

            await mobile_input.click(force=True)
            await asyncio.sleep(0.3)
            await mobile_input.fill("")
            await page.keyboard.type(phone_number, delay=65)
            await asyncio.sleep(0.8)

            send_otp_button = await self._find_send_otp_button(page)
            if send_otp_button is None:
                log_step(slot_id, "ERROR: Continue button not found!")
                error_ss = self.screenshots_dir / f"{slot_id}_continue_btn_err.png"
                await page.screenshot(path=str(error_ss), full_page=False)
                await self.telegram.send_photo(photo_path=str(error_ss), caption="❌ Continue button nahi mila.")
                if error_ss.exists():
                    error_ss.unlink()
                raise RuntimeError("Continue button not found.")

            await send_otp_button.evaluate(
                "(el) => { el.removeAttribute('disabled'); el.removeAttribute('data-disabled'); el.disabled = false; }"
            )
            await send_otp_button.click(force=True, timeout=3000)

            otp_ready = False
            for _ in range(35):
                await asyncio.sleep(0.5)
                if await self._find_otp_inputs(page, phone_number=phone_number):
                    otp_ready = True
                    break

            if not otp_ready:
                log_step(slot_id, "ERROR: OTP boxes did not appear!")
                error_ss = self.screenshots_dir / f"{slot_id}_otp_popup_err.png"
                await page.screenshot(path=str(error_ss), full_page=False)
                await self.telegram.send_photo(photo_path=str(error_ss), caption="❌ OTP box nahi aaya.")
                if error_ss.exists():
                    error_ss.unlink()
                raise RuntimeError("OTP popup did not appear.")

            log_step(slot_id, "SUCCESS: OTP screen is open!")
            account.update(current_page="📩 OTP Sent. Waiting...", last_error=None)
            await update.message.reply_text(f"📩 OTP sent for {slot_id}.\nAb bhej: `/otp {slot_id} 123456`")

        except Exception as exc:
            log_step(slot_id, f"Login failed: {exc}")
            try:
                account.update(last_error=str(exc), active=False)
            except Exception:
                pass
            await update.message.reply_text(f"❌ Login start failed: {exc}")

    async def handle_otp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        slot_id = "UNKNOWN"
        try:
            args = context.args
            if not args or len(args) < 2:
                return await update.message.reply_text("❌ Format:\n/otp S1-A1 123456")
            slot_id, otp = args[0].upper(), args[1].strip()

            account = self.get_account(slot_id)
            if len(otp) not in [4, 6, 8] or not otp.isdigit():
                return await update.message.reply_text("❌ OTP 4, 6 ya 8 digit ka number hona chahiye.")

            page = await self.browser.get_page(slot_id)

            otp_inputs = []
            for _ in range(20):
                otp_inputs = await self._find_otp_inputs(page, phone_number=account.phone_number or "")
                if otp_inputs:
                    break
                await asyncio.sleep(0.5)

            if not otp_inputs:
                raise RuntimeError("OTP fields page par nahi mile.")

            if len(otp_inputs) == 1:
                await otp_inputs[0].click(force=True)
                await asyncio.sleep(0.2)
                await otp_inputs[0].fill("")
                await page.keyboard.type(otp, delay=65)
            else:
                boxes = otp_inputs[: len(otp)]
                for i, digit in enumerate(otp):
                    if i < len(boxes):
                        await boxes[i].click(force=True)
                        await page.keyboard.press(digit)
                        await asyncio.sleep(0.08)

            await asyncio.sleep(0.8)

            verify_button = await self._find_verify_button(page)
            if verify_button is None:
                raise RuntimeError("Verify button nahi mila.")

            await verify_button.evaluate(
                "(el) => { el.removeAttribute('disabled'); el.removeAttribute('data-disabled'); el.disabled = false; }"
            )
            await verify_button.click(force=True, timeout=3000)

            ctx = await self.browser.get_context(slot_id)

            for _ in range(20):
                await asyncio.sleep(0.6)
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

            await asyncio.sleep(3.0)

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
                    await asyncio.sleep(1.5)
                except Exception:
                    pass

            await self._destroy_location_popup(page, slot_id)
            balance_txt = await self._update_live_balance(page, account)

            await self.browser.save_session(slot_id, account.phone_number)

            account.update(logged_in=True, active=True, current_page="✅ Logged In & Saved", last_error=None)
            log_step(slot_id, f"Login successful & saved! ({balance_txt})")
            await update.message.reply_text(
                f"✅ Login successful & Session Saved for {slot_id}!\n💰 {balance_txt}"
            )

            task = self.sniper_tasks.get(slot_id)
            if task is None or task.done():
                self.sniper_tasks[slot_id] = asyncio.create_task(self._sniper_worker(slot_id))
                await update.message.reply_text(f"🔫 Sniper AUTO-STARTED for {slot_id}!")

        except Exception as exc:
            log_step(slot_id, f"OTP failed: {exc}")
            try:
                account.update(logged_in=False, last_error=str(exc))
            except Exception:
                pass
            await update.message.reply_text(f"❌ OTP failed: {exc}")

    async def handle_screen(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            if not context.args:
                return await update.message.reply_text("❌ Format:\n/screen S1-A1")
            slot_id = context.args[0].upper()
            account = self.get_account(slot_id)

            page = await self.browser.get_page(slot_id)
            if not page.url or page.url == "about:blank":
                page = await self.browser.open_product_page(slot_id)
                await asyncio.sleep(1.0)

            await self._destroy_location_popup(page, slot_id)

            screenshot_path = self.screenshots_dir / f"{slot_id}.png"
            await page.screenshot(path=str(screenshot_path), full_page=False)
            await self.telegram.send_photo(
                photo_path=str(screenshot_path),
                caption=(
                    f"🖥 {slot_id} | Target: Rs.{self.target_denom} x {self.target_qty}\n"
                    f"State: {account.current_page}"
                ),
            )
            if screenshot_path.exists():
                screenshot_path.unlink()
        except Exception as exc:
            try:
                account.update(last_error=str(exc))
            except Exception:
                pass
            await update.message.reply_text(f"❌ Screenshot failed: {exc}")

    async def handle_refresh(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            if not context.args:
                return await update.message.reply_text("❌ Format:\n/refresh S1-A1")
            slot_id = context.args[0].upper()
            account = self.get_account(slot_id)

            page = await self.browser.reset_slot_page(slot_id)
            cb = int(time.time())
            fresh_url = (
                f"{settings.product_url}?cb={cb}"
                if "?" not in settings.product_url
                else f"{settings.product_url}&cb={cb}"
            )
            await page.goto(fresh_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(1.2)
            await self._destroy_location_popup(page, slot_id)
            await self.browser.save_session(slot_id, account.phone_number)

            account.update(logged_in=True, active=True, current_page="🔄 Manually Refreshed", last_error=None)
            await update.message.reply_text(f"🔄 Hard Refreshed {slot_id} (Target: Rs.{self.target_denom} x {self.target_qty})")

            task = self.sniper_tasks.get(slot_id)
            if task is None or task.done():
                self.sniper_tasks[slot_id] = asyncio.create_task(self._sniper_worker(slot_id))
                await update.message.reply_text(f"🔫 Sniper AUTO-STARTED for {slot_id}!")

        except Exception as exc:
            try:
                account.update(last_error=str(exc))
            except Exception:
                pass
            await update.message.reply_text(f"❌ Refresh failed: {exc}")

    async def handle_logout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            if not context.args:
                return await update.message.reply_text("❌ Format:\n/logout S1-A1")
            slot_id = context.args[0].upper()
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
            await update.message.reply_text(
                f"🚪 Successfully logged out & RAM freed for {slot_id}. Slot is now fresh!"
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ Logout failed: {exc}")

    # =========================================================
    # 🎯 FAST TARGET SELECTOR & ORDER PAGE CODE EXTRACTOR
    # =========================================================

    async def _select_target_denomination(self, page, denom: str) -> str:
        """
        Clicks 'Rs.<denom> Wallet Code' as soon as it is rendered on the page.
        Returns:
          - 'clicked' if in-stock card was clicked
          - 'oos' if the target card explicitly says 'Out of Stock'
          - 'not_found' if card didn't render
        """
        target_label = f"Rs.{denom} Wallet Code"
        for _ in range(35):
            res = await page.evaluate(
                """(label) => {
                    const lowerLabel = label.toLowerCase();
                    const els = Array.from(document.querySelectorAll('div, button, span, p'));
                    for (const el of els) {
                        const t = (el.innerText || '').trim();
                        const tl = t.toLowerCase();
                        if (tl === lowerLabel || tl.startsWith(lowerLabel + '\\n')) {
                            const card = el.parentElement || el;
                            const cardTxt = (card.innerText || t).toLowerCase();
                            if (cardTxt.includes('out of stock')) {
                                return 'oos';
                            }
                            if (cardTxt.includes('₹') || cardTxt.includes('rs.')) {
                                el.click();
                                card.click();
                                return 'clicked';
                            }
                        }
                    }
                    return 'waiting';
                }""",
                target_label,
            )
            if res in ("clicked", "oos"):
                return res
            await asyncio.sleep(0.08)

        # Fallback Playwright locator click if DOM structure differed
        loc = page.locator(f"text={target_label} >> visible=true")
        if await loc.count() > 0:
            try:
                card_txt = (await loc.first.evaluate("(el) => (el.parentElement ? el.parentElement.innerText : el.innerText)")).lower()
                if "out of stock" in card_txt:
                    return "oos"
                await loc.first.click(force=True, timeout=2000)
                await loc.first.evaluate("(el) => { if(el.parentElement) el.parentElement.click(); }")
                return "clicked"
            except Exception:
                pass
        return "not_found"

    async def _adjust_quantity_on_page(self, page, desired_qty: int, current_qty: int) -> int:
        """
        - If desired_qty == 1 and current_qty == 1: does NOTHING (0ms overhead!).
        - If desired_qty == 2 and current_qty == 1: clicks '+' once.
        - If desired_qty == 1 and current_qty == 2: clicks '-' once to drop back to 1 without reloading!
        """
        if desired_qty == current_qty:
            return current_qty

        symbol = "+" if desired_qty == 2 else "-"
        clicked = await page.evaluate(
            """(sym) => {
                const all = Array.from(document.querySelectorAll('button, span, div'));
                for (const el of all) {
                    const txt = (el.innerText || '').trim();
                    if ((txt === sym || (sym === '-' && txt === '−')) && el.offsetParent !== null) {
                        el.click();
                        if (el.parentElement) el.parentElement.click();
                        return true;
                    }
                }
                return false;
            }""",
            symbol,
        )
        if clicked:
            await asyncio.sleep(0.25)
            return desired_qty
        return current_qty

    async def _verify_order_page_and_extract_codes(
        self, page, timeout_sec: float = 16.0
    ) -> Tuple[bool, str, List[str]]:
        """
        Strictly verifies that the browser has reached Rooter's Payment/Order Confirmation page
        (matching #MO_..., 'items delivered successfully', 'Coupon Codes' -> 'Code #01')
        and extracts the gift codes.
        Returns: (is_verified_order, order_id, list_of_codes)
        """
        deadline = time.monotonic() + timeout_sec
        order_page_reached = False
        order_id = "Confirmed"
        extracted_codes: List[str] = []

        while time.monotonic() < deadline:
            try:
                data = await page.evaluate("""() => {
                    const body = (document.body && document.body.innerText) ? document.body.innerText : '';
                    const lower = body.toLowerCase();
                    const url = (window.location.href || '').toLowerCase();

                    // Dismiss floating promo/sale banner ('X' button on right side) so screenshot is clean
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

                    const orderMatch = body.match(/#?(MO_\\d+_\\d+)/i);
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

                    const codes = [];
                    const lines = body.split('\\n').map(l => l.trim()).filter(Boolean);

                    // Method 1: Exact Rooter 'Code #01' / 'Code #02' next-line parser (from reference image)
                    for (let i = 0; i < lines.length; i++) {
                        if (/^code\\s*#?\\s*\\d+/i.test(lines[i])) {
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

                    // Method 2: Standard PSN 12-digit hyphenated format (XXXX-XXXX-XXXX)
                    const psnMatches = body.match(/\\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\\b/g) || [];
                    for (const m of psnMatches) {
                        codes.push(m);
                    }

                    const uniqueCodes = Array.from(new Set(codes));
                    return {
                        reached: true,
                        orderId: orderMatch ? '#' + orderMatch[1].replace(/^#/, '') : 'Confirmed',
                        codes: uniqueCodes,
                        delivered: lower.includes('delivered successfully')
                    };
                }""")

                if data and data.get("reached"):
                    order_page_reached = True
                    order_id = data.get("orderId") or order_id
                    extracted_codes = data.get("codes") or []
                    # As soon as codes are visible on screen, wait 0.4s for crisp render & return!
                    if extracted_codes:
                        await asyncio.sleep(0.4)
                        return True, order_id, extracted_codes
            except Exception:
                pass

            await asyncio.sleep(0.35)

        return order_page_reached, order_id, extracted_codes

    # =========================================================
    # 🎯 SWARM ATTACK SNIPER BACKGROUND WORKER
    # =========================================================

    async def _sniper_worker(self, slot_id: str):
        account = self.get_account(slot_id)

        try:
            slot_num = int(slot_id.split("-A")[-1])
            await asyncio.sleep((slot_num - 1) * 3)
        except Exception:
            pass

        log_step(slot_id, f"Swarm Sniper Worker started! (Target: Rs.{self.target_denom} x {self.target_qty})")
        await self.telegram.send_message(
            f"🎯 Swarm Sniper ACTIVATED for {slot_id} | Target: Rs.{self.target_denom} | Qty: {self.target_qty}"
        )

        cycle_count = 0

        while account.logged_in:
            try:
                target_denom = self.target_denom
                prefed_qty = self.target_qty

                cycle_count += 1
                if cycle_count % 50 == 0:
                    try:
                        await self.browser.save_session(slot_id, account.phone_number)
                        await self.browser.reset_slot_page(slot_id)
                    except Exception:
                        pass

                last_time = datetime.now().strftime("%I:%M:%S %p")
                account.update(
                    current_page=f"⏳ [Wait 30s | Target: Rs.{target_denom}x{prefed_qty}] ({last_time})",
                    last_error=None,
                )

                try:
                    await asyncio.wait_for(self.stock_event.wait(), timeout=30.0)
                    account.update(current_page=f"⚡ [Swarm Triggered! Attacking Rs.{target_denom}!]")
                except asyncio.TimeoutError:
                    pass

                if not account.logged_in:
                    break

                # Re-read latest target_denom & target_qty in case user updated them during wait
                target_denom = self.target_denom
                prefed_qty = self.target_qty

                page = await self.browser.get_page(slot_id)

                account.update(current_page="🔄 [STAGE: Hard Refreshing Page]")
                cache_buster = int(time.time() * 1000)
                fresh_url = (
                    f"{settings.product_url}?cb={cache_buster}"
                    if "?" not in settings.product_url
                    else f"{settings.product_url}&cb={cache_buster}"
                )

                await page.goto(fresh_url, wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(0.5)
                await self._destroy_location_popup(page, slot_id)

                content = await page.content()
                if "Application error: a client-side exception" in content:
                    account.update(last_error="Website White Screen - Auto Retrying")
                    await asyncio.sleep(2.5)
                    continue

                account.update(current_page=f"🎯 [STAGE: Selecting Rs.{target_denom}]")
                sel_result = await self._select_target_denomination(page, target_denom)

                if sel_result == "oos":
                    check_t = datetime.now().strftime("%I:%M:%S %p")
                    account.update(current_page=f"😴 [Rs.{target_denom} Out of Stock] ({check_t})")
                    await asyncio.sleep(1.0)
                    continue

                await asyncio.sleep(0.35)
                await self._update_live_balance(page, account)

                account.update(current_page="🔍 [STAGE: Checking for Pay Button]")
                pay_btn_1 = page.locator("button >> text='Pay via wallet' >> visible=true").first
                is_in_stock = False
                try:
                    is_in_stock = await pay_btn_1.is_visible(timeout=1800)
                except Exception:
                    pass

                if not is_in_stock:
                    check_t = datetime.now().strftime("%I:%M:%S %p")
                    account.update(current_page=f"😴 [Rs.{target_denom} Out of Stock / Low Bal] ({check_t})")
                    await asyncio.sleep(1.0)
                    continue

                log_step(slot_id, f"🚨 Rs.{target_denom} STOCK DETECTED! Initiating instant buy (Pre-fed Qty: {prefed_qty})!")
                account.update(current_page=f"🚨 [BUYING Rs.{target_denom} x {prefed_qty}!]")

                if not self.stock_event.is_set():
                    self.stock_event.set()
                    asyncio.create_task(self._clear_stock_event_delayed())

                asyncio.create_task(
                    self.telegram.send_message(
                        f"🚨 **Rs.{target_denom} STOCK DETECTED in {slot_id}!**\n"
                        f"⚡ Waking all accounts | Target: Rs.{target_denom} | Qty: {prefed_qty}"
                    )
                )

                # If pre-fed Qty is 1 -> only [1] (never touches '+').
                # If pre-fed Qty is 2 -> tries [2, 1] (tries Qty 2 first, falls back to Qty 1 on same page).
                qty_attempts = [1] if prefed_qty == 1 else [2, 1]
                current_page_qty = 1
                purchase_completed = False

                for attempt_qty in qty_attempts:
                    try:
                        # If we are still on the product page, adjust quantity without reloading!
                        if "playstation-gift-cards" not in (page.url or "").lower():
                            await page.goto(fresh_url, wait_until="domcontentloaded", timeout=25000)
                            await asyncio.sleep(0.5)
                            await self._destroy_location_popup(page, slot_id)
                            await self._select_target_denomination(page, target_denom)
                            current_page_qty = 1

                        if attempt_qty != current_page_qty:
                            account.update(current_page=f"🔢 [STAGE: Setting Quantity to {attempt_qty}]")
                            current_page_qty = await self._adjust_quantity_on_page(
                                page, desired_qty=attempt_qty, current_qty=current_page_qty
                            )

                        # Verify 'Pay via wallet' is visible for this quantity (checks wallet balance dynamically!)
                        pay_btn_1 = page.locator("button >> text='Pay via wallet' >> visible=true").first
                        can_pay_wallet = False
                        try:
                            can_pay_wallet = await pay_btn_1.is_visible(timeout=1500)
                        except Exception:
                            pass

                        if not can_pay_wallet:
                            if attempt_qty == 2:
                                log_step(slot_id, "Wallet insufficient for Qty 2, switching to Qty 1 immediately...")
                                asyncio.create_task(
                                    self.telegram.send_message(
                                        f"⚠️ {slot_id}: Qty 2 ke liye wallet balance kam hai, turant Qty 1 buy kar raha hu!"
                                    )
                                )
                                continue
                            else:
                                account.update(current_page="🛑 [Stopped - Insufficient Balance for Qty 1]")
                                asyncio.create_task(
                                    self.telegram.send_message(
                                        f"🛑 {slot_id}: Rs.{target_denom} x 1 ke liye wallet balance kam hai."
                                    )
                                )
                                break

                        # STEP 1: Click First 'Pay via wallet' immediately (NO pre-buy screenshot delay!)
                        account.update(current_page=f"💳 [STAGE: Clicking Pay via Wallet (Qty {attempt_qty})]")
                        await pay_btn_1.click(force=True, timeout=2500)
                        await pay_btn_1.evaluate("(el) => el.click()")

                        # STEP 2: Wait for 'Delivery Details' popup & click Final 'Pay via wallet'
                        account.update(current_page="📝 [STAGE: Confirming Delivery Popup]")
                        await page.locator("text=Delivery Details >> visible=true").wait_for(
                            state="visible", timeout=5000
                        )
                        await asyncio.sleep(0.25)

                        account.update(current_page="💳 [STAGE: Clicking Final Pay via Wallet]")
                        pay_btn_2 = page.locator("button >> text='Pay via wallet' >> visible=true").last
                        await pay_btn_2.wait_for(state="visible", timeout=4000)
                        await pay_btn_2.click(force=True, timeout=2500)
                        await pay_btn_2.evaluate("(el) => el.click()")

                        # STEP 3: STRICT ORDER PAGE & CODE VERIFICATION (No fake success messages!)
                        account.update(current_page="⏳ [STAGE: Verifying Payment Page & Extracting Code...]")
                        verified, order_id, codes = await self._verify_order_page_and_extract_codes(
                            page, timeout_sec=16.0
                        )

                        if not verified:
                            log_step(slot_id, f"Payment page not reached on Qty {attempt_qty}, retrying without fake success...")
                            account.update(last_error=f"Order page not reached (Qty {attempt_qty}), retrying...")
                            continue

                        # VERIFIED ON PAYMENT / ORDER PAGE! Take 1 clean screenshot & send everything in 1 message!
                        purchase_completed = True
                        current_time = datetime.now().strftime("%I:%M:%S %p, %d %b %Y")
                        account.update(
                            current_page=f"✅ [BOUGHT Rs.{target_denom}x{attempt_qty} | {order_id}]",
                            last_error=None,
                        )
                        log_step(slot_id, f"✅ VERIFIED ORDER {order_id} | Codes: {codes}")

                        if codes:
                            codes_text = "\n".join(
                                f"🎟 **Code #{idx:02d}:** `{code}`"
                                for idx, code in enumerate(codes, start=1)
                            )
                        else:
                            codes_text = "🎟 **Code:** Screenshot mein `Coupon Codes` ke niche dekho भाई!"

                        success_caption = (
                            f"✅✅ **SWARM SUCCESS: CODE PURCHASED!**\n\n"
                            f"🆔 **Order ID:** `{order_id}`\n"
                            f"🎮 **Product:** Rs.{target_denom} Wallet Code\n"
                            f"📦 **Quantity:** {attempt_qty}\n"
                            f"👤 **Slot:** {slot_id} ({account.phone_number or 'Active'})\n"
                            f"{codes_text}\n"
                            f"🕒 **Time:** {current_time}"
                        )

                        shot_path = self.screenshots_dir / f"{slot_id}_order_success_{int(time.time())}.png"
                        try:
                            await page.screenshot(path=str(shot_path), full_page=False)
                            await self.telegram.send_photo(
                                photo_path=str(shot_path),
                                caption=success_caption,
                            )
                        except Exception as ss_err:
                            log_step(slot_id, f"Photo send fallback note: {ss_err}")
                            await self.telegram.send_message(success_caption)
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
                        account.update(last_error=f"Retry Note (Qty {attempt_qty}): {str(flow_err)[:40]}")
                        # No fake success message—quietly continue to next attempt or next loop retry!
                        continue

                if purchase_completed:
                    # Wait briefly after a successful purchase before returning to product page monitoring
                    await asyncio.sleep(8.0)
                else:
                    # Fast retry if stuck or not bought yet
                    await asyncio.sleep(1.5)

            except asyncio.CancelledError:
                account.update(current_page="⏸️ Sniper Paused")
                break
            except Exception as loop_err:
                err_msg = str(loop_err)
                log_step(slot_id, f"Sniper loop error: {err_msg[:60]}")
                account.update(
                    current_page="🛠️ [STAGE: Auto-Healing Tab...]",
                    last_error=f"Self-Healed: {err_msg[:45]}",
                )
                try:
                    await self.browser.reset_slot_page(slot_id)
                except Exception:
                    pass
                await asyncio.sleep(2.5)

        self.sniper_tasks.pop(slot_id, None)

    async def _clear_stock_event_delayed(self):
        await asyncio.sleep(3.0)
        self.stock_event.clear()

    # =========================================================
    # START / STOP SNIPER & MANUAL TRIGGER
    # =========================================================

    async def handle_sniper_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            args = context.args
            target = args[0].lower() if args else "all"

            if target == "all":
                started = self._ensure_snipers_running()
                await update.message.reply_text(
                    f"🔫 Swarm Sniper active/started! ({started} newly launched)\n"
                    f"🎯 Target: Rs.{self.target_denom} Wallet Code | Qty: {self.target_qty}"
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
                        f"🔫 Sniper started for {slot_id} (Target: Rs.{self.target_denom} x {self.target_qty})"
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
            f"🚨 SWARM TRIGGER SENT! Waking up ALL accounts to attack **Rs.{self.target_denom} x {self.target_qty}** NOW!"
        )
        await self.trigger_stock_buy()
