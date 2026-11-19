from pathlib import Path
from typing import Awaitable, Callable, Optional

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import settings

CommandCallback = Callable[
    [Update, ContextTypes.DEFAULT_TYPE],
    Awaitable[None],
]


class TelegramBot:
    def __init__(self):
        self.application: Optional[Application] = None
        self.callbacks: dict[str, CommandCallback] = {}
        self.callback_query_handler: Optional[CommandCallback] = None
        self.text_input_handler: Optional[CommandCallback] = None
        self.document_input_handler: Optional[CommandCallback] = None

    def register_command(
        self,
        command: str,
        callback: CommandCallback,
    ):
        self.callbacks[command] = callback
        if self.application is not None:
            self.application.add_handler(
                CommandHandler(
                    command,
                    self._create_handler(command),
                    block=False,
                )
            )

    def register_callback_query(self, callback: CommandCallback):
        self.callback_query_handler = callback
        if self.application is not None:
            self.application.add_handler(
                CallbackQueryHandler(self._handle_callback_query, block=False)
            )

    def register_text_handler(self, callback: CommandCallback):
        self.text_input_handler = callback
        if self.application is not None:
            self.application.add_handler(
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    self._handle_text_input,
                    block=False,
                )
            )

    def register_document_handler(self, callback: CommandCallback):
        self.document_input_handler = callback

    def _create_handler(self, command: str):
        async def handler(
            update: Update,
            context: ContextTypes.DEFAULT_TYPE,
        ):
            callback = self.callbacks.get(command)
            if callback is None:
                return
            await callback(update, context)

        return handler

    async def _handle_callback_query(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if self.callback_query_handler is not None:
            await self.callback_query_handler(update, context)

    async def _handle_text_input(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if self.text_input_handler is not None:
            await self.text_input_handler(update, context)

    async def _document_handler(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Handles both interactive wizard .json uploads AND '/cookie S1-A1' captioned files."""
        if self.document_input_handler is not None:
            handled = await self.document_input_handler(update, context)
            if handled:
                return

        if not update.message or not update.message.caption:
            return
        caption = update.message.caption.strip()
        if caption.lower().startswith("/cookie"):
            parts = caption.split()
            context.args = parts[1:] if len(parts) > 1 else []
            callback = self.callbacks.get("cookie")
            if callback:
                await callback(update, context)

    def build(self):
        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

        self.application = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .concurrent_updates(True)
            .build()
        )

        default_commands = [
            "start",
            "status",
            "denom",
            "target",
            "code",
            "qty",
            "abort",
            "login",
            "otp",
            "cookie",
            "popup",
            "logout",
            "screen",
            "refresh",
            "help",
            "sniper",
            "stop_sniper",
            "trigger_buy",
            "heal",
        ]

        all_cmds = set(default_commands) | set(self.callbacks.keys())

        for command in all_cmds:
            self.application.add_handler(
                CommandHandler(
                    command,
                    self._create_handler(command),
                    block=False,
                )
            )

        # Inline button callback handler for interactive /start wizard
        self.application.add_handler(
            CallbackQueryHandler(
                self._handle_callback_query,
                block=False,
            )
        )

        # Document (.json cookie file) handler
        self.application.add_handler(
            MessageHandler(
                filters.Document.ALL,
                self._document_handler,
                block=False,
            )
        )

        # Plain text handler for interactive wizard inputs (phone / OTP / token)
        self.application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_text_input,
                block=False,
            )
        )

        return self.application

    async def send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ):
        if self.application is None:
            raise RuntimeError("Telegram application is not built")

        target_chat = chat_id or settings.telegram_chat_id
        if not target_chat:
            raise RuntimeError("TELEGRAM_CHAT_ID is not configured")

        try:
            return await self.application.bot.send_message(
                chat_id=target_chat,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        except Exception:
            if parse_mode is not None:
                return await self.application.bot.send_message(
                    chat_id=target_chat,
                    text=text,
                    reply_markup=reply_markup,
                )
            raise

    async def send_photo(
        self,
        photo_path: str,
        caption: Optional[str] = None,
        chat_id: Optional[str] = None,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ):
        if self.application is None:
            raise RuntimeError("Telegram application is not built")

        target_chat = chat_id or settings.telegram_chat_id
        if not target_chat:
            raise RuntimeError("TELEGRAM_CHAT_ID is not configured")

        path = Path(photo_path)
        if not path.exists():
            raise FileNotFoundError(f"Screenshot not found: {photo_path}")

        safe_caption = caption[:1020] if caption else None

        try:
            with path.open("rb") as photo:
                return await self.application.bot.send_photo(
                    chat_id=target_chat,
                    photo=photo,
                    caption=safe_caption,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
        except Exception:
            if parse_mode is not None:
                with path.open("rb") as photo:
                    return await self.application.bot.send_photo(
                        chat_id=target_chat,
                        photo=photo,
                        caption=safe_caption,
                        reply_markup=reply_markup,
                    )
            raise

    async def start(self):
        if self.application is None:
            self.build()

        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(drop_pending_updates=True)

    async def stop(self):
        if self.application is None:
            return

        if self.application.updater.running:
            await self.application.updater.stop()

        await self.application.stop()
        await self.application.shutdown()
