"""actes générés pour les mutations (documents émis liés à une mutation)

Revision ID: a9e4c6d2b831
Revises: f7c2a4e8d913
"""
from alembic import op
import sqlalchemy as sa

revision = 'a9e4c6d2b831'
down_revision = 'f7c2a4e8d913'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('documents_emis', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mutation_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_documents_emis_mutation_id'), ['mutation_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_documents_emis_mutation_id_mutations'), 'mutations', ['mutation_id'], ['id'])


def downgrade():
    with op.batch_alter_table('documents_emis', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_documents_emis_mutation_id_mutations'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_documents_emis_mutation_id'))
        batch_op.drop_column('mutation_id')
