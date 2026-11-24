"""les dossiers déjà « Certificat émis » entrent dans le titrage (étape Journal officiel)

Revision ID: a4d8f2c6e913
Revises: e8c3d7a1b459
"""
from alembic import op

revision = 'a4d8f2c6e913'
down_revision = 'e8c3d7a1b459'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE dossiers SET etape_titre = 'journal_officiel' "
               "WHERE statut = 'certifie' AND origine = 'nouveau' AND etape_titre IS NULL")


def downgrade():
    pass  # on ne peut pas distinguer les dossiers qui étaient déjà en titrage avant : rien à défaire
