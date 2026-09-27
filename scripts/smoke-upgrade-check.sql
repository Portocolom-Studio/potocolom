DO $$
BEGIN
    IF (SELECT count(*) FROM users) <> 2
       OR (SELECT count(*) FROM jobs) <> 2
       OR (SELECT count(*) FROM assets) <> 2
       OR NOT EXISTS (
           SELECT 1 FROM users u
           JOIN jobs j ON j.user_id = u.id
           JOIN assets a ON a.job_id = j.id AND a.user_id = u.id
           WHERE u.email = 'upgrade-admin@example.invalid'
             AND u.role = 'admin' AND u.state = 'active' AND u.mail_verified
             AND j.params = '{"prompt":"preserve admin","seed":41}'::jsonb
             AND j.attempt = 1 AND j.gpu_ms = 123
             AND a.storage_key = 'upgrade/admin.png'
             AND a.width = 64 AND a.height = 64
       )
       OR NOT EXISTS (
           SELECT 1 FROM users u
           JOIN jobs j ON j.user_id = u.id
           JOIN assets a ON a.job_id = j.id AND a.user_id = u.id
           WHERE u.email = 'upgrade-user@example.invalid'
             AND u.role = 'user' AND u.state = 'active' AND NOT u.mail_verified
             AND j.params = '{"prompt":"preserve user","seed":42}'::jsonb
             AND j.attempt = 2 AND j.gpu_ms = 456
             AND a.storage_key = 'upgrade/user.png'
             AND a.width = 96 AND a.height = 80
       ) THEN
        RAISE EXCEPTION 'upgrade or restore changed account-owned generation data';
    END IF;

    IF (SELECT count(*) FROM auth_identities) <> 1
       OR (SELECT count(*) FROM sessions) <> 1
       OR (SELECT count(*) FROM asset_shares) <> 1
       OR (SELECT count(*) FROM audit_events) <> 1
       OR NOT EXISTS (
           SELECT 1 FROM auth_identities i
           JOIN sessions s ON s.user_id = i.user_id
           JOIN users u ON u.id = i.user_id
           WHERE u.email = 'upgrade-admin@example.invalid'
             AND i.provider = 'password'
             AND i.subject = 'upgrade-admin@example.invalid'
             AND i.password_hash = 'upgrade-password-hash'
             AND i.last_login_at = '2026-09-20T10:00:00Z'
             AND s.token_hash = decode('01020304', 'hex')
             AND s.remember_me
             AND s.absolute_expires_at = '2026-10-20T10:00:00Z'
             AND s.idle_expires_at = '2026-09-27T11:00:00Z'
             AND s.recent_auth_at = '2026-09-20T10:00:00Z'
             AND s.last_seen_at = '2026-09-20T10:05:00Z'
       )
       OR NOT EXISTS (
           SELECT 1 FROM asset_shares share
           JOIN assets a ON a.id = share.asset_id
           JOIN users u ON u.id = a.user_id
           WHERE u.email = 'upgrade-user@example.invalid'
             AND share.token_hash = decode('05060708', 'hex')
             AND share.expires_at = '2026-10-20T10:00:00Z'
             AND share.revoked_at IS NULL
       )
       OR NOT EXISTS (
           SELECT 1 FROM audit_events event
           JOIN users actor ON actor.id = event.actor_user_id
           JOIN users target ON target.id = event.target_user_id
           WHERE actor.email = 'upgrade-admin@example.invalid'
             AND target.email = 'upgrade-user@example.invalid'
             AND event.actor_role = 'admin'
             AND event.action = 'account.role_changed'
             AND event.occurred_at = '2026-09-20T10:10:00Z'
             AND event.object_ids = '["00000000-0000-0000-0000-000000000002"]'::jsonb
             AND event.object_count = 1 AND NOT event.truncated
             AND event.severity = 'high'
       ) THEN
        RAISE EXCEPTION 'upgrade or restore changed security or sharing data';
    END IF;

    IF (SELECT count(*) FROM usage_events) <> 2
       OR (SELECT count(*) FROM usage_event_rollups) <> 1
       OR NOT EXISTS (
           SELECT 1 FROM usage_events event
           JOIN users u ON u.id = event.user_id
           WHERE u.email = 'upgrade-admin@example.invalid'
             AND event.kind = 'job' AND event.action = 'generate'
             AND event.model_id = 'upgrade-test' AND event.tier = 'standard'
             AND event.category = 'illustration' AND event.category_score = 0.75
             AND event.gpu_ms = 123 AND event.duration_ms = 150
             AND event.frames IS NULL
       )
       OR NOT EXISTS (
           SELECT 1 FROM usage_events event
           JOIN users u ON u.id = event.user_id
           WHERE u.email = 'upgrade-user@example.invalid'
             AND event.kind = 'realtime' AND event.action = 'realtime'
             AND event.model_id = 'upgrade-test' AND event.tier = 'trial'
             AND event.category = 'sketch' AND event.category_score = 0.5
             AND event.gpu_ms = 456 AND event.duration_ms = 900
             AND event.frames = 7
       )
       OR NOT EXISTS (
           SELECT 1 FROM usage_event_rollups rollup
           JOIN users u ON u.id = rollup.user_id
           WHERE u.email = 'upgrade-user@example.invalid'
             AND rollup.bucket_date = '2026-09-19'
             AND rollup.kind = 'realtime' AND rollup.action = 'realtime'
             AND rollup.model_id = 'upgrade-test' AND rollup.tier = 'trial'
             AND rollup.category = 'sketch' AND rollup.event_count = 3
             AND rollup.category_score_sum = 1.5
             AND rollup.category_score_count = 3
             AND rollup.gpu_ms_sum = 900 AND rollup.duration_ms_sum = 1800
             AND rollup.frames_sum = 21
       ) THEN
        RAISE EXCEPTION 'upgrade or restore changed usage data';
    END IF;

    IF to_regclass('public.login_attempts') IS NULL THEN
        RAISE EXCEPTION 'upgrade did not create login_attempts';
    END IF;
END $$;
