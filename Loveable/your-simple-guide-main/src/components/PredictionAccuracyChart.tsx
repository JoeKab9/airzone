import { Card } from "@/components/ui/card";
import { usePredictions, useSystemState } from "@/hooks/useControlData";
import {
  ScatterChart, Scatter, XAxis, YAxis, ResponsiveContainer, Tooltip,
  ReferenceLine, CartesianGrid, Cell,
} from "recharts";
import { Brain, Target, TrendingUp } from "lucide-react";

const PredictionAccuracyChart = () => {
  const { data: predictions, isLoading } = usePredictions(200);
  const { data: systemState } = useSystemState();

  const validated = (predictions || []).filter((p) => p.validated && p.actual_dp_spread != null);
  const model = systemState?.prediction_model;

  // Scatter data: predicted vs actual
  const scatterData = validated.map((p) => ({
    predicted: p.predicted_dp_spread,
    actual: p.actual_dp_spread!,
    error: p.prediction_error ?? 0,
    zone: p.zone_name,
    decision: p.decision_made,
    correct: p.decision_correct,
  }));

  // Stats
  const totalDecisions = validated.filter((p) => p.decision_made != null).length;
  const correctDecisions = validated.filter((p) => p.decision_correct === true).length;
  const skippedHeating = validated.filter((p) => p.decision_made === "skip_heating").length;
  const earlyStops = validated.filter((p) => p.decision_made === "early_stop").length;

  return (
    <Card className="glass-card p-5">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Brain className="h-4 w-4 text-primary" />
          <h3 className="font-semibold text-foreground text-sm">Prediction Model</h3>
        </div>
        {model?.rmse != null && (
          <span className={`text-[10px] font-medium px-2 py-0.5 rounded-full ${
            model.rmse < 1 ? "bg-success/10 text-success" : model.rmse < 1.5 ? "bg-warning/10 text-warning" : "bg-destructive/10 text-destructive"
          }`}>
            RMSE {model.rmse}°
          </span>
        )}
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-4 gap-2 mb-4">
        <div className="text-center">
          <p className="text-lg font-bold text-foreground">{validated.length}</p>
          <p className="text-[9px] text-muted-foreground">Validated</p>
        </div>
        <div className="text-center">
          <p className="text-lg font-bold text-success">
            {totalDecisions > 0 ? Math.round((correctDecisions / totalDecisions) * 100) : 0}%
          </p>
          <p className="text-[9px] text-muted-foreground">Correct</p>
        </div>
        <div className="text-center">
          <p className="text-lg font-bold text-primary">{skippedHeating}</p>
          <p className="text-[9px] text-muted-foreground">Skips</p>
        </div>
        <div className="text-center">
          <p className="text-lg font-bold text-heating">{earlyStops}</p>
          <p className="text-[9px] text-muted-foreground">Early stops</p>
        </div>
      </div>

      {/* Model params */}
      {model && (
        <div className="flex gap-3 mb-3 text-[9px] text-muted-foreground">
          <span>Infiltration: {model.infiltration_rate ?? "?"}/h</span>
          <span>Bias: {model.bias ?? "?"}°</span>
          <span>{model.validated_count ?? 0} samples</span>
        </div>
      )}

      {/* Scatter: predicted vs actual */}
      {scatterData.length > 2 ? (
        <ResponsiveContainer width="100%" height={200}>
          <ScatterChart margin={{ top: 5, right: 5, bottom: 20, left: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.3} />
            <XAxis
              dataKey="predicted" type="number" name="Predicted"
              label={{ value: "Predicted DP Spread °C", position: "bottom", offset: 5, fontSize: 9, fill: "hsl(var(--muted-foreground))" }}
              tick={{ fontSize: 9 }} stroke="hsl(var(--muted-foreground))"
            />
            <YAxis
              dataKey="actual" type="number" name="Actual"
              label={{ value: "Actual °C", angle: -90, position: "insideLeft", fontSize: 9, fill: "hsl(var(--muted-foreground))" }}
              tick={{ fontSize: 9 }} stroke="hsl(var(--muted-foreground))"
            />
            <ReferenceLine
              segment={[{ x: 0, y: 0 }, { x: 12, y: 12 }]}
              stroke="hsl(var(--primary))" strokeDasharray="4 4" opacity={0.5}
            />
            <Tooltip
              content={({ payload }) => {
                if (!payload?.[0]) return null;
                const d = payload[0].payload;
                return (
                  <div className="bg-card border border-border rounded p-2 text-xs shadow-lg">
                    <p>Predicted: {d.predicted}° → Actual: {d.actual}°</p>
                    <p className="text-muted-foreground">{d.zone} · {d.decision} · {d.correct ? "✓" : "✗"}</p>
                  </div>
                );
              }}
            />
            <Scatter data={scatterData}>
              {scatterData.map((entry, i) => (
                <Cell
                  key={i}
                  fill={entry.correct === true ? "hsl(var(--success))" : entry.correct === false ? "hsl(var(--destructive))" : "hsl(var(--muted-foreground))"}
                  opacity={0.7}
                />
              ))}
            </Scatter>
          </ScatterChart>
        </ResponsiveContainer>
      ) : (
        <div className="h-[200px] flex items-center justify-center">
          <p className="text-xs text-muted-foreground">
            {isLoading ? "Loading…" : "Collecting predictions — data will appear after a few hours."}
          </p>
        </div>
      )}
    </Card>
  );
};

export default PredictionAccuracyChart;
