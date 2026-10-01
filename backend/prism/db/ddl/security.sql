-- Signed security context: RLS policies trust only claims whose HMAC verifies with a key
-- the query role cannot read. Context format: base64(json) || '.' || hex(hmac_sha256).
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS prism_sec;
REVOKE ALL ON SCHEMA prism_sec FROM PUBLIC;
CREATE TABLE prism_sec.hmac_key (
  id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  k text NOT NULL CHECK (length(k) >= 32)
);
REVOKE ALL ON prism_sec.hmac_key FROM PUBLIC;

CREATE OR REPLACE FUNCTION prism_sec.claims() RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  raw text := current_setting('app.ctx', true);
  v_key text;
  payload text;
  sig text;
  c jsonb;
BEGIN
  IF raw IS NULL OR raw = '' THEN
    RETURN NULL;
  END IF;
  payload := split_part(raw, '.', 1);
  sig := split_part(raw, '.', 2);
  SELECT k INTO v_key FROM prism_sec.hmac_key;
  IF v_key IS NULL THEN
    RAISE EXCEPTION 'security key not configured' USING ERRCODE = '42501';
  END IF;
  IF encode(public.hmac(payload, v_key, 'sha256'), 'hex') IS DISTINCT FROM sig THEN
    RAISE EXCEPTION 'invalid security context' USING ERRCODE = '42501';
  END IF;
  c := convert_from(decode(payload, 'base64'), 'UTF8')::jsonb;
  IF (c ->> 'exp') IS NULL OR (c ->> 'exp')::bigint < extract(epoch FROM clock_timestamp()) THEN
    RAISE EXCEPTION 'security context expired' USING ERRCODE = '42501';
  END IF;
  RETURN c;
END $$;

CREATE OR REPLACE FUNCTION prism_sec.can(db text, tbl text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT COALESCE((prism_sec.claims() -> 'scopes') ?| ARRAY[db, db || '.' || tbl], false)
$$;

CREATE OR REPLACE FUNCTION prism_sec.allowed(dim text) RETURNS text[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT ARRAY(SELECT jsonb_array_elements_text(prism_sec.claims() -> 'rows' -> dim))
$$;

CREATE OR REPLACE FUNCTION prism_sec.has_scope(s text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT COALESCE((prism_sec.claims() -> 'scopes') ? s, false)
$$;
