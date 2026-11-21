"""blocages de dossiers (problème signalé, levé par le Directeur)

Revision ID: b7d2e41c9a05
Revises: a1f3c9e27b64
"""
from alembic import op
import sqlalchemy as sa

revision = 'b7d2e41c9a05'
down_revision = 'a1f3c9e27b64'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'blocages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('dossier_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(length=40), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('signale_par_id', sa.Integer(), nullable=False),
        sa.Column('signale_at', sa.DateTime(), nullable=False),
        sa.Column('leve_par_id', sa.Integer(), nullable=True),
        sa.Column('leve_at', sa.DateTime(), nullable=True),
        sa.Column('motif_levee', sa.Text(), nullable=True),
        sa.Column('preuve_document_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['dossier_id'], ['dossiers.id'], name=op.f('fk_blocages_dossier_id_dossiers')),
        sa.ForeignKeyConstraint(['signale_par_id'], ['users.id'], name=op.f('fk_blocages_signale_par_id_users')),
        sa.ForeignKeyConstraint(['leve_par_id'], ['users.id'], name=op.f('fk_blocages_leve_par_id_users')),
        sa.ForeignKeyConstraint(['preuve_document_id'], ['documents.id'], name=op.f('fk_blocages_preuve_document_id_documents')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_blocages')),
    )
    with op.batch_alter_table('blocages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_blocages_dossier_id'), ['dossier_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_blocages_leve_at'), ['leve_at'], unique=False)


def downgrade():
    op.drop_table('blocages')
