-- Source-of-truth accounts table for the reset service.
-- One row per demo Google account. Populated when an account is authorized
-- (token saved here), and read by the API to resolve persona + auth status.
-- Run once in the Supabase SQL editor.

create table if not exists gab_accounts (
    email               text primary key,
    persona             text not null,

    -- Operational context. NOTE: password and refresh_token are sensitive.
    -- Keep this table reachable only via the service-role key (server side).
    -- Consider encrypting these columns (pgcrypto) before production.
    password            text,          -- for rater / Gemini login; optional
    refresh_token       text,          -- saved when the account is authorized
    token_json          jsonb,         -- full Google authorized-user credential (optional)
    scopes              text[],

    authorized          boolean not null default false,
    authorized_at       timestamptz,
    status              text not null default 'active',   -- active | revoked

    -- Supports the "same persona -> reset / new persona -> reseed" routing:
    -- the current baseline the account is seeded to.
    last_reset_persona  text,
    last_reset_id       uuid,
    last_reset_at       timestamptz,

    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create index if not exists idx_gab_accounts_persona on gab_accounts (persona);
create index if not exists idx_gab_accounts_authorized on gab_accounts (authorized);

-- Keep updated_at fresh on any change.
create or replace function set_updated_at() returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;

drop trigger if exists trg_gab_accounts_updated on gab_accounts;
create trigger trg_gab_accounts_updated before update on gab_accounts
    for each row execute function set_updated_at();
