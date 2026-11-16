"""Cliente do Storage contra um servidor HTTP local: cabeçalhos, caminho, erros e redirecionamento."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

from armazenamento import ArmazenamentoSupabase, ErroDeArmazenamento

CHAVE = "chave-service-role-de-teste"


class _Servidor:
    def __init__(self):
        self.pedidos = []
        self.resposta = 200
        self.corpo = b'{"Key": "ok"}'
        servidor = self

        class Tratador(BaseHTTPRequestHandler):
            def _atender(self):
                tamanho = int(self.headers.get("Content-Length") or 0)
                corpo = self.rfile.read(tamanho) if tamanho else b""
                # Nomes de cabeçalho não diferenciam maiúsculas (o urllib manda "Apikey", "X-upsert").
                cabecalhos = {nome.lower(): valor for nome, valor in self.headers.items()}
                servidor.pedidos.append((self.command, self.path, cabecalhos, corpo))
                status = servidor.resposta
                if status == "linha malformada":
                    self.wfile.write(b"NADA DISSO\r\n\r\n")
                    return
                if status == "resposta cortada":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                     b"Transfer-Encoding: chunked\r\n\r\n10\r\n{\"Key\": \"o")
                    self.close_connection = True
                    return
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", f"http://127.0.0.1:{servidor.porta}/desvio")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(servidor.corpo)))
                self.end_headers()
                self.wfile.write(servidor.corpo)

            do_POST = do_DELETE = do_GET = _atender

            def log_message(self, *_argumentos):
                pass

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Tratador)
        self.porta = self._http.server_address[1]
        self._linha = threading.Thread(target=self._http.serve_forever, daemon=True)
        self._linha.start()

    def parar(self):
        self._http.shutdown()
        self._http.server_close()


class ArmazenamentoTests(unittest.TestCase):
    def setUp(self):
        self.servidor = _Servidor()
        self.addCleanup(self.servidor.parar)
        self.storage = ArmazenamentoSupabase(f"http://127.0.0.1:{self.servidor.porta}", CHAVE, "leads-raio-x",
                                             timeout=5)

    def test_envio_com_os_cabecalhos_e_o_caminho(self):
        self.storage.enviar("2026/11/abc/01-0123456789abcdef.csv", b"Codigo;Produto\n", "text/csv")
        [(metodo, caminho, cabecalhos, corpo)] = self.servidor.pedidos
        self.assertEqual((metodo, caminho, corpo),
                         ("POST", "/storage/v1/object/leads-raio-x/2026/11/abc/01-0123456789abcdef.csv",
                          b"Codigo;Produto\n"))
        self.assertEqual(cabecalhos["authorization"], f"Bearer {CHAVE}")
        self.assertEqual(cabecalhos["apikey"], CHAVE)
        self.assertEqual((cabecalhos["content-type"], cabecalhos["x-upsert"]), ("text/csv", "false"))

    def test_chave_secreta_nova_vai_so_no_apikey(self):
        # O Supabase lê o Authorization: Bearer como JWT e recusa a chave nova ("Invalid JWT").
        storage = ArmazenamentoSupabase(f"http://127.0.0.1:{self.servidor.porta}", "sb_secret_EXEMPLO_sintetico",
                                        "leads-raio-x", timeout=5)
        storage.enviar("2026/11/abc/01-0123456789abcdef.csv", b"x", "text/csv")
        cabecalhos = self.servidor.pedidos[0][2]
        self.assertEqual(cabecalhos["apikey"], "sb_secret_EXEMPLO_sintetico")
        self.assertNotIn("authorization", cabecalhos)

    def test_apagar(self):
        self.assertTrue(self.storage.apagar("2026/11/abc/01.csv"))
        self.assertEqual(self.servidor.pedidos[0][:2], ("DELETE", "/storage/v1/object/leads-raio-x/2026/11/abc/01.csv"))

    def test_conferir_bucket(self):
        self.servidor.corpo = b'{"id": "leads-raio-x", "public": false}'
        self.assertTrue(self.storage.conferir_bucket())
        self.assertEqual(self.servidor.pedidos[0][:2], ("GET", "/storage/v1/bucket/leads-raio-x"))
        self.servidor.corpo = b'{"id": "leads-raio-x", "public": true}'
        self.assertFalse(self.storage.conferir_bucket())
        for status, corpo in ((200, b"sem json"), (200, b"[1]"), (404, b'{"error": "not_found"}')):
            with self.subTest(status=status, corpo=corpo):
                self.servidor.resposta, self.servidor.corpo = status, corpo
                with self.assertRaises(ErroDeArmazenamento):
                    self.storage.conferir_bucket()

    def test_caminho_com_caracteres_especiais_vai_codificado(self):
        self.storage.enviar("2026/11/a b?c#d.csv", b"x", "text/csv")
        self.assertEqual(self.servidor.pedidos[0][1], "/storage/v1/object/leads-raio-x/2026/11/a%20b%3Fc%23d.csv")

    def test_erro_do_storage_sem_a_chave_na_mensagem(self):
        for status in (400, 401, 409, 500):
            with self.subTest(status=status):
                self.servidor.resposta = status
                with self.assertRaises(ErroDeArmazenamento) as erro:
                    self.storage.enviar("x.csv", b"x", "text/csv")
                self.assertIn(str(status), str(erro.exception))
                self.assertNotIn(CHAVE, str(erro.exception))

    def test_apagar_o_que_nao_existe_nao_e_erro(self):
        for status, corpo in ((404, b'{"error": "not_found"}'),
                              (400, b'{"statusCode": "404", "error": "not_found", "message": "Object not found"}'),
                              (400, b'{"statusCode": "404", "error": "Not found"}')):
            with self.subTest(status=status, corpo=corpo):
                self.servidor.resposta, self.servidor.corpo = status, corpo
                self.assertFalse(self.storage.apagar("2026/11/abc/01.csv"))
        # Só o 404, ou o 400 que diz 404 no corpo, quer dizer "não existe"; o resto é falha de verdade.
        for status, corpo in ((400, b'{"statusCode": "400", "error": "InvalidKey"}'), (400, b"sem json"),
                              (500, b'{"error": "internal"}'), (401, b'{"statusCode": "404"}'),
                              (500, b'{"statusCode": "404", "error": "not_found"}')):
            with self.subTest(status=status, corpo=corpo):
                self.servidor.resposta, self.servidor.corpo = status, corpo
                with self.assertRaises(ErroDeArmazenamento):
                    self.storage.apagar("2026/11/abc/01.csv")

    def test_resposta_cortada_ou_malformada_vira_erro_do_storage(self):
        for modo in ("linha malformada", "resposta cortada"):
            with self.subTest(modo=modo):
                self.servidor.resposta = modo
                with self.assertRaises(ErroDeArmazenamento) as erro:
                    self.storage.enviar("x.csv", b"x", "text/csv")
                self.assertNotIn(CHAVE, str(erro.exception))

    def test_redirecionamento_nao_e_seguido(self):
        self.servidor.resposta = 302
        with self.assertRaises(ErroDeArmazenamento):
            self.storage.enviar("x.csv", b"x", "text/csv")
        self.assertEqual([pedido[1] for pedido in self.servidor.pedidos], ["/storage/v1/object/leads-raio-x/x.csv"])

    def test_servidor_fora_do_ar(self):
        self.servidor.parar()
        storage = ArmazenamentoSupabase("http://127.0.0.1:9", CHAVE, "leads-raio-x", timeout=2)
        with self.assertRaises(ErroDeArmazenamento) as erro:
            storage.enviar("x.csv", b"x", "text/csv")
        self.assertNotIn(CHAVE, str(erro.exception))

    def test_so_https_fora_do_localhost(self):
        for url in ("http://abcdefghijklmnopqrst.supabase.co", "ftp://exemplo.com", "file:///etc/passwd", "",
                    "https://[ValorMarcado]"):
            with self.subTest(url=url), self.assertRaises(ValueError) as erro:
                ArmazenamentoSupabase(url, CHAVE, "leads-raio-x")
            self.assertNotIn("ValorMarcado", str(erro.exception))
        self.assertNotIn(CHAVE, repr(ArmazenamentoSupabase("https://abcdefghijklmnopqrst.supabase.co", CHAVE, "b")))


if __name__ == "__main__":
    unittest.main()
