export interface Zone {
  id: string;
  name: string;
  floor: string;
  temperature: number;
  targetTemperature: number;
  humidity: number;
  targetHumidity: number; // threshold to trigger dehumidification (default 65%)
  dewpoint: number;
  isHeating: boolean; // heating is used as dehumidification method
  heatpumpMode: "heat" | "cool" | "auto" | "off";
  lastUpdated: string;
}

export interface WeatherData {
  temperature: number;
  humidity: number;
  dewpoint: number;
  condition: "sunny" | "cloudy" | "rainy" | "snowy" | "partly-cloudy";
  windSpeed: number;
  forecast: WeatherForecast[];
}

export interface WeatherForecast {
  time: string;
  temperature: number;
  humidity: number;
  dewpoint: number;
  condition: string;
}

export interface EnergyData {
  currentPower: number; // watts
  dailyConsumption: number; // kWh
  heatingConsumption: number; // kWh (dehumidification heating)
  hourlyData: { hour: string; consumption: number; heating: number }[];
  dailyData: { date: string; label: string; consumption: number; heating: number }[];
  allHourlyData?: { date: string; hour: string; consumption: number; heating: number }[];
  tariffRate: "peak" | "offpeak";
  costToday: number;
  totalDays: number;
}

export interface OptimizationSuggestion {
  id: string;
  type: "defer" | "preheat" | "ventilate" | "boost";
  zone: string;
  description: string;
  savings: number; // percentage energy savings
  confidence: number; // 0-1
  timeWindow: string;
}

export interface ThermalModel {
  zoneId: string;
  thermalInertia: number; // hours to cool 1°C
  heatupRate: number; // °C per hour
  cooldownRate: number; // °C per hour
  humidityDropRate: number; // %RH drop per hour of heating
  dataPoints: number;
  confidence: number;
  lastCalibrated: string;
}

export interface ClimateHistoryPoint {
  timestamp: string;
  temperature: number;
  humidity: number;
  dewpoint: number;
  isHeating: boolean;
  outdoorTemp?: number;
  outdoorHumidity?: number;
}

export interface NetatmoModule {
  id: string;
  name: string;
  type: string;
  temperature?: number;
  humidity?: number;
  co2?: number;
  noise?: number;
  pressure?: number;
  rain?: number;
  windStrength?: number;
  windAngle?: number;
  lastSeen?: number;
}
