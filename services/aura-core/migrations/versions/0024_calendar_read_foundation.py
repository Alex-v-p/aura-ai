"""Create Core-owned calendar connection and selection state.

The migration intentionally contains no event table or event-content column.
It is forward-only because removing connection/revocation truth during a
rollback would be unsafe for owner-authorized provider credentials.
"""

from alembic import op
from sqlalchemy import text

revision = "0024_calendar_read_foundation"
down_revision = "0023_evaluation_runner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS calendar_connections (
                id UUID PRIMARY KEY,
                principal_issuer VARCHAR(1024) NOT NULL,
                principal_subject VARCHAR(255) NOT NULL,
                provider VARCHAR(32) NOT NULL DEFAULT 'google',
                status VARCHAR(32) NOT NULL DEFAULT 'disconnected',
                version INTEGER NOT NULL DEFAULT 1,
                connected_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_error_category VARCHAR(48),
                last_error_retryable BOOLEAN,
                last_error_recovery_action VARCHAR(32),
                last_error_at TIMESTAMPTZ,
                revocation_attempts INTEGER NOT NULL DEFAULT 0,
                revocation_deadline TIMESTAMPTZ,
                next_revocation_at TIMESTAMPTZ,
                revocation_lease_attempt INTEGER,
                revocation_lease_expires_at TIMESTAMPTZ,
                UNIQUE (principal_issuer, principal_subject, provider),
                CHECK (version > 0),
                CHECK (revocation_attempts BETWEEN 0 AND 6),
                CHECK (status IN (
                    'disconnected','connecting','connected',
                    'reauthorization_required','revocation_uncertain',
                    'disabled','unavailable'
                ))
            )
            """
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_calendar_connections_owner "
            "ON calendar_connections (principal_issuer, principal_subject)"
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS calendar_credentials (
                connection_id UUID PRIMARY KEY REFERENCES calendar_connections(id)
                    ON DELETE CASCADE,
                ciphertext BYTEA NOT NULL,
                nonce BYTEA NOT NULL,
                key_version VARCHAR(64) NOT NULL,
                associated_data_digest BYTEA NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (octet_length(ciphertext) BETWEEN 16 AND 32768),
                CHECK (octet_length(nonce) = 12),
                CHECK (char_length(key_version) BETWEEN 1 AND 64)
            )
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS calendar_discovered_calendars (
                id UUID PRIMARY KEY,
                connection_id UUID NOT NULL REFERENCES calendar_connections(id) ON DELETE CASCADE,
                provider_calendar_id VARCHAR(1024) NOT NULL,
                display_name VARCHAR(255) NOT NULL,
                time_zone VARCHAR(128) NOT NULL,
                readable BOOLEAN NOT NULL DEFAULT TRUE,
                discovered_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (connection_id, provider_calendar_id),
                CHECK (char_length(display_name) BETWEEN 1 AND 255),
                CHECK (char_length(time_zone) BETWEEN 1 AND 128)
            )
            """
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_calendar_metadata_connection_readable "
            "ON calendar_discovered_calendars (connection_id, readable, id)"
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS calendar_selected_calendars (
                connection_id UUID NOT NULL REFERENCES calendar_connections(id) ON DELETE CASCADE,
                calendar_id UUID NOT NULL REFERENCES calendar_discovered_calendars(id)
                    ON DELETE CASCADE,
                selected_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (connection_id, calendar_id)
            )
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS calendar_selection_versions (
                connection_id UUID PRIMARY KEY REFERENCES calendar_connections(id)
                    ON DELETE CASCADE,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (version > 0)
            )
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS calendar_command_idempotency (
                principal_issuer VARCHAR(1024) NOT NULL,
                principal_subject VARCHAR(255) NOT NULL,
                operation VARCHAR(64) NOT NULL,
                idempotency_key UUID NOT NULL,
                fingerprint VARCHAR(64) NOT NULL,
                response JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (principal_issuer, principal_subject, operation, idempotency_key),
                CHECK (
                    NOT (response ? 'authorizationPath')
                    AND NOT (response ? 'authorization_path')
                    AND NOT (response ? 'credential')
                    AND NOT (response ? 'provider_id')
                )
            )
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS calendar_audit_entries (
                id UUID PRIMARY KEY,
                action VARCHAR(48) NOT NULL,
                outcome VARCHAR(32) NOT NULL,
                category VARCHAR(48),
                occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (char_length(action) BETWEEN 1 AND 48),
                CHECK (char_length(outcome) BETWEEN 1 AND 32)
            )
            """
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_calendar_audit_time "
            "ON calendar_audit_entries (occurred_at)"
        )
    )


def downgrade() -> None:
    raise RuntimeError("calendar read foundation is intentionally forward-only")
