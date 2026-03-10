import { NetatmoModule } from "@/types/climate";
import { Card } from "@/components/ui/card";
import { Thermometer, Droplets, Wind, Volume2, Gauge } from "lucide-react";

interface NetatmoWidgetProps {
  modules: NetatmoModule[];
}

const NetatmoWidget = ({ modules }: NetatmoWidgetProps) => {
  const indoor = modules.find((m) => m.type === "NAMain");
  const outdoor = modules.find((m) => m.type === "NAModule1");

  return (
    <Card className="glass-card p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-semibold text-foreground text-sm">Netatmo Sensors</h3>
        <span className="text-[10px] text-success font-medium px-2 py-0.5 rounded-full bg-success/10">
          ✓ Live
        </span>
      </div>

      <div className="space-y-3">
        {indoor && (
          <div className="space-y-1.5">
            <span className="text-xs text-muted-foreground font-medium">{indoor.name}</span>
            <div className="flex flex-wrap gap-3">
              {indoor.temperature != null && (
                <div className="flex items-center gap-1">
                  <Thermometer className="h-3 w-3 text-foreground" />
                  <span className="metric-value text-sm text-foreground">{indoor.temperature}°</span>
                </div>
              )}
              {indoor.humidity != null && (
                <div className="flex items-center gap-1">
                  <Droplets className="h-3 w-3 text-humidity" />
                  <span className="metric-value text-sm text-humidity">
                    {indoor.humidity}%
                  </span>
                </div>
              )}
              {indoor.co2 != null && (
                <div className="flex items-center gap-1">
                  <Wind className="h-3 w-3 text-muted-foreground" />
                  <span className={`metric-value text-sm ${indoor.co2 > 1500 ? "text-destructive" : indoor.co2 > 1000 ? "text-warning" : indoor.co2 > 600 ? "text-muted-foreground" : "text-success"}`}>
                    {indoor.co2} ppm
                  </span>
                </div>
              )}
              {indoor.noise != null && (
                <div className="flex items-center gap-1">
                  <Volume2 className="h-3 w-3 text-muted-foreground" />
                  <span className="metric-value text-sm text-muted-foreground">{indoor.noise} dB</span>
                </div>
              )}
              {indoor.pressure != null && (
                <div className="flex items-center gap-1">
                  <Gauge className="h-3 w-3 text-muted-foreground" />
                  <span className="metric-value text-sm text-muted-foreground">{indoor.pressure} mbar</span>
                </div>
              )}
            </div>
          </div>
        )}

        {outdoor && (
          <div className="space-y-1.5 pt-2 border-t border-border">
            <span className="text-xs text-muted-foreground font-medium">{outdoor.name}</span>
            <div className="flex gap-3">
              {outdoor.temperature != null && (
                <div className="flex items-center gap-1">
                  <Thermometer className="h-3 w-3 text-foreground" />
                  <span className="metric-value text-sm text-foreground">{outdoor.temperature}°</span>
                </div>
              )}
              {outdoor.humidity != null && (
                <div className="flex items-center gap-1">
                  <Droplets className="h-3 w-3 text-humidity" />
                  <span className="metric-value text-sm text-humidity">{outdoor.humidity}%</span>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Extra modules */}
        {modules
          .filter((m) => m.type !== "NAMain" && m.type !== "NAModule1")
          .map((mod) => (
            <div key={mod.id} className="space-y-1 pt-2 border-t border-border">
              <span className="text-xs text-muted-foreground font-medium">{mod.name}</span>
              <div className="flex gap-3">
                {mod.temperature != null && (
                  <div className="flex items-center gap-1">
                    <Thermometer className="h-3 w-3 text-foreground" />
                    <span className="metric-value text-sm text-foreground">{mod.temperature}°</span>
                  </div>
                )}
                {mod.humidity != null && (
                  <div className="flex items-center gap-1">
                    <Droplets className="h-3 w-3 text-humidity" />
                    <span className="metric-value text-sm text-humidity">
                      {mod.humidity}%
                    </span>
                  </div>
                )}
                {mod.co2 != null && (
                  <span className={`metric-value text-xs ${mod.co2! > 1500 ? "text-destructive" : mod.co2! > 1000 ? "text-warning" : mod.co2! > 600 ? "text-muted-foreground" : "text-success"}`}>{mod.co2} ppm</span>
                )}
                {mod.rain != null && (
                  <span className="metric-value text-xs text-cold">{mod.rain} mm</span>
                )}
                {mod.windStrength != null && (
                  <span className="metric-value text-xs text-muted-foreground">{mod.windStrength} km/h</span>
                )}
              </div>
            </div>
          ))}
      </div>
    </Card>
  );
};

export default NetatmoWidget;
