import os
import requests
import pytz
from datetime import datetime, time
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
import logging

# ----------------------------------------------------------
# CONFIGURAÇÃO DE LOGS (MOSTRA ERROS NO RAILWAY)
# ----------------------------------------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------
# VARIÁVEIS DE AMBIENTE
# ----------------------------------------------------------
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
QUESTIONS_API_URL = os.getenv("QUESTIONS_API_URL")

TZ = pytz.timezone("America/Sao_Paulo")

# ----------------------------------------------------------
# CICLO DE MATÉRIAS (Dia 1, Dia 2, Dia 3)
# ----------------------------------------------------------
ROTACAO = {
    1: ["Penal", "Constitucional", "Administrativo"],
    2: ["Lei de Execução Penal", "Informática", "Língua Portuguesa"],
    3: ["Raciocínio Lógico", "Contabilidade", "Legislação Extravagante"]
}

def obter_dia_rotacao():
    """
    Dia 1 = (data % 3 == 1)
    Dia 2 = (data % 3 == 2)
    Dia 3 = (data % 3 == 0)
    """
    dia_mes = datetime.now(TZ).day
    resto = dia_mes % 3
    return 3 if resto == 0 else resto

# ----------------------------------------------------------
# FUNÇÃO DE BUSCAR QUESTÕES DA API
# ----------------------------------------------------------
def buscar_questoes(topico):
    try:
        resp = requests.get(
            QUESTIONS_API_URL,
            params={"qtd": 10, "topico": topico},
            timeout=10
        )
        if resp.status_code == 200:
            return resp.json()
        else:
            logger.error(f"Erro API {resp.status_code}: {resp.text}")
            return []
    except Exception as e:
        logger.error(f"Erro ao consultar API: {e}")
        return []

# ----------------------------------------------------------
# ENVIO DE LOTE (COM DEBUG SE HOUVER ERRO)
# ----------------------------------------------------------
async def enviar_lote_topico(context, topico):
    questoes = buscar_questoes(topico)
    if not questoes:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=f"⚠ Não encontrei questões de {topico}."
        )
        return

    for q in questoes:
        try:
            await context.bot.send_poll(
                chat_id=CHANNEL_ID,
                question=q["pergunta"],
                options=q["opcoes"],
                correct_option_id=q["opcoes"].index(q["correta"]),
                explanation=q.get("comentario", "Sem comentário."),
                is_anonymous=False
            )
        except Exception as e:
            logger.error("ERRO AO ENVIAR ENQUETE:")
            logger.error(f"Tipo: {type(e)}")
            logger.error(f"Detalhes: {e}")
            # também envia mensagem para o canal para ver o erro
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=f"❌ ERRO ao enviar enquete:\n`{e}`",
                parse_mode="Markdown"
            )

# ----------------------------------------------------------
# COMANDO MANUAL /testelote (ENVIA NA HORA)
# ----------------------------------------------------------
async def comando_teste_lote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enviando 10 questões agora (Direito Penal)...")
    await enviar_lote_topico(context, "Penal")
    await update.message.reply_text("Lote enviado!")

# ----------------------------------------------------------
# AGENDA AUTOMÁTICA (08h / 15h / 20h)
# ----------------------------------------------------------
def configurar_agendamentos(app):
    dia = obter_dia_rotacao()
    topicos = ROTACAO[dia]

    app.job_queue.run_daily(
        enviar_lote_topico,
        time=time(8, 0, tzinfo=TZ),
        data={"topico": topicos[0]}
    )

    app.job_queue.run_daily(
        enviar_lote_topico,
        time=time(15, 0, tzinfo=TZ),
        data={"topico": topicos[1]}
    )

    app.job_queue.run_daily(
        enviar_lote_topico,
        time=time(20, 0, tzinfo=TZ),
        data={"topico": topicos[2]}
    )

# ----------------------------------------------------------
# /start
# ----------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot Black House ativo!\n"
        "Use /testelote para enviar 10 questões agora.\n"
        "Os envios automáticos estão configurados."
    )

# ----------------------------------------------------------
# MAIN
# ----------------------------------------------------------
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("testelote", comando_teste_lote))

    configurar_agendamentos(app)

    logger.info("BOT BLACK HOUSE INICIADO NO RAILWAY...")
    app.run_polling()

if __name__ == "__main__":
    main()
