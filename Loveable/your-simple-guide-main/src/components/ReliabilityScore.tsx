import { Card } from "@/components/ui/card";
import { Shield } from "lucide-react";

interface ReliabilityScoreProps {
  score: number | null; // 0-100
  label: string;
  samples?: number;
  compact?: boolean;
}

const ReliabilityScore = ({ score, label, samples, compact = false }: ReliabilityScoreProps) => {
  const getColor = (s: number | null) => {
    if (s == null) return "text-muted-foreground";
    if (s >= 80) return "text-success";
    if (s >= 60) return "text-warning";
    return "text-destructive";
  };

  const getBg = (s: number | null) => {
    if (s == null) return "bg-muted/50";
    if (s >= 80) return "bg-success/10 border-success/20";
    if (s >= 60) return "bg-warning/10 border-warning/20";
    return "bg-destructive/10 border-destructive/20";
  };

  if (compact) {
    return (
      <div className={`rounded-lg border p-2.5 ${getBg(score)}`}>
        <div className="flex items-center gap-1.5 mb-1">
          <Shield className={`h-3 w-3 ${getColor(score)}`} />
          <span className="text-[9px] text-muted-foreground uppercase tracking-wider">{label}</span>
        </div>
        <span className={`metric-value text-lg ${getColor(score)}`}>
          {score != null ? `${Math.round(score)}%` : "—"}
        </span>
        {samples != null && (
          <p className="text-[9px] text-muted-foreground mt-0.5">{samples} samples</p>
        )}
      </div>
    );
  }

  return (
    <Card className={`glass-card p-4 ${getBg(score)}`}>
      <div className="flex items-center gap-2 mb-2">
        <Shield className={`h-4 w-4 ${getColor(score)}`} />
        <span className="text-xs text-muted-foreground uppercase tracking-wider">{label}</span>
      </div>
      <div className="flex items-baseline gap-2">
        <span className={`metric-value text-2xl ${getColor(score)}`}>
          {score != null ? `${Math.round(score)}%` : "—"}
        </span>
        {samples != null && (
          <span className="text-[10px] text-muted-foreground">{samples} samples</span>
        )}
      </div>
      {score != null && (
        <div className="h-1.5 w-full rounded-full bg-secondary overflow-hidden mt-2">
          <div
            className={`h-full rounded-full transition-all duration-500 ${
              score >= 80 ? "bg-success" : score >= 60 ? "bg-warning" : "bg-destructive"
            }`}
            style={{ width: `${Math.min(100, score)}%` }}
          />
        </div>
      )}
    </Card>
  );
};

export default ReliabilityScore;
