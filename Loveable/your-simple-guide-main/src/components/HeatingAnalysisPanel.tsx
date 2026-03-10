import { Card } from "@/components/ui/card";
import { useHeatingAnalysis } from "@/hooks/useHeatingAnalysis";
import {
  FlaskConical,
  TrendingDown,
  TrendingUp,
  Thermometer,
  BarChart3,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Shield,
  Loader2,
  Wind,
} from "lucide-react";

const confidenceColors: Record<string, string> = {
  high: "bg-success/10 text-success",
  medium: "bg-warning/10 text-warning",
  low: "bg-muted text-muted-foreground",
};

const statusColors: Record<string, { bg: string; text: string; label: string }> = {
  scheduled: { bg: "bg-energy/10", text: "text-energy", label: "Scheduled" },
  active: { bg: "bg-warning/10", text: "text-warning", label: "Active" },
  completed: { bg: "bg-success/10", text: "text-success", label: "Completed" },
};

const HeatingAnalysisPanel = () => {
  const { data: analysis, isLoading, error } = useHeatingAnalysis();

  if (isLoading) {
    return (
      <Card className="glass-card p-5">
        <div className="flex items-center gap-3">
          <Loader2 className="h-5 w-5 text-energy animate-spin" />
          <span className="text-sm text-muted-foreground">Running heating effectiveness analysis…</span>
        </div>
      </Card>
    );
  }

  if (error || !analysis) {
    return (
      <Card className="glass-card p-5">
        <div className="flex items-center gap-3">
          <AlertTriangle className="h-5 w-5 text-warning" />
          <span className="text-sm text-muted-foreground">
            {analysis?.status === "insufficient_data"
              ? "Not enough data yet for analysis. Keep collecting."
              : "Could not load analysis."}
          </span>
        </div>
      </Card>
    );
  }

  const { heatingEffectiveness: eff, runoffAnalysis: runoff, passiveReactivity, conclusions, experiments } = analysis;

  return (
    <div className="space-y-4">
      {/* Conclusions Card */}
      <Card className="glass-card p-5 border-energy/15 glow-energy">
        <div className="flex items-center gap-3 mb-3">
          <div className="h-10 w-10 rounded-xl bg-energy/10 flex items-center justify-center">
            <BarChart3 className="h-5 w-5 text-energy" />
          </div>
          <div className="flex-1">
            <h3 className="font-semibold text-foreground">DP Spread Effectiveness Analysis</h3>
            <p className="text-xs text-muted-foreground">
              {Math.round(analysis.daysOfData)} days of data · {analysis.totalDataPoints.toLocaleString()} readings
            </p>
          </div>
          <span className={`text-[10px] font-medium px-2 py-0.5 rounded-full ${confidenceColors[conclusions.confidence]}`}>
            {conclusions.confidence} confidence
          </span>
        </div>

        <div className="rounded-lg bg-secondary/50 border border-border p-3 mb-3">
          <p className="text-sm text-foreground leading-relaxed">{conclusions.summary}</p>
        </div>

        {conclusions.recommendations.length > 0 && (
          <div className="space-y-1.5">
            <span className="text-[10px] font-medium text-muted-foreground uppercase">Recommendations</span>
            {conclusions.recommendations.map((rec, i) => (
              <div key={i} className="flex items-start gap-2">
                <Shield className="h-3 w-3 text-energy mt-0.5 shrink-0" />
                <p className="text-xs text-secondary-foreground leading-relaxed">{rec}</p>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Heating vs No-Heating — RH + DP Spread, weather-normalized */}
      <Card className="glass-card p-5">
        <h4 className="text-sm font-semibold text-foreground mb-3">Heating vs No-Heating (Weather-Normalized)</h4>

        {/* Raw averages grid: RH row + DP spread row */}
        <div className="grid grid-cols-2 gap-3 mb-3">
          {/* With Heating */}
          <div className="rounded-lg bg-secondary/50 p-3">
            <div className="flex items-center gap-1.5 mb-2">
              <TrendingDown className="h-3.5 w-3.5 text-heating" />
              <span className="text-[10px] text-muted-foreground uppercase">With heating</span>
            </div>
            <div className="space-y-1.5">
              <div>
                <span className="metric-value text-lg text-foreground">
                  {eff.avgHumidityWithHeating != null ? `${eff.avgHumidityWithHeating}%` : "—"}
                </span>
                <span className="text-[10px] text-muted-foreground ml-1">avg RH</span>
              </div>
              <div>
                <span className="metric-value text-lg text-foreground">
                  {eff.avgDpSpreadWithHeating != null ? `${eff.avgDpSpreadWithHeating}°C` : "—"}
                </span>
                <span className="text-[10px] text-muted-foreground ml-1">avg DP spread</span>
              </div>
            </div>
            <p className="text-[10px] text-muted-foreground mt-1">{eff.heatingDaysCount} days</p>
          </div>

          {/* Without Heating */}
          <div className="rounded-lg bg-secondary/50 p-3">
            <div className="flex items-center gap-1.5 mb-2">
              <TrendingUp className="h-3.5 w-3.5 text-cold" />
              <span className="text-[10px] text-muted-foreground uppercase">Without heating</span>
            </div>
            <div className="space-y-1.5">
              <div>
                <span className="metric-value text-lg text-foreground">
                  {eff.avgHumidityWithoutHeating != null ? `${eff.avgHumidityWithoutHeating}%` : "—"}
                </span>
                <span className="text-[10px] text-muted-foreground ml-1">avg RH</span>
              </div>
              <div>
                <span className="metric-value text-lg text-foreground">
                  {eff.avgDpSpreadWithoutHeating != null ? `${eff.avgDpSpreadWithoutHeating}°C` : "—"}
                </span>
                <span className="text-[10px] text-muted-foreground ml-1">avg DP spread</span>
              </div>
            </div>
            <p className="text-[10px] text-muted-foreground mt-1">{eff.noHeatingDaysCount} days</p>
          </div>
        </div>

        {/* Weather-normalized diffs */}
        {eff.weatherNormalized?.paired > 0 && (
          <div className="rounded-lg bg-energy/5 border border-energy/10 p-3 mb-3">
            <div className="flex items-center gap-1.5 mb-2">
              <CheckCircle2 className={`h-3.5 w-3.5 ${eff.weatherNormalized.significant ? "text-success" : "text-muted-foreground"}`} />
              <span className="text-[10px] text-muted-foreground uppercase">Weather-normalized diff ({eff.weatherNormalized.paired} pairs)</span>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <span className="metric-value text-lg text-foreground">
                  {eff.weatherNormalized.avgRhDiff != null ? `${eff.weatherNormalized.avgRhDiff > 0 ? "+" : ""}${eff.weatherNormalized.avgRhDiff}%` : "—"}
                </span>
                <span className="text-[10px] text-muted-foreground ml-1">RH diff</span>
              </div>
              <div>
                <span className="metric-value text-lg text-foreground">
                  {eff.weatherNormalized.avgDpSpreadDiff != null ? `${eff.weatherNormalized.avgDpSpreadDiff > 0 ? "+" : ""}${eff.weatherNormalized.avgDpSpreadDiff}°C` : "—"}
                </span>
                <span className="text-[10px] text-muted-foreground ml-1">DP spread diff</span>
              </div>
            </div>
          </div>
        )}

        {eff.weatherNormalized?.description && (
          <p className="text-xs text-secondary-foreground mb-3 leading-relaxed">{eff.weatherNormalized.description}</p>
        )}

        {/* Factor breakdown */}
        {eff.weatherNormalized?.factors && (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
            {eff.weatherNormalized.factors.byOutdoorHumidity && (
              <div className="rounded-md bg-muted/50 p-2 text-[10px]">
                <span className="text-muted-foreground font-medium">By outdoor RH</span>
                <div className="mt-1 space-y-0.5">
                  <div className="flex justify-between gap-1">
                    <span className="text-muted-foreground shrink-0">Dry (&lt;70%)</span>
                    <span className="metric-value text-foreground text-right">
                      {eff.weatherNormalized.factors.byOutdoorHumidity.dry?.avgRhDiff ?? "—"}%
                      {eff.weatherNormalized.factors.byOutdoorHumidity.dry?.avgDpSpreadDiff != null && (
                        <> · {eff.weatherNormalized.factors.byOutdoorHumidity.dry.avgDpSpreadDiff > 0 ? "+" : ""}{eff.weatherNormalized.factors.byOutdoorHumidity.dry.avgDpSpreadDiff}°</>
                      )}
                      <span className="text-muted-foreground"> ({eff.weatherNormalized.factors.byOutdoorHumidity.dry?.pairs}p)</span>
                    </span>
                  </div>
                  <div className="flex justify-between gap-1">
                    <span className="text-muted-foreground shrink-0">Wet (≥70%)</span>
                    <span className="metric-value text-foreground text-right">
                      {eff.weatherNormalized.factors.byOutdoorHumidity.wet?.avgRhDiff ?? "—"}%
                      {eff.weatherNormalized.factors.byOutdoorHumidity.wet?.avgDpSpreadDiff != null && (
                        <> · {eff.weatherNormalized.factors.byOutdoorHumidity.wet.avgDpSpreadDiff > 0 ? "+" : ""}{eff.weatherNormalized.factors.byOutdoorHumidity.wet.avgDpSpreadDiff}°</>
                      )}
                      <span className="text-muted-foreground"> ({eff.weatherNormalized.factors.byOutdoorHumidity.wet?.pairs}p)</span>
                    </span>
                  </div>
                </div>
              </div>
            )}
            {eff.weatherNormalized.factors.byOutdoorTemp && (
              <div className="rounded-md bg-muted/50 p-2 text-[10px]">
                <span className="text-muted-foreground font-medium">By outdoor temp</span>
                <div className="mt-1 space-y-0.5">
                  <div className="flex justify-between gap-1">
                    <span className="text-muted-foreground shrink-0">Cold (&lt;8°C)</span>
                    <span className="metric-value text-foreground text-right">
                      {eff.weatherNormalized.factors.byOutdoorTemp.cold?.avgRhDiff ?? "—"}%
                      {eff.weatherNormalized.factors.byOutdoorTemp.cold?.avgDpSpreadDiff != null && (
                        <> · {eff.weatherNormalized.factors.byOutdoorTemp.cold.avgDpSpreadDiff > 0 ? "+" : ""}{eff.weatherNormalized.factors.byOutdoorTemp.cold.avgDpSpreadDiff}°</>
                      )}
                      <span className="text-muted-foreground"> ({eff.weatherNormalized.factors.byOutdoorTemp.cold?.pairs}p)</span>
                    </span>
                  </div>
                  <div className="flex justify-between gap-1">
                    <span className="text-muted-foreground shrink-0">Warm (≥8°C)</span>
                    <span className="metric-value text-foreground text-right">
                      {eff.weatherNormalized.factors.byOutdoorTemp.warm?.avgRhDiff ?? "—"}%
                      {eff.weatherNormalized.factors.byOutdoorTemp.warm?.avgDpSpreadDiff != null && (
                        <> · {eff.weatherNormalized.factors.byOutdoorTemp.warm.avgDpSpreadDiff > 0 ? "+" : ""}{eff.weatherNormalized.factors.byOutdoorTemp.warm.avgDpSpreadDiff}°</>
                      )}
                      <span className="text-muted-foreground"> ({eff.weatherNormalized.factors.byOutdoorTemp.warm?.pairs}p)</span>
                    </span>
                  </div>
                </div>
              </div>
            )}
            {eff.weatherNormalized.factors.byOutdoorDewpoint && (
              <div className="rounded-md bg-muted/50 p-2 text-[10px]">
                <span className="text-muted-foreground font-medium">By outdoor dewpoint</span>
                <div className="mt-1 space-y-0.5">
                  <div className="flex justify-between gap-1">
                    <span className="text-muted-foreground shrink-0">Low (&lt;5°C)</span>
                    <span className="metric-value text-foreground text-right">
                      {eff.weatherNormalized.factors.byOutdoorDewpoint.low?.avgRhDiff ?? "—"}%
                      <span className="text-muted-foreground"> ({eff.weatherNormalized.factors.byOutdoorDewpoint.low?.pairs}p)</span>
                    </span>
                  </div>
                  <div className="flex justify-between gap-1">
                    <span className="text-muted-foreground shrink-0">High (≥5°C)</span>
                    <span className="metric-value text-foreground text-right">
                      {eff.weatherNormalized.factors.byOutdoorDewpoint.high?.avgRhDiff ?? "—"}%
                      <span className="text-muted-foreground"> ({eff.weatherNormalized.factors.byOutdoorDewpoint.high?.pairs}p)</span>
                    </span>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </Card>

      {/* Thermal Runoff */}
      {runoff.samples > 0 && (
        <Card className="glass-card p-5">
          <div className="flex items-center gap-2 mb-3">
            <Thermometer className="h-4 w-4 text-heating" />
            <h4 className="text-sm font-semibold text-foreground">Thermal Runoff (Concrete Inertia)</h4>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 mb-3">
            <div className="rounded-lg bg-secondary/50 p-3">
              <span className="text-[10px] text-muted-foreground uppercase block mb-1">Runoff duration</span>
              <span className="metric-value text-lg text-foreground">{runoff.estimatedRunoffHours}h</span>
            </div>
            <div className="rounded-lg bg-secondary/50 p-3">
              <span className="text-[10px] text-muted-foreground uppercase block mb-1">Decay rate</span>
              <span className="metric-value text-lg text-foreground">{runoff.tempDecayRate}°C/h</span>
            </div>
            <div className="rounded-lg bg-secondary/50 p-3">
              <span className="text-[10px] text-muted-foreground uppercase block mb-1">Samples</span>
              <span className="metric-value text-lg text-foreground">{runoff.samples}</span>
            </div>
          </div>
          <p className="text-xs text-secondary-foreground leading-relaxed mb-3">{runoff.description}</p>
          {runoff.factors && (
            <div className="grid grid-cols-2 gap-2">
              {runoff.factors.outdoorTempEffect && (
                <div className="rounded-md bg-muted/50 p-2 text-[10px]">
                  <span className="text-muted-foreground font-medium">By outdoor temp</span>
                  <div className="mt-1 space-y-0.5">
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">Cold</span>
                      <span className="metric-value text-foreground">{runoff.factors.outdoorTempEffect.coldAvgRunoffH ?? "—"}h</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">Warm</span>
                      <span className="metric-value text-foreground">{runoff.factors.outdoorTempEffect.warmAvgRunoffH ?? "—"}h</span>
                    </div>
                  </div>
                </div>
              )}
              {runoff.factors.heatingDurationEffect && (
                <div className="rounded-md bg-muted/50 p-2 text-[10px]">
                  <span className="text-muted-foreground font-medium">By heating duration</span>
                  <div className="mt-1 space-y-0.5">
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">&lt;30min</span>
                      <span className="metric-value text-foreground">{runoff.factors.heatingDurationEffect.shortAvgRunoffH ?? "—"}h</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">≥30min</span>
                      <span className="metric-value text-foreground">{runoff.factors.heatingDurationEffect.longAvgRunoffH ?? "—"}h</span>
                    </div>
                  </div>
                </div>
              )}
              {runoff.factors.byZone && Object.keys(runoff.factors.byZone).length > 0 && (
                <div className="rounded-md bg-muted/50 p-2 text-[10px] col-span-2">
                  <span className="text-muted-foreground font-medium">By zone</span>
                  <div className="mt-1.5 grid grid-cols-[1fr_auto_auto_auto] gap-x-3 gap-y-0.5 items-center">
                    {/* Column headers */}
                    <span className="text-muted-foreground font-medium">Zone</span>
                    <span className="text-muted-foreground font-medium text-right">Runoff</span>
                    <span className="text-muted-foreground font-medium text-right">Peak rise</span>
                    <span className="text-muted-foreground font-medium text-right">Samples</span>
                    {Object.entries(runoff.factors.byZone as Record<string, { avgRunoff: number; avgRise: number; count: number }>).map(([zone, d]) => (
                      <>
                        <span key={`${zone}-name`} className="text-muted-foreground truncate">{zone}</span>
                        <span key={`${zone}-runoff`} className={`metric-value text-right ${d.count > 0 ? "text-foreground" : "text-muted-foreground"}`}>
                          {d.count > 0 ? `${d.avgRunoff.toFixed(1)}h` : "—"}
                        </span>
                        <span key={`${zone}-rise`} className={`metric-value text-right ${d.count > 0 ? "text-foreground" : "text-muted-foreground"}`}>
                          {d.count > 0 ? `+${d.avgRise.toFixed(1)}°` : "—"}
                        </span>
                        <span key={`${zone}-count`} className="text-muted-foreground text-right">{d.count}</span>
                      </>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </Card>
      )}

      {/* Passive Thermal Reactivity */}
      {passiveReactivity && (
        <Card className="glass-card p-5">
          <div className="flex items-center gap-2 mb-3">
            <Wind className="h-4 w-4 text-cold" />
            <h4 className="text-sm font-semibold text-foreground">Passive Thermal Reactivity</h4>
            <span className="text-[10px] text-muted-foreground">(no heating)</span>
          </div>

          <p className="text-xs text-secondary-foreground leading-relaxed mb-3">{passiveReactivity.description}</p>

          {passiveReactivity.overall && (
            <div className="grid grid-cols-3 gap-3 mb-3">
              <div className="rounded-lg bg-secondary/50 p-3">
                <span className="text-[10px] text-muted-foreground uppercase block mb-1">Coupling factor</span>
                <span className="metric-value text-lg text-foreground">{passiveReactivity.overall.avgCouplingFactor}</span>
                <span className="text-[10px] text-muted-foreground ml-1">°C/°C</span>
                <p className="text-[10px] text-muted-foreground mt-0.5">
                  {passiveReactivity.overall.avgCouplingFactor < 0.3 ? "Well insulated" :
                   passiveReactivity.overall.avgCouplingFactor < 0.6 ? "Moderate" : "Poor insulation"}
                </p>
              </div>
              <div className="rounded-lg bg-secondary/50 p-3">
                <span className="text-[10px] text-muted-foreground uppercase block mb-1">Passive stretches</span>
                <span className="metric-value text-lg text-foreground">{passiveReactivity.overall.samples}</span>
              </div>
              <div className="rounded-lg bg-secondary/50 p-3">
                <span className="text-[10px] text-muted-foreground uppercase block mb-1">Lag</span>
                <span className="metric-value text-lg text-foreground">{passiveReactivity.overall.avgLagHours}h</span>
                <p className="text-[10px] text-muted-foreground mt-0.5">est. response delay</p>
              </div>
            </div>
          )}

          {Object.keys(passiveReactivity.byZone).length > 0 && (
            <div className="rounded-md bg-muted/50 p-2 text-[10px]">
              <span className="text-muted-foreground font-medium">By zone</span>
              <div className="mt-1.5 grid grid-cols-[1fr_auto_auto_auto] gap-x-3 gap-y-0.5 items-center">
                <span className="text-muted-foreground font-medium">Zone</span>
                <span className="text-muted-foreground font-medium text-right">Coupling</span>
                <span className="text-muted-foreground font-medium text-right">Drift</span>
                <span className="text-muted-foreground font-medium text-right">Samples</span>
                {Object.entries(passiveReactivity.byZone).map(([zone, d]) => (
                  <>
                    <span key={`${zone}-n`} className="text-muted-foreground truncate">{zone}</span>
                    <span key={`${zone}-c`} className={`metric-value text-right ${d.samples > 0 ? "text-foreground" : "text-muted-foreground"}`}>
                      {d.samples > 0 ? `${d.avgCouplingFactor}` : "—"}
                    </span>
                    <span key={`${zone}-d`} className={`metric-value text-right ${d.samples > 0 ? (d.avgDecayRate < 0 ? "text-cold" : "text-heating") : "text-muted-foreground"}`}>
                      {d.samples > 0 ? `${d.avgDecayRate > 0 ? "+" : ""}${d.avgDecayRate}°/h` : "—"}
                    </span>
                    <span key={`${zone}-s`} className="text-muted-foreground text-right">{d.samples}</span>
                  </>
                ))}
              </div>
              <p className="text-[10px] text-muted-foreground mt-2">
                <strong>Coupling</strong> = °C indoor change per °C outdoor change (lower = better insulation).
                <strong> Drift</strong> = avg °C/h indoor temp change during passive periods (negative = cooling).
              </p>
            </div>
          )}
        </Card>
      )}

      {/* Experiments */}
      <Card className="glass-card p-5">
        <div className="flex items-center gap-2 mb-3">
          <FlaskConical className="h-4 w-4 text-energy" />
          <h4 className="text-sm font-semibold text-foreground">No-Heating Experiments</h4>
        </div>

        {experiments.length === 0 ? (
          <div className="rounded-lg bg-secondary/50 border border-border p-3">
            <div className="flex items-center gap-2 mb-1">
              <Clock className="h-3.5 w-3.5 text-muted-foreground" />
              <span className="text-xs text-muted-foreground">No experiments yet</span>
            </div>
            <p className="text-xs text-secondary-foreground">
              {analysis.daysOfData < 14
                ? `Need ${Math.ceil(14 - analysis.daysOfData)} more days of data before auto-scheduling.`
                : "First experiment will be auto-scheduled soon."}
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {experiments.map((exp) => {
              const st = statusColors[exp.status] || statusColors.scheduled;
              return (
                <div key={exp.id} className="rounded-lg bg-secondary/50 border border-border p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-medium text-foreground">
                        {exp.start_date} → {exp.end_date}
                      </span>
                      <span className={`text-[10px] font-medium px-2 py-0.5 rounded-full ${st.bg} ${st.text}`}>
                        {st.label}
                      </span>
                    </div>
                  </div>

                  {exp.status === "completed" && (
                    <>
                      <div className="grid grid-cols-3 gap-2 text-[10px] mb-2">
                        <div>
                          <span className="text-muted-foreground">DP spread before</span>
                          <p className="metric-value text-foreground">{exp.avg_humidity_before != null ? `${exp.avg_humidity_before}%` : "—"}</p>
                        </div>
                        <div>
                          <span className="text-muted-foreground">DP spread during</span>
                          <p className="metric-value text-foreground">{exp.avg_humidity_during != null ? `${exp.avg_humidity_during}%` : "—"}</p>
                        </div>
                        <div>
                          <span className="text-muted-foreground">DP spread after</span>
                          <p className="metric-value text-foreground">{exp.avg_humidity_after != null ? `${exp.avg_humidity_after}%` : "—"}</p>
                        </div>
                      </div>
                      {exp.conclusion && (
                        <p className="text-xs text-foreground leading-relaxed mb-1">{exp.conclusion}</p>
                      )}
                      {exp.recommendation && (
                        <p className="text-xs text-success leading-relaxed">{exp.recommendation}</p>
                      )}
                    </>
                  )}

                  {exp.status === "active" && (
                    <div className="flex items-center gap-2 mt-1">
                      <div className="h-2 w-2 rounded-full bg-warning status-pulse" />
                      <span className="text-xs text-warning">Heating is blocked for this experiment</span>
                    </div>
                  )}

                  {exp.reason && exp.status !== "completed" && (
                    <p className="text-[10px] text-muted-foreground mt-1">{exp.reason}</p>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </div>
  );
};

export default HeatingAnalysisPanel;
