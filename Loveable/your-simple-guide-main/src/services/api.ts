import { supabase } from "@/integrations/supabase/client";
import type { WeatherData, Zone, EnergyData, NetatmoModule } from "@/types/climate";
import { calcDewpoint } from "@/lib/dewpoint";

// ── Weather (Open-Meteo via edge function) ──────────────────────────────────

const WMO_CONDITIONS: Record<number, string> = {
  0: "sunny", 1: "sunny", 2: "partly-cloudy", 3: "cloudy",
  45: "cloudy", 48: "cloudy",
  51: "rainy", 53: "rainy", 55: "rainy",
  61: "rainy", 63: "rainy", 65: "rainy",
  71: "snowy", 73: "snowy", 75: "snowy", 77: "snowy",
  80: "rainy", 81: "rainy", 82: "rainy",
  85: "snowy", 86: "snowy",
};

export async function fetchWeather(lat = 44.07, lon = -1.26): Promise<WeatherData> {
  const { data, error } = await supabase.functions.invoke("weather", {
    body: { lat, lon },
  });
  if (error) throw new Error(`Weather fetch failed: ${error.message}`);

  const current = data.current;
  const hourly = data.hourly;
  const now = new Date();

  const forecast = [];
  for (let i = 0; i < hourly.time.length && forecast.length < 6; i++) {
    const t = new Date(hourly.time[i]);
    if (t <= now) continue;
    if (forecast.length > 0 && t.getTime() - new Date(forecast[forecast.length - 1].time).getTime() < 2.5 * 3600000) continue;
    forecast.push({
      time: t.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }),
      temperature: Math.round(hourly.temperature_2m[i] * 10) / 10,
      humidity: hourly.relative_humidity_2m[i],
      dewpoint: Math.round((hourly.dew_point_2m?.[i] ?? 0) * 10) / 10,
      condition: WMO_CONDITIONS[hourly.weather_code?.[i] ?? 3] || "cloudy",
    });
  }

  return {
    temperature: current.temperature_2m,
    humidity: current.relative_humidity_2m,
    dewpoint: current.dew_point_2m ?? current.temperature_2m - ((100 - current.relative_humidity_2m) / 5),
    condition: (WMO_CONDITIONS[current.weather_code ?? 3] || "cloudy") as WeatherData["condition"],
    windSpeed: Math.round(current.wind_speed_10m ?? 0),
    forecast,
  };
}

// ── Airzone (Cloud API via edge function) ────────────────────────────────────

const MODE_NAMES: Record<number, string> = {
  1: "stop", 2: "cool", 3: "heat", 4: "fan", 5: "dry", 7: "auto",
};

export async function fetchAirzoneZones(): Promise<Zone[]> {
  const { data, error } = await supabase.functions.invoke("airzone", {
    body: { action: "status" },
  });
  if (error) throw new Error(`Airzone fetch failed: ${error.message}`);
  if (data.error) throw new Error(data.error);

  return (data.zones || []).map((z: any) => {
    const name = z.name || z.device_name || z._device_id;
    const temp = z.local_temp?.celsius ?? z.local_temp ?? z.temperature ?? 0;
    const humidity = z.humidity ?? z.local_humidity ?? 0;
    const setpoint = z.setpoint_air_heat?.celsius ?? z.setpoint_air_heat ?? z.setpoint ?? 20;
    const power = z.power === true || z.power === 1;
    const mode = z.mode ?? 0;
    const dewpoint = calcDewpoint(temp, humidity);

    return {
      id: z._device_id || z.device_id || z.id || name,
      name,
      floor: z._installation_name || "",
      temperature: typeof temp === "number" ? Math.round(temp * 10) / 10 : 0,
      targetTemperature: typeof setpoint === "number" ? Math.round(setpoint * 10) / 10 : 20,
      humidity: typeof humidity === "number" ? Math.round(humidity) : 0,
      targetHumidity: 65,
      dewpoint,
      isHeating: power && mode === 3,
      heatpumpMode: (MODE_NAMES[mode] || "auto") as Zone["heatpumpMode"],
      lastUpdated: "now",
      _device_id: z._device_id,
      _installation_id: z._installation_id,
    };
  });
}

export async function setAirzoneZone(
  deviceId: string,
  installationId: string,
  params: Record<string, any>
): Promise<void> {
  const { data, error } = await supabase.functions.invoke("airzone", {
    body: { action: "set_zone", device_id: deviceId, installation_id: installationId, params },
  });
  if (error) throw new Error(`Airzone set failed: ${error.message}`);
  if (data.error) throw new Error(data.error);
}

// ── Linky (Conso API via edge function) ──────────────────────────────────────

const DEFAULT_STANDBY_WH_PER_SLOT = 55;

function formatLocalDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export async function fetchEnergyBaselines(): Promise<Record<number, number>> {
  const { data, error } = await supabase
    .from("energy_baseline")
    .select("hour_of_day, baseline_wh");
  if (error) {
    console.warn("Failed to fetch baselines, using defaults:", error.message);
    return {};
  }
  const map: Record<number, number> = {};
  for (const row of (data || [])) {
    map[row.hour_of_day] = Math.round(Number(row.baseline_wh) / 2);
  }
  return map;
}

/** Fetch hourly load curve data (for ranges up to ~7 days) */
export async function fetchLinkyData(days = 7, baselines?: Record<number, number>): Promise<EnergyData> {
  const now = new Date();
  const end = formatLocalDate(now);
  const start = formatLocalDate(new Date(now.getTime() - days * 86400000));

  const { data, error } = await supabase.functions.invoke("linky", {
    body: { action: "load_curve", start, end },
  });
  if (error) throw new Error(`Linky fetch failed: ${error.message}`);
  if (data.error) throw new Error(data.error);

  const readings: { timestamp: string; wh: number }[] = data.readings || [];

  const hourlyByDateMap = new Map<string, { totalWh: number; heatingWh: number; slots: number }>();
  const dailyMap = new Map<string, { totalWh: number; heatingWh: number }>();
  let totalWh = 0;
  let totalHeatingWh = 0;

  for (const r of readings) {
    const dt = new Date(r.timestamp);
    const dateKey = formatLocalDate(dt);
    const hour = dt.getHours();
    const hourStr = hour.toString().padStart(2, "0");
    const compositeKey = `${dateKey}|${hourStr}`;
    const slotBaseline = baselines?.[hour] ?? DEFAULT_STANDBY_WH_PER_SLOT;

    const entry = hourlyByDateMap.get(compositeKey) || { totalWh: 0, heatingWh: 0, slots: 0 };
    entry.totalWh += r.wh;
    entry.heatingWh += Math.max(0, r.wh - slotBaseline);
    entry.slots += 1;
    hourlyByDateMap.set(compositeKey, entry);

    const dayEntry = dailyMap.get(dateKey) || { totalWh: 0, heatingWh: 0 };
    dayEntry.totalWh += r.wh;
    dayEntry.heatingWh += Math.max(0, r.wh - slotBaseline);
    dailyMap.set(dateKey, dayEntry);

    totalWh += r.wh;
    totalHeatingWh += Math.max(0, r.wh - slotBaseline);
  }

  const sortedDates = Array.from(dailyMap.keys()).sort();
  const lastDate = sortedDates[sortedDates.length - 1] || "";

  const hourlyData = Array.from(hourlyByDateMap.entries())
    .filter(([key]) => key.startsWith(lastDate))
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, entry]) => ({
      hour: key.split("|")[1],
      consumption: Math.round(entry.totalWh / 10) / 100,
      heating: Math.round(entry.heatingWh / 10) / 100,
    }));

  const allHourlyData = Array.from(hourlyByDateMap.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, entry]) => {
      const [date, hour] = key.split("|");
      return {
        date,
        hour,
        consumption: Math.round(entry.totalWh / 10) / 100,
        heating: Math.round(entry.heatingWh / 10) / 100,
      };
    });

  const dailyData = Array.from(dailyMap.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, entry]) => {
      const [y, m, d] = date.split("-").map(Number);
      const localDate = new Date(y, m - 1, d);
      const label = localDate.toLocaleDateString("fr-FR", { weekday: "short", day: "numeric" });
      return {
        date,
        label,
        consumption: Math.round(entry.totalWh / 10) / 100,
        heating: Math.round(entry.heatingWh / 10) / 100,
      };
    });

  const totalKwh = Math.round(totalWh / 10) / 100;
  const heatingKwh = Math.round(totalHeatingWh / 10) / 100;

  return {
    currentPower: 0,
    dailyConsumption: totalKwh,
    heatingConsumption: heatingKwh,
    tariffRate: "peak",
    costToday: Math.round(totalKwh * 0.1927 * 100) / 100,
    hourlyData,
    dailyData,
    allHourlyData,
    totalDays: dailyMap.size,
  };
}

/** Fetch daily consumption data for long ranges (30d, 1y) */
export async function fetchLinkyDaily(days = 365, baselines?: Record<number, number>): Promise<EnergyData> {
  const now = new Date();
  const end = formatLocalDate(now);
  const start = formatLocalDate(new Date(now.getTime() - days * 86400000));

  const { data, error } = await supabase.functions.invoke("linky", {
    body: { action: "daily", start, end },
  });
  if (error) throw new Error(`Linky daily fetch failed: ${error.message}`);
  if (data.error) throw new Error(data.error);

  const readings: { timestamp: string; wh: number }[] = data.readings || [];

  // For daily data, wh is total Wh for the day
  // We estimate heating as total minus 24h of average baseline
  const avgBaselinePerHour = baselines && Object.keys(baselines).length > 0
    ? Object.values(baselines).reduce((s, v) => s + v, 0) / Object.keys(baselines).length * 2 // *2 because baselines are per-slot (30min)
    : DEFAULT_STANDBY_WH_PER_SLOT * 2; // per hour
  const dailyBaseline = avgBaselinePerHour * 24; // Wh per day standby

  let totalWh = 0;
  let totalHeatingWh = 0;

  const dailyData = readings.map((r) => {
    const dt = new Date(r.timestamp);
    const dateKey = formatLocalDate(dt);
    const [y, m, d] = dateKey.split("-").map(Number);
    const localDate = new Date(y, m - 1, d);
    const label = localDate.toLocaleDateString("fr-FR", { weekday: "short", day: "numeric" });
    const heatingWh = Math.max(0, r.wh - dailyBaseline);

    totalWh += r.wh;
    totalHeatingWh += heatingWh;

    return {
      date: dateKey,
      label,
      consumption: Math.round(r.wh / 10) / 100,
      heating: Math.round(heatingWh / 10) / 100,
    };
  }).sort((a, b) => a.date.localeCompare(b.date));

  const totalKwh = Math.round(totalWh / 10) / 100;
  const heatingKwh = Math.round(totalHeatingWh / 10) / 100;

  return {
    currentPower: 0,
    dailyConsumption: totalKwh,
    heatingConsumption: heatingKwh,
    tariffRate: "peak",
    costToday: Math.round(totalKwh * 0.1927 * 100) / 100,
    hourlyData: [],
    dailyData,
    totalDays: dailyData.length,
  };
}

// ── Netatmo (Weather Station via edge function) ──────────────────────────────

export async function fetchNetatmoData(): Promise<NetatmoModule[]> {
  const { data, error } = await supabase.functions.invoke("netatmo");
  if (error) throw new Error(`Netatmo fetch failed: ${error.message}`);
  if (data.error) throw new Error(data.error);
  return data.modules || [];
}
