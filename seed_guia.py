"""
Popula a Base de Conhecimento (guia_topico) com os tópicos iniciais — os mesmos
que antes eram um dicionário estático. Idempotente: só insere o que faltar
(casa por pergunta). Rode manualmente após a migração, conferindo
DATABASE_URL e FLASK_ENV:
    python seed_guia.py
"""
import sys

from sqlalchemy import select

from app import create_app
from extensions import db
from models import GuiaTopico

TOPICOS = [
    {
        "categoria": "Upload",
        "pergunta": "Como fazer o upload dos relatórios?",
        "resposta": (
            "Você envia os relatórios de Vendas e Compras na aba “Enviar relatórios”; "
            "a equipe NEXO valida e processa.\n\n"
            "1. No menu lateral, clique em “Enviar relatórios”.\n"
            "2. Escolha a análise em aberto correspondente ao período desejado.\n"
            "3. Anexe o arquivo de VENDAS e o de COMPRAS (formatos aceitos: .csv ou .xlsx).\n"
            "4. Confirme o envio — o status fica como “Aguardando validação”.\n"
            "5. A equipe NEXO homologa e processa; quando a devolutiva é publicada, "
            "você é avisado pelo sininho e por e-mail."
        ),
    },
    {
        "categoria": "Senhas",
        "pergunta": "Como altero minha senha de acesso?",
        "resposta": (
            "No primeiro acesso o portal pede a troca; depois, use Configurações, no menu "
            "lateral, ou “Esqueci minha senha” na tela de login.\n\n"
            "1. Primeiro acesso: ao entrar pela primeira vez, o portal obriga você a "
            "definir uma senha pessoal antes de liberar o painel.\n"
            "2. Logado: abra Configurações e informe a senha atual e a nova.\n"
            "3. Esqueceu a senha? Na tela de login, clique em “Esqueci minha senha”.\n"
            "4. Informe seu e-mail cadastrado — enviamos um link seguro, válido por 15 minutos.\n"
            "5. Abra o link e defina a nova senha. Depois é só fazer login com ela."
        ),
    },
    {
        "categoria": "PDV",
        "pergunta": "Como exportar os relatórios do meu sistema PDV?",
        "resposta": (
            "Exporte dois arquivos do seu PDV/ERP — um de Vendas e um de Compras — em "
            ".xlsx ou .csv, nomeando por período.\n\n"
            "1. No seu sistema PDV/ERP, gere o relatório de VENDAS do período de referência.\n"
            "2. Gere também o relatório de COMPRAS do mesmo período.\n"
            "3. Exporte ambos em Excel (.xlsx) ou CSV (.csv).\n"
            "4. Nomeie de forma clara por período — ex.: “Relatório mensal março 2026 - "
            "vendas.xlsx” e “Relatório mensal março 2026 - compras.xlsx”.\n"
            "5. Volte ao portal, abra “Enviar relatórios” e anexe os dois arquivos."
        ),
    },
]

def main():
    app = create_app()
    app.config["SQLALCHEMY_ECHO"] = False
    with app.app_context():
        db.engine.echo = False
        criados = 0
        for t in TOPICOS:
            existe = db.session.execute(
                select(GuiaTopico).where(GuiaTopico.pergunta == t["pergunta"])
            ).scalar_one_or_none()
            if existe:
                print(f"  já existe: {t['pergunta']}")
                continue
            db.session.add(GuiaTopico(categoria=t["categoria"], pergunta=t["pergunta"], resposta=t["resposta"]))
            criados += 1
            print(f"  + criado [{t['categoria']}] {t['pergunta']}")
        db.session.commit()
        total = db.session.execute(select(db.func.count(GuiaTopico.id))).scalar()
        print(f"Seed do Guia concluído. {criados} novo(s). Total na base: {total}.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
