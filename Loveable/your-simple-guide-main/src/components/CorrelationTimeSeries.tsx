import { Card } from "@/components/ui/card";
import {
  Line, XAxis, YAxis, ResponsiveContainer, Tooltip,
  CartesianGrid, ComposedChart, ReferenceArea,
} from "recharts";
import { format } from "date-fns";

export interface TimeSeriesPoint {
  ts: number;
  indoor: number;
  outdoor: number;
  delta: number;
  isHeating?: boolean;
}

interface CorrelationTimeSeriesProps {
  title: string;
  data: TimeSeriesPoint[];
  indoorLabel: string;
  outdoorLabel: string;
  indoorColor?: string;
  outdoorColor?: string;
  unit?: string;
  height?: number;
  insight?: React.ReactNode;
}

const CorrelationTimeSeries = ({
  title,
  data,
  indoorLabel,
  outdoorLabel,
  indoorColor = "hsl(var(--primary))",
  outdoorColor = "hsl(210, 70%, 55%)",
  unit = "°C",
  height = 200,
  insight,
}: CorrelationTimeSeriesProps) => {
  if (data.length < 3) {
    return (
      <Card className="glass-card p-4">
        <h4 className="text-xs font-semibold text-foreground mb-2">{title}</h4>
        <div className="flex items-center justify-center" style={{ height }}>
          <p className="text-[10px] text-muted-foreground">Not enough data points yet.</p>
        </div>
      </Card>
    );
  }

  // Build heating ranges for orange shading
  const heatingRanges: { start: number; end: number }[] = [];
  let rangeOpen: number | null = null;
  for (const d of data) {
    if (d.isHeating && rangeOpen === null) rangeOpen = d.ts;
    if (!d.isHeating && rangeOpen !== null) {
      heatingRanges.push({ start: rangeOpen, end: d.ts });
      rangeOpen = null;
    }
  }
  if (rangeOpen !== null) heatingRanges.push({ start: rangeOpen, end: data[data.length - 1].ts });

  // Compute avg delta
  const avgDelta = data.reduce((s, d) => s + d.delta, 0) / data.length;

  return (
    <Card className="glass-card p-4">
      <div className="flex items-center justify-between mb-1">
        <h4 className="text-xs font-semibold text-foreground">{title}</h4>
        <span className="text-[10px] text-muted-foreground">
          avg Δ: <span className={avgDelta > 0 ? "text-warning" : "text-success"}>{avgDelta > 0 ? "+" : ""}{avgDelta.toFixed(1)}{unit}</span>
        </span>
      </div>
      {insight && <p className="text-[10px] text-muted-foreground mb-2">{insight}</p>}
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.3} />
          <XAxis
            dataKey="ts" type="number" scale="time"
            domain={["dataMin", "dataMax"]}
            tickCount={6}
            tick={{ fontSize: 9, fill: "hsl(var(--muted-foreground))" }}
            axisLine={false} tickLine={false}
            tickFormatter={(v) => {
              try { return format(new Date(v), "dd/MM HH:mm"); } catch { return ""; }
            }}
          />
          <YAxis
            tick={{ fontSize: 9, fill: "hsl(var(--muted-foreground))" }}
            axisLine={false} tickLine={false}
            width={32}
            domain={["auto", "auto"]}
          />
          <Tooltip
            labelFormatter={(v) => {
              try { return format(new Date(Number(v)), "dd/MM HH:mm"); } catch { return String(v); }
            }}
            contentStyle={{
              background: "hsl(var(--card))",
              border: "1px solid hsl(var(--border))",
              borderRadius: "8px",
              fontSize: "11px",
            }}
            formatter={(value: number, name: string) => {
              const label = name === "indoor" ? indoorLabel : name === "outdoor" ? outdoorLabel : "Δ";
              return [`${value.toFixed(1)}${unit}`, label];
            }}
          />
          {heatingRanges.map((r, i) => (
            <ReferenceArea
              key={i} x1={r.start} x2={r.end}
              fill="hsl(var(--heating))" fillOpacity={0.15}
            />
          ))}
          <Line
            type="monotone" dataKey="outdoor" stroke={outdoorColor}
            strokeWidth={1.5} dot={false} name="outdoor"
          />
          <Line
            type="monotone" dataKey="indoor" stroke={indoorColor}
            strokeWidth={2} dot={false} name="indoor"
          />
        </ComposedChart>
      </ResponsiveContainer>
      <div className="flex gap-3 mt-1 text-[9px] text-muted-foreground">
        <span className="flex items-center gap-1">
          <div className="h-0.5 w-4" style={{ borderTop: `2px solid ${indoorColor}` }} />
          {indoorLabel}
        </span>
        <span className="flex items-center gap-1">
          <div className="h-0.5 w-4" style={{ borderTop: `1.5px solid ${outdoorColor}` }} />
          {outdoorLabel}
        </span>
        {heatingRanges.length > 0 && (
          <span className="flex items-center gap-1">
            <div className="h-3 w-4 rounded-sm" style={{ backgroundColor: "hsla(var(--heating) / 0.15)" }} />
            Heating
          </span>
        )}
      </div>
    </Card>
  );
};

export default CorrelationTimeSeries;
