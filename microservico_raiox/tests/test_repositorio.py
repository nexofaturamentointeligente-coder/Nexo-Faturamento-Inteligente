"""O repositório com uma conexão espiã: a ordem dos comandos na transação, sem banco de verdade.

O ensaio num PostgreSQL de verdade, com envios simultâneos e publicações de versão concorrentes, é a etapa 6d de
tests/test_leads_postgresql.py, na aplicação principal.
"""

import unittest
from unittest import mock
import uuid

import psycopg2

from repositorio import (TRAVA_DO_TETO, ErroDeBanco, RepositorioDeLeads, TermosDesatualizados, TermosIndisponiveis,
                         TetoDoDiaAtingido, Vigente, conferir_termos, versao_dos_termos)

# As linhas de documento_legal que a leitura das vigentes devolve: tipo, id, versão maior e menor.
PUBLICADOS = [("POLITICA_PRIVACIDADE", 12, 1, 1), ("TERMOS_USO", 11, 1, 0)]
LIDOS = {"TERMOS_USO": 11, "POLITICA_PRIVACIDADE": 12}


class _CursorEspiao:
    def __init__(self, contagem, falhar_em=None, publicados=None):
        self.comandos = []
        self._contagem = contagem
        self._falhar_em = falhar_em
        self._publicados = PUBLICADOS if publicados is None else publicados

    def execute(self, consulta, parametros=None):
        self.comandos.append((consulta, parametros))
        if self._falhar_em and consulta.startswith(self._falhar_em):
            raise psycopg2.OperationalError("conexão perdida")

    def fetchone(self):
        return (self._contagem,)

    def fetchall(self):
        return list(self._publicados)

    def __enter__(self):
        return self

    def __exit__(self, *_erro):
        return False


class _ConexaoEspia:
    def __init__(self, cursor):
        self.cursor_espiao = cursor
        self.desfechos = []
        self.fechada = False

    def cursor(self):
        return self.cursor_espiao

    def __enter__(self):
        return self

    def __exit__(self, tipo, _valor, _rastro):
        self.desfechos.append("rollback" if tipo else "commit")
        return False

    def close(self):
        self.fechada = True


def _lead():
    return {"id": uuid.uuid4(), "nome_contato": "Contato", "email": "a@example.invalid", "whatsapp": "62988881234",
            "nome_loja": "Loja", "interesse": "raio_x", "status_arquivo": "aprovado", "arquivos_storage": None}


class RepositorioTests(unittest.TestCase):
    def conectar(self, contagem=0, falhar_em=None, publicados=None):
        self.conexao = _ConexaoEspia(_CursorEspiao(contagem, falhar_em, publicados))
        espiao = mock.patch("repositorio.psycopg2.connect", return_value=self.conexao)
        self.connect = espiao.start()
        self.addCleanup(espiao.stop)
        return RepositorioDeLeads("postgresql://usuario@banco.exemplo/nexo")

    def comandos(self):
        return [consulta for consulta, _parametros in self.conexao.cursor_espiao.comandos]

    def test_gravacao_trava_conta_e_le_os_termos_na_mesma_transacao_do_insert(self):
        # A trava consultiva antes da contagem, na mesma transação do INSERT: envios simultâneos esperam a vez
        # e contam o lead do anterior, e o teto do dia fica exato. Depois, documento_legal travada em SHARE MODE
        # antes de ler as vigentes: a publicação em andamento termina antes, e a seguinte espera o COMMIT.
        lead = _lead()
        self.conectar(contagem=2).gravar(lead, teto=3, desde="desde", lidos=LIDOS, ip="203.0.113.9")
        comandos = [consulta.split(" ", 2)[:2] for consulta in self.comandos()]
        self.assertEqual(comandos, [["SET", "LOCAL"], ["SELECT", "pg_advisory_xact_lock(%s)"], ["SELECT", "count(*)"],
                                    ["LOCK", "TABLE"], ["SELECT", "DISTINCT"], ["INSERT", "INTO"], ["INSERT", "INTO"],
                                    ["INSERT", "INTO"]])
        self.assertEqual(self.comandos()[3], "LOCK TABLE documento_legal IN SHARE MODE")
        self.assertIn("ORDER BY tipo, versao DESC", self.comandos()[4])
        self.assertEqual(self.conexao.cursor_espiao.comandos[1][1], (TRAVA_DO_TETO,))
        self.assertEqual((self.conexao.desfechos, self.conexao.fechada, self.connect.call_count), (["commit"], True, 1))
        # O lead leva o resumo das versões; cada aceite, a versão exata, o IP e o now() do banco (o mesmo do lead).
        insercao_do_lead, *aceites = self.conexao.cursor_espiao.comandos[5:]
        self.assertTrue(insercao_do_lead[0].startswith("INSERT INTO lead_avaliacao"))
        self.assertEqual(insercao_do_lead[1]["termos_versao"], "TU-v1.0 PP-v1.1")
        self.assertIn("now(), %(termos_versao)s", insercao_do_lead[0])
        self.assertEqual([consulta.split(" (", 1)[0] for consulta, _parametros in aceites],
                         ["INSERT INTO aceite_documento_lead"] * 2)
        self.assertTrue(all("now(), %(ip)s)" in consulta for consulta, _parametros in aceites))
        self.assertEqual([parametros for _consulta, parametros in aceites],
                         [{"id": str(lead["id"]), "id_documento": 11, "ip": "203.0.113.9"},
                          {"id": str(lead["id"]), "id_documento": 12, "ip": "203.0.113.9"}])

    def test_sem_ip_o_aceite_vai_com_nulo(self):
        self.conectar().gravar(_lead(), teto=3, desde="desde", lidos=LIDOS)
        self.assertEqual([parametros["ip"] for consulta, parametros in self.conexao.cursor_espiao.comandos
                          if consulta.startswith("INSERT INTO aceite_documento_lead")], [None, None])

    def test_teto_atingido_nao_trava_os_documentos_nem_insere(self):
        with self.assertRaises(TetoDoDiaAtingido):
            self.conectar(contagem=3).gravar(_lead(), teto=3, desde="desde", lidos=LIDOS)
        self.assertFalse(any(consulta.startswith(("LOCK", "INSERT")) for consulta in self.comandos()))
        self.assertEqual((self.conexao.desfechos, self.conexao.fechada), (["rollback"], True))

    def test_versao_que_nao_e_a_vigente_nao_insere_e_desfaz(self):
        # A política ganhou a v1.2 (id 13) enquanto o visitante lia a v1.1 (id 12).
        publicados = [("POLITICA_PRIVACIDADE", 13, 1, 2), ("TERMOS_USO", 11, 1, 0)]
        with self.assertRaises(TermosDesatualizados):
            self.conectar(publicados=publicados).gravar(_lead(), teto=3, desde="desde", lidos=LIDOS)
        self.assertFalse(any(consulta.startswith("INSERT") for consulta in self.comandos()))
        self.assertEqual((self.conexao.desfechos, self.conexao.fechada), (["rollback"], True))

    def test_sem_documento_publicado_nao_insere_e_desfaz(self):
        for publicados in ([], [("TERMOS_USO", 11, 1, 0)]):
            with self.subTest(publicados=publicados):
                with self.assertRaises(TermosIndisponiveis):
                    self.conectar(publicados=publicados).gravar(_lead(), teto=3, desde="desde", lidos=LIDOS)
                self.assertFalse(any(consulta.startswith("INSERT") for consulta in self.comandos()))
                self.assertEqual(self.conexao.desfechos, ["rollback"])

    def test_falha_do_banco_vira_erro_sem_a_url(self):
        for comando in ("INSERT", "LOCK"):
            with self.subTest(comando=comando):
                with self.assertRaises(ErroDeBanco) as erro:
                    self.conectar(falhar_em=comando).gravar(_lead(), teto=3, desde="desde", lidos=LIDOS)
                self.assertEqual(str(erro.exception), "o banco recusou a operação (OperationalError)")
                self.assertEqual((self.conexao.desfechos, self.conexao.fechada), (["rollback"], True))

    def test_primeira_conferencia_conta_e_le_as_vigentes_sem_trava(self):
        leads, vigentes = self.conectar(contagem=7).primeira_conferencia("desde")
        self.assertEqual((leads, vigentes), (7, {"TERMOS_USO": Vigente(11, "v1.0"),
                                                 "POLITICA_PRIVACIDADE": Vigente(12, "v1.1")}))
        comandos = [consulta.split(" ", 2)[:2] for consulta in self.comandos()]
        self.assertEqual(comandos, [["SET", "LOCAL"], ["SELECT", "count(*)"], ["SELECT", "DISTINCT"]])
        self.assertEqual((self.conexao.desfechos, self.connect.call_count), (["commit"], 1))

    def test_conferencia_da_tabela_le_as_colunas_das_migracoes_e_trava_como_a_gravacao(self):
        vigentes = self.conectar().conferir_tabela()
        self.assertEqual(vigentes, {"TERMOS_USO": Vigente(11, "v1.0"), "POLITICA_PRIVACIDADE": Vigente(12, "v1.1")})
        consultas = self.comandos()
        for coluna in ("interesse", "status_arquivo", "arquivos_storage", "termos_aceitos_em", "termos_versao"):
            self.assertIn(coluna, consultas[1])
        self.assertTrue(consultas[1].endswith("FROM lead_avaliacao LIMIT 0"))
        self.assertEqual(consultas[2], "SELECT id_lead, id_documento, aceito_em, ip FROM aceite_documento_lead LIMIT 0")
        self.assertEqual(consultas[3], "LOCK TABLE documento_legal IN SHARE MODE")
        for coluna in ("tipo", "id_documento", "versao_maior", "versao_menor", "versao DESC"):
            self.assertIn(coluna, consultas[4])
        self.assertTrue(consultas[4].startswith("SELECT DISTINCT ON (tipo)"))

    def test_conexao_com_prazo_keepalive_ssl_e_nome_do_servico(self):
        self.conectar().leads_desde("desde")
        parametros = self.connect.call_args.kwargs
        self.assertEqual({chave: parametros[chave] for chave in ("sslmode", "connect_timeout", "keepalives",
                                                                   "tcp_user_timeout", "application_name")},
                         {"sslmode": "require", "connect_timeout": "10", "keepalives": "1",
                          "tcp_user_timeout": "20000", "application_name": "nexo_microservico_raiox"})


class TermosTests(unittest.TestCase):
    VIGENTES = {"TERMOS_USO": Vigente(11, "v1.0"), "POLITICA_PRIVACIDADE": Vigente(12, "v1.1")}

    def test_conferencia_das_versoes_lidas(self):
        conferir_termos(self.VIGENTES, LIDOS)
        for lidos in ({"TERMOS_USO": 11, "POLITICA_PRIVACIDADE": 13}, {"TERMOS_USO": 12, "POLITICA_PRIVACIDADE": 11},
                      {"TERMOS_USO": 11}, {}):
            with self.subTest(lidos=lidos), self.assertRaises(TermosDesatualizados):
                conferir_termos(self.VIGENTES, lidos)
        # Sem a vigente de um dos documentos, não há o que aceitar, leia o visitante o que for.
        for vigentes in ({}, {"TERMOS_USO": Vigente(11, "v1.0")}, {"POLITICA_PRIVACIDADE": Vigente(12, "v1.1")}):
            with self.subTest(vigentes=vigentes), self.assertRaises(TermosIndisponiveis):
                conferir_termos(vigentes, LIDOS)

    def test_resumo_das_versoes_no_lead(self):
        self.assertEqual(versao_dos_termos(self.VIGENTES), "TU-v1.0 PP-v1.1")
        # Rótulos com números enormes (47 caracteres juntos) são cortados no CHECK da coluna.
        enormes = {tipo: Vigente(indice, "v123456789.123456789") for indice, tipo in enumerate(self.VIGENTES, 1)}
        self.assertEqual(versao_dos_termos(enormes), "TU-v123456789.123456789 PP-v123456789.12")


if __name__ == "__main__":
    unittest.main()
