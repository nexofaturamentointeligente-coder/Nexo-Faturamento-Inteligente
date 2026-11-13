"""Testes do whatsapp_service.py (camada ativa do WhatsApp pela Evolution API).

Cobrem a higienização do telefone, os textos das mensagens automáticas, o POST (contra um abridor
falso e contra um servidor HTTP local de verdade), o fail-safe — nenhum método levanta exceção e
cada falha vira um logger.warning sem a chave, o telefone inteiro ou o texto — e a configuração lida
do app ou do ambiente. Nenhum teste fala com a internet.
"""
import http.client
import io
import json
import logging
import os
import socket
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from flask import Flask

import whatsapp_service
from whatsapp_service import WhatsAppNotificador

CHAVE = "chave-secreta-de-teste-0001"
URL = "https://evolution.example.invalid"
INSTANCIA = "nexo principal/1"
TELEFONE = "(62) 99999-8877"
NUMERO = "5562999998877"
VARIAVEIS_VAZIAS = {"EVOLUTION_API_URL": "", "EVOLUTION_API_KEY": "", "EVOLUTION_INSTANCE_NAME": ""}


class _Resposta:
    def __init__(self, status=201):
        self.status = status

    def read(self, *_):
        return b'{"key": {"id": "ABC"}, "status": "PENDING"}'

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Abridor:
    """No lugar de whatsapp_service._abrir: guarda cada pedido e responde o que o teste mandar."""

    def __init__(self, resposta=None, erro=None, espera=0.0):
        self.pedidos = []
        self.resposta = resposta or _Resposta()
        self.erro = erro
        self.espera = espera

    def __call__(self, pedido, timeout):
        self.pedidos.append((pedido, timeout))
        if self.espera:
            time.sleep(self.espera)
        if self.erro is not None:
            raise self.erro
        return self.resposta


def _erro_http(codigo):
    # O corpo da recusa traz número e texto: nada disso pode ir para o log.
    corpo = io.BytesIO(json.dumps({"number": NUMERO, "text": "texto-que-nao-vai-para-o-log"}).encode())
    return urllib.error.HTTPError(URL + "/message/sendText/x", codigo, "erro", {}, corpo)


class _ComLog(unittest.TestCase):
    def setUp(self):
        # O fileConfig do Alembic (nas migrações de outros testes) desliga loggers já criados.
        patcher = patch.object(whatsapp_service.logger, "disabled", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_log_sem_segredo(self, registros):
        texto = "\n".join(registros)
        for segredo in (CHAVE, NUMERO, "texto-que-nao-vai-para-o-log", "evolution.example.invalid", "Olá"):
            self.assertNotIn(segredo, texto)


# ---------------------------------------------------------------------------
# Higienização e formatação
# ---------------------------------------------------------------------------
class HigienizarTelefoneTests(unittest.TestCase):
    def test_formatos_brasileiros_viram_so_digitos_com_55(self):
        casos = {
            "(62) 99999-8877": NUMERO, "62999998877": NUMERO, "+55 62 99999-8877": NUMERO,
            "+5562999998877": NUMERO, "5562999998877": NUMERO, "0055 62 99999-8877": NUMERO,
            "062 99999-8877": NUMERO, "62 3222-1234": "556232221234", "(11) 90000-0000": "5511900000000",
            "6299998877": "556299998877", 62999998877: NUMERO, "+55 (062) 99999-8877": NUMERO,
            "+55 0 62 99999-8877": NUMERO, "0055 (62) 99999-8877": NUMERO, "55 62 99999-8877": NUMERO,
            "(062) 99999-8877": NUMERO, "62.99999.8877": NUMERO, "(62)999998877": NUMERO,
        }
        for entrada, esperado in casos.items():
            with self.subTest(entrada=entrada):
                self.assertEqual(whatsapp_service.higienizar_telefone(entrada), esperado)

    def test_o_tamanho_decide_e_nao_o_comeco(self):
        # DDD 55 (RS) sem o DDI continua nacional: ganha o 55 na frente.
        self.assertEqual(whatsapp_service.higienizar_telefone("(55) 99999-8877"), "5555999998877")
        self.assertEqual(whatsapp_service.higienizar_telefone("55 3222-1234"), "555532221234")
        self.assertEqual(whatsapp_service.higienizar_telefone("5555999998877"), "5555999998877")

    def test_numero_invalido_nao_e_enviado(self):
        for entrada in ("123", "62 9999-887", "(20) 99999-8877", "(62) 89999-8877", "1 62 99999-8877",
                        "62 99999-8877 ramal 12", "+1 202 555 0100", "44 20 7946 0958", "", "   ", "abc",
                        "5562999998877000", "+", "++55 62 99999-8877", "55+62 99999-8877"):
            with self.subTest(entrada=entrada):
                self.assertIsNone(whatsapp_service.higienizar_telefone(entrada))

    def test_na_duvida_o_aviso_nao_vai_para_outra_pessoa(self):
        # Jogar fora o que não é dígito e decidir só pelo tamanho mandaria o aviso a um terceiro.
        for entrada in (
            "(55) 3222-1234 ramal 21",        # viraria 553222123421, um fixo do DDD 32
            "(55) 99999-8877 ramal 12",       # viraria 5599999887712, um celular do DDD 99
            "(55) 3222-1234 21",              # ramal sem a palavra: grupo final curto
            "62 3222-1234 123",
            "+55 99999-8877", "0055 99999-8877",  # DDI sem DDD: não é o DDD 55
            "+7 991 123-45-67", "+7 9911234567",  # Rússia: viraria um celular do DDD 79
            "+51 987 654 321", "+66 81 234 5678", "+47 912 34 567",  # Peru, Tailândia, Noruega
            "62 3222-1234 / 62 99999-8877", "Tel: (62) 99999-8877", "62 99999-8877, 62 3222-1234",
        ):
            with self.subTest(entrada=entrada):
                self.assertIsNone(whatsapp_service.higienizar_telefone(entrada))

    def test_numero_de_exemplo_nao_e_telefone(self):
        for entrada in ("(62) 99999-9999", "5562999999999", "+55 11 99999-9999", "62 3333-3333"):
            with self.subTest(entrada=entrada):
                self.assertIsNone(whatsapp_service.higienizar_telefone(entrada))
        self.assertEqual(whatsapp_service.higienizar_telefone("(62) 99999-9998"), "5562999999998")

    def test_valor_que_nao_e_texto_nem_numero(self):
        for entrada in (None, True, False, 62.5, ["62999998877"], b"62999998877", Decimal("62999998877")):
            with self.subTest(entrada=entrada):
                self.assertIsNone(whatsapp_service.higienizar_telefone(entrada))

    def test_mascara_so_os_quatro_ultimos_digitos(self):
        self.assertEqual(whatsapp_service.mascarar_telefone(NUMERO), "***8877")
        self.assertEqual(whatsapp_service.mascarar_telefone("12"), "***")
        self.assertEqual(whatsapp_service.mascarar_telefone(None), "***")


class FormatacaoTests(unittest.TestCase):
    def test_reais_com_milhar_e_centavos_meio_para_cima(self):
        casos = {Decimal("1234.5"): "R$ 1.234,50", "12345.675": "R$ 12.345,68", 0.005: "R$ 0,01",
                 1000000: "R$ 1.000.000,00", Decimal("-1234.567"): "-R$ 1.234,57", 0: "R$ 0,00"}
        for valor, esperado in casos.items():
            with self.subTest(valor=valor):
                self.assertEqual(whatsapp_service.formatar_reais(valor), esperado)

    def test_reais_de_valor_que_nao_e_numero_finito(self):
        for valor in (None, True, "abc", float("nan"), float("inf"), Decimal("NaN"), [1], Decimal("1e40")):
            with self.subTest(valor=valor):
                self.assertIsNone(whatsapp_service.formatar_reais(valor))

    def test_mes_por_extenso(self):
        for referencia in ("2026-10", "2026-10-15", "10/2026", date(2026, 10, 1), datetime(2026, 10, 31, 23, 0)):
            with self.subTest(referencia=referencia):
                self.assertEqual(whatsapp_service.mes_por_extenso(referencia), "outubro de 2026")
        self.assertEqual(whatsapp_service.mes_por_extenso("2026-03"), "março de 2026")
        for referencia in ("2026-13", "2026-00", "13/2026", "abc", "", None, 202610, "1999-01"):
            with self.subTest(referencia=referencia):
                self.assertIsNone(whatsapp_service.mes_por_extenso(referencia))

    def test_texto_seguro_tira_controle_quebra_e_trunca(self):
        self.assertEqual(whatsapp_service.texto_seguro("Ana\n\nSouza\t\x00 Lima​", 60), "Ana Souza Lima")
        longo = whatsapp_service.texto_seguro("x" * 200, 60)
        self.assertEqual(len(longo), 60)
        self.assertTrue(longo.endswith("…"))
        self.assertEqual(whatsapp_service.texto_seguro(None, 10), "")
        self.assertEqual(whatsapp_service.texto_seguro(True, 10), "")

    def test_rotulo_dos_status_do_chamado(self):
        self.assertEqual(whatsapp_service.rotulo_status("ABERTO"), "Aberto")
        self.assertEqual(whatsapp_service.rotulo_status("EM_ATENDIMENTO"), "Em atendimento")
        self.assertEqual(whatsapp_service.rotulo_status("em atendimento"), "Em atendimento")
        self.assertEqual(whatsapp_service.rotulo_status("RESOLVIDO"), "Resolvido")
        self.assertEqual(whatsapp_service.rotulo_status("Aguardando\npeças"), "Aguardando peças")


class TextosTests(unittest.TestCase):
    def test_relatorio_pronto_com_valor(self):
        self.assertEqual(
            whatsapp_service.texto_relatorio_pronto("Maria Souza", "Mercado Bom Preço", Decimal("12345.67")),
            "Olá, Maria Souza! O relatório de Mercado Bom Preço está pronto no NEXO Faturamento Inteligente. "
            "Encontramos R$ 12.345,67 em potencial recuperável. Acesse o portal para ver os detalhes.")

    def test_relatorio_pronto_sem_valor_positivo_nao_cita_valor(self):
        for valor in (None, 0, Decimal("0.00"), Decimal("0.004"), -10, "abc", float("nan"), float("inf")):
            with self.subTest(valor=valor):
                texto = whatsapp_service.texto_relatorio_pronto("Ana", "Loja", valor)
                self.assertNotIn("Encontramos", texto)
                self.assertIn("O relatório de Loja está pronto", texto)

    def test_relatorio_pronto_sem_acesso_ao_portal(self):
        texto = whatsapp_service.texto_relatorio_pronto("Ana", "Loja X", 10, acesso_ao_portal=False)
        self.assertTrue(texto.endswith("A equipe NEXO vai entrar em contato para apresentar os resultados."))
        self.assertNotIn("portal", texto)

    def test_relatorio_pronto_sem_nome_e_sem_empresa(self):
        self.assertEqual(whatsapp_service.texto_relatorio_pronto(None, "  ", None),
                         "Olá! O seu relatório está pronto no NEXO Faturamento Inteligente. "
                         "Acesse o portal para ver os detalhes.")

    def test_abertura_mensal(self):
        self.assertEqual(
            whatsapp_service.texto_abertura_mensal("Mercado Bom Preço", "2026-10"),
            "Olá, Mercado Bom Preço! O envio dos relatórios de outubro de 2026 está aberto no NEXO "
            "Faturamento Inteligente. Envie os arquivos pelo portal para receber a análise do mês.")
        self.assertIn("O envio dos relatórios do mês está aberto", whatsapp_service.texto_abertura_mensal("Ana", "2026-13"))
        self.assertIn("O envio dos relatórios da 2ª quinzena de outubro de 2026 está aberto",
                      whatsapp_service.texto_abertura_mensal("Ana", "2026-10", quinzena=2))
        for quinzena in (None, 0, 3, True, "1"):
            with self.subTest(quinzena=quinzena):
                self.assertIn("O envio dos relatórios de outubro de 2026 está aberto",
                              whatsapp_service.texto_abertura_mensal("Ana", "2026-10", quinzena=quinzena))

    def test_chamado_atualizado(self):
        self.assertEqual(
            whatsapp_service.texto_atualizacao_ticket("João", 12, "EM_ATENDIMENTO"),
            "Olá, João! O seu chamado #12 no NEXO Faturamento Inteligente foi atualizado. "
            "Situação atual: Em atendimento. Acesse o portal para ver os detalhes.")
        self.assertIn("chamado #12 no", whatsapp_service.texto_atualizacao_ticket("João", "#12", "RESOLVIDO"))
        sem_numero = whatsapp_service.texto_atualizacao_ticket("João", None, None)
        self.assertIn("O seu chamado no NEXO", sem_numero)
        self.assertNotIn("Situação atual", sem_numero)

    def test_nome_cadastrado_nao_muda_a_forma_da_mensagem(self):
        texto = whatsapp_service.texto_atualizacao_ticket("João\n\nClique: http://x.invalid", 1, "ABERTO")
        self.assertNotIn("\n", texto)
        self.assertTrue(texto.startswith("Olá, João Clique: http://x.invalid! O seu chamado #1"))


# ---------------------------------------------------------------------------
# Notificador (abridor falso)
# ---------------------------------------------------------------------------
class NotificadorTests(_ComLog):
    def _notificador(self, **opcoes):
        opcoes.setdefault("assincrono", False)
        return WhatsAppNotificador(URL + "/", CHAVE, INSTANCIA, **opcoes)

    def test_desligado_sem_configuracao_so_avisa(self):
        abridor = _Abridor()
        notificador = WhatsAppNotificador("", "", "", assincrono=False)
        self.assertFalse(notificador.configurado)
        with patch.object(whatsapp_service, "_abrir", abridor), self.assertLogs(whatsapp_service.logger, "WARNING") as log:
            self.assertFalse(notificador.notificar_relatorio_pronto(TELEFONE, "Ana", "Loja", 10))
            self.assertFalse(notificador.notificar_abertura_mensal(TELEFONE, "Ana", "2026-10"))
            self.assertFalse(notificador.notificar_atualizacao_ticket(TELEFONE, "Ana", 1, "ABERTO"))
            self.assertFalse(notificador.enviar_texto(TELEFONE, "oi"))
        self.assertEqual(abridor.pedidos, [])
        self.assertEqual(len(log.output), 4)
        self.assertIn("EVOLUTION_API_URL, EVOLUTION_API_KEY, EVOLUTION_INSTANCE_NAME", log.output[0])
        self.assertIn("***8877", log.output[0])
        self.assert_log_sem_segredo(log.output)

    def test_url_sem_tls_ou_estranha_desliga_o_envio(self):
        for url in ("http://evolution.example.invalid", "ftp://evolution.example.invalid",
                    "https://usuario:senha@evolution.example.invalid", "https://evolution.example.invalid/?a=1",
                    "https://evolution.example.invalid/#x", "evolution.example.invalid", "https://", "https://[::1",
                    None, 123, "https://evolution.example.invalid:abc", "https://evolution.example.invalid:99999",
                    "https://exa mple.invalid", "https://evolution.example.invalid/ caminho",
                    "https://evolution.example.invalid/\x00",
                    # Os exemplos da documentação: copiados, mandariam a chave a um endereço de terceiro.
                    "https://sua-api.onrender.com", "https://SUA-API.exemplo.com.br", "https://example.com",
                    "https://api.example.org/v2", "https://evolution.example"):
            with self.subTest(url=url):
                self.assertEqual(WhatsAppNotificador(url, CHAVE, INSTANCIA).pendencias, ("EVOLUTION_API_URL",))

    def test_http_so_em_localhost(self):
        for url in ("http://localhost:8080", "http://127.0.0.1:8080/", "http://[::1]:8080"):
            with self.subTest(url=url):
                self.assertTrue(WhatsAppNotificador(url, CHAVE, INSTANCIA).configurado)
        self.assertTrue(WhatsAppNotificador("https://evolution.example.invalid/api/", CHAVE, INSTANCIA).configurado)

    def test_post_aceito_pela_api(self):
        abridor = _Abridor(_Resposta(201))
        with patch.object(whatsapp_service, "_abrir", abridor), self.assertLogs(whatsapp_service.logger, "INFO") as log:
            self.assertTrue(self._notificador().notificar_atualizacao_ticket(TELEFONE, "Ana", 12, "EM_ATENDIMENTO"))
        [(pedido, timeout)] = abridor.pedidos
        self.assertEqual(timeout, 5)
        self.assertEqual(pedido.get_method(), "POST")
        self.assertEqual(pedido.full_url, "https://evolution.example.invalid/message/sendText/nexo%20principal%2F1")
        self.assertEqual(pedido.get_header("Content-type"), "application/json")
        # A chave vai só no cabeçalho que não acompanha redirecionamento.
        self.assertEqual(pedido.unredirected_hdrs.get("Apikey"), CHAVE)
        self.assertNotIn("Apikey", pedido.headers)
        self.assertEqual(json.loads(pedido.data.decode("utf-8")), {
            "number": NUMERO,
            "text": "Olá, Ana! O seu chamado #12 no NEXO Faturamento Inteligente foi atualizado. "
                    "Situação atual: Em atendimento. Acesse o portal para ver os detalhes."})
        self.assertIn("aceito pela Evolution API para ***8877", log.output[-1])
        self.assert_log_sem_segredo(log.output)

    def test_qualquer_2xx_e_aceite_e_o_resto_nao(self):
        for status, esperado in ((200, True), (201, True), (204, True), (None, False), (302, False), (500, False)):
            with self.subTest(status=status), patch.object(whatsapp_service, "_abrir", _Abridor(_Resposta(status))):
                self.assertIs(self._notificador().enviar_texto(TELEFONE, "oi"), esperado)

    def test_recusa_http_vira_aviso_sem_segredo(self):
        for codigo in (400, 401, 403, 404, 429, 500, 502):
            with self.subTest(codigo=codigo), patch.object(whatsapp_service, "_abrir", _Abridor(erro=_erro_http(codigo))), \
                    self.assertLogs(whatsapp_service.logger, "WARNING") as log:
                self.assertFalse(self._notificador().notificar_relatorio_pronto(TELEFONE, "Ana", "Loja", 10))
            self.assertIn(f"recusou o aviso 'relatorio_pronto' para ***8877 (HTTP {codigo})", log.output[0])
            self.assert_log_sem_segredo(log.output)

    def test_api_fora_do_ar_vira_aviso(self):
        for erro in (urllib.error.URLError("sem rota"), TimeoutError("lento"), ConnectionRefusedError(),
                     http.client.RemoteDisconnected("fechou"), http.client.IncompleteRead(b""), OSError("rede"),
                     ValueError("url")):
            with self.subTest(erro=type(erro).__name__), patch.object(whatsapp_service, "_abrir", _Abridor(erro=erro)), \
                    self.assertLogs(whatsapp_service.logger, "WARNING") as log:
                self.assertFalse(self._notificador().notificar_abertura_mensal(TELEFONE, "Ana", "2026-10"))
            self.assertIn("Evolution API indisponível", log.output[0])
            self.assertIn(type(erro).__name__, log.output[0])
            self.assert_log_sem_segredo(log.output)

    def test_nenhum_metodo_levanta_excecao(self):
        estranho = _Abridor(erro=RuntimeError("bug no transporte com " + CHAVE))
        with patch.object(whatsapp_service, "_abrir", estranho), self.assertLogs(whatsapp_service.logger, "WARNING") as log:
            self.assertFalse(self._notificador().notificar_atualizacao_ticket(TELEFONE, "Ana", 1, "ABERTO"))
            self.assertFalse(self._notificador().enviar_texto(TELEFONE, "oi"))
        self.assertTrue(all("RuntimeError" in linha for linha in log.output))
        self.assert_log_sem_segredo(log.output)
        with patch.object(whatsapp_service, "texto_atualizacao_ticket", side_effect=RuntimeError("bug")), \
                patch.object(whatsapp_service, "_abrir", _Abridor()) as abridor, \
                self.assertLogs(whatsapp_service.logger, "WARNING"):
            self.assertFalse(self._notificador().notificar_atualizacao_ticket(TELEFONE, "Ana", 1, "ABERTO"))
        self.assertEqual(abridor.pedidos, [])

    def test_sem_telefone_cadastrado_nao_e_falha(self):
        abridor = _Abridor()
        with patch.object(whatsapp_service, "_abrir", abridor), self.assertNoLogs(whatsapp_service.logger, "WARNING"):
            for telefone in (None, "", "   "):
                self.assertFalse(self._notificador().notificar_relatorio_pronto(telefone, "Ana", "Loja", 10))
        self.assertEqual(abridor.pedidos, [])

    def test_telefone_invalido_avisa_e_nao_envia(self):
        abridor = _Abridor()
        with patch.object(whatsapp_service, "_abrir", abridor), self.assertLogs(whatsapp_service.logger, "WARNING") as log:
            self.assertFalse(self._notificador().notificar_relatorio_pronto("62 99999-8877 ramal 3", "Ana", "Loja", 10))
        self.assertEqual(abridor.pedidos, [])
        self.assertIn("não é um número brasileiro válido", log.output[0])

    def test_texto_livre_vazio_ou_longo(self):
        abridor = _Abridor()
        with patch.object(whatsapp_service, "_abrir", abridor):
            self.assertTrue(self._notificador().enviar_texto(TELEFONE, "x" * 5000))
            with self.assertLogs(whatsapp_service.logger, "WARNING"):
                self.assertFalse(self._notificador().enviar_texto(TELEFONE, "   "))
        [(pedido, _)] = abridor.pedidos
        self.assertEqual(len(json.loads(pedido.data)["text"]), 1000)

    def test_repr_sem_segredo(self):
        representacao = repr(self._notificador())
        self.assertNotIn(CHAVE, representacao)
        self.assertNotIn("evolution.example.invalid", representacao)


class FilaDeFundoTests(_ComLog):
    def test_assincrono_entrega_a_fila_e_nao_espera_a_rede(self):
        fila = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(fila.shutdown, wait=True)
        abridor = _Abridor(espera=1.0)
        notificador = WhatsAppNotificador(URL, CHAVE, INSTANCIA, executor=fila)
        with patch.object(whatsapp_service, "_abrir", abridor):
            inicio = time.monotonic()
            self.assertTrue(notificador.notificar_abertura_mensal(TELEFONE, "Loja", "2026-10"))
            self.assertLess(time.monotonic() - inicio, 0.5)
            fila.shutdown(wait=True)
        self.assertEqual(len(abridor.pedidos), 1)
        self.assertIn("outubro de 2026", json.loads(abridor.pedidos[0][0].data)["text"])

    def test_falha_na_fila_vira_aviso(self):
        fila = ThreadPoolExecutor(max_workers=1)
        notificador = WhatsAppNotificador(URL, CHAVE, INSTANCIA, executor=fila)
        with patch.object(whatsapp_service, "_abrir", _Abridor(erro=RuntimeError("bug"))), \
                self.assertLogs(whatsapp_service.logger, "WARNING") as log:
            self.assertTrue(notificador.notificar_relatorio_pronto(TELEFONE, "Ana", "Loja", 10))
            fila.shutdown(wait=True)
        self.assertIn("falha inesperada no aviso 'relatorio_pronto' (RuntimeError)", log.output[0])

    def test_fila_encerrada_nao_levanta(self):
        fila = ThreadPoolExecutor(max_workers=1)
        fila.shutdown(wait=True)
        notificador = WhatsAppNotificador(URL, CHAVE, INSTANCIA, executor=fila)
        with patch.object(whatsapp_service, "_abrir", _Abridor()) as abridor, \
                self.assertLogs(whatsapp_service.logger, "WARNING") as log:
            self.assertFalse(notificador.notificar_relatorio_pronto(TELEFONE, "Ana", "Loja", 10))
        self.assertEqual(abridor.pedidos, [])
        self.assertIn("RuntimeError", log.output[0])

    def test_sem_configuracao_nada_entra_na_fila(self):
        class Fila:
            submetidos = []

            def submit(self, *args, **kwargs):
                self.submetidos.append(args)

        fila = Fila()
        with self.assertLogs(whatsapp_service.logger, "WARNING"):
            self.assertFalse(WhatsAppNotificador("", "", "", executor=fila).notificar_relatorio_pronto(TELEFONE, "A", "B", 1))
        self.assertEqual(fila.submetidos, [])

    def test_fila_do_processo_e_recriada_depois_de_um_fork(self):
        antes = (whatsapp_service._executor, whatsapp_service._executor_pid)

        def restaurar():
            whatsapp_service._executor, whatsapp_service._executor_pid = antes

        self.addCleanup(restaurar)
        whatsapp_service._executor, whatsapp_service._executor_pid = None, None
        with patch.object(whatsapp_service.os, "getpid", return_value=111):
            primeira = whatsapp_service._executor_do_processo()
            self.assertIs(whatsapp_service._executor_do_processo(), primeira)
        with patch.object(whatsapp_service.os, "getpid", return_value=222):
            segunda = whatsapp_service._executor_do_processo()
        self.assertIsNot(primeira, segunda)

    def test_fila_do_processo_tem_uma_thread_daemon_em_ordem(self):
        fila = whatsapp_service._FilaDeEnvio()
        self.assertTrue(fila._thread.daemon)  # o encerramento do processo não espera a fila
        ordem = []
        for indice in range(6):
            # O primeiro é o mais lento: com mais de uma thread, chegaria por último.
            fila.submit(lambda i=indice: (time.sleep(0.2 if i == 0 else 0), ordem.append(i)))
        fila.aguardar()
        self.assertEqual(ordem, list(range(6)))

    def test_fila_do_processo_continua_depois_de_uma_falha(self):
        fila = whatsapp_service._FilaDeEnvio()
        feitos = []
        fila.submit(lambda: 1 / 0)
        fila.submit(lambda: feitos.append("depois"))
        fila.aguardar()
        self.assertEqual(feitos, ["depois"])

    def test_fila_cheia_descarta_o_aviso_novo_com_aviso_no_log(self):
        fila = whatsapp_service._FilaDeEnvio(limite=1)
        comecou, liberar = threading.Event(), threading.Event()
        fila.submit(lambda: (comecou.set(), liberar.wait()))  # ocupa a thread
        self.assertTrue(comecou.wait(2))  # a thread já tirou o primeiro item da fila
        fila.submit(lambda: None)          # ocupa a única vaga
        notificador = WhatsAppNotificador(URL, CHAVE, INSTANCIA, executor=fila)
        with patch.object(whatsapp_service, "_abrir", _Abridor()) as abridor, \
                self.assertLogs(whatsapp_service.logger, "WARNING") as log:
            self.assertFalse(notificador.notificar_relatorio_pronto(TELEFONE, "Ana", "Loja", 10))
        liberar.set()
        fila.aguardar()
        self.assertEqual(abridor.pedidos, [])
        self.assertIn("aviso 'relatorio_pronto' não enviado (Full)", log.output[0])

    def test_rota_nunca_espera_a_fila_do_processo(self):
        antes = (whatsapp_service._executor, whatsapp_service._executor_pid)
        self.addCleanup(lambda: setattr(whatsapp_service, "_executor", antes[0]) or
                        setattr(whatsapp_service, "_executor_pid", antes[1]))
        whatsapp_service._executor = None
        abridor = _Abridor(espera=0.5)
        notificador = WhatsAppNotificador(URL, CHAVE, INSTANCIA)
        with patch.object(whatsapp_service, "_abrir", abridor):
            inicio = time.monotonic()
            for status in ("EM_ATENDIMENTO", "RESOLVIDO"):
                self.assertTrue(notificador.notificar_atualizacao_ticket(TELEFONE, "Ana", 3, status))
            self.assertLess(time.monotonic() - inicio, 0.3)
            whatsapp_service._executor.aguardar()
        textos = [json.loads(pedido.data)["text"] for pedido, _ in abridor.pedidos]
        self.assertEqual([("Em atendimento" in t, "Resolvido" in t) for t in textos], [(True, False), (False, True)])


# ---------------------------------------------------------------------------
# HTTP de verdade contra um servidor local
# ---------------------------------------------------------------------------
class ServidorLocalTests(_ComLog):
    def setUp(self):
        super().setUp()
        teste = self
        self.recebidos = []

        class Evolution(BaseHTTPRequestHandler):
            def do_POST(self):
                tamanho = int(self.headers.get("Content-Length") or 0)
                corpo = self.rfile.read(tamanho)
                teste.recebidos.append({"caminho": self.path, "apikey": self.headers.get("apikey"),
                                        "tipo": self.headers.get("Content-Type"), "corpo": corpo})
                if self.path.startswith("/lento/"):
                    time.sleep(1.5)
                if self.path.startswith("/redireciona/"):
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{teste.porta}/destino/x")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 401 if self.path.startswith("/recusa/") else 201
                resposta = json.dumps({"key": {"id": "ABC"}, "status": "PENDING"}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resposta)))
                self.end_headers()
                self.wfile.write(resposta)

            do_GET = do_POST

            def log_message(self, *args):
                pass

        class Servidor(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # o cliente que desistiu pelo prazo fecha a conexão: não é falha do teste

        self.servidor = Servidor(("127.0.0.1", 0), Evolution)
        self.porta = self.servidor.server_port
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        self.addCleanup(self.servidor.server_close)
        self.addCleanup(self.servidor.shutdown)

    def _notificador(self, prefixo):
        return WhatsAppNotificador(f"http://127.0.0.1:{self.porta}/{prefixo}", CHAVE, "nexo", assincrono=False)

    def test_post_real_aceito(self):
        self.assertTrue(self._notificador("ok").notificar_atualizacao_ticket(TELEFONE, "Ana", 7, "RESOLVIDO"))
        [recebido] = self.recebidos
        self.assertEqual(recebido["caminho"], "/ok/message/sendText/nexo")
        self.assertEqual(recebido["apikey"], CHAVE)
        self.assertEqual(recebido["tipo"], "application/json")
        corpo = json.loads(recebido["corpo"].decode("utf-8"))
        self.assertEqual(corpo["number"], NUMERO)
        self.assertIn("Situação atual: Resolvido.", corpo["text"])

    def test_recusa_real(self):
        with self.assertLogs(whatsapp_service.logger, "WARNING") as log:
            self.assertFalse(self._notificador("recusa").enviar_texto(TELEFONE, "oi"))
        self.assertIn("(HTTP 401)", log.output[0])

    def test_redirecionamento_nao_e_seguido(self):
        with self.assertLogs(whatsapp_service.logger, "WARNING") as log:
            self.assertFalse(self._notificador("redireciona").enviar_texto(TELEFONE, "oi"))
        self.assertIn("(HTTP 302)", log.output[0])
        self.assertEqual([r["caminho"] for r in self.recebidos], ["/redireciona/message/sendText/nexo"])

    def test_resposta_lenta_estoura_o_prazo(self):
        with patch.object(whatsapp_service, "TIMEOUT_SEGUNDOS", 0.3), \
                self.assertLogs(whatsapp_service.logger, "WARNING") as log:
            inicio = time.monotonic()
            self.assertFalse(self._notificador("lento").enviar_texto(TELEFONE, "oi"))
            self.assertLess(time.monotonic() - inicio, 1.2)
        self.assertIn("Evolution API indisponível", log.output[0])

    def test_porta_fechada(self):
        livre = socket.socket()
        livre.bind(("127.0.0.1", 0))
        porta = livre.getsockname()[1]
        livre.close()
        notificador = WhatsAppNotificador(f"http://127.0.0.1:{porta}", CHAVE, "nexo", assincrono=False)
        with self.assertLogs(whatsapp_service.logger, "WARNING") as log:
            self.assertFalse(notificador.enviar_texto(TELEFONE, "oi"))
        self.assertIn("Evolution API indisponível", log.output[0])


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
class ConfiguracaoTests(_ComLog):
    def _app(self, **config):
        app = Flask(__name__)
        app.config.update({"EVOLUTION_API_URL": None, "EVOLUTION_API_KEY": None, "EVOLUTION_INSTANCE_NAME": None,
                           "WHATSAPP_EMPRESA_NUMERO": ""})
        app.config.update(config)
        return app

    def test_dentro_do_app_vale_so_o_app_config(self):
        # None no app.config é "não configurado", mesmo com as variáveis no ambiente de quem roda.
        ambiente = {"EVOLUTION_API_URL": URL, "EVOLUTION_API_KEY": CHAVE, "EVOLUTION_INSTANCE_NAME": "nexo"}
        with patch.dict(os.environ, ambiente), self._app().app_context():
            self.assertEqual(WhatsAppNotificador().pendencias, whatsapp_service.VARIAVEIS)
        with self._app(EVOLUTION_API_URL=f" {URL} ", EVOLUTION_API_KEY=CHAVE,
                       EVOLUTION_INSTANCE_NAME="nexo").app_context():
            self.assertTrue(WhatsAppNotificador().configurado)

    def test_fora_do_app_le_o_ambiente(self):
        with patch.dict(os.environ, {"EVOLUTION_API_URL": URL, "EVOLUTION_API_KEY": CHAVE,
                                     "EVOLUTION_INSTANCE_NAME": "nexo"}):
            self.assertTrue(WhatsAppNotificador().configurado)
        with patch.dict(os.environ, VARIAVEIS_VAZIAS):
            self.assertFalse(WhatsAppNotificador().configurado)

    def test_configurar_normaliza_o_numero_comercial(self):
        casos = {"(62) 99999-8877": (NUMERO, False), "+55 62 99999-8877": (NUMERO, False), "": ("", False),
                 "   ": ("", False), None: ("", False), "0800 123 4567": ("", True), "abc": ("", True)}
        for bruto, (numero, invalido) in casos.items():
            with self.subTest(bruto=bruto):
                app = self._app(WHATSAPP_EMPRESA_NUMERO=bruto)
                whatsapp_service.configurar(app)
                self.assertEqual(app.config["WHATSAPP_EMPRESA_NUMERO"], numero)
                self.assertIs(app.config["WHATSAPP_EMPRESA_NUMERO_INVALIDO"], invalido)

    def test_diagnostico_de_inicializacao_sem_segredo(self):
        completo = self._app(EVOLUTION_API_URL=URL, EVOLUTION_API_KEY=CHAVE, EVOLUTION_INSTANCE_NAME="nexo")
        with self.assertLogs(whatsapp_service.logger, "INFO") as log:
            whatsapp_service.configurar(completo)
        self.assertIn("disparos ativos", log.output[0])
        parcial = self._app(EVOLUTION_API_URL="http://evolution.example.invalid", EVOLUTION_API_KEY=CHAVE)
        with self.assertLogs(whatsapp_service.logger, "WARNING") as log:
            whatsapp_service.configurar(parcial)
        self.assertIn("EVOLUTION_API_URL, EVOLUTION_INSTANCE_NAME", log.output[0])
        with self.assertLogs(whatsapp_service.logger, "DEBUG") as log:
            whatsapp_service.configurar(self._app())
        self.assertTrue(log.output[0].startswith("DEBUG:whatsapp_service:WhatsApp: disparos desligados"))
        with self.assertLogs(whatsapp_service.logger, "INFO") as log:
            whatsapp_service.configurar(self._app(PRODUCAO_HTTP=True))
        self.assertTrue(log.output[0].startswith("INFO:whatsapp_service:WhatsApp: disparos desligados"))
        with self.assertLogs(whatsapp_service.logger, "WARNING") as log:
            whatsapp_service.configurar(self._app(WHATSAPP_EMPRESA_NUMERO="123"))
        self.assertIn("WHATSAPP_EMPRESA_NUMERO não é um telefone brasileiro válido", log.output[-1])
        self.assertNotIn("123", log.output[-1].split("WHATSAPP_EMPRESA_NUMERO", 1)[1])
        self.assert_log_sem_segredo(log.output)


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
