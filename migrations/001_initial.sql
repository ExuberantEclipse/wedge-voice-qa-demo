create table if not exists public.test_cases (
  id text not null,
  version integer not null check (version > 0),
  title text not null,
  priority text not null check (priority in ('critical', 'high', 'medium', 'low')),
  definition jsonb not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (id, version)
);

create table if not exists public.test_runs (
  id uuid primary key default gen_random_uuid(),
  label text not null,
  mode text not null check (mode in ('manual', 'automated_inbound')),
  status text not null default 'queued' check (status in ('queued', 'running', 'completed', 'failed', 'cancelled')),
  agent_version_label text,
  created_by text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz
);

create table if not exists public.call_assignments (
  id uuid primary key default gen_random_uuid(),
  test_run_id uuid not null references public.test_runs(id) on delete cascade,
  test_case_id text not null,
  test_case_version integer not null,
  called_number text not null check (called_number ~ '^\+[1-9][0-9]{7,14}$'),
  scenario_snapshot jsonb not null,
  status text not null default 'queued' check (status in ('queued', 'claimed', 'completed', 'failed', 'cancelled')),
  livekit_room text,
  sip_call_id text,
  error text,
  created_at timestamptz not null default now(),
  claimed_at timestamptz,
  completed_at timestamptz,
  foreign key (test_case_id, test_case_version)
    references public.test_cases(id, version)
);

create index if not exists call_assignments_claim_idx
  on public.call_assignments (called_number, created_at)
  where status = 'queued';

create index if not exists call_assignments_test_run_id_idx
  on public.call_assignments (test_run_id);

create index if not exists call_assignments_test_case_idx
  on public.call_assignments (test_case_id, test_case_version);

create table if not exists public.call_attempts (
  id uuid primary key default gen_random_uuid(),
  assignment_id uuid not null unique references public.call_assignments(id) on delete cascade,
  test_run_id uuid not null references public.test_runs(id) on delete cascade,
  test_case_id text not null,
  test_case_version integer not null,
  external_call_id text,
  livekit_room text,
  sip_call_id text,
  status text not null check (status in ('in_progress', 'completed', 'failed')),
  outcome text,
  ended_reason text,
  error text,
  started_at timestamptz not null default now(),
  ended_at timestamptz,
  foreign key (test_case_id, test_case_version)
    references public.test_cases(id, version)
);

create table if not exists public.call_artifacts (
  id uuid primary key default gen_random_uuid(),
  call_attempt_id uuid not null references public.call_attempts(id) on delete cascade,
  kind text not null check (kind in ('recording', 'transcript', 'portal_call', 'raw_event_log')),
  uri text not null,
  checksum text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists call_attempts_test_run_id_idx
  on public.call_attempts (test_run_id);

create index if not exists call_attempts_test_case_idx
  on public.call_attempts (test_case_id, test_case_version);

create index if not exists call_artifacts_call_attempt_id_idx
  on public.call_artifacts (call_attempt_id);

create table if not exists public.call_events (
  id uuid primary key default gen_random_uuid(),
  call_attempt_id uuid not null references public.call_attempts(id) on delete cascade,
  sequence bigint generated always as identity,
  offset_ms integer check (offset_ms is null or offset_ms >= 0),
  type text not null,
  actor text not null check (actor in ('agent', 'caller', 'system', 'staff')),
  text text,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists call_events_attempt_sequence_idx
  on public.call_events (call_attempt_id, sequence);

create table if not exists public.grader_results (
  id uuid primary key default gen_random_uuid(),
  call_attempt_id uuid not null references public.call_attempts(id) on delete cascade,
  grader_id text not null,
  grader_version text not null,
  status text not null check (status in ('pass', 'fail', 'not_evaluable', 'error')),
  score numeric,
  evidence jsonb not null default '[]'::jsonb,
  raw_output jsonb,
  created_at timestamptz not null default now(),
  unique (call_attempt_id, grader_id, grader_version)
);

create table if not exists public.findings (
  id uuid primary key default gen_random_uuid(),
  signature text not null unique,
  title text not null,
  category text not null,
  severity text not null check (severity in ('critical', 'high', 'medium', 'low')),
  status text not null default 'candidate' check (
    status in ('candidate', 'confirmed', 'acknowledged', 'fixed', 'verified', 'duplicate', 'not_reproducible', 'not_a_bug')
  ),
  actual_behavior text not null,
  expected_behavior text not null,
  reproduction_steps text not null,
  suspected_cause text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.finding_occurrences (
  id uuid primary key default gen_random_uuid(),
  finding_id uuid not null references public.findings(id) on delete cascade,
  call_attempt_id uuid not null references public.call_attempts(id) on delete cascade,
  artifact_id uuid references public.call_artifacts(id) on delete set null,
  offset_ms integer check (offset_ms is null or offset_ms >= 0),
  evidence_note text not null,
  created_at timestamptz not null default now()
);

create index if not exists finding_occurrences_finding_idx
  on public.finding_occurrences (finding_id, call_attempt_id);

create index if not exists finding_occurrences_call_attempt_id_idx
  on public.finding_occurrences (call_attempt_id);

create index if not exists finding_occurrences_artifact_id_idx
  on public.finding_occurrences (artifact_id)
  where artifact_id is not null;

alter table public.test_cases enable row level security;
alter table public.test_runs enable row level security;
alter table public.call_assignments enable row level security;
alter table public.call_attempts enable row level security;
alter table public.call_artifacts enable row level security;
alter table public.call_events enable row level security;
alter table public.grader_results enable row level security;
alter table public.findings enable row level security;
alter table public.finding_occurrences enable row level security;

revoke all on table
  public.test_cases,
  public.test_runs,
  public.call_assignments,
  public.call_attempts,
  public.call_artifacts,
  public.call_events,
  public.grader_results,
  public.findings,
  public.finding_occurrences
from public, anon, authenticated;

grant select, insert, update, delete on table
  public.test_cases,
  public.test_runs,
  public.call_assignments,
  public.call_attempts,
  public.call_artifacts,
  public.call_events,
  public.grader_results,
  public.findings,
  public.finding_occurrences
to service_role;

grant usage, select on sequence public.call_events_sequence_seq to service_role;

create or replace function public.claim_next_call_assignment(
  p_called_number text,
  p_room_name text,
  p_sip_call_id text default null
)
returns setof public.call_assignments
language plpgsql
security definer
set search_path = ''
as $$
declare
  selected_id uuid;
begin
  select id
    into selected_id
    from public.call_assignments
   where status = 'queued'
     and called_number = p_called_number
   order by created_at asc
   for update skip locked
   limit 1;

  if selected_id is null then
    return;
  end if;

  update public.test_runs
     set status = 'running',
         started_at = coalesce(started_at, now())
   where id = (
     select test_run_id from public.call_assignments where id = selected_id
   );

  return query
    update public.call_assignments
       set status = 'claimed',
           claimed_at = now(),
           livekit_room = p_room_name,
           sip_call_id = p_sip_call_id
     where id = selected_id
     returning *;
end;
$$;

revoke all on function public.claim_next_call_assignment(text, text, text) from public;
revoke all on function public.claim_next_call_assignment(text, text, text) from anon;
revoke all on function public.claim_next_call_assignment(text, text, text) from authenticated;
grant execute on function public.claim_next_call_assignment(text, text, text) to service_role;
