import os
import random
import json
from pathlib import Path
from datetime import time

import pytz
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    PollAnswerHandler,
)

# =======================
# CONFIGURAÇÕES BÁSICAS
# =======================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "COLOQUE_SEU_TOKEN_AQUI")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1001234567890"))

# URL da API externa de questões (você vai criar/fornecer)
QUESTIONS_API_URL = os.getenv("QUESTIONS_API_URL", "")

# Tópicos padrão por período
TOPICO_MANHA = "Penal"
TOPICO_TARDE = "Constitucional"
TOPICO_NOITE = "Administrativo"

# Arquivo de pontuações (ranking)
SCORES_FILE = Path("scores.json")

# Fuso horário Brasil
TZ = pytz.timezone("America/Sao_Paulo")


# =======================
# BANCO LOCAL DE QUESTÕES (FALLBACK)
# =======================

QUESTOES_EXEMPLO = [
    {
        "pergunta": "Sobre o princípio da legalidade, assinale a alternativa correta:",
        "opcoes": [
            "A administração pública pode agir livremente, salvo proibição legal.",
            "O administrador público só pode fazer o que a lei permite.",
            "O administrador público pode agir por analogia mesmo contra a lei.",
            "A lei é mera recomendação para a administração."
        ],
        "correta": "O administrador público só pode fazer o que a lei permite.",
        "comentario": "Administração só age com autorização legal. Particular faz tudo que não é proibido.",
        "topico": "Administrativo"
    },
    {
        "pergunta": "Qual é a natureza jurídica do habeas corpus?",
        "opcoes": [
            "Ação penal pública incondicionada.",
            "Ação constitucional de natureza civil.",
            "Recurso extraordinário.",
            "Ação de controle concentrado de constitucionalidade."
        ],
        "correta": "Ação constitucional de natureza civil.",
        "comentario": "Ação constitucional civil para proteger a liberdade de locomoção.",
        "topico": "Constitucional"
    },
    {
        "pergunta": "O crime permanente é aquele cuja consumação:",
        "opcoes": [
            "Ocorre em um único instante.",
            "Depende de resultado naturalístico.",
            "Se prolonga no tempo por vontade do agente.",
            "Se dá sem qualquer conduta humana."
        ],
        "correta": "Se prolonga no tempo por vontade do agente.",
        "comentario": "Crime permanente: consumação que se prolonga com a continuidade da conduta.",
        "topico": "Penal"
    },
]


# =======================
# FUNÇÕES DE ALTA: RANKING
# =======================

def carregar_scores():
    if SCORES_FILE.exists():
        try:
            with SCORES_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def salvar_scores(scores: dict):
    with SCORES_FILE.open("w", encoding="utf-8") as f:
        json.dump(scores, f, ensure_ascii=False, indent=2)


def registrar_acerto(user, topico: str, pontos: int = 1):
    scores = carregar_scores()
    uid = str(user.id)

    nome = (f"{user.first_name or ''} {user.last_name or ''}").strip()
    if not nome:
        nome = user.username or uid

    if uid not in scores:
        scores[uid] = {"name": nome, "score": 0, "topics": {}}

    scores[uid]["name"] = nome
    scores[uid]["score"] += pontos

    topics = scores[uid].get("topics", {})
    topics[topico] = topics.get(topico, 0) + 1
    scores[uid]["topics"] = topics

    salvar_scores(scores)


# =======================
# BUSCA DE QUESTÕES (API + LOCAL)
# =======================

def buscar_questoes(qtd: int = 10, topico: str | None = None):
    # ---- Tentativa de API externa ----
    if QUESTIONS_API_URL:
        try:
            params = {"qtd": qtd}
            if topico:
                params["topico"] = topico

            resp = requests.get(QUESTIONS_API_URL, params=params, timeout=10)
            resp.raise_for_status()

            dados = resp.json()
            questoes_validas = []

            for q in dados:
                if not all(k in q for k in ("pergunta", "opcoes", "correta")):
                    continue
                if not isinstance(q["opcoes"], list) or len(q["opcoes"]) < 2:
                    continue

                questoes_validas.append({
                    "pergunta": q["pergunta"],
                    "opcoes": q["opcoes"],
                    "correta": q["correta"],
                    "comentario": q.get("comentario", ""),
                    "topico": q.get("topico", topico or "Geral"),
                })

            if questoes_validas:
                return random.sample(questoes_validas, min(qtd, len(questoes_validas)))

        except Exception as e:
            print(f"[WARN] API externa falhou: {e}")

    # ---- Fallback: banco local ----
    questoes_filtradas = (
        [q for q in QUESTOES_EXEMPLO if q.get("topico") == topico]
        if topico else QUESTOES_EXEMPLO[:]
    )

    if not questoes_filtradas:
        questoes_filtradas = QUESTOES_EXEMPLO[:]

    return random.sample(questoes_filtradas, min(qtd, len(questoes_filtradas)))


# =======================
# FUNÇÕES DO BOT
# =======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot da Mentoria Black House ativo!\n\n"
        "Use /ranking para consultar o ranking atual."
    )


async def comando_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scores = carregar_scores()

    if not scores:
        await update.message.reply_text("Ainda não houve participações.")
        return

    ordenado = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)

    linhas = ["🏆 *Ranking Black House* 🏆\n"]
    for pos, (uid, dados) in enumerate(ordenado[:10], start=1):
        linhas.append(f"{pos}. {dados['name']} — *{dados['score']}* pts")

    await update.message.reply_markdown("\n".join(linhas))


async def enviar_lote_topico(context: ContextTypes.DEFAULT_TYPE, topico: str):
    questoes = buscar_questoes(10, topico=topico)

    for q in questoes:
        opcoes = q["opcoes"]
        correta = q["correta"]

        try:
            idx_correto = opcoes.index(correta)
        except:
            continue

        comentario = q.get("comentario", "")
        if len(comentario) > 200:
            comentario = comentario[:197] + "..."

        msg = await context.bot.send_poll(
            chat_id=CHANNEL_ID,
            question=q["pergunta"],
            options=opcoes,
            type="quiz",
            correct_option_id=idx_correto,
            is_anonymous=False,
            explanation=comentario
        )

        poll_id = msg.poll.id
        polls = context.bot_data.setdefault("polls", {})
        polls[poll_id] = {
            "correct_option_id": idx_correto,
            "topico": topico,
            "points": 1
        }


async def enviar_questoes_manha(context):
    await enviar_lote_topico(context, TOPICO_MANHA)


async def enviar_questoes_tarde(context):
    await enviar_lote_topico(context, TOPICO_TARDE)


async def enviar_questoes_noite(context):
    await enviar_lote_topico(context, TOPICO_NOITE)


async def comando_teste_lote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enviando 10 questões agora...")
    await enviar_lote_topico(context, "Penal")
    await update.message.reply_text("Lote enviado!")

async def receber_resposta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    poll_id = answer.poll_id

    polls = context.bot_data.get("polls", {})
    meta = polls.get(poll_id)

    if not meta:
        return

    chosen = answer.option_ids[0]
    correto = meta["correct_option_id"]
    topico = meta["topico"]

    if chosen == correto:
        registrar_acerto(answer.user, topico)


# =======================
# RESUMO SEMANAL (DOMINGO 21h)
# =======================

async def enviar_resumo_semanal(context: ContextTypes.DEFAULT_TYPE):
    scores = carregar_scores()

    if not scores:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text="📊 Resumo semanal: sem participações registradas."
        )
        return

    ordenado = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)

    linhas = ["🏁 *Resumo semanal Black House* 🏁\n"]
    linhas.append("Top 10 da semana:\n")

    for pos, (uid, dados) in enumerate(ordenado[:10], start=1):
        linhas.append(f"{pos}. {dados['name']} — *{dados['score']}* pts")

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text="\n".join(linhas),
        parse_mode="Markdown"
    )

    salvar_scores({})


# =======================
# AGENDAMENTO
# =======================

def configurar_agendamentos(app):
    job = app.job_queue

    job.run_daily(enviar_questoes_manha, time=time(8, 0, tzinfo=TZ))
    job.run_daily(enviar_questoes_tarde, time=time(15, 0, tzinfo=TZ))
    job.run_daily(enviar_questoes_noite, time=time(20, 0, tzinfo=TZ))

    job.run_daily(
        enviar_resumo_semanal,
        time=time(21, 0, tzinfo=TZ),
        days=(6,),  # domingo
        name="resumo_semanal"
    )


# =======================
# MAIN
# =======================

def main():
    if TELEGRAM_TOKEN.startswith("COLOQUE_SEU_TOKEN"):
        raise RuntimeError("Defina TELEGRAM_TOKEN.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("testelote", comando_teste_lote))
    app.add_handler(CommandHandler("ranking", comando_ranking))

    app.add_handler(PollAnswerHandler(receber_resposta))

    configurar_agendamentos(app)

    print("Bot Black House rodando...")
    app.run_polling()


if __name__ == "__main__":
    main()
