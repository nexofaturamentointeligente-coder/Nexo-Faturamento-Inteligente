"""Campos do formulário: nome, empresa, e-mail, WhatsApp, interesse e termos."""

import unittest

import validacao
from validacao import DadoInvalido

CONTATOS_DO_MAC = chr(0x202D) + "+55 (62) 98888-1234" + chr(0x202C)


class WhatsappTests(unittest.TestCase):
    def test_numeros_aceitos_ficam_so_com_ddd_e_numero(self):
        aceitos = {
            "+55 (62) 98888-1234": "62988881234", "0055 62 98888-1234": "62988881234",
            "+55 (062) 98888-1234": "62988881234", "0 62 98888-1234": "62988881234",
            "(62) 98888-1234": "62988881234", "62988881234": "62988881234", "(62) 3222-1234": "6232221234",
            "62.3222.1234": "6232221234", "(55) 99999-1234": "55999991234", "55 99999-1234": "55999991234",
            "+55 55 99999-1234": "55999991234", "(55) 3222-1234": "5532221234", "(0xx62) 3222-1234": "6232221234",
            CONTATOS_DO_MAC: "62988881234", "(62) 98888" + chr(0x2011) + "1234": "62988881234",
            "(62) 98888" + chr(0x2212) + "1234": "62988881234", " +55 11 98765-4321 ": "11987654321",
        }
        for valor, esperado in aceitos.items():
            with self.subTest(valor=valor):
                self.assertEqual(validacao.whatsapp(valor), esperado)

    def test_numeros_recusados_nunca_viram_outro(self):
        recusados = (
            "(55) 99276-54321", "559927654321", "5562988881234",  # 12 ou 13 dígitos sem "+"
            "+55 99999-8877", "0055 99999-8877",  # DDI sem DDD
            "+1 202 555 0100", "+51 987 654 321", "+7 991 123-45-67",  # estrangeiros
            "(55) 3222-1234 ramal 21", "(55) 3222-1234 21", "3222-1234 21", "98917-1218 (99)",  # ramal ou sobra
            "62 3222-1234 / 62 99999-8877", "Tel: (62) 98888-1234", "62 98888 12 34",
            "(62) 9888-1234", "(62) 99999-9999", "(20) 98888-1234", "0 15 62 98888-1234", "123", "6" * 61,
            "+55 62 98888+1234", None, 62988881234,
        )
        for valor in recusados:
            with self.subTest(valor=valor), self.assertRaises(DadoInvalido) as erro:
                validacao.whatsapp(valor)
            self.assertEqual(erro.exception.campo, "whatsapp")
        with self.assertRaises(DadoInvalido) as erro:
            validacao.whatsapp("   ")
        self.assertEqual(str(erro.exception), "Informe o WhatsApp com DDD.")


class DemaisCamposTests(unittest.TestCase):
    def test_email(self):
        self.assertEqual(validacao.email("  Maria.Silva+loja@Exemplo.COM.br "), "maria.silva+loja@exemplo.com.br")
        for valor in ("", None, "sem-arroba", "a@b", "a@@b.com", "a..b@exemplo.com", "a b@exemplo.com",
                      "<a>@exemplo.com", "a@exemplo.c", "x" * 65 + "@exemplo.com", "a@" + "b" * 250 + ".com"):
            with self.subTest(valor=valor), self.assertRaises(DadoInvalido):
                validacao.email(valor)

    def test_nome_e_empresa_numa_linha(self):
        self.assertEqual(validacao.texto_obrigatorio("  Maria \n\t Fictícia" + chr(0x200B) + " ", "nome", "x"),
                         "Maria Fictícia")
        for valor in ("", "   ", None, chr(0x200B), "x" * 121):
            with self.subTest(valor=valor), self.assertRaises(DadoInvalido):
                validacao.texto_obrigatorio(valor, "empresa", "Informe o nome da empresa.")
        self.assertEqual(len(validacao.texto_obrigatorio("x" * 120, "nome", "x")), 120)

    def test_interesse(self):
        self.assertEqual([validacao.interesse(valor) for valor in ("1", "2", " 3 ")],
                         ["raio_x", "ajuda_relatorios", "so_contato"])
        for valor in ("0", "4", "", None, "raio_x", "1.0", "01"):
            with self.subTest(valor=valor), self.assertRaises(DadoInvalido):
                validacao.interesse(valor)

    def test_termos(self):
        for valor in ("true", "TRUE", "1", "on", "sim", " Sim "):
            with self.subTest(valor=valor):
                self.assertTrue(validacao.termos_aceitos(valor))
        for valor in (None, "", "false", "0", "off", "nao", "yes please"):
            with self.subTest(valor=valor), self.assertRaises(DadoInvalido) as erro:
                validacao.termos_aceitos(valor)
            self.assertEqual(erro.exception.campo, "termos_aceitos")


if __name__ == "__main__":
    unittest.main()
