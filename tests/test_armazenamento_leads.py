"""Leitura das planilhas dos leads da landing no Storage do Supabase (armazenamento_leads.py).

  1. Configuração: o endereço https://<projeto>.supabase.co tirado da conexão do banco (direta ou pooler), a chave do
     servidor (a secreta nova ou a service_role legada; a publicável e a anon não servem) e o bucket.
  2. O cliente contra um servidor HTTP local (dublê do Storage; nenhum serviço externo): só GET, os cabeçalhos por tipo
     de chave, a recusa de http fora do localhost, nenhum redirecionamento seguido, o teto de bytes (pelo
     Content-Length e pelo que chega), o prazo total e nenhum segredo, endereço ou caminho nas mensagens de erro; a
     gravação direto no arquivo (baixar_para) com as mesmas conferências.
  3. O registro do microsserviço (arquivos_storage) conferido antes de qualquer pedido: só os caminhos deste lead, os
     formatos csv e xlsx, os tamanhos de 1 byte a 15 MB, o SHA-256 que começa pelo do caminho; o nome de baixar e os
     nomes únicos do .zip.
  4. Render e .env.example: a variável SUPABASE_KEY declarada sem valor.

Dados e chaves fictícios.
"""

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
import zipfile

from dotenv import dotenv_values

import armazenamento_leads
from armazenamento_leads import ArmazenamentoLeads, ErroDeArmazenamento, Planilha
from tests.deploy_support import ROOT

CHAVE_NOVA = "sb_secret_EXEMPLO_sintetico_de_teste"
PROJETO = "abcdefghijklmnopqrst"


def _jwt(papel):
    """Um JWT de exemplo (sem assinatura válida) com o papel na carga, como as chaves legadas do Supabase."""
    def parte(dados):
        return base64.urlsafe_b64encode(json.dumps(dados).encode()).decode().rstrip("=")
    return f"{parte({'alg': 'HS256', 'typ': 'JWT'})}.{parte({'role': papel, 'ref': PROJETO})}.assinatura-de-exemplo"


SERVICE_ROLE = _jwt("service_role")


class _Servidor:
    """Dublê do Storage em 127.0.0.1: guarda os pedidos e responde o que o teste mandar."""

    def __init__(self):
        self.pedidos = []
        self.status = 200
        self.corpo = b"Codigo;Produto\n1;Cafe\n"
        self.modo = None
        servidor = self

        class Tratador(BaseHTTPRequestHandler):
            def _atender(self):
                cabecalhos = {nome.lower(): valor for nome, valor in self.headers.items()}
                servidor.pedidos.append((self.command, self.path, cabecalhos))
                if servidor.modo == "gotejando":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv")
                    self.end_headers()
                    for _ in range(40):
                        try:
                            self.wfile.write(b"x")
                            self.wfile.flush()
                        except OSError:
                            return
                        time.sleep(0.1)
                    return
                if servidor.modo == "sem tamanho":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv")
                    self.end_headers()
                    self.wfile.write(servidor.corpo)
                    self.close_connection = True
                    return
                if servidor.modo == "tamanho mentiroso":
                    self.send_response(200)
                    self.send_header("Content-Length", "10")
                    self.end_headers()
                    self.wfile.write(servidor.corpo)
                    return
                self.send_response(servidor.status)
                if servidor.status == 302:
                    self.send_header("Location", f"http://127.0.0.1:{servidor.porta}/desvio")
                self.send_header("Content-Type", "text/csv" if servidor.status == 200 else "application/json")
                self.send_header("Content-Length", str(len(servidor.corpo)))
                self.end_headers()
                self.wfile.write(servidor.corpo)

            do_GET = do_POST = do_DELETE = _atender

            def log_message(self, *_argumentos):
                pass

        class Http(ThreadingHTTPServer):
            # O tratador que goteja não segura o fim do teste.
            daemon_threads = True
            block_on_close = False

        self._http = Http(("127.0.0.1", 0), Tratador)
        self.porta = self._http.server_address[1]
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

    def parar(self):
        self._http.shutdown()
        self._http.server_close()


# =============================================================================
# 1. Configuração
# =============================================================================
class ConfiguracaoTests(unittest.TestCase):
    def test_endereco_do_projeto_pela_conexao_do_banco(self):
        casos = {
            f"postgresql://postgres:senha-ficticia@db.{PROJETO}.supabase.co:5432/postgres": f"https://{PROJETO}.supabase.co",
            f"postgresql+psycopg2://postgres.{PROJETO}:senha-ficticia@aws-0-us-west-2.pooler.supabase.com:6543/"
            "postgres?sslmode=require": f"https://{PROJETO}.supabase.co",
            f"postgres://postgres.{PROJETO}:senha-ficticia@aws-1-sa-east-1.pooler.supabase.com:5432/postgres":
                f"https://{PROJETO}.supabase.co",
            # Fora do Supabase, ou sem o projeto legível: não há Storage.
            "sqlite:///:memory:": "", "sqlite:///instance/nexo_mvp.db": "",
            "postgresql://usuario:senha-ficticia@localhost:5432/nexo": "",
            f"postgresql://postgres:senha-ficticia@db.{PROJETO}.supabase.co.exemplo.invalid/postgres": "",
            "postgresql://postgres.CURTO:senha-ficticia@aws-0-us-west-2.pooler.supabase.com:6543/postgres": "",
            "postgresql://postgres:senha-ficticia@aws-0-us-west-2.pooler.supabase.com:6543/postgres": "",
            f"mysql://root:senha-ficticia@db.{PROJETO}.supabase.co/x": "",
            "": "", "   ": "", None: "", 42: "", "isto não é url": "",
        }
        for url, esperado in casos.items():
            with self.subTest(url=url):
                self.assertEqual(armazenamento_leads.url_do_projeto(url), esperado)

    def test_chave_do_servidor(self):
        for chave in (CHAVE_NOVA, SERVICE_ROLE):
            with self.subTest(chave=chave[:12]):
                self.assertTrue(armazenamento_leads.chave_do_servidor(chave))
        for chave in ("sb_publishable_EXEMPLO_sintetico", _jwt("anon"), _jwt("authenticated"), "a.b.c", "x", "",
                      None, 7, "sb_", "eyJ.nao-e-base64.x"):
            with self.subTest(chave=chave):
                self.assertFalse(armazenamento_leads.chave_do_servidor(chave))

    def test_cliente_so_com_chave_do_servidor_banco_do_supabase_e_bucket_valido(self):
        banco = f"postgresql+psycopg2://postgres.{PROJETO}:senha-ficticia@aws-0-us-west-2.pooler.supabase.com:6543/postgres"
        pronto = armazenamento_leads.do_app({"SUPABASE_KEY": f"  {CHAVE_NOVA}  ", "SQLALCHEMY_DATABASE_URI": banco})
        self.assertIsInstance(pronto, ArmazenamentoLeads)
        self.assertEqual(pronto.bucket, "leads-raio-x")
        self.assertEqual(armazenamento_leads.do_app({"SUPABASE_KEY": SERVICE_ROLE, "SUPABASE_BUCKET": "outro-bucket",
                                                     "SQLALCHEMY_DATABASE_URI": banco}).bucket, "outro-bucket")
        ausentes = {
            "sem chave": {"SQLALCHEMY_DATABASE_URI": banco},
            "chave vazia": {"SUPABASE_KEY": "  ", "SQLALCHEMY_DATABASE_URI": banco},
            "chave publicável": {"SUPABASE_KEY": "sb_publishable_EXEMPLO_sintetico", "SQLALCHEMY_DATABASE_URI": banco},
            "anon legada": {"SUPABASE_KEY": _jwt("anon"), "SQLALCHEMY_DATABASE_URI": banco},
            "SQLite": {"SUPABASE_KEY": CHAVE_NOVA, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"},
            "sem banco": {"SUPABASE_KEY": CHAVE_NOVA},
            "bucket inválido": {"SUPABASE_KEY": CHAVE_NOVA, "SUPABASE_BUCKET": "../outro", "SQLALCHEMY_DATABASE_URI": banco},
        }
        for caso, config in ausentes.items():
            with self.subTest(caso=caso):
                self.assertIsNone(armazenamento_leads.do_app(config))

    def test_render_e_env_example_declaram_a_chave_sem_valor(self):
        render = (ROOT / "render.yaml").read_text(encoding="utf-8")
        # Sem o PyYAML (que nem todo ambiente tem): a entrada da lista de envVars, com o valor pedido no painel.
        self.assertRegex(render, r"\n\s+- key: SUPABASE_KEY\r?\n\s+sync: false\r?\n")
        self.assertNotRegex(render, r"key: SUPABASE_KEY\r?\n\s+value:")
        valores = dotenv_values(stream=io.StringIO((ROOT / ".env.example").read_text(encoding="utf-8")))
        self.assertEqual((valores.get("SUPABASE_KEY"), valores.get("SUPABASE_BUCKET")), ("", ""))


# =============================================================================
# 2. O cliente contra o dublê do Storage
# =============================================================================
class ClienteTests(unittest.TestCase):
    CAMINHO = "2026/11/abc/01-0123456789abcdef.csv"

    def setUp(self):
        self.servidor = _Servidor()
        self.addCleanup(self.servidor.parar)

    def cliente(self, chave=CHAVE_NOVA, **opcoes):
        opcoes.setdefault("timeout", 5)
        return ArmazenamentoLeads(f"http://127.0.0.1:{self.servidor.porta}", chave, "leads-raio-x", **opcoes)

    def test_so_get_do_objeto_com_a_chave_nova_so_no_apikey(self):
        self.assertEqual(self.cliente().ler(self.CAMINHO), self.servidor.corpo)
        [(metodo, caminho, cabecalhos)] = self.servidor.pedidos
        self.assertEqual((metodo, caminho), ("GET", f"/storage/v1/object/leads-raio-x/{self.CAMINHO}"))
        self.assertEqual(cabecalhos["apikey"], CHAVE_NOVA)
        # O Supabase lê o Authorization: Bearer como JWT e recusaria a chave nova.
        self.assertNotIn("authorization", cabecalhos)

    def test_service_role_legada_no_apikey_e_no_authorization(self):
        self.cliente(SERVICE_ROLE).ler(self.CAMINHO)
        cabecalhos = self.servidor.pedidos[0][2]
        self.assertEqual((cabecalhos["apikey"], cabecalhos["authorization"]), (SERVICE_ROLE, f"Bearer {SERVICE_ROLE}"))

    def test_caminho_com_caracteres_especiais_vai_codificado(self):
        self.cliente().ler("2026/11/a b?c#d.csv")
        self.assertEqual(self.servidor.pedidos[0][1], "/storage/v1/object/leads-raio-x/2026/11/a%20b%3Fc%23d.csv")

    def test_so_https_fora_do_localhost(self):
        for url in (f"http://{PROJETO}.supabase.co", "ftp://exemplo.invalid", "file:///etc/passwd", "", "https://",
                    "https://[ValorMarcado"):
            with self.subTest(url=url), self.assertRaises(ValueError) as erro:
                ArmazenamentoLeads(url, CHAVE_NOVA)
            self.assertNotIn("ValorMarcado", str(erro.exception))
            self.assertNotIn(CHAVE_NOVA, str(erro.exception))
        for chave, bucket in (("", "leads-raio-x"), (CHAVE_NOVA, ""), (CHAVE_NOVA, "Bucket Inválido")):
            with self.subTest(chave=chave, bucket=bucket), self.assertRaises(ValueError):
                ArmazenamentoLeads(f"https://{PROJETO}.supabase.co", chave, bucket)
        self.assertNotIn(CHAVE_NOVA, repr(ArmazenamentoLeads(f"https://{PROJETO}.supabase.co", CHAVE_NOVA)))

    def test_redirecionamento_nao_e_seguido(self):
        self.servidor.status = 302
        with self.assertRaises(ErroDeArmazenamento) as erro:
            self.cliente().ler(self.CAMINHO)
        self.assertEqual(erro.exception.status, 302)
        self.assertEqual([pedido[1] for pedido in self.servidor.pedidos], [f"/storage/v1/object/leads-raio-x/{self.CAMINHO}"])

    def test_teto_de_bytes_pelo_tamanho_declarado_e_pelo_que_chega(self):
        self.servidor.corpo = b"x" * 2000
        self.assertEqual(len(self.cliente(max_bytes=2000).ler(self.CAMINHO)), 2000)
        with self.assertRaises(ErroDeArmazenamento) as erro:
            self.cliente(max_bytes=1999).ler(self.CAMINHO)
        self.assertIn("tamanho permitido", str(erro.exception))
        # Sem Content-Length (até o fim da conexão), o teto vale para o que chega, sem guardar o excedente.
        self.servidor.modo = "sem tamanho"
        with self.assertRaises(ErroDeArmazenamento):
            self.cliente(max_bytes=1500).ler(self.CAMINHO)
        self.assertEqual(len(self.cliente(max_bytes=2000).ler(self.CAMINHO)), 2000)
        # O máximo pedido nunca passa do teto do cliente.
        with self.assertRaises(ErroDeArmazenamento):
            self.cliente(max_bytes=100).ler(self.CAMINHO, maximo=5000)

    def test_prazo_total_da_leitura(self):
        # Um byte a cada 0,1 s: nenhuma operação de rede espera mais que o timeout, mas o total passa do prazo.
        self.servidor.modo = "gotejando"
        inicio = time.monotonic()
        with self.assertRaises(ErroDeArmazenamento) as erro:
            self.cliente(timeout=1).ler(self.CAMINHO)
        self.assertLess(time.monotonic() - inicio, 3.5)
        self.assertIn("prazo", str(erro.exception))

    def test_erros_sem_chave_endereco_nem_caminho(self):
        cliente = self.cliente(SERVICE_ROLE)
        for status in (400, 401, 403, 404, 500, 503):
            with self.subTest(status=status):
                self.servidor.status = status
                with self.assertRaises(ErroDeArmazenamento) as erro:
                    cliente.ler(self.CAMINHO)
                mensagem = str(erro.exception)
                self.assertEqual((erro.exception.status, mensagem), (status, f"o Storage respondeu {status}"))
        mensagens = [str(erro.exception)]
        self.servidor.parar()
        fora_do_ar = ArmazenamentoLeads("http://127.0.0.1:9", SERVICE_ROLE, "leads-raio-x", timeout=2)
        with self.assertRaises(ErroDeArmazenamento) as erro:
            fora_do_ar.ler(self.CAMINHO)
        mensagens.append(str(erro.exception))
        for mensagem in mensagens:
            for segredo in (SERVICE_ROLE, "127.0.0.1", "abc/01-0123456789abcdef", "leads-raio-x"):
                self.assertNotIn(segredo, mensagem)

    def test_baixar_confere_tamanho_e_sha256(self):
        conteudo = self.servidor.corpo
        sha = hashlib.sha256(conteudo).hexdigest()
        planilha = Planilha(1, self.CAMINHO, "vendas.csv", "csv", len(conteudo), sha)
        self.assertEqual(self.cliente().baixar(planilha), conteudo)
        for caso, outra in {"outro conteúdo": Planilha(1, self.CAMINHO, "v.csv", "csv", len(conteudo), "0" * 64),
                            "tamanho menor no registro": Planilha(1, self.CAMINHO, "v.csv", "csv", len(conteudo) - 1, sha),
                            "tamanho maior no registro": Planilha(1, self.CAMINHO, "v.csv", "csv", len(conteudo) + 1, sha)}.items():
            with self.subTest(caso=caso), self.assertRaises(ErroDeArmazenamento) as erro:
                self.cliente().baixar(outra)
            self.assertNotIn(self.CAMINHO, str(erro.exception))
        # O Storage que declara menos bytes do que o registro: o que chega é cortado e não confere.
        self.servidor.modo = "tamanho mentiroso"
        with self.assertRaises(ErroDeArmazenamento) as erro:
            self.cliente().baixar(planilha)
        self.assertEqual(str(erro.exception), "o arquivo do Storage não confere com o registrado")

    def test_baixar_para_grava_no_arquivo_com_as_mesmas_conferencias(self):
        """O processamento dos leads grava a planilha direto no disco (baixar_para), sem guardá-la inteira na
        memória, com o teto e a conferência de tamanho e SHA-256 do baixar."""
        self.servidor.corpo = b"Codigo;Produto\n" + b"1;Cafe\n" * 30000
        conteudo = self.servidor.corpo
        sha = hashlib.sha256(conteudo).hexdigest()
        planilha = Planilha(1, self.CAMINHO, "vendas.csv", "csv", len(conteudo), sha)
        with tempfile.TemporaryDirectory(prefix="nexo-teste-baixar-") as pasta:
            destino = Path(pasta) / "relatorio-01.csv"
            self.cliente().baixar_para(planilha, destino)
            self.assertEqual(destino.read_bytes(), conteudo)
            # Arquivo que já existe não é sobrescrito, e nada é pedido ao Storage.
            pedidos = len(self.servidor.pedidos)
            with self.assertRaises(FileExistsError):
                self.cliente().baixar_para(planilha, destino)
            self.assertEqual(len(self.servidor.pedidos), pedidos)
            casos = {"outro conteúdo": Planilha(1, self.CAMINHO, "v.csv", "csv", len(conteudo), "0" * 64),
                     "tamanho menor no registro": Planilha(1, self.CAMINHO, "v.csv", "csv", len(conteudo) - 1, sha),
                     "tamanho maior no registro": Planilha(1, self.CAMINHO, "v.csv", "csv", len(conteudo) + 1, sha)}
            for numero, (caso, outra) in enumerate(casos.items()):
                with self.subTest(caso=caso), self.assertRaises(ErroDeArmazenamento) as erro:
                    self.cliente().baixar_para(outra, Path(pasta) / f"outra-{numero}.csv")
                self.assertNotIn(self.CAMINHO, str(erro.exception))
            self.servidor.modo = "tamanho mentiroso"
            with self.assertRaises(ErroDeArmazenamento) as erro:
                self.cliente().baixar_para(planilha, Path(pasta) / "mentiroso.csv")
            self.assertEqual(str(erro.exception), "o arquivo do Storage não confere com o registrado")


# =============================================================================
# 3. O registro do microsserviço
# =============================================================================
def _registro(lead_id, indice, conteudo=b"Codigo;Produto\n1;Cafe\n", formato="csv", **trocas):
    sha = hashlib.sha256(conteudo).hexdigest()
    item = {"caminho": f"2026/11/{lead_id}/{indice:02d}-{sha[:16]}.{formato}", "nome": f"vendas-{indice}.{formato}",
            "bytes": len(conteudo), "formato": formato, "sha256": sha}
    item.update(trocas)
    return item


class RegistroTests(unittest.TestCase):
    def setUp(self):
        self.lead = uuid.uuid4()

    def planilhas(self, itens):
        return armazenamento_leads.planilhas_do_lead(self.lead, json.dumps(itens, ensure_ascii=False))

    def test_itens_validos_em_ordem_de_indice(self):
        itens = [_registro(self.lead, 2, b"segundo", formato="xlsx"), _registro(self.lead, 1, b"primeiro")]
        planilhas = self.planilhas(itens)
        self.assertEqual([(p.indice, p.formato, p.tamanho) for p in planilhas], [(1, "csv", 8), (2, "xlsx", 7)])
        self.assertEqual(planilhas[1].tipo, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertEqual(planilhas[0].tipo, "text/csv")
        # O id do lead aceito como texto (com ou sem hífens) ou uuid; o caminho exige a forma do microsserviço.
        texto = json.dumps(itens)
        for identificador in (str(self.lead), self.lead.hex, str(self.lead).upper()):
            self.assertEqual(len(armazenamento_leads.planilhas_do_lead(identificador, texto)), 2)

    def test_registro_fora_do_formato_fica_de_fora(self):
        outro = uuid.uuid4()
        bom = _registro(self.lead, 1)
        sha = bom["sha256"]
        ruins = {
            "caminho de outro lead": _registro(outro, 2),
            "volta de pasta": _registro(self.lead, 3, caminho=f"2026/11/{self.lead}/../{outro}/03-{sha[:16]}.csv"),
            "id em maiúsculas": _registro(self.lead, 4, caminho=f"2026/11/{str(self.lead).upper()}/04-{sha[:16]}.csv"),
            "prefixo a mais": _registro(self.lead, 5, caminho=f"x/2026/11/{self.lead}/05-{sha[:16]}.csv"),
            "sufixo a mais": _registro(self.lead, 6, caminho=f"2026/11/{self.lead}/06-{sha[:16]}.csv.exe"),
            "quebra de linha no fim": _registro(self.lead, 7, caminho=f"2026/11/{self.lead}/07-{sha[:16]}.csv\n"),
            "extensão que não é do formato": _registro(self.lead, 8, formato="xlsx", caminho=f"2026/11/{self.lead}/08-{sha[:16]}.csv"),
            "formato fora da lista": _registro(self.lead, 9, formato="xlsm", caminho=f"2026/11/{self.lead}/09-{sha[:16]}.xlsm"),
            "SHA-256 de outro arquivo": _registro(self.lead, 10, sha256="f" * 64),
            "SHA-256 em maiúsculas": _registro(self.lead, 11, sha256=sha.upper()),
            "índice zero": _registro(self.lead, 0),
            "mês 13": _registro(self.lead, 12, caminho=f"2026/13/{self.lead}/12-{sha[:16]}.csv"),
            "vazio": _registro(self.lead, 13, bytes=0),
            "acima de 15 MB": _registro(self.lead, 14, bytes=15 * 1024 * 1024 + 1),
            "tamanho booleano": _registro(self.lead, 15, bytes=True),
            "tamanho em texto": _registro(self.lead, 16, bytes="10"),
            "tamanho fracionário": _registro(self.lead, 17, bytes=10.0),
            "nome que não é texto": _registro(self.lead, 18, nome=["x"]),
            "sem caminho": {k: v for k, v in _registro(self.lead, 19).items() if k != "caminho"},
            "não é objeto": ["2026/11/x.csv"],
        }
        for caso, item in ruins.items():
            with self.subTest(caso=caso):
                self.assertEqual(self.planilhas([item]), [])
                self.assertEqual([p.indice for p in self.planilhas([item, bom])], [1])

    def test_texto_que_nao_e_registro(self):
        for texto in ("", "não é json", "{\"a\": 1}", "null", "[[[" * 5000, json.dumps([1, 2, 3]),
                      "[" + "x" * 20001 + "]", None, 42):
            with self.subTest(texto=str(texto)[:20]):
                self.assertEqual(armazenamento_leads.planilhas_do_lead(self.lead, texto), [])
        self.assertEqual(armazenamento_leads.planilhas_do_lead("não é uuid", json.dumps([_registro(self.lead, 1)])), [])

    def test_indice_repetido_teto_de_arquivos_e_de_bytes(self):
        repetido = [_registro(self.lead, 1, b"a"), _registro(self.lead, 1, b"b")]
        self.assertEqual([p.sha256 for p in self.planilhas(repetido)], [hashlib.sha256(b"a").hexdigest()])
        treze = [_registro(self.lead, i, f"conteudo {i}".encode()) for i in range(1, 14)]
        self.assertEqual([p.indice for p in self.planilhas(treze)], list(range(1, 13)))
        # Mais que os 15 MB somados de um envio do microsserviço: o que passa do teto fica de fora.
        grandes = [_registro(self.lead, i, f"g{i}".encode(), bytes=6 * 1024 * 1024) for i in range(1, 4)]
        self.assertEqual([p.indice for p in self.planilhas(grandes)], [1, 2])

    def test_nome_exibido_e_nome_de_baixar(self):
        casos = [
            ("Vendas Janeiro 2026.CSV", "csv", "Vendas Janeiro 2026.CSV", "Vendas_Janeiro_2026.csv"),
            ("relatório çãõ.xlsx", "xlsx", "relatório çãõ.xlsx", "relatorio_cao.xlsx"),
            ("<script>alert(1)</script>.csv", "csv", "<script>alert(1)</script>.csv", "scriptalert1_script.csv"),
            ("..\\..\\segredo.csv", "csv", "..\\..\\segredo.csv", "segredo.csv"),
            ("nome" + chr(0x202E) + "sem" + chr(0) + "controle\n.csv", "csv", "nomesemcontrole.csv",
             "nomesemcontrole.csv"),
            ("....", "csv", "....", "relatorio-03.csv"),
            ("出荷.csv", "csv", "出荷.csv", "relatorio-03.csv"),
            ("   ", "csv", "relatorio-03.csv", "relatorio-03.csv"),
            ("vendas", "xlsx", "vendas", "vendas.xlsx"),
            ("CON.csv", "csv", "CON.csv", "relatorio-03.csv"),
            ("lpt1.backup.csv", "csv", "lpt1.backup.csv", "relatorio-03.csv"),
        ]
        for nome, formato, exibido, para_baixar in casos:
            with self.subTest(nome=nome):
                [planilha] = self.planilhas([_registro(self.lead, 3, formato=formato, nome=nome)])
                self.assertEqual((planilha.nome, planilha.nome_para_baixar), (exibido, para_baixar))
        longo = "x" * 300 + ".csv"
        [planilha] = self.planilhas([_registro(self.lead, 1, nome=longo)])
        self.assertLessEqual(len(planilha.nome), 100)
        self.assertLessEqual(len(planilha.nome_para_baixar), 104)

    def test_nomes_unicos_e_zip(self):
        itens = [_registro(self.lead, 1, b"a", nome="vendas.csv"), _registro(self.lead, 2, b"b", nome="VENDAS.csv"),
                 _registro(self.lead, 3, b"c", nome="vendas.csv"), _registro(self.lead, 4, b"d", nome="vendas-2.csv")]
        planilhas = self.planilhas(itens)
        nomes = armazenamento_leads.nomes_unicos(planilhas)
        self.assertEqual(nomes, ["vendas.csv", "VENDAS-2.csv", "vendas-3.csv", "vendas-2-2.csv"])
        pacote = armazenamento_leads.montar_zip(zip(nomes, (b"a", b"b", b"c", b"d")))
        with zipfile.ZipFile(io.BytesIO(pacote)) as lido:
            self.assertEqual(lido.namelist(), nomes)
            self.assertEqual([lido.read(nome) for nome in nomes], [b"a", b"b", b"c", b"d"])
            self.assertIsNone(lido.testzip())


class EstruturaTests(unittest.TestCase):
    def test_modulo_nao_importa_do_microsservico_nem_cita_as_tabelas_de_lead(self):
        fonte = (ROOT / "armazenamento_leads.py").read_text(encoding="utf-8")
        self.assertNotIn("import armazenamento\n", fonte)
        self.assertNotIn("microservico_raiox.", fonte)
        # Só GET: nada que grave ou apague no bucket.
        for metodo in ('method="POST"', 'method="DELETE"', 'method="PUT"', "x-upsert"):
            self.assertNotIn(metodo, fonte)


if __name__ == "__main__":
    unittest.main()
