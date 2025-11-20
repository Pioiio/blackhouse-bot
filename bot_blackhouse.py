import os
import json
import requests
import pytz
from datetime import datetime, timedelta, time
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
import logging

# ----------------------------------------------------------
# LOGS
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
# ROTACAO DAS 9 MATÉRIAS
# ----------------------------------------------------------
ROTACAO = {
    1: ["Penal", "Constitucional", "Administrativo"],
    2: ["Lei de Execução Penal", "Informática", "Língua Portuguesa"],
    3: ["Raciocínio Lógico", "Contabilidade", "Legislação Extravagante"]
}

def obter_dia_rotacao():
    dia = datetime.now(TZ).day
    resto = dia % 3
    return 3 if resto == 0 else resto

# ----------------------------------------------------------
# CACHE PARA EVITAR REPETIÇÃO
# ----------------------------------------------------------
CACHE_FILE = "cache_questoes.json"

def carregar_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def salvar_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def adicionar_cache(ids):
    cache = carregar_cache()
    hoje = datetime.now(TZ).strftime("%Y-%m-%d")
    cache[hoje] = ids

    # remover dias com mais de 2 dias
    dias_validos = []
    for dia in cache:
        dt = datetime.strptime(dia, "%Y-%m-%d")
        if datetime.now(TZ) - dt <= timedelta(days=2):
            dias_validos.append(dia)

    cache = {dia: cache[dia] for dia in dias_validos}
    salvar_cache(cache)

def ids_ultimos_dois_dias():
    cache = carregar_cache()
    ids = []
    for dia in cache:
        ids.extend(cache[dia])
    return ids

# ----------------------------------------------------------
# BUSCAR QUESTÕES NA API
# ----------------------------------------------------------
def buscar_questoes(topico):
    try:
        resp = requests.get(
            QUESTIONS_API_URL,
            params={"qtd": 100, "topico": topico},
            timeout=10
        )

        if resp.status_code != 200:
            logger.error(f"Erro API {resp.status_code}")
            return []

        banco = resp.json()

        # filtrar por substring (Penal detecta Direito Penal etc.)
        banco = [
            q for q in banco
            if topico.lower() in q.get("topico", "").lower()
        ]

        return banco

    except Exception as e:
        logger.error(f"Erro API: {e}")
        return []

# ----------------------------------------------------------
# SERVIR O LOTE
# ----------------------------------------------------------
async def enviar_lote_topico(context, topico):

    questoes = buscar_questoes(topico)
    if not questoes:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=f"⚠ Não encontrei questões de {topico}."
        )
        return

    # evitar repetição
    repetidas = set(ids_ultimos_dois_dias())

    questoes_filtradas = [
        q for q in questoes
        if q["pergunta"] not in repetidas
    ]

    # Se não houver 10 limpas, envia o que tem
    lote = questoes_filtradas[:10]

    if not lote:
        lote = questoes[:10]

    # salvar no cache
    enviados = [q["pergunta"] for q in lote]
    adicionar_cache(enviados)

    # enviar
    for q in lote:
        try:
            await context.bot.send_poll(
                chat_id=CHANNEL_ID,
                question=q["pergunta"],
                options=q["opcoes"],
                correct_option_id=q["opcoes"].index(q["correta"]),
                explanation=q["comentario"][:180],  # evitar limite do Telegram
                is_anonymous=True
            )
        except Exception as e:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=f"❌ ERRO ao enviar enquete:\n`{e}`",
                parse_mode="Markdown"
            )
            logger.error(e)

# ----------------------------------------------------------
# /testelote
# ----------------------------------------------------------
async def comando_teste_lote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await enviar_lote_topico(context, "Penal")
    await update.message.reply_text("Lote enviado!")

# ----------------------------------------------------------
# AGENDA AUTOMÁTICA
# ----------------------------------------------------------
def configurar_agendamentos(app):
    dia = obter_dia_rotacao()
    topicos = ROTACAO[dia]

    app.job_queue.run_daily(enviar_lote_topico, time=time(8, 0, tzinfo=TZ), data={"topico": topicos[0]})
    app.job_queue.run_daily(enviar_lote_topico, time=time(15, 0, tzinfo=TZ), data={"topico": topicos[1]})
    app.job_queue.run_daily(enviar_lote_topico, time=time(20, 0, tzinfo=TZ), data={"topico": topicos[2]})

# ----------------------------------------------------------
# /start
# ----------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot Black House ativo!\n"
        "Use /testelote para enviar 10 questões agora.\n"
        "Envios automáticos configurados."
    )

# ----------------------------------------------------------
# MAIN
# ----------------------------------------------------------
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("testelote", comando_teste_lote))

    configurar_agendamentos(app)

    logger.info("BOT BLACK HOUSE INICIADO...")
    app.run_polling()

if __name__ == "__main__":
    main()
