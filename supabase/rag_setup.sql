-- GAIA RAG Setup — run this in Supabase SQL Editor

-- 1. Enable pgvector
create extension if not exists vector;

-- 2. Documents table
create table if not exists public.documents (
  id bigserial primary key,
  user_id uuid references auth.users(id) on delete cascade,
  filename text not null,
  file_type text,
  file_size int,
  page_count int,
  chunk_count int,
  created_at timestamptz default now()
);

create index if not exists idx_documents_user on public.documents(user_id);

-- 3. Document chunks table
create table if not exists public.document_chunks (
  id bigserial primary key,
  document_id bigint references public.documents(id) on delete cascade,
  user_id uuid references auth.users(id) on delete cascade,
  chunk_index int not null,
  content text not null,
  embedding vector(384),
  created_at timestamptz default now()
);

create index if not exists idx_chunks_doc on public.document_chunks(document_id);
create index if not exists idx_chunks_user on public.document_chunks(user_id);
create index if not exists idx_chunks_embedding on public.document_chunks
  using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- 4. Similarity search function
create or replace function match_document_chunks(
  query_embedding vector(384),
  match_user_id uuid,
  match_count int default 5,
  filter_document_id bigint default null
)
returns table (
  id bigint,
  document_id bigint,
  chunk_index int,
  content text,
  similarity float
)
language sql stable
as $$
  select
    dc.id,
    dc.document_id,
    dc.chunk_index,
    dc.content,
    1 - (dc.embedding <=> query_embedding) as similarity
  from public.document_chunks dc
  where dc.user_id = match_user_id
    and (filter_document_id is null or dc.document_id = filter_document_id)
    and dc.embedding is not null
  order by dc.embedding <=> query_embedding
  limit match_count;
$$;

-- 5. RLS
alter table public.documents enable row level security;
alter table public.document_chunks enable row level security;

drop policy if exists "own documents" on public.documents;
create policy "own documents" on public.documents
  for all using (auth.uid() = user_id);

drop policy if exists "own chunks" on public.document_chunks;
create policy "own chunks" on public.document_chunks
  for all using (auth.uid() = user_id);

-- 6. Admin audit table (used by main.py admin endpoints)
create table if not exists public.admin_audit (
  id bigserial primary key,
  admin_id uuid,
  action text not null,
  target_user_id uuid,
  details jsonb,
  created_at timestamptz default now()
);
