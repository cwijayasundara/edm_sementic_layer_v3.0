CREATE TABLE vendors (vendor_id text PRIMARY KEY, name text NOT NULL, rank_default int NOT NULL);
CREATE TABLE instruments (
  security_id text PRIMARY KEY,
  isin char(12) NOT NULL UNIQUE,
  name text NOT NULL,
  asset_class text NOT NULL,
  ccy char(3) NOT NULL
);
CREATE TABLE golden_prices (
  security_id text NOT NULL REFERENCES instruments,
  price_date date NOT NULL,
  value numeric(20, 6) NOT NULL,
  chosen_vendor_id text NOT NULL REFERENCES vendors,
  rule text NOT NULL,
  asset_class text NOT NULL,
  PRIMARY KEY (security_id, price_date)
);
CREATE TABLE vendor_prices (
  security_id text NOT NULL REFERENCES instruments,
  vendor_id text NOT NULL REFERENCES vendors,
  price_date date NOT NULL,
  price_type text NOT NULL,
  value numeric(20, 6) NOT NULL,
  ccy char(3) NOT NULL,
  received_at timestamptz NOT NULL,
  asset_class text NOT NULL,
  PRIMARY KEY (security_id, vendor_id, price_date, price_type)
);
CREATE TABLE price_suspects (
  suspect_id text PRIMARY KEY,
  security_id text NOT NULL REFERENCES instruments,
  vendor_id text NOT NULL REFERENCES vendors,
  price_date date NOT NULL,
  kind text NOT NULL,
  deviation_pct numeric(10, 4),
  status text NOT NULL,
  asset_class text NOT NULL
);
CREATE INDEX ON price_suspects (price_date, kind);
CREATE TABLE dq_stage_metrics (
  business_date date NOT NULL,
  domain text NOT NULL,
  stage text NOT NULL,
  count int NOT NULL,
  sla_met boolean NOT NULL,
  PRIMARY KEY (business_date, domain, stage)
);
CREATE TABLE esg_scores (
  entity_id text NOT NULL,
  provider text NOT NULL,
  as_of date NOT NULL,
  score numeric(5, 2) NOT NULL,
  PRIMARY KEY (entity_id, provider, as_of)
);
