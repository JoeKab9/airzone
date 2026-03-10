
-- Table to store Netatmo 5-minute interval readings
CREATE TABLE public.netatmo_readings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  module_name text NOT NULL,
  timestamp timestamptz NOT NULL,
  temperature numeric,
  humidity numeric,
  co2 integer,
  noise numeric,
  pressure numeric,
  created_at timestamptz DEFAULT now()
);

-- Index for fast time-range queries per module
CREATE INDEX idx_netatmo_readings_module_ts ON public.netatmo_readings (module_name, timestamp DESC);

-- Unique constraint to avoid duplicates
CREATE UNIQUE INDEX idx_netatmo_readings_unique ON public.netatmo_readings (module_name, timestamp);

ALTER TABLE public.netatmo_readings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow public read on netatmo_readings"
  ON public.netatmo_readings FOR SELECT
  TO anon, authenticated
  USING (true);

CREATE POLICY "Service role write netatmo_readings"
  ON public.netatmo_readings FOR ALL
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Sync status table for resumable backfill
CREATE TABLE public.netatmo_sync_status (
  module_name text PRIMARY KEY,
  device_id text NOT NULL,
  module_id text,
  module_type text NOT NULL,
  last_synced_ts bigint NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'pending',
  updated_at timestamptz DEFAULT now()
);

ALTER TABLE public.netatmo_sync_status ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow public read on netatmo_sync_status"
  ON public.netatmo_sync_status FOR SELECT
  TO anon, authenticated
  USING (true);

CREATE POLICY "Service role write netatmo_sync_status"
  ON public.netatmo_sync_status FOR ALL
  TO service_role
  USING (true)
  WITH CHECK (true);
