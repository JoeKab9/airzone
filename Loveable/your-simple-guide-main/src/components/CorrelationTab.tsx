import { useMemo, useState } from "react";
import { useRecentControlLogs } from "@/hooks/useControlData";
import { ZONE_DISPLAY_NAMES } from "@/data/sensorMapping";
import CorrelationScatter, { CorrelationPoint } from "@/components/CorrelationScatter";
import OutdoorCorrelationChart from "@/components/OutdoorCorrelationChart";
import { calcDewpoint } from "@/lib/dewpoint";

const CorrelationTab = () => {
  const { data: logs, isLoading } = useRecentControlLogs(1000);

  const zoneData = useMemo(() => {
    if (!logs) return {};
    const byZone: Record<string, typeof logs> = {};
    for (const l of logs) {
      const name = ZONE_DISPLAY_NAMES[l.zone_name] || l.zone_name;
      if (!byZone[name]) byZone[name] = [];
      byZone[name].push(l);
    }
    return byZone;
  }, [logs]);

  const zoneNames = Object.keys(zoneData).sort();
  const [selectedZone, setSelectedZone] = useState<string | null>(null);
  const activeZone = selectedZone || zoneNames[0] || null;

  const charts = useMemo(() => {
    if (!activeZone || !zoneData[activeZone]) return null;
    const zoneLogs = zoneData[activeZone];

    const valid = zoneLogs.filter(
      (l) => l.outdoor_temp != null && l.outdoor_humidity != null && l.temperature != null
    );

    const isHeatingLog = (l: typeof valid[0]) =>
      l.action === "heating_on" || (l.action === "no_change" && !!l.reason && /heating\s*\(band/i.test(l.reason || ""));

    const notHeating = valid.filter((l) => !isHeatingLog(l));

    // 1. Outdoor temp → Indoor temp (idle)
    const tempCorr: CorrelationPoint[] = notHeating.map((l) => ({
      x: l.outdoor_temp!,
      y: l.temperature!,
      zone: activeZone,
      dpSpread: l.dp_spread ?? undefined,
    }));

    // 2. Outdoor RH → Indoor RH (idle)
    const humCorr: CorrelationPoint[] = notHeating
      .filter((l) => l.outdoor_humidity != null)
      .map((l) => ({
        x: l.outdoor_humidity!,
        y: l.humidity_netatmo ?? l.humidity_airzone ?? 0,
        zone: activeZone,
        dpSpread: l.dp_spread ?? undefined,
      }));

    // 3. Outdoor DP → Indoor DP (idle)
    const dpCorr: CorrelationPoint[] = notHeating
      .filter((l) => l.outdoor_humidity != null)
      .map((l) => {
        const indoorH = l.humidity_netatmo ?? l.humidity_airzone ?? 0;
        const indoorDp = l.dewpoint ?? calcDewpoint(l.temperature!, indoorH);
        const outdoorDp = calcDewpoint(l.outdoor_temp!, l.outdoor_humidity!);
        return {
          x: Math.round(outdoorDp * 10) / 10,
          y: Math.round(indoorDp * 10) / 10,
          zone: activeZone,
          dpSpread: l.dp_spread ?? undefined,
        };
      });

    // 4. Outdoor temp → Indoor RH (idle) - key relationship
    const tempToHumCorr: CorrelationPoint[] = notHeating
      .map((l) => ({
        x: l.outdoor_temp!,
        y: l.humidity_netatmo ?? l.humidity_airzone ?? 0,
        zone: activeZone,
        dpSpread: l.dp_spread ?? undefined,
      }));

    return { tempCorr, humCorr, dpCorr, tempToHumCorr };
  }, [activeZone, zoneData]);

  if (isLoading) {
    return <p className="text-xs text-muted-foreground p-4">Loading correlation data…</p>;
  }

  return (
    <div className="space-y-4">
      {/* Global AH → DP Spread scatter */}
      <OutdoorCorrelationChart />

      {/* Zone selector */}
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs text-muted-foreground mr-1">Zone:</span>
        {zoneNames.map((z) => (
          <button
            key={z}
            onClick={() => setSelectedZone(z)}
            className={`px-2.5 py-1 rounded-md text-[10px] font-medium transition-colors ${
              z === activeZone
                ? "bg-primary text-primary-foreground"
                : "bg-secondary text-muted-foreground hover:text-foreground"
            }`}
          >
            {z}
          </button>
        ))}
      </div>

      {charts && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          <CorrelationScatter
            title={`Outdoor Temp → Indoor Temp (idle)`}
            points={charts.tempCorr}
            xLabel="Outdoor °C"
            yLabel="Indoor °C"
            colorMode="dp-spread"
          />
          <CorrelationScatter
            title={`Outdoor Temp → Indoor RH (idle)`}
            points={charts.tempToHumCorr}
            xLabel="Outdoor °C"
            yLabel="Indoor RH %"
            colorMode="dp-spread"
            referenceLines={[
              { axis: "y", value: 65, color: "hsl(var(--warning))" },
              { axis: "y", value: 70, color: "hsl(var(--destructive))" },
            ]}
          />
          <CorrelationScatter
            title={`Outdoor RH → Indoor RH (idle)`}
            points={charts.humCorr}
            xLabel="Outdoor RH %"
            yLabel="Indoor RH %"
            colorMode="dp-spread"
            referenceLines={[
              { axis: "y", value: 65, color: "hsl(var(--warning))" },
              { axis: "y", value: 70, color: "hsl(var(--destructive))" },
            ]}
          />
          <CorrelationScatter
            title={`Outdoor DP → Indoor DP (idle)`}
            points={charts.dpCorr}
            xLabel="Outdoor DP °C"
            yLabel="Indoor DP °C"
            colorMode="dp-spread"
          />
        </div>
      )}
    </div>
  );
};

export default CorrelationTab;
