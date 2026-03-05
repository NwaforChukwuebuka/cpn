-- Payment-to-CPN mapping: track how many CPNs an order entitles and how many have been delivered.
-- Ensures we never deliver more CPNs than paid for and supports retry until fulfilled.

alter table public.orders
    add column if not exists cpns_paid integer not null default 1,
    add column if not exists cpns_delivered integer not null default 0;

comment on column public.orders.cpns_paid is 'Number of CPNs this payment entitles the user to (e.g. 1 or 2).';
comment on column public.orders.cpns_delivered is 'Number of CPNs already successfully delivered for this order.';
