import { useQuery } from "@tanstack/react-query";
import { fetchWeather, fetchAirzoneZones, fetchLinkyData, fetchLinkyDaily, fetchNetatmoData, fetchEnergyBaselines } from "@/services/api";
import { supabase } from "@/integrations/supabase/client";

export function useWeather() {
  return useQuery({
    queryKey: ["weather"],
    queryFn: () => fetchWeather(),
    refetchInterval: 15 * 60 * 1000,
    staleTime: 10 * 60 * 1000,
    retry: 2,
  });
}

export function useAirzoneZones() {
  return useQuery({
    queryKey: ["airzone-zones"],
    queryFn: () => fetchAirzoneZones(),
    refetchInterval: 5 * 60 * 1000,
    staleTime: 2 * 60 * 1000,
    retry: 2,
  });
}

export function useEnergyBaselines() {
  return useQuery({
    queryKey: ["energy-baselines"],
    queryFn: () => fetchEnergyBaselines(),
    staleTime: 60 * 60 * 1000,
    retry: 1,
  });
}

/**
 * Smart Linky fetch schedule:
 * - Fetch once, then stale for 12h (until next day)
 * - On error (503), retry every hour automatically via react-query retry
 * - refetchInterval disabled (no polling) — relies on staleTime + window refocus
 */
export function useLinkyData() {
  return useQuery({
    queryKey: ["linky"],
    queryFn: async () => {
      const baselines = await fetchEnergyBaselines();
      return fetchLinkyData(7, baselines);
    },
    staleTime: 12 * 60 * 60 * 1000,   // 12h — fetch once per day effectively
    gcTime: 24 * 60 * 60 * 1000,      // keep in cache 24h
    retry: (failureCount, error) => {
      // On 503 (Enedis unavailable), retry up to 12 times (= ~12 hours of hourly retries)
      if (error?.message?.includes("503") || error?.message?.includes("Service")) {
        return failureCount < 12;
      }
      return failureCount < 2;
    },
    retryDelay: (attemptIndex, error) => {
      // On 503, retry every hour; otherwise standard backoff
      if (error?.message?.includes("503") || error?.message?.includes("Service")) {
        return 60 * 60 * 1000; // 1 hour
      }
      return Math.min(1000 * 2 ** attemptIndex, 30000);
    },
  });
}

/** Fetch Linky daily data for long ranges (30d, 1y) */
export function useLinkyDaily(days: number, enabled = true) {
  return useQuery({
    queryKey: ["linky-daily", days],
    queryFn: async () => {
      const baselines = await fetchEnergyBaselines();
      return fetchLinkyDaily(days, baselines);
    },
    staleTime: 12 * 60 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    retry: (failureCount, error) => {
      if (error?.message?.includes("503") || error?.message?.includes("Service")) {
        return failureCount < 12;
      }
      return failureCount < 2;
    },
    retryDelay: (attemptIndex, error) => {
      if (error?.message?.includes("503") || error?.message?.includes("Service")) {
        return 60 * 60 * 1000;
      }
      return Math.min(1000 * 2 ** attemptIndex, 30000);
    },
    enabled,
  });
}

/** Fetch Linky hourly data for medium ranges (3d, 7d) */
export function useLinkyHourly(days: number, enabled = true) {
  return useQuery({
    queryKey: ["linky-hourly", days],
    queryFn: async () => {
      const baselines = await fetchEnergyBaselines();
      return fetchLinkyData(days, baselines);
    },
    staleTime: 12 * 60 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    retry: (failureCount, error) => {
      if (error?.message?.includes("503") || error?.message?.includes("Service")) {
        return failureCount < 12;
      }
      return failureCount < 2;
    },
    retryDelay: (attemptIndex, error) => {
      if (error?.message?.includes("503") || error?.message?.includes("Service")) {
        return 60 * 60 * 1000;
      }
      return Math.min(1000 * 2 ** attemptIndex, 30000);
    },
    enabled,
  });
}

export function useNetatmoData() {
  return useQuery({
    queryKey: ["netatmo"],
    queryFn: () => fetchNetatmoData(),
    refetchInterval: 5 * 60 * 1000,
    staleTime: 3 * 60 * 1000,
    retry: 2,
  });
}

export interface OccupancyDay {
  date: string;
  avgCo2: number;
  maxCo2: number;
  avgNoise: number;
  occupied: boolean;
}

export function useNetatmoOccupancy(days = 365, enabled = true) {
  return useQuery({
    queryKey: ["netatmo-occupancy", days],
    queryFn: async (): Promise<OccupancyDay[]> => {
      const { data, error } = await supabase.functions.invoke("netatmo-history", {
        body: { days },
      });
      if (error) throw new Error(`Netatmo history failed: ${error.message}`);
      if (data.error) throw new Error(data.error);
      return data.occupancy || [];
    },
    staleTime: 6 * 60 * 60 * 1000, // 6h
    gcTime: 24 * 60 * 60 * 1000,
    retry: 1,
    enabled,
  });
}

export interface TariffRate {
  valid_from: string;
  variable_rate_kwh: number;
  fixed_annual_eur: number;
}

export function useTariffRates() {
  return useQuery({
    queryKey: ["tariff-rates"],
    queryFn: async (): Promise<TariffRate[]> => {
      const { data, error } = await supabase
        .from("tariff_rates")
        .select("valid_from, variable_rate_kwh, fixed_annual_eur")
        .order("valid_from", { ascending: true });
      if (error) throw error;
      return (data || []).map(r => ({
        valid_from: r.valid_from,
        variable_rate_kwh: Number(r.variable_rate_kwh),
        fixed_annual_eur: Number(r.fixed_annual_eur),
      }));
    },
    staleTime: 24 * 60 * 60 * 1000,
  });
}

/** Get the applicable tariff for a given date string (YYYY-MM-DD) */
export function getTariffForDate(rates: TariffRate[], dateStr: string): TariffRate {
  const fallback: TariffRate = { valid_from: "2026-03-01", variable_rate_kwh: 0.1927, fixed_annual_eur: 234.72 };
  if (!rates || rates.length === 0) return fallback;
  // Find latest rate where valid_from <= dateStr
  let applicable = rates[0];
  for (const r of rates) {
    if (r.valid_from <= dateStr) applicable = r;
    else break;
  }
  return applicable;
}
