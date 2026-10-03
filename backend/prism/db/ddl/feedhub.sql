CREATE TABLE sources (
  source_id text PRIMARY KEY,
  name text NOT NULL,
  source_type text NOT NULL,
  bic text NOT NULL,
  country char(2) NOT NULL
);
CREATE TABLE feeds (
  feed_id text PRIMARY KEY,
  source_id text NOT NULL REFERENCES sources,
  data_type text NOT NULL,
  format text NOT NULL,
  frequency text NOT NULL,
  expected_by_utc time NOT NULL,
  source_type text NOT NULL
);
CREATE TABLE feed_deliveries (
  delivery_id text PRIMARY KEY,
  feed_id text NOT NULL REFERENCES feeds,
  source_id text NOT NULL,
  business_date date NOT NULL,
  status text NOT NULL,
  received_at timestamptz,
  latency_min int,
  record_count int,
  error_code text,
  source_type text NOT NULL,
  feed_type text NOT NULL
);
CREATE INDEX ON feed_deliveries (business_date, status);
CREATE TABLE support_tickets (
  ticket_id text PRIMARY KEY,
  feed_id text NOT NULL REFERENCES feeds,
  source_id text NOT NULL,
  category text NOT NULL,
  status text NOT NULL,
  opened_at timestamptz NOT NULL,
  closed_at timestamptz,
  source_type text NOT NULL
);
