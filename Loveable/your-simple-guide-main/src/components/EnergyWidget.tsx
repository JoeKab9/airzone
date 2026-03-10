import { EnergyData } from "@/types/climate";
import { Card } from "@/components/ui/card";
import { Zap, TrendingDown, Clock } from "lucide-react";
import { BarChart, Bar, XAxis, YAxis, ResponsiveContainer, Tooltip, Legend } from "recharts";

interface EnergyWidgetProps {
  data: EnergyData;
}

const EnergyWidget = ({ data }: EnergyWidgetProps) => {
  const heatingPercent = data.dailyConsumption > 0
    ? Math.round((data.heatingConsumption / data.dailyConsumption) * 100)
    : 0;

  // Use daily data (3 days) for chart
  const chartData = data.dailyData.map((d) => ({
    ...d,
    standby: Math.round((d.consumption - d.heating) * 100) / 100,
  }));

  return (
    <Card className="glass-card p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-semibold text-foreground text-sm">Energy · Last {data.totalDays} days</h3>
        <div className="flex items-center gap-1 rounded-full px-2.5 py-1 bg-muted">
          <Clock className="h-3 w-3 text-muted-foreground" />
          <span className="text-xs font-medium text-muted-foreground">
            Base · €0.1927/kWh
          </span>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 mb-4">
        <div>
          <div className="flex items-center gap-1 mb-1">
            <Zap className="h-3 w-3 text-muted-foreground" />
            <span className="text-[10px] text-muted-foreground uppercase tracking-wider">Total ({data.totalDays}d)</span>
          </div>
          <span className="metric-value text-lg text-foreground">{data.dailyConsumption} kWh</span>
        </div>
        <div>
          <span className="text-[10px] text-muted-foreground uppercase tracking-wider block mb-1">Cost</span>
          <span className="metric-value text-lg text-foreground">€{data.costToday.toFixed(2)}</span>
        </div>
      </div>

      {/* Heating share bar */}
      <div className="mb-4">
        <div className="flex justify-between text-[10px] text-muted-foreground mb-1">
          <span>Heating: {data.heatingConsumption} kWh</span>
          <span>{heatingPercent}% of total</span>
        </div>
        <div className="h-2 rounded-full bg-secondary overflow-hidden">
          <div
            className="h-full rounded-full bg-gradient-to-r from-heating to-heating-glow transition-all duration-500"
            style={{ width: `${heatingPercent}%` }}
          />
        </div>
      </div>

      {/* 3-day chart */}
      <div className="h-32">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={chartData} barGap={1}>
            <XAxis
              dataKey="label"
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 10, fill: "hsl(215 12% 55%)" }}
            />
            <YAxis hide />
            <Tooltip
              contentStyle={{
                background: "hsl(220 18% 13%)",
                border: "1px solid hsl(220 14% 22%)",
                borderRadius: "8px",
                fontSize: "11px",
                color: "hsl(210 20% 92%)",
              }}
              formatter={(value: number, name: string) => {
                const labels: Record<string, string> = {
                  standby: "⚡ Standby/Other",
                  heating: "🔥 Heating (est.)",
                };
                return [`${value} kWh`, labels[name] || name];
              }}
            />
            <Bar dataKey="standby" stackId="a" fill="hsl(265 70% 60%)" fillOpacity={0.4} radius={[0, 0, 0, 0]} />
            <Bar dataKey="heating" stackId="a" fill="hsl(32 95% 55%)" radius={[2, 2, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
};

export default EnergyWidget;
