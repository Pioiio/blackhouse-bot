# bot_blackhouse.py
from __future__ import annotations

import logging
import os
from datetime import time
from typing import List, Dict, Any

import httpx
import pytz
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("blackhouse-bot")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
QUESTIONS_API_URL = os.getenv(
    "QUESTIONS_API_URL", "http://localhost:8000/questoes"
).strip()
CANAL_ID = os.getenv("CANAL_ID", "@BLACKHOUSE_CONCURSOS").strip()

TZ = pytz.timezone("America/Sao_Paulo")

TOPIC_SCHEDULES = [
    (time(8, 0), "Direito Penal"),
    (time(13, 0), "Direito Constitucional"),
    (time(19, 0), "Raciocínio Lógico"),
]

QTD_POR_HORARIO = 10


class APIError(Exception):
    pass


async def buscar_questoes_api(
    topico: str,
    qtd: int = QTD_POR_HORARIO,
) -> List[Dict[str, Any]]:
    if not QUESTIONS_API_URL:
        raise APIError("QUESTIONS_API_URL não configurada")

    params = {"qtd": qtd, "topico": topico}

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(QUESTIONS_API_URL, params=params)
            resp.raise_for_status()
        except Exception as e:
            logger.error("Erro ao chamar API de questões: %s", e)
            raise APIError(f"Erro na API: {e}")

    dados = resp.json()
    if not isinstance(dados, list):
        raise APIError("Resposta inesperada da API (não é lista).")

    questoes: List[Dict[str, Any]] = []
    for item in dados:
        try:
            questoes.append(
                {
                    "id": int(item["id"]),
                    "topico": str(item["topico"]),
                    "pergunta": str(item["pergunta"]),
                    "opcoes": list(item["opcoes"]),
                    "correta": int(item["correta"]),
                    "comentario": str(item.get("comentario", "")),
                }
            )
        except Exception:
            continue

    if not questoes:
        raise APIError("API não retornou questões válidas.")

    return questoes


async def job_enviar_lote(context: ContextTypes.DEFAULT_TYPE) -> None:
    dados = context.job.data or {}
    topico = dados.get("topico") or "Geral"

    logger.info("Job disparado para tópico: %s", topico)

    try:
        questoes = await buscar_questoes_api(topico=topico, qtd=QTD_POR_HORARIO)
    except APIError as e:
        logger.error("Falha ao obter questões (%s): %s", topico, e)
        try:
            await context.bot.send_message(
                chat_id=CANAL_ID,
                text=f"⚠️ Não foi possível carregar questões de *{topico}* agora.\nMotivo: {e}",
                parse_mode="Markdown",
            )
        except Exception as ex:
            logger.error("Falha ao enviar mensagem de erro no canal: %s", ex)
        return

    try:
        await context.bot.send_message(
            chat_id=CANAL_ID,
            text=f"📚 Matéria: *{topico}*\nSerão enviadas {len(questoes)} questões agora.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("Erro ao enviar mensagem inicial do lote: %s", e)

    for q in questoes:
        pergunta = f"[{q['topico']}] {q['pergunta']}"
        try:
            await context.bot.send_poll(
                chat_id=CANAL_ID,
                question=pergunta,
                options=q["opcoes"],
                type="quiz",
                correct_option_id=q["correta"],
                explanation=q["comentario"] or None,
                is_anonymous=False,
            )
        except Exception as e:
            logger.error("Erro ao enviar poll (id=%s): %s", q.get("id"), e)

    logger.info("Lote enviado com sucesso para tópico: %s", topico)


def configurar_jobs(app: Application) -> None:
    scheduler = app.job_queue.scheduler
    scheduler.configure(timezone=TZ)

    for hora, topico in TOPIC_SCHEDULES:
        app.job_queue.run_daily(
            job_enviar_lote,
            time=hora,
            data={"topico": topico},
            name=f"auto_{topico}",
        )
        logger.info("Agendado horário %s para tópico %s", hora, topico)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    texto = (
        "👊 *Black House Bot*\n\n"
        "Este bot envia automaticamente lotes de questões no canal:\n"
        f"{CANAL_ID}\n\n"
        "Horários configurados:\n"
        "• 08:00 – Direito Penal\n"
        "• 13:00 – Direito Constitucional\n"
        "• 19:00 – Raciocínio Lógico\n"
        "\n"
        "Cada horário: 10 questões, sem repetir até rodar o banco todo (controle pela API)."
    )
    await update.message.reply_text(texto, parse_mode="Markdown")


async def cmd_testar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Use: /testar <matéria>\nEx: /testar \"Direito Penal\""
        )
        return

    topico = " ".join(context.args)
    await update.message.reply_text(
        f"Enviando lote de teste para matéria: {topico}"
    )

    class SimpleJob:
        def __init__(self, topico):
            self.data = {"topico": topico}

    class SimpleContext:
        def __init__(self, bot, job):
            self.bot = bot
            self.job = job

    fake_job = SimpleJob(topico)
    fake_ctx = SimpleContext(context.bot, fake_job)
    await job_enviar_lote(fake_ctx)


def validar_config():
    erros = []
    if not TELEGRAM_TOKEN:
        erros.append("TELEGRAM_TOKEN não configurado.")
    if not CANAL_ID:
        erros.append("CANAL_ID não configurado.")
    if erros:
        raise RuntimeError("Configuração inválida:\n- " + "\n- ".join(erros))


def criar_app() -> Application:
    validar_config()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("testar", cmd_testar))

    configurar_jobs(app)

    return app


def main():
    logger.info("Iniciando Bot Black House (PTB 21.6)...")
    app = criar_app()
    logger.info("Bot em modo polling.")
    app.run_polling()


if __name__ == "__main__":
    main()
