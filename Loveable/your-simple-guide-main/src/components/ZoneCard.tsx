import { Zone, NetatmoModule } from "@/types/climate";
import { Card } from "@/components/ui/card";
import { Flame, AlertTriangle, Clock } from "lucide-react";
import { calcRoomDewpoint } from "@/lib/dewpoint";
import DpTrendArrow from "@/components/DpTrendArrow";
import { DpPrediction } from "@/hooks/useDpPredictions";

interface ZoneCardProps {
  zone: Zone;
  netatmo?: NetatmoModule;
  heatingForecast?: { action: string; reason: string; forecastBestHour?: string | null } | null;
  dpPrediction?: DpPrediction | null;
  onClick?: () => void;
}

const ZoneCard = ({ zone, netatmo, heatingForecast, dpPrediction, onClick }: ZoneCardProps) => {
  const refHumidity = netatmo?.humidity ?? zone.humidity;
  const dewpoint = calcRoomDewpoint(
    zone.temperature, zone.humidity,
    netatmo?.temperature, netatmo?.humidity,
  );
  const dpSpread = Math.round((zone.temperature - dewpoint) * 10) / 10;
  const isCondensationRisk = dpSpread <= 3;
  const isMouldRisk = dpSpread <= 5;

  const spreadColor = isCondensationRisk
    ? "text-destructive"
    : isMouldRisk
    ? "text-warning"
    : "text-success";

  // Determine heating forecast label
  let heatingLabel: string | null = null;
  if (heatingForecast) {
    if (heatingForecast.action === "defer_heating" && heatingForecast.forecastBestHour) {
      const dt = new Date(heatingForecast.forecastBestHour);
      heatingLabel = `Heat ~${dt.getHours()}:00`;
    }
  }

  return (
    <Card
      className={`glass-card cursor-pointer transition-all duration-200 hover:scale-[1.01] hover:border-primary/30 h-full ${
        zone.isHeating ? "glow-heating border-heating/20" : ""
      } ${isCondensationRisk ? "border-destructive/30" : ""}`}
      onClick={onClick}
    >
      <div className="px-3 py-2">
        {/* Row 1: Name + badges */}
        <div className="flex items-center justify-between mb-1">
          <h3 className="font-semibold text-white text-sm leading-tight truncate">{zone.name}</h3>
          <div className="flex items-center gap-1">
            {zone.isHeating && (
              <Flame className="h-3 w-3 text-heating status-pulse" />
            )}
            {isCondensationRisk && (
              <AlertTriangle className="h-3 w-3 text-destructive" />
            )}
          </div>
        </div>

        {/* Row 2: DP spread (primary) + temp + RH + DP */}
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 mb-1">
          <span className={`metric-value text-xl leading-none font-bold ${spreadColor}`}>
            Δ{dpSpread.toFixed(1)}°
          </span>
          {dpPrediction && (
            <DpTrendArrow
              trend={dpPrediction.trend}
              confidence={dpPrediction.confidence}
              factors={dpPrediction.factors}
              isLearning={dpPrediction.isLearning}
              className="ml-0.5"
            />
          )}
          <span className="metric-value text-xs leading-none font-medium text-foreground">{zone.temperature.toFixed(1)}°</span>
          <span className="metric-value text-xs leading-none font-medium text-foreground">{refHumidity}%</span>
          <span className="metric-value text-[11px] leading-none font-medium text-foreground">DP {dewpoint.toFixed(1)}°</span>
        </div>

        {/* Row 3: Sensor comparison */}
        {netatmo && (
          <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[10px] text-foreground/80">
            <span className="whitespace-nowrap">AZ {zone.temperature.toFixed(1)}°/{zone.humidity}%</span>
            <span>·</span>
            <span className="whitespace-nowrap">NT {netatmo.temperature?.toFixed(1)}°/{netatmo.humidity}%</span>
            {netatmo.co2 != null && (
              <>
                <span>·</span>
                <span className={`whitespace-nowrap ${netatmo.co2 > 1500 ? "text-destructive" : netatmo.co2 > 1000 ? "text-warning" : netatmo.co2 > 600 ? "text-muted-foreground" : "text-success"}`}>{netatmo.co2}ppm</span>
              </>
            )}
          </div>
        )}

        {/* Row 4: Heating forecast */}
        {heatingLabel && (
          <div className="flex items-center gap-1 mt-1.5 text-[9px]">
            <Clock className="h-2.5 w-2.5 text-heating" />
            <span className="text-heating font-medium text-foreground">{heatingLabel}</span>
          </div>
        )}
      </div>
    </Card>
  );
};

export default ZoneCard;
