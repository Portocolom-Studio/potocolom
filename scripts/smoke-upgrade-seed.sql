INSERT INTO users (id, email, role, mail_verified) VALUES
('00000000-0000-0000-0000-000000000001', 'upgrade-admin@example.invalid', 'admin', true),
('00000000-0000-0000-0000-000000000002', 'upgrade-user@example.invalid', 'user', false);

INSERT INTO auth_identities (id, user_id, provider, subject, password_hash, last_login_at) VALUES
('00000000-0000-0000-0000-000000000011',
 '00000000-0000-0000-0000-000000000001', 'password',
 'upgrade-admin@example.invalid', 'upgrade-password-hash', '2026-09-20T10:00:00Z');

INSERT INTO sessions (
    id, user_id, token_hash, remember_me, absolute_expires_at,
    idle_expires_at, recent_auth_at, last_seen_at
) VALUES (
    '00000000-0000-0000-0000-000000000012',
    '00000000-0000-0000-0000-000000000001',
    decode('01020304', 'hex'), true, '2026-10-20T10:00:00Z',
    '2026-09-27T11:00:00Z', '2026-09-20T10:00:00Z', '2026-09-20T10:05:00Z'
);

INSERT INTO models (id, name, capabilities, parameters_schema, min_vram_gb) VALUES
('upgrade-test', 'Upgrade test', '{}', '{}', 0);

INSERT INTO jobs (id, user_id, model_id, params, state, attempt, gpu_ms) VALUES
('00000000-0000-0000-0000-000000000021',
 '00000000-0000-0000-0000-000000000001', 'upgrade-test',
 '{"prompt":"preserve admin","seed":41}', 'succeeded', 1, 123),
('00000000-0000-0000-0000-000000000022',
 '00000000-0000-0000-0000-000000000002', 'upgrade-test',
 '{"prompt":"preserve user","seed":42}', 'succeeded', 2, 456);

INSERT INTO assets (id, user_id, job_id, storage_key, mime, width, height) VALUES
('00000000-0000-0000-0000-000000000031',
 '00000000-0000-0000-0000-000000000001',
 '00000000-0000-0000-0000-000000000021', 'upgrade/admin.png', 'image/png', 64, 64),
('00000000-0000-0000-0000-000000000032',
 '00000000-0000-0000-0000-000000000002',
 '00000000-0000-0000-0000-000000000022', 'upgrade/user.png', 'image/png', 96, 80);

INSERT INTO asset_shares (id, asset_id, token_hash, expires_at) VALUES
('00000000-0000-0000-0000-000000000041',
 '00000000-0000-0000-0000-000000000032', decode('05060708', 'hex'),
 '2026-10-20T10:00:00Z');

INSERT INTO audit_events (
    id, occurred_at, actor_user_id, actor_role, action, target_user_id,
    object_ids, object_count, truncated, severity
) VALUES (
    '00000000-0000-0000-0000-000000000051', '2026-09-20T10:10:00Z',
    '00000000-0000-0000-0000-000000000001', 'admin', 'account.role_changed',
    '00000000-0000-0000-0000-000000000002',
    '["00000000-0000-0000-0000-000000000002"]', 1, false, 'high'
);

INSERT INTO usage_events (
    id, user_id, kind, action, model_id, tier, category, category_score,
    gpu_ms, duration_ms, frames, created_at
) VALUES
('00000000-0000-0000-0000-000000000061',
 '00000000-0000-0000-0000-000000000001', 'job', 'generate', 'upgrade-test',
 'standard', 'illustration', 0.75, 123, 150, NULL, '2026-09-20T10:20:00Z'),
('00000000-0000-0000-0000-000000000062',
 '00000000-0000-0000-0000-000000000002', 'realtime', 'realtime', 'upgrade-test',
 'trial', 'sketch', 0.5, 456, 900, 7, '2026-09-20T10:21:00Z');

INSERT INTO usage_event_rollups (
    id, user_id, bucket_date, kind, action, model_id, tier, category,
    event_count, category_score_sum, category_score_count, gpu_ms_sum,
    duration_ms_sum, frames_sum
) VALUES (
    '00000000-0000-0000-0000-000000000071',
    '00000000-0000-0000-0000-000000000002', '2026-09-19',
    'realtime', 'realtime', 'upgrade-test', 'trial', 'sketch',
    3, 1.5, 3, 900, 1800, 21
);
