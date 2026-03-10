import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-supabase-client-platform, x-supabase-client-platform-version, x-supabase-client-runtime, x-supabase-client-runtime-version",
};

const OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast";

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  try {
    const { lat = 44.07, lon = -1.26 } = await req.json().catch(() => ({}));

    const params = new URLSearchParams({
      latitude: String(lat),
      longitude: String(lon),
      hourly: "temperature_2m,dew_point_2m,relative_humidity_2m,weather_code,wind_speed_10m",
      current: "temperature_2m,relative_humidity_2m,dew_point_2m,weather_code,wind_speed_10m",
      timezone: "Europe/Paris",
      forecast_days: "2",
    });

    const resp = await fetch(`${OPEN_METEO_URL}?${params}`, { 
      headers: { "User-Agent": "HeatSmart/1.0" }
    });
    
    if (!resp.ok) {
      const text = await resp.text();
      console.error("Open-Meteo error:", resp.status, text);
      return new Response(JSON.stringify({ error: "Weather API error" }), {
        status: 500,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    const data = await resp.json();
    return new Response(JSON.stringify(data), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e) {
    console.error("weather error:", e);
    return new Response(JSON.stringify({ error: e instanceof Error ? e.message : "Unknown error" }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
