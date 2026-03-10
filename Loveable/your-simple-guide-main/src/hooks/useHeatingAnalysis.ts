import { useQuery } from "@tanstack/react-query";
import { supabase } from "@/integrations/supabase/client";

export interface HeatingAnalysis {
  status: string;
  daysOfData: number;
  totalDataPoints: number;
  heatingEffectiveness: {
    heatingDaysCount: number;
    noHeatingDaysCount: number;
    avgHumidityWithHeating: number | null;
    avgHumidityWithoutHeating: number | null;
    avgHumidityDuringExperiments: number | null;
    avgDpSpreadWithHeating: number | null;
    avgDpSpreadWithoutHeating: number | null;
    weatherNormalized: {
      paired: number;
      avgRhDiff: number | null;
      avgDpSpreadDiff: number | null;
      significant: boolean;
      factors: any;
      description: string;
    };
  };
  runoffAnalysis: {
    estimatedRunoffHours: number;
    tempDecayRate: number;
    samples: number;
    factors: any;
    description: string;
  };
  passiveReactivity?: {
    description: string;
    overall: { avgCouplingFactor: number; avgLagHours: number; samples: number } | null;
    byZone: Record<string, { avgCouplingFactor: number; avgLagHours: number; avgDecayRate: number; samples: number }>;
  };
  experiments: Array<{
    id: string;
    type: string;
    status: string;
    start_date: string;
    end_date: string;
    reason: string | null;
    avg_humidity_during: number | null;
    avg_humidity_before: number | null;
    avg_humidity_after: number | null;
    avg_outdoor_temp: number | null;
    thermal_runoff_hours: number | null;
    conclusion: string | null;
    recommendation: string | null;
  }>;
  scheduledExperiment: any | null;
  conclusions: {
    summary: string;
    confidence: string;
    recommendations: string[];
  };
}

export function useHeatingAnalysis() {
  return useQuery({
    queryKey: ["heating-analysis"],
    queryFn: async () => {
      const { data, error } = await supabase.functions.invoke("heating-analysis");
      if (error) throw error;
      return data as HeatingAnalysis;
    },
    refetchInterval: 30 * 60_000, // Every 30 min
    staleTime: 15 * 60_000,
  });
}
