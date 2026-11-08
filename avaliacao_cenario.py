"""Avaliação de Cenário (lead): grava o resultado do motor na staging e refaz a triagem.

Fluxo (consultor, rotas admin_required em blueprints/leads.py):
  1. processar(): raio_x_servico.processar_envio com executar=etl_runtime.run_avaliacao_cenario e
     origem="lead": o mesmo teto, a mesma assinatura da Baia, a recusa de arquivo repetido e o
     diretório temporário do Raio-X. O processo isolado lê, limpa e calcula (avaliacao_motor);
     nada é gravado e os arquivos somem com o diretório, sem guardar nem o nome deles.
  2. registrar(): depois do processo isolado e num SAVEPOINT, o lead (ou, no reenvio, a trava
     dele e a troca das linhas), as linhas produto x ano quantizadas como o banco guarda, as
     marcas e a triagem (triagem_cenario) e o resumo. Se o processo isolado falhar, nada é
     gravado: lead sem linhas nunca expiraria.
  3. retriar(): refaz marcas e triagem a partir da staging, sem os arquivos
     (`flask leads retriar <id>`), enquanto as linhas existirem (TTL de 30 dias).

Ordem de trava: o lead e depois as linhas, a mesma do expurgo (leads_avaliacao.py). Sem commit:
quem chama decide.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import json

from sqlalchemy import delete, insert, select, update

import avaliacao_motor
from etl_inputs import ETLInputError
import etl_runtime
from extensions import db
from horario import agora_utc
from models import BrainstormingDadosPdv, LeadAvaliacao
import raio_x
import raio_x_servico
import triagem_cenario

_CENTAVO = Decimal("0.01")
_MILESIMO = Decimal("0.001")
_DINHEIRO = ("valor_venda_bruto", "faturamento_com_custo", "custo_total", "perda", "vazamento_preco", "recuperavel")
_QUANTIDADES = ("quantidade", "quantidade_com_custo")
_SOMAS_GRAVADAS = (*_QUANTIDADES, *_DINHEIRO, "linhas", "meses", "variacao", "conflito_cadastral",
                   "estoque_negativo")
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
    """As linhas do worker (colunas e valores) como o banco as guarda: dinheiro com 2 casas e
    quantidade com 3, em Decimal. As marcas e a triagem saem sempre deste valor gravado."""
    colunas = linhas["colunas"]
    saida = []
    for valores in linhas["valores"]:
        linha = dict(zip(colunas, valores))
        for nome in _QUANTIDADES:
            linha[nome] = _decimal(linha[nome], _MILESIMO)
        for nome in _DINHEIRO:
            linha[nome] = _decimal(linha[nome], _CENTAVO)
        saida.append(linha)
    return saida


def _registros(linhas: list, triagem, versao: str) -> list:
    registros = []
    for linha in linhas:
        registro = {nome: linha[nome] for nome in ("ano_exercicio", "chave_produto", "codigo_produto",
                                                    "descricao_produto", *_SOMAS_GRAVADAS)}
        registro.update(triagem_cenario.marcas(linha))
        registro["bloco_triagem"] = triagem.blocos[linha["chave_produto"]]
        registro["versao_regras"] = versao
        registros.append(registro)
    return registros


def _conferir_tetos(registros: list, vazamento_total: Decimal) -> None:
    """Derivados que o processo isolado não vê: custo médio NUMERIC(14,4), vazamento estimado e
    vazamento total NUMERIC(12,2). Recusa com mensagem em vez de derrubar o SAVEPOINT."""
    tetos = avaliacao_motor.TETOS
    for registro in registros:
        custo_medio = registro["custo_medio_registrado"]
        if ((custo_medio is not None and custo_medio >= tetos["custo_medio_registrado"])
                or registro["vazamento_estimado_reais"] >= tetos["vazamento_estimado_r$"]):
            raise ETLInputError("avaliacao_valor", "Há quantidade ou valor acima do que a avaliação aceita nos "
                                "relatórios. Confira as colunas de quantidade, valor e custo.")
    if vazamento_total >= _TETO_VAZAMENTO_TOTAL:
        raise ETLInputError("avaliacao_valor", "O valor recuperável somado passa do que a avaliação aceita. "
                            "Confira os relatórios enviados.")


def _json(valor) -> str:
    return json.dumps(valor, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _aplicar_triagem(lead: LeadAvaliacao, triagem, vazamento_total: Decimal) -> None:
    lead.resumo_urgencia_json = _json(triagem.resumos["urgencia"])
    lead.resumo_atencao_json = _json(triagem.resumos["atencao"])
    lead.resumo_eletiva_json = _json(triagem.resumos["eletiva"])
    lead.versao_triagem = triagem_cenario.VERSAO_TRIAGEM
    lead.itens_venda_sem_custo = triagem.itens_venda_sem_custo
    lead.itens_venda_abaixo_custo = triagem.itens_venda_abaixo_custo
    lead.vazamento_total_reais = vazamento_total
    lead.atualizado_em = agora_utc()


def registrar(resultado: dict, *, contato: dict | None = None, lead_id=None, registrado_por: int | None = None,
              meses_prata: int | None = None) -> LeadAvaliacao:
    """Grava (sem commit) o resultado do motor: um lead novo com `contato`, ou o lead `lead_id`
    com as linhas trocadas pelas do novo envio. Devolve o lead."""
    if (contato is None) == (lead_id is None):
        raise ValueError("Informe o contato de um lead novo ou o lead do reenvio.")
    try:
        # Tudo o que sai do processo isolado e vira coluna: o formato já foi conferido
        # (etl_runtime.validar_avaliacao); o que ainda falhar aqui é resultado inválido, e não erro 500.
        linhas = quantizar(resultado["linhas"])
        meta = resultado["meta"]
        triagem = triagem_cenario.triar(linhas, triagem_cenario.Cobertura.do_envio(meta))
        registros = _registros(linhas, triagem, resultado["versao"])
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
        _aplicar_triagem(lead, triagem, vazamento_total)
        lead.status = "concluido"
        db.session.flush()
    return lead


def linha_da_staging(registro: BrainstormingDadosPdv) -> dict:
    """A linha gravada no formato que a triagem lê (somas em Decimal)."""
    linha = {nome: getattr(registro, nome) for nome in ("ano_exercicio", "chave_produto", "codigo_produto",
                                                         "descricao_produto", *_SOMAS_GRAVADAS)}
    for nome in (*_QUANTIDADES, *_DINHEIRO):
        linha[nome] = Decimal(linha[nome])
    return linha


def retriar(lead_id) -> LeadAvaliacao:
    """Refaz (sem commit) marcas e triagem do lead a partir das linhas gravadas."""
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
        if {r.versao_regras for r in registros} != {avaliacao_motor.VERSAO_REGRAS_LEAD}:
            raise LeadIndisponivel("Os dados do lead foram gravados por outra versão das regras: envie os "
                                   "relatórios de novo.")
        try:
            cobertura = triagem_cenario.Cobertura.do_envio(json.loads(lead.meta_json or "{}"))
            linhas = [linha_da_staging(r) for r in registros]
            triagem = triagem_cenario.triar(linhas, cobertura)
        except ValueError:
            raise LeadIndisponivel("O lead não guarda os metadados do envio: envie os relatórios de novo.") from None
        mudancas = []
        for registro, linha in zip(registros, linhas):
            marcas = triagem_cenario.marcas(linha)
            mudancas.append({"id": registro.id, **marcas, "bloco_triagem": triagem.blocos[linha["chave_produto"]]})
        vazamento_total = sum((m["vazamento_estimado_reais"] for m in mudancas), Decimal("0.00"))
        _conferir_tetos(mudancas, vazamento_total)
        db.session.execute(update(BrainstormingDadosPdv), mudancas)
        _aplicar_triagem(lead, triagem, vazamento_total)
        db.session.flush()
    return lead
