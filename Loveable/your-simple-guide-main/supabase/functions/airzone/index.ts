import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-supabase-client-platform, x-supabase-client-platform-version, x-supabase-client-runtime, x-supabase-client-runtime-version",
};

const CLOUD_BASE = "https://m.airzonecloud.com";

// In-memory token cache (survives across invocations within same instance)
let cachedToken = "";
let cachedRefreshToken = "";
let tokenExpiry = 0;

async function login(email: string, password: string): Promise<string> {
  const resp = await fetch(`${CLOUD_BASE}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (resp.status === 401) throw new Error("Airzone login failed: check credentials");
  if (!resp.ok) throw new Error(`Airzone login error: ${resp.status}`);
  
  const data = await resp.json();
  cachedToken = data.token || "";
  cachedRefreshToken = data.refreshToken || "";
  tokenExpiry = Date.now() + 12 * 3600 * 1000; // 12h
  return cachedToken;
}

async function refreshToken(): Promise<string> {
  const resp = await fetch(`${CLOUD_BASE}/api/v1/auth/refreshToken`, {
    method: "POST",
    headers: { 
      "Content-Type": "application/json",
      "Authorization": `Bearer ${cachedToken}`,
    },
    body: JSON.stringify({ token: cachedToken, refreshToken: cachedRefreshToken }),
  });
  if (!resp.ok) throw new Error("Token refresh failed");
  const data = await resp.json();
  cachedToken = data.token || "";
  cachedRefreshToken = data.refreshToken || "";
  tokenExpiry = Date.now() + 12 * 3600 * 1000;
  return cachedToken;
}

async function ensureToken(email: string, password: string): Promise<string> {
  if (cachedToken && Date.now() < tokenExpiry) return cachedToken;
  if (cachedRefreshToken) {
    try { return await refreshToken(); } catch { /* fall through */ }
  }
  return await login(email, password);
}

async function apiGet(token: string, path: string, params?: Record<string, string>) {
  const url = new URL(`${CLOUD_BASE}${path}`);
  if (params) Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  
  const resp = await fetch(url.toString(), {
    headers: { 
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
    },
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Airzone API ${path} failed [${resp.status}]: ${text.substring(0, 200)}`);
  }
  return resp.json();
}

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  try {
    const email = Deno.env.get("AIRZONE_EMAIL");
    const password = Deno.env.get("AIRZONE_PASSWORD");
    if (!email || !password) throw new Error("AIRZONE_EMAIL/PASSWORD not configured");

    const body = await req.json().catch(() => ({}));
    const action = body.action || "status";

    const token = await ensureToken(email, password);

    if (action === "status") {
      // Get all installations → groups → devices → status
      const instData = await apiGet(token, "/api/v1/installations");
      const installations = Array.isArray(instData) ? instData : (instData.installations || []);
      
      const zones = [];
      const dhwDevices = [];
      for (const inst of installations) {
        const instId = inst.installation_id || inst.id || "";
        const detail = await apiGet(token, `/api/v1/installations/${instId}`);
        const instDetail = detail.installation || detail;
        
        for (const group of (instDetail.groups || [])) {
          for (const device of (group.devices || [])) {
            const devId = device.device_id || device.id || "";
            try {
              const status = await apiGet(token, `/api/v1/devices/${encodeURIComponent(devId)}/status`, {
                installation_id: instId,
              });
              const fullDevice = {
                ...device,
                ...status,
                _installation_id: instId,
                _installation_name: inst.name || "",
                _device_id: devId,
              };
              if (device.type === "az_zone") {
                zones.push(fullDevice);
              } else if (device.type === "az_acs" || device.type === "az_dhw" || 
                         (device.name && /acs|dhw|hot.?water|eau.?chaude/i.test(device.name))) {
                dhwDevices.push(fullDevice);
              }
            } catch (e) {
              console.error(`Failed to get status for ${devId}:`, e);
              if (device.type === "az_zone") {
                zones.push({ ...device, _device_id: devId, _installation_id: instId, _error: String(e) });
              }
            }
          }
        }
      }

      return new Response(JSON.stringify({ zones, dhw: dhwDevices }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    if (action === "set_zone") {
      const { device_id, installation_id, params: zoneParams } = body;
      if (!device_id || !zoneParams) throw new Error("device_id and params required");
      
      const url = `${CLOUD_BASE}/api/v1/devices/${encodeURIComponent(device_id)}`;
      for (const [paramName, value] of Object.entries(zoneParams)) {
        const resp = await fetch(url, {
          method: "PATCH",
          headers: {
            "Authorization": `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            param: paramName,
            value,
            installation_id: installation_id || "",
          }),
        });
        if (!resp.ok) {
          const text = await resp.text();
          throw new Error(`Set zone failed [${resp.status}]: ${text.substring(0, 200)}`);
        }
      }
      return new Response(JSON.stringify({ success: true }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    return new Response(JSON.stringify({ error: "Unknown action" }), {
      status: 400,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e) {
    console.error("airzone error:", e);
    return new Response(JSON.stringify({ error: e instanceof Error ? e.message : "Unknown error" }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
