# Battery MPC Controller for Home Assistant

Optimal battery scheduling using Model Predictive Control (MPC) with linear programming. Minimizes electricity costs by intelligently timing battery charge/discharge based on:

- **Solar forecasts** from Open-Meteo (free, no API key needed)
- **Time-of-use tariffs** (configurable, defaults to Spain 2.0TD)
- **Rolling 24h optimization** re-solved every 5 minutes

## How It Works

Every 5 minutes, the integration:

1. Fetches a 48h solar irradiance forecast from Open-Meteo
2. Applies monthly calibration factors (corrects seasonal GHI-to-PV bias)
3. Builds a load forecast from historical sensor data
4. Solves a linear program (built-in numpy interior-point solver) to find the cost-optimal battery schedule
5. Sends the charge/discharge command to your inverter

The LP optimizer is the same formulation proven in backtesting to save ~70 EUR/year compared to simple time-based charging strategies.

## Installation via HACS

1. Open HACS in Home Assistant
2. Click the three dots menu > **Custom repositories**
3. Add this repository URL, category: **Integration**
4. Click **Install**
5. Restart Home Assistant
6. Go to **Settings > Devices & Services > Add Integration > Battery MPC Controller**

## Configuration

The setup wizard walks through 4 steps:

### 1. Location
Your latitude/longitude for solar forecast (auto-filled from HA config).

### 2. Battery Specs
- Capacity (kWh)
- Max charge/discharge rate (kW)
- Minimum SoC (%)
- Battery efficiency, each way (%): applied once on charge and once on discharge

### 3. Sensors
- **Battery SoC sensor** (required) — e.g., `sensor.battery_soc`
- **PV power sensor** (optional) — for real-time solar correction
- **House consumption sensor** (optional) — for real-time load
- **Inverter type** — Generic, Solis, or Victron

### 4. Inverter Control
Depends on inverter type:
- **Generic**: map charge/discharge switch and power number entities
- **Solis**: select any Solis sensor entity (prefix auto-detected)
- **Victron**: select Victron GX entity

## Sensors Created

| Sensor | Description |
|--------|-------------|
| `sensor.battery_mpc_next_action` | Current action: charge, discharge, or idle |
| `sensor.battery_mpc_target_power` | Target power in watts |
| `sensor.battery_mpc_target_soc` | Target state of charge (%) |
| `sensor.battery_mpc_forecast_pv_power` | Forecasted PV power (W) |
| `sensor.battery_mpc_forecast_load` | Forecasted house load (W) |
| `sensor.battery_mpc_cost_savings_today` | Estimated savings today (EUR) |
| `sensor.battery_mpc_solve_time` | LP solve time (ms) |
| `sensor.battery_mpc_forecast_age` | Age of solar forecast (min) |

The `next_action` sensor includes the full 24h schedule as an attribute for use in dashboards.

## Services

| Service | Description |
|---------|-------------|
| `battery_mpc.force_charge` | Override MPC, force charge at specified power |
| `battery_mpc.force_discharge` | Override MPC, force discharge at specified power |
| `battery_mpc.set_idle` | Override MPC, stop charge/discharge |
| `battery_mpc.refresh_forecast` | Force a fresh solar forecast fetch |

## Tariff Configuration

Default tariff is Spain's 2.0TD:

| Period | Hours | Price (EUR/kWh) |
|--------|-------|----------------|
| Valley | 00-08 | 0.1228 |
| Shoulder | 08-10, 14-18, 22-24 | 0.1519 |
| Peak | 10-14, 18-22 | 0.2558 |
| Export | All hours | 0.0509 |

Prices include taxes. Weekends and the fixed-date national holidays (1 Jan, 6 Jan, 1 May, 15 Aug, 12 Oct, 1 Nov, 6 Dec, 8 Dec, 25 Dec) are valley all day. Prices can be changed in the integration's options.

## Deferrable load

Optional. A power sensor for a load the battery should not treat as ordinary house consumption (an EV charger, for example). The controller does not assume what the load is.

- The forecast and the LP see house load minus that sensor, floored at zero.
- The grid-import cap still uses total house load, so the deferrable load keeps priority on the contracted import and the battery charges with what is left.
- While that load is above the threshold (default 200 W) and the plan is idle, the GoodWe on-grid depth of discharge is raised to the current SoC (`100 - max(min SoC, floor(SoC))`) so discharge stops and PV can still charge. Operation mode stays on the normal mapping (`general` for idle). Charge and discharge are unchanged. The floor returns to `100 - min SoC` when the load drops below the threshold or the plan is not idle. This needs the depth-of-discharge entity; without it there is no hold. A threshold cross reapplies the floor immediately, at most once every 30 seconds, without re-solving the plan.
- Optional allow-battery entity (`binary_sensor`, `input_boolean`, or `switch`). While its state is `on`, the hold is released (`100 - min SoC`) regardless of the plan, so the deferrable load may use the battery. `unavailable`, `unknown`, or leaving it unset is treated as off. A flip reapplies the floor immediately, with the same 30-second debounce and without re-solving the plan. The integration does not assume what produces that entity.
- The sensor may report W or kW. Leave it unset and behaviour is unchanged.
- `sensor.battery_mpc_next_action` carries `deferrable_load_w`, `deferrable_hold_active`, and `deferrable_battery_allowed`.

## Fail-safe behaviour

- If the battery SoC sensor is unavailable, or a cycle fails or runs longer than 3 minutes, no plan is made and a GoodWe inverter is set to self-consumption (`general`).
- The controller remembers the mode it asked for and re-sends it when a write failed, when the inverter entities come back from unavailable, once an hour, and (with the optional Diag Status sensor configured) when the inverter reports timed charging while self-consumption was requested.
- A watchdog on its own timer sets self-consumption and raises a persistent notification when no cycle has succeeded for 20 minutes.
- Every inverter write times out after 30 seconds.

## Requirements

- Home Assistant 2024.8.0+
- Internet access for Open-Meteo API

## Backtesting Results

Tested on 2.3 years of real data (15 kWh battery, 4.8 kW charge/discharge):

| Strategy | Annual Cost | Savings vs Baseline |
|----------|------------|-------------------|
| Oracle (theoretical limit) | 541 EUR/yr | 156 EUR/yr |
| **MPC 5-min (this integration)** | **628 EUR/yr** | **69 EUR/yr** |
| MPC 15-min | 644 EUR/yr | 53 EUR/yr |
| Simple time-based charging | 697 EUR/yr | baseline |

## License

MIT

`deferrable_assist_min_soc` (optional): while the allow-battery entity is on and the deferrable load draws, the GoodWe on-grid floor drops to this SoC. After hitting its floor a GoodWe refuses to discharge until SoC is floor + 5%, so a lower floor while assisting releases that lock. The floor returns to `min_soc` as soon as assist ends.

