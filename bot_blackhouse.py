import os
import json
import random
from pathlib import Path
from datetime import time

import requests
import pytz
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    PollAnswerHandler,
    CallbackQueryHandler,
)

# ============ CONFIGURAÇÕES BÁSICAS ============

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
QUESTIONS_API_URL = os.getenv("QUESTIONS_API_URL", "").rstrip("/")

# tópicos que existem no seu database.json
TOPICOS_LISTA = [
    "Penal",
    "Constitucional",
    "Administrativo",
    "Português",
    "Raciocínio Lógico",
    "Processo Penal",
    "Informática",
    "Direitos Humanos",
    "LEP",
    "Legislação Extravagante",
]

# horários automáticos (pode ajustar depois)
TZ = pytz.timezone("America/Sao_Paulo")
HORARIOS_AUTOMATICOS = [
    (time(8, 0, tzinfo=TZ), "Penal"),
    (time(13, 0, tzinfo=TZ), "Constitucional"),
    (time(19, 0, tzinfo=TZ), "Administrativo"),
]

SCORES_FILE = Path("scores.json")

# Fallback local simples para caso a API morra de vez
QUESTOES_FALLBACK = [
    {
        "pergunta": "O crime permanente é aquele cuja consumação:",
        "opcoes": [
            "Ocorre em um único instante.",
            "Depende de resultado naturalístico.",
            "Se prolonga no tempo por vontade do agente.",
            "Se dá sem qualquer conduta humana."
        ],
        "correta": "Se prolonga no tempo por vontade do agente.",
        "comentario": "Crime permanente: consumação que se prolonga no tempo por vontade do agente (ex.: sequestro).",
        "topico": "Penal",
    },
]


# ============ UTILITÁRIOS DE SCORE / RANKING ============

def carregar_scores() -> dict:
    if SCORES_FILE.exists():
        try:
            return json.loads(SCORES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def salvar_scores(scores: dict) -> None:
    SCORES_FILE.write_text(
        json.dumps(scores, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def registrar_acerto(user, topico: str, pontos: int = 1) -> None:
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


# ============ BUSCA DE QUESTÕES (API + FALLBACK) ============

def buscar_questoes(qtd: int = 10, topico: str | None = None) -> list:
    """
    Nova lógica:
    - Tenta montar um lote com até `qtd` questões.
    - Aceita tanto resposta em LISTA quanto em DICT da API.
    - Garante que só entram questões válidas e não repetidas (pergunta + correta).
    - Se der tudo errado, usa o fallback local.
    """

    def normalizar_resposta(dados, topico_padrao: str | None):
        """Transforma o JSON da API em uma lista de questões válidas."""
        questoes_validas = []

        # Se vier um dict único com a questão
        if isinstance(dados, dict):
            # Caso 1: já seja uma questão
            if all(k in dados for k in ("pergunta", "opcoes", "correta")):
                dados = [dados]
            # Caso 2: vindo dentro de uma chave tipo "result" / "questoes"
            elif "result" in dados and isinstance(dados["result"], list):
                dados = dados["result"]
            elif "questoes" in dados and isinstance(dados["questoes"], list):
                dados = dados["questoes"]
            else:
                return []

        # Tem que ser lista a partir daqui
        if not isinstance(dados, list):
            return []

        for q in dados:
            if not isinstance(q, dict):
                continue
            if not all(k in q for k in ("pergunta", "opcoes", "correta")):
                continue
            if not isinstance(q["opcoes"], list) or len(q["opcoes"]) < 2:
                continue

            questoes_validas.append({
                "pergunta": q["pergunta"],
                "opcoes": q["opcoes"],
                "correta": q["correta"],
                "comentario": q.get("comentario", ""),
                "topico": q.get("topico", topico_padrao or "Geral"),
            })

        return questoes_validas

    def chamar_api(params: dict) -> list:
        if not QUESTIONS_API_URL:
            return []
        try:
            resp = requests.get(
                QUESTIONS_API_URL,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            dados = resp.json()
            questoes = normalizar_resposta(dados, params.get("topico"))
            return questoes or []
        except Exception as e:
            print(f"[WARN] Erro chamando API: {e}")
            return []

    # ===================== MONTA O LOTE =====================

    lote: list[dict] = []
    vistos: set[tuple] = set()  # (pergunta, correta)

    # Faz várias tentativas para tentar variar as questões
    tentativas_max = qtd * 3
    tentativas = 0

    while len(lote) < qtd and tentativas < tentativas_max:
        tentativas += 1

        params = {}
        if topico:
            params["topico"] = topico
        params["qtd"] = 1  # muitas APIs entregam só 1 por vez, então chamamos várias vezes

        questoes = chamar_api(params)
        if not questoes:
            break

        for q in questoes:
            chave = (q["pergunta"], q["correta"])
            if chave in vistos:
                continue
            vistos.add(chave)
            lote.append(q)
            if len(lote) >= qtd:
                break

    # Se não conseguimos montar nada decente, tenta API sem tópico
    if not lote:
        tentativas = 0
        while len(lote) < qtd and tentativas < tentativas_max:
            tentativas += 1
            questoes = chamar_api({"qtd": 1})
            if not questoes:
                break
            for q in questoes:
                chave = (q["pergunta"], q["correta"])
                if chave in vistos:
                    continue
                vistos.add(chave)
                lote.append(q)
                if len(lote) >= qtd:
                    break

    # Se ainda assim estiver vazio, usa fallback local
    if not lote:
        base = QUESTOES_FALLBACK[:]
        if topico:
            filtradas = [q for q in base if q.get("topico") == topico]
            if filtradas:
                base = filtradas
        # repete fallback até encher o lote
        while len(lote) < qtd:
            q = random.choice(base)
            chave = (q["pergunta"], q["correta"])
            if chave in vistos:
                continue
            vistos.add(chave)
            lote.append(q)

    random.shuffle(lote)
    return lote[:qtd]



# ============ COMANDOS BÁSICOS ============

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 Bot da Mentoria Black House ativo!\n\n"
        "Comandos principais:\n"
        "/testar – escolher matéria e mandar questões agora\n"
        "/ranking – ver ranking de acertos\n"
        "/testelote – teste rápido (Penal)\n"
    )


async def cmd_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scores = carregar_scores()
    if not scores:
        await update.message.reply_text("Ainda não há participações registradas.")
        return

    ordenado = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
    linhas = ["🏆 *Ranking Black House* 🏆\n"]
    for pos, (_, dados) in enumerate(ordenado[:10], start=1):
        linhas.append(f"{pos}. {dados['name']} — *{dados['score']}* pts")

    await update.message.reply_markdown("\n".join(linhas))


# ============ ENVIO DE QUESTÕES ============

async def enviar_lote(context: ContextTypes.DEFAULT_TYPE, topico: str, origem: str):
    """
    origem: 'auto' ou 'manual' (só para log/controle se quiser no futuro)
    """
    print(f"[INFO] Enviando lote para tópico '{topico}' (origem={origem})")

    questoes = buscar_questoes(10, topico=topico)

    if not questoes:
        # Com o fallback novo isso é quase impossível, mas deixo por segurança
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=f"⚠️ Não encontrei questões de {topico}.",
        )
        return

    for q in questoes:
        opcoes = q["opcoes"]
        correta = q["correta"]

        try:
            idx_correta = opcoes.index(correta)
        except ValueError:
            # pular questão mal formatada
            continue

        comentario = q.get("comentario", "")
        if len(comentario) > 200:
            comentario = comentario[:197] + "..."

        msg = await context.bot.send_poll(
    chat_id=CHANNEL_ID,
    question=q["pergunta"],
    options=opcoes,
    type="quiz",
    correct_option_id=idx_correta,
    is_anonymous=True,       # ✔ CORRETO para canal
    explanation=comentario,
)


        poll_id = msg.poll.id
        polls = context.bot_data.setdefault("polls", {})
        polls[poll_id] = {
            "correct_option_id": idx_correta,
            "topico": q.get("topico", topico),
            "points": 1,
        }


# usados pelo agendamento automático
async def job_enviar(context: ContextTypes.DEFAULT_TYPE):
    dados = context.job.data  # {"topico": "..."}
    topico = dados["topico"]
    await enviar_lote(context, topico, origem="auto")


# comando de teste manual rápido
async def cmd_testelote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enviando lote de teste (Penal) para o canal...")
    await enviar_lote(context, "Penal", origem="manual")


# ============ /testar → MENU DE MATÉRIAS ============

async def cmd_testar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(top, callback_data=f"TESTAR|{top}")]
        for top in TOPICOS_LISTA
    ]

    await update.message.reply_text(
        "Selecione a matéria:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cb_botoes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if data.startswith("TESTAR|"):
        topico = data.split("|", 1)[1]
        await query.edit_message_text(
            f"🔍 Disparando questões de *{topico}*...",
            parse_mode="Markdown",
        )
        await enviar_lote(context, topico, origem="manual")


# ============ RESPOSTAS DOS ALUNOS (RANKING) ============

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


# ============ RESUMO SEMANAL (OPCIONAL) ============

async def job_resumo_semanal(context: ContextTypes.DEFAULT_TYPE):
    scores = carregar_scores()
    if not scores:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text="📊 Resumo semanal: ainda não há participações registradas.",
        )
        return

    ordenado = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)

    linhas = ["🏁 *Resumo semanal Black House* 🏁\n", "Top 10:\n"]
    for pos, (_, dados) in enumerate(ordenado[:10], start=1):
        linhas.append(f"{pos}. {dados['name']} — *{dados['score']}* pts")

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text="\n".join(linhas),
        parse_mode="Markdown",
    )

    # se quiser zerar semanalmente, descomente:
    # salvar_scores({})


# ============ AGENDAMENTOS ============

def configurar_agendamentos(app):
    job_queue = app.job_queue

    # horários de questões automáticas
    for hora, topico in HORARIOS_AUTOMATICOS:
        job_queue.run_daily(
            job_enviar,
            time=hora,
            data={"topico": topico},
            name=f"lote_{topico}_{hora.hour}",
        )

    # resumo semanal no domingo às 21h (opcional)
    job_queue.run_daily(
        job_resumo_semanal,
        time=time(21, 0, tzinfo=TZ),
        days=(6,),  # domingo
        name="resumo_semanal",
    )


# ============ MAIN ============

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Defina TELEGRAM_TOKEN nas variáveis de ambiente.")
    if CHANNEL_ID == 0:
        raise RuntimeError("Defina CHANNEL_ID nas variáveis de ambiente.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # comandos
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ranking", cmd_ranking))
    app.add_handler(CommandHandler("testelote", cmd_testelote))
    app.add_handler(CommandHandler("testar", cmd_testar))

    # callback dos botões
    app.add_handler(CallbackQueryHandler(cb_botoes))

    # respostas das enquetes
    app.add_handler(PollAnswerHandler(handle_poll_answer))

    # agendamentos
    configurar_agendamentos(app)

    print("Bot Black House rodando...")
    app.run_polling()


if __name__ == "__main__":
    main()
