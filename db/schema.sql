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
    last_reset_mode     text,          -- last op applied: upload | reconcile | reseed | delta
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
    completed_at       timestamptz,
    consumed_at        timestamptz                             -- set once the session is used (single-use)
);
-- Older DBs: add the single-use column if missing.
alter table reset_sessions add column if not exists consumed_at timestamptz;
create index if not exists idx_reset_sessions_email  on reset_sessions (email);
create index if not exists idx_reset_sessions_task   on reset_sessions (task_allocation_id);
create index if not exists idx_reset_sessions_status on reset_sessions (status);

-- One active reset per email (queued/running). Drop if overlapping is desired.
create unique index if not exists uniq_active_reset_per_email
    on reset_sessions (email)
    where status in ('queued', 'running');

-- ---------------------------------------------------------------------------
-- client_whitelist : IP/CIDR allow-list for the platform (Bearer) reset APIs.
-- An entry authorizes a client server to call POST/GET /api/environment/* and
-- /api/reset-link. cosmo.deccanexperts.ai's egress IP(s) go here; add test IPs
-- as needed. Enforcement is gated by CLIENT_WHITELIST_ENABLED so the list can be
-- populated before it starts rejecting. Validated at the API layer (middleware).
-- ---------------------------------------------------------------------------
create table if not exists client_whitelist (
    id         bigint generated always as identity primary key,
    cidr       text        not null,                 -- an IPv4/IPv6 address or CIDR, e.g. 203.0.113.7 or 10.0.0.0/8
    label      text,                                 -- human note, e.g. 'cosmo-prod' or 'qa-laptop'
    active     boolean     not null default true,
    created_at timestamptz not null default now()
);
create unique index if not exists uniq_client_whitelist_cidr on client_whitelist (cidr);

-- Primary client. 'cidr' accepts a hostname too (DNS-resolved at check time); if
-- cosmo's egress IP differs from its DNS record, add that exact IP as another row.
insert into client_whitelist (cidr, label)
    values ('cosmo.deccanexperts.ai', 'cosmo-prod')
    on conflict (cidr) do nothing;

-- ---------------------------------------------------------------------------
-- freelancers : the allow-list of people permitted to open the reset page.
-- Populated by the Cosmo / Deccan Experts platform (POST /api/freelancers).
-- The reset page verifies the logged-in freelancer's OWN email against this
-- table (email-only check); it does NOT decide which environment they reset.
-- ---------------------------------------------------------------------------
create table if not exists freelancers (
    email       text primary key,                      -- stored lowercased
    name        text,
    active      boolean     not null default true,      -- flip to false to revoke access
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists idx_freelancers_active on freelancers (active);

drop trigger if exists trg_freelancers_updated on freelancers;
create trigger trg_freelancers_updated before update on freelancers
    for each row execute function set_updated_at();

-- ---------------------------------------------------------------------------
-- gab_logins : REMOVED. The sign-in allow-list is the `freelancers` table above.
-- The app reads/writes it via SUPABASE_LOGINS_TABLE (default "freelancers"), so
-- gab_logins was an unused duplicate. Drop it if a legacy deployment still has it.
-- ---------------------------------------------------------------------------
drop table if exists gab_logins cascade;
