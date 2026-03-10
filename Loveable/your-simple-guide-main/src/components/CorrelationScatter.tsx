import { Card } from "@/components/ui/card";
import {
  ScatterChart, Scatter, XAxis, YAxis, ResponsiveContainer, Tooltip,
  CartesianGrid, Cell, ReferenceLine, Line,
} from "recharts";

export interface CorrelationPoint {
  x: number;
  y: number;
  zone: string;
  label?: string;
  isHeating?: boolean;
  dpSpread?: number;
}

interface RegressionResult {
  slope: number;
  intercept: number;
  r2: number;
  points: { x: number; y: number }[];
}

function linearRegression(pts: CorrelationPoint[]): RegressionResult | null {
  if (pts.length < 3) return null;
  const n = pts.length;
  let sumX = 0, sumY = 0, sumXY = 0, sumX2 = 0, sumY2 = 0;
  for (const p of pts) {
    sumX += p.x; sumY += p.y;
    sumXY += p.x * p.y;
    sumX2 += p.x * p.x;
    sumY2 += p.y * p.y;
  }
  const denom = n * sumX2 - sumX * sumX;
  if (Math.abs(denom) < 1e-10) return null;
  const slope = (n * sumXY - sumX * sumY) / denom;
  const intercept = (sumY - slope * sumX) / n;

  // R²
  const meanY = sumY / n;
  let ssTot = 0, ssRes = 0;
  for (const p of pts) {
    ssTot += (p.y - meanY) ** 2;
    ssRes += (p.y - (slope * p.x + intercept)) ** 2;
  }
  const r2 = ssTot > 0 ? 1 - ssRes / ssTot : 0;

  // Generate trend line points
  const xs = pts.map(p => p.x);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const trendPts = [
    { x: minX, y: Math.round((slope * minX + intercept) * 10) / 10 },
    { x: maxX, y: Math.round((slope * maxX + intercept) * 10) / 10 },
  ];

  return { slope, intercept, r2, points: trendPts };
}

interface CorrelationScatterProps {
  title: string;
  points: CorrelationPoint[];
  xLabel: string;
  yLabel: string;
  colorMode?: "dp-spread" | "fixed";
  fixedColor?: string;
  referenceLines?: { axis: "x" | "y"; value: number; color: string }[];
  height?: number;
}

const CorrelationScatter = ({
  title,
  points,
  xLabel,
  yLabel,
  colorMode = "fixed",
  fixedColor = "hsl(var(--primary))",
  referenceLines = [],
  height = 220,
}: CorrelationScatterProps) => {
  const reg = linearRegression(points);

  if (points.length < 3) {
    return (
      <Card className="glass-card p-4">
        <h4 className="text-xs font-semibold text-foreground mb-2">{title}</h4>
        <div className="flex items-center justify-center" style={{ height }}>
          <p className="text-[10px] text-muted-foreground">Not enough data points yet.</p>
        </div>
      </Card>
    );
  }

  const r2Color = reg ? (reg.r2 > 0.6 ? "text-success" : reg.r2 > 0.3 ? "text-warning" : "text-destructive") : "";
  const slopeDir = reg ? (reg.slope > 0.01 ? "↑" : reg.slope < -0.01 ? "↓" : "→") : "";

  return (
    <Card className="glass-card p-4">
      <div className="flex items-center justify-between mb-1">
        <h4 className="text-xs font-semibold text-foreground">{title}</h4>
        {reg && (
          <div className="text-[10px] text-muted-foreground">
            R² = <span className={`font-medium ${r2Color}`}>{reg.r2.toFixed(2)}</span>
            <span className="ml-2">slope: {slopeDir} {reg.slope.toFixed(2)}</span>
          </div>
        )}
      </div>
      {reg && (
        <p className="text-[10px] text-muted-foreground mb-2">
          {Math.abs(reg.slope) < 0.05
            ? "Indoor barely reacts to outdoor changes"
            : reg.r2 > 0.5
              ? `Strong link: +1 outdoor → ${reg.slope > 0 ? "+" : ""}${reg.slope.toFixed(2)} indoor`
              : `Weak link (R²=${reg.r2.toFixed(2)}): other factors dominate`
          }
        </p>
      )}
      <ResponsiveContainer width="100%" height={height}>
        <ScatterChart margin={{ top: 5, right: 5, bottom: 20, left: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.3} />
          <XAxis
            dataKey="x" type="number" name={xLabel}
            label={{ value: xLabel, position: "bottom", offset: 5, fontSize: 9, fill: "hsl(var(--muted-foreground))" }}
            tick={{ fontSize: 9 }} stroke="hsl(var(--muted-foreground))"
          />
          <YAxis
            dataKey="y" type="number" name={yLabel}
            label={{ value: yLabel, angle: -90, position: "insideLeft", fontSize: 9, fill: "hsl(var(--muted-foreground))" }}
            tick={{ fontSize: 9 }} stroke="hsl(var(--muted-foreground))"
            width={35}
          />
          {referenceLines.map((rl, i) =>
            rl.axis === "y" ? (
              <ReferenceLine key={i} y={rl.value} stroke={rl.color} strokeDasharray="4 4" opacity={0.5} />
            ) : (
              <ReferenceLine key={i} x={rl.value} stroke={rl.color} strokeDasharray="4 4" opacity={0.5} />
            )
          )}
          <Tooltip
            content={({ payload }) => {
              if (!payload?.[0]) return null;
              const d = payload[0].payload as CorrelationPoint;
              return (
                <div className="bg-card border border-border rounded p-2 text-[10px] shadow-lg">
                  <p>{xLabel}: {d.x} · {yLabel}: {d.y}</p>
                  <p className="text-muted-foreground">{d.zone}{d.dpSpread != null ? ` · DP spread: ${d.dpSpread}°` : ""}</p>
                </div>
              );
            }}
          />
          <Scatter data={points}>
            {points.map((entry, i) => (
              <Cell
                key={i}
                fill={
                  colorMode === "dp-spread" && entry.dpSpread != null
                    ? entry.dpSpread < 4 ? "hsl(var(--destructive))" : entry.dpSpread < 6 ? "hsl(var(--warning))" : "hsl(var(--success))"
                    : fixedColor
                }
                opacity={0.5}
                r={2.5}
              />
            ))}
          </Scatter>
          {/* Trend line */}
          {reg && (
            <Scatter data={reg.points} line={{ stroke: "hsl(var(--foreground))", strokeWidth: 2, strokeDasharray: "6 3" }} shape={() => null} />
          )}
        </ScatterChart>
      </ResponsiveContainer>
      <div className="flex gap-3 mt-1 text-[9px] text-muted-foreground">
        {colorMode === "dp-spread" && (
          <>
            <span>● <span className="text-destructive">Red</span> = DP &lt;4°</span>
            <span>● <span className="text-warning">Amber</span> = 4-6°</span>
            <span>● <span className="text-success">Green</span> = &gt;6°</span>
          </>
        )}
        <span>--- Trend line</span>
      </div>
    </Card>
  );
};

export default CorrelationScatter;
