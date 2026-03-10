/**
 * Magnus formula dewpoint calculation.
 * More accurate than the simplified (temp - (100 - RH) / 5) approximation.
 *
 * @param tempC  Temperature in °C
 * @param rh     Relative humidity in % (0-100)
 * @returns Dewpoint in °C, rounded to 1 decimal
 */
export function calcDewpoint(tempC: number, rh: number): number {
  if (rh <= 0 || tempC == null) return 0;
  // Magnus-Tetens constants (more accurate for 0–60°C range)
  const a = 17.625;
  const b = 243.04;
  const gamma = (a * tempC) / (b + tempC) + Math.log(Math.max(rh, 1) / 100);
  const dp = (b * gamma) / (a - gamma);
  return Math.round(dp * 10) / 10;
}

/**
 * Calculate the best dewpoint for a room using all available sensor data.
 * Prefers Netatmo humidity (generally more accurate) with Airzone temperature.
 * If both sensors have temp, averages them.
 */
export function calcRoomDewpoint(
  airzoneTemp: number,
  airzoneHum: number,
  netatmoTemp?: number | null,
  netatmoHum?: number | null,
): number {
  // Best temperature: average of available sensors
  const temps = [airzoneTemp];
  if (netatmoTemp != null && netatmoTemp > 0) temps.push(netatmoTemp);
  const bestTemp = temps.reduce((a, b) => a + b, 0) / temps.length;

  // Best humidity: prefer Netatmo if available (typically more accurate)
  const bestHum = (netatmoHum != null && netatmoHum > 0) ? netatmoHum : airzoneHum;

  return calcDewpoint(bestTemp, bestHum);
}
