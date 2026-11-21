"""description du terrain imprimée sur le plan croquis

Revision ID: b2d7e9a4c615
Revises: a9e4c6d2b831
"""
from alembic import op
import sqlalchemy as sa

revision = 'b2d7e9a4c615'
down_revision = 'a9e4c6d2b831'
branch_labels = None
depends_on = None

COLONNES = [
    ('limitrophe_nord', sa.String(200)), ('limitrophe_sud', sa.String(200)), ('limitrophe_est', sa.String(200)), ('limitrophe_ouest', sa.String(200)),
    ('nature_terrain', sa.String(80)), ('usage_terrain', sa.String(200)), ('acces_terrain', sa.String(200)),
    ('date_leve', sa.Date()), ('mode_leve', sa.String(40)), ('topographe_leve', sa.String(120)), ('agrement_topographe', sa.String(60)),
    ('observations_plan', sa.Text()),
]


def upgrade():
    with op.batch_alter_table('dossiers', schema=None) as batch_op:
        for nom, type_ in COLONNES:
            batch_op.add_column(sa.Column(nom, type_, nullable=True))


def downgrade():
    with op.batch_alter_table('dossiers', schema=None) as batch_op:
        for nom, _ in reversed(COLONNES):
            batch_op.drop_column(nom)
