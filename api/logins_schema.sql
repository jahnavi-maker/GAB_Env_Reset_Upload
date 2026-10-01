-- Run once in the Supabase SQL editor.
-- gab_logins = who may Google-sign-in on /reset.
-- gab_accounts = the seeded emails that actually get reset. Do not mix them.

create table if not exists gab_logins (
    email       text primary key,
    name        text,
    active      boolean     not null default true,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists idx_gab_logins_active on gab_logins (active);

create or replace function set_updated_at() returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;

drop trigger if exists trg_gab_logins_updated on gab_logins;
create trigger trg_gab_logins_updated before update on gab_logins
    for each row execute function set_updated_at();
