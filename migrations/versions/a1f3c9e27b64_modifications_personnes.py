"""modifications de personnes (validées par le Directeur)

Revision ID: a1f3c9e27b64
Revises: 67809cbbf865
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1f3c9e27b64'
down_revision = '67809cbbf865'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'modifications_personnes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('proprietaire_id', sa.Integer(), nullable=False),
        sa.Column('changements', sa.Text(), nullable=False),
        sa.Column('motif', sa.Text(), nullable=False),
        sa.Column('statut', sa.String(length=20), nullable=False),
        sa.Column('saisi_par_id', sa.Integer(), nullable=False),
        sa.Column('saisi_at', sa.DateTime(), nullable=False),
        sa.Column('decision_par_id', sa.Integer(), nullable=True),
        sa.Column('decision_at', sa.DateTime(), nullable=True),
        sa.Column('motif_rejet', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['proprietaire_id'], ['proprietaires.id'], name=op.f('fk_modifications_personnes_proprietaire_id_proprietaires')),
        sa.ForeignKeyConstraint(['saisi_par_id'], ['users.id'], name=op.f('fk_modifications_personnes_saisi_par_id_users')),
        sa.ForeignKeyConstraint(['decision_par_id'], ['users.id'], name=op.f('fk_modifications_personnes_decision_par_id_users')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_modifications_personnes')),
    )
    with op.batch_alter_table('modifications_personnes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_modifications_personnes_proprietaire_id'), ['proprietaire_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_modifications_personnes_statut'), ['statut'], unique=False)


def downgrade():
    op.drop_table('modifications_personnes')
