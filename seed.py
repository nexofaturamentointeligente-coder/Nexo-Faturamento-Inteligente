"""
NEXO - Script de seed (dados iniciais para demo)
=================================================
Popula o banco com:
  - 3 planos (BRONZE, PRATA, OURO)
  - alguns segmentos
  - 1 ADMIN master (credenciais do .env)

Execute manualmente após preparar o schema com as migrações (ou init_db.py
somente para SQLite local), conferindo DATABASE_URL e FLASK_ENV:
    python seed.py

Idempotente: pode rodar múltiplas vezes sem duplicar (verifica existência).

A Pessoa 4 vai ESTENDER este script na Semana 3/4 para popular
empresa(s), usuário(s) cliente e 2 análises demo (1 em rascunho e 1 publicada).
"""

import sys

from datetime import date
from sqlalchemy import select

from app import create_app
from extensions import db
from models import Plano, Segmento, Usuario
from plan_catalog import CATALOGO_PLANOS


def seed_planos():
    """Cria os 3 planos comerciais ausentes, com os valores do catálogo padrão.

    Planos já existentes NÃO são alterados aqui: o preço de um banco em uso
    muda só pela atualização auditada `flask planos atualizar-precos`.
    """
    for p in CATALOGO_PLANOS:
        existe = db.session.execute(
            select(Plano).where(Plano.nome_plano == p["nome_plano"])
        ).scalar_one_or_none()
        if existe:
            print(f"   ℹ️  Plano {p['nome_plano']} já existe.")
            continue
        db.session.add(Plano(**p))
        print(f"   ✅ Plano {p['nome_plano']} criado.")


def seed_segmentos():
    """Cria segmentos iniciais. Foco: construção / reforma / pintura."""
    segmentos = [
        ("LOJA_DE_TINTAS", "Loja especializada em tintas e material de pintura."),
        ("MATERIAL_DE_CONSTRUCAO", "Loja de material de construção em geral."),
        ("FERRAGEM", "Loja de ferragens e ferramentas."),
    ]
    for nome, desc in segmentos:
        existe = db.session.execute(
            select(Segmento).where(Segmento.nome_segmento == nome)
        ).scalar_one_or_none()
        if existe:
            print(f"   ℹ️  Segmento {nome} já existe.")
            continue
        db.session.add(Segmento(nome_segmento=nome, descricao=desc))
        print(f"   ✅ Segmento {nome} criado.")


def seed_admin(app):
    """
    Cria o ADMIN master usando credenciais do .env.
    REGRA: id_empresa SEMPRE NULL para ADMIN (CHECK do banco enforça).
    """
    email = app.config["ADMIN_EMAIL"]
    nome = app.config["ADMIN_NOME"]
    senha = app.config["ADMIN_SENHA"]

    if not senha:
        raise RuntimeError(
            "ADMIN_SENHA não definida no .env. Configure antes de rodar o seed."
        )

    existe = db.session.execute(
        select(Usuario).where(Usuario.email == email)
    ).scalar_one_or_none()
    if existe:
        print(f"   ℹ️  Admin {email} já existe.")
        return

    admin = Usuario(
        id_empresa=None,
        nome=nome,
        email=email,
        role="ADMIN",
        ativo=True,
        primeiro_acesso=False,
    )
    admin.set_senha(senha)
    db.session.add(admin)
    print(f"   ✅ Admin {email} criado.")


def main():
    app = create_app()
    app.config["SQLALCHEMY_ECHO"] = False
    with app.app_context():
        # DevelopmentConfig pode habilitar echo; não registrar dados do seed.
        db.engine.echo = False
        print("🌱 Populando dados iniciais...")
        print("📦 Planos:")
        seed_planos()
        print("🏷️  Segmentos:")
        seed_segmentos()
        print("👤 Admin Master:")
        seed_admin(app)
        db.session.commit()
        print("✅ Seed concluído.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
