-- Supabase / Postgres schema for the reset audit + session store.
-- Run this once in the Supabase SQL editor.

create table if not exists reset_sessions (
    reset_session_id   uuid primary key,
    task_allocation_id text        not null,
    email              text        not null,
    persona            text        not null,
    status             text        not null default 'queued',  -- queued|running|completed|failed
    mode               text,                                    -- delta|reseed|reset
    error              text,
    created_at         timestamptz not null default now(),
    started_at         timestamptz,
    completed_at       timestamptz
);

create index if not exists idx_reset_sessions_email  on reset_sessions (email);
create index if not exists idx_reset_sessions_task   on reset_sessions (task_allocation_id);
create index if not exists idx_reset_sessions_status on reset_sessions (status);

-- Enforce "one active reset per email": a second queued/running row for the
-- same account cannot be inserted. Useful now, essential once workers run in
-- parallel. Drop this if you want to allow overlapping requests during testing.
create unique index if not exists uniq_active_reset_per_email
    on reset_sessions (email)
    where status in ('queued', 'running');
