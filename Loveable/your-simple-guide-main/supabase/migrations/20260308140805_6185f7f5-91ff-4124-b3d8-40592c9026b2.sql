
CREATE TABLE public.control_log (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  zone_name text NOT NULL,
  action text NOT NULL, -- 'heating_on', 'heating_off', 'no_change', 'ventilate_suggest'
  humidity_airzone integer,
  humidity_netatmo integer,
  temperature numeric,
  outdoor_humidity integer,
  outdoor_temp numeric,
  reason text,
  success boolean DEFAULT true
);

-- Index for efficient querying
CREATE INDEX idx_control_log_created ON public.control_log (created_at DESC);
CREATE INDEX idx_control_log_zone ON public.control_log (zone_name, created_at DESC);

-- No RLS needed - this is a system table accessed only by edge functions
ALTER TABLE public.control_log ENABLE ROW LEVEL SECURITY;

-- Allow edge functions (service role) full access, no public access
CREATE POLICY "Service role only" ON public.control_log
  FOR ALL USING (false);
