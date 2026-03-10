import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-supabase-client-platform, x-supabase-client-platform-version, x-supabase-client-runtime, x-supabase-client-runtime-version",
};

const CONSO_API_BASE = "https://conso.boris.sh/api";

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  try {
    const token = Deno.env.get("LINKY_TOKEN");
    const prm = Deno.env.get("LINKY_PRM");
    if (!token || !prm) throw new Error("LINKY_TOKEN/PRM not configured");

    const body = await req.json().catch(() => ({}));
    const action = body.action || "load_curve";
    
    const now = new Date();
    const endDate = body.end || now.toISOString().slice(0, 10);
    const startDate = body.start || new Date(now.getTime() - 7 * 86400000).toISOString().slice(0, 10);

    let endpoint = "consumption_load_curve";
    if (action === "daily") endpoint = "daily_consumption";

    // For long ranges (>31 days of load_curve), the API may not support it.
    // We'll let the client decide which action to use.

    const params = new URLSearchParams({
      prm,
      start: startDate,
      end: endDate,
    });

    const resp = await fetch(`${CONSO_API_BASE}/${endpoint}?${params}`, {
      headers: {
        "Authorization": `Bearer ${token}`,
        "User-Agent": "heatsmart/1.0",
      },
    });

    if (resp.status === 401) {
      return new Response(JSON.stringify({ error: "Linky token expired. Re-authenticate at conso.boris.sh" }), {
        status: 401,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }
    if (resp.status === 429) {
      return new Response(JSON.stringify({ error: "Linky rate limited, try again later" }), {
        status: 429,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`Linky API error [${resp.status}]: ${text.substring(0, 200)}`);
    }

    const data = await resp.json();
    
    let intervals: any[] = [];
    if (data.meter_reading?.interval_reading) {
      intervals = data.meter_reading.interval_reading;
    } else if (data.interval_reading) {
      intervals = data.interval_reading;
    }

    const readings = intervals.map((iv: any) => ({
      timestamp: iv.date,
      // load_curve values are in Wh per 30min slot (divide by 2 to get average)
      // daily values are in Wh for the whole day
      wh: action === "load_curve" ? Math.round(parseFloat(iv.value) / 2 * 10) / 10 : parseFloat(iv.value),
    }));

    return new Response(JSON.stringify({ readings, raw_count: intervals.length, action }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e) {
    console.error("linky error:", e);
    return new Response(JSON.stringify({ error: e instanceof Error ? e.message : "Unknown error" }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
