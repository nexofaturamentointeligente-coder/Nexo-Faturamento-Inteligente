"""Selos do sininho pelo começo fixo do aviso: o texto livre do assunto não decide o selo."""
import unittest

from notifications import classificar_notificacao


class SelosDoSininhoTests(unittest.TestCase):
    def test_selos_pelo_comeco_do_aviso(self):
        casos = {
            "Recebemos e processamos seus relatórios de 02/2026. Sua devolutiva está em preparação.": "Em análise",
            "A equipe NEXO respondeu seu chamado “Relatório indisponível”.": "Resposta",
            "A equipe NEXO respondeu seu chamado “Erro não resolvido”.": "Resposta",
            "Seu chamado “Relatório indisponível” foi marcado como resolvido.": "Resolvido",
            "Novo chamado de Loja X: “Erro resolvido?”.": "Aberto",
            "Sua avaliação foi aprovada e já aparece nos depoimentos da NEXO.": "Avaliação",
            "Seu Relatório Estratégico de 02/2026 já está disponível! Clique para ver.": "Nova Análise",
            "Seu contrato com a NEXO vence em 10/03/2026. Fale com a equipe para renovar.": "Contrato",
        }
        for texto, rotulo in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(classificar_notificacao(texto)["rotulo"], rotulo)

    def test_selos_novos_reaproveitam_as_cores_que_ja_existem(self):
        """O selo "Resposta" usa a cor de "Aberto"; "Em análise" e "Avaliação", a de "Nova Análise": nenhum
        tipo novo no CSS nem no rótulo padrão do script do sininho. O nome da empresa também não decide."""
        casos = {
            "A equipe NEXO respondeu seu chamado “Dúvida fictícia”.": ("aberto", "Resposta"),
            "Recebemos e processamos seus relatórios de 02/2026. Sua devolutiva está em preparação.": ("analise", "Em análise"),
            "Sua avaliação foi aprovada pela equipe NEXO. Ela não é publicada porque você não autorizou.": ("analise", "Avaliação"),
            "Sua avaliação foi revisada e não será publicada. Você pode editá-la e reenviá-la.": ("analise", "Avaliação"),
            "Nova resposta de Disponível Materiais no chamado “Erro resolvido”.": ("aberto", "Aberto"),
            "Nova planilha de Vendas enviada por Resolvida Comércio Ltda aguardando homologação.": ("aberto", "Aberto"),
            # Aviso aos consultores de avaliação para moderar (blueprints/avaliacoes.py): pendência deles.
            "Nova avaliação de Disponível Materiais aguardando moderação.": ("aberto", "Aberto"),
            "Nova avaliação de Resolvida Comércio Ltda aguardando moderação (editada).": ("aberto", "Aberto"),
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                selo = classificar_notificacao(texto)
                self.assertEqual((selo["tipo"], selo["rotulo"]), esperado)


if __name__ == "__main__":
    unittest.main()
