"""tracé de bornage (KMZ/KML) déposé à l'étape « Plan de bornage » du titrage

Revision ID: b5e9a1d3c824
Revises: a4d8f2c6e913
"""
from alembic import op
import sqlalchemy as sa

revision = 'b5e9a1d3c824'
down_revision = 'a4d8f2c6e913'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bornages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('dossier_id', sa.Integer(), sa.ForeignKey('dossiers.id'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('geojson', sa.Text(), nullable=False),
        sa.Column('empreinte', sa.String(64), nullable=False),
        sa.Column('superficie_m2', sa.Float(), nullable=False),
        sa.Column('perimetre_m', sa.Float(), nullable=False),
        sa.Column('elements_json', sa.Text()),
        sa.Column('cle', sa.String(255), nullable=False, unique=True),
        sa.Column('nom_original', sa.String(255), nullable=False),
        sa.Column('mime', sa.String(80), nullable=False),
        sa.Column('taille', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('uploaded_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('remplace_at', sa.DateTime()),
        sa.Column('motif_remplacement', sa.Text()),
        sa.UniqueConstraint('dossier_id', 'version', name='uq_bornage_dossier_version'),
    )
    op.create_index('ix_bornages_dossier_id', 'bornages', ['dossier_id'])
    op.create_index('ix_bornages_remplace_at', 'bornages', ['remplace_at'])


def downgrade():
    op.drop_index('ix_bornages_remplace_at', table_name='bornages')
    op.drop_index('ix_bornages_dossier_id', table_name='bornages')
    op.drop_table('bornages')
