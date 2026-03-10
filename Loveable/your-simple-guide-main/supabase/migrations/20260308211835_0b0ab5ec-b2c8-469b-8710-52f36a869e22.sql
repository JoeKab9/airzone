
CREATE TABLE public.heating_experiments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  type text NOT NULL DEFAULT 'no_heating',
  status text NOT NULL DEFAULT 'scheduled',
  start_date date NOT NULL,
  end_date date NOT NULL,
  reason text,
  avg_humidity_during numeric,
  avg_humidity_before numeric,
  avg_humidity_after numeric,
  avg_outdoor_humidity numeric,
  avg_outdoor_temp numeric,
  avg_indoor_temp numeric,
  thermal_runoff_hours numeric,
  conclusion text,
  recommendation text,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);

ALTER TABLE public.heating_experiments ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anon read heating_experiments" ON public.heating_experiments FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY "Service role only experiments" ON public.heating_experiments FOR ALL TO service_role USING (true);
