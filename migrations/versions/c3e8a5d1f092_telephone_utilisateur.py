"""numéro de téléphone du compte

Revision ID: c3e8a5d1f092
Revises: b7d2e41c9a05
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3e8a5d1f092'
down_revision = 'b7d2e41c9a05'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('telephone', sa.String(length=40), nullable=True))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('telephone')
