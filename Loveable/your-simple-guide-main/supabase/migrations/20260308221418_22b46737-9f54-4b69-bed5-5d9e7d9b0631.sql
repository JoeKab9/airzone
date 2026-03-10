
-- Predictions table: stores forecast-based DP spread predictions + actual outcomes
CREATE TABLE public.dp_spread_predictions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  zone_name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  predicted_for timestamptz NOT NULL,
  hours_ahead numeric NOT NULL DEFAULT 3,
  predicted_dp_spread numeric NOT NULL,
  predicted_indoor_temp numeric,
  predicted_outdoor_temp numeric,
  predicted_outdoor_humidity numeric,
  current_dp_spread numeric,
  current_indoor_temp numeric,
  current_outdoor_temp numeric,
  actual_dp_spread numeric,
  actual_indoor_temp numeric,
  prediction_error numeric,
  validated boolean NOT NULL DEFAULT false,
  validated_at timestamptz,
  decision_made text,
  decision_correct boolean
);

ALTER TABLE public.dp_spread_predictions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anon read dp_spread_predictions" ON public.dp_spread_predictions
  FOR SELECT TO anon, authenticated USING (true);

CREATE POLICY "Service role only predictions" ON public.dp_spread_predictions
  FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Index for efficient lookups during validation
CREATE INDEX idx_predictions_validation ON public.dp_spread_predictions (zone_name, predicted_for, validated);
CREATE INDEX idx_predictions_created ON public.dp_spread_predictions (created_at DESC);
