-- RepoMind schema. Safe to run on a fresh database AND on one created from the earlier version
-- (every statement is idempotent). Run in the Supabase SQL editor.
create extension if not exists vector;

create table if not exists repos (
  id             uuid primary key default gen_random_uuid(),
  url            text not null,
  name           text,
  commit_sha     text,
  status         text not null default 'pending'
                 check (status in ('pending','cloning','indexing','ready','failed')),
  error          text,
  files_indexed  int  not null default 0,
  chunks_indexed int  not null default 0,
  created_by     uuid,                      -- first user who indexed it (index is shared; repos are public)
  created_at     timestamptz not null default now(),
  indexed_at     timestamptz
);
alter table repos add column if not exists created_by uuid;
create unique index if not exists repos_url_key on repos (url);
create index if not exists repos_created_by_idx on repos (created_by, created_at);

create table if not exists chunks (
  id         bigserial primary key,
  repo_id    uuid not null references repos(id) on delete cascade,
  path       text not null,
  language   text,
  symbol     text,
  kind       text,
  start_line int  not null,
  end_line   int  not null,
  content    text not null,
  embedding  vector(768),   -- keep in sync with EMBED_DIM
  tsv        tsvector generated always as (
               to_tsvector('simple', coalesce(symbol,'') || ' ' || path || ' ' || content)
             ) stored
);
create index if not exists chunks_repo_idx      on chunks (repo_id);
create index if not exists chunks_tsv_idx       on chunks using gin (tsv);
create index if not exists chunks_embedding_idx on chunks using hnsw (embedding vector_cosine_ops);

create table if not exists queries (
  id         uuid primary key default gen_random_uuid(),
  repo_id    uuid not null references repos(id) on delete cascade,
  user_id    uuid,                          -- owner (Supabase auth user id)
  question   text not null,
  answer     text,
  status     text not null default 'running'
             check (status in ('running','completed','needs_clarification','awaiting_approval','error')),
  created_at timestamptz not null default now()
);
alter table queries add column if not exists user_id uuid;
create index if not exists queries_user_idx on queries (user_id, created_at desc);

create table if not exists traces (
  id                uuid primary key default gen_random_uuid(),
  query_id          uuid not null references queries(id) on delete cascade,
  steps             jsonb not null default '[]',   -- every SSE event emitted for the query
  guardrail_events  jsonb not null default '[]',
  tokens_in         int  not null default 0,
  tokens_out        int  not null default 0,
  latency_ms        int  not null default 0,
  cost_usd          numeric(10,6) not null default 0
);

create table if not exists pending_actions (
  id                  uuid primary key default gen_random_uuid(),
  query_id            uuid references queries(id) on delete set null,
  repo_id             uuid not null references repos(id) on delete cascade,
  user_id             uuid,
  kind                text not null check (kind in ('patch','issue')),
  title               text not null,
  payload             jsonb not null,
  status              text not null default 'pending'
                      check (status in ('pending','approved','edited','rejected')),
  edited_payload      jsonb,
  publish_started_at  timestamptz,             -- claim marker: prevents double-publishing
  result              jsonb,                   -- {kind, url, number, branch?} once published
  created_at          timestamptz not null default now(),
  decided_at          timestamptz
);
alter table pending_actions add column if not exists user_id uuid;
alter table pending_actions add column if not exists publish_started_at timestamptz;
alter table pending_actions add column if not exists result jsonb;
create index if not exists pending_actions_user_idx on pending_actions (user_id);

create table if not exists feedback (
  id          bigserial primary key,
  query_id    uuid not null references queries(id) on delete cascade,
  user_id     uuid,
  rating      smallint not null check (rating in (-1, 1)),
  correction  text,
  created_at  timestamptz not null default now()
);
alter table feedback add column if not exists user_id uuid;

-- SECURITY: Supabase exposes every table in `public` through its REST API using the public anon key.
-- Turning RLS on with NO policies blocks all of that access. The backend connects with the database
-- role from your connection string (which bypasses RLS) and enforces ownership in code (store.py).
alter table repos           enable row level security;
alter table chunks          enable row level security;
alter table queries         enable row level security;
alter table traces          enable row level security;
alter table pending_actions enable row level security;
alter table feedback        enable row level security;
