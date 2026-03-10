import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.49.1";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-supabase-client-platform, x-supabase-client-platform-version, x-supabase-client-runtime, x-supabase-client-runtime-version",
};

const CLOUD_BASE = "https://m.airzonecloud.com";

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

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  const supabase = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);

  try {
    const { action } = await req.json(); // "stop" or "resume"

    if (action === "stop") {
      // 1. Turn off all zones
      const token = await airzoneLogin();
      const instData = await airzoneGet(token, "/api/v1/installations");
      const installations = Array.isArray(instData) ? instData : (instData.installations || []);
      const results: string[] = [];

      for (const inst of installations) {
        const instId = inst.installation_id || inst.id || inst._id || "";
        const detail = await airzoneGet(token, `/api/v1/installations/${instId}`);
        const instDetail = detail.installation || detail;
        for (const group of (instDetail.groups || [])) {
          for (const device of (group.devices || [])) {
            if (device.type !== "az_zone") continue;
            const devId = device.device_id || device.id || "";
            try {
              await setAirzoneParam(token, devId, instId, "power", false);
              results.push(`${device.name}: OFF`);
            } catch (e) {
              results.push(`${device.name}: FAILED - ${e instanceof Error ? e.message : "?"}`);
            }
          }
        }
      }

      // 2. Set system_state to paused
      await supabase.from("system_state").upsert({
        key: "emergency_stop",
        value: { active: true, timestamp: new Date().toISOString(), reason: "Manual emergency stop" },
        updated_at: new Date().toISOString(),
      });

      // 3. Log it
      await supabase.from("control_log").insert({
        zone_name: "ALL",
        action: "emergency_stop",
        reason: "Manual emergency stop — all heating turned off. Automation paused.",
        success: true,
      });

      return new Response(JSON.stringify({ ok: true, action: "stop", results }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    } else if (action === "resume") {
      // Clear emergency stop
      await supabase.from("system_state").upsert({
        key: "emergency_stop",
        value: { active: false, timestamp: new Date().toISOString() },
        updated_at: new Date().toISOString(),
      });

      await supabase.from("control_log").insert({
        zone_name: "ALL",
        action: "emergency_resume",
        reason: "Automation resumed after emergency stop.",
        success: true,
      });

      return new Response(JSON.stringify({ ok: true, action: "resume" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    return new Response(JSON.stringify({ error: "Invalid action. Use 'stop' or 'resume'." }), {
      status: 400,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e) {
    console.error("Emergency stop error:", e);
    return new Response(JSON.stringify({ error: e instanceof Error ? e.message : "Unknown" }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
