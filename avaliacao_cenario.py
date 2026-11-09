"""Avaliação de Cenário (lead): grava o resultado do motor na staging e refaz a triagem.

Fluxo (consultor, rotas admin_required em blueprints/leads.py):
  1. processar(): raio_x_servico.processar_envio com executar=etl_runtime.run_avaliacao_cenario e
     origem="lead": o mesmo teto, a mesma assinatura da Baia, a recusa de arquivo repetido e o
     diretório temporário do Raio-X. O processo isolado lê, limpa e calcula (avaliacao_motor);
     nada é gravado e os arquivos somem com o diretório, sem guardar nem o nome deles.
  2. registrar(): depois do processo isolado e num SAVEPOINT, o lead (ou, no reenvio, a trava
     dele e a troca das linhas), as linhas produto x ano quantizadas como o banco as guarda, as
     marcas e a triagem (triagem_cenario, com os limiares usados gravados no lead) e o resumo. Se o
     processo isolado falhar, nada é gravado: lead sem linhas nunca expiraria.
  3. retriar(): refaz marcas e triagem a partir da staging, sem os arquivos (`flask leads retriar
     <id>`), enquanto as linhas existirem e estiverem dentro dos 30 dias contados do envio (mesmo antes
     de o expurgo rodar), com os limiares gravados no lead (ou os passados). Limiares gravados por
     outra versão da triagem dão lugar aos padrão atuais, com aviso (limiares_para_retriagem).

As colunas gravadas saem de avaliacao_motor.COLUNAS (fonte única): coluna nova do worker que não
for gravada aqui nem conferida em etl_runtime.validar_avaliacao quebra o teste de contrato.

Ordem de trava: o lead e depois as linhas, a mesma do expurgo (leads_avaliacao.py). Sem commit:
quem chama decide.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import json

from sqlalchemy import delete, insert, select, update

import avaliacao_motor
from etl_inputs import ETLInputError
import inteligencia_estoque
import etl_runtime
from extensions import db
from horario import agora_utc
from leads_avaliacao import RETENCAO_DIAS
from models import BrainstormingDadosPdv, LeadAvaliacao
import raio_x
import raio_x_servico
import triagem_cenario

_CENTAVO = Decimal("0.01")
_MILESIMO = Decimal("0.001")
_DINHEIRO = avaliacao_motor.COLUNAS_DINHEIRO
_QUANTIDADES = avaliacao_motor.COLUNAS_QUANTIDADE
# Identidade da linha, gravada à parte; "categoria" (índice) vira o texto em categoria_produto.
_IDENTIDADE = ("ano_exercicio", "chave_produto", "codigo_produto", "descricao_produto")
_SOMAS_GRAVADAS = tuple(nome for nome in avaliacao_motor.COLUNAS if nome not in _IDENTIDADE and nome != "categoria")
_TETO_VAZAMENTO_TOTAL = Decimal(10) ** 10  # NUMERIC(12,2) de lead_avaliacao."vazamento_total_r$"


class LeadIndisponivel(ValueError):
    """Lead inexistente, convertido, expirado ou sem as linhas que a operação precisa."""


def processar(arquivos, *, limits=None) -> dict:
    """Resultado do motor para os relatórios do lead (processo isolado). Não grava nada."""
    resultado, _nomes = raio_x_servico.processar_envio(
        arquivos, limits, executar=etl_runtime.run_avaliacao_cenario, origem="lead")
    return resultado


def _decimal(valor, casas: Decimal) -> Decimal:
    return Decimal(str(valor)).quantize(casas, rounding=ROUND_HALF_UP)


def quantizar(linhas: dict) -> list:
    """As linhas do worker (colunas, valores e categorias) como o banco as guarda: dinheiro com 2 casas
    e quantidade com 3, em Decimal, e a categoria como texto. As marcas e a triagem saem sempre deste
    valor gravado."""
    colunas = linhas["colunas"]
    categorias = linhas.get("categorias", [])
    saida = []
    for valores in linhas["valores"]:
        linha = dict(zip(colunas, valores))
        for nome in _QUANTIDADES:
            if nome in linha:
                linha[nome] = _decimal(linha[nome], _MILESIMO)
        for nome in _DINHEIRO:
            if nome in linha:
                linha[nome] = _decimal(linha[nome], _CENTAVO)
        if linha.get("estoque_final") is not None:
            linha["estoque_final"] = _decimal(linha["estoque_final"], _MILESIMO)
        for nome in avaliacao_motor.COLUNAS_DATA:
            if linha.get(nome) is not None:
                linha[nome] = date.fromisoformat(linha[nome])
        indice = linha.pop("categoria", None)
        linha["categoria_produto"] = categorias[indice] if indice is not None else None
        saida.append(linha)
    return saida


def _registros(linhas: list, triagem, versao: str, cobertura) -> list:
    registros = []
    confiavel = not cobertura.cabecalho_ambiguo
    for linha in linhas:
        registro = {nome: linha[nome] for nome in (*_IDENTIDADE, *_SOMAS_GRAVADAS, "categoria_produto")}
        registro.update(triagem_cenario.marcas(linha, triagem.limiares, conflito_confiavel=confiavel))
        registro["bloco_triagem"] = triagem.blocos[linha["chave_produto"]]
        registro["versao_regras"] = versao
        registros.append(registro)
    return registros


def _conferir_tetos(registros: list, vazamento_total: Decimal) -> None:
    """Derivados que o processo isolado não vê (custo médio NUMERIC(14,4), vazamento estimado e
    vazamento total NUMERIC(12,2)) e as somas já arredondadas (o arredondamento pode levar um valor
    logo abaixo do teto até ele). Recusa com mensagem em vez de derrubar o SAVEPOINT."""
    tetos = avaliacao_motor.TETOS
    for registro in registros:
        custo_medio = registro["custo_medio_registrado"]
        if ((custo_medio is not None and custo_medio >= tetos["custo_medio_registrado"])
                or registro["vazamento_estimado_reais"] >= tetos["vazamento_estimado_r$"]
                or any(registro.get(nome) is not None and abs(registro[nome]) >= teto
                       for nome, teto in tetos.items() if nome in registro)):
            raise ETLInputError("avaliacao_valor", "Há quantidade ou valor acima do que a avaliação aceita nos "
                                "relatórios. Confira as colunas de quantidade, valor e custo.")
    if vazamento_total >= _TETO_VAZAMENTO_TOTAL:
        raise ETLInputError("avaliacao_valor", "O valor recuperável somado passa do que a avaliação aceita. "
                            "Confira os relatórios enviados.")


def _json(valor) -> str:
    return json.dumps(valor, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def resumo_da_inteligencia(linhas: list, cobertura, triagem, meta: dict) -> str | None:
    """O resumo numérico da inteligência de estoque e de preço (sem nome de produto nem de categoria),
    gravado no lead para a lista mostrar sem refazer nada. Meta sem os períodos cobertos: nada."""
    try:
        return _json(inteligencia_estoque.resumo(inteligencia_estoque.calcular(linhas, cobertura, triagem, meta)))
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError, RecursionError):
        # O resumo é auxiliar: um dado que ele não lê não impede a gravação do lead.
        return None


def _aplicar_triagem(lead: LeadAvaliacao, triagem, vazamento_total: Decimal) -> None:
    lead.resumo_urgencia_json = _json(triagem.resumos["urgencia"])
    lead.resumo_atencao_json = _json(triagem.resumos["atencao"])
    lead.resumo_eletiva_json = _json(triagem.resumos["eletiva"])
    lead.versao_triagem = triagem_cenario.VERSAO_TRIAGEM
    lead.itens_venda_sem_custo = triagem.itens_venda_sem_custo
    lead.itens_venda_abaixo_custo = triagem.itens_venda_abaixo_custo
    lead.itens_omissao_erp = triagem.itens_omissao_erp
    lead.limiares_triagem_json = _json(triagem.limiares.para_dict())
    lead.vazamento_total_reais = vazamento_total
    lead.atualizado_em = agora_utc()


def limiares_do_lead(lead: LeadAvaliacao):
    """Os limiares gravados no lead; sem gravação, os padrão. Gravação ilegível: ValueError; gravação
    de outra versão da triagem: triagem_cenario.LimiaresDeOutraVersao (também ValueError)."""
    if not lead.limiares_triagem_json:
        return triagem_cenario.LIMIARES_PADRAO
    try:
        dados = json.loads(lead.limiares_triagem_json)
    except (ValueError, RecursionError):
        raise ValueError("Limiares gravados ilegíveis.") from None
    return triagem_cenario.Limiares.de_dict(dados)


MENSAGEM_LIMIARES_DE_OUTRA_VERSAO = ("Os limiares gravados no lead eram de outra versão da triagem: valeram os "
                                     "padrão atuais. Para usar outros, rode flask leads retriar <id> --limiares.")


def limiares_para_retriagem(lead: LeadAvaliacao) -> tuple:
    """(limiares, aviso ou None) para refazer a triagem ou reenviar: os gravados no lead; os de outra
    versão da triagem dão lugar aos padrão atuais, com aviso (o sentido de um limiar pode ter mudado).
    Gravação ilegível continua ValueError: quem chama decide (a retriagem recusa)."""
    try:
        return limiares_do_lead(lead), None
    except triagem_cenario.LimiaresDeOutraVersao:
        return triagem_cenario.LIMIARES_PADRAO, MENSAGEM_LIMIARES_DE_OUTRA_VERSAO


def dentro_da_retencao(criado_em, agora=None) -> bool:
    """Linhas de PDV gravadas em `criado_em` ainda dentro dos 30 dias: o mesmo critério do diagnóstico,
    que já as trata como expiradas antes de o expurgo rodar."""
    agora = agora or agora_utc()
    return criado_em + timedelta(days=RETENCAO_DIAS) > agora


def registrar(resultado: dict, *, contato: dict | None = None, lead_id=None, registrado_por: int | None = None,
              meses_prata: int | None = None, limiares=None) -> LeadAvaliacao:
    """Grava (sem commit) o resultado do motor: um lead novo com `contato`, ou o lead `lead_id`
    com as linhas trocadas pelas do novo envio. Devolve o lead. `limiares`: os da triagem (padrão:
    os padrão; o reenvio usa os gravados no lead quando não vierem)."""
    if (contato is None) == (lead_id is None):
        raise ValueError("Informe o contato de um lead novo ou o lead do reenvio.")
    if limiares is not None and not isinstance(limiares, triagem_cenario.Limiares):
        raise ValueError("Limiares inválidos.")
    aviso_limiares = None
    if limiares is None and lead_id is not None:
        anterior = db.session.get(LeadAvaliacao, lead_id)
        try:
            limiares, aviso_limiares = limiares_para_retriagem(anterior) if anterior is not None else (None, None)
        except ValueError:
            limiares, aviso_limiares = None, ("Os limiares gravados no lead estavam ilegíveis: a nova avaliação "
                                              "usou os padrão atuais.")
    limiares = limiares or triagem_cenario.LIMIARES_PADRAO
    try:
        # Tudo o que sai do processo isolado e vira coluna: o formato já foi conferido
        # (etl_runtime.validar_avaliacao); o que ainda falhar aqui é resultado inválido, e não erro 500.
        linhas = quantizar(resultado["linhas"])
        meta = resultado["meta"]
        cobertura = triagem_cenario.Cobertura.do_envio(meta)
        triagem = triagem_cenario.triar(linhas, cobertura, limiares)
        resumo_inteligencia = resumo_da_inteligencia(linhas, cobertura, triagem, resultado["meta"])
        registros = _registros(linhas, triagem, resultado["versao"], cobertura)
        vazamento_total = sum((r["vazamento_estimado_reais"] for r in registros), Decimal("0.00"))
        raio = resultado["raio_x"]
        recomendacao = raio_x.recomendar(raio, meses_prata)
        periodo = raio["periodo"]
        periodo_inicio = date.fromisoformat(periodo["inicio"]) if periodo["inicio"] else None
        periodo_fim = date.fromisoformat(periodo["fim"]) if periodo["fim"] else None
        score = _decimal(raio["saude"]["indice"], _CENTAVO)
        meta_json = _json({**meta, "motivos_recomendacao": recomendacao["motivos"]})
    except (KeyError, IndexError, TypeError, ValueError, ArithmeticError):
        raise ETLInputError("worker", "Resultado inválido do processamento isolado.") from None
    _conferir_tetos(registros, vazamento_total)
    agora = agora_utc()
    try:
        lead = _gravar(registros, triagem, resultado, contato, lead_id, registrado_por, agora, score, recomendacao,
                       periodo_inicio, periodo_fim, meta, meta_json, vazamento_total, resumo_inteligencia)
    except LeadIndisponivel:
        raise
    except (ValueError, UnicodeError):
        # O driver recusa texto com NUL ou surrogate (o contrato já recusa; isto é a segunda barreira).
        raise ETLInputError("worker", "Resultado inválido do processamento isolado.") from None
    lead.aviso_limiares = aviso_limiares
    return lead


def _gravar(registros, triagem, resultado, contato, lead_id, registrado_por, agora, score, recomendacao,
            periodo_inicio, periodo_fim, meta, meta_json, vazamento_total, resumo_inteligencia) -> LeadAvaliacao:
    with db.session.begin_nested():
        if lead_id is None:
            lead = LeadAvaliacao(**contato, registrado_por=registrado_por, consentimento_em=agora)
            db.session.add(lead)
            db.session.flush()
        else:
            lead = db.session.get(LeadAvaliacao, lead_id, with_for_update=True, populate_existing=True)
            if lead is None or lead.status == "convertido":
                raise LeadIndisponivel("Lead inexistente ou já convertido em cliente.")
            db.session.execute(delete(BrainstormingDadosPdv).where(BrainstormingDadosPdv.lead_id == lead.id))
            lead.consentimento_em = agora
            if registrado_por is not None:
                lead.registrado_por = registrado_por
        # Um único instante para o envio inteiro: o expurgo de 30 dias leva o lote junto.
        if registros:
            db.session.execute(insert(BrainstormingDadosPdv),
                               [{**registro, "lead_id": lead.id, "criado_em": agora} for registro in registros])
        lead.score_saude_cadastral = score
        lead.plano_recomendado_codigo = recomendacao["plano"]
        lead.versao_regras = resultado["versao"]
        lead.periodo_inicio = periodo_inicio
        lead.periodo_fim = periodo_fim
        lead.arquivos = meta["arquivos"]
        lead.meta_json = meta_json
        lead.resumo_inteligencia_json = resumo_inteligencia
        _aplicar_triagem(lead, triagem, vazamento_total)
        lead.status = "concluido"
        db.session.flush()
    return lead


_COLUNAS_DA_STAGING = (*_IDENTIDADE, *_SOMAS_GRAVADAS, "categoria_produto")


def linha_da_staging(registro: BrainstormingDadosPdv) -> dict:
    """A linha gravada no formato que a triagem lê (somas em Decimal), com a versão das regras e o
    instante do envio (para a retenção)."""
    linha = {nome: getattr(registro, nome) for nome in _COLUNAS_DA_STAGING}
    for nome in (*_QUANTIDADES, *_DINHEIRO):
        linha[nome] = Decimal(linha[nome])
    if linha["estoque_final"] is not None:
        linha["estoque_final"] = Decimal(linha["estoque_final"])
    linha["lead_id"] = registro.lead_id
    linha["versao_regras"] = registro.versao_regras
    linha["criado_em"] = registro.criado_em
    return linha


def retriar(lead_id, limiares=None, *, agora=None) -> LeadAvaliacao:
    """Refaz (sem commit) marcas e triagem do lead a partir das linhas gravadas, com `limiares` ou,
    sem eles, os gravados no lead (os de outra versão da triagem dão lugar aos padrão atuais, e o lead
    devolvido traz o aviso em `aviso_limiares`). Recusa as linhas além dos 30 dias do envio."""
    if limiares is not None and not isinstance(limiares, triagem_cenario.Limiares):
        raise ValueError("Limiares inválidos.")
    with db.session.begin_nested():
        lead = db.session.get(LeadAvaliacao, lead_id, with_for_update=True, populate_existing=True)
        if lead is None or lead.status == "expirado":
            raise LeadIndisponivel("Lead inexistente ou com os dados de PDV já expirados.")
        registros = list(db.session.scalars(
            select(BrainstormingDadosPdv).where(BrainstormingDadosPdv.lead_id == lead.id)
            .order_by(BrainstormingDadosPdv.ano_exercicio, BrainstormingDadosPdv.chave_produto).with_for_update()
        ))
        if not registros:
            raise LeadIndisponivel("O lead não tem dados de PDV gravados: envie os relatórios de novo.")
        if not dentro_da_retencao(min(r.criado_em for r in registros), agora):
            raise LeadIndisponivel("Os dados de PDV do lead passaram dos 30 dias de retenção: envie os "
                                   "relatórios de novo.")
        if {r.versao_regras for r in registros} != {avaliacao_motor.VERSAO_REGRAS_LEAD}:
            raise LeadIndisponivel("Os dados do lead foram gravados por outra versão das regras: envie os "
                                   "relatórios de novo.")
        aviso_limiares = None
        try:
            if limiares is None:
                limiares, aviso_limiares = limiares_para_retriagem(lead)
        except ValueError:
            raise LeadIndisponivel("Os limiares gravados no lead estão ilegíveis: refaça com flask leads retriar "
                                   "<id> --limiares ou envie os relatórios de novo.") from None
        try:
            meta = json.loads(lead.meta_json or "{}")
            cobertura = triagem_cenario.Cobertura.do_envio(meta)
            linhas = [linha_da_staging(r) for r in registros]
            triagem = triagem_cenario.triar(linhas, cobertura, limiares)
        except (ValueError, RecursionError, TypeError, AttributeError):
            raise LeadIndisponivel("O lead não guarda os metadados do envio: envie os relatórios de novo.") from None
        confiavel = not cobertura.cabecalho_ambiguo
        mudancas = []
        for registro, linha in zip(registros, linhas):
            marcas = triagem_cenario.marcas(linha, limiares, conflito_confiavel=confiavel)
            mudancas.append({"id": registro.id, **marcas, "bloco_triagem": triagem.blocos[linha["chave_produto"]]})
        vazamento_total = sum((m["vazamento_estimado_reais"] for m in mudancas), Decimal("0.00"))
        _conferir_tetos(mudancas, vazamento_total)
        db.session.execute(update(BrainstormingDadosPdv), mudancas)
        _aplicar_triagem(lead, triagem, vazamento_total)
        lead.resumo_inteligencia_json = resumo_da_inteligencia(linhas, cobertura, triagem, meta)
        db.session.flush()
    lead.aviso_limiares = aviso_limiares
    return lead
