ALTER TABLE public.control_log ADD COLUMN IF NOT EXISTS dewpoint numeric NULL;
ALTER TABLE public.control_log ADD COLUMN IF NOT EXISTS dp_spread numeric NULL;