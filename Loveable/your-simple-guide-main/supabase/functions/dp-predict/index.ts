import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.49.1";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-supabase-client-platform, x-supabase-client-platform-version, x-supabase-client-runtime, x-supabase-client-runtime-version",
};

const PREDICTION_HORIZONS = [3, 24]; // hours
const MIN_SAMPLES = 3;
const CONFIDENCE_HALF_LIFE = 20; // asymptotic: confidence = 1 - e^(-samples/half_life)

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
  );

  try {
    // 1. Validate past predictions (learning feedback loop)
    await validatePastPredictions(supabase);

    // 2. Get all control_log data for learning (up to 90 days)
    const since90d = new Date(Date.now() - 90 * 86400_000).toISOString();
    const { data: allLogs } = await supabase
      .from("control_log")
      .select("*")
      .gte("created_at", since90d)
      .order("created_at", { ascending: true });

    if (!allLogs || allLogs.length < 10) {
      return respond({ predictions: {}, thermalModels: {}, message: "Insufficient data for learning" });
    }

    // 3. Learn thermal model per zone from ALL historical data
    const thermalModels = learnThermalModels(allLogs);

    // Persist learned models
    await supabase.from("system_state").upsert({
      key: "thermal_models_by_zone",
      value: thermalModels,
      updated_at: new Date().toISOString(),
    });

    // 4. Get weather forecast (extended to 48h for 24h predictions)
    const forecast = await fetchWeatherForecast();

    // 5. Get latest readings per zone
    const since3h = new Date(Date.now() - 3 * 3600_000).toISOString();
    const recentLogs = allLogs.filter(l => l.created_at >= since3h);
    const latestByZone: Record<string, any> = {};
    for (let i = recentLogs.length - 1; i >= 0; i--) {
      const log = recentLogs[i];
      if (!latestByZone[log.zone_name]) latestByZone[log.zone_name] = log;
    }

    // 6. Find last heating-off per zone
    const lastHeatingOff: Record<string, { time: number; durationMin: number }> = {};
    const logsByZone: Record<string, any[]> = {};
    for (const log of allLogs) {
      if (!logsByZone[log.zone_name]) logsByZone[log.zone_name] = [];
      logsByZone[log.zone_name].push(log);
    }
    for (const [zone, zoneLogs] of Object.entries(logsByZone)) {
      for (let i = zoneLogs.length - 1; i >= 0; i--) {
        if (zoneLogs[i].action === "heating_off") {
          const offTime = new Date(zoneLogs[i].created_at).getTime();
          let durMin = 0;
          for (let j = i - 1; j >= Math.max(0, i - 200); j--) {
            if (zoneLogs[j].action === "heating_on") {
              durMin = (offTime - new Date(zoneLogs[j].created_at).getTime()) / 60000;
              break;
            }
            if (zoneLogs[j].action === "heating_off") break;
          }
          // Only use if we found actual heating duration
          if (durMin > 0) {
            lastHeatingOff[zone] = { time: offTime, durationMin: durMin };
          }
          break;
        }
      }
    }

    // 7. Get outdoor trend coefficients
    const { data: coeffRow } = await supabase
      .from("system_state")
      .select("value")
      .eq("key", "dp_predict_coefficients")
      .single();
    const coefficients = coeffRow?.value || {
      outdoor_temp_weight: 0, outdoor_hum_weight: 0,
      time_decay_weight: 0, base_drift: 0, learning_count: 0
    };

    // 8. Compute predictions per zone for each horizon
    const now = Date.now();
    const predictions: Record<string, any> = {};
    const predictions_24h: Record<string, any> = {};

    for (const [zone, log] of Object.entries(latestByZone)) {
      const azHum = log.humidity_airzone || 0;
      const ntHum = log.humidity_netatmo || 0;
      const azTemp = log.temperature;
      if (!azTemp) continue;

      // Use same logic as dashboard: average temps, prefer Netatmo humidity
      const bestHum = ntHum > 0 ? ntHum : azHum;
      if (bestHum <= 0) continue;
      const dp = calcDewpoint(azTemp, bestHum);
      const currentDpSpread = log.dp_spread ?? (azTemp - dp);
      if (currentDpSpread == null) continue;

      const isHeating = log.action === "heating_on";
      const model = thermalModels[zone];
      const hasModel = model && model.samples >= MIN_SAMPLES;

      const outdoorTempNow = log.outdoor_temp ?? forecast?.current_temp ?? null;
      const outdoorHumNow = log.outdoor_humidity ?? forecast?.current_hum ?? null;

      for (const HOURS_AHEAD of PREDICTION_HORIZONS) {
        const outdoorTempFut = HOURS_AHEAD <= 3
          ? (forecast?.temp_3h ?? outdoorTempNow)
          : (forecast?.temp_24h ?? outdoorTempNow);
        const outdoorHumFut = HOURS_AHEAD <= 3
          ? (forecast?.hum_3h ?? outdoorHumNow)
          : (forecast?.hum_24h ?? outdoorHumNow);

        const factors: string[] = [];
        let predictedChange = 0;

        // A) Outdoor forecast trend (learned weights)
        if (outdoorTempNow != null && outdoorTempFut != null && coefficients.learning_count > 0) {
          const tempChange = outdoorTempFut - outdoorTempNow;
          predictedChange += tempChange * coefficients.outdoor_temp_weight;
          if (Math.abs(tempChange) > 1) {
            factors.push(tempChange > 0 ? "Outdoor warming" : "Outdoor cooling");
          }
        }
        if (outdoorHumNow != null && outdoorHumFut != null && coefficients.learning_count > 0) {
          const humChange = outdoorHumFut - outdoorHumNow;
          predictedChange += humChange * coefficients.outdoor_hum_weight;
          if (Math.abs(humChange) > 5) {
            factors.push(humChange > 0 ? "Rising outdoor RH" : "Falling outdoor RH");
          }
        }

        // B) Thermal model (only if learned)
        if (hasModel) {
          const heatingOff = lastHeatingOff[zone];

          if (!isHeating && heatingOff) {
            const hoursSinceOff = (now - heatingOff.time) / 3600_000;
            const expectedRunoff = model.runoff_base + model.runoff_per_heat_min * heatingOff.durationMin;

            if (hoursSinceOff < expectedRunoff) {
              const remainH = expectedRunoff - hoursSinceOff;
              const peakRise = Math.min(
                model.peak_per_heat_min * heatingOff.durationMin,
                model.peak_max_observed
              );
              const runoffInWindow = Math.min(remainH, HOURS_AHEAD);
              const runoffEffect = peakRise * (runoffInWindow / expectedRunoff);
              predictedChange += runoffEffect;
              factors.push(`Learned runoff (${remainH.toFixed(1)}h left, ${model.samples} obs.)`);

              // After runoff ends within the prediction window, apply decay
              if (HOURS_AHEAD > remainH && outdoorTempNow != null && model.decay_coeff > 0) {
                const decayWindow = HOURS_AHEAD - remainH;
                const delta = (azTemp + runoffEffect) - outdoorTempNow;
                const decayPerH = model.decay_coeff * delta;
                predictedChange -= decayPerH * decayWindow;
                if (decayPerH * decayWindow > 0.2) {
                  factors.push(`Post-runoff decay (${decayWindow.toFixed(1)}h)`);
                }
              }
            } else if (outdoorTempNow != null) {
              const delta = azTemp - outdoorTempNow;
              const decayPerH = model.decay_coeff * delta;
              const postRunoffH = hoursSinceOff - expectedRunoff;
              const windowRemaining = Math.max(0, HOURS_AHEAD - postRunoffH);
              predictedChange -= decayPerH * windowRemaining;
              if (decayPerH * windowRemaining > 0.2) {
                factors.push(`Learned decay (${model.samples} obs.)`);
              }
            }

            if (model.rh_drift_coeff !== 0 && model.rh_to_dp_coeff !== 0) {
              const rhEffect = model.rh_drift_coeff * HOURS_AHEAD * model.rh_to_dp_coeff;
              predictedChange += rhEffect;
            }
          } else if (!isHeating && outdoorTempNow != null) {
            const delta = azTemp - outdoorTempNow;
            const decayPerH = model.decay_coeff * delta;
            predictedChange -= decayPerH * HOURS_AHEAD;
            if (decayPerH * HOURS_AHEAD > 0.2) {
              factors.push(`Learned cooling (${model.samples} obs.)`);
            }
          }
        } else {
          factors.push(`Learning (${model?.samples ?? 0}/${MIN_SAMPLES} cycles observed)`);
        }

        // C) Time-of-day (learned) — scale by horizon
        const hour = new Date().getHours();
        if ((hour >= 18 || hour < 6) && coefficients.learning_count > 0) {
          const nightEffect = coefficients.time_decay_weight * (HOURS_AHEAD / 3);
          predictedChange += nightEffect;
          if (Math.abs(nightEffect) > 0.05) {
            factors.push("Learned night effect");
          }
        }

        if (factors.length === 0) factors.push("Insufficient data");

        const predictedDpSpread = Math.round((currentDpSpread + predictedChange) * 10) / 10;

        let confidence = 0;
        if (hasModel) {
          confidence += (1 - Math.exp(-model.samples / CONFIDENCE_HALF_LIFE)) * 0.5;
        }
        if (coefficients.learning_count > 10) confidence += 0.2;
        if (forecast) confidence += 0.15;
        if (historicalTrendAvailable(allLogs, zone, outdoorTempNow, outdoorHumNow)) confidence += 0.15;
        if (HOURS_AHEAD > 12) confidence *= 0.7;
        confidence = Math.min(confidence, 0.99);

        let trend = 0;
        if (predictedChange > 1.5) trend = 2;
        else if (predictedChange > 0.4) trend = 1;
        else if (predictedChange > -0.4) trend = 0;
        else if (predictedChange > -1.5) trend = -1;
        else trend = -2;

        const isLearning = !hasModel;
        const predObj = {
          current_dp_spread: Math.round(currentDpSpread * 10) / 10,
          predicted_dp_spread: predictedDpSpread,
          trend,
          confidence,
          factors,
          isLearning,
        };

        if (HOURS_AHEAD === 3) {
          predictions[zone] = predObj;
        } else {
          predictions_24h[zone] = predObj;
        }
      }

      // Store 3h prediction for validation
      if (!isHeating) {
        const pred3h = predictions[zone];
        if (pred3h) {
          await supabase.from("dp_spread_predictions").insert({
            zone_name: zone,
            predicted_for: new Date(now + 3 * 3600_000).toISOString(),
            hours_ahead: 3,
            predicted_dp_spread: pred3h.predicted_dp_spread,
            current_dp_spread: pred3h.current_dp_spread,
            current_indoor_temp: azTemp,
            current_outdoor_temp: outdoorTempNow,
            predicted_outdoor_temp: forecast?.temp_3h,
            predicted_outdoor_humidity: forecast?.hum_3h,
            predicted_indoor_temp: hasModel && model.decay_coeff > 0
              ? azTemp - (model.decay_coeff * (azTemp - (outdoorTempNow ?? azTemp)) * 3)
              : null,
            decision_made: hasModel ? "model_prediction" : "learning",
          });
        }
      }
    }

    // 9. Update outdoor coefficients from validated predictions
    await updateCoefficients(supabase, coefficients);

    return respond({ predictions, predictions_24h, thermalModels });
  } catch (e) {
    console.error("dp-predict error:", e);
    return new Response(JSON.stringify({ error: e instanceof Error ? e.message : "Unknown" }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});

function respond(data: any) {
  return new Response(JSON.stringify(data), {
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

function calcDewpoint(tempC: number, rh: number): number {
  if (rh <= 0) return 0;
  const a = 17.625, b = 243.04;
  const gamma = (a * tempC) / (b + tempC) + Math.log(Math.max(rh, 1) / 100);
  return (b * gamma) / (a - gamma);
}

async function fetchWeatherForecast(): Promise<{
  current_temp: number; current_hum: number;
  temp_3h: number; hum_3h: number;
  temp_24h: number; hum_24h: number;
} | null> {
  try {
    const lat = 44.26, lon = -1.32;
    const res = await fetch(
      `https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&hourly=temperature_2m,relative_humidity_2m&forecast_hours=30&current=temperature_2m,relative_humidity_2m`
    );
    if (!res.ok) return null;
    const data = await res.json();
    const hourly = data.hourly;
    if (!hourly?.temperature_2m || hourly.temperature_2m.length < 4) return null;
    const idx24 = Math.min(24, hourly.temperature_2m.length - 1);
    return {
      current_temp: data.current?.temperature_2m ?? hourly.temperature_2m[0],
      current_hum: data.current?.relative_humidity_2m ?? hourly.relative_humidity_2m[0],
      temp_3h: hourly.temperature_2m[3],
      hum_3h: hourly.relative_humidity_2m[3],
      temp_24h: hourly.temperature_2m[idx24],
      hum_24h: hourly.relative_humidity_2m[idx24],
    };
  } catch {
    return null;
  }
}

/**
 * Core learning function: analyzes ALL heating_off events per zone to build
 * a regression model for runoff and decay behavior.
 *
 * For each heating_off event, we measure:
 * - How long heating was on before (heating_minutes)
 * - Indoor temp at shutoff
 * - Outdoor temp at shutoff
 * - Post-shutoff: peak temp, time to peak (runoff), then decay rate
 *
 * We then fit simple linear regressions:
 * - runoff_hours = f(heating_minutes)
 * - peak_rise = f(heating_minutes)
 * - decay_rate = f(indoor_outdoor_delta)
 * - rh_drift = observed average
 */
function learnThermalModels(allLogs: any[]): Record<string, any> {
  const byZone: Record<string, any[]> = {};
  for (const log of allLogs) {
    if (!byZone[log.zone_name]) byZone[log.zone_name] = [];
    byZone[log.zone_name].push(log);
  }

  const firstTime = new Date(allLogs[0].created_at).getTime();
  const lastTime = new Date(allLogs[allLogs.length - 1].created_at).getTime();
  const dataDays = (lastTime - firstTime) / 86400_000;

  const models: Record<string, any> = {};

  for (const [zone, zoneLogs] of Object.entries(byZone)) {
    const observations: {
      heatingMin: number;
      runoffH: number;
      peakRise: number;
      indoorOutdoorDelta: number;
      decayRatePerH: number;
      rhDriftPerH: number;
      rhToDpCoeff: number;
    }[] = [];

    for (let i = 0; i < zoneLogs.length; i++) {
      if (zoneLogs[i].action !== "heating_off") continue;
      const offTemp = zoneLogs[i].temperature;
      const offTime = new Date(zoneLogs[i].created_at).getTime();
      const outdoorTemp = zoneLogs[i].outdoor_temp;
      const offHum = Math.max(zoneLogs[i].humidity_airzone || 0, zoneLogs[i].humidity_netatmo || 0);
      if (offTemp == null || outdoorTemp == null) continue;

      // Find heating duration
      let heatingDurMin = 0;
      for (let k = i - 1; k >= Math.max(0, i - 200); k--) {
        if (zoneLogs[k].action === "heating_on") {
          heatingDurMin = (offTime - new Date(zoneLogs[k].created_at).getTime()) / 60000;
          break;
        }
        if (zoneLogs[k].action === "heating_off") break;
      }
      if (heatingDurMin < 3) continue; // too short to learn from

      // Collect post-heating readings (stop at next heating_on)
      const postReadings: { t: number; temp: number; hum: number }[] = [];
      for (let j = i + 1; j < zoneLogs.length; j++) {
        const t = new Date(zoneLogs[j].created_at).getTime();
        if (t - offTime > 8 * 3600_000) break;
        if (zoneLogs[j].action === "heating_on") break; // interrupted by heating
        if (zoneLogs[j].temperature == null) continue;
        const h = Math.max(zoneLogs[j].humidity_airzone || 0, zoneLogs[j].humidity_netatmo || 0);
        postReadings.push({ t, temp: zoneLogs[j].temperature, hum: h });
      }

      if (postReadings.length < 4) continue;

      // Smooth temps (3-point moving average) to reduce sensor noise
      const smoothed: { t: number; temp: number; hum: number }[] = [];
      for (let s = 1; s < postReadings.length - 1; s++) {
        smoothed.push({
          t: postReadings[s].t,
          temp: (postReadings[s - 1].temp + postReadings[s].temp + postReadings[s + 1].temp) / 3,
          hum: (postReadings[s - 1].hum + postReadings[s].hum + postReadings[s + 1].hum) / 3,
        });
      }
      if (smoothed.length < 3) continue;

      // Find peak temperature (end of runoff)
      let peakTemp = smoothed[0].temp;
      let peakIdx = 0;
      for (let s = 1; s < smoothed.length; s++) {
        if (smoothed[s].temp >= peakTemp - 0.05) {
          if (smoothed[s].temp > peakTemp) {
            peakTemp = smoothed[s].temp;
            peakIdx = s;
          }
        } else {
          // Check sustained decline (next 2 readings)
          let sustained = true;
          for (let look = 1; look <= Math.min(2, smoothed.length - s - 1); look++) {
            if (smoothed[s + look].temp >= smoothed[s].temp) { sustained = false; break; }
          }
          if (sustained) break;
          if (smoothed[s].temp > peakTemp) { peakTemp = smoothed[s].temp; peakIdx = s; }
        }
      }

      const runoffH = (smoothed[peakIdx].t - offTime) / 3600_000;
      const peakRise = Math.max(0, peakTemp - offTemp);

      // Decay rate after peak
      const afterPeak = smoothed.slice(peakIdx + 1);
      let decayRatePerH = 0;
      if (afterPeak.length >= 2) {
        const lastReading = afterPeak[afterPeak.length - 1];
        const decayH = (lastReading.t - smoothed[peakIdx].t) / 3600_000;
        const decayC = peakTemp - lastReading.temp;
        const delta = peakTemp - outdoorTemp;
        if (decayH > 0.3 && delta > 0.5) {
          decayRatePerH = decayC / decayH / delta; // normalized by delta
        }
      }

      // Humidity drift after heating stops + DP spread response
      let rhDriftPerH = 0;
      let rhToDpCoeff = 0;
      if (postReadings.length >= 3 && offHum > 0) {
        const lastHum = postReadings[postReadings.length - 1].hum;
        const timeSpanH = (postReadings[postReadings.length - 1].t - offTime) / 3600_000;
        if (timeSpanH > 0.5 && lastHum > 0) {
          rhDriftPerH = (lastHum - offHum) / timeSpanH;
          // Learn how DP spread changes per unit RH change
          const offDp = calcDewpoint(offTemp, offHum);
          const lastTemp = postReadings[postReadings.length - 1].temp;
          const lastDp = calcDewpoint(lastTemp, lastHum);
          const dpSpreadChange = (lastTemp - lastDp) - (offTemp - offDp);
          const rhChange = lastHum - offHum;
          if (Math.abs(rhChange) > 1) {
            rhToDpCoeff = dpSpreadChange / rhChange;
          }
        }
      }

      if (runoffH >= 0.05) {
        observations.push({
          heatingMin: heatingDurMin,
          runoffH,
          peakRise,
          indoorOutdoorDelta: offTemp - outdoorTemp,
          decayRatePerH,
          rhDriftPerH,
          rhToDpCoeff,
        });
      }
    }

    if (observations.length < 1) continue;

    // Linear regression: runoff_hours = a + b * heating_minutes
    const { a: runoffBase, b: runoffPerMin } = linearRegression(
      observations.map(o => o.heatingMin),
      observations.map(o => o.runoffH)
    );

    // Linear regression: peak_rise = b * heating_minutes (forced through ~0)
    const avgPeakPerMin = observations.reduce((s, o) => s + (o.heatingMin > 0 ? o.peakRise / o.heatingMin : 0), 0) / observations.length;

    // Average decay coefficient (already normalized by delta)
    const decayObs = observations.filter(o => o.decayRatePerH > 0);
    const avgDecayCoeff = decayObs.length > 0
      ? decayObs.reduce((s, o) => s + o.decayRatePerH, 0) / decayObs.length
      : 0;

    // Average RH drift
    const rhObs = observations.filter(o => o.rhDriftPerH !== 0);
    const avgRhDrift = rhObs.length > 0
      ? rhObs.reduce((s, o) => s + o.rhDriftPerH, 0) / rhObs.length
      : 0;

    // Learned RH-to-DP coefficient
    const dpObs = observations.filter(o => o.rhToDpCoeff !== 0);
    const avgRhToDp = dpObs.length > 0
      ? dpObs.reduce((s, o) => s + o.rhToDpCoeff, 0) / dpObs.length
      : 0;

    // Asymptotic confidence: 1 - e^(-n/half_life), never caps at 1.0
    const confidence = 1 - Math.exp(-observations.length / CONFIDENCE_HALF_LIFE);

    models[zone] = {
      samples: observations.length,
      runoff_base: Math.round(Math.max(0, runoffBase) * 1000) / 1000,
      runoff_per_heat_min: Math.round(Math.max(0, runoffPerMin) * 10000) / 10000,
      peak_per_heat_min: Math.round(avgPeakPerMin * 10000) / 10000,
      peak_max_observed: Math.round(Math.max(...observations.map(o => o.peakRise)) * 100) / 100,
      decay_coeff: Math.round(avgDecayCoeff * 100000) / 100000,
      rh_drift_coeff: Math.round(avgRhDrift * 1000) / 1000,
      rh_to_dp_coeff: Math.round(avgRhToDp * 10000) / 10000,
      confidence: Math.round(confidence * 100) / 100,
      data_days: Math.round(dataDays * 10) / 10,
      last_updated: new Date().toISOString(),
    };
  }

  return models;
}

/** Simple linear regression: y = a + b*x */
function linearRegression(xs: number[], ys: number[]): { a: number; b: number } {
  const n = xs.length;
  if (n < 2) return { a: ys[0] || 0, b: 0 };
  const sumX = xs.reduce((s, x) => s + x, 0);
  const sumY = ys.reduce((s, y) => s + y, 0);
  const sumXY = xs.reduce((s, x, i) => s + x * ys[i], 0);
  const sumX2 = xs.reduce((s, x) => s + x * x, 0);
  const denom = n * sumX2 - sumX * sumX;
  if (Math.abs(denom) < 0.0001) return { a: sumY / n, b: 0 };
  const b = (n * sumXY - sumX * sumY) / denom;
  const a = (sumY - b * sumX) / n;
  return { a, b };
}

function historicalTrendAvailable(logs: any[], zone: string, outdoorTemp: number | null, outdoorHum: number | null): boolean {
  const zoneLogs = logs.filter(l => l.zone_name === zone && l.dp_spread != null);
  return zoneLogs.length >= 20;
}

async function validatePastPredictions(supabase: any) {
  const now = new Date().toISOString();
  const { data: unvalidated } = await supabase
    .from("dp_spread_predictions")
    .select("*")
    .eq("validated", false)
    .lt("predicted_for", now)
    .order("created_at", { ascending: true })
    .limit(50);

  if (!unvalidated || unvalidated.length === 0) return;

  for (const pred of unvalidated) {
    const targetTime = new Date(pred.predicted_for);
    const windowStart = new Date(targetTime.getTime() - 30 * 60_000).toISOString();
    const windowEnd = new Date(targetTime.getTime() + 30 * 60_000).toISOString();

    const { data: actuals } = await supabase
      .from("control_log")
      .select("dp_spread, temperature, action")
      .eq("zone_name", pred.zone_name)
      .gte("created_at", windowStart)
      .lte("created_at", windowEnd)
      .order("created_at", { ascending: false })
      .limit(1);

    if (!actuals || actuals.length === 0) {
      if (Date.now() - targetTime.getTime() > 6 * 3600_000) {
        await supabase.from("dp_spread_predictions").update({
          validated: true, validated_at: now,
        }).eq("id", pred.id);
      }
      continue;
    }

    const actual = actuals[0];
    if (actual.action === "heating_on") {
      await supabase.from("dp_spread_predictions").update({
        validated: true, validated_at: now,
        decision_made: "heating_active_at_validation",
      }).eq("id", pred.id);
      continue;
    }

    const actualDpSpread = actual.dp_spread;
    if (actualDpSpread == null) continue;

    const error = pred.predicted_dp_spread - actualDpSpread;
    const decisionCorrect = pred.current_dp_spread != null
      ? (pred.predicted_dp_spread >= pred.current_dp_spread) === (actualDpSpread >= pred.current_dp_spread)
      : null;

    await supabase.from("dp_spread_predictions").update({
      validated: true, validated_at: now,
      actual_dp_spread: actualDpSpread,
      actual_indoor_temp: actual.temperature,
      prediction_error: Math.round(error * 100) / 100,
      decision_correct: decisionCorrect,
    }).eq("id", pred.id);
  }
}

async function updateCoefficients(supabase: any, current: any) {
  const { data: validated } = await supabase
    .from("dp_spread_predictions")
    .select("*")
    .eq("validated", true)
    .not("prediction_error", "is", null)
    .not("decision_made", "eq", "heating_active_at_validation")
    .order("validated_at", { ascending: false })
    .limit(100);

  if (!validated || validated.length < 10) return;

  const avgError = validated.reduce((s: number, p: any) => s + (p.prediction_error || 0), 0) / validated.length;
  const learningRate = 0.01;

  let tempCorr = 0, humCorr = 0, count = 0;
  for (const p of validated) {
    if (p.current_outdoor_temp != null && p.predicted_outdoor_temp != null && p.prediction_error != null) {
      tempCorr += (p.predicted_outdoor_temp - p.current_outdoor_temp) * p.prediction_error;
      count++;
    }
    if (p.predicted_outdoor_humidity != null && p.prediction_error != null) {
      humCorr += p.prediction_error;
    }
  }
  if (count > 0) { tempCorr /= count; humCorr /= count; }

  const updated = {
    outdoor_temp_weight: current.outdoor_temp_weight - learningRate * tempCorr,
    outdoor_hum_weight: current.outdoor_hum_weight - learningRate * humCorr,
    time_decay_weight: current.time_decay_weight - learningRate * avgError,
    base_drift: current.base_drift - learningRate * avgError,
    learning_count: (current.learning_count || 0) + validated.length,
    last_avg_error: Math.round(avgError * 100) / 100,
    last_updated: new Date().toISOString(),
  };

  await supabase.from("system_state").upsert({
    key: "dp_predict_coefficients",
    value: updated,
    updated_at: new Date().toISOString(),
  });
}
