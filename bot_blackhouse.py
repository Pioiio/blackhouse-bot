"""
Bot Black House – versão inteligente e precavida

Principais características:
- Validação de configuração (envs obrigatórias)
- Camada de serviço para comunicação com a API
- Tratamento robusto de erros e timeouts
- Evita questões repetidas no mesmo lote e ao longo do tempo (cache em memória)
- Fallback local se a API falhar
- JobQueue com timezone America/Sao_Paulo
"""

from __future__ import annotations

import logging
import os
import random
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, time
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
    "QUESTIONS_API_URL",
    "https://blackhouse-api-production.up.railway.app/questoes",
).strip()

CANAL_ID = os.getenv("CANAL_ID", "@BLACKHOUSE_CONCURSOS").strip()

TZ = pytz.timezone("America/Sao_Paulo")

TOPICOS_LISTA: List[str] = [
    "Penal",
    "Constitucional",
    "Raciocínio Lógico",
    "Processo Penal",
    "Direitos Humanos",
]

# horários automáticos para envio em canal
HORARIOS_AUTOMATICOS: List[Tuple[time, str]] = [
    (time(8, 0), "Penal"),
    (time(13, 0), "Constitucional"),
    (time(19, 0), "Raciocínio Lógico"),
]

# Fallback local se a API estiver fora
FALLBACK_QUESTOES: List[Dict[str, Any]] = [
    {
        "pergunta": "Fallback: Qual a capital do Brasil?",
        "opcoes": ["Rio de Janeiro", "Brasília", "São Paulo", "Belo Horizonte"],
        "correta": 1,
        "comentario": "Brasília é a capital federal desde 1960.",
        "topico": "Geral",
    },
    {
        "pergunta": "Fallback: 2 + 2 é igual a?",
        "opcoes": ["1", "2", "3", "4"],
        "correta": 3,
        "comentario": "Operação básica de adição.",
        "topico": "Raciocínio Lógico",
    },
]

# ================================
# MODELOS E SERVIÇOS
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
        """Usada para detectar repetição."""
        return (self.pergunta.strip(), self.correta)


class QuestaoService:
    """
    Serviço responsável por buscar e filtrar questões.
    - Faz chamadas resilientes à API.
    - Normaliza o JSON recebido.
    - Evita repetição usando um cache em memória.
    """

    def __init__(
        self,
        api_url: str,
        historico_limite: int = 500,
        timeout: int = 10,
        max_retries: int = 3,
        backoff_base: float = 0.7,
    ) -> None:
        self.api_url = api_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base

        self._historico: List[Questao] = []
        self._historico_set: Set[Tuple[str, int]] = set()
        self._historico_limite = historico_limite

    # ---------- utils internos ----------

    def _registrar_no_historico(self, q: Questao) -> None:
        if q.chave in self._historico_set:
            return
        self._historico.append(q)
        self._historico_set.add(q.chave)

        # mantem histórico limitado (para não explodir memória)
        if len(self._historico) > self._historico_limite:
            antigos = self._historico[: len(self._historico) - self._historico_limite]
            for aq in antigos:
                self._historico_set.discard(aq.chave)
            self._historico = self._historico[-self._historico_limite :]

    def _ja_foi_enviada_recentemente(self, q: Questao) -> bool:
        return q.chave in self._historico_set

    # ---------- chamada de API com robustez ----------

    def _chamar_api_bruto(self, params: Dict[str, Any]) -> Any:
        if not self.api_url:
            logger.warning("QUESTIONS_API_URL não configurada. Pulando chamada à API.")
            return None

        for tentativa in range(1, self.max_retries + 1):
            try:
                logger.info("Chamando API: %s params=%s (tentativa %d)", self.api_url, params, tentativa)
                resp = requests.get(self.api_url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                logger.warning("Erro na chamada à API (%d/%d): %s", tentativa, self.max_retries, e)
                if tentativa < self.max_retries:
                    espera = self.backoff_base * (2 ** (tentativa - 1))
                    time_mod.sleep(espera)
                else:
                    return None

    def _normalizar_resposta(self, dados: Any, topico_padrao: Optional[str]) -> List[Questao]:
        questoes: List[Questao] = []

        # se vier um único dict
        if isinstance(dados, dict):
            if all(k in dados for k in ("pergunta", "opcoes", "correta")):
                dados = [dados]
            elif "result" in dados and isinstance(dados["result"], list):
                dados = dados["result"]
            elif "questoes" in dados and isinstance(dados["questoes"], list):
                dados = dados["questoes"]
            else:
                # estrutura inesperada
                return []

        if not isinstance(dados, list):
            return []

        for q in dados:
            if not isinstance(q, dict):
                continue
            if not all(k in q for k in ("pergunta", "opcoes", "correta")):
                continue

            opcoes = q.get("opcoes")
            if not isinstance(opcoes, list) or len(opcoes) < 2:
                continue

            try:
                correta = int(q.get("correta"))
            except Exception:
                continue

            comentario = str(q.get("comentario", "") or "")
            topico = str(q.get("topico") or topico_padrao or "Geral")

            questao = Questao(
                pergunta=str(q.get("pergunta")),
                opcoes=[str(o) for o in opcoes],
                correta=correta,
                comentario=comentario,
                topico=topico,
            )
            questoes.append(questao)

        return questoes

    # ---------- API pública do serviço ----------

    def buscar_lote(
        self,
        qtd: int,
        topico: Optional[str] = None,
        evitar_repetidas: bool = True,
    ) -> List[Questao]:
        """
        Retorna até `qtd` questões, tentando:
        1. Puxar da API várias vezes (1 por vez para forçar variedade).
        2. Evitar questões repetidas (no lote e no histórico recente).
        3. Se nada funcionar, cair para fallback local.
        """
        if qtd <= 0:
            return []

        lote: List[Questao] = []
        vistos_local: Set[Tuple[str, int]] = set()
        tentativas = 0
        tentativas_max = qtd * 4  # agressivo para tentar variedade

        while len(lote) < qtd and tentativas < tentativas_max:
            tentativas += 1

            params: Dict[str, Any] = {"qtd": 1}
            # IMPORTANTE: se a API usar outro nome (ex: "materia"), trocar aqui
            if topico:
                params["topico"] = topico

            dados = self._chamar_api_bruto(params)
            if not dados:
                break

            questoes_api = self._normalizar_resposta(dados, topico)
            if not questoes_api:
                continue

            for q in questoes_api:
                # evita repetição dentro do mesmo lote
                if q.chave in vistos_local:
                    continue
                # evita repetição recente entre lotes
                if evitar_repetidas and self._ja_foi_enviada_recentemente(q):
                    continue

                vistos_local.add(q.chave)
                lote.append(q)
                self._registrar_no_historico(q)

                if len(lote) >= qtd:
                    break

        if lote:
            random.shuffle(lote)
            return lote[:qtd]

        # nada da API → fallback
        logger.warning("API não retornou questões válidas. Usando fallback local.")
        return self._buscar_lote_fallback(qtd, topico)

    def _buscar_lote_fallback(self, qtd: int, topico: Optional[str]) -> List[Questao]:
        base = FALLBACK_QUESTOES[:]
        if topico:
            filtradas = [q for q in base if q.get("topico") == topico]
            if filtradas:
                base = filtradas

        lote: List[Questao] = []
        vistos_local: Set[Tuple[str, int]] = set()

        while len(lote) < qtd and base:
            q_raw = random.choice(base)
            q = Questao(
                pergunta=q_raw["pergunta"],
                opcoes=q_raw["opcoes"],
                correta=int(q_raw["correta"]),
                comentario=q_raw.get("comentario", ""),
                topico=q_raw.get("topico", topico or "Geral"),
            )
            if q.chave in vistos_local:
                continue
            vistos_local.add(q.chave)
            lote.append(q)
            self._registrar_no_historico(q)

        return lote


# Instância global do serviço
questao_service = QuestaoService(api_url=QUESTIONS_API_URL)


# ================================
# FUNÇÕES DE ENVIO
# ================================

async def enviar_lote_para_canal(
    context: ContextTypes.DEFAULT_TYPE,
    topico: str,
    origem: str,
    qtd: int = 10,
) -> None:
    logger.info("Iniciando envio de lote (%s) – tópico: %s", origem, topico)

    questoes = questao_service.buscar_lote(qtd=qtd, topico=topico)

    if not questoes:
        logger.error("Nenhuma questão obtida para o tópico '%s'.", topico)
        await context.bot.send_message(
            chat_id=CANAL_ID,
            text=f"⚠️ Não consegui carregar questões de *{topico}* agora. Tente novamente mais tarde.",
            parse_mode="Markdown",
        )
        return

    for q in questoes:
        try:
            await context.bot.send_poll(
                chat_id=CANAL_ID,
                question=f"[{q.topico}] {q.pergunta}",
                options=q.opcoes,
                type="quiz",
                correct_option_id=q.correta,
                explanation=q.comentario or None,
                is_anonymous=False,
            )
        except Exception as e:
            logger.error("Erro ao enviar poll para o canal: %s", e)

    logger.info("Envio de lote concluído (%s) – tópico: %s (total=%d)", origem, topico, len(questoes))


# ================================
# HANDLERS DE COMANDO
# ================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.info("/start chamado por %s (%s)", user.id if user else "?", user.username if user else "?")

    teclado = [
        [InlineKeyboardButton(text=topico, callback_data=f"TEMA|{topico}")]
        for topico in TOPICOS_LISTA
    ]
    markup = InlineKeyboardMarkup(teclado)

    texto = (
        "👊 *Black House Bot*\n\n"
        "Escolha a matéria para mandar um lote de questões no canal."
    )

    await update.message.reply_text(texto, reply_markup=markup, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    texto = (
        "ℹ️ *Comandos disponíveis*\n\n"
        "/start – escolher matéria e enviar questões\n"
        "/help – exibe esta ajuda\n"
        "\n"
        "O envio automático é feito direto no canal nos horários configurados."
    )
    await update.message.reply_text(texto, parse_mode="Markdown")


# ================================
# HANDLER DE CALLBACK (INLINE BUTTONS)
# ================================

async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    try:
        acao, valor = query.data.split("|", 1)
    except Exception:
        await query.answer("Formato de ação inválido.", show_alert=True)
        return

    if acao == "TEMA":
        topico = valor
        if topico not in TOPICOS_LISTA:
            await query.answer("Matéria inválida.", show_alert=True)
            return

        await query.answer(f"Enviando questões de {topico} no canal...")
        await enviar_lote_para_canal(context, topico=topico, origem="manual")
    else:
        await query.answer("Ação desconhecida.", show_alert=True)


# ================================
# JOBS AUTOMÁTICOS
# ================================

async def job_enviar_lote(context: ContextTypes.DEFAULT_TYPE) -> None:
    dados = context.job.data or {}
    topico = dados.get("topico") or "Geral"
    await enviar_lote_para_canal(context, topico=topico, origem="automático")


def configurar_jobs(job_queue: JobQueue) -> None:
    for hora, topico in HORARIOS_AUTOMATICOS:
        logger.info("Agendando envio automático – %s às %s", topico, hora)
        job_queue.run_daily(
            job_enviar_lote,
            time=hora,
            data={"topico": topico},
            name=f"auto_{topico}",
        )


# ================================
# ERRO GLOBAL
# ================================

async def erro_global(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exceção não tratada: %s", context.error, exc_info=context.error)


# ================================
# VALIDAÇÃO DE CONFIG E MAIN
# ================================

def validar_config() -> None:
    problemas = []

    if not TELEGRAM_TOKEN:
        problemas.append("TELEGRAM_TOKEN não definido.")
    if not CANAL_ID:
        problemas.append("CANAL_ID não definido.")

    if problemas:
        msg = "Configuração inválida:\n- " + "\n- ".join(problemas)
        logger.critical(msg)
        raise RuntimeError(msg)

    logger.info("Configuração validada com sucesso.")
    logger.info("API de questões: %s", QUESTIONS_API_URL or "(não configurada)")
    logger.info("Canal de envio: %s", CANAL_ID)


def criar_aplicacao() -> Application:
    validar_config()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .timezone(TZ)
        .build()
    )

    # handlers de comando
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    # callbacks de botões
    app.add_handler(CallbackQueryHandler(cb_router))

    # handler global de erros
    app.add_error_handler(erro_global)

    # jobs automáticos
    configurar_jobs(app.job_queue)

    return app


def main() -> None:
    logger.info("Iniciando Black House Bot (inteligente)...")
    app = criar_aplicacao()
    logger.info("Bot em modo polling.")
    app.run_polling()


if __name__ == "__main__":
    main()
