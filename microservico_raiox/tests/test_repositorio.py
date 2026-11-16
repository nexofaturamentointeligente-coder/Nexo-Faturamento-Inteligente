"""O repositório com uma conexão espiã: a ordem dos comandos na transação, sem banco de verdade.

O ensaio num PostgreSQL de verdade, com envios simultâneos, é a etapa 6d de tests/test_leads_postgresql.py, na
aplicação principal.
"""

import unittest
from unittest import mock
import uuid

import psycopg2

from repositorio import TRAVA_DO_TETO, ErroDeBanco, RepositorioDeLeads, TetoDoDiaAtingido


class _CursorEspiao:
    def __init__(self, contagem, falhar_em=None):
        self.comandos = []
        self._contagem = contagem
        self._falhar_em = falhar_em

    def execute(self, consulta, parametros=None):
        self.comandos.append((consulta, parametros))
        if self._falhar_em and consulta.startswith(self._falhar_em):
            raise psycopg2.OperationalError("conexão perdida")

    def fetchone(self):
        return (self._contagem,)

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
            "nome_loja": "Loja", "interesse": "raio_x", "status_arquivo": "aprovado", "arquivos_storage": None,
            "termos_versao": "termos-teste-v1"}


class RepositorioTests(unittest.TestCase):
    def conectar(self, contagem=0, falhar_em=None):
        self.conexao = _ConexaoEspia(_CursorEspiao(contagem, falhar_em))
        espiao = mock.patch("repositorio.psycopg2.connect", return_value=self.conexao)
        self.connect = espiao.start()
        self.addCleanup(espiao.stop)
        return RepositorioDeLeads("postgresql://usuario@banco.exemplo/nexo")

    def test_gravacao_trava_antes_de_contar_na_mesma_transacao(self):
        # A trava consultiva antes da contagem, na mesma transação do INSERT: envios simultâneos esperam a vez
        # e contam o lead do anterior, e o teto do dia fica exato.
        self.conectar(contagem=2).gravar(_lead(), teto=3, desde="desde")
        comandos = [consulta.split(" ", 2)[:2] for consulta, _parametros in self.conexao.cursor_espiao.comandos]
        self.assertEqual(comandos, [["SET", "LOCAL"], ["SELECT", "pg_advisory_xact_lock(%s)"], ["SELECT", "count(*)"],
                                    ["INSERT", "INTO"]])
        self.assertEqual(self.conexao.cursor_espiao.comandos[1][1], (TRAVA_DO_TETO,))
        self.assertEqual((self.conexao.desfechos, self.conexao.fechada, self.connect.call_count), (["commit"], True, 1))

    def test_teto_atingido_nao_insere_e_desfaz(self):
        with self.assertRaises(TetoDoDiaAtingido):
            self.conectar(contagem=3).gravar(_lead(), teto=3, desde="desde")
        self.assertFalse(any(consulta.startswith("INSERT") for consulta, _p in self.conexao.cursor_espiao.comandos))
        self.assertEqual((self.conexao.desfechos, self.conexao.fechada), (["rollback"], True))

    def test_falha_do_banco_vira_erro_sem_a_url(self):
        with self.assertRaises(ErroDeBanco) as erro:
            self.conectar(falhar_em="INSERT").gravar(_lead(), teto=3, desde="desde")
        self.assertEqual(str(erro.exception), "o banco recusou a operação (OperationalError)")
        self.assertTrue(self.conexao.fechada)

    def test_conferencia_da_tabela_le_as_colunas_da_migracao(self):
        self.conectar().conferir_tabela()
        consulta = self.conexao.cursor_espiao.comandos[-1][0]
        for coluna in ("interesse", "status_arquivo", "arquivos_storage", "termos_aceitos_em", "termos_versao"):
            self.assertIn(coluna, consulta)
        self.assertTrue(consulta.endswith("LIMIT 0"))

    def test_conexao_com_prazo_keepalive_ssl_e_nome_do_servico(self):
        self.conectar().leads_desde("desde")
        parametros = self.connect.call_args.kwargs
        self.assertEqual({chave: parametros[chave] for chave in ("sslmode", "connect_timeout", "keepalives",
                                                                   "tcp_user_timeout", "application_name")},
                         {"sslmode": "require", "connect_timeout": "10", "keepalives": "1",
                          "tcp_user_timeout": "20000", "application_name": "nexo_microservico_raiox"})


if __name__ == "__main__":
    unittest.main()
