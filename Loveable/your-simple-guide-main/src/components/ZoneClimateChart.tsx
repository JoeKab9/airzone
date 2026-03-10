import { useState, useEffect } from "react";
import ClimateChart from "@/components/ClimateChart";
import { useZoneClimateHistory, TimeRange } from "@/hooks/useControlData";
import { Zone } from "@/types/climate";
import { ZONE_DISPLAY_NAMES } from "@/data/sensorMapping";

// Reverse mapping: display name → raw Airzone name used in control_log
const DISPLAY_TO_RAW: Record<string, string> = {};
for (const [raw, display] of Object.entries(ZONE_DISPLAY_NAMES)) {
  DISPLAY_TO_RAW[display] = raw;
}

const TIME_RANGES: { value: TimeRange; label: string }[] = [
  { value: "1h", label: "1h" },
  { value: "24h", label: "24h" },
  { value: "3d", label: "3d" },
  { value: "7d", label: "7d" },
  { value: "30d", label: "30d" },
];

// Next fallback range when current has no data
const FALLBACK: Partial<Record<TimeRange, TimeRange>> = {
  "1h": "24h",
  "24h": "3d",
  "3d": "7d",
};

interface ZoneClimateChartProps {
  zone: Zone;
}

const ZoneClimateChart = ({ zone }: ZoneClimateChartProps) => {
  const [userRange, setUserRange] = useState<TimeRange>("24h");
  const [effectiveRange, setEffectiveRange] = useState<TimeRange>("24h");
  const rawName = DISPLAY_TO_RAW[zone.name] || zone.name;
  const { data: history, isLoading } = useZoneClimateHistory(rawName, effectiveRange);

  const hasData = history && history.length > 0;

  // Auto-expand range if no data found
  useEffect(() => {
    if (!isLoading && !hasData && effectiveRange === userRange) {
      const fb = FALLBACK[effectiveRange];
      if (fb) {
        setEffectiveRange(fb);
      }
    } else if (effectiveRange !== userRange && hasData) {
      // Keep expanded range, don't reset
    }
  }, [isLoading, hasData, effectiveRange, userRange]);

  const handleSetRange = (r: TimeRange) => {
    setUserRange(r);
    setEffectiveRange(r);
  };

  const wasExpanded = effectiveRange !== userRange;

  return (
    <div>
      <div className="flex items-center gap-1 mb-2">
        {TIME_RANGES.map((tr) => (
          <button
            key={tr.value}
            onClick={() => handleSetRange(tr.value)}
            className={`px-2.5 py-1 rounded-md text-[10px] font-medium transition-colors ${
              userRange === tr.value
                ? "bg-primary text-primary-foreground"
                : "bg-secondary text-muted-foreground hover:text-foreground"
            }`}
          >
            {tr.label}
          </button>
        ))}
        {isLoading && (
          <span className="text-[10px] text-muted-foreground ml-2">Loading…</span>
        )}
        {wasExpanded && hasData && (
          <span className="text-[10px] text-warning ml-2">No data in {userRange}, showing {effectiveRange}</span>
        )}
      </div>
      {hasData ? (
        <ClimateChart
          data={history}
          zoneName={zone.name}
          targetTemp={zone.targetTemperature}
          timeRange={effectiveRange}
        />
      ) : (
        <div className="glass-card rounded-xl p-6 flex items-center justify-center h-48">
          <p className="text-xs text-muted-foreground">
            {isLoading ? "Loading…" : `No data for ${zone.name} in the last ${effectiveRange}. Try a longer range.`}
          </p>
        </div>
      )}
    </div>
  );
};

export default ZoneClimateChart;
