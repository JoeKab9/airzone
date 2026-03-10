import { useState } from "react";
import { Card } from "@/components/ui/card";
import { useDailyAssessments, usePredictions } from "@/hooks/useControlData";
import { Shield, TrendingUp } from "lucide-react";
import {
  LineChart, Line, XAxis, YAxis, ResponsiveContainer, Tooltip,
  CartesianGrid, ReferenceLine,
} from "recharts";
import { format, parseISO } from "date-fns";

type TimeRange = "7d" | "30d" | "1y";

const rangeLabels: Record<TimeRange, string> = {
  "7d": "Week",
  "30d": "Month",
  "1y": "Year",
};

const ReliabilityChart = () => {
  const [range, setRange] = useState<TimeRange>("30d");
  const { data: assessments } = useDailyAssessments();
  const { data: predictions } = usePredictions(500);

  const now = new Date();
  const daysBack = range === "7d" ? 7 : range === "30d" ? 30 : 365;
  const cutoff = new Date(now.getTime() - daysBack * 86400000).toISOString().split("T")[0];

  // Energy estimation accuracy by day
  const energyByDay = (assessments || [])
    .filter((a) => a.date >= cutoff && a.estimation_accuracy_pct != null)
    .sort((a, b) => a.date.localeCompare(b.date))
    .map((a) => ({
      date: a.date,
      label: format(parseISO(a.date), "dd/MM"),
      energyAccuracy: a.estimation_accuracy_pct!,
    }));

  // Prediction accuracy by day — split by horizon (3h vs 24h)
  const validated = (predictions || []).filter(
    (p) => p.validated && p.prediction_error != null && p.created_at >= `${cutoff}T00:00:00Z`
  );

  const predByDay = new Map<string, {
    errors3h: number[]; errors24h: number[];
    correct: number; total: number;
  }>();
  for (const p of validated) {
    const day = p.created_at.slice(0, 10);
    const entry = predByDay.get(day) || { errors3h: [], errors24h: [], correct: 0, total: 0 };
    const horizon = (p as any).hours_ahead ?? 3;
    if (horizon >= 12) {
      entry.errors24h.push(Math.abs(p.prediction_error!));
    } else {
      entry.errors3h.push(Math.abs(p.prediction_error!));
    }
    if (p.decision_correct != null) {
      entry.total++;
      if (p.decision_correct) entry.correct++;
    }
    predByDay.set(day, entry);
  }

  // Merge into chart data
  const allDays = new Set([...energyByDay.map((e) => e.date), ...Array.from(predByDay.keys())]);
  const chartData = Array.from(allDays)
    .filter((d) => d >= cutoff)
    .sort()
    .map((date) => {
      const energy = energyByDay.find((e) => e.date === date);
      const pred = predByDay.get(date);

      const avg3h = pred && pred.errors3h.length > 0
        ? pred.errors3h.reduce((a, b) => a + b, 0) / pred.errors3h.length : null;
      const avg24h = pred && pred.errors24h.length > 0
        ? pred.errors24h.reduce((a, b) => a + b, 0) / pred.errors24h.length : null;

      const pred3hAccuracy = avg3h != null ? Math.max(0, 100 - avg3h * 20) : null;
      const pred24hAccuracy = avg24h != null ? Math.max(0, 100 - avg24h * 20) : null;
      const decisionAccuracy = pred && pred.total > 0 ? (pred.correct / pred.total) * 100 : null;

      return {
        date,
        label: format(parseISO(date), "dd/MM"),
        energyAccuracy: energy?.energyAccuracy ?? null,
        pred3hAccuracy,
        pred24hAccuracy,
        decisionAccuracy,
      };
    });

  const hasData = chartData.some((d) => d.energyAccuracy != null || d.pred3hAccuracy != null || d.pred24hAccuracy != null);

  return (
    <Card className="glass-card p-5">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <TrendingUp className="h-4 w-4 text-primary" />
          <h3 className="font-semibold text-foreground text-sm">Reliability Over Time</h3>
        </div>
        <div className="flex items-center gap-1">
          {(Object.keys(rangeLabels) as TimeRange[]).map((r) => (
            <button
              key={r}
              onClick={() => setRange(r)}
              className={`px-2.5 py-1 rounded-md text-[10px] font-medium transition-colors ${
                range === r
                  ? "bg-primary text-primary-foreground"
                  : "bg-secondary text-muted-foreground hover:text-foreground"
              }`}
            >
              {rangeLabels[r]}
            </button>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap gap-3 text-[10px] mb-3">
        <div className="flex items-center gap-1">
          <div className="h-0.5 w-4" style={{ borderTop: "2px solid hsl(32, 95%, 55%)" }} />
          <span className="text-muted-foreground">Energy est.</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="h-0.5 w-4" style={{ borderTop: "2px solid hsl(265, 70%, 60%)" }} />
          <span className="text-muted-foreground">DP 3h forecast</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="h-0.5 w-4" style={{ borderTop: "2px solid hsl(200, 70%, 55%)" }} />
          <span className="text-muted-foreground">DP 24h forecast</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="h-0.5 w-4" style={{ borderTop: "2px solid hsl(142, 70%, 50%)" }} />
          <span className="text-muted-foreground">Decisions</span>
        </div>
      </div>

      {hasData ? (
        <div className="h-52">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(220 14% 18%)" />
              <XAxis
                dataKey="label"
                axisLine={false}
                tickLine={false}
                tick={{ fontSize: 9, fill: "hsl(215 12% 55%)" }}
                interval={range === "7d" ? 0 : range === "30d" ? 3 : 29}
              />
              <YAxis
                axisLine={false}
                tickLine={false}
                tick={{ fontSize: 9, fill: "hsl(215 12% 55%)" }}
                domain={[0, 100]}
                width={30}
                label={{ value: "%", angle: -90, position: "insideLeft", style: { fontSize: 9, fill: "hsl(215 12% 55%)" } }}
              />
              <ReferenceLine y={80} stroke="hsl(142 70% 50%)" strokeDasharray="4 4" strokeOpacity={0.3} />
              <Tooltip
                contentStyle={{
                  background: "hsl(220 18% 13%)",
                  border: "1px solid hsl(220 14% 22%)",
                  borderRadius: "8px",
                  fontSize: "11px",
                  color: "hsl(210 20% 92%)",
                }}
                formatter={(value: number | null, name: string) => {
                  const labels: Record<string, string> = {
                    energyAccuracy: "Energy accuracy",
                    pred3hAccuracy: "DP 3h forecast",
                    pred24hAccuracy: "DP 24h forecast",
                    decisionAccuracy: "Decision accuracy",
                  };
                  return [value != null ? `${Math.round(value)}%` : "—", labels[name] || name];
                }}
              />
              <Line
                type="monotone"
                dataKey="energyAccuracy"
                stroke="hsl(32 95% 55%)"
                strokeWidth={2}
                dot={{ r: 2 }}
                connectNulls
              />
              <Line
                type="monotone"
                dataKey="pred3hAccuracy"
                stroke="hsl(265 70% 60%)"
                strokeWidth={2}
                dot={{ r: 2 }}
                connectNulls
              />
              <Line
                type="monotone"
                dataKey="pred24hAccuracy"
                stroke="hsl(200 70% 55%)"
                strokeWidth={2}
                dot={{ r: 2 }}
                strokeDasharray="5 3"
                connectNulls
              />
              <Line
                type="monotone"
                dataKey="decisionAccuracy"
                stroke="hsl(142 70% 50%)"
                strokeWidth={2}
                dot={{ r: 2 }}
                connectNulls
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="h-52 flex items-center justify-center">
          <p className="text-xs text-muted-foreground">
            Reliability data appears after a few days of validated predictions and Linky data.
          </p>
        </div>
      )}
    </Card>
  );
};

export default ReliabilityChart;
