"""signature numérique du Directeur

Revision ID: e6b1d3a7c295
Revises: d4a9f6b2c718
"""
from alembic import op
import sqlalchemy as sa

revision = 'e6b1d3a7c295'
down_revision = 'd4a9f6b2c718'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('signature_cle', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('signature_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('signature_titre', sa.String(length=80), nullable=True))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('signature_titre')
        batch_op.drop_column('signature_at')
        batch_op.drop_column('signature_cle')
