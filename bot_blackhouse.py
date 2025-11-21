"""
Bot Black House – versão inteligente, compatível com python-telegram-bot v20.x

Principais características:
- Robusto, resiliente e precavido
- API com retry, backoff, normalização de JSON e proteção contra repetição
- Timezone correto via scheduler (compatível com PTB v20)
- Jobs automáticos funcionando no Railway
"""

from __future__ import annotations

import logging
import os
import random
import time as time_mod
from dataclasses import dataclass
from datetime import time
from typing import Any, Dict, List, Optional, Set, Tuple

import pytz
import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    JobQueue,
)

# ================================
# LOGGING
# ================================
logging.basicConfig(
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("blackhouse-bot")

# ================================
# CONFIG
# ================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
QUESTIONS_API_URL = os.getenv(
    "QUESTIONS_API_URL", "https://blackhouse-api-production.up.railway.app/questoes"
).strip()
CANAL_ID = os.getenv("CANAL_ID", "@BLACKHOUSE_CONCURSOS").strip()

TZ = pytz.timezone("America/Sao_Paulo")

TOPICOS_LISTA = [
    "Penal",
    "Constitucional",
    "Raciocínio Lógico",
    "Processo Penal",
    "Direitos Humanos",
]

HORARIOS_AUTOMATICOS = [
    (time(8, 0), "Penal"),
    (time(13, 0), "Constitucional"),
    (time(19, 0), "Raciocínio Lógico"),
]

FALLBACK_QUESTOES = [
    {
        "pergunta": "Fallback: Qual a capital do Brasil?",
        "opcoes": ["Rio", "Brasília", "SP", "BH"],
        "correta": 1,
        "comentario": "Brasília é a capital desde 1960.",
        "topico": "Geral",
    },
    {
        "pergunta": "Fallback: 2 + 2 = ?",
        "opcoes": ["3", "4", "5", "6"],
        "correta": 1,
        "comentario": "Operação básica.",
        "topico": "Raciocínio Lógico",
    },
]

# ================================
# MODELO DE QUESTÃO
# ================================
@dataclass(frozen=True)
class Questao:
    pergunta: str
    opcoes: List[str]
    correta: int
    comentario: str
    topico: str

    @property
    def chave(self) -> Tuple[str, int]:
        return (self.pergunta.strip(), self.correta)

# ================================
# SERVIÇO DE QUESTÕES
# ================================
class QuestaoService:

    def __init__(self, api_url: str):
        self.api_url = api_url
        self.historico_set: Set[Tuple[str, int]] = set()
        self.historico_lista: List[Questao] = []

    def _registrar(self, q: Questao) -> None:
        if q.chave not in self.historico_set:
            self.historico_set.add(q.chave)
            self.historico_lista.append(q)
            if len(self.historico_lista) > 500:
                velho = self.historico_lista.pop(0)
                if velho.chave in self.historico_set:
                    self.historico_set.remove(velho.chave)

    def _chamar_api(self, params: Dict[str, Any]) -> Any:
        for tentativa in range(1, 4):
            try:
                resp = requests.get(self.api_url, params=params, timeout=10)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                logger.warning("Erro API (%d): %s", tentativa, e)
                time_mod.sleep(0.8 * tentativa)
        return None

    def _normalizar(self, dados: Any, topico_padrao: Optional[str]) -> List[Questao]:
        lista = []

        if isinstance(dados, dict):
            if all(k in dados for k in ("pergunta", "opcoes", "correta")):
                dados = [dados]
            elif "result" in dados:
                dados = dados["result"]
            elif "questoes" in dados:
                dados = dados["questoes"]

        if not isinstance(dados, list):
            return []

        for q in dados:
            try:
                pergunta = str(q["pergunta"])
                opcoes = list(q["opcoes"])
                correta = int(q["correta"])
                comentario = q.get("comentario", "")
                topico = q.get("topico", topico_padrao or "Geral")
                questao = Questao(pergunta, opcoes, correta, comentario, topico)
                lista.append(questao)
            except Exception:
                continue

        return lista

    def buscar_lote(self, qtd: int, topico: Optional[str]) -> List[Questao]:
        lote = []
        vistos_local = set()

        for _ in range(qtd * 3):
            params = {"qtd": 1}
            if topico:
                params["topico"] = topico

            dados = self._chamar_api(params)
            if dados:
                questoes = self._normalizar(dados, topico)
            else:
                questoes = []

            if not questoes:
                continue

            for q in questoes:
                if q.chave in vistos_local:
                    continue
                if q.chave in self.historico_set:
                    continue

                vistos_local.add(q.chave)
                lote.append(q)
                self._registrar(q)

                if len(lote) >= qtd:
                    return lote

        # Fallback
        if not lote:
            base = FALLBACK_QUESTOES[:]
            for _ in range(qtd):
                qd = random.choice(base)
                q = Questao(
                    qd["pergunta"], qd["opcoes"], qd["correta"],
                    qd["comentario"], qd["topico"]
                )
                lote.append(q)
                self._registrar(q)

        return lote

questao_service = QuestaoService(QUESTIONS_API_URL)

# ================================
# ENVIO DE QUESTÕES
# ================================
async def enviar_lote(context: ContextTypes.DEFAULT_TYPE, topico: str, origem: str):
    questoes = questao_service.buscar_lote(10, topico)

    for q in questoes:
        await context.bot.send_poll(
            CANAL_ID,
            f"[{q.topico}] {q.pergunta}",
            q.opcoes,
            type="quiz",
            correct_option_id=q.correta,
            explanation=q.comentario,
            is_anonymous=False,
        )

    logger.info("Lote enviado (%s) para %s", origem, topico)

# ================================
# COMANDOS
# ================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teclado = [
        [InlineKeyboardButton(t, callback_data=f"TEMA|{t}")]
        for t in TOPICOS_LISTA
    ]
    await update.message.reply_text(
        "Escolha a matéria:",
        reply_markup=InlineKeyboardMarkup(teclado)
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /start para escolher uma matéria.")

# ================================
# CALLBACKS
# ================================
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    acao, valor = query.data.split("|", 1)

    if acao == "TEMA":
        await query.answer(f"Enviando questões de {valor}...")
        await enviar_lote(context, valor, "manual")

# ================================
# JOBS AUTOMÁTICOS
# ================================
async def job_enviar(context: ContextTypes.DEFAULT_TYPE):
    topico = context.job.data["topico"]
    await enviar_lote(context, topico, "automático")

def configurar_jobs(job_queue: JobQueue):
    for hora, topico in HORARIOS_AUTOMATICOS:
        job_queue.run_daily(
            job_enviar,
            time=hora,
            data={"topico": topico}
        )

# ================================
# APLICAÇÃO
# ================================
def criar_app() -> Application:
    # Criar app
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # >>> CORREÇÃO IMPORTANTE PARA RAILWAY <<<
    app.job_queue.scheduler.configure(timezone=TZ)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))

    configurar_jobs(app.job_queue)

    return app

def main():
    app = criar_app()
    logger.info("BOT ONLINE")
    app.run_polling()

if __name__ == "__main__":
    main()
