/**
 * Per-zone learned thermal model.
 * All values are derived from historical control_log data — no hardcoded assumptions.
 * The model improves over days/weeks as more heating cycles are observed.
 */
export interface ZoneThermalModel {
  /** Number of heating_off events analyzed */
  samples: number;

  /**
   * Thermal runoff: after heating stops, concrete continues radiating.
   * Learned as function of heating duration.
   * runoff_hours ≈ runoff_base + runoff_per_heat_min × heating_minutes
   */
  runoff_base: number;
  runoff_per_heat_min: number;

  /**
   * Peak temperature rise during runoff.
   * peak_rise ≈ peak_per_heat_min × heating_minutes
   * Capped by observed max.
   */
  peak_per_heat_min: number;
  peak_max_observed: number;

  /**
   * Temperature decay rate after runoff ends.
   * Learned as function of indoor-outdoor temperature delta.
   * decay_per_hour ≈ decay_coeff × (indoor_temp - outdoor_temp)
   * This varies per zone due to insulation, floor area, window exposure.
   */
  decay_coeff: number;

  /**
   * Humidity response: how RH changes after heating stops.
   * rh_change_per_hour ≈ rh_drift_coeff (positive = rising humidity)
   */
  rh_drift_coeff: number;

  /**
   * Learned relationship between RH change and DP spread change.
   * dp_spread_change ≈ rh_to_dp_coeff × rh_change
   * Negative means rising RH shrinks DP spread (expected physics).
   * Learned per zone from observed data — NOT assumed.
   */
  rh_to_dp_coeff: number;

  /** Statistical confidence (0-1) — asymptotic, never stops improving */
  confidence: number;

  /** Data timespan in days used for learning */
  data_days: number;

  /** When the model was last updated */
  last_updated: string;
}

/** Minimum samples before we trust the model enough to show predictions */
export const MIN_SAMPLES_FOR_PREDICTION = 3;

/** Samples at which confidence reaches ~63% (1 - 1/e). Model never stops learning. */
export const CONFIDENCE_HALF_LIFE = 20;

/**
 * Empty model returned when insufficient data exists.
 * All coefficients are null-equivalent (zero) — no assumptions.
 */
export function emptyThermalModel(): ZoneThermalModel {
  return {
    samples: 0,
    runoff_base: 0,
    runoff_per_heat_min: 0,
    peak_per_heat_min: 0,
    peak_max_observed: 0,
    decay_coeff: 0,
    rh_drift_coeff: 0,
    rh_to_dp_coeff: 0,
    confidence: 0,
    data_days: 0,
    last_updated: new Date().toISOString(),
  };
}
