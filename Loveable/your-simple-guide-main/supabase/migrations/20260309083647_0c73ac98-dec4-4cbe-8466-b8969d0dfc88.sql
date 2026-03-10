-- Table to store learned energy baseline per hour-of-day
CREATE TABLE public.energy_baseline (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hour_of_day smallint NOT NULL,
  baseline_wh numeric NOT NULL DEFAULT 110,
  sample_count integer NOT NULL DEFAULT 0,
  last_updated timestamp with time zone DEFAULT now(),
  dhw_active_avg_wh numeric DEFAULT 0,
  notes text,
  UNIQUE(hour_of_day)
);

-- Seed with initial estimates (55Wh per 30min = 110Wh/hour)
INSERT INTO public.energy_baseline (hour_of_day, baseline_wh, sample_count, notes)
SELECT h, 110, 0, 'initial estimate'
FROM generate_series(0, 23) AS h;

-- Disable RLS since this is system-managed data
ALTER TABLE public.energy_baseline ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow anonymous read on energy_baseline"
  ON public.energy_baseline FOR SELECT
  TO anon USING (true);

CREATE POLICY "Allow service role full access on energy_baseline"
  ON public.energy_baseline FOR ALL
  TO service_role USING (true) WITH CHECK (true);