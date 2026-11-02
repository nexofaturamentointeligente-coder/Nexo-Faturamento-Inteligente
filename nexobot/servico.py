"""Camada E — motor de decisão: interpretação → resposta → ação autorizada.

As três etapas ficam separadas:
  1. `classificar()` entende a mensagem (não autoriza nada);
  2. este módulo escolhe a resposta (base de conhecimento, Guia, catálogo,
     recusa) e o NÍVEL de profundidade (resumo, explicação, passo a passo,
     aprofundamento), considerando o assunto em foco na conversa;
  3. `acao_navegacao()` monta a única ação possível — navegar/sugerir — a
     partir de uma chave do catálogo fechado, com URL gerada por `url_for()`.

Nenhum caminho daqui altera dados, cria registros ou chama rotas de escrita.
O modelo generativo opcional apenas redige texto e nunca define a ação.
"""

import logging
import re
import time
import uuid

from . import providers, respostas, saber, seguranca
from .classificacao import classificar
from .conhecimento import Resultado, obter_base, tipo_de_pergunta
from .contexto import Contexto
from .intencoes import Intencao, Interpretacao
from .lexico import GENERICOS, STOPWORDS
from .navegacao import FORTE, acao_navegacao, mencoes, opcao_navegacao
from .normalizacao import PONTUACAO, TextoNormalizado, normalizar, sem_acentos
from .respostas import Montagem

logger = logging.getLogger("nexobot")

_NAO_PAGINAS = (
    (re.compile(r"\bnotificac"), "As notificações ficam no sininho, no topo da página."),
    (re.compile(r"\bsenhas?\b"), "Para trocar a senha, use Configurações, no menu lateral. Se não lembra a senha, "
                                 "use “Esqueci minha senha” na tela de login."),
    (re.compile(r"\b(?:sair|logout|deslog\w*)\b"), "Para sair, use “Sair” no fim do menu lateral."),
    (re.compile(r"\b(?:perfil|cadastro|minha conta|meus dados cadastrais)\b"),
     "Nome, telefone e e-mail de acesso ficam em Configurações; para os demais dados cadastrais, abra um "
     "chamado em Suporte."),
)
_SEM_CALCULO = re.compile(r"\b(?:lucro|lucrei|margem|margens|cmv|rentabilidade|prejuizo)\b")

# Nível de resposta pedido pela formulação.
_PEDE_CURTO = re.compile(r"\b(?:resum\w*(?! executiv)|rapid\w*|curt[oa]s?|breve\w*|em uma frase|de forma simples|"
                         r"simplific\w*|por alto|basicamente|em poucas palavras|so o essencial)\b")
_PEDE_PROFUNDO = re.compile(r"\b(?:tudo|todo o|toda a|complet[oa]|detalhad\w*|aprofund\w*|a fundo|em detalhes?|"
                            r"por completo|inteir[oa]|do inicio ao fim|minuciosa\w*|todas as etapas|cada etapa)\b")
_PEDE_PASSOS = re.compile(r"\b(?:passo a passo|na pratica|quais (?:sao )?os passos|como (?:que )?(?:se )?faz\b|"
                          r"como (?:eu |que eu )?(?:faco|fazer|envio|enviar|anexo|anexar|mando|mandar|subo|subir|abro|abrir|"
                          r"exporto|exportar|acesso|acessar|consulto|consultar|reabro|reabrir|recupero|recuperar|troco|trocar|"
                          r"altero|alterar|mudo|mudar|redefino|redefinir|comeco|comecar|inicio|iniciar|entro|entrar|saio|sair|"
                          r"marco|marcar|vejo|ver|localizo|localizar|acompanho|acompanhar|identifico|identificar|preencho|"
                          r"preencher|baixo|baixar|crio|criar|peco|pedir|respondo|responder|gero|gerar|tiro|tirar|extraio|"
                          r"extrair|navego|navegar|uso|usar|utilizo|utilizar|aplico|aplicar|interpreto|interpretar|leio|ler|"
                          r"escolho|escolher|seleciono|selecionar|substituo|substituir|reenvio|reenviar|corrijo|corrigir|"
                          r"recebo|receber|obtenho|obter|consigo|conseguir))\b")
_DEFINICAO = re.compile(r"\b(?:o que (?:eh|e|sao|significa|quer dizer)|significa|significado|defin\w*|conceito)\b")
_FUNCIONAMENTO = re.compile(r"\b(?:funciona|funcionamento|etapas|processo|fluxo|como)\b")
# Palavras que pedem a forma da resposta, não o assunto ("me explica", "em detalhes").
_MODIFICADORES = re.compile(
    r"\b(?:me |por favor |pode |poderia |voce pode )?(?:explica|explique|explicar|detalha|detalhe|detalhar|fala|fale|falar|"
    r"conta|conte|contar|ensina|ensine|ensinar|descreve|descreva|descrever)(?: me| para mim)?(?: sobre| tudo sobre|"
    r" como funciona)?\b"
    r"|\b(?:em detalhes?|com (?:mais )?detalhes|detalhadamente|resumidamente|rapidamente|brevemente|por favor|melhor|"
    r"a fundo|por completo|de forma (?:simples|resumida|detalhada)|em poucas palavras|so o essencial|mais detalhes|"
    r"com calma|direitinho|tudo sobre|um pouco mais sobre|mais sobre)\b"
)
# Palavras de "pedir definição" que não identificam o assunto.
_RUIDO_DEFINICAO = frozenset("significa significado quer dizer definicao conceito seria serve existe vem fica "
                             "entendi entendo entender compreendi compreendo".split())
_PERGUNTA_COMO = re.compile(r"^(?:\w+ ){0,3}como\b")


def limpar_pedido(consulta: str) -> str:
    """Remove da consulta o pedido de forma ("me explica X em detalhes" → "X")."""
    limpa = _MODIFICADORES.sub(" ", consulta or "")
    limpa = " ".join(limpa.split())
    return limpa or consulta


def nivel_pedido(canonico: str, registro) -> int:
    """1 = curto; 2 = explicação; 3 = passo a passo; 4 = aprofundado."""
    if _PEDE_CURTO.search(canonico):
        return 1
    if _PEDE_PROFUNDO.search(canonico):
        return 4
    if registro.passos:
        if _PEDE_PASSOS.search(canonico) or registro.tipo in ("procedimento", "roteiro"):
            return 3
        if registro.tipo == "visao_geral" and _FUNCIONAMENTO.search(canonico):
            return 3
    return 2


# Pares de termos que um registro próprio compara melhor do que uma comparação genérica.
_COMPARACOES_CONHECIDAS = (
    ({"nexo.indicadores.classe_a", "nexo.indicadores.classe_b", "nexo.indicadores.classe_c"},
     "nexo.indicadores.classes_abc"),
    ({"nexo.tecnologia.nexobot_ia", "nexo.suporte.bot_vs_humano"}, "nexo.suporte.bot_vs_humano"),
    ({"nexo.uploads.vendas_vs_compras", "nexo.indicadores.total_comprado"}, "nexo.uploads.vendas_vs_compras"),
    ({"nexo.analises.status_em_analise", "nexo.analises.status_concluido"}, "nexo.analises.processada_vs_publicada"),
    ({"nexo.planos.quais", "nexo.planos.diferenca"}, "nexo.planos.diferenca"),
)


def _registro_que_compara(pares):
    """Registro cujo próprio título contrapõe todos os termos citados ("mensal e quinzenal")."""
    termos = [termo for termo, _ in pares]
    for _, registro in pares:
        titulo = sem_acentos(registro.titulo.casefold())
        if all(re.search(rf"\b{re.escape(t)}\b", titulo) for t in termos):
            return registro
    return None


def responder(mensagem, ultimo_destino=None, contexto=None, pagina_atual=None) -> dict:
    """Resposta completa do NexoBot. Não lança exceção para entradas de usuário."""
    inicio = time.perf_counter()
    correlacao = uuid.uuid4().hex[:12]
    ctx = Contexto.da_requisicao(ultimo_destino, contexto, pagina_atual)
    texto = normalizar(mensagem)
    interpretacao = classificar(texto, ctx)
    saida = _Decisao(interpretacao, texto, ctx, correlacao).executar()
    duracao = (time.perf_counter() - inicio) * 1000
    logger.info(
        "nexobot correlacao=%s intencao=%s confianca=%.2f origem=%s assunto=%s acao=%s ms=%.1f",
        correlacao, saida["meta"]["intencao"], saida["meta"]["confianca"],
        saida["meta"]["origem"], saida["meta"].get("assunto") or "-", saida.get("acao") or "-", duracao,
    )
    return saida


class _Decisao:
    def __init__(self, interpretacao: Interpretacao, texto: TextoNormalizado, ctx: Contexto,
                 correlacao: str):
        self.i = interpretacao
        self.texto = texto
        self.ctx = ctx
        self.correlacao = correlacao
        self.semente = texto.canonico or texto.bruto
        self.degradado = False
        self.origem = "sistema"
        self.intencao = interpretacao.intencao
        self.confianca = interpretacao.confianca
        self.assunto = None
        self.nivel = 0

    # -- montagem --------------------------------------------------------
    def _saida(self, conteudo, acao: str | None = None, destino: str | None = None,
               opcoes=(), contexto: dict | None = None, fonte: str = "local") -> dict:
        montagem = conteudo if isinstance(conteudo, Montagem) else respostas.simples(conteudo)
        if self.i.saudacao and self.intencao not in (Intencao.SAUDACAO, Intencao.NAVEGAR) and montagem.texto:
            montagem = respostas.com_prefixo(montagem, "Olá! ")
        saida = {
            "resposta": seguranca.texto_resposta(montagem.texto),
            "blocos": montagem.blocos,
            "sugestoes": montagem.sugestoes,
            "fonte": fonte, "acao": None, "destino": None,
        }
        if acao and destino:
            saida.update(acao_navegacao(acao, destino))
        botoes = [b for b in (opcao_navegacao(o) for o in opcoes) if b]
        if botoes:
            saida["opcoes"] = botoes
        saida["contexto"] = contexto if contexto is not None else self.ctx.avancar()
        saida["meta"] = {
            "intencao": self.intencao.value,
            "confianca": round(float(self.confianca), 2),
            "origem": self.origem,
            "motivo": self.i.motivo,
            "correlacao": self.correlacao,
            "assunto": self.assunto,
            "nivel": self.nivel,
        }
        if self.degradado:
            saida["meta"]["degradado"] = True
        return saida

    def _base(self):
        """Base de conhecimento; falhas inesperadas viram modo degradado registrado."""
        try:
            base = obter_base()
        except Exception:  # noqa: BLE001 — registrado com traceback, sem dados do usuário
            logger.exception("NexoBot: falha ao montar a base de conhecimento (correlacao=%s).",
                             self.correlacao)
            self.degradado = True
            return None
        self.degradado = base.degradado
        return base

    # -- roteamento por intenção -------------------------------------------
    def executar(self) -> dict:
        tratador = getattr(self, f"_{self.intencao.value}", None) or self._informativa
        return tratador()

    def _vazia(self):
        return self._saida(respostas.simples(respostas.VAZIA, respostas.SUGESTOES_INICIAIS),
                           contexto=self.ctx.manter())

    def _sem_texto(self):
        return self._saida(respostas.SEM_TEXTO, contexto=self.ctx.manter())

    def _saudacao(self):
        return self._saida(respostas.saudacao(self.semente), contexto=self.ctx.manter())

    def _despedida(self):
        return self._saida(respostas.despedida(self.semente), contexto=self.ctx.manter())

    def _ajuda_geral(self):
        return self._saida(respostas.ajuda_geral(), contexto=self.ctx.manter())

    def _confirmacao(self):
        return self._saida(respostas.simples(respostas.CONFIRMACAO_SEM_CONTEXTO, respostas.SUGESTOES_INICIAIS))

    def _recusa(self):
        return self._saida(respostas.RECUSA)

    def _comando_negado(self):
        return self._saida(respostas.NEGADA)

    def _navegar(self):
        self.origem = "catalogo"
        destino = self.i.destino
        return self._saida(respostas.navegando(destino), "navegar", destino,
                           contexto=self.ctx.avancar(destino))

    def _pagina_atual(self):
        self.origem = "catalogo"
        return self._saida(respostas.pagina_atual(self.i.destino), contexto=self.ctx.avancar(self.i.destino))

    def _ambiguidade(self):
        motivo = self.i.motivo
        if motivo == "relatorio_ambiguo":
            opcoes = self.i.opcoes
            return self._saida(respostas.esclarecer_relatorio(), opcoes=opcoes,
                               contexto=self.ctx.avancar(opcoes=opcoes, modo_opcoes="orientar"))
        if self.i.opcoes:
            return self._saida(respostas.esclarecer_destinos(self.i.opcoes, self.i.modo_opcoes),
                               opcoes=self.i.opcoes,
                               contexto=self.ctx.avancar(opcoes=self.i.opcoes, modo_opcoes=self.i.modo_opcoes))
        if motivo == "pergunta_incompleta":
            return self._saida(respostas.simples(respostas.PERGUNTA_INCOMPLETA, respostas.SUGESTOES_INICIAIS))
        if any(p in {"portal", "sistema", "site", "plataforma"} for p in self.texto.palavras):
            return self._saida(respostas.LUGAR_GENERICO)
        return self._saida(respostas.REFERENCIA_SEM_CONTEXTO)

    def _operacao_nao_permitida(self):
        texto, sugestao = respostas.operacao(self.i.operacoes, self.texto.canonico)
        destino = self.i.destino or sugestao
        if destino == self.ctx.pagina_atual:
            destino = None
        # A recusa fica ligada à regra que a explica: "explique melhor" aprofunda o porquê.
        chamado = re.search(r"\b(?:chamados?|tickets?)\b", self.texto.canonico)
        regra = saber.POR_ID["nexo.suporte.bot_cria_chamado" if chamado else "nexo.admin.nao_posso_executar"]
        montagem = respostas.simples(texto, [respostas.sugestao(respostas._rotulo_curto(regra), regra.titulo)])
        self.assunto, self.nivel = regra.id, 1
        if destino:
            return self._saida(montagem, "sugerir_navegacao", destino,
                               contexto=self.ctx.avancar(destino, oferecido=True, assunto=regra.id, nivel=1))
        return self._saida(montagem, contexto=self.ctx.avancar(assunto=regra.id, nivel=1))

    def _acesso_restrito(self):
        if self.i.motivo == "admin":
            regra = saber.POR_ID["nexo.admin.area_restrita"]
            chips = [respostas.sugestao("O que faz o administrador?", "O que faz o administrador?"),
                     respostas.sugestao("Posso pedir ações ao NexoBot?", saber.POR_ID["nexo.admin.nao_posso_executar"].titulo)]
            self.assunto, self.nivel = regra.id, 1
            return self._saida(respostas.simples(respostas.ACESSO_ADMIN, chips),
                               contexto=self.ctx.avancar(assunto=regra.id, nivel=1))
        return self._saida(respostas.ACESSO_URL)

    def _pagina_inexistente(self):
        dicas = [dica for padrao, dica in _NAO_PAGINAS if padrao.search(self.texto.canonico)]
        texto = respostas.PAGINA_INEXISTENTE + (" " + dicas[0] if dicas else "")
        return self._saida(texto)

    def _fora_do_escopo(self):
        if re.search(r"\b(?:acoes|acao|bolsa|investir|investimentos?|bitcoin|cripto\w*)\b", self.texto.canonico):
            return self._saida(respostas.INVESTIMENTO)
        return self._saida(respostas.variante(respostas.FORA_ESCOPO, self.semente))

    def _entrada_suspeita(self):
        texto = respostas.INJECAO if self.i.motivo == "injecao" else respostas.CODIGO
        return self._saida(texto)

    def _consulta_dados(self):
        if self.i.motivo == "outra_empresa":
            return self._saida(respostas.OUTRA_EMPRESA)
        texto = respostas.DADOS
        if _SEM_CALCULO.search(self.texto.canonico):
            texto += respostas.DADOS_NAO_CALCULADOS
        chips = [respostas.sugestao("Entender meus indicadores", "Como interpreto os meus indicadores?")]
        return self._sugerir(respostas.simples(texto, chips), "dashboard")

    def _suporte_humano(self):
        chips = [respostas.sugestao("Como abro um chamado?", "Como abro um chamado para a equipe NEXO?")]
        return self._sugerir(respostas.simples(respostas.SUPORTE_HUMANO, chips), "suporte")

    def _sobre_o_bot(self):
        return self._saida(respostas.SOBRE_O_BOT)

    def _sugerir(self, conteudo, destino: str | None, assunto: str | None = None, nivel: int = 0):
        if not destino:
            return self._saida(conteudo, contexto=self.ctx.avancar(assunto=assunto, nivel=nivel)
                               if assunto else None)
        self.origem = self.origem if self.origem != "sistema" else "catalogo"
        if destino == self.ctx.pagina_atual:
            # Já está na página: explica sem oferecer botão para ela mesma.
            return self._saida(conteudo, contexto=self.ctx.avancar(destino, assunto=assunto, nivel=nivel))
        return self._saida(conteudo, "sugerir_navegacao", destino,
                           contexto=self.ctx.avancar(destino, oferecido=not assunto, assunto=assunto, nivel=nivel))

    # -- base de conhecimento (saber) -------------------------------------
    def _responder_registro(self, registro, nivel: int | None = None, ja_mostrado: int = 0,
                            modo: str | None = None, confianca: str = "alta", alternativas=(),
                            destino_forte: str | None = None, destino_fraco: str | None = None,
                            lead: str | None = None):
        """Resposta de um registro da base interna no nível adequado, com página relacionada.

        A página nomeada pelo usuário (`destino_forte`) prevalece sobre a página do
        registro; uma página apenas inferida (`destino_fraco`) só entra na falta das duas.
        """
        # Pedido de mudar o que a própria pessoa altera: o caminho é Configurações, não o chamado nem a senha.
        if (registro.id in ("nexo.empresas.alterar_cadastro", "nexo.acesso.recuperar_senha")
                and respostas.pede_alterar_a_conta(self.texto.canonico)):
            registro = saber.POR_ID["nexo.acesso.configuracoes"]
        nivel = nivel or nivel_pedido(self.texto.canonico, registro)
        montagem = respostas.conhecimento(registro, nivel, ja_mostrado, modo)
        if lead:
            montagem.blocos.insert(0, respostas.b_texto(lead))
            montagem.texto = respostas.texto_de(montagem.blocos)
        if confianca == "media":
            aviso = "Encontrei esta orientação, que parece ser o que você procura:"
            montagem.blocos.insert(0, respostas.b_texto(aviso))
            montagem.texto = respostas.texto_de(montagem.blocos)
        for alternativa in alternativas:
            if alternativa.registro is not None and alternativa.registro.id != registro.id:
                chip = respostas.sugestao(respostas._rotulo_curto(alternativa.registro), alternativa.registro.titulo)
                if chip not in montagem.sugestoes and len(montagem.sugestoes) < respostas.MAXIMO_SUGESTOES:
                    montagem.sugestoes.append(chip)
        mostrado = max(nivel, ja_mostrado) if modo is None else max(ja_mostrado, 1)
        self.origem = "portal"
        self.assunto, self.nivel = registro.id, mostrado
        if self.intencao == Intencao.DESCONHECIDA:
            self.intencao = Intencao.DUVIDA_FUNCIONAL
        destino = destino_forte or registro.destino or destino_fraco
        if registro.id == "nexo.acesso.configuracoes":
            # O que este registro descreve acontece em Configurações, mesmo se a pergunta citar chamados.
            destino = registro.destino
        return self._sugerir(montagem, destino, assunto=registro.id, nivel=mostrado)

    def _sobre_o_nexo(self):
        registro = saber.por_id(self.i.detalhes.get("registro")) or saber.POR_ID["nexo.visao_geral.funcionamento"]
        return self._responder_registro(registro)

    def _aprofundar(self):
        registro, pedido, ja = self.ctx.registro, self.i.motivo, self.ctx.nivel
        if registro is None:
            return self._saida(respostas.simples(respostas.APROFUNDAR_SEM_ASSUNTO, respostas.SUGESTOES_INICIAIS))
        if pedido == "explicar":
            novo = 2 if ja < 2 else 4
            return self._responder_registro(registro, nivel=novo, ja_mostrado=ja)
        if pedido == "exemplo":
            return self._responder_registro(registro, nivel=max(ja, 1), ja_mostrado=ja, modo="exemplo")
        if pedido == "passos":
            if registro.passos:
                return self._responder_registro(registro, nivel=3, ja_mostrado=min(ja, 2))
            texto = (f"Não há um passo a passo para “{registro.titulo}”: é um conceito. "
                     "Posso explicar com mais detalhes ou seguir para um assunto relacionado.")
            montagem = respostas.simples(texto, respostas.sugestoes_relacionadas(registro))
            return self._sugerir(montagem, registro.destino, assunto=registro.id, nivel=ja)
        if pedido == "resumir":
            return self._responder_registro(registro, nivel=1, ja_mostrado=ja, modo="resumo")
        if pedido == "nao_entendi":
            return self._responder_registro(registro, nivel=max(ja, 1), ja_mostrado=ja, modo="simplificar")
        if pedido == "proximo":
            alvo = saber.por_id(registro.proximo) or next(
                (saber.por_id(r) for r in registro.relacionados if saber.por_id(r)), None)
            if alvo is None:
                texto = "Esse era o último passo desse assunto. Posso ajudar com outro tema:"
                return self._saida(respostas.simples(texto, respostas.SUGESTOES_INICIAIS),
                                   contexto=self.ctx.manter())
            return self._responder_registro(alvo, nivel=max(2, nivel_pedido("", alvo)),
                                            lead="Próximo passo:")
        if pedido == "onde":
            if registro.destino:
                oferecer = registro.destino != self.ctx.pagina_atual
                texto = respostas.orientacao(registro.destino, self.semente, oferecer=oferecer)
                self.origem = "catalogo"
                self.assunto, self.nivel = registro.id, ja
                return self._saida(texto, "sugerir_navegacao" if oferecer else None, registro.destino,
                                   contexto=self.ctx.avancar(registro.destino, oferecido=True,
                                                             assunto=registro.id, nivel=ja))
            montagem = respostas.simples(respostas.SEM_PAGINA_DO_ASSUNTO, respostas.sugestoes_relacionadas(registro))
            return self._saida(montagem, contexto=self.ctx.manter())
        return self._informativa()

    def _comparacao(self):
        pares = []
        vistos = set()
        for termo, rid in self.i.detalhes.get("pares", ()):
            registro = saber.por_id(rid)
            if registro is None or rid in vistos:
                continue
            vistos.add(rid)
            pares.append((termo, registro))
        if not pares:
            return self._informativa()
        ids = {r.id for _, r in pares}
        for conjunto, alvo in _COMPARACOES_CONHECIDAS:
            if ids <= conjunto and saber.por_id(alvo):
                return self._responder_registro(saber.POR_ID[alvo], nivel=2)
        proprio = _registro_que_compara(pares)
        if proprio is not None or len(pares) == 1:
            return self._responder_registro(proprio or pares[0][1], nivel=2)
        montagem = respostas.comparacao(pares)
        self.origem = "portal"
        self.assunto, self.nivel = pares[0][1].id, 2
        return self._saida(montagem, contexto=self.ctx.avancar(assunto=pares[0][1].id, nivel=2))

    # -- perguntas e orientação ------------------------------------------
    def _informativa(self):
        i = self.i
        if len(i.detalhes.get("partes", ())) >= 2:
            return self._varias_perguntas(i.detalhes["partes"])
        if i.opcoes:
            self.origem = "catalogo"
            return self._saida(respostas.orientacao_varios(i.opcoes), opcoes=i.opcoes,
                               contexto=self.ctx.avancar(opcoes=i.opcoes, modo_opcoes="orientar"))
        destino_forte = i.destino if i.forca_destino >= FORTE else None
        base = self._base()
        # "Onde fica X?" / "como acesso X?" com a página nomeada; uma simples menção
        # ("nunca enviei um relatório") não é pedido de localização.
        localizacao = bool(i.intencao == Intencao.ORIENTACAO_NAVEGACAO and destino_forte
                           and i.motivo != "mencao_de_destino")
        consulta = i.consulta or self.texto.canonico
        consulta_busca = limpar_pedido(consulta)

        # Definição direta pelo glossário ("o que é faturamento?", "classe A"). Palavras que
        # apenas nomeiam uma página ("os semáforos do painel") situam, não definem o assunto.
        de_paginas = {palavra for m in mencoes(consulta_busca) for palavra in consulta_busca[m.inicio:m.fim].split()}
        palavras_todas = [p for p in consulta_busca.split() if p not in PONTUACAO and p not in STOPWORDS
                          and p not in GENERICOS and p not in _RUIDO_DEFINICAO]
        palavras = [p for p in palavras_todas if p not in de_paginas]
        tipo = tipo_de_pergunta(consulta_busca)
        definicional = bool(_DEFINICAO.search(consulta)) or tipo in (None, "oque")
        direto, termo = None, ""
        if not localizacao and (_DEFINICAO.search(consulta) or len(palavras_todas) <= 2):
            par = saber.termo_definido(consulta_busca)
            if par:
                termo, direto = par[0], saber.por_id(par[1])

        resultado = self._buscar(base, destino_forte, consulta_busca)
        topo = resultado.trecho if resultado and resultado.trecho else None
        guia_forte = bool(topo and topo.origem == "guia" and resultado.nivel == "alta")
        if direto is not None and not guia_forte:
            # O glossário decide quando a busca não tem resposta forte, quando aponta para
            # o mesmo registro ou quando a pergunta é apenas a definição do termo. Nos
            # demais casos ("quem controla os planos?") a busca é mais específica.
            busca_forte = bool(topo and topo.registro is not None and resultado.nivel == "alta")
            mesmo = bool(topo and topo.registro is not None and topo.registro.id == direto.id)
            cobre = set(palavras) <= set(termo.split())
            if not busca_forte or mesmo or (cobre and definicional):
                return self._responder_registro(direto, destino_forte=destino_forte, destino_fraco=i.destino)
        if (topo and topo.origem == "portal" and i.destino and not destino_forte and direto is None
                and len(palavras_todas) <= 1 and not _DEFINICAO.search(consulta)):
            # "A tela em que vejo meus dados": uma única palavra comum, que só sugere a
            # página, não comprova o assunto; a orientação da página responde melhor.
            # Com a página nomeada ("como abro um chamado?"), a decisão fica abaixo.
            resultado, topo = None, None

        if (resultado and resultado.trecho and resultado.ambiguo and resultado.trecho.origem == "guia"
                and not localizacao):
            self.origem = "guia"
            self.intencao = Intencao.AMBIGUIDADE
            self.confianca = resultado.confianca
            trechos = (resultado.trecho, *resultado.alternativas)
            return self._saida(respostas.topicos_ambiguos(trechos))

        usar_trecho = bool(topo) and (
            not localizacao
            or (topo.origem == "guia" and resultado.nivel == "alta" and topo.destino in (None, destino_forte))
            # "Como abro um chamado?": o passo a passo da base responde melhor do que
            # só apontar a página; "onde fica X?" continua com a orientação do catálogo.
            or (topo.registro is not None and resultado.nivel == "alta"
                and topo.destino in (None, destino_forte) and _PERGUNTA_COMO.match(consulta)))
        if usar_trecho:
            trecho = resultado.trecho
            self.confianca = max(self.confianca, resultado.confianca)
            if trecho.registro is not None:
                return self._responder_registro(trecho.registro, confianca=resultado.nivel,
                                                alternativas=resultado.alternativas,
                                                destino_forte=destino_forte, destino_fraco=i.destino)
            self.origem = trecho.origem
            if self.intencao == Intencao.DESCONHECIDA:
                self.intencao = Intencao.DUVIDA_FUNCIONAL
            fonte, montagem = "local", respostas.topico(trecho, resultado.nivel)
            if providers.configurado():
                redigido = providers.redigir(self.texto.original, (trecho, *resultado.alternativas))
                if redigido:
                    fonte, montagem, self.origem = "hf", respostas.simples(redigido), "ia"
            destino = destino_forte or trecho.destino or (
                i.destino if i.intencao == Intencao.ORIENTACAO_NAVEGACAO else None)
            return self._sugerir_com(montagem, destino, fonte)

        destino = destino_forte or (i.destino if i.intencao == Intencao.ORIENTACAO_NAVEGACAO else None)
        if destino:
            self.origem = "catalogo"
            if self.intencao == Intencao.DESCONHECIDA:
                self.intencao = Intencao.ORIENTACAO_NAVEGACAO
            oferecer = destino != self.ctx.pagina_atual
            texto = respostas.orientacao(destino, self.semente, oferecer=oferecer)
            return self._sugerir(texto, destino)

        # Último recurso: um único termo do glossário citado na pergunta.
        tardio = saber.por_id(saber.definicao_direta(consulta))
        if tardio is not None:
            return self._responder_registro(tardio, confianca="media")

        if self.degradado:
            return self._saida(respostas.GUIA_INDISPONIVEL, opcoes=("suporte",))
        if self.intencao == Intencao.DESCONHECIDA:
            return self._saida(respostas.desconhecida(self.semente))
        self.confianca = min(self.confianca, 0.4)
        return self._saida(respostas.sem_informacao(), opcoes=("guia", "suporte"))

    def _varias_perguntas(self, partes):
        """Responde até duas perguntas independentes da mesma mensagem."""
        base = self._base()
        montagens, destinos, origens, assunto = [], [], [], None
        for parte in partes[:2]:
            destino_forte = parte["destino"] if parte["forca"] >= FORTE else None
            resultado = self._buscar(base, destino_forte, limpar_pedido(parte["consulta"]))
            trecho = resultado.trecho if resultado and not resultado.ambiguo else None
            if trecho is None and not parte["localizacao"]:
                rid = saber.definicao_direta(limpar_pedido(parte["consulta"]))
                trecho = base.por_id(rid) if (base and rid) else None
            if trecho and parte["localizacao"] and destino_forte and not (
                    trecho.origem == "guia" and resultado and resultado.nivel == "alta"):
                trecho = None  # "onde fica X?": a orientação do catálogo responde melhor
            if trecho and trecho.registro is not None:
                montagens.append(respostas.conhecimento(trecho.registro, 2, compacto=True))
                destinos.append(destino_forte or trecho.destino)
                origens.append("portal")
                assunto = assunto or trecho.registro.id
            elif trecho:
                montagens.append(respostas.topico(trecho, resultado.nivel))
                destinos.append(destino_forte or trecho.destino)
                origens.append(trecho.origem)
            elif destino_forte:
                montagens.append(respostas.simples(respostas.orientacao(destino_forte, self.semente, oferecer=False)))
                destinos.append(destino_forte)
                origens.append("catalogo")
            else:
                montagens.append(respostas.simples(respostas.PARTE_SEM_INFORMACAO))
        self.origem = origens[0] if origens else "sistema"
        if assunto:
            self.assunto, self.nivel = assunto, 1
            if self.intencao == Intencao.DESCONHECIDA:
                self.intencao = Intencao.DUVIDA_FUNCIONAL
        destinos = [d for d in dict.fromkeys(destinos) if d and d != self.ctx.pagina_atual]
        montagem = respostas.varias(montagens)
        if len(destinos) > 1:
            return self._saida(montagem, opcoes=destinos,
                               contexto=self.ctx.avancar(opcoes=destinos, modo_opcoes="orientar",
                                                         assunto=assunto, nivel=1 if assunto else 0))
        return self._sugerir(montagem, destinos[0] if destinos else None, assunto=assunto,
                             nivel=1 if assunto else 0)

    def _sugerir_com(self, conteudo, destino: str | None, fonte: str):
        saida = self._sugerir(conteudo, destino)
        saida["fonte"] = fonte
        return saida

    def _buscar(self, base, destino_forte, consulta: str | None = None) -> Resultado | None:
        if base is None:
            return None
        consulta = consulta or self.i.consulta or self.texto.canonico
        palavras = [p for p in consulta.split() if p not in PONTUACAO and p not in STOPWORDS]
        if not palavras:
            return None
        try:
            return base.buscar(palavras, destino_forte, tipo_de_pergunta(consulta))
        except Exception:  # noqa: BLE001 — dado inesperado no índice; registrado e degradado
            logger.exception("NexoBot: falha na busca do Guia (correlacao=%s).", self.correlacao)
            self.degradado = True
            return None
