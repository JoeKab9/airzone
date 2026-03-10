import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.49.1";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-supabase-client-platform, x-supabase-client-platform-version, x-supabase-client-runtime, x-supabase-client-runtime-version",
};

const MIN_DATA_DAYS = 14; // Minimum days of data before auto-scheduling experiments
const EXPERIMENT_DURATION_DAYS = 4;
const COOLDOWN_DAYS = 7; // Wait between experiments

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
  );

  try {
    // 1. Gather all control_log data
    const { data: allLogs, error: logsErr } = await supabase
      .from("control_log")
      .select("*")
      .order("created_at", { ascending: true });
    if (logsErr) throw logsErr;

    if (!allLogs || allLogs.length < 50) {
      return respond({ status: "insufficient_data", message: "Need more data to analyze.", daysOfData: 0 });
    }

    const firstDate = new Date(allLogs[0].created_at);
    const lastDate = new Date(allLogs[allLogs.length - 1].created_at);
    const daysOfData = (lastDate.getTime() - firstDate.getTime()) / (86400000);

    // 2. Fetch existing experiments
    const { data: experiments } = await supabase
      .from("heating_experiments")
      .select("*")
      .order("start_date", { ascending: false });

    // 3. Analyze heating vs humidity correlation
    // Group logs by day and compute: avg humidity, heating minutes, outdoor conditions
    // Helper: Magnus dewpoint
    function calcDewpoint(tempC: number, rh: number): number {
      if (rh <= 0 || tempC == null) return 0;
      const a = 17.625, b = 243.04;
      const gamma = (a * tempC) / (b + tempC) + Math.log(Math.max(rh, 1) / 100);
      return (b * gamma) / (a - gamma);
    }

    const dayMap: Record<string, {
      humReadings: number[];
      dpSpreadReadings: number[];
      heatingMinutes: number;
      outdoorTemps: number[];
      outdoorHums: number[];
      outdoorDewpoints: number[];
      indoorTemps: number[];
      isExperiment: boolean;
    }> = {};

    const experimentDates = new Set<string>();
    for (const exp of (experiments || [])) {
      const start = new Date(exp.start_date);
      const end = new Date(exp.end_date);
      for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
        experimentDates.add(d.toISOString().split("T")[0]);
      }
    }

    for (const log of allLogs) {
      const day = log.created_at.split("T")[0];
      if (!dayMap[day]) dayMap[day] = { humReadings: [], dpSpreadReadings: [], heatingMinutes: 0, outdoorTemps: [], outdoorHums: [], outdoorDewpoints: [], indoorTemps: [], isExperiment: experimentDates.has(day) };
      const hum = Math.max(log.humidity_airzone || 0, log.humidity_netatmo || 0);
      if (hum > 0) dayMap[day].humReadings.push(hum);
      // Compute DP spread if we have temp + humidity
      if (log.temperature != null && hum > 0) {
        const dp = calcDewpoint(log.temperature, hum);
        dayMap[day].dpSpreadReadings.push(log.temperature - dp);
      }
      if (log.action === "heating_on") dayMap[day].heatingMinutes += 5;
      if (log.outdoor_temp != null) dayMap[day].outdoorTemps.push(log.outdoor_temp);
      if (log.outdoor_humidity != null) {
        dayMap[day].outdoorHums.push(log.outdoor_humidity);
        if (log.outdoor_temp != null) {
          dayMap[day].outdoorDewpoints.push(calcDewpoint(log.outdoor_temp, log.outdoor_humidity));
        }
      }
      if (log.temperature != null) dayMap[day].indoorTemps.push(log.temperature);
    }

    const avg = (arr: number[]) => arr.length > 0 ? arr.reduce((a, b) => a + b, 0) / arr.length : null;

    const dailyStats = Object.entries(dayMap)
      .map(([date, d]) => ({
        date,
        avgHumidity: avg(d.humReadings),
        avgDpSpread: avg(d.dpSpreadReadings),
        heatingMinutes: d.heatingMinutes,
        avgOutdoorTemp: avg(d.outdoorTemps),
        avgOutdoorHum: avg(d.outdoorHums),
        avgOutdoorDp: avg(d.outdoorDewpoints),
        avgIndoorTemp: avg(d.indoorTemps),
        isExperiment: d.isExperiment,
        readings: d.humReadings.length,
      }))
      .filter(d => d.avgHumidity !== null && d.readings >= 10)
      .sort((a, b) => a.date.localeCompare(b.date));

    // 4. Estimate thermal runoff by analyzing temperature decay after heating stops
    const runoffAnalysis = analyzeRunoff(allLogs);

    // 5. Correlation analysis: does more heating = lower humidity?
    const heatingDays = dailyStats.filter(d => d.heatingMinutes > 30 && !d.isExperiment);
    const noHeatingDays = dailyStats.filter(d => d.heatingMinutes <= 5 && !d.isExperiment);
    const experimentDays = dailyStats.filter(d => d.isExperiment);

    const avgHumHeating = avg(heatingDays.map(d => d.avgHumidity!));
    const avgHumNoHeating = avg(noHeatingDays.map(d => d.avgHumidity!));
    const avgHumExperiment = avg(experimentDays.map(d => d.avgHumidity!));
    const avgDpSpreadHeating = avg(heatingDays.filter(d => d.avgDpSpread != null).map(d => d.avgDpSpread!));
    const avgDpSpreadNoHeating = avg(noHeatingDays.filter(d => d.avgDpSpread != null).map(d => d.avgDpSpread!));

    // Weather-normalized comparison (only compare days with similar outdoor conditions)
    const weatherNormalized = analyzeWeatherNormalized(heatingDays, noHeatingDays);

    // 6. Build conclusions
    const conclusions = buildConclusions({
      daysOfData,
      heatingDays: heatingDays.length,
      noHeatingDays: noHeatingDays.length,
      experimentDays: experimentDays.length,
      avgHumHeating,
      avgHumNoHeating,
      avgHumExperiment,
      weatherNormalized,
      runoffAnalysis,
    });

    // 7. Auto-schedule experiment if enough data and no recent experiment
    let scheduledExperiment = null;
    if (daysOfData >= MIN_DATA_DAYS) {
      const lastExp = experiments?.[0];
      const daysSinceLastExp = lastExp
        ? (Date.now() - new Date(lastExp.end_date).getTime()) / 86400000
        : Infinity;

      if (daysSinceLastExp >= COOLDOWN_DAYS || !lastExp) {
        // Check no active experiment
        const activeExp = experiments?.find(e => e.status === "active" || e.status === "scheduled");
        if (!activeExp) {
          const startDate = new Date();
          startDate.setDate(startDate.getDate() + 1); // Start tomorrow
          const endDate = new Date(startDate);
          endDate.setDate(endDate.getDate() + EXPERIMENT_DURATION_DAYS - 1);

          const { data: newExp } = await supabase.from("heating_experiments").insert({
            type: "no_heating",
            status: "scheduled",
            start_date: startDate.toISOString().split("T")[0],
            end_date: endDate.toISOString().split("T")[0],
            reason: `Auto-scheduled after ${Math.round(daysOfData)} days of data. ${heatingDays.length} heating days, ${noHeatingDays.length} no-heating days observed.`,
          }).select().single();

          scheduledExperiment = newExp;
        }
      }
    }

    // 8. Complete finished experiments with results
    for (const exp of (experiments || [])) {
      if (exp.status === "active" && new Date(exp.end_date) < new Date()) {
        // Experiment ended, compute results
        const expDayStats = dailyStats.filter(d => d.date >= exp.start_date && d.date <= exp.end_date);
        const beforeDays = dailyStats.filter(d => d.date < exp.start_date).slice(-4);
        const afterDays = dailyStats.filter(d => d.date > exp.end_date).slice(0, 4);

        const avgDuring = avg(expDayStats.map(d => d.avgHumidity!));
        const avgBefore = avg(beforeDays.map(d => d.avgHumidity!));
        const avgAfter = avg(afterDays.map(d => d.avgHumidity!));
        const avgOutTemp = avg(expDayStats.map(d => d.avgOutdoorTemp!));
        const avgOutHum = avg(expDayStats.map(d => d.avgOutdoorHum!));
        const avgInTemp = avg(expDayStats.map(d => d.avgIndoorTemp!));

        const humDiff = avgDuring != null && avgBefore != null ? avgDuring - avgBefore : null;
        let conclusion = "";
        let recommendation = "";

        if (humDiff != null) {
          if (humDiff < 2) {
            conclusion = `Humidity stayed similar during no-heating (${avgBefore?.toFixed(1)}% → ${avgDuring?.toFixed(1)}%). Heating may not be the primary humidity driver.`;
            recommendation = "Consider reducing heating frequency. Outdoor air exchange and weather may be more significant factors.";
          } else if (humDiff >= 2 && humDiff < 5) {
            conclusion = `Humidity increased slightly without heating (+${humDiff.toFixed(1)}%). Heating has a moderate dehumidifying effect.`;
            recommendation = "Heating contributes to humidity control. Maintain current strategy but optimize timing.";
          } else {
            conclusion = `Humidity increased significantly without heating (+${humDiff.toFixed(1)}%). Heating is an effective dehumidifier here.`;
            recommendation = "Heating is critical for humidity control. Continue aggressive heating when RH is high.";
          }
        }

        await supabase.from("heating_experiments").update({
          status: "completed",
          avg_humidity_during: avgDuring ? Math.round(avgDuring * 10) / 10 : null,
          avg_humidity_before: avgBefore ? Math.round(avgBefore * 10) / 10 : null,
          avg_humidity_after: avgAfter ? Math.round(avgAfter * 10) / 10 : null,
          avg_outdoor_humidity: avgOutHum ? Math.round(avgOutHum * 10) / 10 : null,
          avg_outdoor_temp: avgOutTemp ? Math.round(avgOutTemp * 10) / 10 : null,
          avg_indoor_temp: avgInTemp ? Math.round(avgInTemp * 10) / 10 : null,
          thermal_runoff_hours: runoffAnalysis.estimatedRunoffHours,
          conclusion,
          recommendation,
          completed_at: new Date().toISOString(),
        }).eq("id", exp.id);
      }

      // Activate scheduled experiments that should start today
      if (exp.status === "scheduled" && new Date(exp.start_date) <= new Date()) {
        await supabase.from("heating_experiments").update({ status: "active" }).eq("id", exp.id);
      }
    }

    // 9. Analyze passive thermal reactivity (indoor temp response to outdoor temp when NOT heating)
    const passiveReactivity = analyzePassiveReactivity(allLogs);

    return respond({
      status: "ok",
      daysOfData: Math.round(daysOfData * 10) / 10,
      totalDataPoints: allLogs.length,
      dailyStats: dailyStats.slice(-30),
      heatingEffectiveness: {
        heatingDaysCount: heatingDays.length,
        noHeatingDaysCount: noHeatingDays.length,
        avgHumidityWithHeating: avgHumHeating ? Math.round(avgHumHeating * 10) / 10 : null,
        avgHumidityWithoutHeating: avgHumNoHeating ? Math.round(avgHumNoHeating * 10) / 10 : null,
        avgHumidityDuringExperiments: avgHumExperiment ? Math.round(avgHumExperiment * 10) / 10 : null,
        avgDpSpreadWithHeating: avgDpSpreadHeating ? Math.round(avgDpSpreadHeating * 10) / 10 : null,
        avgDpSpreadWithoutHeating: avgDpSpreadNoHeating ? Math.round(avgDpSpreadNoHeating * 10) / 10 : null,
        weatherNormalized,
      },
      runoffAnalysis,
      passiveReactivity,
      experiments: experiments || [],
      scheduledExperiment,
      conclusions,
    });
  } catch (e) {
    console.error("Analysis error:", e);
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

function analyzeRunoff(logs: any[]): {
  estimatedRunoffHours: number;
  tempDecayRate: number;
  samples: number;
  factors: Record<string, any>;
  description: string;
} {
  // Thermal runoff for concrete underfloor heating:
  // After heating stops, concrete mass continues radiating heat (temp rises or stays elevated).
  // "Runoff" = time from heating_off until temp starts consistently declining.
  // We use a sliding window average to smooth sensor noise.

  interface RunoffSample {
    runoffHours: number;     // Time temp stayed elevated after heating off
    peakRiseC: number;       // Max temp rise after shutoff (due to stored thermal energy)
    decayRatePerH: number;   // °C/h decline after peak
    tempAtOff: number;
    outdoorTemp: number;
    outdoorIndoorDelta: number; // Bigger delta = faster heat loss
    heatingDurationMin: number;
    zone: string;
  }
  const samples: RunoffSample[] = [];

  // Group logs by zone for efficiency
  const logsByZone: Record<string, any[]> = {};
  for (const log of logs) {
    if (!logsByZone[log.zone_name]) logsByZone[log.zone_name] = [];
    logsByZone[log.zone_name].push(log);
  }

  for (const [zone, zoneLogs] of Object.entries(logsByZone)) {
    for (let i = 0; i < zoneLogs.length; i++) {
      if (zoneLogs[i].action !== "heating_off") continue;
      const offTemp = zoneLogs[i].temperature;
      const offTime = new Date(zoneLogs[i].created_at).getTime();
      const outdoorTemp = zoneLogs[i].outdoor_temp;
      if (offTemp == null || outdoorTemp == null) continue;

      // Find heating duration by looking back
      let heatingDurationMin = 5;
      for (let k = i - 1; k >= 0 && k >= i - 200; k--) {
        if (zoneLogs[k].action === "heating_on") {
          heatingDurationMin = (offTime - new Date(zoneLogs[k].created_at).getTime()) / 60000;
          break;
        }
        if (zoneLogs[k].action === "heating_off") break;
      }

      // Collect temp readings after heating off (same zone, no new heating)
      const postReadings: { t: number; temp: number }[] = [];
      for (let j = i + 1; j < zoneLogs.length; j++) {
        const t = new Date(zoneLogs[j].created_at).getTime();
        if (t - offTime > 24 * 60 * 60 * 1000) break;
        if (zoneLogs[j].action === "heating_on") break;
        if (zoneLogs[j].temperature == null) continue;
        postReadings.push({ t, temp: zoneLogs[j].temperature });
      }

      if (postReadings.length < 6) continue; // Need enough readings for smoothing

      // Smooth with 3-point moving average to eliminate sensor noise
      const smoothed: { t: number; temp: number }[] = [];
      for (let s = 1; s < postReadings.length - 1; s++) {
        smoothed.push({
          t: postReadings[s].t,
          temp: (postReadings[s - 1].temp + postReadings[s].temp + postReadings[s + 1].temp) / 3,
        });
      }

      if (smoothed.length < 4) continue;

      // Find the peak: last point where smoothed temp is still rising or flat
      let peakIdx = 0;
      let peakTemp = smoothed[0].temp;
      for (let s = 1; s < smoothed.length; s++) {
        if (smoothed[s].temp >= peakTemp - 0.05) { // Allow 0.05° noise tolerance
          if (smoothed[s].temp > peakTemp) peakTemp = smoothed[s].temp;
          peakIdx = s;
        } else {
          // Check if this is a sustained decline (next 2 readings also declining)
          let sustained = true;
          for (let look = 1; look <= Math.min(2, smoothed.length - s - 1); look++) {
            if (smoothed[s + look].temp >= smoothed[s].temp) { sustained = false; break; }
          }
          if (sustained) break;
          // Otherwise just noise, continue looking
          if (smoothed[s].temp > peakTemp) { peakTemp = smoothed[s].temp; peakIdx = s; }
        }
      }

      const runoffHours = (smoothed[peakIdx].t - offTime) / (3600000);
      const peakRiseC = peakTemp - offTemp;

      // Measure decay rate: from peak to the last smoothed reading
      const lastSmoothed = smoothed[smoothed.length - 1];
      const decaySpanH = (lastSmoothed.t - smoothed[peakIdx].t) / 3600000;
      const decayC = peakTemp - lastSmoothed.temp;
      const decayRatePerH = decaySpanH > 0.5 ? decayC / decaySpanH : 0;

      // Only count if there's meaningful data (runoff > 5min, some temp movement)
      if (runoffHours >= 0.08) {
        samples.push({
          runoffHours,
          peakRiseC: Math.max(0, peakRiseC),
          decayRatePerH,
          tempAtOff: offTemp,
          outdoorTemp,
          outdoorIndoorDelta: offTemp - outdoorTemp,
          heatingDurationMin,
          zone,
        });
      }
    }
  }

  if (samples.length < 2) {
    return { estimatedRunoffHours: 0, tempDecayRate: 0, samples: samples.length, factors: {}, description: "Insufficient data to estimate thermal runoff (need 2+ heating-off events with enough post-readings)." };
  }

  const avgRunoff = samples.reduce((s, d) => s + d.runoffHours, 0) / samples.length;
  const avgPeakRise = samples.reduce((s, d) => s + d.peakRiseC, 0) / samples.length;
  const avgDecayRate = samples.reduce((s, d) => s + d.decayRatePerH, 0) / samples.length;

  // Factor analysis by zone
  // Collect ALL zone names from logs (so we show zones even with 0 heating events)
  const allZoneNames = new Set<string>();
  for (const log of logs) {
    if (log.zone_name) allZoneNames.add(log.zone_name);
  }

  const byZone: Record<string, { avgRunoff: number; avgRise: number; avgDecay: number; count: number }> = {};
  // Initialize all zones with zero
  for (const z of allZoneNames) {
    byZone[z] = { avgRunoff: 0, avgRise: 0, avgDecay: 0, count: 0 };
  }
  for (const s of samples) {
    if (!byZone[s.zone]) byZone[s.zone] = { avgRunoff: 0, avgRise: 0, avgDecay: 0, count: 0 };
    byZone[s.zone].avgRunoff += s.runoffHours;
    byZone[s.zone].avgRise += s.peakRiseC;
    byZone[s.zone].avgDecay += s.decayRatePerH;
    byZone[s.zone].count += 1;
  }
  for (const z of Object.values(byZone)) {
    z.avgRunoff /= z.count;
    z.avgRise /= z.count;
    z.avgDecay /= z.count;
  }

  // By indoor-outdoor temperature delta (bigger delta = faster heat loss from concrete)
  const smallDelta = samples.filter(s => s.outdoorIndoorDelta < 10);
  const largeDelta = samples.filter(s => s.outdoorIndoorDelta >= 10);
  const smallDeltaRunoff = smallDelta.length > 0 ? smallDelta.reduce((s, d) => s + d.runoffHours, 0) / smallDelta.length : null;
  const largeDeltaRunoff = largeDelta.length > 0 ? largeDelta.reduce((s, d) => s + d.runoffHours, 0) / largeDelta.length : null;

  // By outdoor temp
  const coldSamples = samples.filter(s => s.outdoorTemp < 8);
  const warmSamples = samples.filter(s => s.outdoorTemp >= 8);
  const coldRunoff = coldSamples.length > 0 ? coldSamples.reduce((s, d) => s + d.runoffHours, 0) / coldSamples.length : null;
  const warmRunoff = warmSamples.length > 0 ? warmSamples.reduce((s, d) => s + d.runoffHours, 0) / warmSamples.length : null;

  // By heating duration (more energy stored = longer runoff)
  const shortHeat = samples.filter(s => s.heatingDurationMin < 30);
  const longHeat = samples.filter(s => s.heatingDurationMin >= 30);
  const shortRunoff = shortHeat.length > 0 ? shortHeat.reduce((s, d) => s + d.runoffHours, 0) / shortHeat.length : null;
  const longRunoff = longHeat.length > 0 ? longHeat.reduce((s, d) => s + d.runoffHours, 0) / longHeat.length : null;

  const factors = {
    byZone,
    outdoorTempEffect: {
      coldAvgRunoffH: coldRunoff ? Math.round(coldRunoff * 10) / 10 : null,
      warmAvgRunoffH: warmRunoff ? Math.round(warmRunoff * 10) / 10 : null,
      description: coldRunoff && warmRunoff
        ? `Cold outdoor (<8°C): ${coldRunoff.toFixed(1)}h runoff vs warm (≥8°C): ${warmRunoff.toFixed(1)}h`
        : "Need more data across temperature ranges.",
    },
    indoorOutdoorDelta: {
      smallDeltaH: smallDeltaRunoff ? Math.round(smallDeltaRunoff * 10) / 10 : null,
      largeDeltaH: largeDeltaRunoff ? Math.round(largeDeltaRunoff * 10) / 10 : null,
      description: smallDeltaRunoff && largeDeltaRunoff
        ? `Small ΔT (<10°C): ${smallDeltaRunoff.toFixed(1)}h vs large ΔT (≥10°C): ${largeDeltaRunoff.toFixed(1)}h — larger delta means faster heat loss.`
        : "Need more data across temperature differentials.",
    },
    heatingDurationEffect: {
      shortAvgRunoffH: shortRunoff ? Math.round(shortRunoff * 10) / 10 : null,
      longAvgRunoffH: longRunoff ? Math.round(longRunoff * 10) / 10 : null,
      description: shortRunoff && longRunoff
        ? `Short heating (<30min): ${shortRunoff.toFixed(1)}h runoff vs long (≥30min): ${longRunoff.toFixed(1)}h — more stored energy = longer runoff.`
        : "Need more data across heating durations.",
    },
  };

  const parts = [
    `Concrete continues radiating heat ~${avgRunoff.toFixed(1)}h after heating stops.`,
    `Avg peak rise: +${avgPeakRise.toFixed(2)}°C above shutoff temp.`,
    `Avg decay after peak: ${avgDecayRate.toFixed(2)}°C/h.`,
  ];
  if (coldRunoff && warmRunoff) {
    parts.push(`Runoff is ${coldRunoff > warmRunoff ? "longer" : "shorter"} in cold weather (insulation retains more energy).`);
  }
  if (longRunoff && shortRunoff) {
    parts.push(`Longer heating (≥30min) → ${longRunoff.toFixed(1)}h runoff vs short → ${shortRunoff.toFixed(1)}h.`);
  }
  parts.push(`Based on ${samples.length} events across ${Object.keys(byZone).length} zones.`);

  return {
    estimatedRunoffHours: Math.round(avgRunoff * 10) / 10,
    tempDecayRate: Math.round(avgDecayRate * 100) / 100,
    samples: samples.length,
    factors,
    description: parts.join(" "),
  };
}

function analyzeWeatherNormalized(heatingDays: any[], noHeatingDays: any[]) {
  // Multi-factor matching: outdoor temp (±3°C), outdoor humidity (±15%), outdoor dewpoint (±2°C)
  interface Comparison {
    outdoorTemp: number;
    outdoorHum: number;
    outdoorDp: number | null;
    humWithHeating: number;
    humWithout: number;
    rhDiff: number;
    dpSpreadWithHeating: number | null;
    dpSpreadWithout: number | null;
    dpSpreadDiff: number | null;
  }
  const comparisons: Comparison[] = [];
  const usedNoHeat = new Set<number>();

  for (const hd of heatingDays) {
    if (hd.avgOutdoorTemp == null || hd.avgOutdoorHum == null) continue;
    
    let bestIdx = -1;
    let bestScore = Infinity;
    
    for (let i = 0; i < noHeatingDays.length; i++) {
      if (usedNoHeat.has(i)) continue;
      const nhd = noHeatingDays[i];
      if (nhd.avgOutdoorTemp == null || nhd.avgOutdoorHum == null) continue;
      
      const tempDiff = Math.abs(nhd.avgOutdoorTemp - hd.avgOutdoorTemp);
      const humDiff = Math.abs(nhd.avgOutdoorHum - hd.avgOutdoorHum);
      
      if (tempDiff > 3 || humDiff > 15) continue;
      
      // Also check outdoor dewpoint if available
      let dpPenalty = 0;
      if (hd.avgOutdoorDp != null && nhd.avgOutdoorDp != null) {
        const dpDiff = Math.abs(nhd.avgOutdoorDp - hd.avgOutdoorDp);
        if (dpDiff > 3) continue; // outdoor dewpoint must be within ±3°C
        dpPenalty = dpDiff * 0.3;
      }
      
      const score = tempDiff + humDiff * 0.2 + dpPenalty;
      if (score < bestScore) {
        bestScore = score;
        bestIdx = i;
      }
    }
    
    if (bestIdx >= 0) {
      const nhd = noHeatingDays[bestIdx];
      usedNoHeat.add(bestIdx);
      const dpSpreadDiff = (hd.avgDpSpread != null && nhd.avgDpSpread != null)
        ? hd.avgDpSpread - nhd.avgDpSpread
        : null;
      comparisons.push({
        outdoorTemp: hd.avgOutdoorTemp,
        outdoorHum: hd.avgOutdoorHum,
        outdoorDp: hd.avgOutdoorDp,
        humWithHeating: hd.avgHumidity,
        humWithout: nhd.avgHumidity,
        rhDiff: nhd.avgHumidity - hd.avgHumidity,
        dpSpreadWithHeating: hd.avgDpSpread,
        dpSpreadWithout: nhd.avgDpSpread,
        dpSpreadDiff,
      });
    }
  }

  if (comparisons.length === 0) {
    return { paired: 0, avgRhDiff: null, avgDpSpreadDiff: null, significant: false, factors: null, description: "Not enough comparable days (matching outdoor temp ±3°C, humidity ±15%, dewpoint ±3°C)." };
  }

  const avgRhDiff = comparisons.reduce((s, c) => s + c.rhDiff, 0) / comparisons.length;
  const dpSpreadComps = comparisons.filter(c => c.dpSpreadDiff != null);
  const avgDpSpreadDiff = dpSpreadComps.length > 0
    ? dpSpreadComps.reduce((s, c) => s + c.dpSpreadDiff!, 0) / dpSpreadComps.length
    : null;

  // Segment by outdoor humidity level
  const dryOutdoor = comparisons.filter(c => c.outdoorHum < 70);
  const wetOutdoor = comparisons.filter(c => c.outdoorHum >= 70);
  const dryRhDiff = dryOutdoor.length > 0 ? dryOutdoor.reduce((s, c) => s + c.rhDiff, 0) / dryOutdoor.length : null;
  const wetRhDiff = wetOutdoor.length > 0 ? wetOutdoor.reduce((s, c) => s + c.rhDiff, 0) / wetOutdoor.length : null;
  const dryDpDiff = dryOutdoor.filter(c => c.dpSpreadDiff != null).length > 0
    ? dryOutdoor.filter(c => c.dpSpreadDiff != null).reduce((s, c) => s + c.dpSpreadDiff!, 0) / dryOutdoor.filter(c => c.dpSpreadDiff != null).length : null;
  const wetDpDiff = wetOutdoor.filter(c => c.dpSpreadDiff != null).length > 0
    ? wetOutdoor.filter(c => c.dpSpreadDiff != null).reduce((s, c) => s + c.dpSpreadDiff!, 0) / wetOutdoor.filter(c => c.dpSpreadDiff != null).length : null;

  // Segment by outdoor temp
  const coldDays = comparisons.filter(c => c.outdoorTemp < 8);
  const warmDays = comparisons.filter(c => c.outdoorTemp >= 8);
  const coldRhDiff = coldDays.length > 0 ? coldDays.reduce((s, c) => s + c.rhDiff, 0) / coldDays.length : null;
  const warmRhDiff = warmDays.length > 0 ? warmDays.reduce((s, c) => s + c.rhDiff, 0) / warmDays.length : null;
  const coldDpDiff = coldDays.filter(c => c.dpSpreadDiff != null).length > 0
    ? coldDays.filter(c => c.dpSpreadDiff != null).reduce((s, c) => s + c.dpSpreadDiff!, 0) / coldDays.filter(c => c.dpSpreadDiff != null).length : null;
  const warmDpDiff = warmDays.filter(c => c.dpSpreadDiff != null).length > 0
    ? warmDays.filter(c => c.dpSpreadDiff != null).reduce((s, c) => s + c.dpSpreadDiff!, 0) / warmDays.filter(c => c.dpSpreadDiff != null).length : null;

  // Segment by outdoor dewpoint
  const lowDp = comparisons.filter(c => c.outdoorDp != null && c.outdoorDp < 5);
  const highDp = comparisons.filter(c => c.outdoorDp != null && c.outdoorDp >= 5);
  const lowDpRhDiff = lowDp.length > 0 ? lowDp.reduce((s, c) => s + c.rhDiff, 0) / lowDp.length : null;
  const highDpRhDiff = highDp.length > 0 ? highDp.reduce((s, c) => s + c.rhDiff, 0) / highDp.length : null;

  const factors = {
    byOutdoorHumidity: {
      dry: { pairs: dryOutdoor.length, avgRhDiff: dryRhDiff ? Math.round(dryRhDiff * 10) / 10 : null, avgDpSpreadDiff: dryDpDiff ? Math.round(dryDpDiff * 10) / 10 : null },
      wet: { pairs: wetOutdoor.length, avgRhDiff: wetRhDiff ? Math.round(wetRhDiff * 10) / 10 : null, avgDpSpreadDiff: wetDpDiff ? Math.round(wetDpDiff * 10) / 10 : null },
    },
    byOutdoorTemp: {
      cold: { pairs: coldDays.length, avgRhDiff: coldRhDiff ? Math.round(coldRhDiff * 10) / 10 : null, avgDpSpreadDiff: coldDpDiff ? Math.round(coldDpDiff * 10) / 10 : null },
      warm: { pairs: warmDays.length, avgRhDiff: warmRhDiff ? Math.round(warmRhDiff * 10) / 10 : null, avgDpSpreadDiff: warmDpDiff ? Math.round(warmDpDiff * 10) / 10 : null },
    },
    byOutdoorDewpoint: {
      low: { pairs: lowDp.length, avgRhDiff: lowDpRhDiff ? Math.round(lowDpRhDiff * 10) / 10 : null },
      high: { pairs: highDp.length, avgRhDiff: highDpRhDiff ? Math.round(highDpRhDiff * 10) / 10 : null },
    },
  };

  const parts: string[] = [];
  if (avgRhDiff > 2) {
    parts.push(`Heating reduces indoor RH by ~${avgRhDiff.toFixed(1)}% vs comparable no-heating days (${comparisons.length} pairs).`);
  } else if (avgRhDiff < -2) {
    parts.push(`Heating appears to INCREASE RH by ~${Math.abs(avgRhDiff).toFixed(1)}% — unusual.`);
  } else {
    parts.push(`No significant RH difference (${avgRhDiff.toFixed(1)}%) across ${comparisons.length} comparable pairs.`);
  }
  if (avgDpSpreadDiff != null) {
    parts.push(`DP spread is ${avgDpSpreadDiff > 0 ? `+${avgDpSpreadDiff.toFixed(1)}°C wider` : `${avgDpSpreadDiff.toFixed(1)}°C narrower`} with heating (${dpSpreadComps.length} pairs).`);
  }

  if (dryRhDiff != null && wetRhDiff != null) {
    parts.push(`By outdoor RH: dry (<70%) → ${dryRhDiff.toFixed(1)}% RH diff, wet (≥70%) → ${wetRhDiff.toFixed(1)}% RH diff.`);
  }
  if (coldRhDiff != null && warmRhDiff != null) {
    parts.push(`By outdoor temp: cold (<8°C) → ${coldRhDiff.toFixed(1)}% diff, warm (≥8°C) → ${warmRhDiff.toFixed(1)}% diff.`);
  }
  if (lowDpRhDiff != null && highDpRhDiff != null) {
    parts.push(`By outdoor DP: low (<5°C) → ${lowDpRhDiff.toFixed(1)}% diff, high (≥5°C) → ${highDpRhDiff.toFixed(1)}% diff.`);
  }

  return {
    paired: comparisons.length,
    avgRhDiff: Math.round(avgRhDiff * 10) / 10,
    avgDpSpreadDiff: avgDpSpreadDiff != null ? Math.round(avgDpSpreadDiff * 10) / 10 : null,
    significant: Math.abs(avgRhDiff) > 2 || (avgDpSpreadDiff != null && Math.abs(avgDpSpreadDiff) > 1),
    factors,
    description: parts.join(" "),
  };
}

function buildConclusions(data: {
  daysOfData: number;
  heatingDays: number;
  noHeatingDays: number;
  experimentDays: number;
  avgHumHeating: number | null;
  avgHumNoHeating: number | null;
  avgHumExperiment: number | null;
  weatherNormalized: any;
  runoffAnalysis: any;
}): { summary: string; confidence: string; recommendations: string[] } {
  const recs: string[] = [];
  let summary = "";
  let confidence = "low";

  if (data.daysOfData < MIN_DATA_DAYS) {
    summary = `Only ${Math.round(data.daysOfData)} days of data. Need at least ${MIN_DATA_DAYS} days for meaningful analysis.`;
    recs.push("Continue collecting data. First experiment will auto-schedule once enough baseline data exists.");
    return { summary, confidence, recommendations: recs };
  }

  confidence = data.experimentDays >= 4 ? "high" : data.noHeatingDays >= 3 ? "medium" : "low";

  // Prefer weather-normalized result over raw averages
  const wn = data.weatherNormalized;
  const hasNormalized = wn?.paired >= 3;

  if (hasNormalized) {
    const diff = wn.avgRhDiff;
    const dpDiff = wn.avgDpSpreadDiff;
    if (diff > 3) {
      summary = `Weather-normalized (${wn.paired} pairs): heating reduces RH by ~${diff}%`;
      if (dpDiff != null) summary += ` and widens DP spread by +${dpDiff}°C`;
      summary += ".";
      recs.push("Heating is effective for dehumidification when controlling for outdoor conditions.");
    } else if (diff > 0) {
      summary = `Weather-normalized: heating has a minor RH effect (~${diff}% reduction, ${wn.paired} pairs)`;
      if (dpDiff != null) summary += `. DP spread ${dpDiff > 0 ? `+${dpDiff}°C wider` : `${Math.abs(dpDiff)}°C narrower`}`;
      summary += ". Outdoor conditions drive most humidity variation.";
      recs.push("Heating has limited impact. Consider if the energy cost justifies the small humidity reduction.");
    } else {
      summary = `Weather-normalized: no evidence heating reduces RH (diff: ${diff}%, ${wn.paired} pairs)`;
      if (dpDiff != null) summary += `. DP spread diff: ${dpDiff > 0 ? "+" : ""}${dpDiff}°C`;
      summary += ". Outdoor moisture is the dominant factor.";
      recs.push("Heating is not effective for dehumidification. Focus on ventilation when outdoor air is drier.");
    }

    if (wn.factors?.byOutdoorHumidity) {
      const { dry, wet } = wn.factors.byOutdoorHumidity;
      if (dry.avgRhDiff != null && wet.avgRhDiff != null && Math.abs(dry.avgRhDiff - wet.avgRhDiff) > 2) {
        recs.push(`Heating effect varies with outdoor humidity: ${dry.avgRhDiff > wet.avgRhDiff ? "more" : "less"} effective when outdoor air is dry.`);
      }
    }
    if (wn.factors?.byOutdoorDewpoint) {
      const { low, high } = wn.factors.byOutdoorDewpoint;
      if (low.avgRhDiff != null && high.avgRhDiff != null && Math.abs(low.avgRhDiff - high.avgRhDiff) > 2) {
        recs.push(`Outdoor dewpoint matters: heating is ${low.avgRhDiff > high.avgRhDiff ? "more" : "less"} effective when outdoor DP is low (<5°C).`);
      }
    }
  } else {
    const humDiff = data.avgHumHeating != null && data.avgHumNoHeating != null
      ? data.avgHumNoHeating - data.avgHumHeating
      : null;
    if (humDiff != null) {
      summary = `Raw comparison (not weather-adjusted): humidity ${humDiff > 0 ? `${humDiff.toFixed(1)}% lower` : `${Math.abs(humDiff).toFixed(1)}% higher`} on heating days. ⚠ May be confounded by different weather conditions.`;
    } else {
      summary = "Insufficient data variation to compare heating vs non-heating periods.";
    }
    recs.push("More data needed for reliable weather-normalized comparison.");
  }

  if (data.runoffAnalysis?.estimatedRunoffHours > 0) {
    const r = data.runoffAnalysis;
    recs.push(`Thermal runoff: ${r.estimatedRunoffHours}h avg. ${r.factors?.heatingDurationEffect?.description || ""}`);
  }

  if (data.experimentDays === 0 && data.daysOfData >= MIN_DATA_DAYS) {
    recs.push("A controlled 4-day no-heating experiment has been auto-scheduled to validate findings with controlled conditions.");
  }

  return { summary, confidence, recommendations: recs };
}

/**
 * Analyze how indoor temperature reacts to outdoor temperature changes
 * during periods with NO heating. Shows thermal coupling / insulation quality per zone.
 */
function analyzePassiveReactivity(logs: any[]): {
  description: string;
  overall: { avgCouplingFactor: number; avgLagHours: number; samples: number } | null;
  byZone: Record<string, { avgCouplingFactor: number; avgLagHours: number; avgDecayRate: number; samples: number }>;
} {
  // Group logs by zone
  const logsByZone: Record<string, any[]> = {};
  for (const log of logs) {
    if (!logsByZone[log.zone_name]) logsByZone[log.zone_name] = [];
    logsByZone[log.zone_name].push(log);
  }

  const byZone: Record<string, { couplings: number[]; lags: number[]; decays: number[] }> = {};
  // Init all zones
  for (const z of Object.keys(logsByZone)) {
    byZone[z] = { couplings: [], lags: [], decays: [] };
  }

  for (const [zone, zoneLogs] of Object.entries(logsByZone)) {
    // Find stretches of no-heating (at least 4h = ~48 readings at 5min intervals)
    let noHeatStart = -1;

    for (let i = 0; i < zoneLogs.length; i++) {
      const isHeating = zoneLogs[i].action === "heating_on";

      if (isHeating) {
        // Check if we had a long enough no-heat stretch
        if (noHeatStart >= 0) {
          const stretchLogs = zoneLogs.slice(noHeatStart, i);
          const spanH = (new Date(stretchLogs[stretchLogs.length - 1].created_at).getTime() - new Date(stretchLogs[0].created_at).getTime()) / 3600000;

          if (spanH >= 4 && stretchLogs.length >= 20) {
            // Compute indoor vs outdoor temp correlation during this stretch
            const pairs: { indoor: number; outdoor: number; time: number }[] = [];
            for (const l of stretchLogs) {
              if (l.temperature != null && l.outdoor_temp != null) {
                pairs.push({ indoor: l.temperature, outdoor: l.outdoor_temp, time: new Date(l.created_at).getTime() });
              }
            }

            if (pairs.length >= 10) {
              // Coupling factor: how much does indoor temp change per °C of outdoor temp change?
              const outdoorRange = Math.max(...pairs.map(p => p.outdoor)) - Math.min(...pairs.map(p => p.outdoor));
              const indoorRange = Math.max(...pairs.map(p => p.indoor)) - Math.min(...pairs.map(p => p.indoor));

              if (outdoorRange > 1) {
                const coupling = indoorRange / outdoorRange;
                byZone[zone].couplings.push(coupling);

                // Decay rate: °C/h of indoor temp change (negative = cooling)
                const firstTemp = pairs[0].indoor;
                const lastTemp = pairs[pairs.length - 1].indoor;
                const spanHours = (pairs[pairs.length - 1].time - pairs[0].time) / 3600000;
                if (spanHours > 1) {
                  byZone[zone].decays.push((lastTemp - firstTemp) / spanHours);
                }

                // Estimate lag: cross-correlation to find delay
                // Simplified: find time offset where outdoor dip corresponds to indoor dip
                const midIdx = Math.floor(pairs.length / 2);
                const outdoorTrend = pairs[pairs.length - 1].outdoor - pairs[0].outdoor;
                const indoorTrend = pairs[pairs.length - 1].indoor - pairs[0].indoor;
                // If both trending same direction, lag is small
                if ((outdoorTrend > 0) === (indoorTrend > 0) && Math.abs(outdoorTrend) > 0.5) {
                  // Estimate lag from phase difference
                  const lagH = spanHours * 0.15; // rough estimate
                  byZone[zone].lags.push(lagH);
                }
              }
            }
          }
        }
        noHeatStart = -1;
      } else {
        if (noHeatStart < 0) noHeatStart = i;
      }
    }

    // Handle final stretch
    if (noHeatStart >= 0 && noHeatStart < zoneLogs.length - 20) {
      const stretchLogs = zoneLogs.slice(noHeatStart);
      const spanH = (new Date(stretchLogs[stretchLogs.length - 1].created_at).getTime() - new Date(stretchLogs[0].created_at).getTime()) / 3600000;
      if (spanH >= 4) {
        const pairs: { indoor: number; outdoor: number; time: number }[] = [];
        for (const l of stretchLogs) {
          if (l.temperature != null && l.outdoor_temp != null) {
            pairs.push({ indoor: l.temperature, outdoor: l.outdoor_temp, time: new Date(l.created_at).getTime() });
          }
        }
        if (pairs.length >= 10) {
          const outdoorRange = Math.max(...pairs.map(p => p.outdoor)) - Math.min(...pairs.map(p => p.outdoor));
          const indoorRange = Math.max(...pairs.map(p => p.indoor)) - Math.min(...pairs.map(p => p.indoor));
          if (outdoorRange > 1) {
            byZone[zone].couplings.push(indoorRange / outdoorRange);
            const spanHours = (pairs[pairs.length - 1].time - pairs[0].time) / 3600000;
            if (spanHours > 1) {
              byZone[zone].decays.push((pairs[pairs.length - 1].indoor - pairs[0].indoor) / spanHours);
            }
          }
        }
      }
    }
  }

  const avg = (arr: number[]) => arr.length > 0 ? arr.reduce((a, b) => a + b, 0) / arr.length : 0;

  const result: Record<string, { avgCouplingFactor: number; avgLagHours: number; avgDecayRate: number; samples: number }> = {};
  let totalCouplings: number[] = [];
  let totalLags: number[] = [];

  for (const [zone, d] of Object.entries(byZone)) {
    result[zone] = {
      avgCouplingFactor: Math.round(avg(d.couplings) * 100) / 100,
      avgLagHours: Math.round(avg(d.lags) * 10) / 10,
      avgDecayRate: Math.round(avg(d.decays) * 100) / 100,
      samples: d.couplings.length,
    };
    totalCouplings = totalCouplings.concat(d.couplings);
    totalLags = totalLags.concat(d.lags);
  }

  const overall = totalCouplings.length > 0 ? {
    avgCouplingFactor: Math.round(avg(totalCouplings) * 100) / 100,
    avgLagHours: Math.round(avg(totalLags) * 10) / 10,
    samples: totalCouplings.length,
  } : null;

  const parts: string[] = [];
  if (overall) {
    parts.push(`Indoor temp changes ~${overall.avgCouplingFactor}°C per 1°C outdoor change (${overall.samples} passive stretches).`);
    if (overall.avgCouplingFactor < 0.3) {
      parts.push("Good insulation — indoor temperature is well decoupled from outdoor.");
    } else if (overall.avgCouplingFactor < 0.6) {
      parts.push("Moderate coupling — indoor follows outdoor trends with some damping.");
    } else {
      parts.push("High coupling — indoor temperature is strongly affected by outdoor changes.");
    }
  } else {
    parts.push("Not enough non-heating stretches (4h+) to analyze passive thermal reactivity.");
  }

  return { description: parts.join(" "), overall, byZone: result };
}
