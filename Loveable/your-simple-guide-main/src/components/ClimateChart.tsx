import { ClimateHistoryPoint } from "@/types/climate";
import { Card } from "@/components/ui/card";
import {
  Line,
  XAxis,
  YAxis,
  ResponsiveContainer,
  Tooltip,
  ReferenceLine,
  Area,
  ComposedChart,
  ReferenceArea,
  CartesianGrid,
} from "recharts";
import { format, parseISO } from "date-fns";
import { TimeRange } from "@/hooks/useControlData";

interface ClimateChartProps {
  data: ClimateHistoryPoint[];
  zoneName: string;
  targetTemp: number;
  timeRange?: TimeRange;
}

const RANGE_MS: Record<TimeRange, number> = {
  "1h": 60 * 60 * 1000,
  "24h": 24 * 60 * 60 * 1000,
  "3d": 3 * 24 * 60 * 60 * 1000,
  "7d": 7 * 24 * 60 * 60 * 1000,
  "30d": 30 * 24 * 60 * 60 * 1000,
};

const RANGE_LABELS: Record<TimeRange, string> = {
  "1h": "Last Hour",
  "24h": "24h History",
  "3d": "3-Day History",
  "7d": "7-Day History",
  "30d": "30-Day History",
};

function formatTick(ts: number, range: TimeRange) {
  const d = new Date(ts);
  switch (range) {
    case "1h":
    case "24h": return format(d, "HH:mm");
    case "3d": return format(d, "dd/MM HH:mm");
    case "7d": return format(d, "EEE dd");
    case "30d": return format(d, "dd/MM");
  }
}

function getTickCount(range: TimeRange) {
  switch (range) {
    case "1h": return 6;
    case "24h": return 8;
    case "3d": return 8;
    case "7d": return 7;
    case "30d": return 10;
  }
}

const ClimateChart = ({ data, zoneName, targetTemp, timeRange = "24h" }: ClimateChartProps) => {
  const now = Date.now();
  const rangeStart = now - RANGE_MS[timeRange];

  const chartData = data
    .map((d) => {
      const ts = parseISO(d.timestamp).getTime();
      const dpSpread = d.temperature - d.dewpoint;
      const spread = Math.round(dpSpread * 10) / 10;
      return {
        ...d,
        ts,
        dpSpread: spread,
        dpSpreadGood: spread >= 4 ? spread : undefined,
        dpSpreadBad: spread < 4 ? spread : undefined,
        outdoorHumidity: d.outdoorHumidity ?? 75,
        outdoorTemp: d.outdoorTemp ?? 10,
      };
    })
    .filter((d) => Number.isFinite(d.ts))
    .sort((a, b) => a.ts - b.ts);

  // Fill transition points so lines connect at the boundary
  for (let i = 1; i < chartData.length; i++) {
    const prev = chartData[i - 1];
    const curr = chartData[i];
    if (prev.dpSpreadGood !== undefined && curr.dpSpreadBad !== undefined) {
      curr.dpSpreadGood = curr.dpSpread; // bridge point
    } else if (prev.dpSpreadBad !== undefined && curr.dpSpreadGood !== undefined) {
      curr.dpSpreadBad = curr.dpSpread; // bridge point
    }
  }

  const dataSpanLabel = (() => {
    if (chartData.length < 2) return RANGE_LABELS[timeRange];
    const spanMs = chartData[chartData.length - 1].ts - chartData[0].ts;
    const spanHours = spanMs / (60 * 60 * 1000);
    if (spanHours < 1) return `${Math.max(1, Math.round(spanMs / 60000))}min of data`;
    if (spanHours < 24) return `${Math.round(spanHours)}h of data`;
    return `${Math.round(spanHours / 24)}d of data`;
  })();

  const heatingRanges: { start: number; end: number }[] = [];
  let rangeOpenTs: number | null = null;
  chartData.forEach((d) => {
    if (d.isHeating && rangeOpenTs === null) rangeOpenTs = d.ts;
    if (!d.isHeating && rangeOpenTs !== null) {
      heatingRanges.push({ start: rangeOpenTs, end: d.ts });
      rangeOpenTs = null;
    }
  });
  if (rangeOpenTs !== null) heatingRanges.push({ start: rangeOpenTs, end: chartData[chartData.length - 1]?.ts ?? now });

  return (
    <Card className="glass-card p-4">
      <h4 className="text-sm font-semibold text-foreground mb-1">
        {zoneName} — {RANGE_LABELS[timeRange]}
        {dataSpanLabel !== RANGE_LABELS[timeRange] && (
          <span className="text-muted-foreground font-normal ml-2">({dataSpanLabel})</span>
        )}
      </h4>
      <p className="text-[10px] text-muted-foreground mb-3">DP spread: solid = safe, dotted = risk · Orange = heating</p>

      <div className="h-48">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={chartData}>
            <defs>
              <linearGradient id={`humGrad-${zoneName}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="hsl(32, 95%, 55%)" stopOpacity={0.15} />
                <stop offset="95%" stopColor="hsl(32, 95%, 55%)" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(220 14% 18%)" />
            <XAxis
              type="number" dataKey="ts" scale="time"
              domain={[rangeStart, now]}
              tickCount={getTickCount(timeRange)}
              axisLine={false} tickLine={false}
              tick={{ fontSize: 9, fill: "hsl(215 12% 55%)" }}
              tickFormatter={(value) => formatTick(Number(value), timeRange)}
            />
            <YAxis
              yAxisId="hum" axisLine={false} tickLine={false}
              tick={{ fontSize: 9, fill: "hsl(175 65% 45%)" }}
              domain={[60, 90]} width={28}
            />
            <YAxis
              yAxisId="temp" orientation="right"
              axisLine={false} tickLine={false}
              tick={{ fontSize: 9, fill: "hsl(32 95% 55%)" }}
              domain={["auto", "auto"]} width={28}
            />
            <Tooltip
              labelFormatter={(value) => {
                const n = Number(value);
                if (!Number.isFinite(n) || n < 0) return String(value);
                try { return format(new Date(n), "dd/MM HH:mm"); } catch { return String(value); }
              }}
              contentStyle={{
                background: "hsl(220 18% 13%)",
                border: "1px solid hsl(220 14% 22%)",
                borderRadius: "8px",
                fontSize: "11px",
                color: "hsl(210 20% 92%)",
              }}
              formatter={(value: number, name: string) => {
                const labels: Record<string, string> = {
                  humidity: "Indoor RH",
                  temperature: "Indoor T",
                  outdoorTemp: "Outdoor T",
                  dewpoint: "Dewpoint",
                  dpSpreadGood: "DP Spread (safe)",
                  dpSpreadBad: "DP Spread (risk)",
                };
                return [typeof value === "number" ? value.toFixed(1) + "°" : value, labels[name] || name];
              }}
            />
            <ReferenceLine
              yAxisId="temp" y={4}
              stroke="hsl(0, 80%, 55%)" strokeDasharray="4 4" strokeOpacity={0.4}
            />
            {heatingRanges.map((r, i) => (
              <ReferenceArea
                key={i} yAxisId="hum"
                x1={r.start} x2={r.end}
                fill="hsl(32, 95%, 55%)" fillOpacity={0.3}
              />
            ))}
            {/* DP Spread — solid when safe (≥4), dotted when risk (<4) */}
            <Line yAxisId="temp" type="monotone" dataKey="dpSpreadGood" stroke="hsl(0, 80%, 55%)" strokeWidth={2.5} dot={false} connectNulls={false} name="dpSpreadGood" />
            <Line yAxisId="temp" type="monotone" dataKey="dpSpreadBad" stroke="hsl(0, 80%, 55%)" strokeWidth={2.5} strokeDasharray="4 3" dot={false} connectNulls={false} name="dpSpreadBad" />
            {/* Temperature */}
            <Line yAxisId="temp" type="monotone" dataKey="temperature" stroke="hsl(32, 95%, 55%)" strokeWidth={1.5} dot={false} name="temperature" />
            <Line yAxisId="temp" type="monotone" dataKey="outdoorTemp" stroke="hsl(210, 70%, 55%)" strokeWidth={1} dot={false} name="outdoorTemp" />
            {/* Humidity */}
            <Area yAxisId="hum" type="monotone" dataKey="humidity" stroke="hsl(175, 65%, 45%)" fill={`url(#humGrad-${zoneName})`} strokeWidth={1} strokeDasharray="6 3" dot={false} />
            {/* Dewpoint */}
            <Line yAxisId="temp" type="monotone" dataKey="dewpoint" stroke="hsl(0, 0%, 60%)" strokeWidth={1} strokeDasharray="3 3" dot={false} name="dewpoint" />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <div className="flex flex-wrap items-center gap-3 mt-2">
        <div className="flex items-center gap-1">
          <div className="h-0.5 w-4" style={{ borderTop: "2.5px solid hsl(0, 80%, 55%)" }} />
          <span className="text-[9px] text-destructive">DP safe</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="h-0.5 w-4" style={{ borderTop: "2.5px dotted hsl(0, 80%, 55%)" }} />
          <span className="text-[9px] text-destructive">DP risk</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="h-0.5 w-4" style={{ borderTop: "2px solid hsl(32, 95%, 55%)" }} />
          <span className="text-[9px] text-muted-foreground">Indoor T</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="h-0.5 w-4" style={{ borderTop: "2px dashed hsl(175, 65%, 45%)" }} />
          <span className="text-[9px] text-muted-foreground">RH</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="h-3 w-4 rounded-sm" style={{ backgroundColor: "hsla(32, 95%, 55%, 0.3)" }} />
          <span className="text-[9px] text-muted-foreground">Heating</span>
        </div>
      </div>
    </Card>
  );
};

export default ClimateChart;
