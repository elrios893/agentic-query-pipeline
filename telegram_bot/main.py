"""
telegram_bot/main.py
Aplicación principal del bot de Telegram para Creytex
"""
import logging
import sys
from pathlib import Path

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)

# Setup de paths
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from telegram_bot.config import TELEGRAM_BOT_TOKEN, EMOJIS
from telegram_bot.handlers import TelegramHandlers

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('telegram_bot/bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


def main():
    """Punto de entrada principal — síncrono, run_polling gestiona el loop."""

    logger.info(f"{EMOJIS['loading']} Iniciando bot de Telegram...")
    logger.info(f"Token configurado: {TELEGRAM_BOT_TOKEN[:20]}...")

    # Construir aplicación
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    # Registrar handlers de comandos
    app.add_handler(CommandHandler("start",   TelegramHandlers.start))
    app.add_handler(CommandHandler("ayuda",   TelegramHandlers.ayuda))
    app.add_handler(CommandHandler("help",    TelegramHandlers.ayuda))
    app.add_handler(CommandHandler("sesion",  TelegramHandlers.sesion))
    app.add_handler(CommandHandler("session", TelegramHandlers.sesion))
    app.add_handler(CommandHandler("limpiar", TelegramHandlers.limpiar))
    app.add_handler(CommandHandler("clear",   TelegramHandlers.limpiar))
    app.add_handler(CommandHandler("analisis", TelegramHandlers.analisis))

    # Handler para mensajes de texto
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, TelegramHandlers.procesar_mensaje)
    )

    # Handler para botones de descarga
    app.add_handler(
        CallbackQueryHandler(TelegramHandlers.descargar_archivo, pattern="^download_")
    )

    # Registrar comandos visibles en el menú de Telegram
    async def post_init(application: Application) -> None:
        await application.bot.set_my_commands([
            ("start",    "Iniciar el bot"),
            ("ayuda",    "Mostrar ayuda"),
            ("analisis", "Activar agente analista"),
            ("sesion",   "Ver informacion de sesion"),
            ("limpiar",  "Limpiar archivos temporales"),
        ])
        logger.info(f"{EMOJIS['success']} Comandos del menu registrados en Telegram")

    app.post_init = post_init

    logger.info(f"{EMOJIS['success']} Bot listo — presiona Ctrl+C para detener")

    # run_polling es SÍNCRONO en python-telegram-bot v20+;
    # crea y gestiona su propio event loop internamente.
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
