"""repères du croquis (lignes, points, zones) lus dans le KMZ

Revision ID: c8f1a3d5e720
Revises: b2d7e9a4c615
"""
from alembic import op
import sqlalchemy as sa

revision = 'c8f1a3d5e720'
down_revision = 'b2d7e9a4c615'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('geometries') as b:
        b.add_column(sa.Column('elements_json', sa.Text()))


def downgrade():
    with op.batch_alter_table('geometries') as b:
        b.drop_column('elements_json')
