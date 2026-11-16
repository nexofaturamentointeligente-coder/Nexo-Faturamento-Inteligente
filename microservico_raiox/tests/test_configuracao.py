"""Configuração só por variáveis de ambiente, com a lista de problemas no lugar de quebrar."""

import base64
import json
import unittest

from configuracao import Configuracao
from preparar_bucket import problemas_do_bucket
from repositorio import ErroDeBanco, parametros_de_conexao

def jwt_sintetico(papel: str) -> str:
    """Um JWT no formato das chaves legadas do Supabase, com assinatura falsa (só a carga é lida)."""
    def parte(dados):
        return base64.urlsafe_b64encode(json.dumps(dados).encode()).decode().rstrip("=")
    return f"{parte({'alg': 'HS256', 'typ': 'JWT'})}.{parte({'iss': 'supabase', 'role': papel})}.assinatura-de-teste"


SERVICE_ROLE = jwt_sintetico("service_role")
COMPLETO = {
    "DATABASE_URL": "postgresql://postgres:segredo-de-teste@db.abcdefghijklmnopqrst.supabase.co:5432/postgres",
    "SUPABASE_KEY": SERVICE_ROLE,
    "CORS_ORIGENS": "https://nexo.exemplo.com.br, https://www.nexo.exemplo.com.br/",
    "TERMOS_VERSAO": "termos-2026-11",
}


class ConfiguracaoTests(unittest.TestCase):
    def test_completa_fica_pronta_e_tira_a_url_do_projeto(self):
        configuracao = Configuracao.de_ambiente(COMPLETO)
        self.assertTrue(configuracao.pronta, configuracao.problemas)
        self.assertEqual(configuracao.supabase_url, "https://abcdefghijklmnopqrst.supabase.co")
        self.assertEqual(configuracao.origens, ("https://nexo.exemplo.com.br", "https://www.nexo.exemplo.com.br"))
        self.assertEqual((configuracao.bucket, configuracao.limite_por_hora, configuracao.limite_por_dia,
                          configuracao.teto_diario, configuracao.proxies_confiaveis, configuracao.max_bytes,
                          configuracao.prazo_da_triagem),
                         ("leads-raio-x", 5, 10, 100, 2, 15 * 1024 * 1024, 30))
        self.assertEqual(Configuracao.de_ambiente({**COMPLETO, "PRAZO_DA_TRIAGEM": "90"}).prazo_da_triagem, 90)
        self.assertEqual(Configuracao.de_ambiente({**COMPLETO, "PROXIES_CONFIAVEIS": "0"}).proxies_confiaveis, 0)

    def test_url_do_projeto_pelo_pooler_com_qualquer_papel(self):
        for usuario in ("postgres", "raiox", "papel.com.ponto"):
            with self.subTest(usuario=usuario):
                ambiente = {**COMPLETO, "DATABASE_URL": f"postgresql://{usuario}.abcdefghijklmnopqrst:segredo@"
                                                        "aws-0-sa-east-1.pooler.supabase.com:6543/postgres"}
                configuracao = Configuracao.de_ambiente(ambiente)
                self.assertEqual(configuracao.supabase_url, "https://abcdefghijklmnopqrst.supabase.co")
                self.assertTrue(configuracao.pronta, configuracao.problemas)
        sem_projeto = {**COMPLETO, "DATABASE_URL": "postgresql://raiox:segredo@aws-0-sa-east-1.pooler.supabase.com"
                                                   ":6543/postgres"}
        self.assertTrue(any("SUPABASE_URL" in p for p in Configuracao.de_ambiente(sem_projeto).problemas))

    def test_database_url_no_formato_da_aplicacao_principal(self):
        # Os parâmetros do Supabase e do Prisma e o esquema do SQLAlchemy não impedem o serviço de ficar pronto.
        for url in ("postgresql://postgres.abcdefghijklmnopqrst:segredo@aws-0-sa-east-1.pooler.supabase.com:6543/"
                    "postgres?pgbouncer=true&connection_limit=1",
                    "postgresql+psycopg2://postgres:segredo@db.abcdefghijklmnopqrst.supabase.co:5432/postgres"
                    "?sslmode=require&schema=public",
                    "postgres://postgres:segredo@db.abcdefghijklmnopqrst.supabase.co:5432/postgres"):
            with self.subTest(url=url[:40]):
                configuracao = Configuracao.de_ambiente({**COMPLETO, "DATABASE_URL": url})
                self.assertTrue(configuracao.pronta, configuracao.problemas)

    def test_database_url_lida_como_a_aplicacao_principal_le(self):
        # Pelo SQLAlchemy: a senha vai até o @ (com / e % sem codificar), e o + e o %20 da consulta viram espaço.
        casos = {
            "barra na senha": ("postgresql://raiox:ab/cd@127.0.0.1:55491/nexo",
                               {"user": "raiox", "password": "ab/cd", "host": "127.0.0.1", "port": "55491",
                                "dbname": "nexo"}),
            "porcento sem código na senha": ("postgresql://raiox:ab/cd%zz@127.0.0.1/nexo", {"password": "ab/cd%zz"}),
            "senha codificada": ("postgresql://raiox:a%40b%2Fc@127.0.0.1/nexo", {"password": "a@b/c"}),
            "espaço codificado na consulta": ("postgresql://raiox@127.0.0.1/nexo?options=-c%20statement_timeout%3D4321",
                                              {"options": "-c statement_timeout=4321"}),
            "mais na consulta": ("postgresql://raiox@127.0.0.1/nexo?options=-c+statement_timeout%3D4321",
                                 {"options": "-c statement_timeout=4321"}),
            "sslmode da URL vale": ("postgresql://raiox@127.0.0.1/nexo?sslmode=disable", {"sslmode": "disable"}),
            "IPv6": ("postgresql://raiox@[::1]:5432/nexo", {"host": "::1", "port": "5432"}),
        }
        for rotulo, (url, esperado) in casos.items():
            with self.subTest(caso=rotulo):
                parametros = parametros_de_conexao(url)
                self.assertEqual({chave: parametros.get(chave) for chave in esperado}, esperado)
                self.assertEqual(parametros["application_name"], "nexo_microservico_raiox")
        self.assertEqual(parametros_de_conexao("postgresql://raiox@127.0.0.1/nexo")["sslmode"], "require")
        # Com a barra na senha, o pooler ainda dá o projeto (antes, a barra virava host e porta).
        pooler = {**COMPLETO, "DATABASE_URL": "postgresql://postgres.abcdefghijklmnopqrst:ab/cd@aws-0-sa-east-1."
                                              "pooler.supabase.com:6543/postgres"}
        self.assertEqual(Configuracao.de_ambiente(pooler).supabase_url, "https://abcdefghijklmnopqrst.supabase.co")
        for url in ("postgresql://raiox@127.0.0.1:porta/nexo", "postgresql://raiox@127.0.0.1:5²/nexo",
                    "postgresql://raiox@127.0.0.1:70000/nexo",
                    # Host e porta na consulta e parâmetro repetido: a leitura da aplicação principal é outra.
                    "postgresql://raiox@/nexo?host=h1.exemplo:5433", "postgresql://raiox@/nexo?host=h1&host=h2",
                    "postgresql://raiox@127.0.0.1/nexo?port=5433", "postgresql://raiox@127.0.0.1/nexo?hostaddr=10.0.0.1",
                    "postgresql://raiox@127.0.0.1/nexo?sslmode=require&sslmode=disable"):
            with self.subTest(url=url):
                with self.assertRaises(ErroDeBanco):
                    parametros_de_conexao(url)
        # A porta lida como o int() do SQLAlchemy, e a tabulação e a quebra de linha tiradas de qualquer ponto,
        # como o urlsplit da aplicação principal faz.
        for porta in (" 6543", "+6543", "0006543", "6_543"):
            with self.subTest(porta=porta):
                self.assertEqual(parametros_de_conexao(f"postgresql://raiox@127.0.0.1:{porta}/nexo")["port"], "6543")
        parametros = parametros_de_conexao("postgresql://raiox:seg\tre\ndo@127.0.0.1/no\rva")
        self.assertEqual((parametros["password"], parametros["dbname"]), ("segredo", "nova"))
        # Sem senha, o "@" num valor da consulta não corta a URL noutro lugar: a consulta é refeita antes, como na
        # _limpar_url_pg da aplicação principal.
        parametros = parametros_de_conexao("postgresql://postgres.abcdefghijklmnopqrst@aws-0-sa-east-1.pooler."
                                           "supabase.com:6543/postgres?application_name=nexo@render")
        self.assertEqual((parametros["host"], parametros["port"], parametros["user"], parametros["dbname"]),
                         ("aws-0-sa-east-1.pooler.supabase.com", "6543", "postgres.abcdefghijklmnopqrst", "postgres"))
        self.assertNotIn("password", parametros)

    def test_inteiro_so_com_digitos_ascii_e_sem_derrubar_a_subida(self):
        for valor in ("5²", "²", "①", "1" * 5000, "-1", "abc", "1e3"):
            with self.subTest(valor=valor[:10]):
                configuracao = Configuracao.de_ambiente({**COMPLETO, "LIMITE_POR_HORA": valor})
                self.assertFalse(configuracao.pronta)
                self.assertEqual(configuracao.limite_por_hora, 5)
        for nome, valor in (("PRAZO_DA_TRIAGEM", "301"), ("PROXIES_CONFIAVEIS", "6")):
            with self.subTest(nome=nome):
                self.assertFalse(Configuracao.de_ambiente({**COMPLETO, nome: valor}).pronta)

    def test_origem_invalida_vai_para_o_log_sem_o_valor(self):
        colado = "postgresql://postgres.abcdefghijklmnopqrst:Senha-Do-Banco-2026@aws-0.pooler.supabase.com:6543/x"
        configuracao = Configuracao.de_ambiente({**COMPLETO, "CORS_ORIGENS": f"https://nexo.exemplo.com.br,{colado}"})
        self.assertEqual(configuracao.origens, ("https://nexo.exemplo.com.br",))
        texto = " ".join(configuracao.problemas)
        self.assertIn("posição 2", texto)
        self.assertNotIn("senha-do-banco", texto.lower())
        self.assertNotIn("pooler", texto)

    def test_so_a_chave_do_servidor_e_aceita(self):
        # A publicável nova e a anon legada (um JWT com role anon) não gravam no bucket privado.
        for chave in ("sb_publishable_EXEMPLO_sintetico", jwt_sintetico("anon"), jwt_sintetico("authenticated"),
                      "chave-qualquer", "a.b.c", "sb_qualquer_coisa"):
            with self.subTest(chave=chave[:20]):
                configuracao = Configuracao.de_ambiente({**COMPLETO, "SUPABASE_KEY": chave})
                self.assertTrue(any("publicável" in problema for problema in configuracao.problemas),
                                configuracao.problemas)
                self.assertNotIn(chave, " ".join(configuracao.problemas))
        for chave in ("sb_secret_EXEMPLO_sintetico", SERVICE_ROLE):
            with self.subTest(chave=chave[:20]):
                self.assertTrue(Configuracao.de_ambiente({**COMPLETO, "SUPABASE_KEY": chave}).pronta)

    def test_chave_com_carga_patologica_vira_problema_e_nao_derruba_a_subida(self):
        def com_carga(bruta: bytes) -> str:
            return "eyJhbGciOiJIUzI1NiJ9." + base64.urlsafe_b64encode(bruta).decode().rstrip("=") + ".assinatura"
        for rotulo, bruta in (("listas aninhadas", b"[" * 100_000 + b"]" * 100_000),
                              ("objetos aninhados", b'{"a":' * 50_000 + b"1" + b"}" * 50_000),
                              ("número enorme", b'{"role":' + b"9" * 5000 + b"}"),
                              ("bytes inválidos", b"\xff\xfe{")):
            with self.subTest(carga=rotulo):
                configuracao = Configuracao.de_ambiente({**COMPLETO, "SUPABASE_KEY": com_carga(bruta)})
                self.assertFalse(configuracao.pronta)
                self.assertTrue(any("publicável" in problema for problema in configuracao.problemas),
                                configuracao.problemas)

    def test_database_url_invalida_vira_problema_sem_vazar_a_url(self):
        for url in ("mysql://usuario:segredo-de-teste@banco/nexo", "isto não é segredo-de-teste",
                    "postgresql://usuario:segredo-de-teste@banco/nexo?opcao_que_nao_existe=1"):
            with self.subTest(url=url[:20]):
                configuracao = Configuracao.de_ambiente({**COMPLETO, "DATABASE_URL": url})
                self.assertFalse(configuracao.pronta)
                self.assertTrue(any(p.startswith("DATABASE_URL inválida") for p in configuracao.problemas))
                self.assertNotIn("segredo-de-teste", " ".join(configuracao.problemas))

    def test_preparar_bucket_so_pede_o_que_usa(self):
        so_storage = {"SUPABASE_URL": "https://abcdefghijklmnopqrst.supabase.co", "SUPABASE_KEY": "sb_secret_teste"}
        self.assertEqual(problemas_do_bucket(Configuracao.de_ambiente(so_storage), so_storage), [])
        sem_nada = {"SUPABASE_KEY": "sb_secret_teste"}
        faltando = problemas_do_bucket(Configuracao.de_ambiente(sem_nada), sem_nada)
        self.assertTrue(any(p.startswith("DATABASE_URL") for p in faltando), faltando)
        self.assertTrue(any(p.startswith("SUPABASE_URL") for p in faltando), faltando)
        self.assertFalse(any("CORS" in p or "TERMOS" in p for p in faltando), faltando)
        pelo_banco = {"SUPABASE_KEY": "sb_secret_teste", "DATABASE_URL": COMPLETO["DATABASE_URL"]}
        self.assertEqual(problemas_do_bucket(Configuracao.de_ambiente(pelo_banco), pelo_banco), [])
        # A chave pública impede o preparo do bucket.
        publica = {**so_storage, "SUPABASE_KEY": jwt_sintetico("anon")}
        self.assertTrue(problemas_do_bucket(Configuracao.de_ambiente(publica), publica))

    def test_cada_falta_vira_problema(self):
        for nome in ("DATABASE_URL", "SUPABASE_KEY", "CORS_ORIGENS", "TERMOS_VERSAO"):
            with self.subTest(falta=nome):
                configuracao = Configuracao.de_ambiente({chave: valor for chave, valor in COMPLETO.items()
                                                         if chave != nome})
                self.assertFalse(configuracao.pronta)
                self.assertTrue(any(nome in problema for problema in configuracao.problemas), configuracao.problemas)
        sem_url = {**COMPLETO, "DATABASE_URL": "postgresql://usuario@banco.exemplo/nexo"}
        self.assertTrue(any("SUPABASE_URL" in p for p in Configuracao.de_ambiente(sem_url).problemas))

    def test_valores_invalidos(self):
        casos = {
            "origem com caminho": {"CORS_ORIGENS": "https://nexo.exemplo.com.br/landing"},
            "origem http": {"CORS_ORIGENS": "http://nexo.exemplo.com.br"},
            "supabase sem https": {"SUPABASE_URL": "http://abcdefghijklmnopqrst.supabase.co"},
            "supabase com caminho": {"SUPABASE_URL": "https://abcdefghijklmnopqrst.supabase.co/storage"},
            "versão com espaço": {"TERMOS_VERSAO": "termos de uso"},
            "versão longa": {"TERMOS_VERSAO": "v" * 41},
            "limite não numérico": {"LIMITE_POR_HORA": "cinco"},
            "limite zero": {"LIMITE_POR_DIA": "0"},
            "prazo zero": {"PRAZO_DA_TRIAGEM": "0"},
            "bucket inválido": {"SUPABASE_BUCKET": "Leads Raio X"},
        }
        for rotulo, troca in casos.items():
            with self.subTest(caso=rotulo):
                self.assertFalse(Configuracao.de_ambiente({**COMPLETO, **troca}).pronta)

    def test_localhost_so_com_a_liberacao(self):
        configuracao = Configuracao.de_ambiente(COMPLETO)
        self.assertTrue(configuracao.origem_permitida("https://nexo.exemplo.com.br"))
        for origem in ("http://localhost:5000", "https://nexo.exemplo.com.br.atacante.exemplo", "null", "", None,
                       "HTTPS://NEXO.EXEMPLO.COM.BR"):
            with self.subTest(origem=origem):
                self.assertFalse(configuracao.origem_permitida(origem))
        local = Configuracao.de_ambiente({**COMPLETO, "PERMITIR_LOCALHOST": "1",
                                          "SUPABASE_URL": "http://127.0.0.1:54321"})
        self.assertTrue(local.pronta, local.problemas)
        self.assertTrue(local.origem_permitida("http://localhost:5000"))
        self.assertTrue(local.origem_permitida("http://127.0.0.1"))
        sem_origem = Configuracao.de_ambiente({**COMPLETO, "CORS_ORIGENS": "", "PERMITIR_LOCALHOST": "sim"})
        self.assertTrue(sem_origem.pronta, sem_origem.problemas)

    def test_representacao_sem_credenciais(self):
        texto = repr(Configuracao.de_ambiente(COMPLETO)) + str(Configuracao.de_ambiente(COMPLETO))
        for segredo in ("segredo-de-teste", SERVICE_ROLE, "abcdefghijklmnopqrst"):
            self.assertNotIn(segredo, texto)


if __name__ == "__main__":
    unittest.main()
