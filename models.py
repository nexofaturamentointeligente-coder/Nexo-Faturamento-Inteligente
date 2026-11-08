"""
NEXO - Faturamento Inteligente | Modelos SQLAlchemy
====================================================
Apenas as 8 tabelas em uso real no MVP do PI2:

    plano, segmento, empresa, usuario,
    analise, upload_relatorio, indicador_analise, relatorio_analise.

As 4 tabelas FORA DO ESCOPO PI2 (chamado_suporte, mensagem_suporte,
avaliacao_analise, fatura_cobranca) existem no schema SQL como
documentação do modelo lógico completo, mas NÃO têm Model aqui.
Quando voltarem ao escopo pós-PI3, adicione as classes correspondentes.

Estilo: SQLAlchemy 2.x com `Mapped` e `mapped_column` (mais moderno
e type-safe que o estilo Column antigo).
"""

from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Optional
import uuid

from sqlalchemy import (
    String, Integer, Boolean, Date, DateTime, Numeric, Text, LargeBinary, Uuid,
    ForeignKey, ForeignKeyConstraint, CheckConstraint, UniqueConstraint, Index, false, text, true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from sqlalchemy.types import TypeDecorator
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db
from horario import agora_utc


class InstanteUTC(TypeDecorator):
    """Instante absoluto em UTC: timestamptz no PostgreSQL.

    O SQLite não guarda fuso; grava-se sempre UTC no mesmo formato, o que
    mantém as comparações de prazo (SQL) corretas. Valor sem fuso é recusado
    na escrita para que um horário local nunca seja tomado por UTC.
    """
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Instante contratual exige fuso horário explícito.")
        valor = value.astimezone(timezone.utc)
        return valor.replace(tzinfo=None) if dialect.name == "sqlite" else valor

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class InstanteUTCSemFuso(TypeDecorator):
    """Instante absoluto numa coluna legada `timestamp without time zone`.

    Convenção do suporte e do sininho (horario.py): a coluna guarda o relógio
    UTC, sem fuso. A escrita exige fuso explícito e converte para UTC aqui,
    então o TimeZone da sessão do banco não muda o valor gravado. A leitura
    devolve o instante com fuso UTC. O tipo da coluna continua o mesmo, sem
    migração de esquema nem de dados.
    """
    impl = DateTime()
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Horário do suporte exige fuso horário explícito.")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


# =====================================================================
# 1. Plano
# =====================================================================
class Plano(db.Model):
    __tablename__ = "plano"

    id_plano: Mapped[int] = mapped_column(primary_key=True)
    nome_plano: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    descricao: Mapped[Optional[str]] = mapped_column(Text)
    valor_mensal: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    qtd_analises_mes: Mapped[int] = mapped_column(Integer, nullable=False)
    tipo_analise_permitida: Mapped[str] = mapped_column(String, nullable=False)
    nivel_entrega_analise: Mapped[str] = mapped_column(String, nullable=False)
    nivel_dashboard: Mapped[str] = mapped_column(String, nullable=False)
    nivel_atendimento: Mapped[str] = mapped_column(String, nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    data_criacao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (
        CheckConstraint("nome_plano IN ('BRONZE', 'PRATA', 'OURO')"),
        CheckConstraint("tipo_analise_permitida IN ('MENSAL', 'QUINZENAL')"),
        CheckConstraint("nivel_entrega_analise IN ('BASICA', 'COMPLETA', 'PREMIUM')"),
        CheckConstraint("nivel_dashboard IN ('RESUMIDO', 'GERENCIAL', 'COMPLETO')"),
        CheckConstraint("nivel_atendimento IN ('BAIXO', 'MEDIO', 'ALTO')"),
    )


# =====================================================================
# 2. Segmento
# =====================================================================
class Segmento(db.Model):
    __tablename__ = "segmento"

    id_segmento: Mapped[int] = mapped_column(primary_key=True)
    nome_segmento: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    descricao: Mapped[Optional[str]] = mapped_column(Text)
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    data_criacao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(DateTime)


# =====================================================================
# 3. Empresa
# =====================================================================
class Empresa(db.Model):
    __tablename__ = "empresa"

    id_empresa: Mapped[int] = mapped_column(primary_key=True)
    id_segmento: Mapped[int] = mapped_column(ForeignKey("segmento.id_segmento"), nullable=False)
    id_plano_atual: Mapped[int] = mapped_column(ForeignKey("plano.id_plano"), nullable=False)
    cnpj: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    razao_social: Mapped[str] = mapped_column(String, nullable=False)
    nome_fantasia: Mapped[Optional[str]] = mapped_column(String)
    email_contato: Mapped[str] = mapped_column(String, nullable=False)
    telefone_contato: Mapped[Optional[str]] = mapped_column(String)
    data_contratacao: Mapped[date] = mapped_column(Date, nullable=False)
    faturamento_base_mensal: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    status_conta: Mapped[str] = mapped_column(String, nullable=False, default="ATIVA")
    data_criacao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # Soft delete (Lixeira): NULL = ativa; preenchido = movida para a lixeira.
    # As listagens normais filtram deletado_em IS NULL; a tela /admin/lixeira
    # lista o complemento. A exclusão física só ocorre via "excluir-permanente".
    deletado_em: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Relacionamentos
    segmento = relationship("Segmento")
    plano_atual = relationship("Plano")
    usuarios = relationship("Usuario", back_populates="empresa")
    analises = relationship("Analise", back_populates="empresa")

    __table_args__ = (
        CheckConstraint("status_conta IN ('ATIVA', 'SUSPENSA', 'CANCELADA')"),
    )


# =====================================================================
# 4. Usuario
# =====================================================================
class Usuario(db.Model, UserMixin):
    """
    Usuário do sistema. UserMixin do Flask-Login fornece `is_authenticated`,
    `is_active`, `get_id()` etc., evitando boilerplate.
    """
    __tablename__ = "usuario"

    id_usuario: Mapped[int] = mapped_column(primary_key=True)
    id_empresa: Mapped[Optional[int]] = mapped_column(ForeignKey("empresa.id_empresa"))
    nome: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    senha_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # SEC-004: revogacao persistente, independente da senha e do estado ativo.
    geracao_autorizacao: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Primeiro acesso: novos clientes (criados via ORM) nascem com True e são
    # obrigados a trocar a senha temporária no 1º login. server_default="false"
    # garante que usuários JÁ existentes não sejam disruptados pela migração.
    primeiro_acesso: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="false"
    )
    ultimo_acesso: Mapped[Optional[datetime]] = mapped_column(DateTime)
    data_criacao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(DateTime)

    empresa = relationship("Empresa", back_populates="usuarios")

    __table_args__ = (
        CheckConstraint("role IN ('ADMIN', 'CLIENTE')"),
        CheckConstraint(
            "geracao_autorizacao >= 0", name="ck_usuario_geracao_autorizacao"
        ),
        CheckConstraint(
            "(role = 'CLIENTE' AND id_empresa IS NOT NULL) OR "
            "(role = 'ADMIN'   AND id_empresa IS NULL)",
            name="ck_usuario_role_empresa"
        ),
    )

    # --- Override do Flask-Login: usa nossa PK customizada ---
    def get_id(self) -> str:
        return str(self.id_usuario)

    @property
    def is_active(self) -> bool:
        return self.ativo

    # --- Helpers de senha ---
    def set_senha(self, senha_plain: str) -> None:
        self.senha_hash = generate_password_hash(senha_plain)

    def check_senha(self, senha_plain: str) -> bool:
        return check_password_hash(self.senha_hash, senha_plain)

    # --- Helpers de role ---
    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"

    @property
    def is_cliente(self) -> bool:
        return self.role == "CLIENTE"


# =====================================================================
# 4b. SessaoAutenticada  (SEC-001: sessões revogáveis no servidor)
# =====================================================================
MOTIVOS_REVOGACAO_SESSAO = ("logout", "troca_senha", "revogacao_global", "empresa", "administrativo")


class SessaoAutenticada(db.Model):
    """Uma linha por login. O cookie carrega só um identificador aleatório;
    aqui fica o seu hash SHA-256, o dono, a validade e a revogação.

    Sem a linha válida (ou com revogada_em preenchido) o cookie deixa de
    autorizar HTTP e Socket.IO, em qualquer processo, mesmo que a assinatura
    continue íntegra. O logout revoga só a sessão atual; a troca de senha,
    todas as do usuário. A geração de autorização (lixeira/cancelamento) e o
    BOOT_ID continuam valendo em cima desta verificação.
    """
    __tablename__ = "sessao_autenticada"

    id_sessao: Mapped[int] = mapped_column(primary_key=True)
    # Usuário apagado do banco não deixa sessão: a linha cai com ele (CASCADE).
    id_usuario: Mapped[int] = mapped_column(
        ForeignKey("usuario.id_usuario", ondelete="CASCADE"), nullable=False,
    )
    identificador_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    criada_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False)
    expira_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False)
    revogada_em: Mapped[Optional[datetime]] = mapped_column(InstanteUTC())
    motivo_revogacao: Mapped[Optional[str]] = mapped_column(String(40))

    usuario = relationship("Usuario")

    __table_args__ = (
        UniqueConstraint("identificador_hash", name="uq_sessao_identificador"),
        CheckConstraint("expira_em > criada_em", name="ck_sessao_validade"),
        CheckConstraint(
            "(revogada_em IS NULL AND motivo_revogacao IS NULL) OR "
            "(revogada_em IS NOT NULL AND motivo_revogacao IS NOT NULL)",
            name="ck_sessao_revogacao",
        ),
        Index("ix_sessao_usuario", "id_usuario"),
    )


# =====================================================================
# 5. Analise
# =====================================================================
class Analise(db.Model):
    __tablename__ = "analise"

    id_analise: Mapped[int] = mapped_column(primary_key=True)
    id_empresa: Mapped[int] = mapped_column(ForeignKey("empresa.id_empresa"), nullable=False)
    id_plano_referencia: Mapped[int] = mapped_column(ForeignKey("plano.id_plano"), nullable=False)
    id_usuario_admin_responsavel: Mapped[int] = mapped_column(
        ForeignKey("usuario.id_usuario"), nullable=False
    )
    periodo_inicio: Mapped[date] = mapped_column(Date, nullable=False)
    periodo_fim: Mapped[date] = mapped_column(Date, nullable=False)
    mes_referencia: Mapped[int] = mapped_column(Integer, nullable=False)
    ano_referencia: Mapped[int] = mapped_column(Integer, nullable=False)
    tipo_analise: Mapped[str] = mapped_column(String, nullable=False)
    quinzena_referencia: Mapped[Optional[int]] = mapped_column(Integer)
    status_analise: Mapped[str] = mapped_column(
        String, nullable=False, default="AGUARDANDO_RELATORIO"
    )
    data_criacao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(DateTime)
    data_conclusao: Mapped[Optional[datetime]] = mapped_column(DateTime)

    empresa = relationship("Empresa", back_populates="analises")
    plano_referencia = relationship("Plano")
    admin_responsavel = relationship("Usuario")
    # cascade="all, delete-orphan" no nível do ORM: ao deletar uma Analise, o
    # SQLAlchemy remove automaticamente os filhos (uploads, indicador, relatório
    # e Curva ABC) na ordem correta — sem precisar de ON DELETE CASCADE no banco
    # (migration-free) e mantendo a rede de proteção das FKs intacta. Estes
    # registros não têm sentido sem a análise-mãe (ownership forte).
    uploads = relationship(
        "UploadRelatorio", back_populates="analise",
        cascade="all, delete-orphan",
    )
    indicador = relationship(
        "IndicadorAnalise", back_populates="analise", uselist=False,
        cascade="all, delete-orphan",
    )
    relatorio = relationship(
        "RelatorioAnalise", back_populates="analise", uselist=False,
        cascade="all, delete-orphan",
    )
    curva_abc = relationship(
        "ProdutoCurvaABC",
        back_populates="analise",
        order_by="ProdutoCurvaABC.posicao_ranking",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint("mes_referencia BETWEEN 1 AND 12"),
        CheckConstraint("tipo_analise IN ('MENSAL', 'QUINZENAL')"),
        CheckConstraint(
            "status_analise IN ('AGUARDANDO_RELATORIO', 'RELATORIO_RECEBIDO', "
            "'EM_ANALISE', 'CONCLUIDO')"
        ),
        CheckConstraint(
            "(tipo_analise = 'MENSAL'    AND quinzena_referencia IS NULL) OR "
            "(tipo_analise = 'QUINZENAL' AND quinzena_referencia IS NOT NULL "
            "                            AND quinzena_referencia IN (1, 2))",
            name="ck_analise_tipo_quinzena"
        ),
    )


# =====================================================================
# 6. UploadRelatorio
# =====================================================================
class UploadRelatorio(db.Model):
    __tablename__ = "upload_relatorio"

    id_upload: Mapped[int] = mapped_column(primary_key=True)
    id_analise: Mapped[int] = mapped_column(ForeignKey("analise.id_analise"), nullable=False)
    # Quem enviou o arquivo. NULL = enviado pelo PRÓPRIO CLIENTE (self-service
    # onboarding) e ainda aguardando homologação do consultor. Preenchido com o
    # id do ADMIN quando o upload é feito/homologado pelo time NEXO.
    id_usuario_admin: Mapped[Optional[int]] = mapped_column(ForeignKey("usuario.id_usuario"))
    tipo_relatorio: Mapped[str] = mapped_column(String, nullable=False)
    nome_arquivo_original: Mapped[str] = mapped_column(String, nullable=False)
    caminho_arquivo: Mapped[str] = mapped_column(String, nullable=False)
    extensao_arquivo: Mapped[str] = mapped_column(String, nullable=False)
    tamanho_arquivo: Mapped[Optional[int]] = mapped_column(Integer)
    hash_arquivo: Mapped[Optional[str]] = mapped_column(String)
    data_upload: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    status_processamento: Mapped[str] = mapped_column(
        String, nullable=False, default="PENDENTE"
    )
    data_processamento: Mapped[Optional[datetime]] = mapped_column(DateTime)
    mensagem_erro: Mapped[Optional[str]] = mapped_column(Text)

    analise = relationship("Analise", back_populates="uploads")

    __table_args__ = (
        CheckConstraint("tipo_relatorio IN ('VENDAS', 'COMPRAS')"),
        CheckConstraint("extensao_arquivo IN ('CSV', 'XLSX', 'XLS')"),
        CheckConstraint("status_processamento IN ('PENDENTE', 'PROCESSADO', 'ERRO')"),
        UniqueConstraint("id_analise", "tipo_relatorio", name="uq_upload_analise_tipo"),
    )

    @property
    def enviado_por_cliente(self) -> bool:
        """True quando o arquivo foi anexado pelo próprio cliente (sem admin),
        aguardando homologação do consultor."""
        return self.id_usuario_admin is None


# =====================================================================
# 7. IndicadorAnalise
# =====================================================================
class IndicadorAnalise(db.Model):
    __tablename__ = "indicador_analise"

    id_indicador: Mapped[int] = mapped_column(primary_key=True)
    id_analise: Mapped[int] = mapped_column(
        ForeignKey("analise.id_analise"), nullable=False, unique=True
    )
    faturamento_total: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    total_comprado: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    saldo_estimado_compras_vendas: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    produto_mais_vendido_nome: Mapped[Optional[str]] = mapped_column(String)
    produto_mais_vendido_quantidade: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    produto_maior_faturamento_nome: Mapped[Optional[str]] = mapped_column(String)
    produto_maior_faturamento_valor: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    produto_maior_saldo_parado_nome: Mapped[Optional[str]] = mapped_column(String)
    saldo_estimado_parado: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    # Período REAL coberto pela base, lido do cabeçalho do relatório do PDV
    # (via pandas no ETL). Pode ser nulo se o relatório não declarar período.
    periodo_base_inicio: Mapped[Optional[date]] = mapped_column(Date)
    periodo_base_fim: Mapped[Optional[date]] = mapped_column(Date)
    versao_processamento: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    data_geracao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    analise = relationship("Analise", back_populates="indicador")


# =====================================================================
# 8. RelatorioAnalise
# =====================================================================
class RelatorioAnalise(db.Model):
    __tablename__ = "relatorio_analise"

    id_relatorio: Mapped[int] = mapped_column(primary_key=True)
    id_analise: Mapped[int] = mapped_column(
        ForeignKey("analise.id_analise"), nullable=False, unique=True
    )
    titulo: Mapped[str] = mapped_column(String, nullable=False)
    resumo_executivo: Mapped[Optional[str]] = mapped_column(Text)
    pontos_positivos: Mapped[Optional[str]] = mapped_column(Text)
    pontos_de_alerta: Mapped[Optional[str]] = mapped_column(Text)
    recomendacoes: Mapped[Optional[str]] = mapped_column(Text)
    # NOT NULL: conclusao_estrategica é o produto entregue ao cliente.
    conclusao_estrategica: Mapped[str] = mapped_column(Text, nullable=False)
    publicado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    data_publicacao: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # True quando resumo/conclusão foram gerados pelo motor de IA (Claude).
    # Usado para exibir, com honestidade, o selo "Gerado por IA Nexo" ao cliente.
    # server_default garante a migração segura sobre linhas já existentes.
    gerado_por_ia: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # SEC-003: o que o cliente vê é a CÓPIA publicada, congelada no ato de
    # publicar. Os campos acima são o rascunho em edição (IA, "Salvar rascunho").
    # Relatórios publicados antes desta versão não têm cópia: a leitura cai
    # no rascunho até a primeira edição, que congela o texto vigente antes de
    # alterá-lo (ver blueprints/admin.py). Nenhum dado é migrado em massa.
    titulo_publicado: Mapped[Optional[str]] = mapped_column(String)
    resumo_executivo_publicado: Mapped[Optional[str]] = mapped_column(Text)
    conclusao_estrategica_publicada: Mapped[Optional[str]] = mapped_column(Text)
    gerado_por_ia_publicado: Mapped[Optional[bool]] = mapped_column(Boolean)
    # Controle otimista do editor: o formulário devolve a versão exibida e o
    # UPDATE só vale se ela ainda for a atual (version_id_col: SQLAlchemy
    # incrementa a cada flush e levanta StaleDataError se outra sessão gravou).
    versao_rascunho: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    __mapper_args__ = {"version_id_col": versao_rascunho}

    analise = relationship("Analise", back_populates="relatorio")

    @property
    def tem_copia_publicada(self) -> bool:
        return self.conclusao_estrategica_publicada is not None

    @property
    def rascunho_difere_da_publicacao(self) -> bool:
        """Há texto editado (ou gerado) ainda não entregue ao cliente."""
        if not self.publicado:
            return False
        publicada = self.publicada
        return (self.titulo, self.resumo_executivo, self.conclusao_estrategica, bool(self.gerado_por_ia)) != (
            publicada.titulo, publicada.resumo_executivo, publicada.conclusao_estrategica,
            publicada.gerado_por_ia,
        )

    @property
    def publicada(self):
        """Versão visível ao cliente e ao PDF: a cópia publicada ou, para
        relatórios publicados antes da cópia existir, o rascunho (legado)."""
        if self.tem_copia_publicada:
            return VersaoPublicada(
                self.titulo_publicado or self.titulo,
                self.resumo_executivo_publicado,
                self.conclusao_estrategica_publicada,
                bool(self.gerado_por_ia_publicado),
                self.data_publicacao, False,
            )
        return VersaoPublicada(
            self.titulo, self.resumo_executivo, self.conclusao_estrategica,
            bool(self.gerado_por_ia), self.data_publicacao, True,
        )

    def congelar_publicacao(self) -> None:
        """Copia o rascunho para os campos publicados (ato explícito de publicar)."""
        self.titulo_publicado = self.titulo
        self.resumo_executivo_publicado = self.resumo_executivo
        self.conclusao_estrategica_publicada = self.conclusao_estrategica
        self.gerado_por_ia_publicado = bool(self.gerado_por_ia)

    def preservar_publicacao_legada(self) -> None:
        """Antes de editar um relatório publicado sem cópia: congela o texto
        que o cliente já vê, para a edição não o alterar."""
        if self.publicado and not self.tem_copia_publicada:
            self.congelar_publicacao()


class VersaoPublicada:
    """Leitura imutável do texto publicado (sem acesso ao rascunho)."""
    __slots__ = ("titulo", "resumo_executivo", "conclusao_estrategica", "gerado_por_ia",
                 "data_publicacao", "legado")

    def __init__(self, titulo, resumo_executivo, conclusao_estrategica, gerado_por_ia,
                 data_publicacao, legado):
        self.titulo = titulo
        self.resumo_executivo = resumo_executivo
        self.conclusao_estrategica = conclusao_estrategica
        self.gerado_por_ia = gerado_por_ia
        self.data_publicacao = data_publicacao
        self.legado = legado


# =====================================================================
# 9. ProdutoCurvaABC  (embasamento metodológico — Princípio de Pareto)
# =====================================================================
class ProdutoCurvaABC(db.Model):
    """
    Ranking de produtos por faturamento de uma análise, classificados pela
    Curva ABC (Princípio de Pareto / regra 80-20):

        Classe A (Estratégicos):  produtos até 80% do faturamento acumulado.
        Classe B (Táticos):       de 80% a 95% do faturamento acumulado.
        Classe C (Operacionais):  os ~5% restantes (cauda longa).

    Diferente de indicador_analise (KPIs consolidados 1:1 com a análise), aqui
    persistimos uma LINHA POR PRODUTO do período, o que viabiliza a tabela completa
    e o gráfico de barras horizontais coloridas por classe no painel do cliente.

    Mantém a regra inegociável do ETL: NÃO há lucro/margem/CMV — apenas
    faturamento agregado por produto, que é dado bruto honesto do PDV.
    """
    __tablename__ = "produto_curva_abc"

    id_produto_abc: Mapped[int] = mapped_column(primary_key=True)
    id_analise: Mapped[int] = mapped_column(ForeignKey("analise.id_analise"), nullable=False)
    posicao_ranking: Mapped[int] = mapped_column(Integer, nullable=False)
    produto_nome: Mapped[str] = mapped_column(String, nullable=False)
    faturamento: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    quantidade: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    percentual_individual: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    percentual_acumulado: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    classe_abc: Mapped[str] = mapped_column(String, nullable=False)
    data_geracao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    analise = relationship("Analise", back_populates="curva_abc")

    __table_args__ = (
        CheckConstraint("classe_abc IN ('A', 'B', 'C')"),
        UniqueConstraint("id_analise", "posicao_ranking", name="uq_abc_analise_posicao"),
        Index("ix_abc_analise", "id_analise"),
    )


# =====================================================================
# 10. ChamadoSuporte  (REATIVAÇÃO da tabela documentada chamado_suporte)
# =====================================================================
class ChamadoSuporte(db.Model):
    """
    Ticket de suporte aberto por um usuário CLIENTE para a equipe NEXO.
    Reativa a tabela `chamado_suporte` que já existia no DER como documentação
    (pós-PI3), adaptada ao escopo atual:
      - adicionado `categoria` (Financeiro / Dúvida Técnica / Erro de Integração);
      - `status_chamado` simplificado para 3 estados (ABERTO / EM_ATENDIMENTO /
        RESOLVIDO);
      - `prioridade` mantido (documentado), default MEDIA — não exposto na UI.
    O campo `assunto` é o título do ticket.
    """
    __tablename__ = "chamado_suporte"

    id_chamado: Mapped[int] = mapped_column(primary_key=True)
    id_empresa: Mapped[int] = mapped_column(ForeignKey("empresa.id_empresa"), nullable=False)
    id_usuario_cliente: Mapped[int] = mapped_column(ForeignKey("usuario.id_usuario"), nullable=False)
    assunto: Mapped[str] = mapped_column(String, nullable=False)
    categoria: Mapped[str] = mapped_column(String, nullable=False)
    status_chamado: Mapped[str] = mapped_column(String, nullable=False, default="ABERTO")
    prioridade: Mapped[str] = mapped_column(String, nullable=False, default="MEDIA")
    # Horários em UTC (InstanteUTCSemFuso); o relógio é o do Python, não o
    # CURRENT_TIMESTAMP da sessão. O server_default só espelha o esquema.
    data_abertura: Mapped[datetime] = mapped_column(
        InstanteUTCSemFuso(), nullable=False, default=agora_utc, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(InstanteUTCSemFuso())
    data_fechamento: Mapped[Optional[datetime]] = mapped_column(InstanteUTCSemFuso())

    empresa = relationship("Empresa")
    usuario_cliente = relationship("Usuario")
    mensagens = relationship(
        "MensagemSuporte",
        back_populates="chamado",
        order_by="[MensagemSuporte.data_envio, MensagemSuporte.id_mensagem]",
    )

    __table_args__ = (
        CheckConstraint(
            "categoria IN ('FINANCEIRO', 'DUVIDA_TECNICA', 'ERRO_INTEGRACAO')"
        ),
        CheckConstraint(
            "status_chamado IN ('ABERTO', 'EM_ATENDIMENTO', 'RESOLVIDO')"
        ),
        CheckConstraint("prioridade IN ('BAIXA', 'MEDIA', 'ALTA')"),
        Index("ix_chamado_empresa", "id_empresa"),
    )


# =====================================================================
# 11. MensagemSuporte  (REATIVAÇÃO da tabela documentada mensagem_suporte)
# =====================================================================
class MensagemSuporte(db.Model):
    """Mensagem dentro de um chamado (conversa cliente ↔ consultor)."""
    __tablename__ = "mensagem_suporte"

    id_mensagem: Mapped[int] = mapped_column(primary_key=True)
    id_chamado: Mapped[int] = mapped_column(ForeignKey("chamado_suporte.id_chamado"), nullable=False)
    id_usuario_remetente: Mapped[int] = mapped_column(ForeignKey("usuario.id_usuario"), nullable=False)
    mensagem: Mapped[str] = mapped_column(Text, nullable=False)
    data_envio: Mapped[datetime] = mapped_column(
        InstanteUTCSemFuso(), nullable=False, default=agora_utc, server_default=func.current_timestamp()
    )
    lida: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    chamado = relationship("ChamadoSuporte", back_populates="mensagens")
    remetente = relationship("Usuario")

    __table_args__ = (
        Index("ix_mensagem_chamado", "id_chamado"),
    )


# =====================================================================
# 12. Notificacao  (sininho da navbar — engajamento)
# =====================================================================
class Notificacao(db.Model):
    """
    Notificação dirigida a UM usuário (cliente ou admin). Alimenta o sininho
    da navbar. Gerada por gatilhos do sistema (upload de cliente, homologação,
    publicação de análise, resposta em ticket).
    """
    __tablename__ = "notificacao"

    id_notificacao: Mapped[int] = mapped_column(primary_key=True)
    id_usuario: Mapped[int] = mapped_column(ForeignKey("usuario.id_usuario"), nullable=False)
    texto: Mapped[str] = mapped_column(String, nullable=False)
    link_destino: Mapped[Optional[str]] = mapped_column(String)
    lida: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Preenchido no flush (UTC): o payload do Socket.IO usa este mesmo instante.
    data_criacao: Mapped[datetime] = mapped_column(
        InstanteUTCSemFuso(), nullable=False, default=agora_utc, server_default=func.current_timestamp()
    )

    usuario = relationship("Usuario")

    __table_args__ = (
        Index("ix_notificacao_usuario", "id_usuario"),
    )


# =====================================================================
# 13. GuiaTopico  (CMS — Base de Conhecimento do Guia / NexoBot)
# =====================================================================
class GuiaTopico(db.Model):
    """
    Tópico da Base de Conhecimento, gerenciável pelo ADMIN (CMS). Fonte ÚNICA
    de verdade consumida pela aba "Guia" do cliente E pelo NexoBot (DRY).
      - categoria: agrupador (ex.: "Upload", "Senhas", "PDV");
      - pergunta:  título da dúvida (vira o cabeçalho do accordion);
      - resposta:  texto livre (passo a passo, quebras de linha preservadas);
      - imagem_url: caminho relativo em static/ (ex.: 'uploads/guia/x.png'), opcional.
    """
    __tablename__ = "guia_topico"

    id: Mapped[int] = mapped_column(primary_key=True)
    categoria: Mapped[str] = mapped_column(String, nullable=False)
    pergunta: Mapped[str] = mapped_column(String, nullable=False)
    resposta: Mapped[str] = mapped_column(Text, nullable=False)
    imagem_url: Mapped[Optional[str]] = mapped_column(String)
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    data_criacao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_guia_topico_categoria", "categoria"),
    )


# =====================================================================
# 14. ArquivoArmazenado  (conteúdo dos arquivos enviados, dentro do Postgres)
# =====================================================================
class ArquivoArmazenado(db.Model):
    """
    Bytes dos arquivos enviados: planilhas VENDAS/COMPRAS e imagens do Guia.
    Existe porque hosts gratuitos (ex.: Render Free) têm disco EFÊMERO — tudo
    gravado no filesystem some a cada deploy, restart ou hibernação. No banco,
    o arquivo sobrevive e entra na MESMA transação do registro que o cita
    (rollback do upload desfaz o arquivo também). Acesso só via storage.py.
      - chave: nome único (ex.: 'analise_7_VENDAS_ab12cd34.xlsx', 'guia/<hash>.png').
        As outras tabelas referenciam o arquivo como 'db:' + chave.
      - conteudo: deferred — só é lido do banco quando realmente usado.
    """
    __tablename__ = "arquivo_armazenado"

    chave: Mapped[str] = mapped_column(String(255), primary_key=True)
    conteudo: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)
    content_type: Mapped[Optional[str]] = mapped_column(String(100))
    tamanho: Mapped[int] = mapped_column(Integer, nullable=False)
    data_criacao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


# =====================================================================
# 15. RegimeContratual  (habilitação gradual da gestão contratual)
# =====================================================================
class RegimeContratual(db.Model):
    """
    Presença da linha = regras contratuais ativas para a empresa. Sem ela, a
    empresa segue o regime legado até a conciliação administrativa. Tabela
    própria (e não coluna em Empresa) para que o código possa chegar à
    produção antes da migração sem quebrar consultas de Empresa (padrão
    expand -> migrate -> deploy da Etapa 0.2). Ativação e desativação ficam
    registradas em EventoContratual.
    """
    __tablename__ = "regime_contratual"

    id_empresa: Mapped[int] = mapped_column(
        ForeignKey("empresa.id_empresa"), primary_key=True, autoincrement=False
    )
    ativado_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False)
    id_usuario_admin_ativacao: Mapped[int] = mapped_column(
        ForeignKey("usuario.id_usuario"), nullable=False
    )


# =====================================================================
# 16. Contrato  (gestão contratual)
# =====================================================================
SITUACOES_CONTRATO = ("ATIVO", "SUSPENSO", "ENCERRADO", "CANCELADO")
# Situações que fazem do contrato o vínculo vigente da empresa (no máximo um).
SITUACOES_CONTRATO_APLICAVEL = ("ATIVO", "SUSPENSO")


class Contrato(db.Model):
    """
    Acordo específico entre a NEXO e uma empresa. Guarda um snapshot do valor
    contratado: o catálogo de Plano pode mudar sem afetar contratos antigos.
    Renovação e troca de plano criam um NOVO contrato ligado ao anterior,
    que passa a ENCERRADO; nada é sobrescrito. Os prazos da suspensão são
    instantes UTC calculados por contract_policy no momento da suspensão:
    o original fica preservado e a prorrogação só move o limite vigente.
    """
    __tablename__ = "contrato"

    id_contrato: Mapped[int] = mapped_column(primary_key=True)
    id_empresa: Mapped[int] = mapped_column(ForeignKey("empresa.id_empresa"), nullable=False)
    id_plano: Mapped[int] = mapped_column(ForeignKey("plano.id_plano"), nullable=False)
    # Contrato renovado/substituído por este (mesma empresa, ver FK composta).
    id_contrato_anterior: Mapped[Optional[int]] = mapped_column(Integer)
    valor_mensal_contratado: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    data_inicio: Mapped[date] = mapped_column(Date, nullable=False)
    # NULL = prazo indeterminado (a duração dos contratos é decisão comercial).
    data_fim: Mapped[Optional[date]] = mapped_column(Date)
    data_formalizacao: Mapped[Optional[date]] = mapped_column(Date)
    referencia_formalizacao: Mapped[Optional[str]] = mapped_column(String(200))
    situacao: Mapped[str] = mapped_column(String(20), nullable=False, default="ATIVO")
    # Prazo de suspensão menor previsto em contrato; NULL = padrão configurado.
    meses_suspensao: Mapped[Optional[int]] = mapped_column(Integer)
    suspenso_em: Mapped[Optional[datetime]] = mapped_column(InstanteUTC())
    suspensao_motivo: Mapped[Optional[str]] = mapped_column(Text)
    suspensao_limite_original: Mapped[Optional[datetime]] = mapped_column(InstanteUTC())
    suspensao_limite: Mapped[Optional[datetime]] = mapped_column(InstanteUTC())
    pdf_limite_original: Mapped[Optional[datetime]] = mapped_column(InstanteUTC())
    pdf_limite: Mapped[Optional[datetime]] = mapped_column(InstanteUTC())
    encerrado_em: Mapped[Optional[datetime]] = mapped_column(InstanteUTC())
    cancelado_em: Mapped[Optional[datetime]] = mapped_column(InstanteUTC())
    cancelamento_motivo: Mapped[Optional[str]] = mapped_column(Text)
    observacoes: Mapped[Optional[str]] = mapped_column(Text)
    id_usuario_admin_responsavel: Mapped[int] = mapped_column(
        ForeignKey("usuario.id_usuario"), nullable=False
    )
    # Controle otimista: os formulários enviam a versão exibida.
    versao: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    data_criacao: Mapped[datetime] = mapped_column(
        InstanteUTC(), nullable=False, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(InstanteUTC())

    empresa = relationship("Empresa")
    plano = relationship("Plano")
    responsavel = relationship("Usuario", foreign_keys=[id_usuario_admin_responsavel])
    coberturas = relationship(
        "CoberturaPaga", viewonly=True,
        primaryjoin="and_(Contrato.id_contrato == foreign(CoberturaPaga.id_contrato), "
                    "Contrato.id_empresa == foreign(CoberturaPaga.id_empresa))",
        order_by="CoberturaPaga.periodo_inicio",
    )

    __table_args__ = (
        UniqueConstraint("id_contrato", "id_empresa", name="uq_contrato_id_empresa"),
        UniqueConstraint("id_contrato_anterior", name="uq_contrato_anterior"),
        ForeignKeyConstraint(
            ["id_contrato_anterior", "id_empresa"],
            ["contrato.id_contrato", "contrato.id_empresa"],
            name="fk_contrato_anterior_mesma_empresa",
        ),
        CheckConstraint(
            "situacao IN ('ATIVO', 'SUSPENSO', 'ENCERRADO', 'CANCELADO')",
            name="ck_contrato_situacao",
        ),
        CheckConstraint("valor_mensal_contratado >= 0", name="ck_contrato_valor"),
        CheckConstraint(
            "data_fim IS NULL OR data_fim >= data_inicio", name="ck_contrato_vigencia",
        ),
        CheckConstraint(
            "meses_suspensao IS NULL OR meses_suspensao BETWEEN 1 AND 12",
            name="ck_contrato_meses_suspensao",
        ),
        CheckConstraint("versao >= 1", name="ck_contrato_versao"),
        CheckConstraint(
            "id_contrato_anterior IS NULL OR id_contrato_anterior <> id_contrato",
            name="ck_contrato_anterior_distinto",
        ),
        CheckConstraint(
            "situacao <> 'SUSPENSO' OR ("
            "suspenso_em IS NOT NULL AND suspensao_limite_original IS NOT NULL "
            "AND suspensao_limite IS NOT NULL AND pdf_limite_original IS NOT NULL "
            "AND pdf_limite IS NOT NULL "
            "AND suspensao_limite_original > suspenso_em "
            "AND suspensao_limite >= suspensao_limite_original "
            "AND pdf_limite_original > suspenso_em "
            "AND pdf_limite >= pdf_limite_original "
            "AND pdf_limite <= suspensao_limite)",
            name="ck_contrato_suspensao",
        ),
        CheckConstraint(
            "situacao <> 'ATIVO' OR (suspenso_em IS NULL AND suspensao_limite IS NULL "
            "AND suspensao_limite_original IS NULL AND pdf_limite IS NULL "
            "AND pdf_limite_original IS NULL AND encerrado_em IS NULL "
            "AND cancelado_em IS NULL)",
            name="ck_contrato_ativo_sem_prazos",
        ),
        CheckConstraint(
            "situacao <> 'ENCERRADO' OR encerrado_em IS NOT NULL",
            name="ck_contrato_encerrado",
        ),
        CheckConstraint(
            "situacao <> 'CANCELADO' OR (cancelado_em IS NOT NULL "
            "AND cancelamento_motivo IS NOT NULL)",
            name="ck_contrato_cancelado",
        ),
        Index("ix_contrato_empresa", "id_empresa"),
        # No máximo UM contrato vigente (ATIVO/SUSPENSO) por empresa.
        Index(
            "uq_contrato_aplicavel_empresa", "id_empresa", unique=True,
            postgresql_where=text("situacao IN ('ATIVO', 'SUSPENSO')"),
            sqlite_where=text("situacao IN ('ATIVO', 'SUSPENSO')"),
        ),
    )


# =====================================================================
# 17. CoberturaPaga  (períodos efetivamente pagos)
# =====================================================================
class CoberturaPaga(db.Model):
    """
    Intervalo de datas (inclusivo) efetivamente pago, confirmado por um ADMIN
    com referência administrativa (ex.: comprovante). É o que gera direito às
    análises dos períodos correspondentes. Registro imutável: correções são
    feitas revogando (com motivo) e registrando outra cobertura.
    """
    __tablename__ = "cobertura_paga"

    id_cobertura: Mapped[int] = mapped_column(primary_key=True)
    id_contrato: Mapped[int] = mapped_column(Integer, nullable=False)
    id_empresa: Mapped[int] = mapped_column(ForeignKey("empresa.id_empresa"), nullable=False)
    periodo_inicio: Mapped[date] = mapped_column(Date, nullable=False)
    periodo_fim: Mapped[date] = mapped_column(Date, nullable=False)
    referencia_confirmacao: Mapped[str] = mapped_column(String(200), nullable=False)
    confirmado_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False)
    id_usuario_admin_confirmacao: Mapped[int] = mapped_column(
        ForeignKey("usuario.id_usuario"), nullable=False
    )
    revogada_em: Mapped[Optional[datetime]] = mapped_column(InstanteUTC())
    revogacao_motivo: Mapped[Optional[str]] = mapped_column(Text)
    id_usuario_admin_revogacao: Mapped[Optional[int]] = mapped_column(
        ForeignKey("usuario.id_usuario")
    )
    data_criacao: Mapped[datetime] = mapped_column(
        InstanteUTC(), nullable=False, server_default=func.current_timestamp()
    )

    confirmado_por = relationship("Usuario", foreign_keys=[id_usuario_admin_confirmacao])
    revogado_por = relationship("Usuario", foreign_keys=[id_usuario_admin_revogacao])

    __table_args__ = (
        UniqueConstraint("id_cobertura", "id_empresa", name="uq_cobertura_id_empresa"),
        # A cobertura só pode pertencer a um contrato da MESMA empresa.
        ForeignKeyConstraint(
            ["id_contrato", "id_empresa"],
            ["contrato.id_contrato", "contrato.id_empresa"],
            name="fk_cobertura_contrato_mesma_empresa",
        ),
        CheckConstraint("periodo_fim >= periodo_inicio", name="ck_cobertura_periodo"),
        CheckConstraint(
            "length(trim(referencia_confirmacao)) > 0", name="ck_cobertura_referencia",
        ),
        CheckConstraint(
            "(revogada_em IS NULL AND revogacao_motivo IS NULL "
            "AND id_usuario_admin_revogacao IS NULL) OR "
            "(revogada_em IS NOT NULL AND revogacao_motivo IS NOT NULL "
            "AND id_usuario_admin_revogacao IS NOT NULL)",
            name="ck_cobertura_revogacao",
        ),
        Index("ix_cobertura_empresa", "id_empresa", "periodo_inicio"),
        Index("ix_cobertura_contrato", "id_contrato"),
    )


# =====================================================================
# 18. EventoContratual  (histórico auditável, somente inclusão)
# =====================================================================
TIPOS_EVENTO_CONTRATUAL = (
    "CONTRATACAO", "RENOVACAO", "ALTERACAO_PLANO", "SUSPENSAO", "PRORROGACAO",
    "REATIVACAO", "CANCELAMENTO", "COBERTURA_CONFIRMADA", "COBERTURA_REVOGADA",
    "REGRAS_ATIVADAS", "REGRAS_DESATIVADAS",
)


class EventoContratual(db.Model):
    """
    Trilha de auditoria do ciclo contratual. Cada operação administrativa ou
    da reconciliação grava um evento na MESMA transação da mudança; a aplicação
    nunca altera nem apaga eventos. `detalhes` guarda JSON com os valores
    anteriores e novos (datas-limite, plano, valor, período pago).
    """
    __tablename__ = "evento_contratual"

    id_evento: Mapped[int] = mapped_column(primary_key=True)
    id_empresa: Mapped[int] = mapped_column(ForeignKey("empresa.id_empresa"), nullable=False)
    id_contrato: Mapped[Optional[int]] = mapped_column(Integer)
    id_cobertura: Mapped[Optional[int]] = mapped_column(Integer)
    tipo: Mapped[str] = mapped_column(String(40), nullable=False)
    ocorrido_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False)
    origem: Mapped[str] = mapped_column(String(20), nullable=False)
    id_usuario_responsavel: Mapped[Optional[int]] = mapped_column(ForeignKey("usuario.id_usuario"))
    situacao_anterior: Mapped[Optional[str]] = mapped_column(String(20))
    situacao_posterior: Mapped[Optional[str]] = mapped_column(String(20))
    justificativa: Mapped[Optional[str]] = mapped_column(Text)
    detalhes: Mapped[Optional[str]] = mapped_column(Text)

    responsavel = relationship("Usuario", foreign_keys=[id_usuario_responsavel])

    __table_args__ = (
        ForeignKeyConstraint(
            ["id_contrato", "id_empresa"],
            ["contrato.id_contrato", "contrato.id_empresa"],
            name="fk_evento_contrato_mesma_empresa",
        ),
        ForeignKeyConstraint(
            ["id_cobertura", "id_empresa"],
            ["cobertura_paga.id_cobertura", "cobertura_paga.id_empresa"],
            name="fk_evento_cobertura_mesma_empresa",
        ),
        CheckConstraint(
            "tipo IN ('CONTRATACAO', 'RENOVACAO', 'ALTERACAO_PLANO', 'SUSPENSAO', "
            "'PRORROGACAO', 'REATIVACAO', 'CANCELAMENTO', 'COBERTURA_CONFIRMADA', "
            "'COBERTURA_REVOGADA', 'REGRAS_ATIVADAS', 'REGRAS_DESATIVADAS')",
            name="ck_evento_tipo",
        ),
        CheckConstraint(
            "(origem = 'ADMIN' AND id_usuario_responsavel IS NOT NULL) OR "
            "(origem = 'RECONCILIACAO' AND id_usuario_responsavel IS NULL)",
            name="ck_evento_origem",
        ),
        Index("ix_evento_empresa", "id_empresa", "ocorrido_em"),
        Index("ix_evento_contrato", "id_contrato"),
    )


# =====================================================================
# 19. HistoricoPrecoPlano  (auditoria do catálogo público de preços)
# =====================================================================
class HistoricoPrecoPlano(db.Model):
    """Cada alteração de Plano.valor_mensal feita pela atualização do catálogo.
    Contratos não são afetados: cada um guarda o próprio valor contratado."""
    __tablename__ = "historico_preco_plano"

    id_historico: Mapped[int] = mapped_column(primary_key=True)
    id_plano: Mapped[int] = mapped_column(ForeignKey("plano.id_plano"), nullable=False)
    valor_anterior: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    valor_novo: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    alterado_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False)
    origem: Mapped[str] = mapped_column(String(80), nullable=False)

    plano = relationship("Plano")

    __table_args__ = (
        CheckConstraint("valor_novo >= 0", name="ck_historico_preco_valor"),
        Index("ix_historico_preco_plano", "id_plano", "alterado_em"),
    )


# =====================================================================
# 20. PreferenciaUsuario  (avisos que o próprio usuário escolhe receber)
# =====================================================================
class PreferenciaUsuario(db.Model):
    """
    Escolhas de aviso de um usuário. Tabela própria, e não coluna em Usuario,
    pelo mesmo motivo de RegimeContratual: o código chega à produção antes da
    migração sem quebrar nenhuma consulta de Usuario. Sem a linha valem os
    padrões (tudo ligado). Avisos de segurança não têm escolha: saem sempre.
    """
    __tablename__ = "preferencia_usuario"

    id_usuario: Mapped[int] = mapped_column(
        ForeignKey("usuario.id_usuario", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    lembrete_renovacao: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    alerta_chamado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    data_atualizacao: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False)


# =====================================================================
# 21. DocumentoLegal  (Termos de Uso e Política de Privacidade, por versão)
# =====================================================================
TIPOS_DOCUMENTO_LEGAL = ("TERMOS_USO", "POLITICA_PRIVACIDADE")


class DocumentoLegal(db.Model):
    """
    Uma versão publicada de um documento. O texto é escrito pelo ADMIN e
    exibido como texto puro, nunca como HTML. Registro imutável: salvar cria a
    versão seguinte e a de maior número é a vigente, para que todo aceite
    continue apontando para o texto que a pessoa de fato leu.
    """
    __tablename__ = "documento_legal"

    id_documento: Mapped[int] = mapped_column(primary_key=True)
    tipo: Mapped[str] = mapped_column(String(30), nullable=False)
    versao: Mapped[int] = mapped_column(Integer, nullable=False)
    # Rótulo exibido (v1.0, v1.1, v2.0): o admin escolhe, ao publicar, entre revisão e nova
    # versão. Ordem e vigência seguem a sequência `versao`; qualquer versão nova pede aceite.
    versao_maior: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    versao_menor: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    conteudo: Mapped[str] = mapped_column(Text, nullable=False)
    publicado_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False)
    id_usuario_admin: Mapped[int] = mapped_column(ForeignKey("usuario.id_usuario"), nullable=False)

    publicado_por = relationship("Usuario")

    __table_args__ = (
        CheckConstraint("tipo IN ('TERMOS_USO', 'POLITICA_PRIVACIDADE')", name="ck_documento_tipo"),
        CheckConstraint("versao >= 1", name="ck_documento_versao"),
        CheckConstraint("length(trim(conteudo)) > 0", name="ck_documento_conteudo"),
        CheckConstraint("versao_maior >= 1", name="ck_documento_versao_maior"),
        CheckConstraint("versao_menor >= 0", name="ck_documento_versao_menor"),
        UniqueConstraint("tipo", "versao", name="uq_documento_tipo_versao"),
        Index("uq_documento_tipo_rotulo", "tipo", "versao_maior", "versao_menor", unique=True),
    )

    @property
    def rotulo_versao(self) -> str:
        return f"v{self.versao_maior}.{self.versao_menor}"


# =====================================================================
# 22. AceiteDocumento  (quem aceitou qual versão, e quando)
# =====================================================================
class AceiteDocumento(db.Model):
    """Aceite explícito de uma versão de DocumentoLegal, feito pelo próprio usuário.
    Nunca é presumido: sem a linha, a tela diz que não há aceite registrado."""
    __tablename__ = "aceite_documento"

    id_aceite: Mapped[int] = mapped_column(primary_key=True)
    id_usuario: Mapped[int] = mapped_column(
        ForeignKey("usuario.id_usuario", ondelete="CASCADE"), nullable=False,
    )
    id_documento: Mapped[int] = mapped_column(ForeignKey("documento_legal.id_documento"), nullable=False)
    # Instante em UTC, exibido no horário de Brasília. O IP vem da requisição do aceite, já
    # corrigido pelo ProxyFix; os aceites anteriores à revisão a8d3f6b91c24 ficam sem IP.
    aceito_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False)
    ip_usuario: Mapped[Optional[str]] = mapped_column(String(45))

    documento = relationship("DocumentoLegal")

    __table_args__ = (
        UniqueConstraint("id_usuario", "id_documento", name="uq_aceite_usuario_documento"),
        Index("ix_aceite_documento", "id_documento"),
    )


# =====================================================================
# 23. ConfiguracaoSistema  (ajustes feitos pelo ADMIN em Configurações)
# =====================================================================
CHAVES_CONFIGURACAO_SISTEMA = ("suporte_bcc_email",)


class ConfiguracaoSistema(db.Model):
    """Ajuste do sistema por chave. Valor NULL = desligado; a linha guarda quem mexeu por último."""
    __tablename__ = "configuracao_sistema"

    chave: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[Optional[str]] = mapped_column(Text)
    atualizado_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False)
    id_usuario_admin: Mapped[int] = mapped_column(ForeignKey("usuario.id_usuario"), nullable=False)

    __table_args__ = (
        CheckConstraint("chave IN ('suporte_bcc_email')", name="ck_configuracao_chave"),
    )


# =====================================================================
# 24. ValidacaoPdv  (trava do total do PDV)
# =====================================================================
class ValidacaoPdv(db.Model):
    """
    Conferência do faturamento calculado pelo ETL com o total que o PDV mostra. Uma
    linha por análise: nasce no envio do relatório de VENDAS (PENDENTE) e recebe o
    resultado no processamento (CONCLUIDO ou DIVERGENTE_PDV).

    O CONCLUIDO daqui só diz que os totais batem. Quem decide a publicação continua
    sendo o ADMIN, em Analise.status_analise, e DIVERGENTE_PDV ou PENDENTE a impedem.

    Tabela própria, e não coluna ou status em Analise, pelo mesmo motivo de
    RegimeContratual: o código chega à produção antes da migração sem quebrar nenhuma
    consulta. Sem relationship em Analise: a linha sai com a análise por ON DELETE
    CASCADE, e excluir uma análise não consulta esta tabela. Ver validacao_pdv.py.
    """
    __tablename__ = "validacao_pdv"

    id_analise: Mapped[int] = mapped_column(
        ForeignKey("analise.id_analise", ondelete="CASCADE", name="fk_validacao_pdv_analise"),
        primary_key=True, autoincrement=False,
    )
    valor_pdv_informado: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Sem precisão, como os KPIs em IndicadorAnalise: o arquivo não tem teto, e justamente o
    # valor absurdo precisa virar DIVERGENTE_PDV em vez de estourar a coluna no processamento.
    faturamento_bruto_calculado: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    diferenca: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    # Parcelas do relatório que explicam o total ao consultor; nulas quando não houver coluna de situação.
    valor_devolucoes: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    valor_cancelado: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    status_validacao: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDENTE")
    id_usuario_informante: Mapped[Optional[int]] = mapped_column(
        ForeignKey("usuario.id_usuario", ondelete="SET NULL", name="fk_validacao_pdv_informante")
    )
    informado_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False)
    validado_em: Mapped[Optional[datetime]] = mapped_column(InstanteUTC())

    __table_args__ = (
        CheckConstraint("valor_pdv_informado >= 0", name="ck_validacao_pdv_valor"),
        CheckConstraint("status_validacao IN ('PENDENTE', 'CONCLUIDO', 'DIVERGENTE_PDV')",
                        name="ck_validacao_pdv_status"),
        CheckConstraint(
            "(status_validacao = 'PENDENTE' AND faturamento_bruto_calculado IS NULL "
            "AND diferenca IS NULL AND validado_em IS NULL) OR "
            "(status_validacao <> 'PENDENTE' AND faturamento_bruto_calculado IS NOT NULL "
            "AND diferenca IS NOT NULL AND diferenca >= 0 AND validado_em IS NOT NULL)",
            name="ck_validacao_pdv_resultado",
        ),
    )


# =====================================================================
# Planos por nível de inteligência (entitlements) e Raio-X de entrada
# =====================================================================
class PlanoNivel(db.Model):
    """
    Nível de acesso de cada plano: posição na hierarquia (BRONZE < PRATA < OURO <
    CUSTOM) e quantos meses de histórico o plano mantém (NULL = sem limite).

    Liga-se ao plano comercial pelo código (plano.nome_plano = plano_nivel.codigo),
    sem coluna, FK ou CHECK novos em `plano`: a tabela existente continua com preço,
    frequência e contratos, e o código chega à produção antes da migração sem quebrar
    nenhuma consulta. CUSTOM existe aqui como nível; nenhuma empresa fica nele enquanto
    o CHECK de plano.nome_plano não o aceitar. Ver entitlements.py.
    """
    __tablename__ = "plano_nivel"

    codigo: Mapped[str] = mapped_column(String(20), primary_key=True)
    nome: Mapped[str] = mapped_column(String(60), nullable=False)
    modelo_atendimento: Mapped[str] = mapped_column(String(60), nullable=False)
    atendimento: Mapped[str] = mapped_column(String(255), nullable=False)
    nivel_hierarquico: Mapped[int] = mapped_column(Integer, nullable=False)
    limite_historico_meses: Mapped[Optional[int]] = mapped_column(Integer)
    criado_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False)

    __table_args__ = (
        UniqueConstraint("nivel_hierarquico", name="uq_plano_nivel_hierarquia"),
        CheckConstraint("length(codigo) BETWEEN 2 AND 20", name="ck_plano_nivel_codigo"),
        CheckConstraint("nivel_hierarquico >= 1", name="ck_plano_nivel_hierarquia"),
        CheckConstraint("limite_historico_meses IS NULL OR limite_historico_meses >= 1",
                        name="ck_plano_nivel_historico"),
    )


class Funcionalidade(db.Model):
    """Recurso que um plano pode liberar, identificado pela `chave` usada no código."""
    __tablename__ = "funcionalidade"

    id_funcionalidade: Mapped[int] = mapped_column(primary_key=True)
    chave: Mapped[str] = mapped_column(String(60), nullable=False)
    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    descricao: Mapped[str] = mapped_column(String(500), nullable=False)

    __table_args__ = (
        UniqueConstraint("chave", name="uq_funcionalidade_chave"),
        CheckConstraint("length(chave) BETWEEN 3 AND 60", name="ck_funcionalidade_chave"),
    )


class PlanoFuncionalidade(db.Model):
    """Quais funcionalidades cada nível libera. A migração grava a matriz cumulativa:
    cada nível tem as do nível abaixo."""
    __tablename__ = "plano_funcionalidade"

    codigo_plano: Mapped[str] = mapped_column(
        ForeignKey("plano_nivel.codigo", name="fk_plano_funcionalidade_plano"),
        primary_key=True, autoincrement=False,
    )
    id_funcionalidade: Mapped[int] = mapped_column(
        ForeignKey("funcionalidade.id_funcionalidade", name="fk_plano_funcionalidade_funcionalidade"),
        primary_key=True, autoincrement=False,
    )

    __table_args__ = (
        Index("ix_plano_funcionalidade_funcionalidade", "id_funcionalidade"),
    )


class DiagnosticoEntrada(db.Model):
    """
    Último Raio-X de entrada de cada empresa (um por empresa: o novo substitui o anterior).

    Guarda só agregados e as listas resumidas do resultado (JSON em `resultado`); os
    arquivos enviados são lidos no processo isolado do ETL e descartados. A linha sai com
    a empresa por ON DELETE CASCADE, como a validacao_pdv sai com a análise: a exclusão
    permanente da empresa não precisa consultar esta tabela. Ver raio_x.py.
    """
    __tablename__ = "diagnostico_entrada"

    id_empresa: Mapped[int] = mapped_column(
        ForeignKey("empresa.id_empresa", ondelete="CASCADE", name="fk_diagnostico_entrada_empresa"),
        primary_key=True, autoincrement=False,
    )
    id_usuario: Mapped[Optional[int]] = mapped_column(
        ForeignKey("usuario.id_usuario", ondelete="SET NULL", name="fk_diagnostico_entrada_usuario")
    )
    gerado_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False)
    versao_regras: Mapped[str] = mapped_column(String(40), nullable=False)
    arquivos: Mapped[int] = mapped_column(Integer, nullable=False)
    periodo_inicio: Mapped[Optional[date]] = mapped_column(Date)
    periodo_fim: Mapped[Optional[date]] = mapped_column(Date)
    indice_saude: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    # Sem precisão, como os KPIs: o arquivo não tem teto e o valor não pode estourar a coluna.
    valor_recuperavel: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    plano_recomendado: Mapped[str] = mapped_column(
        ForeignKey("plano_nivel.codigo", name="fk_diagnostico_entrada_plano"), nullable=False,
    )
    resultado: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("arquivos >= 1", name="ck_diagnostico_entrada_arquivos"),
        CheckConstraint("indice_saude IS NULL OR (indice_saude >= 0 AND indice_saude <= 100)",
                        name="ck_diagnostico_entrada_indice"),
        CheckConstraint("valor_recuperavel >= 0", name="ck_diagnostico_entrada_recuperavel"),
        CheckConstraint("periodo_inicio IS NULL OR periodo_fim IS NULL OR periodo_inicio <= periodo_fim",
                        name="ck_diagnostico_entrada_periodo"),
        Index("ix_diagnostico_entrada_usuario", "id_usuario"),
    )


# =====================================================================
# Avaliação de Cenário: captação de leads (revisão f4c2a8e1d9b6)
# =====================================================================
STATUS_LEAD_AVALIACAO = ("pendente", "em_processamento", "concluido", "processamento_manual",
                         "convertido", "expirado")


class LeadAvaliacao(db.Model):
    """
    Quem pediu a Avaliação de Cenário (ainda não é cliente) e o resumo do resultado.

    O contato (nome, e-mail, WhatsApp e loja) é dado pessoal: a tabela tem RLS no
    PostgreSQL, e os dados do PDV do lead (BrainstormingDadosPdv) saem em 30 dias se ele não
    converter (leads_avaliacao.expurgar_dados_pdv). O resumo fica: score de saúde cadastral
    (0 a 100), vazamento total em R$, plano recomendado e os três blocos de prioridade
    (urgência, atenção e eletiva) em JSON. Score e vazamento ficam nulos até a avaliação
    terminar. O nome da coluna do vazamento segue a especificação (com cifrão); o atributo
    Python é vazamento_total_reais.
    """
    __tablename__ = "lead_avaliacao"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    nome_contato: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    whatsapp: Mapped[Optional[str]] = mapped_column(String(20))
    nome_loja: Mapped[str] = mapped_column(String(120), nullable=False)
    score_saude_cadastral: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    vazamento_total_reais: Mapped[Optional[Decimal]] = mapped_column("vazamento_total_r$", Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pendente",
                                        server_default=text("'pendente'"))
    plano_recomendado_codigo: Mapped[Optional[str]] = mapped_column(
        ForeignKey("plano_nivel.codigo", name="fk_lead_avaliacao_plano")
    )
    resumo_urgencia_json: Mapped[Optional[str]] = mapped_column(Text)
    resumo_atencao_json: Mapped[Optional[str]] = mapped_column(Text)
    resumo_eletiva_json: Mapped[Optional[str]] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False, default=agora_utc,
                                                server_default=func.current_timestamp())
    atualizado_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False, default=agora_utc,
                                                    onupdate=agora_utc, server_default=func.current_timestamp())
    # Revisão 5e1b9c3a7d20: de onde o resumo saiu (versões, período, relatórios e contagens do envio,
    # sem nome de produto), as marcas agregadas, quem registrou o lead e quando confirmou a autorização.
    versao_regras: Mapped[Optional[str]] = mapped_column(String(40))
    versao_triagem: Mapped[Optional[str]] = mapped_column(String(40))
    periodo_inicio: Mapped[Optional[date]] = mapped_column(Date)
    periodo_fim: Mapped[Optional[date]] = mapped_column(Date)
    arquivos: Mapped[Optional[int]] = mapped_column(Integer)
    meta_json: Mapped[Optional[str]] = mapped_column(Text)
    itens_venda_sem_custo: Mapped[Optional[int]] = mapped_column(Integer)
    itens_venda_abaixo_custo: Mapped[Optional[int]] = mapped_column(Integer)
    registrado_por: Mapped[Optional[int]] = mapped_column(
        ForeignKey("usuario.id_usuario", ondelete="SET NULL", name="fk_lead_avaliacao_registrado_por")
    )
    consentimento_em: Mapped[Optional[datetime]] = mapped_column(InstanteUTC())

    dados_pdv = relationship("BrainstormingDadosPdv", back_populates="lead",
                             cascade="all, delete-orphan", passive_deletes=True)

    __table_args__ = (
        CheckConstraint("status IN (" + ", ".join(f"'{s}'" for s in STATUS_LEAD_AVALIACAO) + ")",
                        name="ck_lead_avaliacao_status"),
        CheckConstraint("arquivos IS NULL OR arquivos >= 1", name="ck_lead_avaliacao_arquivos"),
        CheckConstraint("(itens_venda_sem_custo IS NULL OR itens_venda_sem_custo >= 0) "
                        "AND (itens_venda_abaixo_custo IS NULL OR itens_venda_abaixo_custo >= 0)",
                        name="ck_lead_avaliacao_itens"),
        CheckConstraint("periodo_inicio IS NULL OR periodo_fim IS NULL OR periodo_inicio <= periodo_fim",
                        name="ck_lead_avaliacao_periodo"),
        Index("ix_lead_avaliacao_registrado_por", "registrado_por"),
        CheckConstraint("score_saude_cadastral IS NULL OR (score_saude_cadastral >= 0 "
                        "AND score_saude_cadastral <= 100)", name="ck_lead_avaliacao_score"),
        CheckConstraint('"vazamento_total_r$" IS NULL OR "vazamento_total_r$" >= 0',
                        name="ck_lead_avaliacao_vazamento"),
        CheckConstraint("length(trim(nome_contato)) > 0 AND length(trim(nome_loja)) > 0",
                        name="ck_lead_avaliacao_nomes"),
        CheckConstraint("email LIKE '%_@_%'", name="ck_lead_avaliacao_email"),
        CheckConstraint("whatsapp IS NULL OR length(whatsapp) BETWEEN 10 AND 20", name="ck_lead_avaliacao_whatsapp"),
        Index("ix_lead_avaliacao_plano", "plano_recomendado_codigo"),
        Index("ix_lead_avaliacao_status", "status"),
        Index("ix_lead_avaliacao_email", "email"),
    )


class BrainstormingDadosPdv(db.Model):
    """
    Uma linha por produto e ano de exercício do PDV enviado pelo lead: quantidade, venda
    bruta, custo médio registrado, as marcas de venda sem custo, abaixo do custo e omissão
    no ERP, e o vazamento estimado da linha (coluna `vazamento_estimado_r$`, atributo
    vazamento_estimado_reais). Sai com o lead (ON DELETE CASCADE) e, se o lead não
    converter, em 30 dias pelo expurgo, que mantém o resumo em LeadAvaliacao.
    """
    __tablename__ = "brainstorming_dados_pdv"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lead_avaliacao.id", ondelete="CASCADE", name="fk_brainstorming_dados_pdv_lead"),
        nullable=False,
    )
    ano_exercicio: Mapped[int] = mapped_column(Integer, nullable=False)
    codigo_produto: Mapped[Optional[str]] = mapped_column(String(60))
    descricao_produto: Mapped[str] = mapped_column(String(200), nullable=False)
    quantidade: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    valor_venda_bruto: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    custo_medio_registrado: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    flag_venda_sem_custo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                       server_default=false())
    flag_venda_abaixo_custo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                          server_default=false())
    flag_omissao_erp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    vazamento_estimado_reais: Mapped[Decimal] = mapped_column("vazamento_estimado_r$", Numeric(12, 2),
                                                              nullable=False, default=Decimal("0"),
                                                              server_default=text("0"))
    criado_em: Mapped[datetime] = mapped_column(InstanteUTC(), nullable=False, default=agora_utc,
                                                server_default=func.current_timestamp())
    # Revisão 5e1b9c3a7d20: a staging ampliada. Os arquivos do lead não são guardados; com estas
    # somas do produto no ano (as mesmas do Raio-X), a triagem e as marcas são refeitas sem eles
    # (avaliacao_cenario.retriar). chave_produto identifica o produto entre os anos
    # (avaliacao_motor.chave_produto).
    chave_produto: Mapped[str] = mapped_column(String(16), nullable=False)
    quantidade_com_custo: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=Decimal("0"),
                                                          server_default=text("0"))
    faturamento_com_custo: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"),
                                                           server_default=text("0"))
    custo_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"),
                                                 server_default=text("0"))
    perda: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"),
                                           server_default=text("0"))
    vazamento_preco: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"),
                                                     server_default=text("0"))
    recuperavel: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"),
                                                 server_default=text("0"))
    linhas: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    meses: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    variacao: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    conflito_cadastral: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    estoque_negativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    bloco_triagem: Mapped[Optional[str]] = mapped_column(String(10))
    versao_regras: Mapped[Optional[str]] = mapped_column(String(40))

    lead = relationship("LeadAvaliacao", back_populates="dados_pdv")

    __table_args__ = (
        CheckConstraint("ano_exercicio BETWEEN 2000 AND 2100", name="ck_brainstorming_dados_pdv_ano"),
        CheckConstraint("length(chave_produto) > 0", name="ck_brainstorming_dados_pdv_chave"),
        CheckConstraint("quantidade_com_custo >= 0 AND faturamento_com_custo >= 0 AND custo_total >= 0 "
                        "AND perda >= 0 AND vazamento_preco >= 0 AND recuperavel >= 0 AND linhas >= 0 "
                        "AND meses >= 0", name="ck_brainstorming_dados_pdv_somas"),
        CheckConstraint("bloco_triagem IS NULL OR bloco_triagem IN ('urgencia', 'atencao', 'eletiva')",
                        name="ck_brainstorming_dados_pdv_bloco"),
        UniqueConstraint("lead_id", "ano_exercicio", "chave_produto",
                         name="uq_brainstorming_dados_pdv_lead_ano_produto"),
        CheckConstraint("length(trim(descricao_produto)) > 0", name="ck_brainstorming_dados_pdv_descricao"),
        CheckConstraint("custo_medio_registrado IS NULL OR custo_medio_registrado >= 0",
                        name="ck_brainstorming_dados_pdv_custo"),
        CheckConstraint('"vazamento_estimado_r$" >= 0', name="ck_brainstorming_dados_pdv_vazamento"),
        Index("ix_brainstorming_dados_pdv_lead", "lead_id"),
        Index("ix_brainstorming_dados_pdv_criado_em", "criado_em"),
    )
