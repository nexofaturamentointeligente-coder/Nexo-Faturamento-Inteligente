"""Raio-X de entrada no portal: recebe os arquivos, grava o resultado e decide o que cada
plano vê. O cálculo fica em raio_x.py, no processo isolado do ETL (etl_runtime.run_raio_x).

  - Os arquivos vão para um diretório temporário com o mesmo teto de tamanho do upload,
    são validados e lidos no processo isolado e somem com o diretório: só o resultado
    (agregados e listas resumidas) fica no banco, uma linha por empresa, mais o retrato
    mensal da evolução (Etapa 3, diagnostico_fechamento: só agregados, um por mês).
  - visao() devolve uma cópia do resultado só com o que o plano libera. O dado protegido
    vira None, então nem o HTML nem o JSON levam o valor: o desfoque da tela é desenho
    sobre um marcador, não um filtro sobre o número verdadeiro.
"""

from __future__ import annotations

import copy
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
from weakref import WeakKeyDictionary

from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.utils import secure_filename

from etl_erros import ETLInputError
from etl_limits import get_limits
import etl_runtime
import evolucao
from extensions import db
from horario import agora_utc, instante_utc
from models import DiagnosticoEntrada, DiagnosticoFechamento
import raio_x_regras
import security_gatekeeper

logger = logging.getLogger(__name__)

_BLOCO = 64 * 1024
# Formatos do Raio-X e da Avaliação de Cenário: os arquivos são lidos no processo isolado e descartados.
# O .xls é o do Excel 97-2003 e o .xml, a Planilha XML 2003 (security_gatekeeper, leitura direta).
EXTENSOES_ACEITAS = ("csv", "xlsx", "xls", "xml")
CAMPOS_MARGEM = ("custo_total", "custo_medio", "margem_valor", "margem_percentual")
CAMPOS_MARGEM_LIQUIDA = ("margem_liquida_valor", "margem_liquida_percentual")
CAMPOS_CMV_LIQUIDO = ("cmv_liquido", "custo_bonificacoes", "custo_devolucoes")
# Indicadores executivos (indicadores_executivos.py): a funcionalidade do plano que libera cada um. None: todo
# plano com Raio-X vê (faturamento, venda, volume, estoque em unidades e o mapa de calor de vendas). Margem,
# CMV, giro e custo seguem as funcionalidades da auditoria do Raio-X; a recompra, a curva de clientes do Ouro.
FUNCIONALIDADE_DO_INDICADOR = {
    "faturamento": None, "ticket_medio": None, "margem": "margem_bruta", "giro_estoque": "diagnostico_cmv",
    "mix": None, "itens_por_venda": None, "recompra": "curva_abc_clientes", "saving": "diagnostico_cmv",
    "evolucao_custo": "alerta_precificacao", "cmv": "diagnostico_cmv", "curva_abc_faturamento": "curva_abc_vendas",
    "curva_abc_volume": "curva_abc_vendas", "curva_abc_lucro": "margem_bruta",
    "margem_negativa": "auditoria_abaixo_custo", "margem_abaixo_minimo": "margem_bruta", "indice_descontos": None,
    "ruptura": "produtos_parados_ruptura", "encalhados": "produtos_parados_ruptura",
    "cobertura_estoque": "produtos_parados_ruptura", "gmroi": "margem_bruta", "mapa_calor": None,
}
# Sem a funcionalidade, o indicador vira {"status": "protegido", "funcionalidade": ...}, com estes campos ainda
# abertos: as contagens de margem negativa e de custo a confirmar, que todo plano vê na auditoria.
_CAMPOS_ABERTOS = {"margem_negativa": ("itens", "itens_custo_a_confirmar")}


def _indicadores_visiveis(indicadores, liberadas: frozenset, corte: date | None):
    """Os indicadores executivos com o que o plano libera; os meses (AAAA-MM) anteriores ao corte do
    histórico saem da margem negativa por mês e do mapa por produto e mês, como as fatias. Raio-X gravado
    antes da raio-x-2026-11.6: None."""
    if not isinstance(indicadores, dict) or not isinstance(indicadores.get("indicadores"), dict):
        return None
    itens = indicadores["indicadores"]
    for chave, valor in list(itens.items()):
        funcionalidade = FUNCIONALIDADE_DO_INDICADOR.get(chave)
        if funcionalidade is not None and funcionalidade not in liberadas and isinstance(valor, dict):
            abertos = {campo: valor.get(campo) for campo in _CAMPOS_ABERTOS.get(chave, ())}
            itens[chave] = {**abertos, "status": "protegido", "funcionalidade": funcionalidade}
    if "diagnostico_cmv" not in liberadas:
        # Valores a custo são do diagnóstico de CMV, também dentro de indicadores de outra funcionalidade: o
        # estoque parado em unidades é do Bronze; a venda com custo dos itens a confirmar (que, com a perda
        # desconsiderada da auditoria, daria o custo deles) e o estoque médio a custo do GMROI, também.
        for chave, campos in (("encalhados", ("valor_a_custo",)), ("margem_negativa", ("faturamento_custo_a_confirmar",)),
                              ("gmroi", ("estoque_medio_a_custo",))):
            indicador = itens.get(chave)
            if isinstance(indicador, dict) and indicador.get("status") != "protegido":
                for campo in campos:
                    if campo in indicador:
                        indicador[campo] = None
        encalhados = itens.get("encalhados")
        if isinstance(encalhados, dict) and encalhados.get("status") != "protegido":
            for item in encalhados.get("lista") or []:
                item["valor_a_custo"] = None
    if corte is None:
        return indicadores
    limite = f"{corte.year:04d}-{corte.month:02d}"
    negativa = itens.get("margem_negativa")
    if isinstance(negativa, dict) and negativa.get("status") == "calculado":
        negativa["por_mes"] = [mes for mes in negativa.get("por_mes") or [] if mes["mes"] >= limite]
        matriz = []
        for linha in negativa.get("matriz") or []:
            meses = [mes for mes in linha["meses"] if mes["mes"] >= limite]
            if meses:
                matriz.append({**linha, "meses": meses, "perda_total": round(sum(mes["perda"] for mes in meses), 2)})
        # Na ordem dos meses que o plano mostra (a perda oculta não pode decidir quem vem primeiro).
        matriz.sort(key=lambda linha: -linha["perda_total"])
        negativa["matriz"] = matriz
        negativa["perda_q4"] = round(sum(mes["perda"] for mes in negativa["por_mes"] if mes["impacto_q4"]), 2)
        if "perda_nos_meses" in negativa:
            negativa["perda_nos_meses"] = round(sum(mes["perda"] for mes in negativa["por_mes"]), 2)
    calor = itens.get("mapa_calor")
    if isinstance(calor, dict) and calor.get("tipo") == "sku_mes" and calor.get("meses"):
        manter = [posicao for posicao, mes in enumerate(calor["meses"]) if mes >= limite]
        calor["meses"] = [calor["meses"][posicao] for posicao in manter]
        calor["impacto_q4"] = [calor["impacto_q4"][posicao] for posicao in manter]
        linhas = []
        for linha in calor.get("linhas") or []:
            valores = [linha["valores"][posicao] for posicao in manter]
            if any(valores):
                linhas.append({**linha, "valores": valores})
        linhas.sort(key=lambda linha: -sum(linha["valores"]))
        calor["linhas"] = linhas
    return indicadores


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------
def _gravar_limitado(arquivo, caminho: Path, limits, numero: int) -> str:
    """Copia no máximo o teto + 1 byte; o Content-Length do cliente não é autoridade."""
    digest, tamanho = hashlib.sha256(), 0
    teto = security_gatekeeper.teto_bytes(limits)
    with caminho.open("wb") as destino:
        caminho.chmod(0o600)
        while True:
            bloco = arquivo.read(min(_BLOCO, teto + 1 - tamanho))
            if not bloco:
                break
            if not isinstance(bloco, bytes):
                raise ETLInputError("upload_io", f"Arquivo {numero}: não foi possível ler o arquivo enviado.")
            tamanho += len(bloco)
            if tamanho > teto:
                raise ETLInputError("file_size", f"Arquivo {numero}: acima do tamanho permitido para processamento.")
            digest.update(bloco)
            destino.write(bloco)
    if not tamanho:
        raise ETLInputError("empty_file", f"Arquivo {numero}: está vazio.")
    return digest.hexdigest()


def processar_envio(arquivos, limits=None, *, executar=None, origem: str = "raio_x") -> tuple[dict, list]:
    """(resultado do Raio-X, nomes seguros dos arquivos), na ordem em que foram enviados.

    `executar` troca a operação do processo isolado (padrão: etl_runtime.run_raio_x; a Avaliação
    de Cenário passa o executor dela, também de etl_runtime) e `origem` marca o log da Baia. O resto da
    entrada (teto, assinatura, recusa de arquivo repetido e diretório temporário) é o mesmo."""
    executar = etl_runtime.run_raio_x if executar is None else executar
    arquivos = [arquivo for arquivo in arquivos if arquivo is not None and arquivo.filename]
    if not arquivos:
        raise ETLInputError("raio_x_vazio", "Selecione ao menos um relatório de vendas (CSV, XLSX ou XLS).")
    if len(arquivos) > raio_x_regras.MAX_ARQUIVOS:
        raise ETLInputError("raio_x_arquivos", f"Envie no máximo {raio_x_regras.MAX_ARQUIVOS} relatórios por vez: "
                            "até 3 anos, um por mês.")
    try:
        limits = get_limits() if limits is None else limits
    except ValueError:
        raise ETLInputError("invalid_limits", "Configuração de processamento inválida.") from None
    nomes, vistos = [], set()
    try:
        with tempfile.TemporaryDirectory(prefix="nexo-raio-x-") as diretorio:
            caminhos = []
            for numero, arquivo in enumerate(arquivos, 1):
                nome = secure_filename(arquivo.filename) or f"arquivo-{numero}"
                extensao = os.path.splitext(nome)[1].lower().lstrip(".")
                if extensao not in EXTENSOES_ACEITAS:
                    raise ETLInputError("unsupported_format", f"Arquivo {numero}: formato não suportado. Envie "
                                        "CSV, XLSX, XLS ou Planilha XML 2003 (.xml).")
                caminho = Path(diretorio) / f"arquivo_{numero:02d}.{extensao}"
                digest = _gravar_limitado(arquivo, caminho, limits, numero)
                # A cópia no disco é a que segue: o envio (a parte que o Werkzeug guarda na memória, ou o
                # arquivo aberto) é solto antes do processo isolado.
                arquivo.close()
                # Baia de Segurança: assinatura e extensão antes do processo isolado. Os arquivos
                # do Raio-X não são gravados, então não há cópia neutralizada a liberar; a
                # leitura do motor já neutraliza as células (etl_inputs.read_raw). Por isso a
                # leitura direta aceita também o .xls verdadeiro e a Planilha XML 2003.
                try:
                    security_gatekeeper.inspecionar_arquivo(caminho, extensao, rotulo=f"Arquivo {numero}",
                                                            leitura_direta=True)
                except security_gatekeeper.SecurityGatekeeperError as erro:
                    security_gatekeeper.registrar_recusa(erro, sha256=digest, tamanho=caminho.stat().st_size,
                                                         extensao=extensao, origem=origem)
                    raise
                if digest in vistos:
                    raise ETLInputError("raio_x_repetido", f"Arquivo {numero}: é igual a outro arquivo enviado.")
                vistos.add(digest)
                caminhos.append(str(caminho))
                nomes.append(nome[:80])
            resultado = executar(caminhos, limits)
    except OSError:
        raise ETLInputError("upload_io", "Não foi possível preparar os arquivos enviados. Tente novamente.") from None
    return resultado, nomes


# ---------------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------------
def salvar(id_empresa: int, id_usuario: int, resultado: dict, recomendacao: dict, nomes: list) -> DiagnosticoEntrada:
    """Grava (sem commit) o Raio-X da empresa no lugar do anterior e o retrato dele na evolução mensal (Etapa 3)."""
    linha = db.session.get(DiagnosticoEntrada, id_empresa, with_for_update=True, populate_existing=True)
    # O Raio-X que sai: vira o Marco Zero de quem ainda não tem retrato (_registrar_fechamento).
    anterior = (linha.gerado_em, linha.resultado) if linha is not None else None
    if linha is None:
        linha = DiagnosticoEntrada(id_empresa=id_empresa)
        db.session.add(linha)
    periodo = resultado["periodo"]
    indice = resultado["saude"]["indice"]
    linha.id_usuario = id_usuario
    linha.gerado_em = agora_utc()
    linha.versao_regras = resultado["versao"]
    # Os arquivos enviados (desde a raio-x-2026-11.6, "arquivos" do resultado traz um relatório por mês do arquivo
    # fatiado).
    linha.arquivos = len(nomes) or len(resultado["arquivos"])
    linha.periodo_inicio = date.fromisoformat(periodo["inicio"]) if periodo["inicio"] else None
    linha.periodo_fim = date.fromisoformat(periodo["fim"]) if periodo["fim"] else None
    linha.indice_saude = None if indice is None else Decimal(str(indice)).quantize(Decimal("0.01"))
    linha.valor_recuperavel = Decimal(str(resultado["recuperavel"]["total"]))
    linha.plano_recomendado = recomendacao["plano"]
    linha.resultado = json.dumps({**resultado, "recomendacao": recomendacao, "nomes_arquivos": nomes},
                                 ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    # O Raio-X vai ao banco antes do retrato: um erro dele sobe como sempre subiu.
    db.session.flush()
    _registrar_fechamento(id_empresa, linha.gerado_em, resultado, anterior)
    return linha


def ultimo(id_empresa: int) -> tuple[DiagnosticoEntrada, dict] | None:
    """(linha, resultado) do último Raio-X da empresa; None sem Raio-X ou com JSON ilegível."""
    linha = db.session.get(DiagnosticoEntrada, id_empresa)
    if linha is None:
        return None
    try:
        dados = json.loads(linha.resultado)
    except ValueError:
        logger.warning("Raio-X da empresa %s ilegível: ignorado.", id_empresa)
        return None
    # O Raio-X gravado antes da raio-x-2026-11.6 não tem os indicadores executivos e continua valendo.
    if not isinstance(dados, dict) or not raio_x_regras.CHAVES_OBRIGATORIAS <= set(dados):
        return None
    return linha, dados


# ---------------------------------------------------------------------------
# Evolução & Conquistas (Etapa 3, 14/11/2026): os retratos mensais
# ---------------------------------------------------------------------------
TABELA_FECHAMENTO = "diagnostico_fechamento"
REVISAO_FECHAMENTO = "d9e4b7a2c6f1"
_REVERIFICAR_FECHAMENTO_SEGUNDOS = 30
_fechamento_por_engine: "WeakKeyDictionary" = WeakKeyDictionary()


def fechamentos_disponiveis(sessao=None) -> bool:
    """True quando a tabela dos retratos (migração d9e4b7a2c6f1) existe. Confirmada a presença, o resultado fica fixo
    para o engine; a ausência é reavaliada a cada 30 s, para a migração aplicada valer sem reiniciar o processo (como
    entitlements.esquema_planos_disponivel). Sem a tabela, o Raio-X grava igual e a evolução mostra o estado inicial."""
    sessao = sessao or db.session
    engine = sessao.get_bind()
    estado = _fechamento_por_engine.get(engine)
    if estado is True:
        return True
    if estado is not None and time.monotonic() - estado < _REVERIFICAR_FECHAMENTO_SEGUNDOS:
        return False
    presente = inspect(sessao.connection()).has_table(TABELA_FECHAMENTO)
    _fechamento_por_engine[engine] = True if presente else time.monotonic()
    if not presente:
        logger.info("Tabela %s ausente: a evolução mensal espera a migração %s.", TABELA_FECHAMENTO,
                    REVISAO_FECHAMENTO)
    return presente


def retrato_do_raio_x(dados: dict, gerado_em) -> dict:
    """O retrato de um Raio-X gravado (evolucao.retrato), com tudo calculado e nada escondido: o plano esconde na
    hora de mostrar (evolucao.comparar)."""
    return evolucao.retrato(visao(dados, evolucao.TUDO, None), dados, gerado_em)


def _gravar_retrato(id_empresa: int, retrato: dict) -> None:
    """Grava (sem commit) o retrato no mês dele, no lugar do retrato do mesmo mês."""
    competencia = date.fromisoformat(retrato["competencia"])
    linha = db.session.scalar(
        select(DiagnosticoFechamento)
        .where(DiagnosticoFechamento.id_empresa == id_empresa, DiagnosticoFechamento.competencia == competencia)
        .with_for_update()
    )
    if linha is None:
        linha = DiagnosticoFechamento(id_empresa=id_empresa, competencia=competencia)
        db.session.add(linha)
    linha.gerado_em = datetime.fromisoformat(retrato["gerado_em"])
    linha.versao_regras = (retrato.get("versao_regras") or retrato["versao"])[:40]
    linha.indicadores = json.dumps(retrato, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def esquecer_fechamentos(sessao=None) -> None:
    """Esquece a presença da tabela dos retratos: a próxima leitura confere de novo (depois de uma falha de banco ou
    de um downgrade com o processo no ar)."""
    sessao = sessao or db.session
    _fechamento_por_engine.pop(sessao.get_bind(), None)


def _registrar_fechamento(id_empresa: int, gerado_em, resultado: dict, anterior: tuple | None) -> None:
    """O retrato do Raio-X recém-gravado, na mesma transação, num SAVEPOINT (a linha do Raio-X já foi enviada ao
    banco: um erro dela nunca se confunde com um erro do retrato). Sem retrato nenhum da empresa e com um Raio-X
    anterior de outro mês (e das regras raio-x-2026-11.5 em diante), grava antes o retrato dele: o Marco Zero de quem
    fez o Raio-X antes da Etapa 3. Sem a
    tabela (migração não aplicada) ou com qualquer erro, o Raio-X grava igual e só o retrato falta (fica no log): a
    evolução nunca derruba o Raio-X."""
    try:
        with db.session.begin_nested():
            # Dentro do SAVEPOINT: no PostgreSQL, uma consulta que falha fora dele abortaria a transação do Raio-X.
            if not fechamentos_disponiveis():
                return
            ja_tem = db.session.scalar(select(DiagnosticoFechamento.id_fechamento)
                                       .where(DiagnosticoFechamento.id_empresa == id_empresa).limit(1)) is not None
            novos = []
            if (not ja_tem and anterior is not None
                    and evolucao.competencia(anterior[0]) != evolucao.competencia(gerado_em)):
                dados_anteriores = _resultado_legivel(anterior[1])
                # Com as regras de antes da raio-x-2026-11.5 (o custo implausível dentro da perda), o Raio-X
                # anterior não serve de base: o novo vira o Marco Zero.
                if dados_anteriores is not None and evolucao.regras_comparaveis(dados_anteriores.get("versao")):
                    novos.append(retrato_do_raio_x(dados_anteriores, anterior[0]))
            novos.append(retrato_do_raio_x(resultado, gerado_em))
            for retrato in novos:
                _gravar_retrato(id_empresa, retrato)
            db.session.flush()
    except SQLAlchemyError as erro:
        esquecer_fechamentos()
        logger.warning("Retrato da evolução não gravado (empresa %s): %s.", id_empresa, type(erro).__name__)
    except (ArithmeticError, KeyError, TypeError, ValueError, AttributeError) as erro:
        logger.warning("Retrato da evolução não gravado (empresa %s): %s.", id_empresa, type(erro).__name__)


def _resultado_legivel(texto) -> dict | None:
    """O JSON gravado de um Raio-X, se for legível e completo; None senão."""
    try:
        dados = json.loads(texto)
    except (TypeError, ValueError):
        return None
    if not isinstance(dados, dict) or not raio_x_regras.CHAVES_OBRIGATORIAS <= set(dados):
        return None
    return dados


def retratos_da_empresa(id_empresa: int) -> list:
    """Os retratos gravados da empresa, do mês mais antigo ao mais recente (vazio sem a tabela). O JSON ilegível
    fica de fora (evolucao.comparar também descarta o retrato fora do formato)."""
    if not fechamentos_disponiveis():
        return []
    retratos = []
    for texto in db.session.scalars(select(DiagnosticoFechamento.indicadores)
                                    .where(DiagnosticoFechamento.id_empresa == id_empresa)
                                    .order_by(DiagnosticoFechamento.competencia)):
        try:
            retratos.append(json.loads(texto))
        except (TypeError, ValueError):
            logger.warning("Retrato da evolução ilegível (empresa %s): ignorado.", id_empresa)
    return retratos


def _mesmo_instante(texto, instante) -> bool:
    try:
        return abs((datetime.fromisoformat(texto) - instante_utc(instante)).total_seconds()) < 1
    except (TypeError, ValueError):
        return False


def evolucao_da_empresa(id_empresa: int, liberadas: frozenset) -> dict | None:
    """O painel Evolução & Conquistas da empresa (evolucao.comparar): None sem o Raio-X do PDV no plano ou sem Raio-X.
    O Raio-X atual sem retrato (feito antes da Etapa 3, ou com a gravação do retrato falhando) entra calculado na
    hora, sem gravar: com ele sozinho, o painel mostra o estado inicial."""
    if "raio_x_entrada" not in liberadas:
        return None
    retratos = retratos_da_empresa(id_empresa)
    gerado_em = db.session.scalar(select(DiagnosticoEntrada.gerado_em)
                                  .where(DiagnosticoEntrada.id_empresa == id_empresa))
    if gerado_em is not None and not any(isinstance(r, dict) and _mesmo_instante(r.get("gerado_em"), gerado_em)
                                         for r in retratos):
        registro = ultimo(id_empresa)
        # O Raio-X com regras de antes da raio-x-2026-11.5 não vira Marco Zero (o próximo Raio-X fica no lugar dele).
        if registro is not None and (retratos or evolucao.regras_comparaveis(registro[1].get("versao"))):
            linha, dados = registro
            try:
                retratos.append(retrato_do_raio_x(dados, linha.gerado_em))
            except (ArithmeticError, KeyError, TypeError, ValueError, AttributeError) as erro:
                logger.warning("Retrato do Raio-X atual não montado (empresa %s): %s.", id_empresa,
                               type(erro).__name__)
    return evolucao.comparar(retratos, liberadas)


# ---------------------------------------------------------------------------
# O que cada plano vê
# ---------------------------------------------------------------------------
def _corte_historico(fatias: list, limite_meses: int | None) -> date | None:
    """Primeiro dia do mês mais antigo que o plano mostra (contado do mês mais recente enviado)."""
    fins = [date.fromisoformat(fatia["fim"]) for fatia in fatias if fatia.get("fim")]
    if not limite_meses or not fins:
        return None
    recente = max(fins)
    meses = recente.year * 12 + recente.month - 1 - (limite_meses - 1)
    return date(meses // 12, meses % 12 + 1, 1)


def mostra_todas_as_fatias(fatias: list, limite_meses: int | None) -> bool:
    """O plano com esse limite de histórico mostra todos os relatórios datados enviados."""
    corte = _corte_historico(fatias, limite_meses)
    return corte is None or all(not fatia.get("fim") or date.fromisoformat(fatia["fim"]) >= corte
                                for fatia in fatias)


def _centavos(valor) -> int | None:
    """Reais (float gravado pelo worker, já em centavos) em centavos inteiros; None se não houver número (o
    json.loads aceita NaN e Infinity num JSON editado à mão, que o worker nunca grava)."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not math.isfinite(valor):
        return None
    try:
        return int((Decimal(str(valor)) * 100).to_integral_value())
    except (ArithmeticError, ValueError):
        return None


def _itens_a_confirmar(bloco) -> int:
    """Quantos itens de custo a confirmar o bloco gravado traz (0 sem o bloco, com lixo ou sem item)."""
    itens = bloco.get("itens") if isinstance(bloco, dict) else None
    return itens if isinstance(itens, int) and not isinstance(itens, bool) and itens > 0 else 0


def margem_sem_custo_a_confirmar(auditoria: dict) -> dict:
    """A margem bruta do envio sem os itens de custo a confirmar (custo a partir de raio_x.RAZAO_CUSTO_IMPLAUSIVEL
    vezes a venda: erro de cadastro, não prejuízo), pedida pelo dono em 14/11/2026 para o card "Margem bruta
    estimada" da Auditoria de custos. Sai das duas parcelas a venda com custo e o custo desses itens
    (auditoria.custo_a_confirmar, gravado desde a raio-x-2026-11.5), em centavos inteiros, como o worker.

    Devolve {"valor", "percentual", "base", "itens_fora"}: base é a venda com custo que sobra; itens_fora, quantos
    itens saíram. Sem custo informado, valor, percentual e base são None. Sem o bloco (Raio-X gravado até a 11.4)
    ou sem item a confirmar, é a margem de sempre. Quando todo o custo informado é de itens a confirmar, não sobra
    custo confiável: valor e percentual None."""
    custo, venda = _centavos(auditoria.get("custo_total")), _centavos(auditoria.get("faturamento_com_custo"))
    bloco = auditoria.get("custo_a_confirmar")
    itens = _itens_a_confirmar(bloco)
    if not custo or custo <= 0 or venda is None:
        return {"valor": None, "percentual": None, "base": None, "itens_fora": itens}
    if itens:
        custo_fora, venda_fora = _centavos(bloco.get("custo_total")), _centavos(bloco.get("faturamento_com_custo"))
        if custo_fora is None or venda_fora is None:
            return {"valor": None, "percentual": None, "base": None, "itens_fora": itens}
        custo, venda = custo - custo_fora, venda - venda_fora
    if custo <= 0 or venda <= 0:
        return {"valor": None, "percentual": None, "base": round(max(venda, 0) / 100, 2), "itens_fora": itens}
    margem = venda - custo
    return {"valor": round(margem / 100, 2), "percentual": round(margem / venda, 4), "base": round(venda / 100, 2),
            "itens_fora": itens}


def custo_sem_custo_a_confirmar(auditoria: dict) -> float | None:
    """O custo das vendas (custo_total) sem o dos itens de custo a confirmar, pedido pelo dono em 14/11/2026 (Etapa 1)
    para o card "Custo dos produtos vendidos" da Auditoria de custos: o custo das mesmas vendas da "Sobra bruta nas
    vendas" (margem_sem_custo_a_confirmar). Em centavos inteiros, como o worker.

    None sem número gravado, ou com item a confirmar e o bloco sem o custo. Sem o bloco (Raio-X gravado até a 11.4)
    ou sem item a confirmar, é o custo de sempre. Nunca negativo: se todo o custo informado é de itens a confirmar,
    sobra zero."""
    custo = _centavos(auditoria.get("custo_total"))
    if custo is None:
        return None
    bloco = auditoria.get("custo_a_confirmar")
    if _itens_a_confirmar(bloco):
        fora = _centavos(bloco.get("custo_total"))
        if fora is None:
            return None
        custo = max(custo - fora, 0)
    return round(custo / 100, 2)


def visao(dados: dict, liberadas: frozenset, limite_meses: int | None) -> dict:
    """Cópia do resultado sem o que o plano não libera (valores trocados por None)."""
    resultado = copy.deepcopy(dados)
    margem = "margem_bruta" in liberadas
    if not margem:
        for item in resultado["scanner"]["itens"]:
            for campo in CAMPOS_MARGEM:
                if campo in item:
                    item[campo] = None
        for fatia in resultado["fatias"]:
            for campo in ("custo_total", "margem_valor", "margem_percentual"):
                fatia[campo] = None
    auditoria = resultado["auditoria"]
    # Margem e custo sem os itens de custo a confirmar (14/11/2026), calculados antes de o plano esconder o custo.
    limpa = margem_sem_custo_a_confirmar(auditoria)
    custo_limpo = custo_sem_custo_a_confirmar(auditoria)
    # Raio-X gravado antes do saneamento de 11/2026 (raio-x-2026-11.3) não tem os campos líquidos:
    # eles entram como nulos, a mesma forma do que o plano não libera, e a tela e a API não quebram.
    for campo in CAMPOS_MARGEM_LIQUIDA + CAMPOS_CMV_LIQUIDO:
        auditoria.setdefault(campo, None)
    if "auditoria_abaixo_custo" not in liberadas:
        auditoria["itens_abaixo_custo"] = None
    if not margem:
        auditoria["margem_por_item"] = None
        auditoria["margem_valor"] = auditoria["margem_percentual"] = None
        for campo in CAMPOS_MARGEM_LIQUIDA:
            auditoria[campo] = None
    if "diagnostico_cmv" not in liberadas:
        auditoria["custo_total"] = None
        for campo in CAMPOS_CMV_LIQUIDO:
            auditoria[campo] = None
    if "alerta_precificacao" not in liberadas:
        auditoria["precificacao"] = None
    # Custo a confirmar (unificação de 11/2026): os itens de custo implausível, fora da perda e do
    # recuperável. Raio-X gravado antes (até raio-x-2026-11.4) não tem o bloco: entra como nulo. A contagem e
    # a perda desconsiderada são totais como a perda abaixo do custo, que todo plano vê; a lista, com o
    # custo de cada item, é da auditoria de custos, como a das vendas abaixo do custo; e o custo e a venda
    # com custo do bloco (que com a perda dariam o custo) são do diagnóstico de CMV, como o custo total.
    a_confirmar = auditoria.get("custo_a_confirmar")
    if not isinstance(a_confirmar, dict):
        auditoria["custo_a_confirmar"] = None
    else:
        if "auditoria_abaixo_custo" not in liberadas:
            a_confirmar["lista"] = None
        if "diagnostico_cmv" not in liberadas:
            a_confirmar["custo_total"] = a_confirmar["faturamento_com_custo"] = None
    # Os campos "sem_a_confirmar": o card da margem e a contagem de margem negativa sem os itens de custo a
    # confirmar. A margem limpa e a venda com custo que sobra pedem a funcionalidade da margem, a mesma que libera o
    # indicador 3 (indicadores_executivos, FUNCIONALIDADE_DO_INDICADOR["margem"]), que já traz essa margem e essa
    # venda sem os itens a confirmar; sem item a confirmar, são a margem e a venda com custo de sempre. A venda com
    # custo sozinha também sai com o diagnóstico de CMV, que já mostra o custo e a venda dos itens a confirmar.
    cmv = "diagnostico_cmv" in liberadas
    auditoria["margem_sem_a_confirmar_valor"] = limpa["valor"] if margem else None
    auditoria["margem_sem_a_confirmar_percentual"] = limpa["percentual"] if margem else None
    auditoria["faturamento_com_custo_sem_a_confirmar"] = limpa["base"] if (margem or cmv or not limpa["itens_fora"]) \
        else None
    # O custo das vendas sem os itens a confirmar (card "Custo dos produtos vendidos", Etapa 1 de 14/11/2026) é do
    # diagnóstico de CMV, como o custo total e o custo do bloco, de que ele é a diferença.
    auditoria["custo_total_sem_a_confirmar"] = custo_limpo if cmv else None
    # Todo item a confirmar tem margem negativa (custo de 3 vezes a venda ou mais): a contagem limpa é a diferença.
    negativos = auditoria.get("itens_margem_negativa")
    auditoria["itens_margem_negativa_sem_a_confirmar"] = (
        max(0, negativos - limpa["itens_fora"]) if isinstance(negativos, int) and not isinstance(negativos, bool)
        else None)

    corte = _corte_historico(resultado["fatias"], limite_meses)
    visiveis, ocultas = [], []
    for fatia in resultado["fatias"]:
        if corte is not None and fatia.get("fim") and date.fromisoformat(fatia["fim"]) < corte:
            ocultas.append(fatia)
        else:
            visiveis.append(fatia)
    resultado["fatias"] = visiveis
    # A lista de relatórios também perde os meses ocultos (o arquivo fatiado traz um item por mês, com as linhas
    # de venda de cada um): fatias e arquivos usam o mesmo índice.
    indices_ocultos = {fatia["indice"] for fatia in ocultas}
    resultado["arquivos"] = [arquivo for arquivo in resultado.get("arquivos") or []
                             if arquivo.get("indice") not in indices_ocultos]
    # A faixa oculta pelas datas, em mês/ano: um relatório anual oculto não vira "A a B a C".
    de = min((date.fromisoformat(f["inicio"] or f["fim"]) for f in ocultas), default=None)
    ate = max((date.fromisoformat(f["fim"]) for f in ocultas), default=None)
    resultado["historico"] = {"limite_meses": limite_meses, "ocultas": len(ocultas),
                              "primeira_oculta": f"{de.month:02d}/{de.year}" if de else None,
                              "ultima_oculta": f"{ate.month:02d}/{ate.year}" if ate else None}
    resultado["indicadores_executivos"] = _indicadores_visiveis(resultado.get("indicadores_executivos"), liberadas,
                                                                corte)
    resultado["protegido"] = {
        "margem": not margem,
        "auditoria": "auditoria_abaixo_custo" not in liberadas,
        "cmv": "diagnostico_cmv" not in liberadas,
        "precificacao": "alerta_precificacao" not in liberadas,
    }
    return resultado
