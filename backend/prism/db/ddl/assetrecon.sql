CREATE TABLE custodians (custodian_id text PRIMARY KEY, name text NOT NULL, feed_source_id text NOT NULL);
CREATE TABLE portfolios (
  portfolio_id text PRIMARY KEY,
  name text NOT NULL,
  fund_group text NOT NULL,
  base_ccy char(3) NOT NULL,
  custodian_id text NOT NULL REFERENCES custodians,
  region text NOT NULL,
  fund_entity_id text NOT NULL,
  custodian_source_id text NOT NULL
);
CREATE TABLE internal_positions (
  portfolio_id text NOT NULL REFERENCES portfolios,
  security_id text NOT NULL,
  book text NOT NULL,
  qty numeric(20, 4) NOT NULL,
  mv numeric(20, 2) NOT NULL,
  as_of date NOT NULL,
  fund_group text NOT NULL,
  PRIMARY KEY (portfolio_id, security_id, book, as_of)
);
CREATE TABLE custodian_positions (
  portfolio_id text NOT NULL REFERENCES portfolios,
  security_id text NOT NULL,
  qty numeric(20, 4) NOT NULL,
  mv numeric(20, 2) NOT NULL,
  as_of date NOT NULL,
  delivery_id text NOT NULL,
  fund_group text NOT NULL,
  PRIMARY KEY (portfolio_id, security_id, as_of)
);
CREATE TABLE internal_transactions (
  txn_id text PRIMARY KEY,
  portfolio_id text NOT NULL REFERENCES portfolios,
  security_id text NOT NULL,
  trade_date date NOT NULL,
  settle_date date NOT NULL,
  txn_type text NOT NULL,
  qty numeric(20, 4) NOT NULL,
  amount numeric(20, 2) NOT NULL,
  fund_group text NOT NULL
);
CREATE TABLE custodian_transactions (
  txn_id text PRIMARY KEY,
  portfolio_id text NOT NULL REFERENCES portfolios,
  security_id text NOT NULL,
  trade_date date NOT NULL,
  settle_date date NOT NULL,
  txn_type text NOT NULL,
  qty numeric(20, 4) NOT NULL,
  amount numeric(20, 2) NOT NULL,
  internal_ref text,
  fund_group text NOT NULL
);
CREATE TABLE recon_runs (
  run_id text PRIMARY KEY,
  portfolio_id text NOT NULL REFERENCES portfolios,
  recon_type text NOT NULL,
  business_date date NOT NULL,
  matched int NOT NULL,
  unmatched int NOT NULL,
  status text NOT NULL,
  signed_off_by text,
  fund_group text NOT NULL
);
CREATE TABLE recon_exceptions (
  exc_id text PRIMARY KEY,
  run_id text NOT NULL REFERENCES recon_runs,
  portfolio_id text NOT NULL,
  security_id text NOT NULL,
  business_date date NOT NULL,
  diff_qty numeric(20, 4) NOT NULL,
  diff_mv numeric(20, 2) NOT NULL,
  cause_code text NOT NULL,
  assigned_to text,
  status text NOT NULL,
  sla_due date NOT NULL,
  fund_group text NOT NULL
);
CREATE TABLE nav_checks (
  portfolio_id text NOT NULL REFERENCES portfolios,
  nav_date date NOT NULL,
  admin_nav numeric(22, 2) NOT NULL,
  internal_nav numeric(22, 2) NOT NULL,
  diff_bps numeric(10, 3) NOT NULL,
  fund_group text NOT NULL,
  PRIMARY KEY (portfolio_id, nav_date)
);
