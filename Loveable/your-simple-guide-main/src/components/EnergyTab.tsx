import { useState, useMemo } from "react";
import { EnergyData } from "@/types/climate";
import { Card } from "@/components/ui/card";
import { Zap, Flame, TrendingDown, Euro, Info } from "lucide-react";
import {
  Bar, XAxis, YAxis, ResponsiveContainer, Tooltip,
  ComposedChart, CartesianGrid, ReferenceArea, Line, ReferenceLine,
} from "recharts";
import { useDailyAssessments, useRecentControlLogs } from "@/hooks/useControlData";
import { useEnergyBaselines, useLinkyHourly, useLinkyDaily, useTariffRates, getTariffForDate, TariffRate, useNetatmoOccupancy } from "@/hooks/useClimateData";
import ReliabilityChart from "@/components/ReliabilityChart";

interface EnergyTabProps {
  data: EnergyData;
}

const STANDBY_WH_PER_SLOT = 55;
const HEATING_KW = 1.5;

type TimeRange = "1d" | "3d" | "7d" | "30d" | "1y";

const rangeLabels: Record<TimeRange, string> = {
  "1d": "Today + Yesterday",
  "3d": "3 Days",
  "7d": "Week",
  "30d": "Month",
  "1y": "Year",
};

function buildHeatingHourSet(logs: any[]): Set<string> {
  const set = new Set<string>();
  for (const log of logs) {
    if (log.action === "heating_on" || (log.action === "no_change" && log.reason && /heating\s*\(band/i.test(log.reason))) {
      const dt = new Date(log.created_at);
      const dateKey = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
      const hour = dt.getHours().toString().padStart(2, "0");
      set.add(`${dateKey}|${hour}`);
    }
  }
  return set;
}

/** Build today's estimated hourly consumption from control_log heating data */
function buildTodayEstimate(logs: any[]): { date: string; hour: string; consumption: number; heating: number }[] {
  const now = new Date();
  const todayKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;

  const hourlyHeatingMin: Record<string, number> = {};

  for (const log of logs) {
    const dt = new Date(log.created_at);
    const dateKey = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
    if (dateKey !== todayKey) continue;

    const hourStr = dt.getHours().toString().padStart(2, "0");

    if (log.action === "heating_on" || (log.action === "no_change" && log.reason && /heating\s*\(band/i.test(log.reason))) {
      hourlyHeatingMin[hourStr] = (hourlyHeatingMin[hourStr] || 0) + 30;
    }
  }

  const currentHour = now.getHours();
  const result: { date: string; hour: string; consumption: number; heating: number }[] = [];

  for (let h = 0; h <= currentHour; h++) {
    const hourStr = h.toString().padStart(2, "0");
    const heatingMin = Math.min(hourlyHeatingMin[hourStr] || 0, 60);
    const heatingKwh = Math.round((HEATING_KW * heatingMin / 60) * 100) / 100;
    const baselineKwh = Math.round((STANDBY_WH_PER_SLOT * 2 / 1000) * 100) / 100;

    result.push({
      date: todayKey,
      hour: hourStr,
      consumption: Math.round((baselineKwh + heatingKwh) * 100) / 100,
      heating: heatingKwh,
    });
  }

  return result;
}

interface ChartEntry {
  label: string;
  date?: string; // YYYY-MM-DD for daily views
  // Green bars — verified Linky data only
  standby: number;
  heating: number;
  // Orange bars — estimated (no Linky yet)
  estStandby: number;
  estHeating: number;
  total: number;
  estimatedHeating: number;
  isHeatingLog: boolean;
  isHeatingLinky: boolean;
  isEstimate: boolean;
  // Cost lines
  costLinky: number;    // green cost line (verified)
  costEstimate: number; // orange cost line (estimated)
  // Consumption lines
  consumptionLinky: number | null;    // green line (verified)
  consumptionEstimate: number | null; // orange line (estimated)
}

const EnergyTab = ({ data }: EnergyTabProps) => {
  const [range, setRange] = useState<TimeRange>("1d");
  const { data: controlLogs } = useRecentControlLogs(1000);
  const { data: baselines } = useEnergyBaselines();
  const { data: tariffRates } = useTariffRates();

  const { data: hourly3d } = useLinkyHourly(3, range === "3d");
  const { data: hourly7d } = useLinkyHourly(7, range === "7d");
  const { data: daily30d } = useLinkyDaily(30, range === "30d");
  const { data: daily1y } = useLinkyDaily(365, range === "1y");
  const isDaily = range === "30d" || range === "1y";
  const { data: occupancyData } = useNetatmoOccupancy(365, isDaily);

  const avgBaseline = useMemo(() => {
    if (!baselines || Object.keys(baselines).length === 0) return STANDBY_WH_PER_SLOT;
    const values = Object.values(baselines);
    return Math.round(values.reduce((s, v) => s + v, 0) / values.length);
  }, [baselines]);

  const heatingHours = useMemo(
    () => buildHeatingHourSet(controlLogs || []),
    [controlLogs]
  );

  const todayEstimate = useMemo(
    () => buildTodayEstimate(controlLogs || []),
    [controlLogs]
  );

  const getActiveData = (): EnergyData => {
    if (range === "3d" && hourly3d) return hourly3d;
    if (range === "7d" && hourly7d) return hourly7d;
    if (range === "30d" && daily30d) return daily30d;
    if (range === "1y" && daily1y) return daily1y;
    return data;
  };

  const activeData = getActiveData();

  const getChartData = (): ChartEntry[] => {
    const isDaily = range === "30d" || range === "1y";
    const rates = tariffRates || [];
    const costForKwh = (kwh: number, dateStr: string) => {
      const t = getTariffForDate(rates, dateStr);
      return Math.round(kwh * t.variable_rate_kwh * 100) / 100;
    };

    if (isDaily) {
      if (!activeData.dailyData || activeData.dailyData.length === 0) return [];
      return activeData.dailyData.map((d) => ({
        label: d.label,
        date: d.date,
        standby: Math.round((d.consumption - d.heating) * 100) / 100,
        heating: d.heating,
        estStandby: 0,
        estHeating: 0,
        total: d.consumption,
        estimatedHeating: 0,
        isHeatingLog: false,
        isHeatingLinky: d.heating > 0.01,
        isEstimate: false,
        costLinky: costForKwh(d.consumption, d.date),
        costEstimate: 0,
        consumptionLinky: d.consumption,
        consumptionEstimate: null,
      }));
    }

    const allHourly = activeData.allHourlyData || [];

    if (range === "1d") {
      const now = new Date();
      const todayKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
      const yesterday = new Date(now.getTime() - 86400000);
      const yesterdayKey = `${yesterday.getFullYear()}-${String(yesterday.getMonth() + 1).padStart(2, "0")}-${String(yesterday.getDate()).padStart(2, "0")}`;

      const linkyHours = allHourly.length > 0
        ? allHourly.filter((h) => h.date >= yesterdayKey)
        : [];

      const linkyTodayHours = new Set(
        linkyHours.filter((h) => h.date === todayKey).map((h) => h.hour)
      );

      const merged = [...linkyHours];
      for (const est of todayEstimate) {
        if (!linkyTodayHours.has(est.hour)) {
          merged.push(est);
        }
      }

      merged.sort((a, b) => `${a.date}|${a.hour}`.localeCompare(`${b.date}|${b.hour}`));

      return merged.map((h) => {
        const isEstimate = !linkyTodayHours.has(h.hour) && h.date === todayKey;
        const isHeatingLog = heatingHours.has(`${h.date}|${h.hour}`);
        const [, m, d] = h.date.split("-");
        const base = Math.round((h.consumption - h.heating) * 100) / 100;
        const cost = costForKwh(h.consumption, h.date);
        return {
          label: `${d}/${m} ${h.hour}h`,
          standby: isEstimate ? 0 : base,
          heating: isEstimate ? 0 : h.heating,
          estStandby: isEstimate ? base : 0,
          estHeating: isEstimate ? h.heating : 0,
          total: h.consumption,
          estimatedHeating: isHeatingLog ? Math.round(HEATING_KW * 100) / 100 : 0,
          isHeatingLog,
          isHeatingLinky: h.heating > 0.01,
          isEstimate,
          costLinky: isEstimate ? 0 : cost,
          costEstimate: isEstimate ? cost : 0,
          consumptionLinky: isEstimate ? null : h.consumption,
          consumptionEstimate: isEstimate ? h.consumption : null,
        };
      });
    }

    // 3d, 7d
    return allHourly.map((h) => {
      const isHeatingLog = heatingHours.has(`${h.date}|${h.hour}`);
      const [, m, d] = h.date.split("-");
      return {
        label: `${d}/${m} ${h.hour}h`,
        standby: Math.round((h.consumption - h.heating) * 100) / 100,
        heating: h.heating,
        estStandby: 0,
        estHeating: 0,
        total: h.consumption,
        estimatedHeating: isHeatingLog ? Math.round(HEATING_KW * 100) / 100 : 0,
        isHeatingLog,
        isHeatingLinky: h.heating > 0.01,
        isEstimate: false,
        costLinky: costForKwh(h.consumption, h.date),
        costEstimate: 0,
        consumptionLinky: h.consumption,
        consumptionEstimate: null,
      };
    });
  };

  const chartData = getChartData();

  const getRangeSummary = () => {
    const rates = tariffRates || [];
    const currentTariff = getTariffForDate(rates, new Date().toISOString().slice(0, 10));

    if (activeData.dailyData && activeData.dailyData.length > 0) {
      const totalKwh = activeData.dailyData.reduce((s, d) => s + d.consumption, 0);
      const heatingKwh = activeData.dailyData.reduce((s, d) => s + d.heating, 0);
      const variableCost = chartData.reduce((s, d) => s + d.costLinky + d.costEstimate, 0);
      const days = activeData.dailyData.length;
      const fixedCost = Math.round((currentTariff.fixed_annual_eur / 365) * days * 100) / 100;
      return {
        estKwh: Math.round(totalKwh * 10) / 10,
        heatingKwh: Math.round(heatingKwh * 10) / 10,
        actualKwh: Math.round(totalKwh * 10) / 10,
        variableCost: Math.round(variableCost * 100) / 100,
        fixedCost,
        cost: Math.round((variableCost + fixedCost) * 100) / 100,
        days,
        tariffRate: currentTariff.variable_rate_kwh,
        fixedAnnual: currentTariff.fixed_annual_eur,
      };
    }

    const totalKwh = chartData.reduce((s, d) => s + (d.total || 0), 0);
    const heatingKwh = chartData.reduce((s, d) => s + (d.heating || 0) + (d.estHeating || 0), 0);
    const variableCost = chartData.reduce((s, d) => s + d.costLinky + d.costEstimate, 0);
    const days = range === "1d" ? 2 : range === "3d" ? 3 : 7;
    const fixedCost = Math.round((currentTariff.fixed_annual_eur / 365) * days * 100) / 100;
    return {
      estKwh: Math.round(totalKwh * 10) / 10,
      heatingKwh: Math.round(heatingKwh * 10) / 10,
      actualKwh: Math.round(totalKwh * 10) / 10,
      variableCost: Math.round(variableCost * 100) / 100,
      fixedCost,
      cost: Math.round((variableCost + fixedCost) * 100) / 100,
      days,
      tariffRate: currentTariff.variable_rate_kwh,
      fixedAnnual: currentTariff.fixed_annual_eur,
    };
  };

  const summary = getRangeSummary();

  const buildRanges = (key: keyof ChartEntry) => {
    const ranges: { start: string; end: string }[] = [];
    let rs: string | null = null;
    for (let i = 0; i < chartData.length; i++) {
      const d = chartData[i];
      const xKey = isDaily ? (d.date || d.label) : d.label;
      if (d[key] && !rs) rs = xKey;
      if (!d[key] && rs) {
        const prev = chartData[i - 1];
        ranges.push({ start: rs, end: isDaily ? (prev.date || prev.label) : prev.label });
        rs = null;
      }
    }
    if (rs) {
      const last = chartData[chartData.length - 1];
      ranges.push({ start: rs, end: isDaily ? (last.date || last.label) : last.label });
    }
    return ranges;
  };

  const heatingLogRanges = buildRanges("isHeatingLog");
  const heatingLinkyRanges = buildRanges("isHeatingLinky");
  const isHourlyView = range === "1d" || range === "3d" || range === "7d";
  const isLoading = (range === "3d" && !hourly3d) || (range === "7d" && !hourly7d) ||
                    (range === "30d" && !daily30d) || (range === "1y" && !daily1y);

  // Build occupancy ranges for daily views (blue shading)
  const occupancyRanges = useMemo(() => {
    if (!isDaily || !occupancyData || chartData.length === 0) return [];
    const occupiedDates = new Set(occupancyData.filter(o => o.occupied).map(o => o.date));
    const ranges: { start: string; end: string }[] = [];
    let rs: string | null = null;
    for (let i = 0; i < chartData.length; i++) {
      const d = chartData[i];
      const isOccupied = d.date ? occupiedDates.has(d.date) : false;
      if (isOccupied && !rs) rs = d.date || d.label;
      if (!isOccupied && rs) {
        ranges.push({ start: rs, end: chartData[i - 1].date || chartData[i - 1].label });
        rs = null;
      }
    }
    if (rs) ranges.push({ start: rs, end: chartData[chartData.length - 1].date || chartData[chartData.length - 1].label });
    return ranges;
  }, [isDaily, occupancyData, chartData]);

  // Compute month boundaries for daily views (30d, 1y)
  const monthBoundaries = useMemo(() => {
    if (!isDaily || chartData.length === 0) return [];
    const monthNames = ["Jan", "Fév", "Mar", "Avr", "Mai", "Juin", "Juil", "Aoû", "Sep", "Oct", "Nov", "Déc"];
    const boundaries: { date: string; label: string; monthName: string; idx: number }[] = [];
    let prevMonth = "";
    for (let i = 0; i < chartData.length; i++) {
      const d = chartData[i];
      if (!d.date) continue;
      const month = d.date.substring(0, 7); // "YYYY-MM"
      if (month !== prevMonth) {
        const m = parseInt(d.date.substring(5, 7), 10) - 1;
        const year = d.date.substring(0, 4);
        const showYear = boundaries.length > 0 && boundaries[boundaries.length - 1].monthName === monthNames[m];
        boundaries.push({ date: d.date, label: d.label, monthName: showYear ? `${monthNames[m]} ${year}` : monthNames[m], idx: i });
        prevMonth = month;
      }
    }
    return boundaries;
  }, [chartData, isDaily]);

  return (
    <div className="space-y-6">
      {/* Time range selector */}
      <div className="flex items-center gap-1 p-1 rounded-lg bg-secondary border border-border w-fit">
        {(Object.keys(rangeLabels) as TimeRange[]).map((r) => (
          <button
            key={r}
            onClick={() => setRange(r)}
            className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
              range === r
                ? "bg-card text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {rangeLabels[r]}
          </button>
        ))}
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <Card className="glass-card p-4">
          <div className="flex items-center gap-2 mb-2">
            <Flame className="h-4 w-4 text-heating" />
            <span className="text-xs text-muted-foreground uppercase tracking-wider">Heating (est.)</span>
          </div>
          <span className="metric-value text-2xl text-heating">{summary.heatingKwh}</span>
          <span className="text-sm text-muted-foreground ml-1">kWh</span>
          {summary.days > 1 && <p className="text-[10px] text-muted-foreground mt-0.5">{summary.days} days</p>}
        </Card>

        <Card className="glass-card p-4 border-success/20">
          <div className="flex items-center gap-2 mb-2">
            <Zap className="h-4 w-4 text-success" />
            <span className="text-xs text-muted-foreground uppercase tracking-wider">Total (Linky)</span>
          </div>
          <span className="metric-value text-2xl text-success">{summary.actualKwh ?? "—"}</span>
          <span className="text-sm text-muted-foreground ml-1">kWh</span>
        </Card>

        <Card className="glass-card p-4">
          <div className="flex items-center gap-2 mb-2">
            <Euro className="h-4 w-4 text-foreground" />
            <span className="text-xs text-muted-foreground uppercase tracking-wider">Cost</span>
          </div>
          <span className="metric-value text-2xl text-foreground">€{summary.cost.toFixed(2)}</span>
          <div className="flex gap-2 mt-1">
            <span className="text-[10px] text-muted-foreground">Variable: €{summary.variableCost.toFixed(2)}</span>
            <span className="text-[10px] text-muted-foreground">Fixed: €{summary.fixedCost.toFixed(2)}</span>
          </div>
        </Card>

        <Card className="glass-card p-4">
          <div className="flex items-center gap-2 mb-2">
            <TrendingDown className="h-4 w-4 text-foreground" />
            <span className="text-xs text-muted-foreground uppercase tracking-wider">Heating share</span>
          </div>
          <span className="metric-value text-2xl text-foreground">{summary.estKwh > 0 ? Math.round((summary.heatingKwh / summary.estKwh) * 100) : 0}%</span>
        </Card>
      </div>

      {/* Main chart */}
      <Card className="glass-card p-5">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h3 className="text-sm font-semibold text-foreground">
              {isHourlyView ? "Hourly" : "Daily"} Consumption Breakdown
              {range === "1d" && <span className="text-xs text-muted-foreground font-normal ml-2">(Today + Yesterday)</span>}
              {isLoading && <span className="text-xs text-muted-foreground font-normal ml-2">Loading…</span>}
            </h3>
            <p className="text-xs text-muted-foreground">
              Green = verified Linky · Orange = today's estimate · Red zone = system heating
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-[10px]">
            <div className="flex items-center gap-1">
              <div className="h-2.5 w-2.5 rounded-sm" style={{ background: "hsl(152 50% 36%)" }} />
              <span className="text-muted-foreground">Linky base</span>
            </div>
            <div className="flex items-center gap-1">
              <div className="h-2.5 w-2.5 rounded-sm" style={{ background: "hsl(142 60% 50%)" }} />
              <span className="text-muted-foreground">Linky heating</span>
            </div>
            <div className="flex items-center gap-1">
              <div className="h-2.5 w-2.5 rounded-sm" style={{ background: "hsl(32 80% 45%)" }} />
              <span className="text-muted-foreground">Est. base</span>
            </div>
            <div className="flex items-center gap-1">
              <div className="h-2.5 w-2.5 rounded-sm" style={{ background: "hsl(25 90% 55%)" }} />
              <span className="text-muted-foreground">Est. heating</span>
            </div>
            <div className="flex items-center gap-1">
              <div className="h-2.5 w-6 rounded-sm" style={{ background: "hsla(0, 70%, 50%, 0.2)" }} />
              <span className="text-muted-foreground">System heating</span>
            </div>
            <div className="flex items-center gap-1">
              <div className="h-0.5 w-4" style={{ background: "hsl(142 60% 50%)" }} />
              <span className="text-muted-foreground">Conso (Linky)</span>
            </div>
            <div className="flex items-center gap-1">
              <div className="h-0.5 w-4 border-t-2 border-dashed" style={{ borderColor: "hsl(25 90% 55%)" }} />
              <span className="text-muted-foreground">Conso (est.)</span>
            </div>
            <div className="flex items-center gap-1">
              <div className="h-0.5 w-4 opacity-50" style={{ background: "hsl(142 60% 50%)" }} />
              <span className="text-muted-foreground">Cost (Linky)</span>
            </div>
            <div className="flex items-center gap-1">
              <div className="h-0.5 w-4 border-t border-dashed opacity-50" style={{ borderColor: "hsl(25 90% 55%)" }} />
              <span className="text-muted-foreground">Cost (est.)</span>
            </div>
            {isDaily && (
              <div className="flex items-center gap-1">
                <div className="h-2.5 w-6 rounded-sm" style={{ background: "hsla(210, 70%, 50%, 0.15)" }} />
                <span className="text-muted-foreground">Présence (CO₂/bruit)</span>
              </div>
            )}
          </div>
        </div>

        {chartData.length === 0 ? (
          <div className="h-64 flex items-center justify-center">
            <p className="text-sm text-muted-foreground">
              {isLoading ? "Loading Linky data…" : "No data available for this period"}
            </p>
          </div>
        ) : (
          <div className={isDaily ? "h-80" : "h-72"}>
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={chartData} barGap={0}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(220 14% 18%)" />
                {/* Occupancy shading — blue tint when people present */}
                {isDaily && occupancyRanges.map((or, i) => (
                  <ReferenceArea
                    key={`occ-${i}`}
                    xAxisId={0}
                    yAxisId="left"
                    x1={or.start}
                    x2={or.end}
                    fill="hsl(210 70% 50%)"
                    fillOpacity={0.25}
                    strokeOpacity={0}
                  />
                ))}
                {heatingLinkyRanges.map((hr, i) => (
                  <ReferenceArea
                    key={`linky-${i}`}
                    xAxisId={0}
                    yAxisId="left"
                    x1={hr.start}
                    x2={hr.end}
                    fill="hsl(32 95% 55%)"
                    fillOpacity={0.06}
                    strokeOpacity={0}
                  />
                ))}
                {heatingLogRanges.map((hr, i) => (
                  <ReferenceArea
                    key={`log-${i}`}
                    xAxisId={0}
                    yAxisId="left"
                    x1={hr.start}
                    x2={hr.end}
                    fill="hsl(0 70% 50%)"
                    fillOpacity={0.15}
                    strokeOpacity={0}
                  />
                ))}
                {/* Month boundary lines for daily views */}
                {isDaily && monthBoundaries.map((mb, i) => (
                  <ReferenceLine
                    key={`month-${i}`}
                    xAxisId={0}
                    yAxisId="left"
                    x={mb.date}
                    stroke="hsl(215 12% 40%)"
                    strokeDasharray="3 3"
                    strokeWidth={1}
                  />
                ))}
                <XAxis
                  xAxisId={0}
                  dataKey={isDaily ? "date" : "label"}
                  axisLine={false}
                  tickLine={false}
                  tick={{ fontSize: 9, fill: "hsl(215 12% 55%)" }}
                  interval={
                    range === "1d" ? 1
                    : range === "3d" ? 5
                    : range === "7d" ? 11
                    : range === "30d" ? 4
                    : 29
                  }
                  angle={isHourlyView && range !== "1d" ? -45 : 0}
                  textAnchor={isHourlyView && range !== "1d" ? "end" : "middle"}
                  height={isHourlyView && range !== "1d" ? 50 : 20}
                />
                {/* Month name axis for daily views */}
                {isDaily && (
                  <XAxis
                    xAxisId={1}
                    dataKey="date"
                    axisLine={false}
                    tickLine={false}
                    height={24}
                    tick={(props: any) => {
                      const { x, y, index } = props;
                      const mb = monthBoundaries.find(b => b.idx === index);
                      if (!mb) return <g />;
                      return (
                        <g>
                          <line x1={x} y1={y} x2={x + 80} y2={y} stroke="hsl(0 70% 50%)" strokeWidth={1} opacity={0.6} />
                          <text x={x + 4} y={y + 14} textAnchor="start" fontSize={11} fontWeight={600} fill="hsl(215 20% 70%)">
                            {mb.monthName}
                          </text>
                        </g>
                      );
                    }}
                    interval={0}
                  />
                )}
                <YAxis
                  yAxisId="left"
                  axisLine={false}
                  tickLine={false}
                  tick={{ fontSize: 10, fill: "hsl(215 12% 55%)" }}
                  width={35}
                  label={{ value: "kWh", angle: -90, position: "insideLeft", style: { fontSize: 10, fill: "hsl(215 12% 55%)" } }}
                />
                <YAxis
                  yAxisId="right"
                  orientation="right"
                  axisLine={false}
                  tickLine={false}
                  tick={{ fontSize: 10, fill: "hsl(45 80% 60%)" }}
                  width={40}
                  label={{ value: "€", angle: 90, position: "insideRight", style: { fontSize: 10, fill: "hsl(45 80% 60%)" } }}
                />
                <Tooltip
                  contentStyle={{
                    background: "hsl(220 18% 13%)",
                    border: "1px solid hsl(220 14% 22%)",
                    borderRadius: "8px",
                    fontSize: "11px",
                    color: "hsl(210 20% 92%)",
                  }}
                  labelFormatter={(label: string, payload: any[]) => {
                    const entry = payload?.[0]?.payload;
                    if (entry?.date) {
                      const [y, m, d] = entry.date.split("-").map(Number);
                      const dt = new Date(y, m - 1, d);
                      return dt.toLocaleDateString("fr-FR", { weekday: "short", day: "numeric", month: "long" });
                    }
                    return label;
                  }}
                  formatter={(value: number | null, name: string) => {
                    const labels: Record<string, string> = {
                      heating: "🟢 Linky heating",
                      standby: "🌿 Linky base",
                      estHeating: "🟠 Est. heating",
                      estStandby: "🟤 Est. base",
                      estimatedHeating: "🔥 Control log heating",
                      costLinky: "💚 Cost (Linky)",
                      costEstimate: "🟠 Cost (est.)",
                      consumptionLinky: "🟢 Total (Linky)",
                      consumptionEstimate: "🟠 Total (est.)",
                    };
                    const unit = name.startsWith("cost") ? "€" : "kWh";
                    return [value != null ? `${value} ${unit}` : "—", labels[name] || name];
                  }}
                />
                {/* Verified Linky data — green */}
                <Bar xAxisId={0} yAxisId="left" dataKey="standby" stackId="main" fill="hsl(152 50% 36%)" fillOpacity={0.7} radius={[0, 0, 0, 0]} />
                <Bar xAxisId={0} yAxisId="left" dataKey="heating" stackId="main" fill="hsl(142 60% 50%)" fillOpacity={0.85} radius={[0, 0, 0, 0]} />
                {/* Estimated data — orange */}
                <Bar xAxisId={0} yAxisId="left" dataKey="estStandby" stackId="main" fill="hsl(32 80% 45%)" fillOpacity={0.6} radius={[0, 0, 0, 0]} />
                <Bar xAxisId={0} yAxisId="left" dataKey="estHeating" stackId="main" fill="hsl(25 90% 55%)" fillOpacity={0.8} radius={[2, 2, 0, 0]} />
                {/* Consumption lines — green solid for Linky, orange dashed for estimated */}
                <Line xAxisId={0} yAxisId="left" dataKey="consumptionLinky" stroke="hsl(142 60% 50%)" strokeWidth={2} dot={false} connectNulls={false} />
                <Line xAxisId={0} yAxisId="left" dataKey="consumptionEstimate" stroke="hsl(25 90% 55%)" strokeWidth={2} dot={false} strokeDasharray="4 2" connectNulls={false} />
                {/* Cost lines — green for Linky, orange for estimated */}
                <Line xAxisId={0} yAxisId="right" dataKey="costLinky" stroke="hsl(142 60% 50%)" strokeWidth={1} dot={false} connectNulls={false} opacity={0.5} />
                <Line xAxisId={0} yAxisId="right" dataKey="costEstimate" stroke="hsl(25 90% 55%)" strokeWidth={1} dot={false} strokeDasharray="4 2" connectNulls={false} opacity={0.5} />
                {isHourlyView && (
                  <Line
                    xAxisId={0}
                    yAxisId="left"
                    dataKey="estimatedHeating"
                    stroke="hsl(32 95% 55%)"
                    strokeWidth={2}
                    dot={false}
                    strokeDasharray="4 2"
                    connectNulls={false}
                  />
                )}
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )}
      </Card>

      {/* Method explanation */}
      <Card className="glass-card p-5 border-heating/15">
        <div className="flex items-center gap-2 mb-3">
          <Info className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-semibold text-foreground">Estimation Method</h3>
        </div>
        <div className="space-y-2 text-sm text-secondary-foreground">
          <p>
            <span className="text-muted-foreground">Verified (Linky):</span> Green bars = real consumption from Enedis (1-day delay). Bright green = heating share above baseline.
          </p>
          <p>
            <span className="text-muted-foreground">Estimated (today):</span> Orange bars = hours not yet confirmed by Linky. Based on baseline (~{avgBaseline} Wh/slot) + heating from control_log (~{HEATING_KW} kW).
          </p>
          <p>
            <span className="text-muted-foreground">Fetch schedule:</span> Linky data is fetched once daily around 10:00. If unavailable (503), retries hourly until success.
          </p>
          <div className="flex flex-wrap gap-4 mt-3 pt-3 border-t border-border">
            <div>
              <span className="text-[10px] text-muted-foreground uppercase tracking-wider">Variable rate</span>
              <p className="metric-value text-lg text-foreground">€{summary.tariffRate}/kWh</p>
            </div>
            <div>
              <span className="text-[10px] text-muted-foreground uppercase tracking-wider">Subscription</span>
              <p className="metric-value text-lg text-foreground">€{summary.fixedAnnual}/yr</p>
            </div>
            <div>
              <span className="text-[10px] text-muted-foreground uppercase tracking-wider">Tariff type</span>
              <p className="metric-value text-lg text-foreground">Base TTC</p>
            </div>
            <div>
              <span className="text-[10px] text-muted-foreground uppercase tracking-wider">Avg baseline</span>
              <p className="metric-value text-lg text-foreground">{avgBaseline} Wh/slot</p>
            </div>
          </div>
        </div>
      </Card>

      <ReliabilityChart />
    </div>
  );
};

export default EnergyTab;
