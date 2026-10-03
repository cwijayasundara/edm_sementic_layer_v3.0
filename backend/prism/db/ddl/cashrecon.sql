CREATE SCHEMA private;
CREATE TABLE private.cash_accounts (
  account_id text PRIMARY KEY,
  legal_entity_id text NOT NULL,
  bank_source_id text NOT NULL,
  bank_bic text NOT NULL,
  nostro_no text NOT NULL,
  ccy char(3) NOT NULL,
  region text NOT NULL
);
CREATE TABLE statements (
  stmt_id text PRIMARY KEY,
  account_id text NOT NULL REFERENCES private.cash_accounts,
  msg_type text NOT NULL,
  stmt_no int NOT NULL,
  value_date date NOT NULL,
  opening_bal numeric(20, 2) NOT NULL,
  closing_bal numeric(20, 2) NOT NULL,
  region text NOT NULL
);
CREATE TABLE statement_entries (
  entry_id text PRIMARY KEY,
  stmt_id text NOT NULL REFERENCES statements,
  account_id text NOT NULL,
  value_date date NOT NULL,
  amount numeric(20, 2) NOT NULL,
  dc char(1) NOT NULL,
  reference text NOT NULL,
  narrative text NOT NULL,
  region text NOT NULL
);
CREATE TABLE ledger_entries (
  entry_id text PRIMARY KEY,
  account_id text NOT NULL REFERENCES private.cash_accounts,
  gl_ref text NOT NULL,
  amount numeric(20, 2) NOT NULL,
  dc char(1) NOT NULL,
  booking_date date NOT NULL,
  reference text NOT NULL,
  region text NOT NULL
);
CREATE TABLE match_rules (
  rule_id text PRIMARY KEY,
  name text NOT NULL,
  cardinality text NOT NULL,
  tol_amount numeric(12, 2),
  tol_days int
);
CREATE TABLE match_groups (
  match_id text PRIMARY KEY,
  rule_id text NOT NULL REFERENCES match_rules,
  status text NOT NULL,
  matched_at timestamptz NOT NULL,
  matched_by text NOT NULL,
  region text NOT NULL
);
CREATE TABLE match_items (
  match_id text NOT NULL REFERENCES match_groups,
  side text NOT NULL,
  entry_id text NOT NULL,
  region text NOT NULL,
  PRIMARY KEY (match_id, side, entry_id)
);
CREATE TABLE breaks (
  break_id text PRIMARY KEY,
  account_id text NOT NULL REFERENCES private.cash_accounts,
  legal_entity_id text NOT NULL,
  break_type text NOT NULL,
  amount numeric(20, 2) NOT NULL,
  ccy char(3) NOT NULL,
  opened_on date NOT NULL,
  age_days int NOT NULL,
  status text NOT NULL,
  owner text,
  root_cause text,
  region text NOT NULL,
  bank_source_id text NOT NULL
);
CREATE INDEX ON breaks (status, ccy);
CREATE TABLE break_actions (
  action_id text PRIMARY KEY,
  break_id text NOT NULL REFERENCES breaks,
  action text NOT NULL,
  actor text NOT NULL,
  ts timestamptz NOT NULL,
  comment text,
  region text NOT NULL
);
-- Masking view: account numbers are visible only with the pii:read scope.
-- Owned by a non-superuser so the base table's RLS still applies through the view.
CREATE VIEW public.cash_accounts WITH (security_barrier = true) AS
  SELECT account_id, legal_entity_id, bank_source_id, bank_bic,
         CASE WHEN (SELECT prism_sec.has_scope('pii:read')) THEN nostro_no
              ELSE '****' || right(nostro_no, 4) END AS nostro_no,
         ccy, region
  FROM private.cash_accounts;
ALTER VIEW public.cash_accounts OWNER TO prism_view_owner;
GRANT USAGE ON SCHEMA private TO prism_view_owner;
GRANT SELECT ON private.cash_accounts TO prism_view_owner;
