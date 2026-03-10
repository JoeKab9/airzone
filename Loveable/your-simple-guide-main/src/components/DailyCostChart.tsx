import { Card } from "@/components/ui/card";
import { useLinkyData } from "@/hooks/useClimateData";
import {
  Bar, XAxis, YAxis, ResponsiveContainer, Tooltip, ComposedChart, CartesianGrid,
} from "recharts";
import { Euro } from "lucide-react";

const STANDBY_WH_PER_SLOT = 55;
const TARIFF = 0.1927;

function formatLocalDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

const DailyCostChart = () => {
  const { data: linky, isLoading } = useLinkyData();

  const allHourly = linky?.allHourlyData || [];

  // Show last 3 days (yesterday, Sunday, today if available)
  const now = new Date();
  const cutoff = formatLocalDate(new Date(now.getTime() - 3 * 86400000));

  const filtered = allHourly.filter((h) => h.date >= cutoff);

  const chartData = filtered.map((h) => {
    const [, m, d] = h.date.split("-");
    return {
      label: `${d}/${m} ${h.hour}h`,
      standby: Math.round((h.consumption - h.heating) * 100) / 100,
      heating: h.heating,
      total: h.consumption,
    };
  });

  // Summary
  const totalKwh = filtered.reduce((s, h) => s + h.consumption, 0);
  const heatingKwh = filtered.reduce((s, h) => s + h.heating, 0);
  const cost = Math.round(totalKwh * TARIFF * 100) / 100;

  return (
    <Card className="glass-card p-4">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <Euro className="h-3.5 w-3.5 text-primary" />
          <h3 className="font-semibold text-foreground text-xs">Energy (3 days)</h3>
        </div>
        <div className="flex gap-3 text-[9px] text-muted-foreground">
          <span>{totalKwh.toFixed(1)} kWh</span>
          <span className="text-heating">{heatingKwh.toFixed(1)} heating</span>
          <span>€{cost.toFixed(2)}</span>
        </div>
      </div>

      {chartData.length > 0 ? (
        <ResponsiveContainer width="100%" height={120}>
          <ComposedChart data={chartData} margin={{ top: 2, right: 0, bottom: 0, left: -20 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(220 14% 18%)" />
            <XAxis
              dataKey="label"
              tick={{ fontSize: 7, fill: "hsl(215 12% 55%)" }}
              axisLine={false}
              tickLine={false}
              interval={11}
              angle={-45}
              textAnchor="end"
              height={35}
            />
            <YAxis
              tick={{ fontSize: 8, fill: "hsl(215 12% 55%)" }}
              axisLine={false}
              tickLine={false}
              width={30}
            />
            <Tooltip
              contentStyle={{
                background: "hsl(220 18% 13%)",
                border: "1px solid hsl(220 14% 22%)",
                borderRadius: "8px",
                fontSize: "10px",
                color: "hsl(210 20% 92%)",
              }}
              formatter={(value: number, name: string) => {
                const labels: Record<string, string> = { heating: "🔥 Heating", standby: "⚡ Other" };
                return [`${value} kWh`, labels[name] || name];
              }}
            />
            <Bar dataKey="standby" stackId="a" fill="hsl(220 14% 40%)" fillOpacity={0.6} />
            <Bar dataKey="heating" stackId="a" fill="hsl(32 95% 55%)" fillOpacity={0.85} radius={[1, 1, 0, 0]} />
          </ComposedChart>
        </ResponsiveContainer>
      ) : (
        <div className="h-[120px] flex items-center justify-center">
          <p className="text-[10px] text-muted-foreground">
            {isLoading ? "Loading…" : "No Linky data available"}
          </p>
        </div>
      )}
    </Card>
  );
};

export default DailyCostChart;