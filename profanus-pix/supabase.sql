-- Execute uma vez em Supabase > SQL Editor, em um projeto novo.
-- Pedidos só são acessíveis pelo backend; não crie políticas públicas.
create table if not exists public.pix_orders (
  id text primary key,
  session text not null,
  amount integer not null check (amount >= 100),
  gateway_id text,
  code text,
  qr text,
  status text not null default 'creating'
    check (status in ('creating', 'pending', 'paid', 'expired', 'cancelled')),
  expires text,
  created double precision not null,
  checked double precision not null default 0
);
create index if not exists pix_orders_session_created
  on public.pix_orders(session, created desc);
create unique index if not exists pix_orders_active_session
  on public.pix_orders(session)
  where status in ('creating', 'pending', 'paid');
alter table public.pix_orders enable row level security;
revoke all on public.pix_orders from anon, authenticated;
-- O backend usa a conexão PostgreSQL obtida em Connect > Session pooler.
-- Configure o bucket videos-pagos como PRIVATE no painel Storage.
