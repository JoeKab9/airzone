import { forwardRef } from "react";
import { ArrowRight } from "lucide-react";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";

interface DpTrendArrowProps {
  /** -2 = strong worsening, -1 = slight worsening, 0 = stable, 1 = slight improvement, 2 = strong improvement */
  trend: number;
  confidence: number;
  factors?: string[];
  isLearning?: boolean;
  className?: string;
}

/**
 * 5-position arrow showing predicted DP spread trend (assuming no heating).
 * When model is still learning: shows arrow with low opacity.
 * All predictions are from learned data only — no hardcoded assumptions.
 */
const DpTrendArrow = forwardRef<HTMLSpanElement, DpTrendArrowProps>(
  ({ trend, confidence, factors, isLearning, className = "" }, ref) => {
    const getRotation = (t: number) => {
      if (t >= 2) return -90;
      if (t >= 1) return -45;
      if (t >= -0.5) return 0;
      if (t >= -1.5) return 45;
      return 90;
    };

    const getColor = (t: number) => {
      if (t >= 2) return "text-success";
      if (t >= 1) return "text-success";
      if (t >= -0.5) return "text-foreground";
      if (t >= -1.5) return "text-destructive";
      return "text-destructive";
    };

    const getLabel = (t: number) => {
      if (t >= 2) return "Improving fast";
      if (t >= 1) return "Improving";
      if (t >= -0.5) return "Stable";
      if (t >= -1.5) return "Worsening";
      return "Worsening fast";
    };

    const clampedTrend = Math.max(-2, Math.min(2, trend));
    const rotation = getRotation(clampedTrend);
    const color = isLearning ? "text-foreground" : getColor(clampedTrend);
    const label = isLearning ? "Learning..." : getLabel(clampedTrend);

    const tooltipLines = isLearning
      ? [
          "🧠 Learning thermal behavior",
          ...(factors?.length ? factors.map((f) => `• ${f}`) : []),
          "Needs more heating cycles to predict",
        ]
      : [
          `Prediction: ${label}`,
          `Confidence: ${Math.round(confidence * 100)}%`,
          ...(factors?.length ? factors.map((f) => `• ${f}`) : []),
          "(assuming no heating)",
        ];

    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span ref={ref} className={`inline-flex items-center ${className}`}>
            <ArrowRight
              className={`h-5 w-5 ${color} transition-transform duration-500`}
              strokeWidth={3}
              style={{
                transform: `rotate(${rotation}deg)`,
                opacity: isLearning ? 0.5 : 0.7 + confidence * 0.3,
              }}
            />
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" className="max-w-[220px]">
          <div className="text-[10px] space-y-0.5">
            {tooltipLines.map((line, i) => (
              <p key={i}>{line}</p>
            ))}
          </div>
        </TooltipContent>
      </Tooltip>
    );
  },
);

DpTrendArrow.displayName = "DpTrendArrow";

export default DpTrendArrow;
