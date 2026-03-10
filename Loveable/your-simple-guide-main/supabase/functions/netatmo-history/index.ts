import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-supabase-client-platform, x-supabase-client-platform-version, x-supabase-client-runtime, x-supabase-client-runtime-version",
};

const NETATMO_API = "https://api.netatmo.com";

async function refreshToken(): Promise<string> {
  const clientId = Deno.env.get("NETATMO_CLIENT_ID")!;
  const clientSecret = Deno.env.get("NETATMO_CLIENT_SECRET")!;
  const refreshTkn = Deno.env.get("NETATMO_REFRESH_TOKEN")!;

  const resp = await fetch(`${NETATMO_API}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: refreshTkn,
      client_id: clientId,
      client_secret: clientSecret,
    }),
  });

  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Netatmo token refresh failed [${resp.status}]: ${text}`);
  }

  const data = await resp.json();
  return data.access_token;
}

async function getAccessToken(): Promise<string> {
  const accessToken = Deno.env.get("NETATMO_ACCESS_TOKEN");
  if (accessToken) {
    const test = await fetch(`${NETATMO_API}/api/getstationsdata`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/x-www-form-urlencoded",
      },
    });
    if (test.ok) return accessToken;
  }
  return await refreshToken();
}

interface MeasureRequest {
  device_id: string;
  module_id?: string;
  scale: string; // "1day", "3hours", "1hour", "30min"
  type: string; // comma-separated: "CO2,Noise,Temperature,Humidity"
  date_begin: number; // unix timestamp
  date_end: number;
}

async function getMeasure(token: string, params: MeasureRequest): Promise<Record<string, number[]>> {
  const body = new URLSearchParams({
    device_id: params.device_id,
    scale: params.scale,
    type: params.type,
    date_begin: params.date_begin.toString(),
    date_end: params.date_end.toString(),
    optimize: "false",
  });
  if (params.module_id) {
    body.set("module_id", params.module_id);
  }

  const resp = await fetch(`${NETATMO_API}/api/getmeasure`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body,
  });

  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Netatmo getmeasure failed [${resp.status}]: ${text}`);
  }

  const data = await resp.json();
  return data.body || {};
}

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  try {
    const accessToken = await getAccessToken();
    const body = await req.json().catch(() => ({}));
    const days = body.days || 365;

    const now = Math.floor(Date.now() / 1000);
    const dateBegin = now - days * 86400;
    // Use "1day" scale for long ranges to avoid hitting API limits
    const scale = days > 30 ? "1day" : "3hours";

    // First, get station data to discover device/module IDs
    const stationResp = await fetch(`${NETATMO_API}/api/getstationsdata`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/x-www-form-urlencoded",
      },
    });

    if (!stationResp.ok) {
      throw new Error(`Failed to get station data: ${stationResp.status}`);
    }

    const stationData = await stationResp.json();
    const devices = stationData.body?.devices || [];

    if (devices.length === 0) {
      return new Response(JSON.stringify({ error: "No Netatmo devices found" }), {
        status: 404,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const mainDevice = devices[0];
    const deviceId = mainDevice._id;

    // Collect measurements from all indoor modules with CO2
    // Main station (Cuisine Base) has CO2 + Noise
    // Indoor modules (Boyz, Slaapkamer) have CO2
    const results: {
      moduleName: string;
      moduleId: string;
      measurements: Record<string, number[]>;
      types: string[];
    }[] = [];

    // Main station - CO2 + Noise
    const mainTypes = ["CO2", "Noise"];
    try {
      const mainMeasures = await getMeasure(accessToken, {
        device_id: deviceId,
        scale,
        type: mainTypes.join(","),
        date_begin: dateBegin,
        date_end: now,
      });
      results.push({
        moduleName: mainDevice.module_name || mainDevice.station_name || "Station",
        moduleId: deviceId,
        measurements: mainMeasures,
        types: mainTypes,
      });
    } catch (e) {
      console.error(`Failed to get main station measures:`, e);
    }

    // Indoor modules with CO2
    for (const mod of mainDevice.modules || []) {
      // Only indoor modules (NAModule4) have CO2
      if (mod.type !== "NAModule4") continue;

      const modTypes = ["CO2"];
      try {
        const modMeasures = await getMeasure(accessToken, {
          device_id: deviceId,
          module_id: mod._id,
          scale,
          type: modTypes.join(","),
          date_begin: dateBegin,
          date_end: now,
        });
        results.push({
          moduleName: mod.module_name || mod._id,
          moduleId: mod._id,
          measurements: modMeasures,
          types: modTypes,
        });
      } catch (e) {
        console.error(`Failed to get measures for ${mod.module_name}:`, e);
      }
    }

    // Process into daily occupancy data
    // For each day, compute average CO2 across all modules and average noise from main station
    const dailyOccupancy: Record<string, { avgCo2: number; maxCo2: number; avgNoise: number; co2Readings: number; noiseReadings: number }> = {};

    for (const result of results) {
      const typeIdx: Record<string, number> = {};
      result.types.forEach((t, i) => { typeIdx[t] = i; });

      for (const [timestamp, values] of Object.entries(result.measurements)) {
        const ts = parseInt(timestamp, 10);
        const date = new Date(ts * 1000);
        const dateKey = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;

        if (!dailyOccupancy[dateKey]) {
          dailyOccupancy[dateKey] = { avgCo2: 0, maxCo2: 0, avgNoise: 0, co2Readings: 0, noiseReadings: 0 };
        }

        const entry = dailyOccupancy[dateKey];

        if ("CO2" in typeIdx && values[typeIdx["CO2"]] != null) {
          const co2 = values[typeIdx["CO2"]];
          entry.avgCo2 += co2;
          entry.maxCo2 = Math.max(entry.maxCo2, co2);
          entry.co2Readings += 1;
        }

        if ("Noise" in typeIdx && values[typeIdx["Noise"]] != null) {
          entry.avgNoise += values[typeIdx["Noise"]];
          entry.noiseReadings += 1;
        }
      }
    }

    // Finalize averages and determine occupancy
    const occupancyData = Object.entries(dailyOccupancy)
      .map(([date, entry]) => {
        const avgCo2 = entry.co2Readings > 0 ? Math.round(entry.avgCo2 / entry.co2Readings) : 0;
        const avgNoise = entry.noiseReadings > 0 ? Math.round(entry.avgNoise / entry.noiseReadings * 10) / 10 : 0;
        // Occupancy heuristic:
        // - CO2 > 600 ppm average suggests presence (outdoor ~400)
        // - Noise > 40 dB average from Cuisine also suggests presence
        const occupied = avgCo2 > 600 || avgNoise > 40;
        return {
          date,
          avgCo2,
          maxCo2: entry.maxCo2,
          avgNoise,
          occupied,
        };
      })
      .sort((a, b) => a.date.localeCompare(b.date));

    return new Response(JSON.stringify({ occupancy: occupancyData, modules: results.map(r => r.moduleName) }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e) {
    console.error("Netatmo history error:", e);
    return new Response(
      JSON.stringify({ error: e instanceof Error ? e.message : "Unknown error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});
