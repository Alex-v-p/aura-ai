-- AURA-0096 additive metadata-only live-event vocabulary.
-- Keep AURA-0095's ledger and sequence unchanged; only extend the closed
-- resource/change allowlists so SSE can carry experiment lifecycle updates.
DO $$
BEGIN
    ALTER TABLE observatory_evaluation_live_events
        ADD COLUMN IF NOT EXISTS resource_version VARCHAR(64);
    ALTER TABLE observatory_evaluation_live_events /* IF NOT EXISTS */
        DROP CONSTRAINT IF EXISTS observatory_eval_live_resource_type_ck;
    ALTER TABLE observatory_evaluation_live_events /* IF NOT EXISTS */
        ADD CONSTRAINT observatory_eval_live_resource_type_ck CHECK (
            resource_type IN (
                'evaluation_dataset', 'evaluation_case', 'evaluation_suite',
                'evaluation_evaluator', 'evaluation_job', 'evaluation_attempt',
                'evaluation_result', 'evaluation_artifact', 'evaluation_review',
                'evaluation_calibration', 'evaluation_qualification',
                'evaluation_deletion', 'evaluation_experiment',
                'evaluation_baseline', 'evaluation_regression',
                'evaluation_attribution'
            )
        );
    ALTER TABLE observatory_evaluation_live_events /* IF NOT EXISTS */
        DROP CONSTRAINT IF EXISTS observatory_eval_live_change_ck;
    ALTER TABLE observatory_evaluation_live_events /* IF NOT EXISTS */
        ADD CONSTRAINT observatory_eval_live_change_ck CHECK (
            change IN (
                'created', 'updated', 'state_changed', 'cancel_requested',
                'qualified', 'revoked', 'deletion_requested', 'deleted',
                'candidate_created', 'promoted', 'comparison_completed',
                'attribution_completed'
            )
        );
END
$$;

CREATE INDEX IF NOT EXISTS observatory_eval_live_resource_version_idx
    ON observatory_evaluation_live_events(resource_type, resource_id, resource_version, sequence);

INSERT INTO observatory_migration_ledger(version, applied_at)
SELECT '0013_evaluation_experiment_live_events', CURRENT_TIMESTAMP
WHERE NOT EXISTS (
    SELECT 1 FROM observatory_migration_ledger
    WHERE version = '0013_evaluation_experiment_live_events'
);
