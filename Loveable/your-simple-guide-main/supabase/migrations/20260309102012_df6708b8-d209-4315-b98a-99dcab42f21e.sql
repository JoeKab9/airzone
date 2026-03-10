
CREATE TABLE public.tariff_rates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  valid_from date NOT NULL,
  variable_rate_kwh numeric NOT NULL,
  fixed_annual_eur numeric NOT NULL,
  notes text,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE public.tariff_rates ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow public read on tariff_rates"
  ON public.tariff_rates FOR SELECT
  TO anon, authenticated
  USING (true);

INSERT INTO public.tariff_rates (valid_from, variable_rate_kwh, fixed_annual_eur, notes) VALUES
  ('2022-02-01', 0.1740, 199, 'Feb 2022 tariff'),
  ('2023-08-01', 0.2245, 222, 'Aug 2023 tariff'),
  ('2024-02-01', 0.2516, 240, 'Feb 2024 tariff'),
  ('2024-08-01', 0.2516, 243, 'Aug 2024 tariff'),
  ('2025-02-01', 0.2016, 168, 'Feb 2025 tariff'),
  ('2026-03-01', 0.1927, 234.72, 'Mar 2026 tariff');
