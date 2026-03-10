import { Card } from "@/components/ui/card";
import { useRecentControlLogs } from "@/hooks/useControlData";
import {
  ScatterChart, Scatter, XAxis, YAxis, ResponsiveContainer, Tooltip,
  CartesianGrid, Cell, ReferenceLine,
} from "recharts";
import { CloudSun } from "lucide-react";

const OutdoorCorrelationChart = () => {
  const { data: logs, isLoading } = useRecentControlLogs(500);

  // Build scatter: outdoor AH vs indoor DP spread
  const calcAH = (temp: number, rh: number) => {
    const es = 6.112 * Math.exp((17.67 * temp) / (temp + 243.5));
    return (es * rh * 2.1674) / (273.15 + temp);
  };

  const points = (logs || [])
    .filter((l) => l.outdoor_temp != null && l.outdoor_humidity != null && l.dp_spread != null && l.temperature != null)
    .map((l) => {
      const outdoorAH = calcAH(l.outdoor_temp!, l.outdoor_humidity!);
      const indoorHum = l.humidity_netatmo ?? l.humidity_airzone ?? 0;
      const indoorAH = calcAH(l.temperature!, indoorHum);
      return {
        outdoorAH: Math.round(outdoorAH * 10) / 10,
        indoorAH: Math.round(indoorAH * 10) / 10,
        dpSpread: l.dp_spread!,
        outdoorTemp: l.outdoor_temp!,
        zone: l.zone_name,
        isHeating: l.action === "heating_on",
        ahDiff: Math.round((indoorAH - outdoorAH) * 10) / 10,
      };
    });

  // Deduplicate: sample every 3rd point for readability
  const sampled = points.filter((_, i) => i % 3 === 0);

  // Correlation insight
  const dryOutdoor = sampled.filter((p) => p.ahDiff > 2); // indoor wetter than outdoor
  const wetOutdoor = sampled.filter((p) => p.ahDiff <= 0); // outdoor wetter
  const avgSpreadDry = dryOutdoor.length > 0 ? dryOutdoor.reduce((s, p) => s + p.dpSpread, 0) / dryOutdoor.length : null;
  const avgSpreadWet = wetOutdoor.length > 0 ? wetOutdoor.reduce((s, p) => s + p.dpSpread, 0) / wetOutdoor.length : null;

  return (
    <Card className="glass-card p-5">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <CloudSun className="h-4 w-4 text-primary" />
          <h3 className="font-semibold text-foreground text-sm">Outdoor → Indoor Correlation</h3>
        </div>
      </div>

      {/* Insight */}
      <div className="flex gap-4 mb-3 text-[10px]">
        {avgSpreadDry != null && (
          <div>
            <span className="text-muted-foreground">When outdoor drier: avg spread </span>
            <span className="text-success font-medium">{avgSpreadDry.toFixed(1)}°</span>
          </div>
        )}
        {avgSpreadWet != null && (
          <div>
            <span className="text-muted-foreground">When outdoor wetter: avg spread </span>
            <span className="text-warning font-medium">{avgSpreadWet.toFixed(1)}°</span>
          </div>
        )}
      </div>

      {sampled.length > 5 ? (
        <ResponsiveContainer width="100%" height={220}>
          <ScatterChart margin={{ top: 5, right: 5, bottom: 20, left: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.3} />
            <XAxis
              dataKey="ahDiff" type="number" name="AH Diff"
              label={{ value: "Indoor−Outdoor AH (g/m³)", position: "bottom", offset: 5, fontSize: 9, fill: "hsl(var(--muted-foreground))" }}
              tick={{ fontSize: 9 }} stroke="hsl(var(--muted-foreground))"
            />
            <YAxis
              dataKey="dpSpread" type="number" name="DP Spread"
              label={{ value: "DP Spread °C", angle: -90, position: "insideLeft", fontSize: 9, fill: "hsl(var(--muted-foreground))" }}
              tick={{ fontSize: 9 }} stroke="hsl(var(--muted-foreground))"
            />
            <ReferenceLine y={4} stroke="hsl(var(--destructive))" strokeDasharray="4 4" opacity={0.5} />
            <ReferenceLine y={6} stroke="hsl(var(--success))" strokeDasharray="4 4" opacity={0.5} />
            <ReferenceLine x={0} stroke="hsl(var(--muted-foreground))" strokeDasharray="2 2" opacity={0.3} />
            <Tooltip
              content={({ payload }) => {
                if (!payload?.[0]) return null;
                const d = payload[0].payload;
                return (
                  <div className="bg-card border border-border rounded p-2 text-xs shadow-lg">
                    <p>ΔAH: {d.ahDiff > 0 ? "+" : ""}{d.ahDiff} g/m³ · DP spread: {d.dpSpread}°</p>
                    <p className="text-muted-foreground">{d.zone} · outdoor {d.outdoorTemp}°C · {d.isHeating ? "🔥 heating" : "idle"}</p>
                  </div>
                );
              }}
            />
            <Scatter data={sampled}>
              {sampled.map((entry, i) => (
                <Cell
                  key={i}
                  fill={entry.dpSpread < 4 ? "hsl(var(--destructive))" : entry.dpSpread < 6 ? "hsl(var(--warning))" : "hsl(var(--success))"}
                  opacity={entry.isHeating ? 0.9 : 0.4}
                  r={entry.isHeating ? 4 : 2.5}
                />
              ))}
            </Scatter>
          </ScatterChart>
        </ResponsiveContainer>
      ) : (
        <div className="h-[220px] flex items-center justify-center">
          <p className="text-xs text-muted-foreground">
            {isLoading ? "Loading…" : "Need more data points to show correlation."}
          </p>
        </div>
      )}

      <div className="flex gap-3 mt-2 text-[9px] text-muted-foreground">
        <span>● <span className="text-destructive">Red</span> = spread &lt;4° (risk)</span>
        <span>● <span className="text-warning">Amber</span> = 4-6°</span>
        <span>● <span className="text-success">Green</span> = &gt;6° (safe)</span>
        <span>● Larger dots = heating active</span>
      </div>
    </Card>
  );
};

export default OutdoorCorrelationChart;
