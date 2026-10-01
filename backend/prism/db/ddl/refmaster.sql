CREATE TABLE legal_entities (
  entity_id text PRIMARY KEY,
  lei char(20) NOT NULL UNIQUE,
  name text NOT NULL,
  country char(2) NOT NULL,
  region text NOT NULL,
  sector text NOT NULL,
  parent_entity_id text REFERENCES legal_entities (entity_id),
  status text NOT NULL
);
CREATE TABLE securities (
  security_id text PRIMARY KEY,
  isin char(12) NOT NULL UNIQUE,
  cusip char(9),
  sedol char(7),
  ticker text,
  name text NOT NULL,
  asset_class text NOT NULL,
  sub_class text NOT NULL,
  ccy char(3) NOT NULL,
  issuer_entity_id text REFERENCES legal_entities (entity_id),
  country char(2) NOT NULL,
  status text NOT NULL,
  valid_from date NOT NULL,
  valid_to date
);
CREATE INDEX ON securities (asset_class);
CREATE TABLE products (product_id text PRIMARY KEY, name text NOT NULL, product_type text NOT NULL);
CREATE TABLE accounts (
  account_id text PRIMARY KEY,
  product_id text NOT NULL REFERENCES products (product_id),
  name text NOT NULL,
  account_type text NOT NULL,
  owner_entity_id text NOT NULL REFERENCES legal_entities (entity_id),
  region text NOT NULL,
  lifecycle_state text NOT NULL
);
CREATE TABLE corporate_actions (
  ca_id text PRIMARY KEY,
  security_id text NOT NULL REFERENCES securities (security_id),
  event_type text NOT NULL,
  ex_date date NOT NULL,
  pay_date date NOT NULL,
  ratio numeric(12, 6),
  status text NOT NULL
);
CREATE TABLE dq_rules (rule_id text PRIMARY KEY, domain text NOT NULL, name text NOT NULL, severity text NOT NULL);
CREATE TABLE exceptions (
  exc_id text PRIMARY KEY,
  rule_id text NOT NULL REFERENCES dq_rules (rule_id),
  domain text NOT NULL,
  record_ref text NOT NULL,
  asset_class text NOT NULL,
  status text NOT NULL,
  assignee text,
  opened_at timestamptz NOT NULL,
  closed_at timestamptz
);
CREATE INDEX ON exceptions (status, domain);
CREATE TABLE change_requests (
  change_id text PRIMARY KEY,
  domain text NOT NULL,
  record_ref text NOT NULL,
  maker text NOT NULL,
  checker text,
  status text NOT NULL,
  created_at timestamptz NOT NULL
);
CREATE TABLE data_dictionary (
  domain text NOT NULL,
  attribute text NOT NULL,
  definition text NOT NULL,
  owner text NOT NULL,
  source text NOT NULL,
  lineage text NOT NULL,
  PRIMARY KEY (domain, attribute)
);
