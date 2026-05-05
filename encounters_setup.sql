-- encounters_setup.sql
-- Run once in Supabase SQL Editor

-- MONSTR asset registry (optional — assets are hardcoded in encounters.py)
create table if not exists monstr_assets (
    asa_id      text primary key,
    name        text not null,
    ipfs_hash   text not null,
    active      boolean default true
);

-- Encounter log
create table if not exists encounters (
    id                      uuid primary key default gen_random_uuid(),
    monstr_asa              text not null,
    monstr_name             text not null,
    max_hp                  integer not null,
    is_boss                 boolean default false,
    started_at              timestamptz not null,
    ended_at                timestamptz,
    status                  text default 'active',
    total_attackers         integer default 0,
    total_goo_distributed   integer default 0
);

-- Per-attack records
create table if not exists encounter_attacks (
    id                  uuid primary key default gen_random_uuid(),
    encounter_id        uuid references encounters(id),
    user_id             text not null,
    damage_dealt        integer default 0,
    tagged_user_id      text,
    goo_earned          integer default 0,
    got_first_strike    boolean default false,
    got_kill_shot       boolean default false,
    attacked_at         timestamptz default now()
);

-- Weekly stats (resets Monday — new row per user per week)
create table if not exists weekly_stats (
    id                  uuid primary key default gen_random_uuid(),
    user_id             text not null,
    week_start          text not null,   -- e.g. "2024-01-15"
    total_damage        integer default 0,
    kill_shots          integer default 0,
    encounters_joined   integer default 0,
    unique(user_id, week_start)
);

-- Linked Algorand wallets
create table if not exists linked_wallets (
    user_id         text primary key,
    wallet_address  text not null,
    linked_at       timestamptz default now()
);

-- Pending GOO (held for unlinked / unopted-in players)
create table if not exists pending_goo (
    user_id     text primary key,
    amount      integer default 0,
    updated_at  timestamptz default now()
);

-- Indexes
create index if not exists idx_encounter_attacks_user     on encounter_attacks(user_id);
create index if not exists idx_encounter_attacks_encounter on encounter_attacks(encounter_id);
create index if not exists idx_weekly_stats_user_week     on weekly_stats(user_id, week_start);
create index if not exists idx_encounters_boss            on encounters(is_boss, started_at);
