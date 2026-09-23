-- Shared Supabase schema for the GAB Environment Platform.
-- Both the seeder (upload flow) and the api (reset flow) use these two tables.
-- Run once in the Supabase SQL editor.

-- ---------------------------------------------------------------------------
-- gab_accounts : source-of-truth per account. The SEEDER writes here on
-- authorize (token/details); the API reads persona + last_reset_persona here.
-- ---------------------------------------------------------------------------
create table if not exists gab_accounts (
    email               text primary key,
    persona             text not null,
    password            text,          -- sensitive; service-role only
    refresh_token       text,          -- sensitive; saved on authorize
    token_json          jsonb,
    scopes              text[],
    authorized          boolean not null default false,
    authorized_at       timestamptz,
    status              text not null default 'active',
    last_reset_persona  text,          -- drives "same persona -> reset / new -> reseed"
    last_reset_id       uuid,
    last_reset_at       timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);
create index if not exists idx_gab_accounts_persona    on gab_accounts (persona);
create index if not exists idx_gab_accounts_authorized on gab_accounts (authorized);

create or replace function set_updated_at() returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;
drop trigger if exists trg_gab_accounts_updated on gab_accounts;
create trigger trg_gab_accounts_updated before update on gab_accounts
    for each row execute function set_updated_at();

-- ---------------------------------------------------------------------------
-- reset_sessions : one row per operation (upload / reset / reseed). The SEEDER
-- writes an 'upload' row on push; the API writes reset/reseed rows.
-- ---------------------------------------------------------------------------
create table if not exists reset_sessions (
    reset_session_id   uuid primary key,
    task_allocation_id text        not null,   -- 'upload-<uuid>' placeholder for uploads
    email              text        not null,
    persona            text        not null,
    status             text        not null default 'queued', -- queued|running|completed|failed
    mode               text,                                   -- upload|delta|reseed|reset
    error              text,
    created_at         timestamptz not null default now(),
    started_at         timestamptz,
    completed_at       timestamptz
);
create index if not exists idx_reset_sessions_email  on reset_sessions (email);
create index if not exists idx_reset_sessions_task   on reset_sessions (task_allocation_id);
create index if not exists idx_reset_sessions_status on reset_sessions (status);

-- One active reset per email (queued/running). Drop if overlapping is desired.
create unique index if not exists uniq_active_reset_per_email
    on reset_sessions (email)
    where status in ('queued', 'running');
