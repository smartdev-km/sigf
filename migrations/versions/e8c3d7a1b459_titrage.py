"""titrage : étapes après la certification (journal officiel, bornage, journal de clôture, titre)

Revision ID: e8c3d7a1b459
Revises: d5a2b8c4f136
"""
from alembic import op
import sqlalchemy as sa

revision = 'e8c3d7a1b459'
down_revision = 'd5a2b8c4f136'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('dossiers') as b:
        b.add_column(sa.Column('etape_titre', sa.String(30)))
        b.create_index('ix_dossiers_etape_titre', ['etape_titre'])
    op.create_table(
        'etapes_titrage',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('dossier_id', sa.Integer(), sa.ForeignKey('dossiers.id'), nullable=False),
        sa.Column('etape', sa.String(30), nullable=False),
        sa.Column('type', sa.String(10), nullable=False),
        sa.Column('reference', sa.String(120)),
        sa.Column('date_etape', sa.Date()),
        sa.Column('observations', sa.Text()),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('annule_at', sa.DateTime()),
    )
    op.create_index('ix_etapes_titrage_dossier_id', 'etapes_titrage', ['dossier_id'])


def downgrade():
    op.drop_index('ix_etapes_titrage_dossier_id', table_name='etapes_titrage')
    op.drop_table('etapes_titrage')
    with op.batch_alter_table('dossiers') as b:
        b.drop_index('ix_dossiers_etape_titre')
        b.drop_column('etape_titre')
