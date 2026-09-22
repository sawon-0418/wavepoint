-- Supabase SQL Editor에서 한 번 실행하세요. RLS는 켜 두고 서버(Service Role)만 쓰는 구조입니다.
create table if not exists public.posts (
  id uuid primary key default gen_random_uuid(),
  spot_id text not null,
  author text not null default '익명 낚시꾼',
  content text not null,
  species text,
  length numeric,
  length_is_ai boolean not null default false,
  image_url text,
  likes integer not null default 0,
  created_at timestamptz not null default now()
);
alter table public.posts add column if not exists author_id uuid references auth.users(id), add column if not exists is_hidden boolean not null default false;
alter table public.posts add column if not exists hidden_at timestamptz, add column if not exists hidden_by uuid references auth.users(id);
create table if not exists public.reports (
  id uuid primary key default gen_random_uuid(), kind text not null, target_id text,
  reason text, message text not null, contact text, created_at timestamptz not null default now()
);
alter table public.reports add column if not exists user_id uuid references auth.users(id), add column if not exists status text not null default 'pending', add column if not exists reviewed_at timestamptz, add column if not exists reviewed_by uuid references auth.users(id), add column if not exists admin_note text;
create table if not exists public.inquiries (
  id uuid primary key default gen_random_uuid(), kind text not null, target_id text,
  reason text, message text not null, contact text, created_at timestamptz not null default now()
);
alter table public.inquiries add column if not exists user_id uuid references auth.users(id), add column if not exists status text not null default 'pending', add column if not exists reviewed_at timestamptz, add column if not exists reviewed_by uuid references auth.users(id), add column if not exists admin_note text;
create table if not exists public.community_spots (
  id uuid primary key default gen_random_uuid(), title text not null, kind text not null,
  description text not null, species text not null default '새 포인트',
  lat double precision not null, lng double precision not null, address text,
  created_at timestamptz not null default now()
);
alter table public.community_spots add column if not exists user_id uuid references auth.users(id), add column if not exists is_hidden boolean not null default false;
alter table public.community_spots add column if not exists hidden_at timestamptz, add column if not exists hidden_by uuid references auth.users(id);
-- 이미 만든 테이블에도 실제 주소 칼럼을 추가한다.
alter table public.community_spots add column if not exists address text;
-- 포인트 추천은 사용자당 포인트 하나에 한 번만 기록한다. 공식·사용자 공유 포인트 모두 spot_id로 연결한다.
create table if not exists public.spot_recommendations (
  id uuid primary key default gen_random_uuid(),
  spot_id text not null,
  user_id uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  unique (spot_id, user_id)
);
-- 게시글 추천도 계정당 게시글 하나에 하나만 활성화한다.
-- 행 자체를 추천 상태로 사용하므로 같은 계정이 여러 번 눌러도 추천 수가 중복되지 않는다.
create table if not exists public.post_recommendations (
  id uuid primary key default gen_random_uuid(),
  post_id uuid not null references public.posts(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  unique (post_id, user_id)
);
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text not null default '낚시꾼',
  role text not null default 'user' check (role in ('user', 'admin')),
  created_at timestamptz not null default now()
);
-- 운영자 이메일 목록: 아래 예시 이메일을 실제 운영자 이메일로 바꾼 뒤 SQL Editor에서 실행하세요.
create table if not exists public.admin_emails (
  email text primary key,
  created_at timestamptz not null default now()
);
insert into public.admin_emails (email) values ('fish_admin@admin.com') on conflict (email) do nothing;
create or replace function public.handle_new_user() returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, display_name, role)
  values (
    new.id,
    coalesce(new.raw_user_meta_data->>'display_name', '낚시꾼'),
    case when exists (select 1 from public.admin_emails where email = new.email) then 'admin' else 'user' end
  ) on conflict (id) do nothing;
  return new;
end; $$;
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users for each row execute procedure public.handle_new_user();
insert into public.profiles (id, display_name)
select id, coalesce(raw_user_meta_data->>'display_name', '낚시꾼') from auth.users
on conflict (id) do nothing;
create table if not exists public.official_spots (
  id text primary key, title text not null, kind text not null, lat double precision not null,
  lng double precision not null, species text, description text, address text, source text,
  updated_at timestamptz not null default now()
);
create table if not exists public.protected_areas (
  id text primary key, name text not null, code text, geometry jsonb not null,
  updated_at timestamptz not null default now()
);
create table if not exists public.sync_runs (
  id uuid primary key default gen_random_uuid(), source text not null, status text not null,
  item_count integer not null default 0, detail text, created_at timestamptz not null default now()
);
create table if not exists public.admin_audit_logs (
  id uuid primary key default gen_random_uuid(), actor_id uuid not null references auth.users(id),
  action text not null, target_type text not null, target_id text not null,
  note text, created_at timestamptz not null default now()
);
create index if not exists posts_ranking_idx on public.posts (is_hidden, length desc, created_at desc);
create index if not exists posts_spot_idx on public.posts (spot_id, is_hidden, created_at desc);
create index if not exists reports_status_idx on public.reports (status, created_at desc);
create index if not exists inquiries_status_idx on public.inquiries (status, created_at desc);
create index if not exists community_spots_visibility_idx on public.community_spots (is_hidden, created_at desc);
create index if not exists spot_recommendations_spot_idx on public.spot_recommendations (spot_id, created_at desc);
create index if not exists post_recommendations_post_idx on public.post_recommendations (post_id, created_at desc);
alter table public.posts enable row level security;
alter table public.reports enable row level security;
alter table public.inquiries enable row level security;
alter table public.community_spots enable row level security;
alter table public.spot_recommendations enable row level security;
alter table public.post_recommendations enable row level security;
alter table public.official_spots enable row level security;
alter table public.protected_areas enable row level security;
alter table public.sync_runs enable row level security;
alter table public.profiles enable row level security;
alter table public.admin_emails enable row level security;
alter table public.admin_audit_logs enable row level security;
drop function if exists public.increment_post_like(uuid);
revoke all on public.posts, public.reports, public.inquiries, public.community_spots, public.spot_recommendations, public.post_recommendations, public.official_spots, public.protected_areas, public.sync_runs, public.profiles from anon, authenticated;
revoke all on public.admin_audit_logs from anon, authenticated;
-- Data API 자동 공개를 끈 경우에도, 서버의 Service Role에는 필요한 권한을 명시한다.
grant select, insert, update, delete on public.posts, public.reports, public.inquiries, public.community_spots, public.spot_recommendations, public.post_recommendations, public.official_spots, public.protected_areas, public.sync_runs to service_role;
grant select, insert, update, delete on public.profiles to service_role;
grant select, insert, update, delete on public.admin_emails to service_role;
grant select, insert, update, delete on public.admin_audit_logs to service_role;

-- 이미 가입된 운영자도 권한을 반영한다.
update public.profiles p set role = 'admin' from auth.users u join public.admin_emails a on a.email = u.email where p.id = u.id;
-- 카카오 OAuth를 추가하면 Supabase Auth는 "검증된 동일 이메일"의 OAuth identity를 기존 이메일 계정에 자동 연결합니다.
-- 따라서 profiles는 이메일이 아니라 auth.users.id를 기준으로 유지되어 기존 게시글·권한·포인트가 그대로 보존됩니다.

-- 게시글 사진은 서버가 Service Role로만 업로드합니다.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('post-images', 'post-images', true, 52428800, array['image/jpeg','image/png','image/webp'])
on conflict (id) do nothing;
update storage.buckets set file_size_limit = 52428800 where id = 'post-images';

-- 어종별 조과 랭킹: 이 줄부터 마지막 줄까지 기존 DB에도 추가 실행할 수 있습니다.
begin;
create table if not exists public.fish_species (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  normalized_name text not null unique check (normalized_name <> ''),
  created_at timestamptz not null default now()
);
alter table public.fish_species enable row level security;
revoke all on public.fish_species from public, anon, authenticated;
grant select, insert, update, delete on public.fish_species to service_role;
alter table public.posts add column if not exists species_id uuid references public.fish_species(id);

-- UNIQUE와 UPSERT로 동시 등록에도 같은 어종이 중복 생성되지 않습니다.
-- 띄어쓰기·대소문자는 통합하지만 우럭/조피볼락 같은 별칭은 자동 추측하지 않습니다.
create or replace function public.link_post_species() returns trigger
language plpgsql set search_path = '' as $$
declare species_key text;
begin
  species_key := lower(regexp_replace(coalesce(new.species, ''), '[[:space:]]+', '', 'g'));
  if species_key = '' then
    new.species_id := null;
    new.species := null;
  else
    if char_length(species_key) > 30 then
      raise exception '어종은 30자 이내로 입력해 주세요.';
    end if;
    insert into public.fish_species(name, normalized_name)
    values (regexp_replace(new.species, '[[:space:]]+', '', 'g'), species_key)
    on conflict (normalized_name) do update set normalized_name = excluded.normalized_name
    returning id, name into new.species_id, new.species;
  end if;
  return new;
end; $$;
revoke all on function public.link_post_species() from public, anon, authenticated;
grant execute on function public.link_post_species() to service_role;
drop trigger if exists posts_link_species on public.posts;
create trigger posts_link_species before insert or update of species, species_id
on public.posts for each row execute function public.link_post_species();
-- 기존 게시글도 분류합니다. 제목·사진·길이·작성자는 보존합니다.
update public.posts set species = species where species_id is null and nullif(btrim(species), '') is not null;
create index if not exists posts_species_ranking_idx on public.posts(species_id, length desc) where is_hidden = false;

create or replace view public.species_catch_rankings with (security_invoker = true) as
with personal_best as (
  select p.*, row_number() over (
    partition by p.species_id, p.author_id
    order by p.length desc, p.created_at asc, p.id asc
  ) as personal_order
  from public.posts p
  where p.is_hidden = false and p.author_id is not null and p.species_id is not null
    and p.length > 0 and p.length <= 300
    and not exists (select 1 from public.community_spots s where s.id::text = p.spot_id and s.is_hidden)
)
select id, spot_id, author_id, author, species_id, species, length, length_is_ai, created_at,
  rank() over (partition by species_id order by length desc) as rank
from personal_best where personal_order = 1;
revoke all on public.species_catch_rankings from public, anon, authenticated;
grant select on public.species_catch_rankings to service_role;

create or replace function public.get_species_rankings(p_species_id uuid default null, p_viewer_id uuid default null)
returns jsonb language sql stable set search_path = '' as $$
  with chosen as (
    select coalesce(p_species_id, (select id from public.fish_species order by name, id limit 1)) as id
  ), ranked as (
    select r.* from public.species_catch_rankings r where r.species_id = (select id from chosen)
  )
  select jsonb_build_object(
    'species', coalesce((select jsonb_agg(jsonb_build_object('id', id, 'name', name) order by name, id) from public.fish_species), '[]'::jsonb),
    'selectedSpeciesId', (select id from chosen),
    'top', coalesce((select jsonb_agg(to_jsonb(t) order by t.rank, t.created_at, t.id) from (select * from ranked order by rank, created_at, id limit 5) t), '[]'::jsonb),
    'myRank', (select to_jsonb(r) from ranked r where r.author_id = p_viewer_id limit 1)
  );
$$;
revoke all on function public.get_species_rankings(uuid, uuid) from public, anon, authenticated;
grant execute on function public.get_species_rankings(uuid, uuid) to service_role;
notify pgrst, 'reload schema';
commit;
