"""
telegram_bot/handlers.py
Manejadores de eventos del bot de Telegram
"""
import logging
import re
import time
from pathlib import Path
from typing import List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler

from telegram_bot.config import TELEGRAM_BOT_TOKEN, SERVER_URL, EMOJIS, TEMP_DIR
from telegram_bot.session_manager import session_manager
from telegram_bot.api_client import APIClient
from telegram_bot.formatters import MessageFormatter, md_a_telegram

logger = logging.getLogger(__name__)

# Raíz del proyecto (un nivel arriba de telegram_bot/)
BASE_DIR = Path(__file__).resolve().parent.parent

# Cliente API global
api_client = APIClient(SERVER_URL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extraer_imagenes(respuesta: str, imagenes_campo: list) -> List[Tuple[str, Path]]:
    """
    Extrae pares (caption, ruta_absoluta) de imágenes a enviar.

    El servidor devuelve imágenes en dos lugares:
    - campo 'imagenes': lista de strings "![titulo](reports\\charts\\archivo.png)"
    - campo 'respuesta': el texto puede contener los mismos ![...](...)

    Ambos usan rutas relativas al BASE_DIR del proyecto.
    Deduplicamos por nombre de archivo para no enviar dos veces la misma imagen.
    """
    vistos = set()
    resultado = []

    # Regex que captura ![titulo](ruta)
    patron = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

    def procesar(titulo: str, ruta_str: str):
        # Normalizar separadores de ruta
        ruta_str = ruta_str.replace('\\', '/')
        ruta_abs = BASE_DIR / ruta_str
        nombre = ruta_abs.name
        if nombre not in vistos and ruta_abs.exists():
            vistos.add(nombre)
            resultado.append((titulo or 'Gráfico generado', ruta_abs))

    # 1. Parsear el campo imagenes[]
    for item in (imagenes_campo or []):
        m = patron.search(item)
        if m:
            procesar(m.group(1), m.group(2))
        else:
            # Si es una ruta directa (sin formato markdown)
            ruta_abs = BASE_DIR / item.replace('\\', '/')
            if ruta_abs.exists() and ruta_abs.name not in vistos:
                vistos.add(ruta_abs.name)
                resultado.append(('Gráfico generado', ruta_abs))

    # 2. Parsear el texto de la respuesta (puede tener imágenes extra, ej: informes)
    for m in patron.finditer(respuesta):
        procesar(m.group(1), m.group(2))

    return resultado


async def _send_safe(update, text: str) -> None:
    """
    Envía un mensaje con ParseMode.MARKDOWN.
    Espera recibir texto ya convertido con md_a_telegram().
    Si Telegram rechaza el parseo, reenvía como texto plano.
    """
    try:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            await update.message.reply_text(
                text,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"Error enviando mensaje (fallback plano): {e}")


async def _enviar_imagenes(update, respuesta: str, imagenes_campo: list) -> None:
    """Envía todas las imágenes encontradas en la respuesta y el campo imagenes."""
    imagenes = _extraer_imagenes(respuesta, imagenes_campo)
    if not imagenes:
        return
    for caption, ruta in imagenes[:5]:  # máximo 5
        try:
            with open(ruta, 'rb') as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=f"{EMOJIS['chart']} {caption}",
                )
        except Exception as e:
            logger.error(f"Error enviando imagen {ruta.name}: {e}")


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------
# Flujo: botones -> callback de calificación -> (comentario opcional | Omitir).
# El comentario se intercepta en procesar_mensaje ANTES de reenviar nada al
# servidor, así que jamás pasa por /chat ni entra al historial de sesión que
# se manda como contexto al LLM.

async def _enviar_prompt_feedback(update, log_id: str) -> None:
    """Ofrece calificar la última respuesta. No hace nada si no hay log_id
    (por ejemplo, si el registro del prompt falló en el servidor)."""
    if not log_id:
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("👍 Bueno",   callback_data=f"fb:bueno:{log_id}"),
        InlineKeyboardButton("😐 Regular", callback_data=f"fb:regular:{log_id}"),
        InlineKeyboardButton("👎 Malo",    callback_data=f"fb:malo:{log_id}"),
    ]])
    await update.message.reply_text(
        "¿Fue útil esta respuesta?",
        reply_markup=keyboard,
    )

class TelegramHandlers:
    """Manejadores de eventos del bot"""
    
    @staticmethod
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /start — inicia la sesión del usuario"""
        user = update.effective_user
        chat_id = update.effective_chat.id
        
        # Crear o actualizar sesión
        sesion = session_manager.obtener_o_crear(
            user_id=user.id,
            chat_id=chat_id,
            username=user.username,
            first_name=user.first_name
        )
        
        mensaje = f"""
{EMOJIS['info']} *¡Bienvenido al Consultor de Ventas Creytex!*

Soy un bot de análisis de datos que puede ayudarte con:

{EMOJIS['chart']} Análisis de ventas por región, tienda, producto
{EMOJIS['graph']} Gráficos y visualizaciones
{EMOJIS['report']} Informes detallados en Word
{EMOJIS['table']} Tablas y comparativas
{EMOJIS['search']} Búsquedas y filtros complejos

*¿Cómo funciona?*
1. Escribe tu pregunta en lenguaje natural
2. Procesaré tu consulta y te daré resultados
3. Puedes descargar gráficos e informes

*Ejemplos de preguntas:*
• "¿Cuántas ventas hubo en Bogotá?"
• "Top 5 departamentos con más ingresos"
• "Genera un informe de ventas del mes"
• "Compara enero y febrero"

Escribe tu pregunta para comenzar {EMOJIS['search']}
        """
        
        await update.message.reply_text(
            mensaje,
            parse_mode=ParseMode.MARKDOWN
        )
    
    @staticmethod
    async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /ayuda — muestra información de ayuda"""
        mensaje = f"""
*{EMOJIS['info']} Guía de Ayuda*

*Comandos disponibles:*
/start - Iniciar el bot
/ayuda - Mostrar esta ayuda
/sesion - Ver info de tu sesión
/limpiar - Limpiar archivos temporales
/reset - Reiniciar conversación a estado inicial

*Ejemplos de consultas:*

{EMOJIS['chart']} *Análisis básico:*
  • Ventas por departamento
  • Top 10 referencias más vendidas
  • ¿Cuál es la talla más vendida?

{EMOJIS['graph']} *Con gráficos:*
  • Graficame las ventas por mes
  • Top 5 tiendas - gráfico de barras
  • Evolución de ventas en junio

{EMOJIS['report']} *Informes:*
  • Genera un informe de ventas
  • Informe detallado para Éxito
  • Reporte mensual

*Formato de respuestas:*
- Las respuestas se envían en formato legible
- Los gráficos se envían como imágenes
- Los informes se pueden descargar

¿Necesitas ayuda con algo específico? Escribe tu pregunta {EMOJIS['search']}
        """
        
        await update.message.reply_text(
            mensaje,
            parse_mode=ParseMode.MARKDOWN
        )
    
    @staticmethod
    async def sesion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /sesion — muestra información de la sesión actual"""
        user_id = update.effective_user.id
        sesion = session_manager.obtener(user_id)
        
        if not sesion:
            await update.message.reply_text(f"{EMOJIS['error']} No hay sesión activa.")
            return
        
        mensaje = f"""
{EMOJIS['info']} *Información de tu sesión*

*Usuario:* {sesion.first_name or sesion.username}
*ID de usuario:* `{sesion.user_id}`
*Chat ID:* `{sesion.chat_id}`
*Turnos procesados:* {sesion.turno}
*Archivos generados:* {len(sesion.archivos_generados)}
*Creada:* {sesion.created_at.strftime('%d/%m/%Y %H:%M')}
        """
        
        if sesion.archivos_generados:
            mensaje += "\n\n*Archivos en esta sesión:*\n"
            for archivo in sesion.archivos_generados[-5:]:  # Últimos 5
                nombre = Path(archivo).name
                mensaje += f"  • {nombre}\n"
        
        await update.message.reply_text(
            mensaje,
            parse_mode=ParseMode.MARKDOWN
        )
    
    @staticmethod
    async def limpiar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /limpiar — limpia archivos temporales"""
        user_id = update.effective_user.id
        session_manager.limpiar_archivos(user_id)

        await update.message.reply_text(
            f"{EMOJIS['success']} Archivos temporales limpiados.",
            parse_mode=ParseMode.MARKDOWN
        )

    @staticmethod
    async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /reset — borra el historial de conversación y reinicia el turno."""
        user = update.effective_user
        session_id = f"telegram_{user.id}"

        # 1. Borrar memoria en el servidor (historial + DataFrames)
        ok_servidor = api_client.reset_sesion(session_id)

        # 2. Resetear sesión local (turno, contexto, archivos)
        session_manager.resetear(user.id)

        # 3. Limpiar datos en memoria de contexto de Telegram
        context.user_data.clear()

        if ok_servidor:
            msg = (
                f"{EMOJIS['success']} *Conversación reiniciada*\n\n"
                f"El historial y la memoria de esta sesión han sido borrados. "
                f"Puedes empezar una consulta nueva."
            )
        else:
            msg = (
                f"{EMOJIS['warning']} *Sesión reiniciada localmente*\n\n"
                f"No se pudo contactar al servidor para borrar el historial remoto, "
                f"pero el estado local fue reseteado."
            )

        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    @staticmethod
    async def analisis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /analisis — activa el agente analista sobre los datos.
        Uso:
          /analisis                → análisis general de ventas
          /analisis por qué cayeron las ventas de caballero
        El orquestador detecta el prefijo '/analisis' y fuerza el agente analista.
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        # Un comando explícito significa que el usuario ya siguió adelante:
        # cualquier feedback pendiente de comentario queda descartado.
        context.user_data.pop('esperando_feedback', None)

        # El texto adicional que el usuario escribió después de /analisis
        prompt_extra = " ".join(context.args).strip() if context.args else ""
        pregunta = f"/analisis {prompt_extra}" if prompt_extra else "/analisis"

        # Obtener o crear sesión
        session_manager.obtener_o_crear(
            user_id=user.id,
            chat_id=chat_id,
            username=user.username,
            first_name=user.first_name,
        )
        session_id = f"telegram_{user.id}"

        mensaje_espera = await update.message.reply_text(
            f"{EMOJIS['loading']} Activando agente analista...\n_(Esto puede tardar un momento)_",
            parse_mode=ParseMode.MARKDOWN,
        )

        try:
            inicio = time.time()
            resultado = api_client.enviar_consulta(session_id, pregunta)
            duracion = time.time() - inicio

            try:
                await mensaje_espera.delete()
            except Exception:
                pass

            if not resultado.get('success', True):
                error = resultado.get('error', 'Error desconocido')
                await _send_safe(update, f"{EMOJIS['error']} *Error:*\n{error}")
                return

            respuesta = resultado.get('respuesta', '')
            imagenes  = resultado.get('imagenes', [])
            ruta_excel = resultado.get('ruta_excel', '')
            ruta_docx  = resultado.get('ruta_docx', '')
            log_id     = resultado.get('log_id', '')

            for msg in MessageFormatter.dividir_mensaje_largo(md_a_telegram(respuesta)):
                await _send_safe(update, msg)

            # Enviar imágenes
            await _enviar_imagenes(update, respuesta, imagenes)

            if ruta_excel or ruta_docx:
                botones = []
                if ruta_excel:
                    botones.append(InlineKeyboardButton(
                        f"{EMOJIS['download']} Descargar Excel",
                        callback_data=f"download_excel_{user.id}",
                    ))
                if ruta_docx:
                    botones.append(InlineKeyboardButton(
                        f"{EMOJIS['report']} Descargar Informe",
                        callback_data=f"download_docx_{user.id}",
                    ))
                if botones:
                    keyboard = InlineKeyboardMarkup([botones])
                    await update.message.reply_text(
                        f"{EMOJIS['file']} Archivos disponibles para descargar:",
                        reply_markup=keyboard,
                    )
                    context.user_data['ultima_ruta_excel'] = ruta_excel
                    context.user_data['ultima_ruta_docx']  = ruta_docx

            await _enviar_prompt_feedback(update, log_id)

            session_manager.incrementar_turno(user.id)

        except Exception as e:
            logger.error(f"Error en /analisis: {e}")
            try:
                await mensaje_espera.delete()
            except Exception:
                pass
            await _send_safe(update, f"{EMOJIS['error']} Error inesperado: {str(e)}")
    
    @staticmethod
    async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Procesa un mensaje de texto del usuario"""
        user = update.effective_user
        chat_id = update.effective_chat.id
        texto = update.message.text.strip()

        # Si estamos esperando un comentario de feedback, este texto es ESO,
        # no una consulta nueva: se guarda vía /feedback y se corta acá mismo,
        # sin tocar api_client.enviar_consulta ni el historial de sesión.
        pendiente = context.user_data.pop('esperando_feedback', None)
        if pendiente:
            api_client.enviar_feedback(pendiente['log_id'], pendiente['feedback'], texto)
            await update.message.reply_text(f"{EMOJIS['success']} Gracias por tu comentario.")
            return

        # Validar entrada
        if not texto or len(texto) < 3:
            await update.message.reply_text(
                f"{EMOJIS['warning']} Por favor, escribe una pregunta más clara.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Obtener o crear sesión
        sesion = session_manager.obtener_o_crear(
            user_id=user.id,
            chat_id=chat_id,
            username=user.username,
            first_name=user.first_name
        )
        
        # Crear ID de sesión único (basado en user_id)
        session_id = f"telegram_{user.id}"
        
        # Enviar mensaje de "escribiendo..."
        mensaje_espera = await update.message.reply_text(
            f"{EMOJIS['loading']} Procesando tu consulta...\n_(Esto puede tomar unos segundos)_",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            # Enviar consulta al servidor
            inicio = time.time()
            resultado = api_client.enviar_consulta(session_id, texto)
            duracion = time.time() - inicio
            
            # Eliminar mensaje de espera
            await mensaje_espera.delete()
            
            # Procesar resultado
            if not resultado.get('success', True):
                error = resultado.get('error', 'Error desconocido')
                await update.message.reply_text(
                    f"{EMOJIS['error']} *Error:*\n{error}",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            
            # Formatear respuesta
            respuesta = resultado.get('respuesta', '')
            tipo = resultado.get('tipo', 'consulta')
            imagenes = resultado.get('imagenes', [])
            ruta_excel = resultado.get('ruta_excel', '')
            ruta_docx = resultado.get('ruta_docx', '')
            log_id = resultado.get('log_id', '')

            if tipo == 'informe' and ruta_docx:
                # Para informes: no enviar el contenido completo (es enorme),
                # solo confirmar que se generó y ofrecer descarga directa.
                nombre_archivo = Path(ruta_docx).name
                botones = [
                    InlineKeyboardButton(
                        f"{EMOJIS['report']} Descargar Informe (.docx)",
                        callback_data=f"download_docx_{user.id}",
                    )
                ]
                if ruta_excel:
                    botones.append(InlineKeyboardButton(
                        f"{EMOJIS['download']} Descargar Excel",
                        callback_data=f"download_excel_{user.id}",
                    ))
                keyboard = InlineKeyboardMarkup([botones])
                await update.message.reply_text(
                    f"{EMOJIS['report']} *Informe generado correctamente*\n\n"
                    f"Archivo: `{nombre_archivo}`\n\n"
                    f"Descárgalo con el botón de abajo para verlo completo.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard,
                )
                context.user_data['ultima_ruta_excel'] = ruta_excel
                context.user_data['ultima_ruta_docx']  = ruta_docx
                # Enviar imágenes del informe si las hay
                await _enviar_imagenes(update, respuesta, imagenes)
                await _enviar_prompt_feedback(update, log_id)
            else:
                # Consulta normal: enviar el texto completo convertido
                for msg in MessageFormatter.dividir_mensaje_largo(md_a_telegram(respuesta)):
                    await _send_safe(update, msg)

                # Enviar imágenes
                await _enviar_imagenes(update, respuesta, imagenes)

                # Botones de descarga si hay archivos
                if ruta_excel or ruta_docx:
                    botones = []
                    if ruta_excel:
                        botones.append(InlineKeyboardButton(
                            f"{EMOJIS['download']} Descargar Excel",
                            callback_data=f"download_excel_{user.id}",
                        ))
                    if ruta_docx:
                        botones.append(InlineKeyboardButton(
                            f"{EMOJIS['report']} Descargar Informe",
                            callback_data=f"download_docx_{user.id}",
                        ))
                    if botones:
                        keyboard = InlineKeyboardMarkup([botones])
                        await update.message.reply_text(
                            f"{EMOJIS['file']} Archivos disponibles para descargar:",
                            reply_markup=keyboard,
                        )
                        context.user_data['ultima_ruta_excel'] = ruta_excel
                        context.user_data['ultima_ruta_docx']  = ruta_docx

                await _enviar_prompt_feedback(update, log_id)

            # Incrementar turno
            session_manager.incrementar_turno(user.id)
            
        except Exception as e:
            logger.error(f"Error procesando mensaje: {e}")
            try:
                await mensaje_espera.delete()
            except Exception:
                pass
            await _send_safe(update, f"{EMOJIS['error']} Error inesperado: {str(e)}")
    
    @staticmethod
    async def calificar_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Maneja el toque en 👍/😐/👎 bajo una respuesta.
        callback_data: 'fb:<bueno|regular|malo>:<log_id>'
        """
        query = update.callback_query
        await query.answer()

        try:
            _, feedback, log_id = query.data.split(':', 2)
        except ValueError:
            return

        etiquetas = {'bueno': '👍 Bueno', 'regular': '😐 Regular', 'malo': '👎 Malo'}
        context.user_data['esperando_feedback'] = {'log_id': log_id, 'feedback': feedback}

        await query.edit_message_text(f"Calificación: {etiquetas.get(feedback, feedback)}")
        await query.message.reply_text(
            "¿Quieres agregar un comentario? Escríbelo en tu próximo mensaje, "
            "o pulsa Omitir.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Omitir", callback_data=f"fbskip:{log_id}"),
            ]]),
        )

    @staticmethod
    async def omitir_comentario_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Maneja el botón 'Omitir' del comentario opcional de feedback."""
        query = update.callback_query
        await query.answer()

        pendiente = context.user_data.pop('esperando_feedback', None)
        if pendiente:
            api_client.enviar_feedback(pendiente['log_id'], pendiente['feedback'], '')

        await query.edit_message_text(f"{EMOJIS['success']} Gracias por tu feedback.")

    @staticmethod
    async def descargar_archivo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Maneja la descarga de archivos"""
        query = update.callback_query
        user_id = update.effective_user.id
        
        await query.answer()
        
        if "excel" in query.data:
            ruta = context.user_data.get('ultima_ruta_excel', '')
            tipo_archivo = "Excel"
        else:
            ruta = context.user_data.get('ultima_ruta_docx', '')
            tipo_archivo = "Informe"
        
        if not ruta:
            await query.edit_message_text(f"{EMOJIS['error']} Archivo no disponible.")
            return
        
        try:
            ruta_local = Path(ruta)
            if not ruta_local.exists():
                await query.edit_message_text(
                    f"{EMOJIS['error']} El archivo no existe."
                )
                return
            
            # Enviar archivo
            with open(ruta_local, 'rb') as f:
                await query.message.reply_document(
                    document=f,
                    filename=ruta_local.name,
                    caption=f"{EMOJIS['download']} {tipo_archivo} generado"
                )
            
            await query.edit_message_text(
                f"{EMOJIS['success']} Archivo enviado."
            )
            
        except Exception as e:
            logger.error(f"Error descargando archivo: {e}")
            await query.edit_message_text(
                f"{EMOJIS['error']} Error al descargar el archivo."
            )
