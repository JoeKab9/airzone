# Lovable Program - Feature Requests

## Cross-Zone Heating Impact Analysis

**Goal:** Analyse how heating in one or more zones impacts the temperatures in other zones, depending on whether heating in those other zones is on or off.

**Requirements:**

- Matrix-based complex analysis covering all zone combinations
- Should assess the effect of Zone A heating ON → Zone B (heating ON) and Zone A heating ON → Zone B (heating OFF), for every pair of zones
- The analysis needs to run continuously over a long period to build statistical reliability — the longer it runs, the more accurate the correlations become
- Output should be a cross-zone impact matrix showing thermal transfer coefficients or influence scores between all zones

---

## Hot Water Heating Impact on Zone Temperatures

**Goal:** Assess how hot water heating activity affects the temperatures in each zone.

**Important:** Hot water heating uses the same heat pump but does NOT heat the zones — it simply switches on to heat the water tank independently. However, the heat pump running for hot water still affects zone temperatures as a side effect (waste heat, pipe routing through the house, etc.). The code must track the hot water system's on/off state as an independent variable.

**Requirements:**

- **Track hot water state independently:** The hot water heater on/off status must be monitored as its own data point, separate from zone heating states. When hot water kicks in, zones are NOT being actively heated — but temperatures may still change as a side effect
- Measure the passive temperature impact in each zone when the hot water system is actively heating vs. idle
- Differentiate results based on whether each zone's own heating is on or off at the time
- This creates a matrix of: hot water state (heating/not) x zone heating state (on/off) → temperature effect per zone
- This is particularly useful because hot water heating events are "free" thermal experiments — the zones aren't being heated, but you can observe temperature effects, which reveals thermal coupling through the building structure and pipework
- Like the cross-zone analysis, this should run long-term to accumulate reliable data

---

## Transparency on Existing Analysis (Thermal Runoff & Passive Reactivity)

**Context:** The app already shows analysis panels like "Concrete Thermal Runoff" (runoff duration, peak rise, decay rate by zone) and "Passive Thermal Reactivity" (coupling factor, drift per zone). These need better transparency.

**Requirements:**

- **Data source visibility:** For each analysis/metric, show exactly where the datapoints come from — which sensors, which time periods, how frequently they are sampled
- **Sample count clarity:** The current "Samples" column shows very low counts (often just 1). The UI should make it obvious how many independent data events feed into each calculation, and flag when sample sizes are too low to be reliable
- **Hardcoded vs. learned indicators:** Clearly indicate whether each value or assumption in the analysis is hardcoded/assumed (e.g. a fixed thermal mass constant) or empirically derived from collected data. If a value is learned from data, show the confidence level or variance. If hardcoded, explain what the assumption is and ideally allow the user to override it
- **Data collection frequency:** Show how often new datapoints are being collected and when the last update occurred, so the user can judge whether the analysis is still actively improving or has stalled

---

## Enhancement: Thermal Runoff × Outdoor Temperature Interaction

**Context:** The current Thermal Runoff panel shows runoff duration, peak rise, and decay rate per zone, with a basic "By outdoor temp" split (Cold vs Warm). But it currently shows `–h` for Cold, meaning insufficient data. More importantly, the analysis doesn't capture the real question.

**What's needed:**

- **Outdoor temperature's role in runoff duration:** After heating stops, the concrete keeps radiating heat — but how long that lasts and how much it contributes depends heavily on outdoor temperature. On a cold day, heat loss through walls/windows competes with floor runoff and the indoor temp will drop faster despite the stored heat. On a mild day, the runoff effect dominates longer. The analysis should quantify this relationship: for a given outdoor temp range, how long does the concrete's stored heat keep indoor temps elevated above what they would be without that thermal mass?
- **Net indoor temperature benefit by outdoor temp:** Show the effective "bonus" the concrete runoff provides at different outdoor temperatures — i.e. how many degrees warmer does the zone stay, and for how long, compared to what the passive cooling curve would be without the stored heat
- **Decay rate as a function of outdoor temp:** The current flat 0.12°C/h decay rate should be broken out by outdoor temperature bands. The decay will be faster when it's colder outside
- **Per-zone breakdown:** Each zone likely has different exposure to outdoor conditions (exterior walls, windows, orientation), so the outdoor temp interaction should be calculated per zone

---

## BUG/GAP: History Graphs Show Almost No Data

**Problem:** The 30-Day History graphs (e.g. Mur bleu) show only ~22h of data despite claiming to be 30-day views. The rest of the chart is empty.

**Investigation needed:**

- **Airzone API historic data:** Verify whether the Airzone API provides access to historical temperature/humidity readings (not just current state). If it does, the app should be pulling and storing that data to populate the history graphs. Confirm what time range and granularity the API offers
- **Netatmo API historic data:** The Netatmo API is known to provide historic measurements (via `getmeasure` endpoint). The app should be pulling this historical data as well — it likely supports weeks or months of history at various intervals
- **Root cause:** Determine why the app is not using available historic data — is it only storing data from the moment it starts polling? Is there a missing API call to fetch historical records? Is there a storage/database issue?
- **Fix:** Ensure the app fetches all available historical data from both the Airzone and Netatmo APIs on first load and backfills the history graphs accordingly, then continues appending new data going forward

---

## Enhancement: Energy Graphs — Distinguish Confirmed vs Estimated Data

**Problem:** All energy bars in the graphs (both the 8-day summary and the 3-day detailed view) are shown in orange with no distinction between confirmed Linky meter readings and estimated/calculated values.

**Requirements:**

- **Green bars** for confirmed Linky data — actual consumption readings pulled directly from the Linky smart meter API
- **Orange bars** for estimated data — any energy values that are calculated, interpolated, or based on assumptions rather than confirmed meter readings
- This colour distinction should apply to all energy graphs throughout the app (8-day summary, 3-day detailed, and any other energy visualisations)
- Include a legend clearly explaining the colour coding
- If a bar contains a mix of confirmed and estimated data, consider a split/stacked bar or a visual indicator showing the proportion

---

## BUG/Enhancement: Handle Missing Airzone Readings with Interpolation

**Problem:** The Airzone API sometimes returns no data for a zone in a given polling cycle (as seen with Ontario and Plaf bleu showing Δ0.0°, 0.0°, 0% RH, DP 0.0° — all zeroes with warning triangles). The app currently displays these as zero values or errors, which breaks the UI and corrupts any analysis that depends on continuous data.

**Requirements:**

- **Detect missing readings:** When Airzone returns null, zero, or clearly invalid data for a zone, the app should recognise this as a missing reading rather than treating it as real data
- **Interpolate between confirmed readings:** When a reading is missing, linearly interpolate between the last confirmed reading and the next confirmed reading to fill the gap. Mark interpolated values as estimated (not confirmed)
- **Visual indicator:** Show interpolated/gap-filled data differently in charts — e.g. dashed line segments or lighter colour — so the user can see where real data stops and estimates begin
- **Protect analyses:** Ensure that analyses (thermal runoff, passive reactivity, cross-zone impact, etc.) either use the interpolated values with appropriate weighting or exclude gap periods entirely, rather than ingesting zeroes as real temperatures
- **Logging:** Log when and how often missing readings occur per zone, so the user can see if certain zones have unreliable API connectivity

---

## BUG: Reliability Over Time Graph Stops Updating After First Confirmation

**Problem:** The Reliability Over Time graph shows a single DP 3h forecast accuracy point (99% on 09/03) and then flatlines. The system appears to stop updating the reliability score after the first confirmation, rather than continuously validating.

**What should happen:**

- **Rolling validation every polling cycle:** The system pulls Airzone data every ~5 minutes. After the 3h forecast window elapses, every subsequent 5-minute poll provides a new actual vs predicted comparison. The reliability score should be recalculated on every poll — not just once when the first 3h window completes
- **Continuous accuracy curve:** The graph should show an evolving accuracy line that updates every 5 minutes, reflecting the rolling prediction accuracy. Early on (few data points), the line will be volatile. Over days, it should smooth out and reveal the true forecast reliability
- **Per-forecast-type tracking:** The legend shows four series (Energy est., DP 3h forecast, DP 24h forecast, Decisions) — each should be independently and continuously validated. The 3h forecast can be confirmed after 3h and then re-confirmed every 5 min going forward. The 24h forecast takes longer for the first confirmation but then also rolls forward
- **Aggregate vs per-prediction view:** Show both the aggregate reliability (overall % of predictions within acceptable error) and ideally a way to drill into individual predictions to see which ones were accurate and which missed
- **The dashed line (~80%)** appears to be a target threshold — make this explicit in the UI (e.g. "Target: 80% accuracy") and track how long each forecast type stays above or below it

**Root cause to investigate:** The code likely evaluates forecast accuracy once (when the forecast window expires) and stores a single result, rather than treating every new actual reading as another validation point against all recently expired forecasts.

**Note:** The same bug will almost certainly affect the 24h forecast validation once it completes its first window — to be confirmed. Both forecast types need the same fix: continuous rolling validation, not one-shot.

---

## UI/UX Layout Restructure (see PowerPoint for full details)

**Key recommendations from visual review:**

- **Split Optimize tab** into sub-tabs: Control, Forecasts, Analysis, Thermal — it's far too long as a single scroll
- **Merge Correlation into Analysis** — too thin for its own tab
- **Zone selector buttons on Analytics** (like on Correlation tab) instead of 8 stacked charts
- **Remove duplicate widgets** — Energy chart, Reliability Over Time appear on multiple tabs
- **Collapse empty/collecting sections** to single-line status bars instead of full cards
- **Standardize zone cards** on Dashboard — all cards should show the same fields
- **Expand Settings** with zone config, notification prefs, thresholds, data export

See `HeatSmart-UI-Review.pptx` for detailed per-tab analysis and proposed navigation structure.

---

## Refactoring Safety: Temporary Archive Tab

**Goal:** During any refactoring or layout restructuring, preserve all removed code, components, and graphs in a temporary archive.

**Requirements:**

- **Create a hidden/dev-only tab or section** (e.g. "Archive" or "Legacy") that stores all deprecated components, graphs, and code during refactoring
- Old code and visualisations should remain accessible and functional in this tab until the refactoring is confirmed complete
- This tab can be deleted at the end once everything has been verified working in its new location
- Prevents accidental loss of functionality during restructuring — anything removed from a visible tab must first be moved to the archive

---

## Code Quality: Stricter Testing & Cleanup

**Problem:** Recurring issues with disappearing information, likely caused by unused references, dead code paths, or broken data bindings that go undetected.

**Requirements:**

- **Unused reference detection:** Automated checks for variables, imports, API calls, or data bindings that are defined but never used, or referenced but no longer exist
- **Dead code elimination:** Identify and flag code that is unreachable or components that are rendered but have no data source connected
- **Data flow integrity testing:** For every piece of data displayed in the UI, verify the complete chain: API call → data store → component prop → rendered value. If any link in the chain is broken, flag it
- **Regression testing on refactors:** When moving components between tabs or restructuring layouts, automated tests should verify that all data still flows correctly to the new locations
- **Console error monitoring:** Log and surface any JavaScript errors, failed API calls, or undefined references that occur during normal operation
- **Inconsistency detection:** Flag cases where the same data is displayed in multiple places but shows different values (e.g. energy on Dashboard vs Energy tab)

---

## Presence Detection via Strava Integration

**Goal:** Use Strava activity data to determine whether the user is at home (present) or away, feeding into the House Status system.

**Requirements:**

- **Connect to the Strava API** to pull recent activities (rides, runs, etc.)
- **Geofencing logic:** When a Strava activity starts or ends within a defined radius of the home location, mark the user as present in the area. An activity that starts from home and returns to home confirms full-day presence
- **Feed into House Status:** The current "Empty (unattended mode)" vs occupied status should incorporate Strava data as one signal of presence. When a ride is detected in the local area, it confirms someone is home
- **Combine with other signals:** Strava presence should complement (not replace) other presence indicators if they exist. It's an additional confirmation layer
- **Historical presence logging:** Build a presence history over time — this can improve heating schedule optimization (e.g. the system learns that the user is typically home on weekends and can pre-heat accordingly)
- **Privacy:** Only use location proximity (within radius of home), not detailed route data

---

## Core Principle: No Hardcoded Assumptions — Everything Self-Learning

**All parameters, thresholds, and models must be learned from data, not assumed.**

This applies to:

- Thermal runoff duration and decay rate — must be learned per zone, per outdoor temp range, not fixed at 5.7h / 0.12°C/h
- Coupling factors and passive reactivity — must evolve as more data arrives
- DP spread thresholds — the system should learn what spread levels actually correlate with condensation risk in THIS house, not use textbook values
- Heating efficiency (COP) as a function of outdoor temperature — learned from actual Linky consumption vs heating duration
- Zone thermal response times — how quickly each zone responds to heating, learned per zone
- Baseline energy consumption — learned from non-heating periods, not a fixed 55 Wh/slot assumption
- Presence patterns — learned from Strava and other signals over time
- Any constant currently used as a starting assumption must be progressively replaced by the learned value, with a visual indicator showing "assumed (default)" vs "learned (N samples, confidence X%)"

---

## Additional Suggested Features

### Heating Efficiency by Outdoor Temperature (COP Curve)

The heat pump's actual efficiency varies with outdoor temperature. By correlating Linky energy consumption with heating duration and outdoor temp, the system can build a real-world COP curve for your specific installation. This enables smarter scheduling — heating during warmer periods isn't just about thermal comfort, it's measurably cheaper per degree gained. Show the learned COP curve on the Energy tab.

### Predictive Heating Scheduler

Once the system has learned thermal response times per zone, decay rates by outdoor temp, and the COP curve, it can build a full predictive scheduler: given tomorrow's weather forecast (already available via Open-Meteo), calculate the optimal heating windows to maintain safe DP spread at minimum cost. Show this as a proposed schedule the user can approve or override.

### Condensation Event Logger

Track and log every time DP spread drops below 4° (risk) or 2° (critical) — when it happened, which zone, what the outdoor conditions were, whether heating was active, and how long it took to recover. Over time this builds a condensation risk profile that helps the model predict and prevent future events.

### Building Insulation Quality Assessment

The **temperature difference (ΔT) between inside and outside** — not the absolute outdoor temperature — is what actually drives heat loss. Heat flow is proportional to this delta (Newton's law of cooling). Using ΔT as the primary variable rather than raw outdoor temp makes the insulation analysis physically correct and comparable across seasons.

**Requirements:**

- **Use ΔT (indoor minus outdoor) as the primary variable in all thermal models.** The coupling factor should express: "for every degree of ΔT, this zone loses X °C/h." This is more meaningful than "outdoor temp → indoor temp change" because it directly reflects heat flow physics
- **Insulation score per zone:** Derived from the learned ΔT-based coupling factor. A zone that loses 0.05°C/h per degree of ΔT is better insulated than one losing 0.12°C/h per degree of ΔT. Display as a clear rating: "Good / Moderate / Poor" insulation with the raw value
- **Consistent across seasons:** Using ΔT means the insulation metric stays comparable year-round. With absolute outdoor temp, the same zone looks differently insulated in winter vs autumn because indoor temp varies. ΔT eliminates this confusion
- **Wind speed impact on heat loss:** The model should correlate wind speed with the ΔT-based heat loss rate during passive (no heating) periods. Separate the heat loss into: conductive component (proportional to ΔT alone) and infiltration component (proportional to ΔT × wind speed). If wind significantly increases the loss rate, it reveals air leakage through frames. Show this as: "Calm: 0.05°C/h per ΔT degree, Wind 30km/h: 0.09°C/h per ΔT degree → significant infiltration"
- **Per-zone wind sensitivity:** Zones with more exterior exposure or older frames will show higher wind sensitivity. The model should learn which zones are affected, identifying where draught-proofing would have the most impact
- **Wind direction factor:** If wind direction data is available, correlate with per-zone heat loss increases. A zone facing the prevailing wind will show much higher sensitivity than a sheltered zone — pinpointing exactly which walls/windows leak
- **Practical output:** Per-zone insulation report card showing: base insulation quality (calm, ΔT-normalised), wind sensitivity, and a recommendation on whether sealing improvements would meaningfully reduce heating costs

#### Extend ΔT Analysis to Relative Humidity and Dew Point

The same ΔT-based approach used for temperature should also apply to **relative humidity (RH)** and **dew point (DP)** differences between inside and outside:

- **ΔRH (indoor RH minus outdoor RH) analysis:** Correlate the indoor-outdoor RH difference with conditions like heating state, ventilation, wind speed, and ΔT. A well-sealed zone should maintain its own RH regime largely independent of outdoor RH — if indoor RH tracks outdoor RH closely, it indicates air infiltration. Zones where ΔRH collapses when it's windy are leaking outdoor air through frames, confirming the same infiltration that the temperature analysis detects but from a different angle
- **ΔDP (indoor DP minus outdoor DP) analysis:** Dew point is a more stable measure of absolute moisture content than RH (which varies with temperature). Tracking ΔDP reveals whether moisture is entering the building from outside (ΔDP shrinks → outdoor moisture infiltrating) or being generated internally (ΔDP grows → internal sources like drying, cooking, occupants). Wind-correlated ΔDP changes are a strong infiltration signal — if ΔDP drops toward zero on windy days, outdoor air is displacing indoor air
- **Cross-validation with temperature ΔT:** Zones that show high wind sensitivity in temperature ΔT analysis should also show high sensitivity in ΔRH and ΔDP analyses. If they don't correlate, it reveals different mechanisms: e.g. a zone might have good air sealing (low ΔRH/ΔDP wind sensitivity) but poor wall insulation (high ΔT wind sensitivity due to convective cooling of external surfaces)
- **Per-zone moisture profile:** Build a moisture behaviour profile alongside the thermal profile for each zone — does it gain moisture when occupied? How quickly does RH recover after heating? Does outdoor humidity penetrate on windy days? This feeds directly into condensation risk prediction: a zone that rapidly absorbs outdoor moisture during storms is at higher condensation risk even if its temperature is well-controlled
- **Practical output:** Add to the per-zone insulation report card: moisture infiltration score (based on ΔRH and ΔDP wind sensitivity), moisture source identification (internal vs external), and whether draught-proofing would reduce condensation risk in addition to reducing heat loss

### Zone Thermal Profile Cards

Each zone should build a "thermal identity" over time: insulation quality (coupling factor), thermal mass (runoff characteristics), sun exposure patterns (does it warm up in the afternoon?), wind sensitivity, relationship to adjacent zones. Display this as a per-zone profile card accessible from the zone selector. Useful for understanding why some zones behave differently.

### Weather Forecast Integration for Proactive Alerts

The system already pulls Open-Meteo data. Use the forecast to generate proactive alerts: "Cold front arriving tonight — outdoor temp dropping to 2°C. Based on learned decay rates, Zone Mur bleu will reach DP risk in ~6h without heating. Recommended: start heating at 22:00 during the current warmer window."

### Seasonal Baseline Tracking

Track how the house behaves across seasons — monthly averages for energy consumption, heating hours, average DP spread, number of risk events. This builds a year-over-year comparison that shows whether the system is actually improving outcomes and reducing costs.

### Electricity Price History Management

The cost calculations that go back in time must use the correct price that was active at that point in time, not just the current rate. Requirements:

- **Price history timeline:** Store electricity prices (variable rate per kWh and subscription cost) with effective dates. When calculating historical costs, use the price that was in effect on that date
- **API-first:** If the Linky/Enedis API or another source provides tariff history, pull it automatically
- **Manual fallback in Settings:** If no API source is available, provide a settings panel where the user can enter new prices with an effective date. Old prices remain unchanged — this creates a price history timeline (e.g. €0.1927/kWh from 01/01/2026, €0.2050/kWh from 01/04/2026)
- **CSV import option:** Allow importing a simple CSV with columns like `date, rate_kwh, subscription_yearly` to bulk-load price history
- **Retroactive recalculation:** When a new price entry is added, automatically recalculate all historical cost figures using the correct rate for each period

### Data Export

Allow exporting all collected data (temperatures, humidity, heating events, energy consumption) as CSV or JSON for external analysis or backup.

### Notification System

Push notifications or email alerts for: DP spread reaching critical levels, Airzone API connection loss, unusual energy consumption patterns, or system anomalies. Configurable per zone and per threshold.

---

## Design Decisions (Intentional — Do Not Change)

These choices were specifically requested and should not be flagged as issues:

- **Zone cards show AZ/NT/ppm only on zones with Netatmo sensors** — only 3 indoor zones plus outside have Netatmo; other zones are Airzone-only. This is correct.
- **3h and 24h DP Spread Forecasts are separate cards** — both are needed to compare forecast reliability across different time horizons. As the model learns, this reveals which forecast window is most accurate. The 24h window specifically enables preemptive heating decisions: if DP spread trends toward <4° within the next 24h, the system can schedule heating during a warmer (more efficient) time slot rather than waiting for an emergency trigger.
- **History charts showing 22h on a '24h' view** — this is simply because the system has only been running for 22 hours. The charts will fill in as more data is collected. Not a bug.
- **Zone cards have fixed positions** — zones always appear in the same layout so the user can instantly find each zone by position. Risk level is communicated through the DP delta color coding (red/amber/green), not through card ordering. Do not sort by risk.
- **Correlation tab R² values are low** — expected with only ~1 day of data. Needs weeks of varied conditions to produce meaningful correlations. Not a bug.
- **Zone selector should use buttons (pills), not dropdowns** — consistent with the Correlation tab pattern. No dropdown selectors for zone picking.
- **Energy tab shows limited data** — system has only been running ~1 day. Reliability Over Time, heating benchmarks, and cost projections all need more data to populate. The empty/placeholder states will resolve as data accumulates.

---

## Critical Review of Project Spec (holiday_house_rl_spec.md)

### Agreed — Good Foundations

- **Magnus formula for dew point** — correct, standard approach
- **Pre-heating during warm outdoor windows** — exactly right for COP optimization
- **Continuous self-learning with conservative fallbacks** — solid safety philosophy
- **Linky for real COP measurement** — much better than theoretical values
- **Occupied vs unoccupied distinction** — essential for a holiday house
- **Anomaly detection for fire/oven** — smart idea, though thresholds need tuning (see below)

### Pushback & Corrections

**1. The 4°C DP safety floor conflicts with full self-learning.**
The spec says "never <4°C margin" but also says the system should learn what spread levels actually correlate with condensation risk in THIS house. These conflict. Concrete floors with high thermal mass may be safe at 3°C spread because the surface temperature stays elevated longer. Recommendation: keep 4°C as the initial conservative default, allow the learned threshold to go as low as 3°C after sufficient confidence, but never lower. This balances safety with optimization.

**2. Fire/oven detection at +1°C/hr is too sensitive.**
A sunny window, several people in a room, or opening an oven briefly could trigger this. This threshold must be learned per zone (kitchen will spike differently from Cellier), and should require a sustained anomaly, not a single reading. Additionally, the spec only mentions cutting heating — but the system should also log these events as thermal disturbances and exclude them from the learning models so they don't corrupt thermal response estimates.

**3. Full RL (Stable Baselines3) is likely overkill.**
RL requires exploration — deliberately trying bad actions to learn — which means real discomfort or wasted energy during training. Your system has well-defined physics with uncertain parameters. A better approach: learn the physical parameters (thermal mass, COP curve, coupling factors — which you're already doing), then use model-predictive control (MPC) to optimize scheduling. MPC uses your learned model to simulate forward and pick the cheapest heating plan. Benefits: no exploration waste, much less data needed, fully interpretable, and deterministic. RL makes sense for truly unknown environments — yours is well-characterized, just with parameters to calibrate.

**4. Initial τ=2hr for floor inertia is too low.**
Your own measured thermal runoff data already shows 5.7h average (with some zones at 6-7h). Starting at 2hr will cause the system to massively underestimate concrete heat storage, leading to unnecessary early heating cycles. Use your measured data: start at 5h and let the system refine from there.

**5. The spec doesn't mention hot water as a thermal variable.**
Hot water heating uses the heat pump but doesn't heat zones — yet it affects zone temperatures as a side effect. This is actually a free experimental signal: when hot water runs, zone heating is off, so any temperature changes reveal building thermal coupling. The spec should track hot water state and use these events for learning. (Already captured in feature requests above.)

**6. Cross-zone thermal coupling is missing from the spec.**
The spec treats zones independently. In reality, heating Zone A affects Zone B through walls, open doors, and shared air. The cross-zone impact matrix (already in feature requests) is one of the most valuable analyses for optimizing which zones to heat and when.

**7. Missing: interpolation of failed API readings.**
The spec doesn't address what happens when Airzone returns null/zero data. This is already a real problem (Ontario and Plaf bleu showing all zeroes). Without interpolation, these corrupt all analyses.

**8. Missing: electricity price history.**
The spec mentions "kWh rate (Linky)" as a state variable but doesn't handle the fact that electricity prices change over time. Historical cost calculations need the rate that was active at that point. (Already captured in feature requests.)

**9. Missing: Strava/presence detection.**
Not in the original spec. Now added as a feature request — presence confirmed by cycling activity in the area feeds into occupied/unoccupied mode switching.

**10. Lovable implementation constraints.**
The spec mentions "Export to Python → Stable Baselines3." Lovable generates React/TypeScript frontends with Supabase backends. Running Python ML models requires either: (a) a separate backend service, (b) Supabase edge functions (limited compute), or (c) keeping the learning logic simple enough to run in TypeScript. If going with MPC instead of RL (recommended), the optimization can likely run in TypeScript with the learned parameters stored in Supabase.

**11. Wind speed is not modelled as a variable.**
The spec uses outdoor temperature as the main external driver but ignores wind speed. Wind drives air infiltration through gaps in frames, increasing effective heat loss significantly. On a windy 10°C day, a zone may lose heat faster than on a calm 5°C day. The model must include wind speed (and ideally direction) as an input variable alongside outdoor temperature. Open-Meteo already provides both.

**12. The model should use ΔT (indoor-outdoor difference), not absolute outdoor temperature — and extend this to ΔRH and ΔDP.**
Heat loss is proportional to the temperature difference between inside and outside, not to the absolute outdoor temp. Using ΔT as the primary variable is physically correct, makes insulation metrics comparable across seasons, and cleanly separates conductive loss (proportional to ΔT) from wind-driven infiltration loss (proportional to ΔT × wind speed). The current Correlation tab scatter plots show "Outdoor Temp → Indoor Temp" — these should be reframed as "ΔT → heat loss rate" for more meaningful analysis. The same principle applies to relative humidity (ΔRH) and dew point (ΔDP) — tracking indoor-outdoor differences for these variables reveals air infiltration patterns, moisture ingress sources, and cross-validates the temperature-based insulation assessment from a completely different physical signal.

### Summary

The spec is a strong starting point. The main adjustments are: use MPC instead of full RL, fix the initial τ estimate, make fire/oven detection per-zone and sustained, add hot water and cross-zone coupling as variables, include wind speed as a thermal variable, and handle the practical data integrity issues (missing readings, price history) that the spec doesn't address.

---

*Last updated: 2026-03-09*
