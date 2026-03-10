import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-supabase-client-platform, x-supabase-client-platform-version, x-supabase-client-runtime, x-supabase-client-runtime-version",
};

const NETATMO_API = "https://api.netatmo.com";
// Max measurements per batch to stay within edge function timeout
const BATCH_DAYS = 30;

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
    throw new Error(`Token refresh failed [${resp.status}]: ${text}`);
  }
  return (await resp.json()).access_token;
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

interface ModuleInfo {
  name: string;
  deviceId: string;
  moduleId: string | null; // null for main station
  type: string;
  dataTypes: string[];
}

async function discoverModules(token: string): Promise<ModuleInfo[]> {
  const resp = await fetch(`${NETATMO_API}/api/getstationsdata`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
  });
  if (!resp.ok) throw new Error(`getstationsdata failed: ${resp.status}`);
  const data = await resp.json();
  const devices = data.body?.devices || [];
  const modules: ModuleInfo[] = [];

  for (const device of devices) {
    // Main station (NAMain) — has Temperature, Humidity, CO2, Noise, Pressure
    modules.push({
      name: device.module_name || device.station_name || "Station",
      deviceId: device._id,
      moduleId: null,
      type: device.type || "NAMain",
      dataTypes: ["Temperature", "Humidity", "CO2", "Noise", "Pressure"],
    });

    for (const mod of device.modules || []) {
      const types: string[] = [];
      if (mod.type === "NAModule1") types.push("Temperature", "Humidity"); // Outdoor
      if (mod.type === "NAModule4") types.push("Temperature", "Humidity", "CO2"); // Indoor extra
      if (mod.type === "NAModule3") types.push("Rain"); // Rain gauge
      if (mod.type === "NAModule2") types.push("WindStrength", "WindAngle"); // Wind

      if (types.length === 0) continue;
      // Only collect temp/humidity/CO2/noise modules
      if (!types.some(t => ["Temperature", "Humidity", "CO2"].includes(t))) continue;

      modules.push({
        name: mod.module_name || mod._id,
        deviceId: device._id,
        moduleId: mod._id,
        type: mod.type,
        dataTypes: types,
      });
    }
  }
  return modules;
}

async function fetchMeasures(
  token: string,
  deviceId: string,
  moduleId: string | null,
  types: string[],
  dateBegin: number,
  dateEnd: number,
  scale = "5min"
): Promise<Record<string, number[]>> {
  const body = new URLSearchParams({
    device_id: deviceId,
    scale,
    type: types.join(","),
    date_begin: dateBegin.toString(),
    date_end: dateEnd.toString(),
    optimize: "false",
  });
  if (moduleId) body.set("module_id", moduleId);

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
    throw new Error(`getmeasure [${resp.status}]: ${text}`);
  }
  const data = await resp.json();
  return data.body || {};
}

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  try {
    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const supabaseKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const supabase = createClient(supabaseUrl, supabaseKey);

    const token = await getAccessToken();
    const modules = await discoverModules(token);
    const now = Math.floor(Date.now() / 1000);

    const body = await req.json().catch(() => ({}));
    const action = body.action || "collect"; // "collect" (5min) or "backfill"

    // Ensure sync_status rows exist for all modules
    for (const mod of modules) {
      await supabase.from("netatmo_sync_status").upsert({
        module_name: mod.name,
        device_id: mod.deviceId,
        module_id: mod.moduleId || "",
        module_type: mod.type,
        // Don't overwrite existing last_synced_ts
      }, { onConflict: "module_name", ignoreDuplicates: true });
    }

    const results: { module: string; inserted: number; status: string }[] = [];

    for (const mod of modules) {
      // Get sync status
      const { data: syncRow } = await supabase
        .from("netatmo_sync_status")
        .select("*")
        .eq("module_name", mod.name)
        .single();

      let dateBegin: number;
      let dateEnd: number;
      let scale: string;

      if (action === "backfill" || (syncRow && syncRow.last_synced_ts === 0)) {
        // Backfill: fetch from last synced point or 1 year ago
        const lastSynced = syncRow?.last_synced_ts || 0;
        dateBegin = lastSynced > 0 ? lastSynced : now - 365 * 86400;
        dateEnd = Math.min(dateBegin + BATCH_DAYS * 86400, now);
        scale = "5min";
      } else {
        // Regular collect: fetch last 10 minutes to catch new readings
        dateBegin = now - 600;
        dateEnd = now;
        scale = "5min";
      }

      // Only fetch data types relevant to this module
      const relevantTypes = mod.dataTypes.filter(t =>
        ["Temperature", "Humidity", "CO2", "Noise", "Pressure"].includes(t)
      );
      if (relevantTypes.length === 0) continue;

      try {
        const measures = await fetchMeasures(
          token,
          mod.deviceId,
          mod.moduleId,
          relevantTypes,
          dateBegin,
          dateEnd,
          scale
        );

        // Convert to rows
        const typeIdx: Record<string, number> = {};
        relevantTypes.forEach((t, i) => { typeIdx[t] = i; });

        const rows: any[] = [];
        for (const [ts, values] of Object.entries(measures)) {
          const timestamp = new Date(parseInt(ts, 10) * 1000).toISOString();
          rows.push({
            module_name: mod.name,
            timestamp,
            temperature: typeIdx.Temperature !== undefined ? values[typeIdx.Temperature] : null,
            humidity: typeIdx.Humidity !== undefined ? values[typeIdx.Humidity] : null,
            co2: typeIdx.CO2 !== undefined ? values[typeIdx.CO2] : null,
            noise: typeIdx.Noise !== undefined ? values[typeIdx.Noise] : null,
            pressure: typeIdx.Pressure !== undefined ? values[typeIdx.Pressure] : null,
          });
        }

        // Batch upsert (on conflict do nothing — keep existing data)
        if (rows.length > 0) {
          // Insert in chunks of 500
          for (let i = 0; i < rows.length; i += 500) {
            const chunk = rows.slice(i, i + 500);
            const { error } = await supabase
              .from("netatmo_readings")
              .upsert(chunk, { onConflict: "module_name,timestamp", ignoreDuplicates: true });
            if (error) console.error(`Insert error for ${mod.name}:`, error.message);
          }
        }

        // Update sync status
        const maxTs = rows.length > 0
          ? Math.max(...Object.keys(measures).map(Number))
          : dateEnd;

        const isComplete = dateEnd >= now;
        await supabase.from("netatmo_sync_status").update({
          last_synced_ts: maxTs,
          status: isComplete ? "synced" : "backfilling",
          updated_at: new Date().toISOString(),
        }).eq("module_name", mod.name);

        results.push({
          module: mod.name,
          inserted: rows.length,
          status: isComplete ? "synced" : "backfilling",
        });

        // Small delay between modules to respect rate limits
        await new Promise(r => setTimeout(r, 500));
      } catch (e) {
        console.error(`Error processing ${mod.name}:`, e);
        results.push({
          module: mod.name,
          inserted: 0,
          status: `error: ${e instanceof Error ? e.message : "unknown"}`,
        });
      }
    }

    // Check if any module still needs backfilling
    const { data: pendingModules } = await supabase
      .from("netatmo_sync_status")
      .select("module_name, status")
      .in("status", ["pending", "backfilling"]);

    return new Response(JSON.stringify({
      action,
      results,
      pendingBackfill: (pendingModules || []).length,
      modules: modules.map(m => m.name),
    }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e) {
    console.error("netatmo-collect error:", e);
    return new Response(
      JSON.stringify({ error: e instanceof Error ? e.message : "Unknown error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});
