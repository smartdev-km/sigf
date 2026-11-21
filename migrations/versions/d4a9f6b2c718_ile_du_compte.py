"""île attribuée à un compte

Revision ID: d4a9f6b2c718
Revises: c3e8a5d1f092
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4a9f6b2c718'
down_revision = 'c3e8a5d1f092'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ile_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_ile_id'), ['ile_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_users_ile_id_iles'), 'iles', ['ile_id'], ['id'])


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_users_ile_id_iles'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_users_ile_id'))
        batch_op.drop_column('ile_id')
