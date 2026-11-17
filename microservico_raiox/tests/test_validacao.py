"""Campos do formulário: nome, empresa, e-mail, WhatsApp, interesse e termos; e o IP do visitante para o aceite."""

import unittest

from repositorio import SIGLAS_DOS_DOCUMENTOS
import validacao
from validacao import MENSAGEM_TERMOS, DadoInvalido

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
            self.assertEqual((erro.exception.campo, str(erro.exception)), ("termos_aceitos", MENSAGEM_TERMOS))


class DocumentosLidosTests(unittest.TestCase):
    def test_os_dois_ids_viram_o_id_de_cada_tipo(self):
        self.assertEqual(validacao.documentos_lidos({"documento_termos": "7", "documento_privacidade": "123456789"}),
                         {"TERMOS_USO": 7, "POLITICA_PRIVACIDADE": 123456789})
        # Espaço em volta e caractere invisível de formatação saem, como nos outros campos.
        self.assertEqual(validacao.documentos_lidos({"documento_termos": " 7 ",
                                                     "documento_privacidade": "8" + chr(0x200B)}),
                         {"TERMOS_USO": 7, "POLITICA_PRIVACIDADE": 8})
        # Os tipos são os que o repositório confere com documento_legal.
        self.assertEqual(set(validacao.CAMPOS_DOS_DOCUMENTOS.values()), set(SIGLAS_DOS_DOCUMENTOS))

    def test_falta_ou_valor_que_nao_e_id_recusa_no_campo_dos_termos(self):
        recusados = (
            {}, {"documento_termos": "7"}, {"documento_privacidade": "8"},
            {"documento_termos": "", "documento_privacidade": "8"},
            {"documento_termos": "sete", "documento_privacidade": "8"},
            {"documento_termos": "7.0", "documento_privacidade": "8"},
            {"documento_termos": "-7", "documento_privacidade": "8"},
            {"documento_termos": "+7", "documento_privacidade": "8"},
            {"documento_termos": "0", "documento_privacidade": "8"},
            {"documento_termos": "07", "documento_privacidade": "8"},
            {"documento_termos": "1" * 10, "documento_privacidade": "8"},
            {"documento_termos": "7 8", "documento_privacidade": "9"},
            {"documento_termos": chr(0x0667), "documento_privacidade": "8"},  # o 7 árabe-índico
            {"documento_termos": chr(0xFF17), "documento_privacidade": "8"},  # o 7 de largura cheia
            {"documento_termos": 7, "documento_privacidade": 8},
            {"documento_termos": "7", "documento_privacidade": "7"},  # o mesmo id nos dois
        )
        for formulario in recusados:
            with self.subTest(formulario=formulario), self.assertRaises(DadoInvalido) as erro:
                validacao.documentos_lidos(formulario)
            self.assertEqual((erro.exception.campo, str(erro.exception)), ("termos_aceitos", MENSAGEM_TERMOS))


class IpDoVisitanteTests(unittest.TestCase):
    def test_normalizacao(self):
        casos = {
            "203.0.113.9": "203.0.113.9",
            " 203.0.113.9 ": "203.0.113.9",
            "::ffff:203.0.113.9": "203.0.113.9",
            "2001:0DB8:0000:0000:0000:0000:0000:0001": "2001:db8::1",
            "2001:db8:1:2:3:4:5:6": "2001:db8:1:2:3:4:5:6",
            "::1": "::1",
        }
        for endereco, esperado in casos.items():
            with self.subTest(endereco=endereco):
                self.assertEqual(validacao.ip_do_visitante(endereco), esperado)

    def test_o_que_nao_e_ip_de_visitante_vira_nulo(self):
        for endereco in (None, "", "desconhecido", "203.0.113.256", "010.0.0.1", "203.0.113.9:443",
                         "fe80::1%eth0", "fe80::1%" + "x" * 50, "2001:db8::1/64", 3232235777, b"203.0.113.9"):
            with self.subTest(endereco=endereco):
                self.assertIsNone(validacao.ip_do_visitante(endereco))
        # O maior IPv6 na forma curta cabe nos 45 caracteres da coluna.
        maior = validacao.ip_do_visitante("ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff")
        self.assertLessEqual(len(maior), validacao.IP_MAXIMO)


if __name__ == "__main__":
    unittest.main()
