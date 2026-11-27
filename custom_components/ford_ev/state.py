"""Everything the engine persists, with (de)serialisation for the Store."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

from .accounting import SocAccountingState
from .const import NOTIFY_DEFAULT_OFF, NOTIFY_KEYS
from .periods import PeriodSnapshot, PeriodState, add_distance
from .range_model import RangeModelState

STORAGE_VERSION = 1


def _default_notify() -> dict[str, bool]:
    return {key: key not in NOTIFY_DEFAULT_OFF for key in NOTIFY_KEYS}


@dataclass
class Settings:
    """Settings exposed as number and switch entities."""

    climate_on: bool = True
    climate_setpoint: float = 21.0
    charge_target: float = 100.0
    plug_in_reminder_soc: float = 40.0
    notify: dict[str, bool] = field(default_factory=_default_notify)


@dataclass
class ChargingSession:
    """An open charging session."""

    start_ts: float
    start_soc: float | None
    charger_at_start: float
    added_at_start: float
    cost_at_start: float
    model_soc_at_start: float | None
    home: bool
    fallback_kwh: float = 0.0
    # The charger counter was read in this session, and how often it started again from zero.
    counter_seen: bool = False
    counter_restarts: int = 0


@dataclass
class EngineState:
    """All persisted engine state."""

    soc: SocAccountingState = field(default_factory=SocAccountingState)
    charger_last_reading: float | None = None
    charger_energy: float = 0.0
    charger_energy_home: float = 0.0
    charger_energy_solar: float = 0.0
    charger_last_ts: float | None = None
    # cost is the total; it always equals cost_home + cost_public.
    cost: float = 0.0
    cost_home: float = 0.0
    cost_public: float = 0.0
    session: ChargingSession | None = None
    pending_session_end: dict[str, Any] | None = None
    last_session: dict[str, Any] | None = None
    periods: PeriodState = field(default_factory=PeriodState)
    model: RangeModelState = field(default_factory=RangeModelState)
    settings: Settings = field(default_factory=Settings)
    pending_trip_end_ts: float | None = None
    last_odometer_km: float | None = None
    alerts_armed: dict[str, bool] = field(default_factory=dict)
    # Money value of the energy now in the battery; None until cost tracking is seeded.
    battery_value: float | None = None
    # Cost of the energy taken out of the battery, driving and parked.
    driving_cost: float = 0.0
    parked_energy: float = 0.0
    parked_cost: float = 0.0
    parked_since: str | None = None  # ISO date
    trip_energy_start: float | None = None
    trip_cost_start: float | None = None
    last_trip: dict[str, Any] | None = None
    temp_bins: dict[str, dict[str, float]] = field(default_factory=dict)
    capacity_estimate: float | None = None

    @classmethod
    def new(cls) -> EngineState:
        """Fresh state for a new vehicle."""
        return cls()

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EngineState:
        """Rebuild from stored data, ignoring unknown keys and defaulting missing ones."""
        st = _build(cls, data)
        if "cost_home" not in data:
            # Before 0.2.0 only charging at home was priced.
            st.cost_home = st.cost
        st.soc =_build(SocAccountingState, data.get("soc") or {})
        st.model = _build(RangeModelState, data.get("model") or {})
        settings = _build(Settings, data.get("settings") or {})
        settings.notify = {**_default_notify(), **(settings.notify or {})}
        st.settings = settings
        periods = data.get("periods") or {}
        st.periods = add_distance(
            PeriodState(
                snapshots={k: _build(PeriodSnapshot, v) for k, v in (periods.get("snapshots") or {}).items()},
                previous=dict(periods.get("previous") or {}),
            )
        )
        session = data.get("session")
        st.session = _build(ChargingSession, session) if session else None
        st.alerts_armed = dict(data.get("alerts_armed") or {})
        st.temp_bins = {str(k): dict(v) for k, v in (data.get("temp_bins") or {}).items()}
        if "capacity_estimate" not in data:
            st.capacity_estimate = (st.model.last_result or {}).get("capacity_estimate_kwh")
        return st


def _build[T](cls: type[T], data: dict[str, Any]) -> T:
    names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    return cls(**{k: v for k, v in data.items() if k in names})
