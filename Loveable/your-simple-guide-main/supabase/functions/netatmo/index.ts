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
  const refreshToken = Deno.env.get("NETATMO_REFRESH_TOKEN")!;

  const resp = await fetch(`${NETATMO_API}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: refreshToken,
      client_id: clientId,
      client_secret: clientSecret,
    }),
  });

  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Netatmo token refresh failed [${resp.status}]: ${text}`);
  }

  const data = await resp.json();
  // Note: In production, you'd want to persist the new refresh_token
  // For now we use the access token from the initial setup
  return data.access_token;
}

async function getAccessToken(): Promise<string> {
  // Try using stored access token first, fall back to refresh
  const accessToken = Deno.env.get("NETATMO_ACCESS_TOKEN");
  if (accessToken) {
    // Test if token is still valid
    const test = await fetch(`${NETATMO_API}/api/getstationsdata`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/x-www-form-urlencoded",
      },
    });
    if (test.ok) return accessToken;
  }
  // Token expired, refresh it
  return await refreshToken();
}

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  try {
    const accessToken = await getAccessToken();

    const resp = await fetch(`${NETATMO_API}/api/getstationsdata`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/x-www-form-urlencoded",
      },
    });

    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`Netatmo API error [${resp.status}]: ${text}`);
    }

    const data = await resp.json();

    // Extract station and module data
    const devices = data.body?.devices || [];
    const result: any[] = [];

    for (const device of devices) {
      // Main station (indoor)
      result.push({
        id: device._id,
        name: device.module_name || device.station_name || "Station",
        type: device.type,
        temperature: device.dashboard_data?.Temperature,
        humidity: device.dashboard_data?.Humidity,
        co2: device.dashboard_data?.CO2,
        noise: device.dashboard_data?.Noise,
        pressure: device.dashboard_data?.Pressure,
        lastSeen: device.last_status_store,
      });

      // Sub-modules (outdoor, extra indoor, rain, wind)
      for (const mod of device.modules || []) {
        result.push({
          id: mod._id,
          name: mod.module_name || mod.type,
          type: mod.type,
          temperature: mod.dashboard_data?.Temperature,
          humidity: mod.dashboard_data?.Humidity,
          co2: mod.dashboard_data?.CO2,
          rain: mod.dashboard_data?.Rain,
          windStrength: mod.dashboard_data?.WindStrength,
          windAngle: mod.dashboard_data?.WindAngle,
          lastSeen: mod.last_seen,
        });
      }
    }

    return new Response(JSON.stringify({ modules: result }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e) {
    console.error("Netatmo error:", e);
    return new Response(
      JSON.stringify({ error: e instanceof Error ? e.message : "Unknown error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});
