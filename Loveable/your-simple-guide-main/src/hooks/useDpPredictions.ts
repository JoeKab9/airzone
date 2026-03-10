import { useQuery } from "@tanstack/react-query";
import { supabase } from "@/integrations/supabase/client";
import { Zone, WeatherData } from "@/types/climate";
import { calcRoomDewpoint } from "@/lib/dewpoint";
import { ControlLogEntry } from "@/hooks/useControlData";
import { ZoneThermalModel, MIN_SAMPLES_FOR_PREDICTION, CONFIDENCE_HALF_LIFE } from "@/types/thermalModel";

export interface DpPrediction {
  current_dp_spread: number;
  predicted_dp_spread: number;
  trend: number; // -2 to +2
  confidence: number;
  factors: string[];
  isLearning: boolean; // true when model has insufficient data
}

export interface ZoneHeatingHistory {
  lastHeatingOffTime: number | null;
  lastHeatingOnTime: number | null;
  heatingDurationMin: number;
}

/** Build per-zone heating history from recent control logs */
export function buildHeatingHistory(
  recentLogs: ControlLogEntry[] | undefined,
  zoneDisplayNames: Record<string, string>,
): Record<string, ZoneHeatingHistory> {
  const result: Record<string, ZoneHeatingHistory> = {};
  if (!recentLogs) return result;

  for (const log of recentLogs) {
    const name = zoneDisplayNames[log.zone_name] || log.zone_name;
    if (!result[name]) {
      result[name] = { lastHeatingOffTime: null, lastHeatingOnTime: null, heatingDurationMin: 0 };
    }
    const entry = result[name];
    const t = new Date(log.created_at).getTime();

    if (log.action === "heating_off" && !entry.lastHeatingOffTime) {
      entry.lastHeatingOffTime = t;
    }
    if (log.action === "heating_on" && !entry.lastHeatingOnTime) {
      entry.lastHeatingOnTime = t;
    }
    if (entry.lastHeatingOffTime && entry.lastHeatingOnTime && entry.heatingDurationMin === 0) {
      entry.heatingDurationMin = (entry.lastHeatingOffTime - entry.lastHeatingOnTime) / 60_000;
    }
  }
  return result;
}

/** Fetch learned thermal models from system_state */
export function useThermalModels() {
  return useQuery({
    queryKey: ["thermal-models"],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("system_state")
        .select("value")
        .eq("key", "thermal_models_by_zone")
        .single();
      if (error || !data) return {} as Record<string, ZoneThermalModel>;
      return data.value as unknown as Record<string, ZoneThermalModel>;
    },
    refetchInterval: 30 * 60_000, // models update slowly
    staleTime: 15 * 60_000,
    retry: 1,
  });
}

/** DP spread prediction using ONLY learned models — no hardcoded assumptions */
export function useDpPredictions(
  zones: Zone[],
  weather: WeatherData | null,
  netatmoByZone: Record<string, { temperature?: number; humidity?: number }>,
  heatingHistory?: Record<string, ZoneHeatingHistory>,
  thermalModels?: Record<string, ZoneThermalModel> | null,
) {
  const zoneKey = zones.length > 0 ? zones.map(z => z.id).join(",") : "none";
  const hasModels = thermalModels && Object.keys(thermalModels).length > 0;

  return useQuery({
    queryKey: ["dp-predictions", zoneKey, weather?.temperature ?? 0, hasModels ? "m" : "n"],
    queryFn: async (): Promise<{ predictions: Record<string, DpPrediction>; predictions_24h: Record<string, DpPrediction> }> => {
      // Try edge function (returns both predictions + updated models)
      try {
        const { data, error } = await supabase.functions.invoke("dp-predict");
        if (!error && data?.predictions && Object.keys(data.predictions).length > 0) {
          return {
            predictions: data.predictions as Record<string, DpPrediction>,
            predictions_24h: (data.predictions_24h || {}) as Record<string, DpPrediction>,
          };
        }
      } catch (_e) {
        // Fall through to client-side
      }

      const preds = computePredictions(zones, weather, netatmoByZone, heatingHistory, thermalModels, 3);
      const preds24 = computePredictions(zones, weather, netatmoByZone, heatingHistory, thermalModels, 24);
      return { predictions: preds, predictions_24h: preds24 };
    },
    refetchInterval: 10 * 60_000,
    staleTime: 5 * 60_000,
    retry: 0,
    enabled: zones.length > 0 && weather != null,
  });
}

/**
 * Client-side prediction using ONLY learned thermal models.
 * When a zone has no learned model yet → isLearning=true, trend=0.
 * No hardcoded thermal constants anywhere.
 */
function computePredictions(
  zones: Zone[],
  weather: WeatherData | null,
  netatmoByZone: Record<string, { temperature?: number; humidity?: number }>,
  heatingHistory?: Record<string, ZoneHeatingHistory>,
  thermalModels?: Record<string, ZoneThermalModel> | null,
  hoursAhead: number = 3,
): Record<string, DpPrediction> {
  const predictions: Record<string, DpPrediction> = {};
  if (!weather) return predictions;

  const now = Date.now();
  const HOURS_AHEAD = hoursAhead;

  // Weather forecast 3h out
  const forecast3h = weather.forecast?.find((f) => {
    const ft = new Date(f.time).getTime();
    return ft >= now + 2 * 3600_000 && ft <= now + 4 * 3600_000;
  });

  const outdoorTempNow = weather.temperature;
  const outdoorTemp3h = forecast3h?.temperature ?? outdoorTempNow;
  const outdoorHum3h = forecast3h?.humidity ?? weather.humidity;
  const tempChange = outdoorTemp3h - outdoorTempNow;
  const humChange = outdoorHum3h - weather.humidity;

  for (const zone of zones) {
    const netatmo = netatmoByZone[zone.name];
    const bestTemp = netatmo?.temperature ?? zone.temperature;
    const dp = calcRoomDewpoint(zone.temperature, zone.humidity, netatmo?.temperature, netatmo?.humidity);
    const currentDpSpread = zone.temperature - dp;

    // Find model — try display name, then original zone name
    const model = thermalModels?.[zone.name] ?? null;
    const hasModel = model != null && model.samples >= MIN_SAMPLES_FOR_PREDICTION;
    const history = heatingHistory?.[zone.name];

    const factors: string[] = [];
    let change = 0;
    let isLearning = !hasModel;

    if (!hasModel) {
      factors.push(`Learning thermal behavior (${model?.samples ?? 0}/${MIN_SAMPLES_FOR_PREDICTION} heating cycles observed)`);
      
      // With no model, we can only report what weather forecast says
      // but make NO assumptions about how the building responds
      if (Math.abs(tempChange) > 2) {
        factors.push(tempChange > 0 ? "Outdoor warming expected" : "Outdoor cooling expected");
      }
      if (Math.abs(humChange) > 10) {
        factors.push(humChange > 0 ? "Outdoor RH rising" : "Outdoor RH falling");
      }
    } else {
      // === USE LEARNED MODEL ONLY ===

      // A) Thermal runoff / decay (zone-specific learned behavior)
      if (!zone.isHeating && history?.lastHeatingOffTime) {
        const hoursSinceOff = (now - history.lastHeatingOffTime) / 3600_000;
        const heatMin = Math.max(history.heatingDurationMin, 0);

        // Learned runoff duration for this heating cycle
        const expectedRunoff = model.runoff_base + model.runoff_per_heat_min * heatMin;

        if (hoursSinceOff < expectedRunoff && expectedRunoff > 0) {
          // In runoff: concrete still radiating stored heat
          const remainH = expectedRunoff - hoursSinceOff;
          const peakRise = Math.min(
            model.peak_per_heat_min * heatMin,
            model.peak_max_observed
          );
          // Proportional to remaining runoff time
          const runoffEffect = peakRise * (remainH / expectedRunoff);
          change += runoffEffect;
          factors.push(`Thermal runoff (${remainH.toFixed(1)}h, learned from ${model.samples} cycles)`);
        } else if (model.decay_coeff > 0) {
          // Post-runoff: learned decay rate
          const delta = bestTemp - outdoorTempNow;
          const decayPerH = model.decay_coeff * delta;
          const effectiveWindow = Math.min(HOURS_AHEAD, Math.max(0, HOURS_AHEAD - (hoursSinceOff - expectedRunoff)));
          change -= decayPerH * effectiveWindow;
          if (decayPerH * effectiveWindow > 0.1) {
            factors.push(`Cooling (learned rate, ${model.samples} cycles, ${hoursSinceOff.toFixed(1)}h since heat)`);
          }
        }

        // Learned humidity drift effect on dewpoint — using learned coefficient
        if (model.rh_drift_coeff !== 0 && model.rh_to_dp_coeff !== 0) {
          const rhEffect = model.rh_drift_coeff * HOURS_AHEAD * model.rh_to_dp_coeff;
          change += rhEffect;
          if (Math.abs(rhEffect) > 0.05) {
            factors.push("Learned RH drift");
          }
        }

      } else if (!zone.isHeating && model.decay_coeff > 0) {
        // No recent heating data but have learned decay
        const delta = bestTemp - outdoorTempNow;
        const decayPerH = model.decay_coeff * delta;
        change -= decayPerH * HOURS_AHEAD;
        if (decayPerH * HOURS_AHEAD > 0.1) {
          factors.push(`Learned cooling (${model.samples} cycles)`);
        }
      }

      // B) Outdoor weather trend effect (only use if there are learned observations)
      // Even this is modulated by the building's response, but weather direction is physics
      if (Math.abs(tempChange) > 0.5) {
        // Effect of outdoor temp change on indoor: scaled by decay_coeff
        // Warmer outside → less heat loss → DP spread improves (and vice versa)
        const outdoorEffect = tempChange * model.decay_coeff * HOURS_AHEAD;
        change += outdoorEffect;
        if (Math.abs(tempChange) > 1) {
          factors.push(tempChange > 0 ? "Outdoor warming" : "Outdoor cooling");
        }
      }
    }

    if (factors.length === 0) factors.push("Insufficient data");

    const predictedDpSpread = Math.round((currentDpSpread + change) * 10) / 10;

    let trend = 0;
    if (!isLearning) {
      if (change > 1.5) trend = 2;
      else if (change > 0.4) trend = 1;
      else if (change > -0.4) trend = 0;
      else if (change > -1.5) trend = -1;
      else trend = -2;
    }
    // When learning: trend stays 0 (horizontal = "I don't know yet")

    predictions[zone.name] = {
      current_dp_spread: Math.round(currentDpSpread * 10) / 10,
      predicted_dp_spread: predictedDpSpread,
      trend,
      confidence: hasModel ? (model.confidence * (HOURS_AHEAD > 12 ? 0.7 : 1)) : 0,
      factors,
      isLearning,
    };
  }

  return predictions;
}
