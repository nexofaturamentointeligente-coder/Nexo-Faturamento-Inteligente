"""Horário oficial de Brasília no suporte e no sininho (cenários A–L).

A correção grava cada evento em UTC (models.InstanteUTCSemFuso) e o exibe
sempre em America/Sao_Paulo, formatado no servidor (horario.py). Jinja e
Socket.IO usam a mesma função sobre o mesmo instante gravado. Os cenários
percorrem as rotas reais com dois usuários fictícios, ADMIN e CLIENTE, cada um
com sessão HTTP e Socket.IO próprios. O relógio do portal é injetado
(NEXO_RELOGIO): nenhum teste espera tempo real.

  A  29/10/2026 17:34 UTC aparece como 14:34 em todos os pontos
  B  30/10/2026 01:30 UTC é 29/10/2026 22:30, também onde só se mostra a data
  C  o fuso do processo (TZ do servidor) não muda nada
  D  a diferença observada: o relógio de outro fuso exibido como Brasília
  E  horário de verão histórico pelas regras IANA, sem deslocamento fixo
  F  tempo real: o payload do Socket.IO leva o instante gravado
  G  recarga: o sininho recarregado mostra o texto do tempo real
  H  ADMIN e CLIENTE veem o mesmo instante de cada mensagem
  I  roteiro: abertura, respostas, status, resolução e reabertura
  J  PostgreSQL descartável com o TimeZone da sessão em quatro fusos, num
     processo separado (requer NEXO_TEST_POSTGRES_BIN)
  K  registros legados no relógio UTC, sem deslocamento indevido
  L  segurança: nada é entregue após rollback, isolamento entre papéis,
     escape e recusa de horários sem fuso
Navegador (NEXO_TEST_BROWSER=1): os dois papéis em navegadores com fusos que
não são o de Brasília, com Socket.IO de verdade, recarga e conversa.
"""

import base64
import contextlib
from datetime import datetime, timedelta
import hashlib
import html
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
import types
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # o processo do cenário J executa este arquivo diretamente
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from horario import BRASILIA, UTC, horario_brasilia, iso_utc  # noqa: E402
# Antes de qualquer create_app: o Flask-SocketIO só repassa a cada novo servidor
# (um por app de teste) os handlers declarados quando ainda não havia servidor.
# Sem isso, a partir do segundo app a conexão não entra na sala do usuário.
import realtime  # noqa: E402,F401
from tests.deploy_support import DeployTestCase  # noqa: E402

A = datetime(2026, 10, 29, 17, 34, tzinfo=UTC)  # 29/10/2026 14:34 em Brasília
B = datetime(2026, 10, 30, 1, 30, tzinfo=UTC)   # 29/10/2026 22:30 em Brasília (em UTC já é 30/10)
PAPEIS = ("ADMIN", "CLIENTE")
CHAVES_PAYLOAD = {"id", "texto", "link", "hora", "hora_completa", "instante", "badge", "badge_rotulo"}
ZONAS_SESSAO_PG = ("UTC", "America/Sao_Paulo", "Atlantic/Azores", "Asia/Tokyo")


def tempo(texto, iso, completo):
    """O que um <time> mostra: [texto, atributo datetime, title]."""
    return [texto, iso, f"{completo} (Horário de Brasília)"]


@contextlib.contextmanager
def fuso_do_processo(zona):
    """TZ do processo (o relógio de datetime.now()), restaurado ao sair."""
    anterior = os.environ.get("TZ")
    os.environ["TZ"] = zona
    time.tzset()
    try:
        yield
    finally:
        if anterior is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = anterior
        time.tzset()


class Portal:
    """ADMIN e CLIENTE em sessões HTTP próprias, cada um com o seu Socket.IO."""

    def __init__(self, app, senha, admin="admin-test@example.invalid", cliente="cliente-test@example.invalid"):
        from extensions import socketio
        self.app = app
        self.http = {"ADMIN": self._entrar(admin, senha), "CLIENTE": self._entrar(cliente, senha)}
        self.ws = {}
        for papel, http in self.http.items():
            conexao = socketio.test_client(app, flask_test_client=http)
            if not conexao.is_connected():
                raise AssertionError(f"Socket.IO do {papel} não conectou")
            conexao.get_received()
            self.ws[papel] = conexao

    def fechar(self):
        for conexao in self.ws.values():
            if conexao.is_connected():
                conexao.disconnect()

    def relogio(self, instante):
        self.app.config["NEXO_RELOGIO"] = lambda: instante

    # ------------------------------------------------------------------ HTTP
    @staticmethod
    def _csrf(http, caminho):
        resposta = http.get(caminho)
        if resposta.status_code != 200:
            raise AssertionError(f"GET {caminho}: HTTP {resposta.status_code}")
        return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', resposta.get_data(as_text=True)).group(1)

    def _entrar(self, email, senha):
        http = self.app.test_client()
        resposta = http.post("/auth/login", data={"email": email, "senha": senha,
                                                  "csrf_token": self._csrf(http, "/auth/login")})
        if resposta.status_code != 302:
            raise AssertionError(f"Login de {email}: HTTP {resposta.status_code}")
        return http

    def _postar(self, papel, caminho, dados):
        http = self.http[papel]
        resposta = http.post(caminho, data=dict(dados, csrf_token=self._csrf(http, caminho)))
        if resposta.status_code != 302:
            raise AssertionError(f"POST {caminho}: HTTP {resposta.status_code}")
        return resposta

    def pagina(self, papel, caminho):
        resposta = self.http[papel].get(caminho)
        if resposta.status_code != 200:
            raise AssertionError(f"GET {caminho} ({papel}): HTTP {resposta.status_code}")
        return resposta.get_data(as_text=True)

    # ----------------------------------------------------------------- ações
    def abrir_chamado(self, assunto="Dúvida fictícia sobre o relatório", mensagem="Mensagem fictícia do cliente."):
        resposta = self._postar("CLIENTE", "/cliente/tickets",
                                {"assunto": assunto, "categoria": "DUVIDA_TECNICA", "mensagem": mensagem})
        return int(re.search(r"/cliente/tickets/(\d+)", resposta.headers["Location"]).group(1))

    def responder_admin(self, id_chamado, texto="Resposta fictícia da equipe."):
        self._postar("ADMIN", f"/admin/tickets/{id_chamado}", {"acao": "responder", "mensagem": texto})

    def responder_cliente(self, id_chamado, texto="Nova mensagem fictícia do cliente."):
        self._postar("CLIENTE", f"/cliente/tickets/{id_chamado}", {"mensagem": texto})

    def alterar_status(self, id_chamado, status):
        self._postar("ADMIN", f"/admin/tickets/{id_chamado}", {"acao": "alterar_status", "status_chamado": status})

    # --------------------------------------------------------------- leituras
    def recebidos(self, papel):
        return [pacote["args"][0] for pacote in self.ws[papel].get_received() if pacote["name"] == "nova_notificacao"]

    @staticmethod
    def horarios(fragmento):
        return [[html.unescape(texto), iso, html.unescape(titulo)] for iso, titulo, texto in
                re.findall(r'<time datetime="([^"]*)" title="([^"]*)">([^<]*)</time>', fragmento)]

    def sininho(self, papel):
        """Sininho da página recarregada: {id_notificacao: [texto, datetime, title]}."""
        pagina = self.pagina(papel, "/admin/tickets" if papel == "ADMIN" else "/cliente/tickets")
        lista = re.search(r'id="bellList">(.*?)id="bellEmpty"', pagina, re.S).group(1)
        itens = {}
        # SEC-002: cada item é um <form method="POST"> com um <button class="bell-item">.
        for id_notificacao, corpo in re.findall(
                r'<form method="POST" action="/notificacoes/(\d+)/abrir" class="bell-item-form">'
                r'.*?<button type="submit" class="bell-item[^"]*">(.*?)</button>', lista, re.S):
            itens[int(id_notificacao)] = self.horarios(re.search(r"<small>(.*?)</small>", corpo, re.S).group(1))[0]
        return itens

    def conversa(self, papel, id_chamado):
        """Horários das mensagens (na ordem exibida) e do cabeçalho "aberto em"."""
        pagina = self.pagina(papel, f"/{'admin' if papel == 'ADMIN' else 'cliente'}/tickets/{id_chamado}")
        mensagens = [self.horarios(span)[0] for span in re.findall(r'<span class="chat-time">(.*?)</span>', pagina, re.S)]
        aberto = self.horarios(re.search(r"aberto em (.*?)</p>", pagina, re.S).group(1))[0]
        aviso = re.search(r"resolvido</strong>(.*?)\. Envie", pagina, re.S)
        return {"mensagens": mensagens, "aberto_em": aberto,
                "resolvido_em": (self.horarios(aviso.group(1)) or [None])[0] if aviso else None}

    def painel_admin(self, id_chamado):
        """Painel lateral do chamado: {rótulo: horário}."""
        pagina = self.pagina("ADMIN", f"/admin/tickets/{id_chamado}")
        painel = {}
        for rotulo, valor in re.findall(
                r'<div class="kpi-label(?: mt-2)?">([^<]+)</div>\s*<div class="t-mono"[^>]*>(.*?)</div>', pagina, re.S):
            painel[rotulo] = (self.horarios(valor) or [html.unescape(valor.strip())])[0]
        return painel

    def lista(self, papel):
        """Listagem de chamados: {id_chamado: horário} (Última atividade / Aberto em)."""
        pagina = self.pagina(papel, "/admin/tickets" if papel == "ADMIN" else "/cliente/tickets")
        linhas = {}
        for tr in re.findall(r"<tr>(.*?)</tr>", pagina, re.S):
            ident = re.search(r'<td class="t-mono t-mut">(\d+)</td>', tr)
            if ident:
                linhas[int(ident.group(1))] = self.horarios(tr)[0]
        # O painel do admin lista os chamados em cards, não em linhas de tabela.
        for card in re.findall(r'<article class="nexo-card ticket-card">(.*?)</article>', pagina, re.S):
            ident = re.search(r'<span class="t-mono t-mut">#(\d+)</span>', card)
            linhas[int(ident.group(1))] = self.horarios(card)[0]
        return linhas


# ---------------------------------------------------------------------------
# Roteiro I: o mesmo percurso em SQLite e em cada banco PostgreSQL do cenário J
# ---------------------------------------------------------------------------
ROTEIRO = (
    # (etapa, instante UTC, papel notificado, horário de Brasília)
    ("abertura", A, "ADMIN", "14:34"),
    ("resposta do admin", datetime(2026, 10, 29, 17, 52, tzinfo=UTC), "CLIENTE", "14:52"),
    ("resposta do cliente", datetime(2026, 10, 29, 18, 3, tzinfo=UTC), "ADMIN", "15:03"),
    ("mudança de status", datetime(2026, 10, 29, 18, 15, tzinfo=UTC), None, "15:15"),
    ("resolução", datetime(2026, 10, 29, 18, 27, tzinfo=UTC), "CLIENTE", "15:27"),
    # O status continua RESOLVIDO: "Resolvido em" (15:27) fica antes da "Última atualização".
    ("resposta após resolver", datetime(2026, 10, 29, 18, 40, tzinfo=UTC), "CLIENTE", "15:40"),
    ("reabertura", B, "ADMIN", "22:30"),
)
RESOLVIDO_EM = ("29/10/2026 às 15:27", "2026-10-29T18:27:00+00:00")


def executar_roteiro(portal):
    """Percorre o roteiro e devolve o que cada papel viu em cada etapa (JSON)."""
    etapas, id_chamado = [], None
    for etapa, instante, _notificado, _hora in ROTEIRO:
        portal.relogio(instante)
        if etapa == "abertura":
            id_chamado = portal.abrir_chamado()
        elif etapa in ("resposta do admin", "resposta após resolver"):
            portal.responder_admin(id_chamado)
        elif etapa in ("resposta do cliente", "reabertura"):
            portal.responder_cliente(id_chamado)
        elif etapa == "mudança de status":
            portal.alterar_status(id_chamado, "ABERTO")
        else:
            portal.alterar_status(id_chamado, "RESOLVIDO")
        etapas.append({
            "etapa": etapa,
            "recebidos": {papel: portal.recebidos(papel) for papel in PAPEIS},
            "sininho": {papel: portal.sininho(papel) for papel in PAPEIS},
            "conversa": {papel: portal.conversa(papel, id_chamado) for papel in PAPEIS},
            "painel": portal.painel_admin(id_chamado),
            "lista": {papel: portal.lista(papel)[id_chamado] for papel in PAPEIS},
        })
    return json.loads(json.dumps({"id_chamado": id_chamado, "etapas": etapas}))


def conferir_roteiro(caso, visto):
    """Os horários esperados, calculados à mão, em cada ponto do roteiro."""
    mensagens = []
    for (etapa, instante, notificado, hora), passo in zip(ROTEIRO, visto["etapas"]):
        iso = instante.isoformat()
        completo = f"29/10/2026 às {hora}"
        with caso.subTest(etapa=etapa):
            for papel in PAPEIS:
                recebidos = passo["recebidos"][papel]
                if papel != notificado:
                    caso.assertEqual(recebidos, [], f"{papel} não deveria ser notificado")
                    continue
                (payload,) = recebidos
                caso.assertEqual(set(payload), CHAVES_PAYLOAD)
                caso.assertEqual([payload["hora"], payload["instante"], payload["hora_completa"]],
                                 tempo(f"29/10 {hora}", iso, completo))
                # Recarga: o mesmo texto do tempo real, para a mesma notificação.
                caso.assertEqual(passo["sininho"][papel][str(payload["id"])],
                                 [payload["hora"], payload["instante"], payload["hora_completa"]])
            if etapa not in ("mudança de status", "resolução"):
                mensagens.append(tempo(completo, iso, completo))
            for papel in PAPEIS:  # o mesmo instante para os dois papéis
                caso.assertEqual(passo["conversa"][papel]["mensagens"], mensagens)
                caso.assertEqual(passo["conversa"][papel]["aberto_em"],
                                 tempo("29/10/2026 às 14:34", A.isoformat(), "29/10/2026 às 14:34"))
            # Última atividade: a última mensagem ou mudança de status (sem fallback enganoso).
            caso.assertEqual(passo["lista"]["ADMIN"], tempo(completo, iso, completo))
            caso.assertEqual(passo["lista"]["CLIENTE"], tempo("29/10/2026", A.isoformat(), "29/10/2026 às 14:34"))
            painel = passo["painel"]
            caso.assertEqual(painel["Aberto em"], tempo("29/10/2026 às 14:34", A.isoformat(), "29/10/2026 às 14:34"))
            caso.assertEqual(painel["Última mensagem"], mensagens[-1])
            caso.assertEqual(painel["Última atualização"],
                             "—" if etapa == "abertura" else tempo(completo, iso, completo))
            if etapa in ("resolução", "resposta após resolver"):
                resolvido = tempo(*RESOLVIDO_EM, RESOLVIDO_EM[0])
                caso.assertEqual(painel["Resolvido em"], resolvido)
                caso.assertEqual(passo["conversa"]["CLIENTE"]["resolvido_em"], resolvido)
            else:
                caso.assertNotIn("Resolvido em", painel)
                caso.assertIsNone(passo["conversa"]["CLIENTE"]["resolvido_em"])


class HorarioBrasiliaTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()
        self.portal = Portal(self.app, self.password)
        self.addCleanup(self.portal.fechar)
        self.portal.relogio(A)

    def gravados(self, sql):
        return [str(valor) for valor in self.db.session.execute(text(sql)).scalars()]

    # ------------------------------------------------------------- A e B
    def test_a_formatador_converte_o_instante_para_brasilia(self):
        self.assertEqual(horario_brasilia(A), "29/10/2026 às 14:34")
        self.assertEqual(horario_brasilia(A, "curto"), "29/10 14:34")
        self.assertEqual(horario_brasilia(A, "data"), "29/10/2026")
        self.assertEqual(horario_brasilia(A, rotulo=True), "29/10/2026 às 14:34 (Horário de Brasília)")
        self.assertEqual(iso_utc(A), "2026-10-29T17:34:00+00:00")
        # O mesmo instante com outro fuso: uma conversão só, sem acumular deslocamentos.
        for zona in ("Asia/Tokyo", "America/Sao_Paulo", "Pacific/Kiritimati"):
            self.assertEqual(horario_brasilia(A.astimezone(ZoneInfo(zona))), "29/10/2026 às 14:34")
            self.assertEqual(iso_utc(A.astimezone(ZoneInfo(zona))), "2026-10-29T17:34:00+00:00")
        self.assertEqual((horario_brasilia(None), iso_utc(None)), ("—", ""))
        with self.assertRaises(ValueError):
            horario_brasilia(A, "relativo")

    def test_a_29_10_2026_17h34_utc_aparece_14h34_em_todos_os_pontos(self):
        id_chamado = self.portal.abrir_chamado()
        (payload,) = self.portal.recebidos("ADMIN")
        completo = tempo("29/10/2026 às 14:34", "2026-10-29T17:34:00+00:00", "29/10/2026 às 14:34")
        self.assertEqual([payload["hora"], payload["instante"], payload["hora_completa"]],
                         tempo("29/10 14:34", "2026-10-29T17:34:00+00:00", "29/10/2026 às 14:34"))
        self.assertEqual(self.portal.sininho("ADMIN")[payload["id"]][0], "29/10 14:34")
        self.assertEqual(self.portal.lista("ADMIN")[id_chamado], completo)
        self.assertEqual(self.portal.lista("CLIENTE")[id_chamado][0], "29/10/2026")
        for papel in PAPEIS:
            conversa = self.portal.conversa(papel, id_chamado)
            self.assertEqual(conversa["mensagens"], [completo])
            self.assertEqual(conversa["aberto_em"], completo)
        # Gravado como relógio UTC sem fuso, não como horário local.
        for sql in ("SELECT data_criacao FROM notificacao", "SELECT data_envio FROM mensagem_suporte",
                    "SELECT data_abertura FROM chamado_suporte"):
            for bruto in self.gravados(sql):
                self.assertTrue(bruto.startswith("2026-10-29 17:34:00"), (sql, bruto))

    def test_b_virada_do_dia_usa_a_data_de_brasilia(self):
        self.portal.relogio(B)
        id_chamado = self.portal.abrir_chamado()
        (payload,) = self.portal.recebidos("ADMIN")
        self.assertEqual([payload["hora"], payload["instante"]], ["29/10 22:30", "2026-10-30T01:30:00+00:00"])
        completo = tempo("29/10/2026 às 22:30", "2026-10-30T01:30:00+00:00", "29/10/2026 às 22:30")
        self.assertEqual(self.portal.sininho("ADMIN")[payload["id"]][0], "29/10 22:30")
        self.assertEqual(self.portal.lista("ADMIN")[id_chamado], completo)
        self.assertEqual(self.portal.lista("CLIENTE")[id_chamado][0], "29/10/2026")  # e não 30/10
        for papel in PAPEIS:
            self.assertEqual(self.portal.conversa(papel, id_chamado)["aberto_em"], completo)
        # Logo depois da meia-noite em Brasília a data já é a do dia seguinte.
        self.portal.relogio(datetime(2026, 10, 30, 3, 5, tzinfo=UTC))
        outro = self.portal.abrir_chamado()
        self.assertEqual(self.portal.recebidos("ADMIN")[0]["hora"], "30/10 00:05")
        self.assertEqual(self.portal.lista("CLIENTE")[outro][0], "30/10/2026")

    # ---------------------------------------------------------------- C e D
    @unittest.skipUnless(hasattr(time, "tzset"), "Troca do fuso do processo exige POSIX (time.tzset).")
    def test_c_fuso_do_processo_nao_muda_o_horario_exibido(self):
        for minuto, zona in enumerate(("UTC", "Asia/Tokyo", "America/Noronha", "Atlantic/Azores",
                                       "Pacific/Kiritimati", "America/Los_Angeles", "America/Sao_Paulo")):
            instante = A + timedelta(minutes=minuto)
            hora = f"14:{34 + minuto}"
            with self.subTest(zona=zona), fuso_do_processo(zona):
                # O relógio de parede do processo de fato mudou.
                self.assertEqual(datetime.fromtimestamp(instante.timestamp()),
                                 instante.astimezone(ZoneInfo(zona)).replace(tzinfo=None))
                self.portal.relogio(instante)
                id_chamado = self.portal.abrir_chamado()
                (payload,) = self.portal.recebidos("ADMIN")
                self.assertEqual(payload["hora"], f"29/10 {hora}")
                self.assertEqual(self.portal.sininho("ADMIN")[payload["id"]][0], f"29/10 {hora}")
                self.assertEqual(self.portal.lista("ADMIN")[id_chamado][0], f"29/10/2026 às {hora}")
                self.assertEqual(self.portal.conversa("CLIENTE", id_chamado)["mensagens"][0][0],
                                 f"29/10/2026 às {hora}")

    @unittest.skipUnless(hasattr(time, "tzset"), "Troca do fuso do processo exige POSIX (time.tzset).")
    def test_d_relogio_de_outro_fuso_exibido_como_brasilia_adianta_o_deslocamento(self):
        """Ilustra o mecanismo do código anterior, que exibia sem conversão o relógio de
        parede de outro fuso (datetime.now() do processo e o relógio da sessão do
        banco): o adiantamento é o deslocamento entre esse fuso e Brasília. É 3 h em
        UTC (o padrão do Render e do Supabase) e 2 h num relógio em UTC−1, uma das
        hipóteses para as "cerca de 2 h" relatadas, não confirmada em produção. Por
        isso nenhuma subtração fixa serve. O código anterior foi executado nessas
        configurações pelo ensaio repro.py (pacote de evidências); aqui se confere que
        o código novo dá 14:34 em todas."""
        antigo = {"UTC": ("29/10 17:34", 3), "Atlantic/Azores": ("29/10 16:34", 2),
                  "America/Noronha": ("29/10 15:34", 1), "America/Sao_Paulo": ("29/10 14:34", 0)}
        for zona, (esperado, diferenca) in antigo.items():
            with self.subTest(zona=zona), fuso_do_processo(zona):
                # A mesma leitura do código removido (datetime.now().strftime) no instante A.
                exibido_antes = datetime.fromtimestamp(A.timestamp())
                self.assertEqual(exibido_antes.strftime("%d/%m %H:%M"), esperado)
                adiantamento = exibido_antes - A.astimezone(BRASILIA).replace(tzinfo=None)
                self.assertEqual(adiantamento, timedelta(hours=diferenca))
                self.portal.relogio(A)
                self.portal.abrir_chamado()
                self.assertEqual(self.portal.recebidos("ADMIN")[0]["hora"], "29/10 14:34")

    # -------------------------------------------------------------------- E
    def test_e_horario_de_verao_historico_pelas_regras_iana(self):
        casos = (
            (datetime(2018, 12, 15, 15, 0, tzinfo=UTC), "15/12/2018 às 13:00", -2),  # horário de verão
            (datetime(2019, 6, 15, 15, 0, tzinfo=UTC), "15/06/2019 às 12:00", -3),
            (datetime(2018, 11, 4, 2, 59, tzinfo=UTC), "03/11/2018 às 23:59", -3),   # véspera do início
            (datetime(2018, 11, 4, 3, 0, tzinfo=UTC), "04/11/2018 às 01:00", -2),    # 00:00 local não existiu
            (datetime(2019, 2, 17, 1, 59, tzinfo=UTC), "16/02/2019 às 23:59", -2),   # último minuto do verão
            (datetime(2019, 2, 17, 2, 0, tzinfo=UTC), "16/02/2019 às 23:00", -3),    # a hora 23 se repete
            (A, "29/10/2026 às 14:34", -3),                                          # sem horário de verão desde 2019
        )
        for instante, esperado, horas in casos:
            with self.subTest(instante=instante.isoformat()):
                self.assertEqual(horario_brasilia(instante), esperado)
                self.assertEqual(instante.astimezone(BRASILIA).utcoffset(), timedelta(hours=horas))
        # Ponta a ponta num dia de horário de verão: o sininho segue a regra da data.
        self.portal.relogio(casos[0][0])
        self.portal.abrir_chamado()
        (payload,) = self.portal.recebidos("ADMIN")
        self.assertEqual([payload["hora"], payload["instante"]], ["15/12 13:00", "2018-12-15T15:00:00+00:00"])
        self.assertEqual(self.portal.sininho("ADMIN")[payload["id"]][0], "15/12 13:00")

    # ---------------------------------------------------------------- F e G
    def test_f_payload_do_tempo_real_usa_o_instante_gravado(self):
        # Relógio que avança um minuto a cada leitura: qualquer leitura extra para
        # montar o payload mostraria um horário diferente do gravado.
        leituras = (A + timedelta(minutes=i) for i in range(10_000))
        self.app.config["NEXO_RELOGIO"] = lambda: next(leituras)
        id_chamado = self.portal.abrir_chamado()
        (payload,) = self.portal.recebidos("ADMIN")
        self.assertEqual(set(payload), CHAVES_PAYLOAD)
        self.assertTrue(all(isinstance(payload[c], str) for c in CHAVES_PAYLOAD - {"id"}))
        from models import ChamadoSuporte, Notificacao
        self.db.session.expire_all()
        gravado = self.db.session.get(Notificacao, payload["id"]).data_criacao
        self.assertEqual(gravado.utcoffset(), timedelta(0))
        self.assertEqual(payload["instante"], gravado.isoformat(timespec="seconds"))
        self.assertEqual(payload["hora"], gravado.astimezone(BRASILIA).strftime("%d/%m %H:%M"))
        # Cada evento tem o próprio instante: nada força abertura = mensagem = notificação.
        chamado = self.db.session.get(ChamadoSuporte, id_chamado)
        self.assertLess(chamado.data_abertura, chamado.mensagens[0].data_envio)
        self.assertLess(chamado.mensagens[0].data_envio, gravado)

    def test_f_resposta_e_resolucao_leem_o_relogio_uma_vez(self):
        """Na resposta, a mensagem e a atualização do chamado são o mesmo evento; na
        resolução, a atualização e o fechamento. Com um relógio que avança um minuto a
        cada leitura, duas leituras deixariam um minuto de diferença entre eles."""
        id_chamado = self.portal.abrir_chamado()
        leituras = (A + timedelta(minutes=i) for i in range(1, 10_000))
        self.app.config["NEXO_RELOGIO"] = lambda: next(leituras)
        for papel, responder in (("ADMIN", self.portal.responder_admin), ("CLIENTE", self.portal.responder_cliente)):
            with self.subTest(resposta=papel):
                responder(id_chamado)
                ultima = self.portal.conversa("ADMIN", id_chamado)["mensagens"][-1]
                painel = self.portal.painel_admin(id_chamado)
                self.assertEqual(painel["Última mensagem"], ultima)
                self.assertEqual(painel["Última atualização"], ultima)
                self.assertEqual(self.portal.lista("ADMIN")[id_chamado], ultima)
        with self.subTest(acao="resolução"):
            self.portal.alterar_status(id_chamado, "RESOLVIDO")
            painel = self.portal.painel_admin(id_chamado)
            self.assertEqual(painel["Resolvido em"], painel["Última atualização"])
            self.assertEqual(self.portal.conversa("CLIENTE", id_chamado)["resolvido_em"], painel["Resolvido em"])

    def test_g_sininho_recarregado_mostra_o_texto_do_tempo_real(self):
        id_chamado = self.portal.abrir_chamado()
        self.portal.relogio(datetime(2026, 10, 29, 18, 45, tzinfo=UTC))
        self.portal.responder_admin(id_chamado)
        for papel, hora in (("ADMIN", "29/10 14:34"), ("CLIENTE", "29/10 15:45")):
            (payload,) = self.portal.recebidos(papel)
            self.assertEqual(payload["hora"], hora)
            self.assertEqual(self.portal.sininho(papel)[payload["id"]],
                             [payload["hora"], payload["instante"], payload["hora_completa"]])

    # ---------------------------------------------------------------- H e I
    def test_h_admin_e_cliente_veem_o_mesmo_instante_de_cada_mensagem(self):
        id_chamado = self.portal.abrir_chamado()
        for minutos in (11, 47):
            self.portal.relogio(A + timedelta(minutes=minutos))
            self.portal.responder_admin(id_chamado)
            self.portal.relogio(A + timedelta(minutes=minutos + 2))
            self.portal.responder_cliente(id_chamado)
        admin = self.portal.conversa("ADMIN", id_chamado)["mensagens"]
        self.assertEqual(admin, self.portal.conversa("CLIENTE", id_chamado)["mensagens"])
        self.assertEqual([texto for texto, _iso, _titulo in admin],
                         [f"29/10/2026 às {h}" for h in ("14:34", "14:45", "14:47", "15:21", "15:23")])

    def test_i_roteiro_completo_de_um_chamado(self):
        conferir_roteiro(self, executar_roteiro(self.portal))

    def test_i_demais_gatilhos_do_sininho_usam_o_mesmo_horario(self):
        """Envio de relatório (admins), processamento e publicação (clientes): os
        textos reais dos gatilhos, pelos mesmos helpers que as rotas chamam."""
        import notifications
        gatilhos = (
            (notifications.notificar_admins, (), "ADMIN",
             "Nova planilha de Vendas enviada por Teste 1 aguardando homologação.", "/admin/analise/1/upload"),
            (notifications.notificar_clientes_empresa, (self.empresa_cliente(),), "CLIENTE",
             "Recebemos e processamos seus relatórios de 01/2026. Sua devolutiva está em preparação.", "/cliente/dashboard"),
            (notifications.notificar_clientes_empresa, (self.empresa_cliente(),), "CLIENTE",
             "Seu Relatório Estratégico de 01/2026 já está disponível! Clique para ver.", "/cliente/analise/1"),
        )
        for minutos, (funcao, argumentos, papel, texto_aviso, link) in enumerate(gatilhos, start=1):
            instante = B + timedelta(minutes=minutos)
            with self.subTest(aviso=texto_aviso):
                self.portal.relogio(instante)
                funcao(*argumentos, texto_aviso, link)
                self.db.session.commit()
                (payload,) = self.portal.recebidos(papel)
                esperado = tempo(f"29/10 22:{30 + minutos}", instante.isoformat(), f"29/10/2026 às 22:{30 + minutos}")
                self.assertEqual([payload["hora"], payload["instante"], payload["hora_completa"]], esperado)
                self.assertEqual(self.portal.sininho(papel)[payload["id"]], esperado)
                self.assertEqual(self.portal.recebidos("CLIENTE" if papel == "ADMIN" else "ADMIN"), [])

    # -------------------------------------------------------------------- K
    def test_k_registros_legados_no_relogio_utc_sem_deslocamento(self):
        """O código anterior gravava o relógio UTC (CURRENT_TIMESTAMP do SQLite e da
        sessão em UTC). Esses valores são lidos como UTC e exibidos em Brasília."""
        db = self.db
        db.session.execute(text(
            "INSERT INTO notificacao (id_usuario, texto, lida, data_criacao) "
            "VALUES (:u, 'Aviso legado fictício', 0, '2026-10-28 13:05:00')"), {"u": self.admin_id})
        db.session.execute(text(
            "INSERT INTO chamado_suporte (id_empresa, id_usuario_cliente, assunto, categoria, status_chamado, "
            "prioridade, data_abertura) VALUES (:e, :c, 'Chamado legado fictício', 'FINANCEIRO', 'ABERTO', "
            "'MEDIA', '2026-10-28 13:05:00')"), {"e": self.empresa_cliente(), "c": self.cliente_id})
        id_legado = db.session.scalar(text("SELECT max(id_chamado) FROM chamado_suporte"))
        db.session.execute(text(
            "INSERT INTO mensagem_suporte (id_chamado, id_usuario_remetente, mensagem, lida, data_envio) "
            "VALUES (:ch, :c, 'Mensagem legada fictícia', 0, '2026-10-28 13:05:00')"),
            {"ch": id_legado, "c": self.cliente_id})
        # Gravado pelo DEFAULT CURRENT_TIMESTAMP, como faziam as rotas antigas.
        db.session.execute(text("INSERT INTO notificacao (id_usuario, texto, lida) VALUES (:u, 'Aviso padrão', 0)"),
                           {"u": self.admin_id})
        db.session.commit()
        antes = self.gravados("SELECT data_criacao FROM notificacao ORDER BY id_notificacao")
        id_novo = self.portal.abrir_chamado()  # novo, às 14:34 de 29/10
        sininho = self.portal.sininho("ADMIN")
        ids = sorted(sininho)
        legado, padrao, novo = (sininho[i] for i in ids)
        self.assertEqual(legado, tempo("28/10 10:05", "2026-10-28T13:05:00+00:00", "28/10/2026 às 10:05"))
        agora_real = datetime.now(UTC)
        self.assertLess(abs(datetime.fromisoformat(padrao[1]) - agora_real), timedelta(minutes=2))
        self.assertEqual(novo[0], "29/10 14:34")
        lista = self.portal.lista("ADMIN")
        self.assertEqual(lista[id_legado][0], "28/10/2026 às 10:05")
        self.assertEqual(lista[id_novo][0], "29/10/2026 às 14:34")
        self.assertEqual(self.portal.conversa("CLIENTE", id_legado)["mensagens"][0][0], "28/10/2026 às 10:05")
        # Ler não regrava: os valores antigos continuam como estavam.
        self.assertEqual(self.gravados("SELECT data_criacao FROM notificacao ORDER BY id_notificacao")[:2], antes)

    def test_k_escrita_converte_qualquer_fuso_para_o_relogio_utc(self):
        """Instantes com fuso de Brasília ou de Tóquio, gravados pelo ORM nas cinco
        colunas, ficam como o relógio UTC (17:34) e aparecem 14:34."""
        from models import ChamadoSuporte, MensagemSuporte, Notificacao
        for zona in ("America/Sao_Paulo", "Asia/Tokyo"):
            with self.subTest(zona=zona):
                local = A.astimezone(ZoneInfo(zona))
                chamado = ChamadoSuporte(id_empresa=self.empresa_cliente(), id_usuario_cliente=self.cliente_id,
                                         assunto=f"Chamado fictício {zona}", categoria="FINANCEIRO",
                                         data_abertura=local, data_atualizacao=local, data_fechamento=local)
                self.db.session.add(chamado)
                self.db.session.flush()
                mensagem = MensagemSuporte(id_chamado=chamado.id_chamado, id_usuario_remetente=self.cliente_id,
                                           mensagem="Mensagem fictícia.", data_envio=local)
                aviso = Notificacao(id_usuario=self.admin_id, texto=f"Aviso fictício {zona}", data_criacao=local)
                self.db.session.add_all([mensagem, aviso])
                self.db.session.commit()
                brutos = self.db.session.execute(text(
                    "SELECT c.data_abertura, c.data_atualizacao, c.data_fechamento, m.data_envio, n.data_criacao "
                    "FROM chamado_suporte c JOIN mensagem_suporte m ON m.id_chamado = c.id_chamado, notificacao n "
                    "WHERE c.id_chamado = :c AND n.id_notificacao = :n"),
                    {"c": chamado.id_chamado, "n": aviso.id_notificacao}).one()
                self.assertTrue(all(str(b).startswith("2026-10-29 17:34:00") for b in brutos), brutos)
                self.assertEqual(self.portal.lista("ADMIN")[chamado.id_chamado][0], "29/10/2026 às 14:34")
                self.assertEqual(self.portal.sininho("ADMIN")[aviso.id_notificacao][0], "29/10 14:34")

    def empresa_cliente(self):
        from models import Usuario
        return self.db.session.get(Usuario, self.cliente_id).id_empresa

    # -------------------------------------------------------------------- L
    def test_l_nada_e_entregue_apos_rollback(self):
        import notifications
        notifications.criar_notificacao(self.admin_id, "Aviso fictício revertido", "/admin/tickets")
        self.db.session.rollback()
        self.assertEqual(self.portal.recebidos("ADMIN"), [])
        notifications.criar_notificacao(self.admin_id, "Aviso fictício confirmado", "/admin/tickets")
        self.db.session.commit()
        (payload,) = self.portal.recebidos("ADMIN")
        self.assertEqual((payload["texto"], payload["hora"]), ("Aviso fictício confirmado", "29/10 14:34"))
        self.assertEqual(self.portal.recebidos("CLIENTE"), [])

    def test_l_texto_do_chamado_nao_entra_nos_campos_de_horario(self):
        assunto = '<img src=x onerror="alert(1)"> 23:59 30/12/1999'
        self.portal.abrir_chamado(assunto=assunto)
        (payload,) = self.portal.recebidos("ADMIN")
        self.assertIn(assunto, payload["texto"])  # escapado pelo JavaScript (teste de navegador)
        self.assertRegex(payload["hora"], r"^\d{2}/\d{2} \d{2}:\d{2}$")
        self.assertRegex(payload["instante"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
        self.assertRegex(payload["hora_completa"], r"^\d{2}/\d{2}/\d{4} às \d{2}:\d{2} \(Horário de Brasília\)$")
        pagina = self.portal.pagina("ADMIN", "/admin/tickets")
        self.assertNotIn("<img src=x", pagina)
        self.assertEqual(self.portal.sininho("ADMIN")[payload["id"]][0], "29/10 14:34")
        self.assertEqual(self.portal.recebidos("CLIENTE"), [])

    def test_l_horario_sem_fuso_e_recusado(self):
        ingenuo = datetime(2026, 10, 29, 17, 34)
        for funcao in (horario_brasilia, iso_utc):
            with self.assertRaises(ValueError):
                funcao(ingenuo)
        from models import Notificacao
        self.db.session.add(Notificacao(id_usuario=self.admin_id, texto="Aviso fictício", data_criacao=ingenuo))
        with self.assertRaises(Exception) as contexto:
            self.db.session.flush()
        self.assertIn("fuso horário explícito", str(contexto.exception))
        self.db.session.rollback()
        self.assertEqual(self.portal.recebidos("ADMIN"), [])


# ---------------------------------------------------------------------------
# J: PostgreSQL descartável, num processo separado
# ---------------------------------------------------------------------------
def _ambiente_seguro():
    """Nunca herdar DATABASE_URL, PGSERVICE, PGPASSWORD ou chaves de serviços."""
    permitidas = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    ambiente = {chave: valor for chave, valor in os.environ.items() if chave.upper() in permitidas}
    ambiente.update(PYTHONUTF8="1", FLASK_ENV="development", NEXO_DB_DIRECT="0",
                    NEXO_TEST_POSTGRES_BIN=os.environ.get("NEXO_TEST_POSTGRES_BIN", ""))
    return ambiente


class HorarioBrasiliaPostgreSQLTests(unittest.TestCase):
    def test_j_postgresql_mostra_o_mesmo_horario_em_qualquer_timezone_de_sessao(self):
        if not os.environ.get("NEXO_TEST_POSTGRES_BIN"):
            self.skipTest("Defina NEXO_TEST_POSTGRES_BIN para criar um PostgreSQL descartável.")
        resultado = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--ensaio-postgresql"],
            cwd=str(ROOT), env=_ambiente_seguro(), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600, check=False,
        )
        try:
            relatorio = json.loads(resultado.stdout)
        except json.JSONDecodeError:
            self.fail("O ensaio PostgreSQL não produziu um relatório JSON (saída omitida).")
        self.assertEqual(resultado.returncode, 0, relatorio.get("erro"))
        self.assertFalse(relatorio["acesso_a_producao"])
        self.assertEqual(relatorio["fuso_do_processo"], "JST")  # servidor fora de UTC e de Brasília
        self.assertEqual(sorted(relatorio["zonas"]), sorted(ZONAS_SESSAO_PG))
        utc_esperado = {"2026-10-29 17:34:00", "2026-10-29 17:52:00", "2026-10-29 18:03:00",
                        "2026-10-29 18:27:00", "2026-10-29 18:40:00", "2026-10-30 01:30:00"}
        antes = {"UTC": ("28/10 13:05", 3), "America/Sao_Paulo": ("28/10 10:05", 0),
                 "Atlantic/Azores": ("28/10 12:05", 2), "Asia/Tokyo": ("28/10 22:05", 12)}
        for zona, dados in relatorio["zonas"].items():
            with self.subTest(timezone_da_sessao=zona):
                self.assertEqual(dados["show_time_zone"], zona)
                conferir_roteiro(self, dados["roteiro"])  # o mesmo que em SQLite (teste I)
                # Gravado como relógio UTC qualquer que seja o TimeZone da sessão.
                self.assertEqual(set(dados["notificacoes_gravadas"]), utc_esperado)
                self.assertEqual(dados["gravado_com_fuso_de_toquio"], "2026-10-29 17:34:00")
                self.assertEqual(dados["chamado_gravado"],
                                 ["2026-10-29 17:34:00", "2026-10-30 01:30:00", "2026-10-29 18:27:00"])
                # Como o código anterior gravava (pela sessão) e exibia (sem conversão):
                # o adiantamento é o deslocamento da sessão em relação a Brasília.
                exibido_antes, horas = antes[zona]
                legado = dados["legado"]
                self.assertEqual(datetime.fromisoformat(legado["gravado"]).strftime("%d/%m %H:%M"), exibido_antes)
                self.assertEqual(datetime.fromisoformat(legado["gravado"])
                                 - datetime(2026, 10, 28, 10, 5), timedelta(hours=horas))
                self.assertEqual(legado["gravado_depois_da_leitura"], legado["gravado"])
                diagnostico = dados["diagnostico"]
                self.assertTrue(diagnostico["transacao_somente_leitura"])
                self.assertEqual(diagnostico["timezone_da_sessao"], zona)
                self.assertEqual(diagnostico["origem_da_configuracao"], "database")
                self.assertEqual(set(diagnostico["tipos_das_colunas"].values()), {"timestamp without time zone"})
                self.assertEqual(diagnostico["linhas_antes_e_depois"][0], diagnostico["linhas_antes_e_depois"][1])
                pela_url = diagnostico["pela_url"]  # com PGTZ=UTC e PGOPTIONS no ambiente, ignorados
                self.assertEqual((pela_url["timezone_da_sessao"], pela_url["origem_da_configuracao"]),
                                 (zona, "database"))
                self.assertEqual(pela_url["banco"], diagnostico["banco"])
                if zona == "UTC":
                    # K: o legado da convenção de produção aparece sem deslocamento.
                    self.assertEqual(legado["exibido"], tempo("28/10 10:05", "2026-10-28T13:05:00+00:00",
                                                              "28/10/2026 às 10:05"))
                    self.assertTrue(diagnostico["compativel"])
                    self.assertEqual((pela_url["codigo"], pela_url["situacao"]), (0, "compativel"))
                else:
                    # Em outra sessão o legado não está em UTC: o diagnóstico avisa antes.
                    self.assertFalse(diagnostico["compativel"])
                    self.assertIn("ATENÇÃO", diagnostico["veredito"])
                    self.assertEqual((pela_url["codigo"], pela_url["situacao"]), (1, "verificar"))
        sem_esquema = relatorio["banco_sem_esquema"]
        self.assertEqual((sem_esquema["codigo"], sem_esquema["situacao"], sem_esquema["compativel"]),
                         (2, "inconclusivo", False))
        self.assertEqual(sem_esquema["tipos_das_colunas"], {})


class DiagnosticoFusoHorarioTests(unittest.TestCase):
    """Sem URL PostgreSQL válida, o diagnóstico não consulta nada e não exibe a senha."""

    def executar(self, url):
        from scripts import diagnosticar_fuso_horario as diagnostico
        saida, erros = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"DATABASE_URL": url}), patch("dotenv.load_dotenv", return_value=False), \
                contextlib.redirect_stdout(saida), contextlib.redirect_stderr(erros):
            codigo = diagnostico.main()
        return codigo, saida.getvalue(), erros.getvalue()

    def test_url_malformada_sai_com_codigo_2_sem_a_senha(self):
        # Senha colada sem percent-encoding: o "?" vira início da query e a porta fica inválida.
        for url in ("postgresql://postgres.ficticio:MinhaSenha?Forte123@pooler.example.invalid:6543/postgres",
                    "postgresql://postgres.ficticio:OutraSenha@pooler.example.invalid:porta/postgres"):
            with self.subTest(url=url.split("@")[-1]):
                codigo, saida, erros = self.executar(url)
                self.assertEqual(codigo, 2)
                self.assertIn("Diagnóstico não executado", json.loads(saida)["erro"])
                self.assertNotIn("Senha", saida + erros)

    def test_host_remoto_sem_sslmode_exige_tls(self):
        from sqlalchemy.engine import make_url
        from scripts.diagnosticar_fuso_horario import parametros_tls
        remoto = "postgresql://usuario:ficticia@pooler.example.invalid:6543/postgres"
        sem_tls = {k: v for k, v in os.environ.items() if k not in ("PGSSLMODE", "PGREQUIRESSL")}
        with patch.dict(os.environ, sem_tls, clear=True):
            self.assertEqual(parametros_tls(make_url(remoto)), {"sslmode": "require"})
            for url in (remoto + "?sslmode=verify-full", "postgresql://usuario@/nexo?host=/tmp/socket",
                        "postgresql://usuario@localhost/nexo", "postgresql://usuario@127.0.0.1/nexo"):
                self.assertEqual(parametros_tls(make_url(url)), {}, url)
        with patch.dict(os.environ, {"PGSSLMODE": "verify-full"}):
            self.assertEqual(parametros_tls(make_url(remoto)), {})  # vale o do terminal

    def test_variaveis_de_alvo_e_sessao_sao_ignoradas_e_as_de_tls_preservadas(self):
        from scripts import diagnosticar_fuso_horario as diagnostico
        ambiente = {"PGTZ": "UTC", "PGOPTIONS": "-c TimeZone=UTC", "PGHOST": "outro.example.invalid",
                    "PGSERVICE": "outro", "PGSSLMODE": "verify-full", "PGSSLROOTCERT": "/caminho/raiz.crt",
                    "PGREQUIREAUTH": "scram-sha-256"}
        with patch.dict(os.environ, dict(ambiente, DATABASE_URL="sqlite:///:memory:")), \
                patch("dotenv.load_dotenv", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(diagnostico.main(), 2)
            restantes = {chave for chave in ambiente if chave in os.environ}
        self.assertEqual(restantes, {"PGSSLMODE", "PGSSLROOTCERT", "PGREQUIREAUTH"})

    def test_url_que_nao_e_postgresql_nao_consulta_nada(self):
        codigo, saida, _erros = self.executar("sqlite:///:memory:")
        self.assertEqual(codigo, 2)
        self.assertIn("não aponta para PostgreSQL", json.loads(saida)["erro"])


def _app_postgresql(pg, banco, pasta):
    with patch("dotenv.load_dotenv", return_value=False):
        import app as app_module
        from config import DevelopmentConfig
    from flask import Flask
    uri = pg.url(banco)
    valores = {
        "SECRET_KEY": "isolated-timezone-test-key",
        "SQLALCHEMY_DATABASE_URI": uri, "SQLALCHEMY_DIRECT_URI": uri,
        "SQLALCHEMY_ENGINE_OPTIONS": {"pool_pre_ping": True, "connect_args": {
            "connect_timeout": 5, "password": "", "passfile": str(pg.passfile),
            "options": "-c statement_timeout=15000 -c lock_timeout=10000",
        }},
        "UPLOAD_FOLDER": pasta / "uploads", "DEBUG": False, "SQLALCHEMY_ECHO": False, "TESTING": True,
        "MAIL_ATIVO": False, "MAIL_SUPPRESS_SEND": True, "MAIL_USERNAME": None, "MAIL_PASSWORD": None,
        "BREVO_API_KEY": None, "ANTHROPIC_API_KEY": None, "HF_API_TOKEN": None,
        "RATELIMIT_ENABLED": False, "RATELIMIT_STORAGE_URI": "memory://", "PROXY_FIX_HOPS": 0,
    }
    with contextlib.ExitStack() as configuracao:
        for chave, valor in valores.items():
            configuracao.enter_context(patch.object(DevelopmentConfig, chave, valor, create=True))
        configuracao.enter_context(patch.object(app_module, "Flask", side_effect=lambda *a, **kw: Flask(
            *a, instance_path=str(pasta / "instance"), **kw)))
        return app_module.create_app("development")


def _ensaio_em_zona(pg, zona, pasta):
    from flask_migrate import upgrade
    from psycopg2 import sql
    from extensions import db
    from scripts.diagnosticar_fuso_horario import diagnosticar
    banco = pg.create_database()
    try:
        with contextlib.closing(pg.connect()) as conexao:
            conexao.autocommit = True
            with conexao.cursor() as cursor:
                cursor.execute(sql.SQL("ALTER DATABASE {} SET timezone TO {}").format(
                    sql.Identifier(banco), sql.Literal(zona)))
        app = _app_postgresql(pg, banco, pasta)
        base = types.SimpleNamespace(db=db)
        with app.app_context():
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                upgrade(directory=str(ROOT / "migrations"))
            DeployTestCase.fixtures(base)  # os mesmos usuários fictícios do SQLite
            show = db.session.scalar(text("SHOW TIME ZONE"))
        portal = Portal(app, base.password)
        try:
            roteiro = executar_roteiro(portal)
            with app.app_context():
                # Como o código anterior gravava: o instante passava pelo TimeZone da sessão.
                db.session.execute(text(
                    "INSERT INTO notificacao (id_usuario, texto, lida, data_criacao) VALUES "
                    "(:u, 'Aviso legado fictício', false, (TIMESTAMPTZ '2026-10-28 13:05:00+00')::timestamp)"),
                    {"u": base.admin_id})
                db.session.commit()
                id_legado = db.session.scalar(text("SELECT max(id_notificacao) FROM notificacao"))
                ler = text("SELECT data_criacao::text FROM notificacao WHERE id_notificacao = :i")
                gravado = db.session.scalar(ler, {"i": id_legado})
                # Instante com fuso de Tóquio pelo ORM: o tipo da coluna converte para UTC.
                from models import Notificacao
                toquio = Notificacao(id_usuario=base.admin_id, texto="Aviso fictício com fuso de Tóquio",
                                     data_criacao=A.astimezone(ZoneInfo("Asia/Tokyo")))
                db.session.add(toquio)
                db.session.commit()
                gravado_toquio = db.session.scalar(ler, {"i": toquio.id_notificacao})
            exibido = portal.sininho("ADMIN")[id_legado]
        finally:
            portal.fechar()
        with app.app_context():
            gravado_depois = db.session.scalar(ler, {"i": id_legado})
            notificacoes = [v for v in db.session.execute(text(
                "SELECT data_criacao::text FROM notificacao WHERE id_notificacao <> :i"), {"i": id_legado}).scalars()]
            chamado = list(db.session.execute(text(
                "SELECT data_abertura::text, data_atualizacao::text, data_fechamento::text FROM chamado_suporte")).one())
            contar = text("SELECT (SELECT count(*) FROM notificacao) + (SELECT count(*) FROM mensagem_suporte)")
            linhas_antes = db.session.scalar(contar)
            db.session.remove()
            db.engine.dispose()
        with contextlib.closing(pg.connect(banco)) as conexao:
            diagnostico = diagnosticar(conexao)
        # Pela URL, como o proprietário roda: PGTZ/PGOPTIONS do terminal não valem.
        diagnostico["pela_url"] = _diagnostico_pela_url(
            pg, banco, {"PGTZ": "UTC", "PGOPTIONS": "-c TimeZone=UTC"})
        with app.app_context():
            diagnostico["linhas_antes_e_depois"] = [linhas_antes, db.session.scalar(contar)]
            db.session.remove()
            db.engine.dispose()
    finally:
        pg.drop_database(banco)
    return {"show_time_zone": show, "roteiro": roteiro, "notificacoes_gravadas": notificacoes,
            "chamado_gravado": chamado, "diagnostico": diagnostico, "gravado_com_fuso_de_toquio": gravado_toquio,
            "legado": {"gravado": gravado, "exibido": exibido, "gravado_depois_da_leitura": gravado_depois}}


def _diagnostico_pela_url(pg, banco, ambiente):
    """scripts/diagnosticar_fuso_horario.main() com DATABASE_URL para o banco privado."""
    from scripts import diagnosticar_fuso_horario as diagnostico
    url = f"postgresql://{pg.username}@/{banco}?host={pg.socket}&port={pg.port}"
    saida = io.StringIO()
    with patch.dict(os.environ, dict(ambiente, DATABASE_URL=url)), contextlib.redirect_stdout(saida):
        codigo = diagnostico.main()
    return {"codigo": codigo, **json.loads(saida.getvalue())}


def _ensaio_postgresql():
    from tests.postgresql_support import PrivatePostgreSQL
    relatorio = {"acesso_a_producao": False, "zonas": {}}
    pg = PrivatePostgreSQL()
    # PrivatePostgreSQL troca os.environ por um ambiente saneado; depois disso, o
    # processo passa a rodar com o relógio em Tóquio (nem UTC nem Brasília).
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    relatorio["fuso_do_processo"] = datetime.now().astimezone().tzname()
    try:
        with tempfile.TemporaryDirectory(prefix="nexo-horario-pg-") as pasta:
            for zona in ZONAS_SESSAO_PG:
                relatorio["zonas"][zona] = _ensaio_em_zona(pg, zona, Path(pasta))
            # Banco em UTC sem as tabelas do NEXO: o diagnóstico não pode dizer "compatível".
            vazio = pg.create_database()
            try:
                relatorio["banco_sem_esquema"] = _diagnostico_pela_url(pg, vazio, {})
            finally:
                pg.drop_database(vazio)
    finally:
        pg.close()
    return relatorio


# ---------------------------------------------------------------------------
# Navegador: Socket.IO de verdade, dois papéis, fusos de navegador diferentes
# ---------------------------------------------------------------------------
try:
    from playwright.sync_api import sync_playwright
except ImportError:  # dependência opcional de desenvolvimento
    sync_playwright = None

_NAVEGADOR = os.environ.get("NEXO_TEST_BROWSER") == "1" and sync_playwright is not None
_SOCKETIO_URL = "https://cdn.socket.io/4.7.5/socket.io.min.js"
# sha384 oficial do socket.io.min.js 4.7.5 (o mesmo arquivo em cdn.socket.io e no jsdelivr).
_SOCKETIO_SRI = "2huaZvOR9iDzHqslqwpR87isEmrfxqyWOF7hr7BY6KG0+hVKLoEXMPUJw3ynWuhO"
# Expõe ao teste o socket criado pelo base.html (que o guarda numa closure).
_CAPTURAR_SOCKET = """
(() => {
  let original;
  Object.defineProperty(window, 'io', {
    configurable: true,
    get() { return original; },
    set(valor) {
      original = function () { const s = valor.apply(this, arguments); window.__nexoSocket = s; return s; };
      Object.assign(original, valor);
    },
  });
})();
"""


# Assunto do chamado com HTML: precisa aparecer como texto no sininho em tempo real.
ASSUNTO_COM_HTML = 'Dúvida <img src=x onerror="window.__xss=1"> fictícia'


def _socketio_local():
    pasta = os.environ.get("NEXO_TEST_CDN_DIR")
    arquivo = Path(pasta) / "socket.io.min.js" if pasta else None
    if not arquivo or not arquivo.is_file():
        return None
    resumo = base64.b64encode(hashlib.sha384(arquivo.read_bytes()).digest()).decode()
    return str(arquivo) if resumo == _SOCKETIO_SRI else None


@unittest.skipUnless(_NAVEGADOR, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class HorarioBrasiliaNavegadorTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.socketio_local = _socketio_local()
        if not self.socketio_local:
            self.skipTest("Cliente Socket.IO 4.7.5 íntegro indisponível (NEXO_TEST_CDN_DIR/socket.io.min.js).")
        from werkzeug.serving import make_server
        from tests.test_contratos_navegador import _copias_locais_da_cdn
        from tests.test_nexobot_navegador import _CONECTAR_LOCAL
        from tests.test_overflow_navegador import _PLOTLY_URL, _plotly_local
        self.fixtures()
        self.agora = A
        self.app.config["NEXO_RELOGIO"] = lambda: self.agora
        for alvo, original in _CONECTAR_LOCAL.items():
            self.stack.enter_context(patch(alvo, original))
        self.servidor = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.base = f"http://127.0.0.1:{self.servidor.server_port}"
        # A mesma verificação de origem do produto (SOCKETIO_CORS_ORIGINS), com a
        # origem exata deste servidor, que só é conhecida depois de abri-lo.
        from extensions import socketio
        socketio.server.eio.cors_allowed_origins = [self.base]
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        self.addCleanup(self._parar_servidor)
        self.cdn_local = _copias_locais_da_cdn()
        if _plotly_local():
            self.cdn_local[_PLOTLY_URL] = (_plotly_local(), "application/javascript")
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.navegador = self.pw.chromium.launch(executable_path=os.environ.get("NEXO_TEST_CHROMIUM") or None)
        self.addCleanup(self.navegador.close)
        self.erros_js = []

    def _parar_servidor(self):
        self.servidor.shutdown()
        self.servidor.server_close()

    def tearDown(self):
        self.assertEqual(self.erros_js, [], "Erros de JavaScript na página")

    def _rota(self, rota):
        url = rota.request.url
        if url.startswith(self.base + "/"):
            rota.continue_()
        elif url == _SOCKETIO_URL:
            rota.fulfill(status=200, path=self.socketio_local, content_type="application/javascript")
        elif url in self.cdn_local:
            caminho, tipo = self.cdn_local[url]
            rota.fulfill(status=200, path=caminho, content_type=tipo)
        elif rota.request.resource_type in ("stylesheet", "script"):
            tipo = "text/css" if rota.request.resource_type == "stylesheet" else "application/javascript"
            rota.fulfill(status=200, content_type=tipo, body="")
        else:
            rota.abort()

    def abrir(self, papel, fuso_do_navegador):
        contexto = self.navegador.new_context(viewport={"width": 1366, "height": 900}, locale="pt-BR",
                                              timezone_id=fuso_do_navegador)
        self.addCleanup(contexto.close)
        contexto.add_init_script(_CAPTURAR_SOCKET)
        pagina = contexto.new_page()
        pagina.on("pageerror", lambda erro: self.erros_js.append(f"{papel}: {erro}"))
        pagina.route("**/*", self._rota)
        pagina.goto(self.base + "/auth/login", wait_until="load")
        pagina.fill("#email", f"{papel.lower()}-test@example.invalid")
        pagina.fill("#senha", self.password)
        with pagina.expect_navigation(wait_until="load"):
            pagina.click("button[type=submit]")
        self.assertEqual(pagina.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone"), fuso_do_navegador)
        return pagina

    def ir(self, pagina, caminho):
        resposta = pagina.goto(self.base + caminho, wait_until="load")
        self.assertEqual(resposta.status, 200, caminho)
        pagina.wait_for_function("() => window.__nexoSocket && window.__nexoSocket.connected", timeout=15000)

    def assert_sem_html_injetado(self, pagina):
        """O assunto com HTML chega como texto: sem <img>, sem script executado."""
        item = pagina.eval_on_selector("#bellList .bell-item", "a => [a.querySelectorAll('img, script').length, "
                                       "a.querySelector('.bell-msg').textContent]")
        self.assertEqual(item[0], 0)
        self.assertIn(ASSUNTO_COM_HTML, item[1])
        self.assertEqual(pagina.eval_on_selector_all("#nexoToasts img, #nexoToasts script", "els => els.length"), 0)
        self.assertIsNone(pagina.evaluate("() => window.__xss === undefined ? null : window.__xss"))

    @staticmethod
    def tempo_no_dom(pagina, seletor):
        return pagina.eval_on_selector(seletor, "t => [t.textContent, t.getAttribute('datetime'), t.title]")

    def test_navegador_tempo_real_recarga_e_conversa_no_horario_de_brasilia(self):
        admin = self.abrir("ADMIN", "Asia/Tokyo")
        cliente = self.abrir("CLIENTE", "America/Los_Angeles")
        self.ir(admin, "/admin/tickets")
        self.ir(cliente, "/cliente/tickets?novo=1")
        cliente.fill("input[name=assunto]", ASSUNTO_COM_HTML)
        cliente.select_option("select[name=categoria]", "DUVIDA_TECNICA")
        cliente.fill("textarea[name=mensagem]", "Mensagem fictícia do cliente.")
        with cliente.expect_navigation(wait_until="load"):
            cliente.click("#formNovoChamado button[type=submit]")
        id_chamado = int(re.search(r"/cliente/tickets/(\d+)", cliente.url).group(1))
        abertura = tempo("29/10/2026 às 14:34", "2026-10-29T17:34:00+00:00", "29/10/2026 às 14:34")
        self.assertEqual(self.tempo_no_dom(cliente, ".chat-time time"), abertura)
        # Tempo real no sininho do admin, sem recarregar.
        admin.wait_for_selector("#bellList .bell-item time", state="attached", timeout=15000)
        sino = tempo("29/10 14:34", "2026-10-29T17:34:00+00:00", "29/10/2026 às 14:34")
        self.assertEqual(self.tempo_no_dom(admin, "#bellList .bell-item time"), sino)
        self.assertEqual(admin.text_content("#bellBadge").strip(), "1")
        self.assert_sem_html_injetado(admin)
        # Recarga: o mesmo texto, agora vindo do banco.
        self.ir(admin, "/admin/tickets")
        self.assertEqual(self.tempo_no_dom(admin, "#bellList .bell-item time"), sino)
        # A Central de Suporte mostra os chamados em cartões (não há mais tabela nessa página): o
        # horário do cartão precisa estar na tela antes de o atributo ser lido.
        admin.wait_for_selector(".ticket-card time", timeout=5000)
        self.assertEqual(self.tempo_no_dom(admin, ".ticket-card time"), abertura)
        self.assert_sem_html_injetado(admin)
        # O admin responde às 14:52; o cliente recebe em tempo real.
        self.ir(admin, f"/admin/tickets/{id_chamado}")
        self.ir(cliente, f"/cliente/tickets/{id_chamado}")
        self.agora = datetime(2026, 10, 29, 17, 52, tzinfo=UTC)
        admin.fill("textarea[name=mensagem]", "Resposta fictícia da equipe.")
        with admin.expect_navigation(wait_until="load"):
            admin.click("form.chat-reply button[type=submit]")
        cliente.wait_for_selector("#bellList .bell-item time", state="attached", timeout=15000)
        self.assertEqual(self.tempo_no_dom(cliente, "#bellList .bell-item time"),
                         tempo("29/10 14:52", "2026-10-29T17:52:00+00:00", "29/10/2026 às 14:52"))
        resposta = tempo("29/10/2026 às 14:52", "2026-10-29T17:52:00+00:00", "29/10/2026 às 14:52")
        for pagina in (admin, cliente):
            self.ir(pagina, f"/{'admin' if pagina is admin else 'cliente'}/tickets/{id_chamado}")
            horarios = pagina.eval_on_selector_all(
                ".chat-time time", "ts => ts.map(t => [t.textContent, t.getAttribute('datetime'), t.title])")
            self.assertEqual(horarios, [abertura, resposta])


if __name__ == "__main__":
    if "--ensaio-postgresql" in sys.argv:
        relatorio_json = sys.stdout  # só o relatório sai por aqui; o resto vai para stderr
        try:
            with contextlib.redirect_stdout(sys.stderr):
                saida = _ensaio_postgresql()
            codigo = 0
        except Exception as erro:  # noqa: BLE001 - relatado ao teste pai, dados fictícios
            saida, codigo = {"acesso_a_producao": False, "erro": f"{type(erro).__name__}: {erro}"[:2000]}, 1
        print(json.dumps(saida, ensure_ascii=False), file=relatorio_json)
        sys.exit(codigo)
    unittest.main()
