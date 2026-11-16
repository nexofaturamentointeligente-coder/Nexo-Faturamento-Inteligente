"""As rotas do microsserviço, com o banco e o Storage substituídos por dublês."""

from datetime import datetime, timedelta
import io
import json
import re
import threading
import unittest
import uuid

from app import BRASILIA, SERVICO, FilaDaTriagem, conferir_dependencias, create_app
from configuracao import MEGABYTE
from limite import LimitePorIp
from tests.apoio import (CSV, ORIGEM, TIPO_XLSX, ArmazenamentoFalso, RepositorioFalso, configuracao, folha,
                         xlsx)

ROTA = "/api/leads/raio-x"
CAMINHO_SEGURO = re.compile(r"[0-9]{4}/[0-9]{2}/[0-9a-f-]{36}/[0-9]{2}-[0-9a-f]{16}\.(?:csv|xlsx)")
DOIS_ARQUIVOS = (("a.csv", CSV, "text/csv"), ("b.xlsx", xlsx(), TIPO_XLSX))


class _RotasCase(unittest.TestCase):
    def montar(self, configuracao_do_teste=None, repositorio=None, armazenamento=None, limite=None):
        self.repositorio = repositorio or RepositorioFalso()
        self.armazenamento = armazenamento or ArmazenamentoFalso()
        self.app = create_app(configuracao_do_teste or configuracao(), self.repositorio, self.armazenamento,
                              limite or LimitePorIp(100, 100))
        self.cliente = self.app.test_client()

    def setUp(self):
        self.montar()

    @staticmethod
    def dados(**troca):
        base = {"nome": "Maria Fictícia", "empresa": "Mercado Exemplo", "email": "Maria@Example.invalid",
                "whatsapp": "+55 (62) 98888-1234", "interesse": "1", "termos_aceitos": "true"}
        base.update(troca)
        return {chave: valor for chave, valor in base.items() if valor is not None}

    def enviar(self, arquivos=(("vendas.csv", CSV, "text/csv"),), origem=ORIGEM, cliente=None, **troca):
        dados = self.dados(**troca)
        if arquivos:
            dados["arquivos"] = [(io.BytesIO(conteudo), nome, tipo) for nome, conteudo, tipo in arquivos]
        cabecalhos = {"Origin": origem} if origem else {}
        return (cliente or self.cliente).post(ROTA, data=dados, content_type="multipart/form-data",
                                              headers=cabecalhos)

    def assert_nada_gravado(self):
        self.assertEqual(self.repositorio.gravados, [])
        self.assertEqual(self.armazenamento.objetos, {})


class KeepAliveTests(_RotasCase):
    def test_ping_na_raiz_e_em_ping_sem_banco(self):
        for caminho in ("/", "/ping"):
            with self.subTest(caminho=caminho):
                resposta = self.cliente.get(caminho)
                self.assertEqual((resposta.status_code, resposta.get_json()), (200, SERVICO))
        self.assertEqual(self.cliente.head("/ping").status_code, 200)
        self.assertEqual((self.repositorio.contagens, self.armazenamento.envios), (0, 0))

    def test_ping_responde_mesmo_com_a_configuracao_incompleta(self):
        self.montar(configuracao(problemas=("DATABASE_URL ausente.",)))
        self.assertEqual(self.cliente.get("/ping").get_json(), SERVICO)
        resposta = self.enviar()
        self.assertEqual(resposta.status_code, 503)
        self.assert_nada_gravado()

    def test_banco_mal_configurado_nao_derruba_o_servico(self):
        armazenamento = ArmazenamentoFalso()
        with self.assertLogs("nexo_raiox", "ERROR") as registro:
            app = create_app(configuracao(database_url="isto não é uma conexão"), None, armazenamento,
                             LimitePorIp(100, 100))
        self.assertIn("Banco não configurado", registro.output[0])
        self.assertNotIn("isto não é", "\n".join(registro.output))
        cliente = app.test_client()
        self.assertEqual(cliente.get("/ping").get_json(), SERVICO)
        self.assertEqual(self.enviar(cliente=cliente).status_code, 503)
        self.assertEqual(armazenamento.objetos, {})

    def test_sem_origem_configurada_o_envio_recebe_403_e_a_subida_confere_banco_e_bucket(self):
        # O estado da implantação antes da LGPD: CORS_ORIGENS vazia. O envio para na origem (403), mas o banco e o
        # Storage nascem, para a conferência da subida.
        sem_origem = configuracao(origens=(), problemas=("CORS_ORIGENS vazia: nenhuma landing pode enviar.",))
        with self.assertLogs("nexo_raiox", "ERROR"):
            app = create_app(sem_origem, limite=LimitePorIp(100, 100))
        repositorio, armazenamento = app.extensions["nexo_raiox_dependencias"]
        self.assertEqual((type(repositorio).__name__, type(armazenamento).__name__),
                         ("RepositorioDeLeads", "ArmazenamentoSupabase"))
        self.assertEqual(self.enviar(cliente=app.test_client()).status_code, 403)

    def test_storage_mal_configurado_vai_para_o_log_sem_o_valor(self):
        # O urlsplit recusa o host entre colchetes com uma mensagem que traz o valor; o log não pode levá-lo.
        with self.assertLogs("nexo_raiox", "ERROR") as registro:
            app = create_app(configuracao(supabase_url="https://[SenhaMarcada7Q]"), RepositorioFalso(), None,
                             LimitePorIp(100, 100))
        self.assertNotIn("SenhaMarcada7Q", "\n".join(registro.output))
        self.assertIsNone(app.extensions["nexo_raiox_dependencias"][1])

    def test_conferencia_da_subida_so_registra_o_resultado(self):
        self.montar()
        with self.assertLogs("nexo_raiox", "INFO") as registro:
            conferir_dependencias(self.app)
        self.assertEqual([linha.split(":", 2)[2] for linha in registro.output],
                         ["Conferência da subida: banco ok, com as colunas da migração f2b6d8a4c1e9.",
                          "Conferência da subida: bucket ok, privado."])
        self.montar(repositorio=RepositorioFalso(falhar_ao_contar=True),
                    armazenamento=ArmazenamentoFalso(falhar_no_bucket=True))
        with self.assertLogs("nexo_raiox", "ERROR") as registro:
            conferir_dependencias(self.app)
        texto = "\n".join(registro.output)
        self.assertIn("UndefinedColumn", texto)
        self.assertIn("bucket inacessível (o Storage respondeu 403)", texto)
        self.montar(armazenamento=ArmazenamentoFalso(bucket_publico=True))
        with self.assertLogs("nexo_raiox", "ERROR") as registro:
            conferir_dependencias(self.app)
        self.assertIn("NÃO é privado", registro.output[0])

    def test_cabecalhos_de_seguranca_e_erros_em_json(self):
        resposta = self.cliente.get("/ping")
        for nome, valor in (("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store"),
                            ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
                            ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")):
            self.assertEqual(resposta.headers[nome], valor)
        self.assertEqual(self.cliente.get("/nao-existe").status_code, 404)
        self.assertIn("erro", self.cliente.get("/nao-existe").get_json())
        resposta = self.cliente.get(ROTA)
        self.assertEqual((resposta.status_code, resposta.get_json()), (405, {"erro": "Método não permitido."}))


class CorsTests(_RotasCase):
    def test_preflight_da_landing(self):
        resposta = self.cliente.options(ROTA, headers={"Origin": ORIGEM, "Access-Control-Request-Method": "POST",
                                                       "Access-Control-Request-Headers": "Content-Type"})
        self.assertEqual(resposta.status_code, 204)
        self.assertEqual(resposta.headers["Access-Control-Allow-Origin"], ORIGEM)
        self.assertEqual(resposta.headers["Access-Control-Allow-Methods"], "POST")
        self.assertEqual(resposta.headers["Access-Control-Allow-Headers"], "content-type")
        self.assertIn("Origin", resposta.headers["Vary"])
        self.assertNotIn("Access-Control-Allow-Credentials", resposta.headers)

    def test_preflight_recusa_outra_origem_outro_metodo_e_cabecalho_extra(self):
        for cabecalhos in ({"Origin": "https://outra.exemplo", "Access-Control-Request-Method": "POST"},
                           {"Origin": ORIGEM, "Access-Control-Request-Method": "DELETE"},
                           {"Origin": ORIGEM, "Access-Control-Request-Method": "POST",
                            "Access-Control-Request-Headers": "X-Admin"},
                           {"Origin": ORIGEM.replace("https", "http"), "Access-Control-Request-Method": "POST"},
                           {"Origin": ORIGEM + ".atacante.exemplo", "Access-Control-Request-Method": "POST"}):
            with self.subTest(cabecalhos=cabecalhos):
                resposta = self.cliente.options(ROTA, headers=cabecalhos)
                self.assertEqual(resposta.status_code, 403)
                if cabecalhos["Origin"] != ORIGEM:
                    self.assertNotIn("Access-Control-Allow-Origin", resposta.headers)

    def test_post_de_outra_origem_ou_sem_origem_e_recusado(self):
        for origem in ("https://outra.exemplo", "null", None, "http://localhost:5000"):
            with self.subTest(origem=origem):
                resposta = self.enviar(origem=origem)
                self.assertEqual(resposta.status_code, 403)
                self.assertNotIn("Access-Control-Allow-Origin", resposta.headers)
        self.assert_nada_gravado()

    def test_localhost_so_quando_liberado(self):
        self.montar(configuracao(permitir_localhost=True))
        for origem in ("http://localhost:5000", "http://127.0.0.1:8080", "http://localhost"):
            with self.subTest(origem=origem):
                resposta = self.enviar(origem=origem, email=f"{uuid.uuid4().hex[:6]}@exemplo.com.br")
                self.assertEqual(resposta.status_code, 201, resposta.get_json())
                self.assertEqual(resposta.headers["Access-Control-Allow-Origin"], origem)
        self.assertEqual(self.enviar(origem="http://localhost.atacante.exemplo").status_code, 403)

    def test_resposta_da_landing_leva_a_origem_ate_no_erro(self):
        resposta = self.enviar(termos_aceitos=None)
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.headers["Access-Control-Allow-Origin"], ORIGEM)
        self.assertEqual(resposta.headers["Access-Control-Expose-Headers"], "Retry-After")


class EnvioTests(_RotasCase):
    def test_interesse_1_com_csv_e_xlsx(self):
        planilha = xlsx()
        resposta = self.enviar(arquivos=(("Vendas Jan.csv", CSV, "application/vnd.ms-excel"),
                                         ("pasta/vendas fev.xlsx", planilha, TIPO_XLSX)))
        self.assertEqual(resposta.status_code, 201, resposta.get_json())
        corpo = resposta.get_json()
        [lead] = self.repositorio.gravados
        self.assertEqual(corpo["protocolo"], str(lead["id"]).split("-")[0].upper())
        self.assertEqual((corpo["status"], corpo["interesse"], corpo["status_arquivo"]),
                         ("recebido", "raio_x", "aprovado"))
        self.assertEqual(corpo["arquivos"], [{"nome": "Vendas Jan.csv", "situacao": "aprovado"},
                                             {"nome": "vendas fev.xlsx", "situacao": "aprovado"}])
        self.assertEqual({chave: lead[chave] for chave in ("nome_contato", "nome_loja", "email", "whatsapp",
                                                           "interesse", "status_arquivo", "termos_versao")},
                         {"nome_contato": "Maria Fictícia", "nome_loja": "Mercado Exemplo",
                          "email": "maria@example.invalid", "whatsapp": "62988881234", "interesse": "raio_x",
                          "status_arquivo": "aprovado", "termos_versao": "termos-teste-v1"})
        registros = json.loads(lead["arquivos_storage"])
        self.assertEqual([registro["nome"] for registro in registros], ["Vendas Jan.csv", "vendas fev.xlsx"])
        for registro, conteudo in zip(registros, (CSV, planilha)):
            self.assertRegex(registro["caminho"], CAMINHO_SEGURO)
            self.assertIn(str(lead["id"]), registro["caminho"])
            self.assertEqual(self.armazenamento.objetos[registro["caminho"]][0], conteudo)
            self.assertEqual(registro["bytes"], len(conteudo))
        self.assertEqual([tipo for _conteudo, tipo in self.armazenamento.objetos.values()],
                         ["text/csv", TIPO_XLSX])
        # O teto do dia vai junto com a gravação, contado desde a 0h de Brasília.
        [pedido] = self.repositorio.pedidos_de_gravacao
        agora = datetime.now(BRASILIA)
        self.assertEqual(pedido["teto"], 100)
        self.assertEqual((pedido["desde"].utcoffset(), pedido["desde"].time().isoformat()),
                         (timedelta(hours=-3), "00:00:00"))
        self.assertIn(pedido["desde"].date(), (agora.date(), (agora - timedelta(minutes=1)).date()))

    def test_interesses_2_e_3_sem_arquivo(self):
        for numero, codigo in (("2", "ajuda_relatorios"), ("3", "so_contato")):
            with self.subTest(interesse=numero):
                resposta = self.enviar(arquivos=(), interesse=numero)
                self.assertEqual(resposta.status_code, 201)
                self.assertEqual((resposta.get_json()["interesse"], resposta.get_json()["status_arquivo"],
                                  resposta.get_json()["arquivos"]), (codigo, "sem_arquivo", []))
        self.assertEqual([(lead["interesse"], lead["arquivos_storage"]) for lead in self.repositorio.gravados],
                         [("ajuda_relatorios", None), ("so_contato", None)])
        self.assertEqual(self.armazenamento.objetos, {})

    def test_arquivo_so_no_interesse_1_e_obrigatorio_nele(self):
        resposta = self.enviar(interesse="2")
        self.assertEqual((resposta.status_code, resposta.get_json()["campo"]), (400, "arquivos"))
        resposta = self.enviar(arquivos=(), interesse="1")
        self.assertEqual((resposta.status_code, resposta.get_json()["campo"]), (400, "arquivos"))
        self.assert_nada_gravado()

    def test_parcial_e_recusado(self):
        resposta = self.enviar(arquivos=(("vendas.csv", CSV, "text/csv"),
                                         ("macro.xlsm", xlsx(), "application/vnd.ms-excel.sheet.macroEnabled.12")))
        corpo = resposta.get_json()
        self.assertEqual((resposta.status_code, corpo["status_arquivo"]), (201, "parcial"))
        self.assertEqual(corpo["arquivos"][1]["situacao"], "recusado")
        self.assertIn(".xlsm", corpo["arquivos"][1]["motivo"])
        self.assertEqual(len(self.armazenamento.objetos), 1)
        resposta = self.enviar(arquivos=(("macro.xlsm", xlsx(), TIPO_XLSX), ("relatorio.pdf", b"%PDF-1.4", "application/pdf")),
                               email="outra@exemplo.com.br")
        self.assertEqual((resposta.status_code, resposta.get_json()["status_arquivo"]), (201, "recusado"))
        self.assertEqual(self.repositorio.gravados[-1]["arquivos_storage"], None)
        self.assertEqual(len(self.armazenamento.objetos), 1)

    def test_xlsx_corrompido_recusa_so_ele(self):
        planilha = bytearray(xlsx(planilha=folha('<c r="A1"><v>1</v></c>' * 400)))
        inicio = planilha.index(b"xl/worksheets/sheet1.xml") + len(b"xl/worksheets/sheet1.xml")
        planilha[inicio + 10] ^= 0xFF  # um byte trocado dentro do fluxo comprimido da aba
        resposta = self.enviar(arquivos=(("vendas.csv", CSV, "text/csv"), ("vendas.xlsx", bytes(planilha), TIPO_XLSX)))
        corpo = resposta.get_json()
        self.assertEqual((resposta.status_code, corpo["status_arquivo"]), (201, "parcial"), corpo)
        self.assertEqual(corpo["arquivos"][1]["situacao"], "recusado")

    def test_triagem_fora_do_prazo_recusa_o_relatorio_e_grava_o_lead(self):
        self.montar(configuracao(prazo_da_triagem=-1))  # o prazo já começa vencido
        resposta = self.enviar(arquivos=DOIS_ARQUIVOS)
        corpo = resposta.get_json()
        self.assertEqual((resposta.status_code, corpo["status_arquivo"]), (201, "recusado"), corpo)
        self.assertTrue(all(arquivo["motivo"].startswith("O arquivo é grande demais para conferir agora")
                            for arquivo in corpo["arquivos"]), corpo)
        self.assertEqual(self.armazenamento.objetos, {})

    def test_termos_precisam_vir_aceitos(self):
        for valor in (None, "", "false", "0", "nao", "off"):
            with self.subTest(termos=valor):
                resposta = self.enviar(termos_aceitos=valor)
                self.assertEqual((resposta.status_code, resposta.get_json()["campo"]), (400, "termos_aceitos"))
        self.assert_nada_gravado()
        for valor in ("true", "on", "1", "Sim", "TRUE"):
            with self.subTest(termos=valor):
                self.assertEqual(self.enviar(termos_aceitos=valor).status_code, 201)

    def test_campos_invalidos(self):
        casos = {
            "nome": {"nome": "   "}, "empresa": {"empresa": None}, "email": {"email": "sem-arroba"},
            "whatsapp": {"whatsapp": "(55) 99276-54321"}, "interesse": {"interesse": "4"},
        }
        for campo, troca in casos.items():
            with self.subTest(campo=campo):
                resposta = self.enviar(**troca)
                self.assertEqual((resposta.status_code, resposta.get_json()["campo"]), (400, campo))
        self.assertEqual(self.enviar(email=None).get_json()["campo"], "email")
        self.assertEqual(self.enviar(nome="x" * 121).status_code, 400)
        self.assert_nada_gravado()

    def test_campo_isca_formato_e_campo_de_arquivo_desconhecido(self):
        self.assertEqual(self.enviar(campo_isca="http://spam.exemplo").status_code, 400)
        resposta = self.cliente.post(ROTA, json=self.dados(), headers={"Origin": ORIGEM})
        self.assertEqual(resposta.status_code, 415)
        dados = {**self.dados(), "outro": (io.BytesIO(CSV), "vendas.csv", "text/csv")}
        resposta = self.cliente.post(ROTA, data=dados, content_type="multipart/form-data", headers={"Origin": ORIGEM})
        self.assertEqual((resposta.status_code, resposta.get_json()["campo"]), (400, "arquivos"))
        self.assert_nada_gravado()

    def test_quantidade_de_arquivos(self):
        for quantidade in (13, 30, 49):
            with self.subTest(quantidade=quantidade):
                arquivos = tuple((f"mes{indice}.csv", CSV, "text/csv") for indice in range(quantidade))
                resposta = self.enviar(arquivos=arquivos)
                self.assertEqual((resposta.status_code, resposta.get_json()["erro"]),
                                 (400, "Envie no máximo 12 relatórios por vez."))
        # Muito acima: o Werkzeug para de ler o formulário, com a mensagem que vale para os dois limites.
        arquivos = tuple((f"mes{indice}.csv", CSV, "text/csv") for indice in range(80))
        resposta = self.enviar(arquivos=arquivos)
        self.assertEqual((resposta.status_code, resposta.get_json()["erro"]),
                         (413, "O envio passa do permitido: até 15 MB somados e 12 relatórios por vez."))
        self.assert_nada_gravado()

    def test_tamanho_dos_arquivos(self):
        self.montar(configuracao(max_bytes=MEGABYTE))
        # Acima do teto do pedido (1 MB de relatórios mais 64 KB de folga para os campos): o Werkzeug recusa.
        grande = ("grande.csv", CSV + b"1;x;1;1\n" * ((MEGABYTE + 100 * 1024) // 8), "text/csv")
        resposta = self.enviar(arquivos=(grande,))
        self.assertEqual((resposta.status_code, resposta.get_json()["erro"]),
                         (413, "O envio passa do permitido: até 1 MB somados e 12 relatórios por vez."))
        meio = CSV + b"1;x;1;1\n" * (520 * 1024 // 8)
        resposta = self.enviar(arquivos=(("a.csv", meio, "text/csv"), ("b.csv", meio, "text/csv")))
        self.assertEqual((resposta.status_code, resposta.get_json()["erro"]),
                         (413, "Os relatórios passam de 1 MB somados."))
        self.assert_nada_gravado()


class LimitesTests(_RotasCase):
    def test_limite_por_ip(self):
        self.montar(limite=LimitePorIp(2, 10))
        status = [self.enviar(email=f"{indice}@exemplo.com.br").status_code for indice in range(3)]
        self.assertEqual(status, [201, 201, 429])
        resposta = self.enviar()
        self.assertGreater(int(resposta.headers["Retry-After"]), 0)
        # A landing, em outra origem, só lê o Retry-After se ele estiver exposto.
        self.assertEqual(resposta.headers["Access-Control-Expose-Headers"], "Retry-After")
        outro = self.app.test_client()
        outro.environ_base["REMOTE_ADDR"] = "203.0.113.9"
        self.assertEqual(self.enviar(cliente=outro).status_code, 201)

    def test_limite_por_ip_conta_o_ipv6_pela_rede_64(self):
        self.montar(limite=LimitePorIp(2, 10))
        status = []
        for final in range(1, 5):
            cliente = self.app.test_client()
            cliente.environ_base["REMOTE_ADDR"] = f"2001:db8:1:2::{final}"
            status.append(self.enviar(cliente=cliente, email=f"{final}@exemplo.com.br").status_code)
        self.assertEqual(status, [201, 201, 429, 429])
        vizinho = self.app.test_client()
        vizinho.environ_base["REMOTE_ADDR"] = "2001:db8:1:3::1"
        self.assertEqual(self.enviar(cliente=vizinho).status_code, 201)

    def test_teto_diario_do_servico(self):
        self.montar(configuracao(teto_diario=3), repositorio=RepositorioFalso(hoje=3))
        resposta = self.enviar()
        self.assertEqual(resposta.status_code, 429)
        self.assertEqual(self.repositorio.pedidos_de_gravacao, [])
        self.assert_nada_gravado()

    def test_teto_atingido_na_gravacao_desfaz_os_relatorios(self):
        # Outros envios fecharam o teto entre a primeira contagem e a gravação.
        self.montar(configuracao(teto_diario=3), repositorio=RepositorioFalso(hoje=2, leads_que_chegam_antes=1))
        resposta = self.enviar(arquivos=DOIS_ARQUIVOS)
        self.assertEqual((resposta.status_code, resposta.get_json()["erro"]),
                         (429, "O limite de envios de hoje foi atingido. Tente de novo amanhã."))
        self.assertEqual(len(self.armazenamento.apagados), 2)
        self.assert_nada_gravado()

    def test_banco_fora_do_ar_no_teto(self):
        self.montar(repositorio=RepositorioFalso(falhar_ao_contar=True))
        self.assertEqual(self.enviar().status_code, 503)
        self.assert_nada_gravado()

    def ocupar_a_fila(self, quantos):
        fila = self.app.extensions["nexo_raiox_fila"]
        senhas = [fila.entrar() for _ in range(quantos)]
        self.addCleanup(lambda: [fila.sair(senha) for senha in senhas if senha is not None])
        return senhas

    def test_a_vez_que_nao_chega_responde_503_com_retry_after(self):
        self.ocupar_a_fila(1)  # uma triagem em curso, que não termina
        self.app.config["NEXO_ESPERA_PELA_TRIAGEM"] = 0.01
        with self.assertLogs("nexo_raiox", "WARNING") as registro:
            resposta = self.enviar()
        self.assertEqual((resposta.status_code, resposta.headers["Retry-After"]), (503, "30"))
        self.assertIn("não chegou a tempo", registro.output[0])
        self.assert_nada_gravado()
        self.assertEqual(len(self.app.extensions["nexo_raiox_fila"]), 1)  # a senha do envio saiu da fila

    def test_fila_da_triagem_cheia_responde_503_na_hora(self):
        # Uma triagem em curso e duas na fila: o próximo envio não prende mais uma thread esperando.
        senhas = self.ocupar_a_fila(4)
        self.assertEqual(sum(senha is not None for senha in senhas), 3)
        with self.assertLogs("nexo_raiox", "WARNING") as registro:
            resposta = self.enviar()
        self.assertEqual((resposta.status_code, resposta.headers["Retry-After"]), (503, "30"))
        self.assertIn("fila da triagem cheia", registro.output[0])
        self.assert_nada_gravado()
        # A espera pela vez cobre a triagem em curso e a da frente, cada uma no seu prazo, com folga.
        self.assertEqual(self.app.config["NEXO_ESPERA_PELA_TRIAGEM"], 2 * 30 + 10)

    def test_envio_sem_relatorio_nao_entra_na_fila(self):
        # O contato dos interesses 2 e 3 não tem o que triar: não espera a triagem dos outros nem gasta a vaga.
        self.ocupar_a_fila(3)
        for interesse in ("2", "3"):
            with self.subTest(interesse=interesse):
                resposta = self.enviar(arquivos=(), interesse=interesse, email=f"i{interesse}@exemplo.com.br")
                self.assertEqual(resposta.status_code, 201)
        self.assertEqual(len(self.repositorio.gravados), 2)

    def test_fila_atende_em_ordem_de_chegada(self):
        fila = FilaDaTriagem(3)
        primeira, segunda, terceira = fila.entrar(), fila.entrar(), fila.entrar()
        self.assertIsNone(fila.entrar())
        ordem = []

        def esperar(senha, rotulo):
            if fila.esperar_a_vez(senha, 10):
                ordem.append(rotulo)
                fila.sair(senha)

        fios = [threading.Thread(target=esperar, args=(terceira, "terceira")),
                threading.Thread(target=esperar, args=(segunda, "segunda"))]
        for fio in fios:
            fio.start()
        self.assertTrue(fila.esperar_a_vez(primeira, 0))
        fila.sair(primeira)
        for fio in fios:
            fio.join(10)
        self.assertEqual(ordem, ["segunda", "terceira"])
        self.assertEqual(len(fila), 0)

    def test_limite_por_ip_atras_do_cloudflare_e_do_render(self):
        # No Render, o X-Forwarded-For chega como "visitante, borda do Cloudflare", e o REMOTE_ADDR é o proxy do
        # Render. Com 2 proxies confiáveis, cada visitante conta pelo próprio IP, e o IP forjado à frente não vale.
        self.montar(configuracao(proxies_confiaveis=2), limite=LimitePorIp(2, 10))
        cliente = self.app.test_client()
        cliente.environ_base["REMOTE_ADDR"] = "10.21.157.68"
        status = [cliente.post(ROTA, data={**self.dados(email=f"{indice}@exemplo.com.br", interesse="3")},
                               content_type="multipart/form-data",
                               headers={"Origin": ORIGEM, "X-Forwarded-For": f"177.{indice}.10.1, 104.22.17.40"}
                               ).status_code for indice in range(5)]
        self.assertEqual(status, [201] * 5)
        forjado = [cliente.post(ROTA, data={**self.dados(email=f"f{indice}@exemplo.com.br", interesse="3")},
                                content_type="multipart/form-data",
                                headers={"Origin": ORIGEM, "X-Forwarded-For": f"6.6.6.{indice}, 177.99.10.99, "
                                                                              "104.22.17.40"}).status_code
                   for indice in range(3)]
        self.assertEqual(forjado, [201, 201, 429])

    def test_contagem_do_x_forwarded_for_vai_para_o_log_sem_os_ips(self):
        with self.assertLogs("nexo_raiox", "INFO") as registro:
            for cabecalho in ("177.1.10.1, 104.22.17.40", "177.2.10.2, 104.22.17.41", "", "1.1.1.1"):
                self.cliente.get("/ping", headers={"X-Forwarded-For": cabecalho} if cabecalho else {})
        contagens = [linha for linha in registro.output if "X-Forwarded-For" in linha]
        self.assertEqual(len(contagens), 3)  # 2, 0 e 1 entradas, cada contagem uma vez
        self.assertIn("X-Forwarded-For com 2 entrada(s); PROXIES_CONFIAVEIS=0.", contagens[0])
        texto = "\n".join(registro.output)
        for ip in ("177.1.10.1", "104.22.17.40", "1.1.1.1"):
            self.assertNotIn(ip, texto)

    def test_cliente_nao_gasta_as_contagens_do_x_forwarded_for(self):
        # Um cliente que manda de 3 a 3000 entradas antes do operador não esconde a contagem dele: acima de 6, todas
        # contam como "7 ou mais", e o 2 do operador (e o 0 do health check) ainda aparecem.
        with self.assertLogs("nexo_raiox", "INFO") as registro:
            for entradas in (3, 4, 5, 6, 7, 8, 9, 10, 11, 50, 3000):
                self.cliente.get("/ping", headers={"X-Forwarded-For": ", ".join(["9.9.9.9"] * entradas)})
            self.cliente.get("/ping", headers={"X-Forwarded-For": "177.1.10.1, 104.22.17.40"})
            self.cliente.get("/ping")
        contagens = [linha.split("com ", 1)[1].split(" entrada", 1)[0] for linha in registro.output
                     if "X-Forwarded-For" in linha]
        self.assertEqual(sorted(contagens), sorted(["3", "4", "5", "6", "7 ou mais", "2", "0"]))


class FalhasTests(_RotasCase):
    def test_storage_falha_no_meio_e_o_envio_e_desfeito(self):
        self.montar(armazenamento=ArmazenamentoFalso(falhar_no_envio=2))
        with self.assertLogs("nexo_raiox", "WARNING") as registro:
            resposta = self.enviar(arquivos=DOIS_ARQUIVOS)
        self.assertEqual(resposta.status_code, 503)
        # O primeiro e o que estava a caminho (que pode ter sido gravado sem a resposta chegar).
        self.assertEqual(len(self.armazenamento.apagados), 2)
        self.assert_nada_gravado()
        # O que estava a caminho não foi achado para apagar: o Storage pode terminar de gravá-lo depois do DELETE
        # (prazo esgotado, 502 do gateway), e o caminho vai para o log.
        [aviso] = [linha for linha in registro.output if "a caminho" in linha]
        self.assertIn(self.armazenamento.apagados[1], aviso)
        self.assertNotIn(self.armazenamento.apagados[0], aviso)

    def test_envio_que_grava_e_perde_a_resposta_tambem_e_apagado(self):
        self.montar(armazenamento=ArmazenamentoFalso(gravar_e_falhar_no_envio=2))
        resposta = self.enviar(arquivos=DOIS_ARQUIVOS)
        self.assertEqual(resposta.status_code, 503)
        self.assertEqual(len(self.armazenamento.apagados), 2)
        self.assert_nada_gravado()

    def test_erro_inesperado_no_meio_tambem_desfaz(self):
        self.montar(armazenamento=ArmazenamentoFalso(falhar_no_envio=2, erro_no_envio=RuntimeError("inesperado")))
        with self.assertLogs("nexo_raiox", "ERROR") as registro:
            resposta = self.enviar(arquivos=DOIS_ARQUIVOS)
        self.assertEqual((resposta.status_code, resposta.get_json()), (500, {"erro": "Erro interno."}))
        self.assertIn("RuntimeError", "\n".join(registro.output))
        self.assertEqual(len(self.armazenamento.apagados), 2)
        self.assert_nada_gravado()

    def test_banco_falha_e_os_relatorios_saem_do_storage(self):
        self.montar(repositorio=RepositorioFalso(falhar_ao_gravar=True))
        with self.assertLogs("nexo_raiox", "WARNING"):
            resposta = self.enviar(arquivos=DOIS_ARQUIVOS)
        self.assertEqual(resposta.status_code, 503)
        self.assertEqual(len(self.armazenamento.apagados), 2)
        self.assertEqual(self.repositorio.conferencias, 1)
        self.assert_nada_gravado()

    def test_commit_aplicado_com_a_resposta_perdida_vale_como_gravado(self):
        self.montar(repositorio=RepositorioFalso(gravar_e_falhar=True))
        with self.assertLogs("nexo_raiox", "WARNING") as registro:
            resposta = self.enviar(arquivos=DOIS_ARQUIVOS)
        self.assertEqual(resposta.status_code, 201, resposta.get_json())
        self.assertIn("confirmado", "\n".join(registro.output))
        self.assertEqual(len(self.repositorio.gravados), 1)
        self.assertEqual((self.armazenamento.apagados, len(self.armazenamento.objetos)), ([], 2))

    def test_sem_confirmacao_do_banco_os_relatorios_ficam_e_vao_para_o_log(self):
        self.montar(repositorio=RepositorioFalso(falhar_ao_gravar=True, falhar_ao_conferir=True))
        with self.assertLogs("nexo_raiox", "ERROR") as registro:
            resposta = self.enviar(arquivos=DOIS_ARQUIVOS)
        self.assertEqual(resposta.status_code, 503)
        self.assertEqual(self.armazenamento.apagados, [])
        texto = "\n".join(registro.output)
        for caminho in self.armazenamento.objetos:
            self.assertIn(caminho, texto)

    def test_orfao_que_nao_sai_vai_para_o_log(self):
        self.montar(repositorio=RepositorioFalso(falhar_ao_gravar=True),
                    armazenamento=ArmazenamentoFalso(falhar_ao_apagar=True))
        with self.assertLogs("nexo_raiox", "ERROR") as registro:
            self.assertEqual(self.enviar().status_code, 503)
        self.assertIn("órfão", registro.output[0])

    def test_log_sem_dado_pessoal(self):
        with self.assertLogs("nexo_raiox", "INFO") as registro:
            self.assertEqual(self.enviar().status_code, 201)
            self.montar(repositorio=RepositorioFalso(falhar_ao_gravar=True, falhar_ao_conferir=True))
            self.assertEqual(self.enviar().status_code, 503)
        texto = "\n".join(registro.output)
        for dado in ("Maria", "maria@example.invalid", "98888", "Mercado", "chave-de-teste"):
            self.assertNotIn(dado, texto)

    def test_xlsx_com_formula_perigosa_e_recusado_sem_gravar_o_arquivo(self):
        perigosa = xlsx(planilha=folha('<c r="A1"><f>HYPERLINK("http://atacante.exemplo","clique")</f></c>'))
        resposta = self.enviar(arquivos=(("vendas.xlsx", perigosa, TIPO_XLSX),))
        self.assertEqual(resposta.get_json()["status_arquivo"], "recusado")
        self.assertEqual(self.armazenamento.objetos, {})


if __name__ == "__main__":
    unittest.main()
