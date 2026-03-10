import { useQuery } from "@tanstack/react-query";
import { supabase } from "@/integrations/supabase/client";
import { ClimateHistoryPoint } from "@/types/climate";
import { calcDewpoint } from "@/lib/dewpoint";

export interface ControlLogEntry {
  id: string;
  zone_name: string;
  action: string;
  humidity_airzone: number | null;
  humidity_netatmo: number | null;
  temperature: number | null;
  dewpoint: number | null;
  dp_spread: number | null;
  outdoor_humidity: number | null;
  outdoor_temp: number | null;
  forecast_temp_max: number | null;
  forecast_best_hour: string | null;
  occupancy_detected: boolean | null;
  energy_saved_pct: number | null;
  reason: string | null;
  success: boolean | null;
  created_at: string;
}

export interface DailyAssessment {
  id: string;
  date: string;
  avg_humidity_before: number | null;
  avg_humidity_after: number | null;
  humidity_improved: boolean | null;
  total_heating_kwh: number | null;
  total_cost_eur: number | null;
  heating_minutes: number | null;
  ventilation_suggestions: number | null;
  occupancy_detected: boolean | null;
  zones_above_65: number | null;
  zones_total: number | null;
  notes: string | null;
  actual_kwh: number | null;
  estimation_accuracy_pct: number | null;
  correction_factor: number | null;
  created_at: string;
}

export interface SystemState {
  key: string;
  value: any;
  updated_at: string;
}

export function useRecentControlLogs(limit = 50) {
  return useQuery({
    queryKey: ["control-logs", limit],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("control_log")
        .select("*")
        .order("created_at", { ascending: false })
        .limit(limit);
      if (error) throw error;
      return data as ControlLogEntry[];
    },
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
}

export function useControlLogsByDay() {
  return useQuery({
    queryKey: ["control-logs-today"],
    queryFn: async () => {
      const today = new Date().toISOString().split("T")[0];
      const { data, error } = await supabase
        .from("control_log")
        .select("*")
        .gte("created_at", `${today}T00:00:00Z`)
        .order("created_at", { ascending: true });
      if (error) throw error;
      return data as ControlLogEntry[];
    },
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
}

export function useDailyAssessments() {
  return useQuery({
    queryKey: ["daily-assessments"],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("daily_assessment")
        .select("*")
        .order("date", { ascending: false })
        .limit(30);
      if (error) throw error;
      return data as DailyAssessment[];
    },
    refetchInterval: 5 * 60_000,
    staleTime: 60_000,
  });
}

export function useSystemState() {
  return useQuery({
    queryKey: ["system-state"],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("system_state")
        .select("*");
      if (error) throw error;
      const map: Record<string, any> = {};
      for (const row of (data || [])) map[row.key] = row.value;
      return map;
    },
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
}

// Build climate history from control_log for a specific zone and time range
export type TimeRange = "1h" | "24h" | "3d" | "7d" | "30d";

const RANGE_MS: Record<TimeRange, number> = {
  "1h": 60 * 60 * 1000,
  "24h": 24 * 60 * 60 * 1000,
  "3d": 3 * 24 * 60 * 60 * 1000,
  "7d": 7 * 24 * 60 * 60 * 1000,
  "30d": 30 * 24 * 60 * 60 * 1000,
};

export function useZoneClimateHistory(zoneName: string, range: TimeRange = "24h") {
  return useQuery({
    queryKey: ["zone-climate-history", zoneName, range],
    queryFn: async () => {
      const since = new Date(Date.now() - RANGE_MS[range]).toISOString();
      const { data, error } = await supabase
        .from("control_log")
        .select("*")
        .eq("zone_name", zoneName)
        .gte("created_at", since)
        .order("created_at", { ascending: true });
      if (error) throw error;
      
      const history: ClimateHistoryPoint[] = (data || []).map((log) => {
        const humidity = log.humidity_netatmo ?? log.humidity_airzone ?? 0;
        const temp = log.temperature ?? 0;
        // Use stored dewpoint if available, otherwise calculate
        const dewpoint = log.dewpoint ?? calcDewpoint(temp, humidity);
        return {
          timestamp: log.created_at,
          temperature: temp,
          humidity,
          dewpoint,
          isHeating: log.action === "heating_on" || 
            (log.action === "no_change" && !!log.reason && /heating\s*\(band/i.test(log.reason)),
          outdoorTemp: log.outdoor_temp ?? undefined,
          outdoorHumidity: log.outdoor_humidity ?? undefined,
        };
      });
      
      return history;
    },
    refetchInterval: 5 * 60_000,
    staleTime: 2 * 60_000,
  });
}

// Predictions data for analytics
export interface PredictionEntry {
  id: string;
  zone_name: string;
  created_at: string;
  predicted_for: string;
  predicted_dp_spread: number;
  current_dp_spread: number | null;
  actual_dp_spread: number | null;
  prediction_error: number | null;
  validated: boolean;
  decision_made: string | null;
  decision_correct: boolean | null;
  predicted_outdoor_temp: number | null;
  predicted_outdoor_humidity: number | null;
  current_indoor_temp: number | null;
  actual_indoor_temp: number | null;
}

export function usePredictions(limit = 200) {
  return useQuery({
    queryKey: ["predictions", limit],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("dp_spread_predictions")
        .select("*")
        .order("created_at", { ascending: false })
        .limit(limit);
      if (error) throw error;
      return (data || []) as PredictionEntry[];
    },
    refetchInterval: 5 * 60_000,
    staleTime: 2 * 60_000,
  });
}
