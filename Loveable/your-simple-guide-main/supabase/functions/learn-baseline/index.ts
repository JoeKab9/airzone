import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

/**
 * learn-baseline: Analyzes Linky consumption data during confirmed non-heating hours
 * to learn the true standby baseline per hour-of-day.
 * 
 * Called periodically (e.g. daily via cron or manually).
 * 
 * Logic:
 * 1. Fetch last 7 days of Linky load curve data
 * 2. Fetch control_log to identify hours when heating was active
 * 3. For hours with NO heating, compute average consumption = baseline
 * 4. Update energy_baseline table with exponential moving average
 */
serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  try {
    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const supabase = createClient(supabaseUrl, serviceKey);

    const linkyToken = Deno.env.get("LINKY_TOKEN");
    const linkyPrm = Deno.env.get("LINKY_PRM");
    if (!linkyToken || !linkyPrm) throw new Error("LINKY_TOKEN/PRM not configured");

    // Fetch 7 days of Linky load curve
    const now = new Date();
    const end = formatDate(now);
    const start = formatDate(new Date(now.getTime() - 7 * 86400000));

    const linkyResp = await fetch(
      `https://conso.boris.sh/api/consumption_load_curve?prm=${linkyPrm}&start=${start}&end=${end}`,
      {
        headers: {
          Authorization: `Bearer ${linkyToken}`,
          "User-Agent": "heatsmart/1.0",
        },
      }
    );

    if (!linkyResp.ok) {
      throw new Error(`Linky API error [${linkyResp.status}]`);
    }

    const linkyData = await linkyResp.json();
    const intervals = linkyData.meter_reading?.interval_reading || linkyData.interval_reading || [];

    // Parse Linky readings into hourly buckets
    // Each reading is 30min, value in Wh
    const hourlyConsumption = new Map<string, { totalWh: number; slots: number }>();

    for (const iv of intervals) {
      const dt = new Date(iv.date);
      const dateKey = formatDate(dt);
      const hour = dt.getHours();
      const key = `${dateKey}|${hour}`;
      const wh = Math.round(parseFloat(iv.value) / 2 * 10) / 10; // 30-min value

      const entry = hourlyConsumption.get(key) || { totalWh: 0, slots: 0 };
      entry.totalWh += wh;
      entry.slots += 1;
      hourlyConsumption.set(key, entry);
    }

    // Fetch control_log for the same period to identify heating hours
    const { data: logs } = await supabase
      .from("control_log")
      .select("created_at, action, reason")
      .gte("created_at", `${start}T00:00:00Z`)
      .order("created_at", { ascending: true });

    const heatingHours = new Set<string>();
    for (const log of (logs || [])) {
      const isHeating = log.action === "heating_on" ||
        (log.action === "no_change" && log.reason && /heating\s*\(band/i.test(log.reason));
      if (isHeating) {
        const dt = new Date(log.created_at);
        const dateKey = formatDate(dt);
        heatingHours.add(`${dateKey}|${dt.getHours()}`);
      }
    }

    // Group non-heating consumption by hour-of-day
    const hourBaselines = new Map<number, { totalWh: number; count: number }>();

    for (const [key, entry] of hourlyConsumption.entries()) {
      if (heatingHours.has(key)) continue; // Skip heating hours
      if (entry.slots < 2) continue; // Need at least 2 slots (1 hour)

      const hour = parseInt(key.split("|")[1]);
      const existing = hourBaselines.get(hour) || { totalWh: 0, count: 0 };
      existing.totalWh += entry.totalWh;
      existing.count += 1;
      hourBaselines.set(hour, existing);
    }

    // Update energy_baseline table with exponential moving average
    const ALPHA = 0.3; // EMA weight for new data
    const updates: { hour: number; newBaseline: number; samples: number }[] = [];

    // Get current baselines
    const { data: currentBaselines } = await supabase
      .from("energy_baseline")
      .select("*");

    const currentMap = new Map<number, any>();
    for (const b of (currentBaselines || [])) {
      currentMap.set(b.hour_of_day, b);
    }

    for (const [hour, data] of hourBaselines.entries()) {
      const avgWh = Math.round((data.totalWh / data.count) * 10) / 10;
      const current = currentMap.get(hour);

      let newBaseline: number;
      if (!current || current.sample_count === 0) {
        // First real data point — use directly
        newBaseline = avgWh;
      } else {
        // EMA: new = alpha * observed + (1-alpha) * old
        newBaseline = Math.round((ALPHA * avgWh + (1 - ALPHA) * Number(current.baseline_wh)) * 10) / 10;
      }

      const newSampleCount = (current?.sample_count || 0) + data.count;

      await supabase
        .from("energy_baseline")
        .upsert({
          hour_of_day: hour,
          baseline_wh: newBaseline,
          sample_count: newSampleCount,
          last_updated: new Date().toISOString(),
          notes: `EMA updated from ${data.count} samples, avg=${avgWh}Wh`,
        }, { onConflict: "hour_of_day" });

      updates.push({ hour, newBaseline, samples: data.count });
    }

    return new Response(
      JSON.stringify({
        success: true,
        linky_readings: intervals.length,
        heating_hours_excluded: heatingHours.size,
        hours_updated: updates.length,
        updates,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  } catch (e) {
    console.error("learn-baseline error:", e);
    return new Response(
      JSON.stringify({ error: e instanceof Error ? e.message : "Unknown error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});

function formatDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
