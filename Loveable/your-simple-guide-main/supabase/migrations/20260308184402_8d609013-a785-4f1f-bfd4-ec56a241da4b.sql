ALTER TABLE public.daily_assessment 
  ADD COLUMN actual_kwh numeric,
  ADD COLUMN estimation_accuracy_pct numeric,
  ADD COLUMN correction_factor numeric DEFAULT 1.0;