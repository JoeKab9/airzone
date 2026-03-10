import { WeatherData, NetatmoModule } from "@/types/climate";
import { Card } from "@/components/ui/card";
import { Cloud, Sun, CloudRain, Snowflake, CloudSun, Wind, Droplets, Thermometer, Activity, Moon, CloudMoon } from "lucide-react";

// Sunrise/sunset approximation for Contis (lat ~44°N)
function isNightTime(timeStr: string): boolean {
  const hour = parseInt(timeStr.split(":")[0], 10);
  const month = new Date().getMonth(); // 0-11
  // Approximate sunrise/sunset hours by month for lat 44°N
  const sunrise = [8, 7, 7, 7, 6, 6, 6, 7, 7, 8, 7, 8][month];
  const sunset = [17, 18, 19, 20, 21, 21, 21, 21, 20, 19, 17, 17][month];
  return hour < sunrise || hour >= sunset;
}

function getConditionIcon(condition: string, size: "sm" | "lg", night: boolean): React.ReactNode {
  const cls = size === "lg" ? "h-5 w-5" : "h-3.5 w-3.5";
  
  if (condition === "sunny") {
    return night ? <Moon className={`${cls} text-muted-foreground`} /> : <Sun className={`${cls} text-warning`} />;
  }
  if (condition === "partly-cloudy") {
    return night ? <CloudMoon className={`${cls} text-muted-foreground`} /> : <CloudSun className={`${cls} text-warning`} />;
  }
  if (condition === "cloudy") return <Cloud className={`${cls} text-muted-foreground`} />;
  if (condition === "rainy") return <CloudRain className={`${cls} text-cold`} />;
  if (condition === "snowy") return <Snowflake className={`${cls} text-cold`} />;
  return <Cloud className={`${cls} text-muted-foreground`} />;
}

interface WeatherWidgetProps {
  data: WeatherData;
  netatmoOutdoor?: NetatmoModule;
}

const WeatherWidget = ({ data, netatmoOutdoor }: WeatherWidgetProps) => {
  const temp = netatmoOutdoor?.temperature ?? data.temperature;
  const humidity = netatmoOutdoor?.humidity ?? data.humidity;
  const hasNetatmo = !!netatmoOutdoor;

  return (
    <Card className="glass-card p-5">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <h3 className="font-semibold text-foreground text-sm">Weather Outside</h3>
          {hasNetatmo && (
            <div className="flex items-center gap-1 rounded-full bg-success/10 px-2 py-0.5">
              <Activity className="h-2.5 w-2.5 text-success" />
              <span className="text-[9px] font-medium text-success">Netatmo</span>
            </div>
          )}
        </div>
        {getConditionIcon(data.condition, "lg", isNightTime(new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })))}
      </div>

      <div className="flex items-end gap-3 mb-4">
        <span className="metric-value text-3xl text-foreground leading-none">
          {temp.toFixed(1)}°
        </span>
        <div className="flex flex-col gap-0.5 mb-0.5">
          <div className="flex items-center gap-1">
            <Droplets className="h-3 w-3 text-humidity" />
            <span className="text-xs text-muted-foreground">{humidity}%</span>
          </div>
          <div className="flex items-center gap-1">
            <Thermometer className="h-3 w-3 text-muted-foreground" />
            <span className="text-xs text-muted-foreground">DP {data.dewpoint.toFixed(1)}°</span>
          </div>
          <div className="flex items-center gap-1">
            <Wind className="h-3 w-3 text-muted-foreground" />
            <span className="text-xs text-muted-foreground">{data.windSpeed} km/h</span>
          </div>
        </div>
      </div>

      {hasNetatmo && (
        <div className="rounded-md bg-secondary/50 border border-border p-2 mb-3">
          <div className="flex items-center justify-between text-[10px]">
            <span className="text-muted-foreground">Open-Meteo</span>
            <span className="metric-value text-foreground">{data.temperature.toFixed(1)}° · {data.humidity}%</span>
          </div>
          <div className="flex items-center justify-between text-[10px] mt-0.5">
            <span className="text-muted-foreground">Netatmo local</span>
            <span className="metric-value text-success">{netatmoOutdoor?.temperature?.toFixed(1)}° · {netatmoOutdoor?.humidity}%</span>
          </div>
        </div>
      )}

      <div className="border-t border-border pt-3">
        <span className="text-xs text-muted-foreground font-medium mb-2 block">Forecast</span>
        <div className="grid grid-cols-6 gap-1">
          {data.forecast.map((f) => (
            <div key={f.time} className="flex flex-col items-center gap-1 py-1">
              <span className="text-[10px] text-muted-foreground">{f.time}</span>
              {getConditionIcon(f.condition, "sm", isNightTime(f.time))}
              <span className="metric-value text-xs text-foreground">{f.temperature}°</span>
              <span className="text-[10px] text-humidity">{f.humidity}%</span>
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
};

export default WeatherWidget;
