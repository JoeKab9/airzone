
-- System state for learned parameters, occupancy, and running stats
CREATE TABLE public.system_state (
  key TEXT PRIMARY KEY,
  value JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Daily assessments (after 1 week, then daily)
CREATE TABLE public.daily_assessment (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  date DATE NOT NULL UNIQUE,
  avg_humidity_before NUMERIC,
  avg_humidity_after NUMERIC,
  humidity_improved BOOLEAN,
  total_heating_kwh NUMERIC,
  total_cost_eur NUMERIC,
  heating_minutes INTEGER,
  ventilation_suggestions INTEGER,
  occupancy_detected BOOLEAN,
  zones_above_65 INTEGER,
  zones_total INTEGER,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Add forecast and occupancy columns to control_log
ALTER TABLE public.control_log 
  ADD COLUMN IF NOT EXISTS forecast_temp_max NUMERIC,
  ADD COLUMN IF NOT EXISTS forecast_best_hour TEXT,
  ADD COLUMN IF NOT EXISTS occupancy_detected BOOLEAN DEFAULT false,
  ADD COLUMN IF NOT EXISTS heating_minutes_today INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS energy_saved_pct NUMERIC DEFAULT 0;

-- RLS: service role only for both tables
ALTER TABLE public.system_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_assessment ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role only" ON public.system_state FOR ALL USING (false);
CREATE POLICY "Service role only" ON public.daily_assessment FOR ALL USING (false);

-- Allow anon read for dashboard display
CREATE POLICY "Anon read system_state" ON public.system_state FOR SELECT TO anon USING (true);
CREATE POLICY "Anon read daily_assessment" ON public.daily_assessment FOR SELECT TO anon USING (true);
CREATE POLICY "Anon read control_log" ON public.control_log FOR SELECT TO anon USING (true);
