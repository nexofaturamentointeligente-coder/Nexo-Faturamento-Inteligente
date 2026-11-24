"""Hardening complementar (FASE 6): teto do polling do sininho por usuário,
nome de produto com teto de tamanho/sem controles no ETL, e confinamento do
script de migração de imagens do Guia a static/."""

import unittest

from . import test_auth_tokens_security as token_cases
from . import test_session_revocation_security as session_cases


class SininhoPollingCapTests(token_cases._TokenCase):
    """O polling do sininho (substituiu o Socket.IO) tem teto por usuário: várias
    abas somam, e um cliente em laço recebe 429 sem ocupar o servidor."""

    ENABLE_LIMITS = True  # o Flask-Limiter só instala os limites se nasce ligado
    _login_client = session_cases.SessionRevocationCases._login_client

    def test_per_user_polling_is_capped(self):
        http = self._login_client()
        outra_aba = self.app.test_client()
        outra_aba.set_cookie(self.app.config["SESSION_COOKIE_NAME"],
                             http.get_cookie(self.app.config["SESSION_COOKIE_NAME"]).value)
        # Até o teto (30 por minuto), somando as duas abas: todas respondem.
        status = [(http if i % 2 else outra_aba).get("/notificacoes/novas?desde=0").status_code
                  for i in range(30)]
        self.assertEqual(set(status), {200})
        # A próxima excede o teto do usuário, em qualquer aba, e recebe JSON curto
        # (sem montar a página, que consultaria o banco a cada recusa).
        excedente = http.get("/notificacoes/novas?desde=0")
        self.assertEqual(excedente.status_code, 429)
        self.assertTrue(excedente.is_json)
        self.assertIn("Aguarde", excedente.get_json()["erro"])
        self.assertEqual(outra_aba.get("/notificacoes/novas?desde=0").status_code, 429)
        # O teto é do usuário: outro usuário, no mesmo IP, segue consultando; e sem
        # sessão a chave é o IP, que tem a sua própria contagem.
        self.assertEqual(self._login_client("other").get("/notificacoes/novas?desde=0").status_code, 200)
        self.assertEqual(self.app.test_client().get("/notificacoes/novas?desde=0").status_code, 401)


class ProductNameCapTests(unittest.TestCase):
    def _kpis(self, nome_produto):
        import tempfile
        from pathlib import Path
        from etl_processor import calcular_kpis_em_memoria
        with tempfile.TemporaryDirectory(prefix="nexo-nome-") as diretorio:
            vendas = Path(diretorio) / "vendas.csv"
            compras = Path(diretorio) / "compras.csv"
            vendas.write_text(f"Produto;Quantidade;Valor total\n{nome_produto};3;30,00\nOutro item;1;5,00\n",
                              encoding="utf-8")
            compras.write_text("Fornecedor;Valor NF\nFornecedor sintetico;40,00\n", encoding="utf-8")
            resultado = calcular_kpis_em_memoria(str(vendas), str(compras))
        self.assertTrue(resultado.sucesso, getattr(resultado, "erro", None))
        return resultado.kpis

    def test_oversized_product_name_is_length_capped_and_nul_stripped(self):
        from etl_processor import LIMITE_NOME_PRODUTO
        # NUL embutido + nome muito acima do teto (sem quebra de linha, que seria
        # separador de linha no CSV). O teto protege o banco e o layout.
        bruto = "Produto \x00" + ("X" * 400) + " fim"
        kpis = self._kpis(bruto)
        nome = kpis["produto_mais_vendido_nome"]
        self.assertEqual(len(nome), LIMITE_NOME_PRODUTO)
        self.assertNotIn("\x00", nome)
        self.assertTrue(nome.startswith("Produto X"))
        for produto in kpis["curva_abc"]["produtos"]:
            self.assertLessEqual(len(produto["nome"]), LIMITE_NOME_PRODUTO)


class GuiaImageContainmentTests(unittest.TestCase):
    def test_migration_refuses_paths_outside_static_or_non_images(self):
        import os
        import tempfile
        from migrar_arquivos_para_db import _imagem_contida
        with tempfile.TemporaryDirectory(prefix="nexo-static-") as static:
            dentro = os.path.join(static, "img")
            os.makedirs(dentro)
            with open(os.path.join(dentro, "logo.png"), "wb") as arquivo:
                arquivo.write(b"\x89PNG\r\n")
            # Dentro de static/ e com extensão de imagem: aceito.
            self.assertIsNotNone(_imagem_contida(static, "img/logo.png"))
            # Traversal para fora de static/: recusado.
            self.assertIsNone(_imagem_contida(static, "../../etc/passwd"))
            self.assertIsNone(_imagem_contida(static, "/etc/passwd"))
            # Extensão não-imagem, mesmo dentro de static/: recusado.
            with open(os.path.join(dentro, "nota.txt"), "w", encoding="utf-8") as arquivo:
                arquivo.write("x")
            self.assertIsNone(_imagem_contida(static, "img/nota.txt"))


if __name__ == "__main__":
    unittest.main()
