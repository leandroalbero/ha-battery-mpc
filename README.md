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
4. Solves a linear program (scipy/HiGHS) to find the cost-optimal battery schedule
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
- Round-trip efficiency (%)

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
| Valley | 00-08 | 0.085 |
| Shoulder | 08-10, 14-18, 22-24 | 0.134 |
| Peak | 10-14, 18-22 | 0.182 |
| Export | All hours | 0.08 |

Custom tariffs can be set via the options flow (coming soon) or by editing the config entry data.

## Requirements

- Home Assistant 2024.1.0+
- scipy (auto-installed)
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
