"""Clique duplo em PostgreSQL real: chamado, respostas e análise repetidos se serializam pela trava da empresa.

Por padrão o ensaio é ignorado. Para habilitar, indique apenas o diretório dos
binários PostgreSQL em NEXO_TEST_POSTGRES_BIN e execute:
    python -B -m unittest tests.test_chamados_postgresql -v

Mesmo protocolo de tests/test_contratos_postgresql.py, de onde vem o cluster temporário
(um processo separado, com ambiente filtrado e dotenv desabilitado, e nenhum banco ou
servidor existente), e das corridas de tests/test_trava_pdv_postgresql.py. Os dados são
fictícios.

Complementa tests/test_onda3_portal_admin.py, que cobre as regras em SQLite; aqui só o
que depende do lock real: em cada corrida, o primeiro pedido confere, não acha nada e
para logo depois, com a empresa bloqueada e nada gravado; o segundo precisa esperar no
SELECT ... FROM empresa ... FOR UPDATE e, liberado, confere com o que o primeiro gravou.
Sem a trava, os dois gravariam.
  1. duas aberturas do mesmo chamado: um chamado, uma mensagem e um aviso a cada admin;
  2. duas respostas iguais do cliente: uma mensagem;
  3. duas respostas iguais da equipe: uma mensagem e um aviso ao cliente;
  4. duas aberturas da mesma análise (empresa, competência e tipo): uma análise.
"""

from decimal import Decimal
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_contratos_postgresql import MIGRATIONS, PASSWORD, ROOT, TOOLS, _PrivateCluster, _safe_environment, _tool

CHECKS = (
    "private_engine", "migration_reaches_head",
    "ticket_opening_waits_and_is_not_duplicated", "client_reply_waits_and_is_not_duplicated",
    "team_reply_waits_and_is_not_duplicated", "analysis_waits_and_is_not_duplicated",
)


class PostgreSQLChamadosTests(unittest.TestCase):
    def test_clique_duplo_em_postgresql_real(self):
        raw_bin = os.environ.get("NEXO_TEST_POSTGRES_BIN")
        if not raw_bin:
            self.skipTest("Defina NEXO_TEST_POSTGRES_BIN para criar um cluster PostgreSQL temporário isolado.")
        self.assertTrue("://" not in raw_bin and Path(raw_bin).is_absolute(),
                        "NEXO_TEST_POSTGRES_BIN aceita apenas um caminho local absoluto; URLs são recusadas.")
        try:
            bin_dir = Path(raw_bin).resolve(strict=True)
        except (OSError, ValueError):
            self.fail("NEXO_TEST_POSTGRES_BIN não indica um diretório local acessível; valor omitido.")
        self.assertTrue(all(_tool(bin_dir, tool).is_file() for tool in TOOLS),
                        "O diretório precisa conter initdb, pg_ctl e createdb.")
        with tempfile.TemporaryDirectory(prefix="nexo-chamados-pg-") as work:
            work_dir = Path(work).resolve()
            data_dir = work_dir / "private-cluster"
            try:
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "tests.test_chamados_postgresql", "--isolated-worker",
                     str(bin_dir), str(work_dir)],
                    cwd=str(ROOT), env=_safe_environment(), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=240, check=False,
                )
            except subprocess.TimeoutExpired:
                self.fail("O ensaio isolado excedeu 240 segundos; nenhuma saída ou credencial foi exibida.")
            finally:
                self.assertTrue(data_dir.resolve().is_relative_to(work_dir))
                if (data_dir / "postmaster.pid").is_file():
                    subprocess.run(
                        [str(_tool(bin_dir, "pg_ctl")), "--pgdata", str(data_dir), "--mode", "fast",
                         "--wait", "--timeout", "20", "stop"],
                        env=_safe_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=30, check=False,
                    )
        try:
            report = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            self.fail("O processo isolado não produziu um relatório seguro válido; saídas brutas omitidas.")
        self.assertEqual(result.returncode, 0,
                         f"Ensaio isolado falhou na etapa {report.get('stage', 'desconhecida')} "
                         f"({report.get('error', 'erro não identificado')}); saídas brutas omitidas.")
        self.assertEqual(report.get("status"), "ok")
        self.assertEqual(sorted(report.get("checks", [])), sorted(CHECKS))
        self.assertFalse(report.get("production_access"))
        print("\nEnsaio PostgreSQL clique duplo: " + json.dumps(report, ensure_ascii=True, sort_keys=True))


class _PortaDepoisDaConferencia:
    """Deixa a primeira chamada conferir e a segura logo depois, com a empresa bloqueada e nada gravado:
    o pior caso do clique duplo, em que os dois pedidos já conferiram antes de qualquer um gravar. Sem a
    trava, o segundo conferiria também sem achar nada e haveria dois registros."""

    def __init__(self, real):
        self.real = real
        self.chegou, self.liberada = threading.Event(), threading.Event()

    def __call__(self, *args, **kwargs):
        resultado = self.real(*args, **kwargs)
        if not self.chegou.is_set():
            self.chegou.set()
            if not self.liberada.wait(20):
                raise RuntimeError("Porta do ensaio não liberada")
        return resultado


class _ClusterDosChamados(_PrivateCluster):
    """Auxiliar do processo isolado; não é um caso descoberto pelo unittest."""

    def _etapas(self):
        self._etapa_preparo()
        self._etapa_corridas()

    # ------------------------------------------------------------------ apoio
    def _esperar_lock_da_empresa(self, limite=6.0):
        """True quando outra conexão espera o lock de linha da EMPRESA (company_security.bloquear_empresa)."""
        from sqlalchemy import text
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                conexao = conexao.execution_options(isolation_level="AUTOCOMMIT")
                fim = time.monotonic() + limite
                while time.monotonic() < fim:
                    for consulta in conexao.execute(text(
                            "SELECT query FROM pg_stat_activity WHERE datname = current_database() "
                            "AND cardinality(pg_blocking_pids(pid)) > 0")).scalars():
                        consulta = " ".join((consulta or "").split()).upper()
                        if "FROM EMPRESA" in consulta and consulta.endswith("FOR UPDATE"):
                            return True
                    time.sleep(0.01)  # espera curta pelo lock entre conexões
        return False

    @staticmethod
    def _em_paralelo(funcao):
        saida = {}

        def corpo():
            try:
                saida["resposta"] = funcao()
            except BaseException as erro:  # devolvido ao fluxo principal para as verificações
                saida["erro"] = erro

        fio = threading.Thread(target=corpo, daemon=True)
        fio.start()
        return fio, saida

    def _corrida(self, alvo, atributo, primeiro, segundo):
        """`primeiro` confere e para na porta com o lock da empresa, e `segundo` precisa esperar por ele.
        Devolve se a espera aconteceu e a resposta de cada um (ou o nome do erro)."""
        porta = _PortaDepoisDaConferencia(getattr(alvo, atributo))
        with patch.object(alvo, atributo, porta):
            fio_primeiro, saida_primeiro = self._em_paralelo(primeiro)
            try:
                chegou = porta.chegou.wait(15)
                fio_segundo, saida_segundo = self._em_paralelo(segundo)
                esperou = chegou and self._esperar_lock_da_empresa()
            finally:
                porta.liberada.set()
            fio_primeiro.join(30)
            fio_segundo.join(30)
        if fio_primeiro.is_alive() or fio_segundo.is_alive():
            raise AssertionError("Pedido da corrida não terminou")

        def desfecho(saida):
            if "resposta" not in saida:
                return repr(saida.get("erro")), None
            return saida["resposta"].status_code, saida["resposta"].headers.get("Location", "")

        return esperou, desfecho(saida_primeiro), desfecho(saida_segundo)

    def _contar(self, modelo, *condicoes):
        from sqlalchemy import func, select
        with self._sessao() as sessao:
            return sessao.scalar(select(func.count()).select_from(modelo).where(*condicoes))

    # ------------------------------------------------------------- 1. preparo
    def _etapa_preparo(self):
        from alembic.script import ScriptDirectory
        from flask_migrate import upgrade
        from models import Plano, Segmento, Usuario

        self.stage = "migração até a última revisão"
        self._migrar(upgrade, "head")
        self._limpar_cache_esquema()
        self.check("migration_reaches_head", self._revisao() == ScriptDirectory(MIGRATIONS).get_current_head())

        self.stage = "dados fictícios"
        with self._sessao() as sessao:
            plano = Plano(nome_plano="BRONZE", valor_mensal=Decimal("100"), qtd_analises_mes=1,
                          tipo_analise_permitida="MENSAL", nivel_entrega_analise="BASICA",
                          nivel_dashboard="RESUMIDO", nivel_atendimento="BAIXO")
            segmento = Segmento(nome_segmento="Chamados sintéticos")
            admin = Usuario(nome="Admin fictício", email="admin-chamados@example.invalid", role="ADMIN",
                            primeiro_acesso=False)
            admin.set_senha(PASSWORD)
            sessao.add_all([plano, segmento, admin])
            sessao.commit()
            self.plano_id, self.segmento_id, self.admin_id = plano.id_plano, segmento.id_segmento, admin.id_usuario
        self._nova_empresa("E1", senha=True)
        self.cliente_a = self._login("cliente-e1@example.invalid")[0]
        self.cliente_b = self._login("cliente-e1@example.invalid")[0]
        self.admin_a = self._login("admin-chamados@example.invalid")[0]
        self.admin_b = self._login("admin-chamados@example.invalid")[0]

    # ------------------------------------------------------------ 2. corridas
    def _etapa_corridas(self):
        from blueprints import admin as rotas_admin
        import chamados_suporte
        from models import Analise, ChamadoSuporte, MensagemSuporte, Notificacao

        id_empresa, id_cliente = self.ids["E1"]

        self.stage = "corrida: duas aberturas do mesmo chamado"

        def abertura(web):
            dados = {"csrf_token": self._csrf(web, "/cliente/tickets"), "assunto": "Dúvida fictícia",
                     "categoria": "FINANCEIRO", "mensagem": "Mensagem fictícia do cliente."}
            return lambda: web.post("/cliente/tickets", data=dados)

        esperou, (primeira, destino_a), (segunda, destino_b) = self._corrida(
            chamados_suporte, "chamado_repetido", abertura(self.cliente_a), abertura(self.cliente_b))
        chamados = self._contar(ChamadoSuporte, ChamadoSuporte.id_empresa == id_empresa)
        avisos = self._contar(Notificacao, Notificacao.id_usuario == self.admin_id)
        self.check("ticket_opening_waits_and_is_not_duplicated",
                   esperou and (primeira, segunda) == (302, 302) and destino_a == destino_b
                   and re.search(r"/cliente/tickets/\d+$", destino_a or "") is not None
                   and chamados == 1 and avisos == 1,
                   f"esperou={esperou} {primeira}/{segunda} {destino_a} {destino_b} chamados={chamados} avisos={avisos}")
        id_chamado = int(destino_a.rsplit("/", 1)[1])

        self.stage = "corrida: duas respostas iguais do cliente"
        caminho = f"/cliente/tickets/{id_chamado}"

        def resposta_do_cliente(web):
            dados = {"csrf_token": self._csrf(web, caminho), "mensagem": "Mensagem repetida do cliente."}
            return lambda: web.post(caminho, data=dados)

        esperou, (primeira, _), (segunda, _) = self._corrida(
            chamados_suporte, "resposta_repetida", resposta_do_cliente(self.cliente_a),
            resposta_do_cliente(self.cliente_b))
        mensagens = self._contar(MensagemSuporte, MensagemSuporte.id_chamado == id_chamado)
        self.check("client_reply_waits_and_is_not_duplicated",
                   esperou and (primeira, segunda) == (302, 302) and mensagens == 2,
                   f"esperou={esperou} {primeira}/{segunda} mensagens={mensagens}")

        self.stage = "corrida: duas respostas iguais da equipe"
        caminho_admin = f"/admin/tickets/{id_chamado}"

        def resposta_da_equipe(web):
            dados = {"csrf_token": self._csrf(web, caminho_admin), "acao": "responder",
                     "mensagem": "Resposta repetida da equipe."}
            return lambda: web.post(caminho_admin, data=dados)

        with patch.object(rotas_admin, "_whatsapp_chamado_atualizado"):
            esperou, (primeira, _), (segunda, _) = self._corrida(
                chamados_suporte, "resposta_repetida", resposta_da_equipe(self.admin_a),
                resposta_da_equipe(self.admin_b))
        mensagens = self._contar(MensagemSuporte, MensagemSuporte.id_chamado == id_chamado)
        avisos = self._contar(Notificacao, Notificacao.id_usuario == id_cliente)
        self.check("team_reply_waits_and_is_not_duplicated",
                   esperou and (primeira, segunda) == (302, 302) and mensagens == 3 and avisos == 1,
                   f"esperou={esperou} {primeira}/{segunda} mensagens={mensagens} avisos={avisos}")

        self.stage = "corrida: duas aberturas da mesma análise"

        def nova_analise(web):
            dados = {"csrf_token": self._csrf(web, "/admin/analises/nova"), "id_empresa": str(id_empresa),
                     "tipo_analise": "MENSAL", "periodo_inicio": "2026-04-01", "periodo_fim": "2026-04-30",
                     "mes_referencia": "4", "ano_referencia": "2026"}
            return lambda: web.post("/admin/analises/nova", data=dados)

        with patch.object(rotas_admin, "_whatsapp_abertura_do_envio"):
            esperou, (primeira, destino_a), (segunda, destino_b) = self._corrida(
                rotas_admin, "_analise_da_competencia", nova_analise(self.admin_a), nova_analise(self.admin_b))
        analises = self._contar(Analise, Analise.id_empresa == id_empresa, Analise.mes_referencia == 4)
        self.check("analysis_waits_and_is_not_duplicated",
                   esperou and (primeira, segunda) == (302, 302) and destino_a == destino_b
                   and re.search(r"/admin/analise/\d+/upload$", destino_a or "") is not None and analises == 1,
                   f"esperou={esperou} {primeira}/{segunda} {destino_a} {destino_b} analises={analises}")


def _worker_main(bin_dir, work_dir):
    cluster = _ClusterDosChamados(Path(bin_dir).resolve(), Path(work_dir).resolve())
    # Não encaminha mensagens de Flask, SQLAlchemy, Alembic ou PostgreSQL.
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            report = cluster.run()
    except Exception as error:
        detail = f": {error}" if isinstance(error, AssertionError) else ""
        print(json.dumps({"status": "error", "stage": cluster.stage, "error": type(error).__name__ + detail}))
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--isolated-worker":
        raise SystemExit(_worker_main(sys.argv[2], sys.argv[3]))
    unittest.main()
