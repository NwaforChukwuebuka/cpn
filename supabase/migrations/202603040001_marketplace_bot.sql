create extension if not exists "pgcrypto";

create table if not exists public.users (
    id uuid primary key default gen_random_uuid(),
    telegram_id bigint not null unique,
    username text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.orders (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id) on delete cascade,
    status text not null check (status in ('pending_payment', 'paid', 'processing', 'completed', 'failed')),
    btcpay_invoice_id text,
    btcpay_checkout_url text,
    amount_usd numeric(10, 2) not null,
    currency text not null default 'USD',
    profile_snapshot jsonb not null,
    result_csv_path text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_orders_user_id on public.orders(user_id);
create index if not exists idx_orders_status on public.orders(status);
create unique index if not exists idx_orders_btcpay_invoice on public.orders(btcpay_invoice_id) where btcpay_invoice_id is not null;

create table if not exists public.payments (
    id uuid primary key default gen_random_uuid(),
    order_id uuid not null references public.orders(id) on delete cascade,
    invoice_id text not null,
    amount numeric(10, 2),
    currency text,
    status text not null,
    payload jsonb,
    paid_at timestamptz,
    created_at timestamptz not null default now()
);

create index if not exists idx_payments_order_id on public.payments(order_id);
create index if not exists idx_payments_invoice_id on public.payments(invoice_id);

create table if not exists public.workflow_jobs (
    id uuid primary key default gen_random_uuid(),
    order_id uuid not null references public.orders(id) on delete cascade,
    job_id text not null unique,
    status text not null check (status in ('queued', 'running', 'succeeded', 'failed')),
    result jsonb,
    error text,
    created_at timestamptz not null default now(),
    started_at timestamptz,
    completed_at timestamptz
);

create index if not exists idx_workflow_jobs_order_id on public.workflow_jobs(order_id);
create index if not exists idx_workflow_jobs_status on public.workflow_jobs(status);
