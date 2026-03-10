import { useState } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { LayoutDashboard, Brain, BarChart3, Settings, RefreshCw, AlertCircle, Zap, GitCompareArrows } from "lucide-react";
import { calcRoomDewpoint } from "@/lib/dewpoint";
import ZoneCard from "@/components/ZoneCard";
import WeatherWidget from "@/components/WeatherWidget";
import EnergyWidget from "@/components/EnergyWidget";
import OptimizationPanel from "@/components/OptimizationPanel";
import NetatmoWidget from "@/components/NetatmoWidget";
import ZoneClimateChart from "@/components/ZoneClimateChart";
import PredictionAccuracyChart from "@/components/PredictionAccuracyChart";
import DailyCostChart from "@/components/DailyCostChart";
import CorrelationTab from "@/components/CorrelationTab";
import EnergyTab from "@/components/EnergyTab";
import EmergencyStopButton from "@/components/EmergencyStopButton";
import { useWeather, useAirzoneZones, useLinkyData, useNetatmoData } from "@/hooks/useClimateData";
import { useDpPredictions, buildHeatingHistory, useThermalModels } from "@/hooks/useDpPredictions";
import { useRecentControlLogs } from "@/hooks/useControlData";
import { NETATMO_TO_AIRZONE, ZONE_DISPLAY_NAMES, NETATMO_IGNORE } from "@/data/sensorMapping";
import { NetatmoModule } from "@/types/climate";
import { zones as mockZones, weatherData as mockWeather } from "@/data/mockData";

const Dashboard = () => {
  const { data: weatherData, isLoading: weatherLoading, error: weatherError } = useWeather();
  const { data: airzoneZones, isLoading: zonesLoading, error: zonesError } = useAirzoneZones();
  const { data: linkyData, error: linkyError } = useLinkyData();
  const { data: netatmoData, error: netatmoError } = useNetatmoData();
  const { data: recentLogs } = useRecentControlLogs(20);

  const rawZones = airzoneZones || mockZones;
  const weather = weatherData || mockWeather;
  const energy = linkyData || null;

  const zones = rawZones.map((z) => ({
    ...z,
    name: ZONE_DISPLAY_NAMES[z.name] || z.name,
  }));

  // Netatmo lookup
  const netatmoByZone: Record<string, NetatmoModule> = {};
  if (netatmoData) {
    for (const mod of netatmoData) {
      if (NETATMO_IGNORE.includes(mod.name)) continue;
      const airzoneName = NETATMO_TO_AIRZONE[mod.name];
      if (airzoneName && airzoneName !== "__outdoor_contis__") {
        const displayName = ZONE_DISPLAY_NAMES[airzoneName] || airzoneName;
        netatmoByZone[displayName] = mod;
      }
    }
  }

  // Latest control log per zone for heating forecast
  const latestLogByZone: Record<string, { action: string; reason: string; forecastBestHour?: string | null }> = {};
  if (recentLogs) {
    for (const log of recentLogs) {
      const displayName = ZONE_DISPLAY_NAMES[log.zone_name] || log.zone_name;
      if (!latestLogByZone[displayName]) {
        latestLogByZone[displayName] = {
          action: log.action,
          reason: log.reason || "",
          forecastBestHour: log.forecast_best_hour,
        };
      }
    }
  }

  const outdoorNetatmo = netatmoData?.find((m) => NETATMO_TO_AIRZONE[m.name] === "__outdoor_contis__");

  const heatingHistory = buildHeatingHistory(recentLogs, ZONE_DISPLAY_NAMES);
  const { data: thermalModels } = useThermalModels();
  const { data: dpData } = useDpPredictions(zones, weather, netatmoByZone, heatingHistory, thermalModels);
  const dpPredictions = dpData?.predictions;
  const dpPredictions24h = dpData?.predictions_24h;

  const [selectedZone, setSelectedZone] = useState(zones[0]);

  const activeHeatingCount = zones.filter((z) => z.isHeating).length;

  const dpSpreads = zones.map((z) => {
    const netatmo = netatmoByZone[z.name];
    const dp = calcRoomDewpoint(z.temperature, z.humidity, netatmo?.temperature, netatmo?.humidity);
    return z.temperature - dp;
  });
  const avgDpSpread = dpSpreads.length > 0 ? dpSpreads.reduce((a, b) => a + b, 0) / dpSpreads.length : 0;
  const zonesAtRisk = dpSpreads.filter((s) => s <= 3).length;

  const hasErrors = weatherError || zonesError || linkyError || netatmoError;
  const isLive = !!(airzoneZones || weatherData || linkyData || netatmoData);

  return (
    <div className="min-h-screen bg-background">
      {/* Top bar */}
      <header className="border-b border-border px-6 py-4">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="h-8 w-8 rounded-lg bg-gradient-to-br from-heating to-heating-glow flex items-center justify-center">
              <span className="text-sm font-bold text-background">H</span>
            </div>
            <div>
              <h1 className="text-lg font-bold text-foreground leading-tight">HeatSmart</h1>
              <p className="text-[10px] text-muted-foreground uppercase tracking-widest">Humidity Control</p>
            </div>
          </div>

          <div className="flex items-center gap-6">
            <div className="hidden md:flex items-center gap-4 text-xs text-muted-foreground">
              <span>Avg ΔDP <span className={`metric-value ${avgDpSpread <= 3 ? "text-destructive" : avgDpSpread <= 5 ? "text-warning" : "text-success"}`}>{avgDpSpread.toFixed(1)}°</span></span>
              <span>At risk <span className="metric-value text-destructive">{zonesAtRisk}/{zones.length}</span></span>
              <span>Drying <span className="metric-value text-heating">{activeHeatingCount}/{zones.length}</span></span>
            </div>
            <div className="flex items-center gap-2">
              {(zonesLoading || weatherLoading) ? (
                <>
                  <RefreshCw className="h-3 w-3 text-muted-foreground animate-spin" />
                  <span className="text-xs text-muted-foreground">Loading...</span>
                </>
              ) : hasErrors ? (
                <>
                  <AlertCircle className="h-3 w-3 text-warning" />
                  <span className="text-xs text-warning">Some APIs unavailable</span>
                </>
              ) : (
                <>
                  <div className={`h-2 w-2 rounded-full ${isLive ? "bg-success" : "bg-warning"} status-pulse`} />
                  <span className="text-xs text-muted-foreground">{isLive ? "Live data" : "Mock data"}</span>
                </>
              )}
            </div>
          </div>
        </div>
      </header>

      {/* Content */}
      <main className="max-w-7xl mx-auto px-6 py-6">
        <Tabs defaultValue="dashboard" className="space-y-4">
          <div className="space-y-2 sm:space-y-0">
            <div className="flex flex-wrap items-center gap-2">
              <TabsList className="bg-secondary border border-border">
                <TabsTrigger value="dashboard" className="data-[state=active]:bg-card gap-1.5 text-xs">
                  <LayoutDashboard className="h-3.5 w-3.5" /> Dashboard
                </TabsTrigger>
                <TabsTrigger value="optimize" className="data-[state=active]:bg-card gap-1.5 text-xs">
                  <Brain className="h-3.5 w-3.5" /> Optimize
                </TabsTrigger>
                <TabsTrigger value="analytics" className="data-[state=active]:bg-card gap-1.5 text-xs">
                  <BarChart3 className="h-3.5 w-3.5" /> Analytics
                </TabsTrigger>
              </TabsList>
              <div className="flex items-center gap-2">
                <TabsList className="bg-secondary border border-border">
                  <TabsTrigger value="correlation" className="data-[state=active]:bg-card gap-1.5 text-xs">
                    <GitCompareArrows className="h-3.5 w-3.5" /> Correlation
                  </TabsTrigger>
                  <TabsTrigger value="energy" className="data-[state=active]:bg-card gap-1.5 text-xs">
                    <Zap className="h-3.5 w-3.5" /> Energy
                  </TabsTrigger>
                  <TabsTrigger value="settings" className="data-[state=active]:bg-card gap-1.5 text-xs">
                    <Settings className="h-3.5 w-3.5" /> Settings
                  </TabsTrigger>
                </TabsList>
                <EmergencyStopButton />
              </div>
            </div>
          </div>

          <TabsContent value="dashboard" className="space-y-3">
            {/* Error banner */}
            {hasErrors && (
              <div className="rounded-lg bg-warning/10 border border-warning/20 p-2 flex items-start gap-2">
                <AlertCircle className="h-3.5 w-3.5 text-warning shrink-0 mt-0.5" />
                <div className="text-xs text-warning space-y-0.5">
                  {zonesError && <p>Airzone: {(zonesError as Error).message}</p>}
                  {weatherError && <p>Weather: {(weatherError as Error).message}</p>}
                  {linkyError && <p>Linky: {(linkyError as Error).message}</p>}
                  {netatmoError && <p>Netatmo: {(netatmoError as Error).message}</p>}
                </div>
              </div>
            )}

            {/* Zones — full width */}
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
              {zones.map((zone) => (
                <ZoneCard
                  key={zone.id}
                  zone={zone}
                  netatmo={netatmoByZone[zone.name]}
                  heatingForecast={latestLogByZone[zone.name] || null}
                  dpPrediction={dpPredictions?.[zone.name] || null}
                  onClick={() => setSelectedZone(zone)}
                />
              ))}
            </div>

            {/* Chart — above info boxes */}
            {selectedZone && <ZoneClimateChart zone={selectedZone} />}

            {/* Info row: weather + energy + netatmo in equal columns */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <WeatherWidget data={weather} netatmoOutdoor={outdoorNetatmo} />
              {energy ? <EnergyWidget data={energy} /> : (
                <div className="glass-card rounded-xl p-5 flex items-center justify-center">
                  <p className="text-xs text-muted-foreground">No Linky data yet</p>
                </div>
              )}
              {netatmoData ? <NetatmoWidget modules={netatmoData} /> : (
                <div className="glass-card rounded-xl p-5 flex items-center justify-center">
                  <p className="text-xs text-muted-foreground">No Netatmo data yet</p>
                </div>
              )}
            </div>
          </TabsContent>

          <TabsContent value="optimize">
            <div className="max-w-2xl">
              <OptimizationPanel
                dpPredictions3h={dpPredictions}
                dpPredictions24h={dpPredictions24h}
                zones={zones}
              />
            </div>
          </TabsContent>

          <TabsContent value="analytics">
            <div className="space-y-4">
              {/* Insight panels */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                <PredictionAccuracyChart />
                <DailyCostChart />
              </div>

              {/* Zone climate histories */}
              <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider">
                Zone Climate History
              </h2>
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {zones.map((zone) => (
                  <ZoneClimateChart key={zone.id} zone={zone} />
                ))}
              </div>
            </div>
          </TabsContent>

          <TabsContent value="correlation">
            <CorrelationTab />
          </TabsContent>

          <TabsContent value="energy">
            {energy ? <EnergyTab data={energy} /> : (
              <div className="glass-card rounded-xl p-8 max-w-xl">
                <p className="text-sm text-muted-foreground">No Linky data available. Data has a 1-day delay from Enedis.</p>
              </div>
            )}
          </TabsContent>

          <TabsContent value="settings">
            <div className="glass-card rounded-xl p-8 max-w-xl">
              <h2 className="text-lg font-semibold text-foreground mb-2">API Connections</h2>
              <p className="text-sm text-muted-foreground mb-6">Real-time data sources status.</p>
              <div className="space-y-4">
                {[
                  { name: "Airzone Cloud", desc: "Heatpump & zone control", status: airzoneZones ? "connected" : zonesError ? "error" : "loading", error: zonesError },
                  { name: "Open-Meteo", desc: "Weather & forecast", status: weatherData ? "connected" : weatherError ? "error" : "loading", error: weatherError },
                  { name: "Linky / Enedis", desc: "Energy consumption", status: linkyData ? "connected" : linkyError ? "error" : "loading", error: linkyError },
                  { name: "Netatmo", desc: "Indoor climate sensors", status: netatmoData ? "connected" : netatmoError ? "error" : "loading", error: netatmoError },
                ].map((api) => (
                  <div key={api.name} className="flex items-center justify-between p-3 rounded-lg bg-secondary/50 border border-border">
                    <div>
                      <span className="text-sm font-medium text-foreground">{api.name}</span>
                      <p className="text-xs text-muted-foreground">{api.desc}</p>
                      {api.error && <p className="text-xs text-destructive mt-0.5">{(api.error as Error).message?.substring(0, 60)}</p>}
                    </div>
                    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${
                      api.status === "connected"
                        ? "bg-success/10 text-success"
                        : api.status === "error"
                        ? "bg-destructive/10 text-destructive"
                        : api.status === "loading"
                        ? "bg-warning/10 text-warning"
                        : "bg-secondary text-muted-foreground"
                    }`}>
                      {api.status === "connected" ? "✓ Connected" : api.status === "error" ? "✗ Error" : api.status === "loading" ? "Loading..." : "Not configured"}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </TabsContent>
        </Tabs>
      </main>
    </div>
  );
};

export default Dashboard;
