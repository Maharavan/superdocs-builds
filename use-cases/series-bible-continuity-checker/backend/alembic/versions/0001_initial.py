"""Initial durable Series Bible schema.

Revision ID: 0001
"""
from alembic import op
from series_bible.infrastructure.database import Base
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
