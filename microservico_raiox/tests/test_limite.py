"""Limite de envios por IP em janelas deslizantes de 1 hora e 24 horas."""

import unittest

from limite import DIA, HORA, LimitePorIp, chave_do_endereco


class _Relogio:
    def __init__(self):
        self.agora = 1000.0

    def __call__(self):
        return self.agora


class LimiteTests(unittest.TestCase):
    def test_janela_de_uma_hora(self):
        relogio = _Relogio()
        limite = LimitePorIp(2, 10, relogio=relogio)
        self.assertEqual([limite.registrar("ip") for _ in range(2)], [None, None])
        espera = limite.registrar("ip")
        self.assertTrue(0 < espera <= HORA + 1)
        self.assertIsNone(limite.registrar("outro ip"))
        relogio.agora += HORA
        self.assertIsNone(limite.registrar("ip"))

    def test_janela_de_um_dia(self):
        relogio = _Relogio()
        limite = LimitePorIp(10, 3, relogio=relogio)
        for _ in range(3):
            self.assertIsNone(limite.registrar("ip"))
            relogio.agora += HORA
        espera = limite.registrar("ip")
        self.assertTrue(HORA < espera <= DIA + 1)
        relogio.agora += DIA
        self.assertIsNone(limite.registrar("ip"))

    def test_tentativa_recusada_nao_conta(self):
        relogio = _Relogio()
        limite = LimitePorIp(1, 10, relogio=relogio)
        limite.registrar("ip")
        for _ in range(5):
            self.assertIsNotNone(limite.registrar("ip"))
        relogio.agora += HORA
        self.assertIsNone(limite.registrar("ip"))

    def test_chave_do_endereco(self):
        casos = {
            "203.0.113.9": "203.0.113.9",
            "2001:db8:1:2::1": "2001:db8:1:2::/64",
            "2001:0db8:0001:0002:ffff:ffff:ffff:ffff": "2001:db8:1:2::/64",
            "fe80::1%eth0": "fe80::/64",
            "::ffff:203.0.113.9": "203.0.113.9",
            "": "desconhecido",
            "não é ip": "não é ip",
            "x" * 100: "x" * 64,
        }
        for endereco, chave in casos.items():
            with self.subTest(endereco=endereco[:20]):
                self.assertEqual(chave_do_endereco(endereco), chave)

    def test_ipv6_da_mesma_rede_64_divide_o_limite(self):
        limite = LimitePorIp(2, 10, relogio=_Relogio())
        self.assertEqual([limite.registrar(f"2001:db8:1:2::{final}") for final in (1, 2)], [None, None])
        self.assertIsNotNone(limite.registrar("2001:db8:1:2:aaaa::3"))
        self.assertIsNone(limite.registrar("2001:db8:1:3::1"))
        # O IPv4 mapeado em IPv6 e o próprio IPv4 contam juntos.
        self.assertEqual([limite.registrar("::ffff:198.51.100.1"), limite.registrar("198.51.100.1")], [None, None])
        self.assertIsNotNone(limite.registrar("::ffff:198.51.100.1"))

    def test_memoria_limitada(self):
        limite = LimitePorIp(5, 10, maximo_de_enderecos=3)
        for indice in range(10):
            limite.registrar(f"ip{indice}")
        self.assertEqual(len(limite._envios), 3)


if __name__ == "__main__":
    unittest.main()
