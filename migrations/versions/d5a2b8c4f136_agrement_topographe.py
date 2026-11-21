"""n° d'agrément porté par le compte du topographe (au lieu d'être saisi sur chaque dossier)

Revision ID: d5a2b8c4f136
Revises: c8f1a3d5e720
"""
from alembic import op
import sqlalchemy as sa

revision = 'd5a2b8c4f136'
down_revision = 'c8f1a3d5e720'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users') as b:
        b.add_column(sa.Column('agrement', sa.String(60)))
    with op.batch_alter_table('dossiers') as b:
        b.drop_column('topographe_leve')
        b.drop_column('agrement_topographe')


def downgrade():
    with op.batch_alter_table('dossiers') as b:
        b.add_column(sa.Column('topographe_leve', sa.String(120)))
        b.add_column(sa.Column('agrement_topographe', sa.String(60)))
    with op.batch_alter_table('users') as b:
        b.drop_column('agrement')
