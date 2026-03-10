import { Card } from "@/components/ui/card";
import { Brain, Clock, Flame, Zap, Droplets, Home, TrendingDown, AlertCircle, Check, Euro, ThermometerSun, CalendarDays, ThermometerSnowflake, PiggyBank, ArrowRight } from "lucide-react";
import { useRecentControlLogs, useControlLogsByDay, useDailyAssessments, useSystemState } from "@/hooks/useControlData";
import { format, parseISO } from "date-fns";
import HeatingAnalysisPanel from "@/components/HeatingAnalysisPanel";
import DpTrendArrow from "@/components/DpTrendArrow";
import { DpPrediction } from "@/hooks/useDpPredictions";
import { Zone } from "@/types/climate";
import ReliabilityChart from "@/components/ReliabilityChart";

const TARIFF = 0.1927;

interface OptimizationPanelProps {
  dpPredictions3h?: Record<string, DpPrediction> | null;
  dpPredictions24h?: Record<string, DpPrediction> | null;
  zones?: Zone[];
}

const actionConfig: Record<string, { icon: any; label: string; color: string }> = {
  heating_on: { icon: Flame, label: "Heating On", color: "text-heating" },
  heating_off: { icon: Check, label: "Heating Off", color: "text-success" },
  defer_heating: { icon: Clock, label: "Deferred", color: "text-energy" },
  no_change: { icon: TrendingDown, label: "Idle", color: "text-muted-foreground" },
};

const OptimizationPanel = ({ dpPredictions3h, dpPredictions24h, zones }: OptimizationPanelProps) => {
  const { data: recentLogs, isLoading: logsLoading } = useRecentControlLogs(100);
  const { data: todayLogs } = useControlLogsByDay();
  const { data: assessments } = useDailyAssessments();
  const { data: systemState } = useSystemState();
  

  const heatingStats = systemState?.heating_stats || { totalOnMinutes: 0, totalCycles: 0, totalSaved: 0 };
  const occupancy = systemState?.occupancy_history || { occupied: false, signals: [] };

  // Today's stats
  const todayActions = todayLogs || [];
  const heatingOnCount = todayActions.filter((l) => l.action === "heating_on").length;
  const deferCount = todayActions.filter((l) => l.action === "defer_heating").length;
  const heatingMinutes = heatingOnCount * 5;
  const estKwh = (heatingMinutes / 60) * 2.5;
  const estCost = estKwh * TARIFF;

  // Savings from deferrals: each deferred cycle = 5min * 2.5kW avoided
  const savedKwh = (deferCount * 5 / 60) * 2.5;
  const savedEur = savedKwh * TARIFF;
  const savedPct = (estKwh + savedKwh) > 0 ? Math.round((savedKwh / (estKwh + savedKwh)) * 100) : 0;

  // Latest forecast info
  const latest = recentLogs?.[0];
  const forecastBestHour = latest?.forecast_best_hour;
  const forecastTempMax = latest?.forecast_temp_max;
  const currentOutdoorTemp = latest?.outdoor_temp;

  // Last distinct actions (not no_change)
  const recentActions = (recentLogs || [])
    .filter((l) => l.action !== "no_change")
    .slice(0, 10);

  return (
    <div className="space-y-4">
      {/* AI Status Header */}
      <Card className="glass-card p-5 glow-energy border-energy/15">
        <div className="flex items-center gap-3 mb-3">
          <div className="h-10 w-10 rounded-xl bg-energy/10 flex items-center justify-center">
            <Brain className="h-5 w-5 text-energy" />
          </div>
          <div>
            <h3 className="font-semibold text-foreground">Autonomous Humidity Controller</h3>
            <p className="text-xs text-muted-foreground">
              Runs every 5 min · DP spread triggers · 18°C cap with predictive shutoff
            </p>
          </div>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4">
          <div className="rounded-lg bg-secondary/50 p-3">
            <div className="flex items-center gap-1.5 mb-1">
              <Flame className="h-3.5 w-3.5 text-heating" />
              <span className="text-[10px] text-muted-foreground uppercase">Heating today</span>
            </div>
            <span className="metric-value text-lg text-foreground">{heatingMinutes}</span>
            <span className="text-xs text-muted-foreground ml-1">min</span>
          </div>
          <div className="rounded-lg bg-secondary/50 p-3">
            <div className="flex items-center gap-1.5 mb-1">
              <Zap className="h-3.5 w-3.5 text-energy" />
              <span className="text-[10px] text-muted-foreground uppercase">Est. energy</span>
            </div>
            <span className="metric-value text-lg text-foreground">{estKwh.toFixed(1)}</span>
            <span className="text-xs text-muted-foreground ml-1">kWh</span>
          </div>
          <div className="rounded-lg bg-secondary/50 p-3">
            <div className="flex items-center gap-1.5 mb-1">
              <Euro className="h-3.5 w-3.5 text-foreground" />
              <span className="text-[10px] text-muted-foreground uppercase">Est. cost</span>
            </div>
            <span className="metric-value text-lg text-foreground">€{estCost.toFixed(2)}</span>
          </div>
          <div className="rounded-lg bg-secondary/50 p-3">
            <div className="flex items-center gap-1.5 mb-1">
              <TrendingDown className="h-3.5 w-3.5 text-success" />
              <span className="text-[10px] text-muted-foreground uppercase">Deferrals</span>
            </div>
            <span className="metric-value text-lg text-success">{deferCount}</span>
            <span className="text-xs text-muted-foreground ml-1">cycles</span>
          </div>
        </div>
      </Card>

      {/* Savings Card */}
      <Card className="glass-card p-5 border-success/15">
        <div className="flex items-center gap-3 mb-3">
          <div className="h-10 w-10 rounded-xl bg-success/10 flex items-center justify-center">
            <PiggyBank className="h-5 w-5 text-success" />
          </div>
          <div>
            <h3 className="font-semibold text-foreground">Estimated Savings</h3>
            <p className="text-xs text-muted-foreground">
              From smart deferrals to warmer outdoor windows
            </p>
          </div>
        </div>
        <div className="grid grid-cols-3 gap-3">
          <div className="rounded-lg bg-secondary/50 p-3">
            <div className="flex items-center gap-1.5 mb-1">
              <TrendingDown className="h-3.5 w-3.5 text-success" />
              <span className="text-[10px] text-muted-foreground uppercase">Saved</span>
            </div>
            <span className="metric-value text-lg text-success">{savedPct}%</span>
            <span className="text-xs text-muted-foreground ml-1">of total</span>
          </div>
          <div className="rounded-lg bg-secondary/50 p-3">
            <div className="flex items-center gap-1.5 mb-1">
              <Zap className="h-3.5 w-3.5 text-success" />
              <span className="text-[10px] text-muted-foreground uppercase">Energy saved</span>
            </div>
            <span className="metric-value text-lg text-success">{savedKwh.toFixed(1)}</span>
            <span className="text-xs text-muted-foreground ml-1">kWh</span>
          </div>
          <div className="rounded-lg bg-secondary/50 p-3">
            <div className="flex items-center gap-1.5 mb-1">
              <Euro className="h-3.5 w-3.5 text-success" />
              <span className="text-[10px] text-muted-foreground uppercase">Money saved</span>
            </div>
            <span className="metric-value text-lg text-success">€{savedEur.toFixed(2)}</span>
          </div>
        </div>
      </Card>

      {/* DP Spread Forecasts + Reliability */}
      {zones && zones.length > 0 && (
        <div className="space-y-4">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {/* 3h Forecast */}
            <Card className="glass-card p-4 border-humidity/15">
              <div className="flex items-center gap-2 mb-3">
                <Clock className="h-4 w-4 text-humidity" />
                <span className="text-sm font-semibold text-foreground">3h DP Spread Forecast</span>
              </div>
              {dpPredictions3h && Object.keys(dpPredictions3h).length > 0 ? (
                <div className="space-y-2">
                  {zones.map((zone) => {
                    const pred = dpPredictions3h[zone.name];
                    if (!pred) return null;
                    const spreadColor = pred.predicted_dp_spread <= 3 ? "text-destructive" : pred.predicted_dp_spread <= 5 ? "text-warning" : "text-success";
                    return (
                      <div key={zone.id} className="flex items-center justify-between py-1.5 border-b border-border last:border-0">
                        <span className="text-xs text-foreground truncate flex-1">{zone.name}</span>
                        <div className="flex items-center gap-2">
                          <span className="text-[10px] text-muted-foreground">Δ{pred.current_dp_spread}°</span>
                          <ArrowRight className="h-3 w-3 text-muted-foreground" />
                          <span className={`text-xs font-semibold metric-value ${spreadColor}`}>Δ{pred.predicted_dp_spread}°</span>
                          <DpTrendArrow
                            trend={pred.trend}
                            confidence={pred.confidence}
                            factors={pred.factors}
                            isLearning={pred.isLearning}
                          />
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">Waiting for prediction data…</p>
              )}
            </Card>

            {/* 24h Forecast */}
            <Card className="glass-card p-4 border-energy/15">
              <div className="flex items-center gap-2 mb-3">
                <CalendarDays className="h-4 w-4 text-energy" />
                <span className="text-sm font-semibold text-foreground">24h DP Spread Forecast</span>
              </div>
              {dpPredictions24h && Object.keys(dpPredictions24h).length > 0 ? (
                <div className="space-y-2">
                  {zones.map((zone) => {
                    const pred = dpPredictions24h[zone.name];
                    if (!pred) return null;
                    const spreadColor = pred.predicted_dp_spread <= 3 ? "text-destructive" : pred.predicted_dp_spread <= 5 ? "text-warning" : "text-success";
                    return (
                      <div key={zone.id} className="flex items-center justify-between py-1.5 border-b border-border last:border-0">
                        <span className="text-xs text-foreground truncate flex-1">{zone.name}</span>
                        <div className="flex items-center gap-2">
                          <span className="text-[10px] text-muted-foreground">Δ{pred.current_dp_spread}°</span>
                          <ArrowRight className="h-3 w-3 text-muted-foreground" />
                          <span className={`text-xs font-semibold metric-value ${spreadColor}`}>Δ{pred.predicted_dp_spread}°</span>
                          <DpTrendArrow
                            trend={pred.trend}
                            confidence={pred.confidence}
                            factors={pred.factors}
                            isLearning={pred.isLearning}
                          />
                        </div>
                      </div>
                    );
                  })}
                  <p className="text-[9px] text-muted-foreground mt-2 italic">
                    24h predictions use the same learned thermal model. Confidence is scaled down due to longer horizon.
                  </p>
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">Waiting for prediction data…</p>
              )}
            </Card>
          </div>

          {/* Reliability over time graph */}
          <ReliabilityChart />
        </div>
      )}

      {/* Forecast & Occupancy */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <Card className="glass-card p-4">
          <div className="flex items-center gap-2 mb-2">
            <ThermometerSun className="h-4 w-4 text-energy" />
            <span className="text-sm font-semibold text-foreground">24h Lookahead</span>
          </div>
          {forecastBestHour ? (
            <div className="space-y-1">
              <p className="text-sm text-secondary-foreground">
                Best heating window: <span className="metric-value text-foreground">{forecastTempMax}°C</span>
              </p>
              <p className="text-xs text-muted-foreground">
                At {forecastBestHour ? format(parseISO(forecastBestHour), "HH:mm EEE") : "—"}
              </p>
              <p className="text-xs text-muted-foreground">
                Current outdoor: {currentOutdoorTemp}°C. Heatpump COP improves with warmer outdoor temps.
              </p>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">No forecast data yet. Waiting for first control cycle.</p>
          )}
        </Card>

        <Card className="glass-card p-4">
          <div className="flex items-center gap-2 mb-2">
            <Home className="h-4 w-4 text-primary" />
            <span className="text-sm font-semibold text-foreground">House Status</span>
          </div>
          <div className="flex items-center gap-2 mb-1">
            <div className={`h-2.5 w-2.5 rounded-full ${occupancy.occupied ? "bg-success status-pulse" : "bg-muted-foreground"}`} />
            <span className="text-sm text-foreground">{occupancy.occupied ? "Occupied" : "Empty (unattended mode)"}</span>
          </div>
          <p className="text-xs text-muted-foreground mt-1">
            No ventilation suggestions — house is empty. System operates autonomously with heating only.
          </p>
        </Card>
      </div>

      {/* Control Rules */}
      <Card className="glass-card p-4">
        <div className="flex items-center gap-2 mb-2">
          <ThermometerSnowflake className="h-4 w-4 text-cold" />
          <span className="text-sm font-semibold text-foreground">Active Rules</span>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs">
          <div className="flex items-center gap-2">
            <div className="h-1.5 w-1.5 rounded-full bg-destructive" />
            <span className="text-secondary-foreground">Heat ON when DP spread &lt; 4°C (condensation risk)</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="h-1.5 w-1.5 rounded-full bg-success" />
            <span className="text-secondary-foreground">Heat OFF when DP spread ≥ 6°C (safe)</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="h-1.5 w-1.5 rounded-full bg-heating" />
            <span className="text-secondary-foreground">Critical override: heat immediately if spread ≤ 2°C</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="h-1.5 w-1.5 rounded-full bg-cold" />
            <span className="text-secondary-foreground">No heating if indoor &gt;18°C (predictive shutoff at 17°C)</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="h-1.5 w-1.5 rounded-full bg-energy" />
            <span className="text-secondary-foreground">Defer to warmer outdoor for better COP (unless critical)</span>
          </div>
        </div>
      </Card>

      {/* Recent Actions */}
      <Card className="glass-card p-5">
        <h4 className="text-sm font-semibold text-foreground mb-3">Recent Actions</h4>
        {logsLoading ? (
          <p className="text-xs text-muted-foreground">Loading control log…</p>
        ) : recentActions.length === 0 ? (
          <p className="text-xs text-muted-foreground">No actions yet. The controller runs every 5 minutes via cron.</p>
        ) : (
          <div className="space-y-2">
            {recentActions.map((log) => {
              const config = actionConfig[log.action] || actionConfig.no_change;
              const Icon = config.icon;
              return (
                <div key={log.id} className="flex items-start gap-3 py-2 border-b border-border last:border-0">
                  <div className="h-7 w-7 rounded-lg bg-secondary flex items-center justify-center shrink-0 mt-0.5">
                    <Icon className={`h-3.5 w-3.5 ${config.color}`} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className={`text-xs font-medium ${config.color}`}>{config.label}</span>
                      <span className="text-xs text-muted-foreground">· {log.zone_name}</span>
                      <span className="text-xs text-muted-foreground ml-auto">
                        {format(parseISO(log.created_at), "HH:mm")}
                      </span>
                    </div>
                    <p className="text-xs text-secondary-foreground leading-relaxed mt-0.5">{log.reason}</p>
                    {(log.energy_saved_pct ?? 0) > 0 && (
                      <span className="text-[10px] text-success metric-value">~{log.energy_saved_pct}% energy saved by deferring</span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Card>

      {/* Daily Assessments */}
      <Card className="glass-card p-5">
        <div className="flex items-center gap-2 mb-3">
          <CalendarDays className="h-4 w-4 text-primary" />
          <h4 className="text-sm font-semibold text-foreground">Daily Assessments</h4>
        </div>
        {(!assessments || assessments.length === 0) ? (
          <div className="space-y-2">
            <p className="text-xs text-muted-foreground">
              First assessment will run after 7 days of data collection, then daily.
            </p>
            <div className="h-1.5 w-full rounded-full bg-secondary overflow-hidden">
              <div className="h-full rounded-full bg-energy/50 transition-all" style={{ width: "10%" }} />
            </div>
            <p className="text-[10px] text-muted-foreground">Collecting baseline data…</p>
          </div>
        ) : (
          <div className="space-y-3">
            {assessments.slice(0, 7).map((a) => (
              <div key={a.id} className="rounded-lg bg-secondary/50 border border-border p-3">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-medium text-foreground">{a.date}</span>
                  <span className={`text-[10px] font-medium px-2 py-0.5 rounded-full ${
                    a.humidity_improved ? "bg-success/10 text-success" : "bg-warning/10 text-warning"
                  }`}>
                    {a.humidity_improved ? "✓ Improved" : "✗ No improvement"}
                  </span>
                </div>
                <div className="grid grid-cols-3 gap-2 text-[10px] mt-2">
                  <div>
                    <span className="text-muted-foreground">Humidity</span>
                    <p className="metric-value text-foreground">{a.avg_humidity_before}% → {a.avg_humidity_after}%</p>
                  </div>
                  <div>
                    <span className="text-muted-foreground">Heating</span>
                    <p className="metric-value text-foreground">{a.heating_minutes}min · {a.total_heating_kwh}kWh</p>
                  </div>
                  <div>
                    <span className="text-muted-foreground">Cost</span>
                    <p className="metric-value text-foreground">€{a.total_cost_eur}</p>
                  </div>
                </div>
                {a.notes && <p className="text-xs text-secondary-foreground mt-2">{a.notes}</p>}
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Heating Effectiveness Analysis */}
      <HeatingAnalysisPanel />
    </div>
  );
};

export default OptimizationPanel;
