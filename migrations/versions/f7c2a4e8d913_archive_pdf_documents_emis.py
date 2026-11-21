"""archivage du PDF de chaque document généré

Revision ID: f7c2a4e8d913
Revises: e6b1d3a7c295
"""
from alembic import op
import sqlalchemy as sa

revision = 'f7c2a4e8d913'
down_revision = 'e6b1d3a7c295'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('documents_emis', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pdf_cle', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('pdf_sha256', sa.String(length=64), nullable=True))


def downgrade():
    with op.batch_alter_table('documents_emis', schema=None) as batch_op:
        batch_op.drop_column('pdf_sha256')
        batch_op.drop_column('pdf_cle')
