// humidity-control v3 - redeployed 2026-03-09
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.49.1";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-supabase-client-platform, x-supabase-client-platform-version, x-supabase-client-runtime, x-supabase-client-runtime-version",
};

// DP spread thresholds — hysteresis band to prevent cycling
const DP_SPREAD_HEAT_ON = 4;    // Heat ON when spread drops below 4°C
const DP_SPREAD_HEAT_OFF = 6;   // Heat OFF when spread reaches 6°C (safe margin)
const MAX_INDOOR_TEMP = 18;     // Never heat if indoor > 18°C
const PREDICTIVE_SHUTOFF_MARGIN = 1.0; // Shut off 1°C early for concrete runoff
const TARIFF = 0.1927;          // €/kWh
const PREDICTION_HORIZON_H = 3; // Predict DP spread 3 hours ahead
const PREDICTION_TRUST_RMSE = 1.5; // Trust predictions only if RMSE < 1.5°C
const MIN_PREDICTIONS_FOR_TRUST = 10; // Need this many validated predictions before trusting

// Netatmo → Airzone zone mapping (must match exact Airzone casing)
const NETATMO_TO_AIRZONE: Record<string, string> = {
  "Cuisine Base": "Cuisine",
  "Boyz": "Studio",
  "Slaapkamer": "Mur bleu",
};
const NETATMO_IGNORE = ["Indoor", "Ukkel Buiten"];

const CLOUD_BASE = "https://m.airzonecloud.com";

// ── Magnus formula dewpoint ────────────────────────────────────────────

function calcDewpoint(tempC: number, rh: number): number {
  if (rh <= 0 || tempC == null) return 0;
  const a = 17.625;
  const b = 243.04;
  const gamma = (a * tempC) / (b + tempC) + Math.log(Math.max(rh, 1) / 100);
  return Math.round((b * gamma) / (a - gamma) * 10) / 10;
}

// Best dewpoint for a room using all available sensor data
function calcRoomDewpoint(
  azTemp: number, azHum: number,
  ntTemp?: number | null, ntHum?: number | null
): number {
  const temps = [azTemp];
  if (ntTemp != null && ntTemp > 0) temps.push(ntTemp);
  const bestTemp = temps.reduce((a, b) => a + b, 0) / temps.length;
  const bestHum = (ntHum != null && ntHum > 0) ? ntHum : azHum;
  return calcDewpoint(bestTemp, bestHum);
}

// ── Airzone API ────────────────────────────────────────────────────────

async function airzoneLogin(): Promise<string> {
  const resp = await fetch(`${CLOUD_BASE}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: Deno.env.get("AIRZONE_EMAIL")!, password: Deno.env.get("AIRZONE_PASSWORD")! }),
  });
  if (!resp.ok) throw new Error(`Airzone login failed: ${resp.status}`);
  return (await resp.json()).token;
}

async function airzoneGet(token: string, path: string, params?: Record<string, string>) {
  const url = new URL(`${CLOUD_BASE}${path}`);
  if (params) Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  const resp = await fetch(url.toString(), {
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
  });
  if (!resp.ok) throw new Error(`Airzone GET ${path} failed: ${resp.status}`);
  return resp.json();
}

async function getAirzoneZones(token: string) {
  const instData = await airzoneGet(token, "/api/v1/installations");
  const installations = Array.isArray(instData) ? instData : (instData.installations || []);
  const zones: any[] = [];
  for (const inst of installations) {
    const instId = inst.installation_id || inst.id || inst._id || "";
    const detail = await airzoneGet(token, `/api/v1/installations/${instId}`);
    const instDetail = detail.installation || detail;
    for (const group of (instDetail.groups || [])) {
      for (const device of (group.devices || [])) {
        if (device.type !== "az_zone") continue;
        const devId = device.device_id || device.id || "";
        try {
          const status = await airzoneGet(token, `/api/v1/devices/${encodeURIComponent(devId)}/status`, { installation_id: instId });
          zones.push({ ...device, ...status, _installation_id: instId, _device_id: devId });
        } catch (e) { console.error(`Status fetch failed for ${devId}:`, e); }
      }
    }
  }
  return zones;
}

async function setAirzoneParam(token: string, deviceId: string, installationId: string, param: string, value: any) {
  const resp = await fetch(`${CLOUD_BASE}/api/v1/devices/${encodeURIComponent(deviceId)}`, {
    method: "PATCH",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ param, value, installation_id: installationId }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Airzone PATCH failed [${resp.status}]: ${text.substring(0, 200)}`);
  }
}

// ── Netatmo API ────────────────────────────────────────────────────────

async function getNetatmoModules(): Promise<any[]> {
  let accessToken = Deno.env.get("NETATMO_ACCESS_TOKEN");
  const NETATMO_API = "https://api.netatmo.com";
  let resp = await fetch(`${NETATMO_API}/api/getstationsdata`, {
    method: "POST",
    headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/x-www-form-urlencoded" },
  });
  if (!resp.ok) {
    const refreshResp = await fetch(`${NETATMO_API}/oauth2/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "refresh_token",
        refresh_token: Deno.env.get("NETATMO_REFRESH_TOKEN")!,
        client_id: Deno.env.get("NETATMO_CLIENT_ID")!,
        client_secret: Deno.env.get("NETATMO_CLIENT_SECRET")!,
      }),
    });
    if (!refreshResp.ok) throw new Error("Netatmo token refresh failed");
    const tokenData = await refreshResp.json();
    accessToken = tokenData.access_token;
    resp = await fetch(`${NETATMO_API}/api/getstationsdata`, {
      method: "POST",
      headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/x-www-form-urlencoded" },
    });
    if (!resp.ok) throw new Error("Netatmo API failed after refresh");
  }
  const data = await resp.json();
  const modules: any[] = [];
  for (const device of data.body?.devices || []) {
    modules.push({ name: device.module_name || device.station_name, type: device.type, ...device.dashboard_data });
    for (const mod of device.modules || []) {
      modules.push({ name: mod.module_name || mod.type, type: mod.type, ...mod.dashboard_data });
    }
  }
  return modules;
}

// ── Weather with 24h forecast ──────────────────────────────────────────

interface ForecastHour { time: string; temp: number; humidity: number; }

async function getWeatherWithForecast(): Promise<{
  current: { temp: number; humidity: number; dewpoint: number };
  forecast24h: ForecastHour[];
  bestHeatingWindow: { hour: string; temp: number } | null;
}> {
  const resp = await fetch(
    "https://api.open-meteo.com/v1/forecast?latitude=44.07&longitude=-1.26&current=temperature_2m,relative_humidity_2m&hourly=temperature_2m,relative_humidity_2m&forecast_days=2"
  );
  const data = await resp.json();
  const temp = data.current.temperature_2m;
  const rh = data.current.relative_humidity_2m;
  const dewpoint = calcDewpoint(temp, rh);

  const now = new Date();
  const forecast24h: ForecastHour[] = [];
  const times = data.hourly?.time || [];
  const temps = data.hourly?.temperature_2m || [];
  const hums = data.hourly?.relative_humidity_2m || [];

  for (let i = 0; i < times.length && forecast24h.length < 24; i++) {
    const t = new Date(times[i]);
    if (t > now) forecast24h.push({ time: times[i], temp: temps[i], humidity: hums[i] });
  }

  let bestHeatingWindow: { hour: string; temp: number } | null = null;
  let maxTemp = -Infinity;
  for (const f of forecast24h) {
    if (f.temp > maxTemp) { maxTemp = f.temp; bestHeatingWindow = { hour: f.time, temp: f.temp }; }
  }

  return { current: { temp, humidity: rh, dewpoint }, forecast24h, bestHeatingWindow };
}

// ── Occupancy detection ────────────────────────────────────────────────

function detectOccupancy(netatmoMods: any[]): { occupied: boolean; signals: string[] } {
  const signals: string[] = [];
  for (const mod of netatmoMods) {
    if (NETATMO_IGNORE.includes(mod.name)) continue;
    if (mod.CO2 && mod.CO2 > 600) signals.push(`CO2 ${mod.CO2}ppm in ${mod.name}`);
    if (mod.Noise && mod.Noise > 45) signals.push(`Noise ${mod.Noise}dB in ${mod.name}`);
  }
  return { occupied: signals.length >= 2, signals };
}

// ── Learning ───────────────────────────────────────────────────────────

async function getLearnedParams(supabase: any): Promise<Record<string, any>> {
  const { data } = await supabase.from("system_state").select("key, value").in("key", [
    "learned_runoff", "heating_stats", "occupancy_history", "emergency_stop"
  ]);
  const params: Record<string, any> = {};
  for (const row of (data || [])) params[row.key] = row.value;
  return params;
}

async function updateLearnedParams(supabase: any, key: string, value: any) {
  await supabase.from("system_state").upsert({ key, value, updated_at: new Date().toISOString() });
}

// ── Linky reconciliation ───────────────────────────────────────────────

async function fetchLinkyActualKwh(date: string): Promise<number | null> {
  try {
    const token = Deno.env.get("LINKY_TOKEN");
    const prm = Deno.env.get("LINKY_PRM");
    if (!token || !prm) return null;
    const nextDay = new Date(new Date(date).getTime() + 86400000).toISOString().slice(0, 10);
    const params = new URLSearchParams({ prm, start: date, end: nextDay });
    const resp = await fetch(`https://conso.boris.sh/api/daily_consumption?${params}`, {
      headers: { Authorization: `Bearer ${token}`, "User-Agent": "heatsmart/1.0" },
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    const intervals = data.meter_reading?.interval_reading || data.interval_reading || [];
    if (intervals.length === 0) return null;
    const totalWh = intervals.reduce((sum: number, iv: any) => sum + parseFloat(iv.value || "0"), 0);
    return Math.round(totalWh / 1000 * 100) / 100;
  } catch (e) { console.error("Linky reconciliation fetch error:", e); return null; }
}

async function reconcileYesterday(supabase: any): Promise<any | null> {
  const yesterday = new Date(Date.now() - 86400000).toISOString().split("T")[0];
  const { data: assessment } = await supabase
    .from("daily_assessment").select("*").eq("date", yesterday).is("actual_kwh", null).single();
  if (!assessment) return null;
  const actualKwh = await fetchLinkyActualKwh(yesterday);
  if (actualKwh === null) return null;

  const estimatedKwh = assessment.total_heating_kwh || 0;
  const accuracyPct = estimatedKwh > 0
    ? Math.round((1 - Math.abs(actualKwh - estimatedKwh) / actualKwh) * 100 * 10) / 10
    : (actualKwh === 0 ? 100 : 0);
  const correctionFactor = estimatedKwh > 0.1 ? Math.round((actualKwh / estimatedKwh) * 1000) / 1000 : 1.0;

  await supabase.from("daily_assessment").update({
    actual_kwh: actualKwh, estimation_accuracy_pct: accuracyPct, correction_factor: correctionFactor,
    notes: `${assessment.notes || ""} | Reconciled: est ${estimatedKwh} kWh vs actual ${actualKwh} kWh (${accuracyPct}% accurate).`,
  }).eq("date", yesterday);

  const { data: recentAssessments } = await supabase
    .from("daily_assessment").select("correction_factor").not("correction_factor", "is", null)
    .order("date", { ascending: false }).limit(7);
  if (recentAssessments && recentAssessments.length > 0) {
    const avgCF = recentAssessments.reduce((s: number, a: any) => s + (a.correction_factor || 1), 0) / recentAssessments.length;
    await supabase.from("system_state").upsert({
      key: "learned_correction_factor",
      value: { factor: Math.round(avgCF * 1000) / 1000, samples: recentAssessments.length },
      updated_at: new Date().toISOString(),
    });
  }
  return { date: yesterday, actualKwh, estimatedKwh, accuracyPct, correctionFactor };
}

// ── Daily assessment ───────────────────────────────────────────────────

async function runDailyAssessment(supabase: any) {
  const today = new Date().toISOString().split("T")[0];
  const { data: existing } = await supabase.from("daily_assessment").select("id").eq("date", today).single();
  if (existing) return null;

  const startOfDay = `${today}T00:00:00Z`;
  const { data: logs } = await supabase.from("control_log").select("*")
    .gte("created_at", startOfDay).order("created_at", { ascending: true });
  if (!logs || logs.length < 10) return null;

  const { count } = await supabase.from("daily_assessment").select("id", { count: "exact", head: true });
  if ((count || 0) === 0) {
    const weekAgo = new Date(Date.now() - 7 * 86400000).toISOString();
    const { data: oldLogs } = await supabase.from("control_log").select("id").lt("created_at", weekAgo).limit(1);
    if (!oldLogs || oldLogs.length === 0) return null;
  }

  const heatingOnCount = logs.filter((l: any) => l.action === "heating_on").length;
  const heatingMinutes = heatingOnCount * 5;

  // Assess dewpoint changes instead of just RH
  const firstLogs = logs.slice(0, Math.min(5, logs.length));
  const lastLogs = logs.slice(-Math.min(5, logs.length));
  const avgDpBefore = firstLogs.reduce((s: number, l: any) => s + (l.dewpoint || 0), 0) / firstLogs.length;
  const avgDpAfter = lastLogs.reduce((s: number, l: any) => s + (l.dewpoint || 0), 0) / lastLogs.length;
  const avgSpreadBefore = firstLogs.reduce((s: number, l: any) => s + (l.dp_spread || 0), 0) / firstLogs.length;
  const avgSpreadAfter = lastLogs.reduce((s: number, l: any) => s + (l.dp_spread || 0), 0) / lastLogs.length;

  // Fallback to RH if dewpoint columns not yet populated
  const avgHumBefore = firstLogs.reduce((s: number, l: any) => s + Math.max(l.humidity_airzone || 0, l.humidity_netatmo || 0), 0) / firstLogs.length;
  const avgHumAfter = lastLogs.reduce((s: number, l: any) => s + Math.max(l.humidity_airzone || 0, l.humidity_netatmo || 0), 0) / lastLogs.length;

  const zonesAbove65 = new Set(logs.filter((l: any) => (l.dp_spread || 99) < DP_SPREAD_HEAT_ON).map((l: any) => l.zone_name)).size;
  const zonesTotal = new Set(logs.map((l: any) => l.zone_name)).size;

  const { data: cfRow } = await supabase.from("system_state").select("value").eq("key", "learned_correction_factor").single();
  const cf = cfRow?.value?.factor ?? 1.0;
  const estKwh = (heatingMinutes / 60) * 2.5 * cf;
  const estCost = estKwh * TARIFF;

  const dpImproved = avgSpreadAfter > avgSpreadBefore;
  const humImproved = avgHumAfter < avgHumBefore;

  const assessment = {
    date: today,
    avg_humidity_before: Math.round(avgHumBefore),
    avg_humidity_after: Math.round(avgHumAfter),
    humidity_improved: dpImproved || humImproved,
    total_heating_kwh: Math.round(estKwh * 10) / 10,
    total_cost_eur: Math.round(estCost * 100) / 100,
    heating_minutes: heatingMinutes,
    ventilation_suggestions: 0,
    occupancy_detected: logs.some((l: any) => l.occupancy_detected),
    zones_above_65: zonesAbove65, // now: zones with spread < 4°C
    zones_total: zonesTotal,
    notes: dpImproved
      ? `DP spread improved: ${avgSpreadBefore.toFixed(1)}° → ${avgSpreadAfter.toFixed(1)}°. DP: ${avgDpBefore.toFixed(1)}° → ${avgDpAfter.toFixed(1)}°. CF=${cf}.`
      : `DP spread did not improve (${avgSpreadBefore.toFixed(1)}° → ${avgSpreadAfter.toFixed(1)}°). CF=${cf}.`,
  };

  await supabase.from("daily_assessment").upsert(assessment);
  return assessment;
}

// ── Absolute humidity helper ───────────────────────────────────────────

function calcAbsoluteHumidity(tempC: number, rh: number): number {
  // g/m³ — Magnus formula variant
  const es = 6.112 * Math.exp((17.67 * tempC) / (tempC + 243.5));
  return (es * rh * 2.1674) / (273.15 + tempC);
}

// ── DP Spread Prediction Model ────────────────────────────────────────

interface DpPrediction {
  predictedDpSpread: number;
  predictedIndoorTemp: number;
  forecastOutdoorTemp: number;
  forecastOutdoorHumidity: number;
  confidence: "high" | "medium" | "low";
  reasoning: string;
  naturalDrying: boolean;       // outdoor AH < indoor AH
  runoffBoost: number;          // extra °C DP spread from concrete runoff (if heating was recently on)
  bestCopHour: string | null;   // best COP window in 24h
  bestCopTemp: number | null;
  copSavingPct: number;         // estimated COP saving from deferral
}

interface ZoneRunoff {
  avgRunoffH: number;
  avgPeakRiseC: number;
  avgDecayRatePerH: number;
}

function getZoneRunoff(learned: Record<string, any>, zoneName: string): ZoneRunoff {
  // Try zone-specific data from heating-analysis byZone factors
  const runoffData = learned.learned_runoff;
  if (runoffData?.factors?.byZone?.[zoneName]) {
    const z = runoffData.factors.byZone[zoneName];
    return {
      avgRunoffH: z.avgRunoff ?? 1.5,
      avgPeakRiseC: z.avgRise ?? 0.3,
      avgDecayRatePerH: z.avgDecay ?? 0.2,
    };
  }
  // Fallback to global averages
  return {
    avgRunoffH: runoffData?.estimatedRunoffHours ?? 1.5,
    avgPeakRiseC: 0.3,
    avgDecayRatePerH: runoffData?.tempDecayRate ?? 0.2,
  };
}

function predictDpSpread(
  currentIndoorTemp: number,
  currentIndoorHum: number,
  currentDpSpread: number,
  forecast: ForecastHour[],
  learned: Record<string, any>,
  zoneName: string,
  isCurrentlyHeating: boolean,
  hoursAhead: number = PREDICTION_HORIZON_H,
  bestHeatingWindow: { hour: string; temp: number } | null = null,
): DpPrediction | null {
  if (forecast.length < hoursAhead) return null;

  // ── Zone-specific thermal parameters ──
  const zoneRunoff = getZoneRunoff(learned, zoneName);

  // Learned infiltration rate (how fast indoor AH drifts toward outdoor AH)
  const infiltrationRate = learned.prediction_model?.infiltration_rate ?? 0.15;

  const currentIndoorAH = calcAbsoluteHumidity(currentIndoorTemp, currentIndoorHum);

  // ── Forecast conditions over prediction window ──
  const windowForecast = forecast.slice(0, hoursAhead);
  const avgOutdoorTemp = windowForecast.reduce((s, f) => s + f.temp, 0) / windowForecast.length;
  const avgOutdoorHum = windowForecast.reduce((s, f) => s + f.humidity, 0) / windowForecast.length;
  const outdoorAH = calcAbsoluteHumidity(avgOutdoorTemp, avgOutdoorHum);

  // ── Predict indoor temp ──
  // Base: drift toward outdoor temp at zone-specific decay rate
  const tempDelta = avgOutdoorTemp - currentIndoorTemp;
  let predictedTemp = currentIndoorTemp + tempDelta * zoneRunoff.avgDecayRatePerH * hoursAhead * 0.15;

  // If currently heating: account for concrete thermal runoff
  // After heating stops, temp continues rising for runoffH hours by peakRiseC
  let runoffTempBoost = 0;
  if (isCurrentlyHeating) {
    // Heating will add stored energy. Even after we stop, temp rises by peakRise over runoffH hours
    runoffTempBoost = zoneRunoff.avgPeakRiseC;
    predictedTemp += runoffTempBoost;
  }

  // Clamp temperature
  const clampedTemp = tempDelta < 0
    ? Math.max(avgOutdoorTemp, Math.min(predictedTemp, currentIndoorTemp + runoffTempBoost + 1))
    : Math.min(avgOutdoorTemp + 3, predictedTemp);

  // ── Predict indoor AH ──
  const ahDelta = outdoorAH - currentIndoorAH;
  let predictedAH = currentIndoorAH + ahDelta * infiltrationRate * hoursAhead;

  // If heating: warm air holds more moisture at same AH but RH drops → spread widens
  // Concrete radiant heating also slightly reduces surface condensation
  if (isCurrentlyHeating) {
    // Heating doesn't change AH much, but the temp rise widens DP spread
    // (already captured by higher predictedTemp)
  }

  const clampedAH = Math.max(1, Math.min(predictedAH, 30));

  // ── Convert to RH and DP spread ──
  const es = 6.112 * Math.exp((17.67 * clampedTemp) / (clampedTemp + 243.5));
  const maxAH = (es * 100 * 2.1674) / (273.15 + clampedTemp);
  const predictedRH = Math.min(100, Math.max(10, (clampedAH / maxAH) * 100));
  const predictedDP = calcDewpoint(clampedTemp, predictedRH);
  const predictedDpSpread = Math.round((clampedTemp - predictedDP) * 10) / 10;

  // ── Runoff boost to DP spread (if heating is on, how much more spread after we stop) ──
  // After stop: temp rises by peakRise → spread widens by roughly same amount
  const runoffBoost = isCurrentlyHeating ? Math.round(zoneRunoff.avgPeakRiseC * 10) / 10 : 0;

  // ── Best COP window analysis ──
  let bestCopHour: string | null = null;
  let bestCopTemp: number | null = null;
  let copSavingPct = 0;
  const currentOutdoorTemp = forecast.length > 0 ? forecast[0].temp : avgOutdoorTemp;
  if (bestHeatingWindow && bestHeatingWindow.temp > currentOutdoorTemp + 2) {
    bestCopHour = bestHeatingWindow.hour;
    bestCopTemp = bestHeatingWindow.temp;
    // COP improvement ≈ 3% per °C warmer outdoor
    copSavingPct = Math.round((bestHeatingWindow.temp - currentOutdoorTemp) * 3);
  }

  // ── Confidence ──
  const predictionAccuracy = learned.prediction_model?.rmse;
  let confidence: "high" | "medium" | "low" = "low";
  if (predictionAccuracy != null && predictionAccuracy < 1.0) confidence = "high";
  else if (predictionAccuracy != null && predictionAccuracy < PREDICTION_TRUST_RMSE) confidence = "medium";

  // ── Reasoning ──
  const naturalDrying = outdoorAH < currentIndoorAH;
  const parts: string[] = [];
  if (naturalDrying) {
    parts.push(`Outdoor AH ${outdoorAH.toFixed(1)}g/m³ < indoor ${currentIndoorAH.toFixed(1)}g/m³ → drying.`);
  } else {
    parts.push(`Outdoor AH ${outdoorAH.toFixed(1)}g/m³ ≥ indoor ${currentIndoorAH.toFixed(1)}g/m³ → no drying.`);
  }
  if (isCurrentlyHeating && runoffBoost > 0) {
    parts.push(`Runoff: +${runoffBoost}° spread after stop (${zoneRunoff.avgRunoffH.toFixed(1)}h thermal mass).`);
  }
  parts.push(`Predicted: ${predictedDpSpread}° in ${hoursAhead}h (temp ${clampedTemp.toFixed(1)}°C).`);
  if (copSavingPct > 0) {
    parts.push(`Best COP: ${bestCopTemp}°C at ${bestCopHour} (${copSavingPct}% saving).`);
  }

  return {
    predictedDpSpread,
    predictedIndoorTemp: Math.round(clampedTemp * 10) / 10,
    forecastOutdoorTemp: Math.round(avgOutdoorTemp * 10) / 10,
    forecastOutdoorHumidity: Math.round(avgOutdoorHum),
    confidence,
    reasoning: parts.join(" "),
    naturalDrying,
    runoffBoost,
    bestCopHour,
    bestCopTemp,
    copSavingPct,
  };
}

// ── Validate past predictions ─────────────────────────────────────────

async function validatePredictions(
  supabase: any,
  zoneName: string,
  actualDpSpread: number,
  actualIndoorTemp: number,
): Promise<void> {
  const now = new Date();
  const windowStart = new Date(now.getTime() - 20 * 60000); // ±20 min
  const windowEnd = new Date(now.getTime() + 20 * 60000);

  const { data: pending } = await supabase
    .from("dp_spread_predictions")
    .select("id, predicted_dp_spread, decision_made")
    .eq("zone_name", zoneName)
    .eq("validated", false)
    .gte("predicted_for", windowStart.toISOString())
    .lte("predicted_for", windowEnd.toISOString())
    .limit(5);

  if (!pending || pending.length === 0) return;

  for (const pred of pending) {
    const error = Math.round((pred.predicted_dp_spread - actualDpSpread) * 10) / 10;
    // Validate decisions:
    // - skip_heating correct if spread ended up ≥ threshold (didn't need to heat)
    // - heat_anyway correct if spread ended up < threshold (needed heating)
    // - early_stop correct if spread ended up ≥ threshold (runoff was enough)
    // - defer_cop correct if spread stayed > 2° (didn't become critical while waiting)
    let decisionCorrect: boolean | null = null;
    if (pred.decision_made === "skip_heating" || pred.decision_made === "early_stop") {
      decisionCorrect = actualDpSpread >= DP_SPREAD_HEAT_ON;
    } else if (pred.decision_made === "heat_anyway") {
      decisionCorrect = actualDpSpread < DP_SPREAD_HEAT_ON;
    } else if (pred.decision_made === "defer_cop") {
      decisionCorrect = actualDpSpread > 2; // didn't become critical
    }

    await supabase.from("dp_spread_predictions").update({
      actual_dp_spread: actualDpSpread,
      actual_indoor_temp: actualIndoorTemp,
      prediction_error: error,
      validated: true,
      validated_at: now.toISOString(),
      decision_correct: decisionCorrect,
    }).eq("id", pred.id);
  }
}

// ── Get prediction model accuracy ─────────────────────────────────────

async function getPredictionAccuracy(supabase: any): Promise<{
  rmse: number | null;
  bias: number | null;
  count: number;
  correctDecisions: number;
  totalDecisions: number;
}> {
  const { data } = await supabase
    .from("dp_spread_predictions")
    .select("prediction_error, decision_correct, decision_made")
    .eq("validated", true)
    .order("validated_at", { ascending: false })
    .limit(100);

  if (!data || data.length === 0) return { rmse: null, bias: null, count: 0, correctDecisions: 0, totalDecisions: 0 };

  const errors = data.filter((d: any) => d.prediction_error != null).map((d: any) => d.prediction_error);
  const rmse = errors.length > 0
    ? Math.round(Math.sqrt(errors.reduce((s: number, e: number) => s + e * e, 0) / errors.length) * 100) / 100
    : null;
  const bias = errors.length > 0
    ? Math.round(errors.reduce((s: number, e: number) => s + e, 0) / errors.length * 100) / 100
    : null;

  const decisions = data.filter((d: any) => d.decision_made != null && d.decision_correct != null);
  const correct = decisions.filter((d: any) => d.decision_correct === true).length;

  return { rmse, bias, count: errors.length, correctDecisions: correct, totalDecisions: decisions.length };
}

// ── Store prediction ──────────────────────────────────────────────────

async function storePrediction(
  supabase: any,
  zoneName: string,
  prediction: DpPrediction,
  currentDpSpread: number,
  currentIndoorTemp: number,
  currentOutdoorTemp: number,
  decision: string,
) {
  const predictedFor = new Date(Date.now() + PREDICTION_HORIZON_H * 3600000);
  await supabase.from("dp_spread_predictions").insert({
    zone_name: zoneName,
    predicted_for: predictedFor.toISOString(),
    hours_ahead: PREDICTION_HORIZON_H,
    predicted_dp_spread: prediction.predictedDpSpread,
    predicted_indoor_temp: prediction.predictedIndoorTemp,
    predicted_outdoor_temp: prediction.forecastOutdoorTemp,
    predicted_outdoor_humidity: prediction.forecastOutdoorHumidity,
    current_dp_spread: currentDpSpread,
    current_indoor_temp: currentIndoorTemp,
    current_outdoor_temp: currentOutdoorTemp,
    decision_made: decision,
  });
}

// ── Main control loop ──────────────────────────────────────────────────

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  const supabase = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
  const logs: any[] = [];
  const results: string[] = [];

  try {
    // 1. Fetch all data in parallel
    const [token, netatmoMods, weather] = await Promise.all([
      airzoneLogin(),
      getNetatmoModules().catch((e) => { console.error("Netatmo:", e); return []; }),
      getWeatherWithForecast(),
    ]);

    const zones = await getAirzoneZones(token);
    const learned = await getLearnedParams(supabase);

    console.log(`Zones: ${zones.length}, Outdoor: ${weather.current.temp}°C ${weather.current.humidity}% DP ${weather.current.dewpoint}°C`);

    // Check emergency stop
    const emergencyStop = learned.emergency_stop;
    if (emergencyStop?.active) {
      console.log("⛔ Emergency stop active — skipping all control");
      for (const zone of zones) {
        const temp = zone.local_temp?.celsius ?? zone.local_temp ?? 0;
        const hum = zone.humidity ?? 0;
        const dp = calcDewpoint(temp, hum);
        logs.push({
          zone_name: zone.name, action: "no_change",
          humidity_airzone: hum, temperature: temp,
          dewpoint: dp, dp_spread: Math.round((temp - dp) * 10) / 10,
          outdoor_humidity: weather.current.humidity, outdoor_temp: weather.current.temp,
          reason: "⛔ Emergency stop active. Manual control via Airzone app.", success: true,
        });
      }
      if (logs.length > 0) await supabase.from("control_log").insert(logs);
      return new Response(JSON.stringify({ timestamp: new Date().toISOString(), emergency_stop: true, zones: logs }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // Netatmo lookup
    const netatmoByZone: Record<string, any> = {};
    for (const mod of netatmoMods) {
      if (NETATMO_IGNORE.includes(mod.name)) continue;
      const azName = NETATMO_TO_AIRZONE[mod.name];
      if (azName) netatmoByZone[azName] = mod;
    }

    // Occupancy
    const occupancy = detectOccupancy(netatmoMods);
    console.log(`Occupancy: ${occupancy.occupied ? "YES" : "no"} — ${occupancy.signals.join(", ") || "no signals"}`);

    // Heating stats
    const heatingStats = learned.heating_stats || { totalOnMinutes: 0, totalCycles: 0, totalSaved: 0 };

    // Best heating window
    const currentOutdoorTemp = weather.current.temp;
    const bestTemp = weather.bestHeatingWindow?.temp ?? currentOutdoorTemp;
    const bestWindow = weather.bestHeatingWindow;

    // Get prediction model accuracy
    const predAccuracy = await getPredictionAccuracy(supabase).catch(() => ({ rmse: null, bias: null, count: 0, correctDecisions: 0, totalDecisions: 0 }));
    const trustPredictions = predAccuracy.count >= MIN_PREDICTIONS_FOR_TRUST && predAccuracy.rmse != null && predAccuracy.rmse < PREDICTION_TRUST_RMSE;
    console.log(`Prediction model: ${predAccuracy.count} validated, RMSE ${predAccuracy.rmse ?? "N/A"}, trusted: ${trustPredictions}, decisions correct: ${predAccuracy.correctDecisions}/${predAccuracy.totalDecisions}`);

    // Update learned prediction model params
    if (predAccuracy.count >= 5 && predAccuracy.bias != null) {
      const currentModel = learned.prediction_model || {};
      // Adjust infiltration rate based on bias: if we consistently over-predict spread, infiltration is slower
      const currentRate = currentModel.infiltration_rate ?? 0.15;
      const adjustment = predAccuracy.bias > 0.3 ? -0.005 : predAccuracy.bias < -0.3 ? 0.005 : 0;
      const newRate = Math.max(0.05, Math.min(0.4, currentRate + adjustment));
      await updateLearnedParams(supabase, "prediction_model", {
        ...currentModel,
        infiltration_rate: Math.round(newRate * 1000) / 1000,
        rmse: predAccuracy.rmse,
        bias: predAccuracy.bias,
        validated_count: predAccuracy.count,
        correct_decisions: predAccuracy.correctDecisions,
        total_decisions: predAccuracy.totalDecisions,
        last_updated: new Date().toISOString(),
      });
    }

    // Check for active heating experiments
    const experimentStatus = await (async () => {
      try {
        const today = new Date().toISOString().slice(0, 10);
        const { data } = await supabase
          .from("heating_experiments")
          .select("*")
          .eq("status", "active")
          .lte("start_date", today)
          .gte("end_date", today)
          .limit(1);
        if (data && data.length > 0) return { active: true, experiment: data[0] };
      } catch (e) { console.error("Experiment check failed:", e); }
      return { active: false, experiment: null };
    })();

    // 2. Control each zone — unified predictive + COP decision engine
    for (const zone of zones) {
      const name = zone.name;
      const azHum = zone.humidity ?? 0;
      const indoorTemp = zone.local_temp?.celsius ?? zone.local_temp ?? 0;
      const power = zone.power === true || zone.power === 1;
      const netatmo = netatmoByZone[name];
      const ntHum = netatmo?.Humidity ?? null;
      const ntTemp = netatmo?.Temperature ?? null;

      // Calculate dewpoint using best available data
      const dewpoint = calcRoomDewpoint(indoorTemp, azHum, ntTemp, ntHum);
      const dpSpread = Math.round((indoorTemp - dewpoint) * 10) / 10;

      // Validate past predictions for this zone
      await validatePredictions(supabase, name, dpSpread, indoorTemp).catch((e) => console.error(`Prediction validation error for ${name}:`, e));

      // Generate prediction with zone-specific runoff data
      const bestHum = ntHum ?? azHum;
      const prediction = predictDpSpread(
        indoorTemp, bestHum, dpSpread, weather.forecast24h, learned,
        name, power, PREDICTION_HORIZON_H, bestWindow,
      );

      // Zone-specific runoff for early-stop logic
      const zoneRunoff = getZoneRunoff(learned, name);

      let action = "no_change";
      let reason = "";
      let success = true;
      let energySavedPct = 0;
      let predictionDecision: string | null = null;

      // Predictive shutoff for concrete thermal inertia
      const predictiveLimit = MAX_INDOOR_TEMP - PREDICTIVE_SHUTOFF_MARGIN;
      const tooWarmIndoor = indoorTemp > predictiveLimit;
      const approachingLimit = indoorTemp > (predictiveLimit - 0.5) && power;

      if (experimentStatus.active) {
        // ── Experiment: block all heating ──
        if (power) {
          action = "heating_off";
          reason = `🧪 Experiment active (${experimentStatus.experiment.start_date} → ${experimentStatus.experiment.end_date}). Off for analysis. DP spread ${dpSpread}°.`;
          try { await setAirzoneParam(token, zone._device_id, zone._installation_id, "power", false); }
          catch (e) { reason += ` FAILED: ${e instanceof Error ? e.message : "?"}`; success = false; }
        } else {
          reason = `🧪 Experiment active. Heating blocked. DP spread ${dpSpread}°, indoor ${indoorTemp}°C.`;
        }
      } else if (tooWarmIndoor || approachingLimit) {
        // ── Temp limit: concrete runoff shutoff ──
        if (power) {
          action = "heating_off";
          reason = `Predictive shutoff: ${indoorTemp}°C approaching ${MAX_INDOOR_TEMP}°C (margin ${PREDICTIVE_SHUTOFF_MARGIN}°C, runoff ${zoneRunoff.avgRunoffH.toFixed(1)}h). DP spread ${dpSpread}°.`;
          try { await setAirzoneParam(token, zone._device_id, zone._installation_id, "power", false); }
          catch (e) { reason += ` FAILED: ${e instanceof Error ? e.message : "?"}`; success = false; }
        } else {
          reason = `Indoor ${indoorTemp}°C ≥ ${predictiveLimit}°C limit. No heating. DP spread ${dpSpread}°.`;
        }

      } else if (power && dpSpread < DP_SPREAD_HEAT_OFF) {
        // ── Currently heating: check if runoff will carry us to safety (early stop) ──
        const spreadAfterRunoff = dpSpread + zoneRunoff.avgPeakRiseC;
        if (
          prediction &&
          trustPredictions &&
          spreadAfterRunoff >= DP_SPREAD_HEAT_OFF &&
          prediction.predictedDpSpread >= DP_SPREAD_HEAT_ON
        ) {
          // Runoff + natural trends will carry spread to safe zone → stop early!
          action = "heating_off";
          predictionDecision = "early_stop";
          reason = `⏱ Early stop: spread ${dpSpread}° + runoff ${zoneRunoff.avgPeakRiseC.toFixed(1)}° = ~${spreadAfterRunoff.toFixed(1)}° (≥${DP_SPREAD_HEAT_OFF}°). Model: ${prediction.predictedDpSpread}° in ${PREDICTION_HORIZON_H}h. ${prediction.reasoning}`;
          energySavedPct = 30; // Rough estimate of savings from early stop
          try { await setAirzoneParam(token, zone._device_id, zone._installation_id, "power", false); }
          catch (e) { reason += ` FAILED: ${e instanceof Error ? e.message : "?"}`; success = false; }
        } else {
          // Keep heating — spread not yet safe even with runoff
          reason = `Heating: spread ${dpSpread}° (target ≥${DP_SPREAD_HEAT_OFF}°), runoff would add ~${zoneRunoff.avgPeakRiseC.toFixed(1)}°.`;
          if (prediction) reason += ` Model: ${prediction.predictedDpSpread}° in ${PREDICTION_HORIZON_H}h.`;
          heatingStats.totalOnMinutes += 5;
        }

      } else if (dpSpread < DP_SPREAD_HEAT_ON && !power) {
        // ── DP spread too low — unified decision: skip / defer / heat ──
        if (dpSpread <= 2) {
          // CRITICAL: always heat immediately
          action = "heating_on";
          predictionDecision = "heat_anyway";
          reason = `⚠ CRITICAL DP spread ${dpSpread}° (< 2°). Condensation imminent. Indoor ${indoorTemp}°C, DP ${dewpoint}°C.`;
          heatingStats.totalOnMinutes += 5;
          heatingStats.totalCycles += 1;
          try { await setAirzoneParam(token, zone._device_id, zone._installation_id, "power", true); }
          catch (e) { reason += ` FAILED: ${e instanceof Error ? e.message : "?"}`; success = false; }
        } else if (prediction && trustPredictions && prediction.confidence !== "low") {
          // We have a trusted prediction — make unified decision
          const willRecover = prediction.predictedDpSpread >= DP_SPREAD_HEAT_ON;
          const hasBetterCopWindow = prediction.copSavingPct >= 10;

          if (willRecover && prediction.naturalDrying) {
            // 🔮 Natural drying will fix it — skip entirely
            action = "skip_heating";
            predictionDecision = "skip_heating";
            energySavedPct = 100;
            reason = `🔮 Skip: spread ${dpSpread}° but model predicts ${prediction.predictedDpSpread}° in ${PREDICTION_HORIZON_H}h. ${prediction.reasoning}`;
          } else if (hasBetterCopWindow && !willRecover) {
            // 📅 Need to heat, but defer to better COP window
            action = "defer_heating";
            predictionDecision = "defer_cop";
            energySavedPct = prediction.copSavingPct;
            reason = `📅 Defer: spread ${dpSpread}° needs heating, but outdoor ${currentOutdoorTemp}°C → ${prediction.bestCopTemp}°C at ${prediction.bestCopHour} (${prediction.copSavingPct}% COP saving). ${prediction.reasoning}`;
          } else if (willRecover && hasBetterCopWindow) {
            // 🔮📅 Both: natural drying AND better window available — definitely skip
            action = "skip_heating";
            predictionDecision = "skip_heating";
            energySavedPct = 100;
            reason = `🔮📅 Skip: natural drying predicts ${prediction.predictedDpSpread}° in ${PREDICTION_HORIZON_H}h, plus better COP window at ${prediction.bestCopHour}. ${prediction.reasoning}`;
          } else {
            // Model says it won't recover, no better window → heat now
            action = "heating_on";
            predictionDecision = "heat_anyway";
            reason = `DP spread ${dpSpread}° < ${DP_SPREAD_HEAT_ON}°. Model: ${prediction.predictedDpSpread}° in ${PREDICTION_HORIZON_H}h (no recovery expected). Heating on.`;
            heatingStats.totalOnMinutes += 5;
            heatingStats.totalCycles += 1;
            try { await setAirzoneParam(token, zone._device_id, zone._installation_id, "power", true); }
            catch (e) { reason += ` FAILED: ${e instanceof Error ? e.message : "?"}`; success = false; }
          }
        } else {
          // No trusted prediction — fallback to simple COP deferral
          const isNearBestWindow = currentOutdoorTemp >= bestTemp - 2;
          if (!isNearBestWindow && bestTemp > currentOutdoorTemp + 3) {
            action = "defer_heating";
            predictionDecision = prediction ? "defer_cop" : null;
            energySavedPct = Math.round((bestTemp - currentOutdoorTemp) * 3);
            reason = `DP spread ${dpSpread}°. No trusted prediction yet. Deferring: outdoor ${currentOutdoorTemp}°C → forecast ${bestTemp}°C at ${bestWindow?.hour}.`;
          } else {
            action = "heating_on";
            predictionDecision = "heat_anyway";
            reason = `DP spread ${dpSpread}° < ${DP_SPREAD_HEAT_ON}°. Indoor ${indoorTemp}°C, DP ${dewpoint}°C, outdoor ${currentOutdoorTemp}°C.`;
            if (prediction) reason += ` Model (untrusted): ${prediction.predictedDpSpread}° in ${PREDICTION_HORIZON_H}h.`;
            reason += " Heating on.";
            heatingStats.totalOnMinutes += 5;
            heatingStats.totalCycles += 1;
            try { await setAirzoneParam(token, zone._device_id, zone._installation_id, "power", true); }
            catch (e) { reason += ` FAILED: ${e instanceof Error ? e.message : "?"}`; success = false; }
          }
        }
      } else if (dpSpread >= DP_SPREAD_HEAT_OFF && power) {
        // ── DP spread safe — turn off ──
        action = "heating_off";
        reason = `DP spread ${dpSpread}° ≥ ${DP_SPREAD_HEAT_OFF}° (safe). DP ${dewpoint}°C. Off.`;
        try { await setAirzoneParam(token, zone._device_id, zone._installation_id, "power", false); }
        catch (e) { reason += ` FAILED: ${e instanceof Error ? e.message : "?"}`; success = false; }
      } else {
        // ── Idle or in-band ──
        reason = `DP spread ${dpSpread}°, indoor ${indoorTemp}°C, DP ${dewpoint}°C — ${power ? "heating (band)" : "idle"}.`;
        if (prediction) reason += ` Forecast: ${prediction.predictedDpSpread}° in ${PREDICTION_HORIZON_H}h.`;
      }

      // Store prediction if we made one
      if (prediction && predictionDecision) {
        await storePrediction(supabase, name, prediction, dpSpread, indoorTemp, currentOutdoorTemp, predictionDecision).catch((e) => console.error(`Store prediction error:`, e));
      }

      results.push(`${name}: ${action} — ${reason}`);
      logs.push({
        zone_name: name,
        action,
        humidity_airzone: azHum,
        humidity_netatmo: ntHum,
        temperature: indoorTemp,
        dewpoint,
        dp_spread: dpSpread,
        outdoor_humidity: weather.current.humidity,
        outdoor_temp: weather.current.temp,
        forecast_temp_max: bestTemp,
        forecast_best_hour: weather.bestHeatingWindow?.hour || null,
        occupancy_detected: occupancy.occupied,
        energy_saved_pct: energySavedPct,
        reason,
        success,
      });
    }

    // 3. Save logs
    if (logs.length > 0) {
      const { error } = await supabase.from("control_log").insert(logs);
      if (error) console.error("Log insert error:", error);
    }

    // 4. Update learned params
    heatingStats.totalSaved += logs.reduce((s: number, l: any) => s + (l.energy_saved_pct || 0), 0);
    await updateLearnedParams(supabase, "heating_stats", heatingStats);
    await updateLearnedParams(supabase, "occupancy_history", {
      lastCheck: new Date().toISOString(), occupied: occupancy.occupied, signals: occupancy.signals,
    });

    // 5. Daily assessment + reconcile
    const [assessment, reconciliation] = await Promise.all([
      runDailyAssessment(supabase).catch((e) => { console.error("Assessment error:", e); return null; }),
      reconcileYesterday(supabase).catch((e) => { console.error("Reconciliation error:", e); return null; }),
    ]);

    return new Response(JSON.stringify({
      timestamp: new Date().toISOString(),
      outdoor: weather.current,
      bestHeatingWindow: weather.bestHeatingWindow,
      occupancy,
      predictionModel: {
        trusted: trustPredictions,
        rmse: predAccuracy.rmse,
        bias: predAccuracy.bias,
        validatedCount: predAccuracy.count,
        correctDecisions: `${predAccuracy.correctDecisions}/${predAccuracy.totalDecisions}`,
      },
      zones: logs,
      summary: results,
      assessment,
      reconciliation,
    }), { headers: { ...corsHeaders, "Content-Type": "application/json" } });
  } catch (e) {
    console.error("Control error:", e);
    return new Response(JSON.stringify({ error: e instanceof Error ? e.message : "Unknown" }), {
      status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
