"""Transport-aware routing rules for the Delhi/Kushak road graph.

The Kushak routing inputs do not currently contain surveyed pedestrian
facilities, road-closure feeds, or road-water velocity observations.  This
module therefore keeps two things explicit:

* access is a conservative, OSM-highway-class screening rule; it is not a
  sidewalk or accessibility survey; and
* depth limits are provisional screening parameters, not universal safety
  standards.  They are derived from the NOAA/NWS *Turn Around, Don't Drown*
  warnings (about 0.15 m for a fast-moving adult and about 0.30 m for most
  cars) and are intentionally configurable.

Missing hazard inputs are represented as UNKNOWN.  In particular, a missing
velocity is never converted to zero velocity.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Dict, Mapping, Optional, Tuple


TransportMode = str  # runtime-validated values: "car" | "walk"


@dataclass(frozen=True)
class TransportRiskConfig:
    """Mode-specific routing parameters.

    ``depth_*`` and optional velocity limits are *provisional screening
    parameters*.  The project has no calibrated vehicle/pedestrian hazard
    study, so these values must not be presented as a guarantee of safety.
    ``None`` velocity limits mean the factor is not applied until a
    documented project-specific value is configured.
    """

    mode: TransportMode
    depth_hazard_m: float
    depth_exclusion_m: float
    velocity_hazard_m_s: Optional[float] = None
    velocity_exclusion_m_s: Optional[float] = None
    depth_velocity_hazard_m2_s: Optional[float] = None
    depth_velocity_exclusion_m2_s: Optional[float] = None
    hazard_penalty_s_per_m: float = 0.30
    uncertainty_penalty_s_per_m: float = 0.16
    source: str = (
        "Provisional screening parameters informed by NOAA/NWS Turn Around, "
        "Don't Drown guidance (weather.gov/safety/flood-turn-around-dont-drown); "
        "not calibrated for Delhi/Kushak"
    )

    def as_metadata(self) -> dict:
        return asdict(self)


# NWS warns that approximately six inches of fast-moving water can knock over
# an adult and twelve inches of rushing water can carry away most cars.  The
# exclusion values below are rounded metric screens aligned to those warnings;
# the lower hazard values are an intentionally conservative early-warning
# margin. Neither is a passability standard, and the route never claims a
# guarantee from them.
TRANSPORT_RISK_CONFIG: Dict[TransportMode, TransportRiskConfig] = {
    "car": TransportRiskConfig(
        mode="car",
        depth_hazard_m=0.10,
        depth_exclusion_m=0.30,
        hazard_penalty_s_per_m=0.30,
        uncertainty_penalty_s_per_m=0.16,
    ),
    "walk": TransportRiskConfig(
        mode="walk",
        depth_hazard_m=0.05,
        depth_exclusion_m=0.15,
        # Walking exposure is penalized more heavily because a person has no
        # vehicle protection and the committed network has no surveyed
        # sidewalk/crossing layer.  This is a configurable provisional
        # screening weight, not a calibrated pedestrian travel model.
        hazard_penalty_s_per_m=0.45,
        uncertainty_penalty_s_per_m=0.24,
    ),
}


def validate_transport_mode(mode: Optional[str]) -> Optional[str]:
    """Return a normalized mode or raise a useful validation error."""

    if mode is None:
        return None
    normalized = mode.strip().lower()
    if normalized not in TRANSPORT_RISK_CONFIG:
        raise ValueError("transport_mode must be one of: car, walk")
    return normalized


def transport_config(
    mode: TransportMode,
    overrides: Optional[Mapping[str, Any]] = None,
) -> TransportRiskConfig:
    """Return the configured mode rules, optionally with test/runtime overrides."""

    normalized = validate_transport_mode(mode)
    assert normalized is not None
    base = TRANSPORT_RISK_CONFIG[normalized]
    if not overrides:
        return base
    allowed = {
        key: value
        for key, value in overrides.items()
        if key in base.__dataclass_fields__ and key != "mode"
    }
    return replace(base, **allowed)


def _tag(edge: Any, name: str) -> Optional[str]:
    """Read optional OSM-like tags from a test edge without requiring them."""

    value = getattr(edge, name, None)
    if value is None:
        return None
    return str(value).strip().lower() or None


_CAR_EXCLUDED_HIGHWAYS = frozenset(
    {
        "footway",
        "path",
        "pedestrian",
        "steps",
        "cycleway",
        "bridleway",
        "corridor",
        "construction",
        "proposed",
    }
)
_WALK_EXCLUDED_HIGHWAYS = frozenset({"motorway", "motorway_link"})
_WALK_REQUIRES_EXPLICIT_ACCESS = frozenset({"trunk", "trunk_link"})
_CLOSED_ACCESS = frozenset({"no", "private", "emergency", "customers"})


def edge_access(
    edge: Any,
    mode: TransportMode,
) -> Tuple[bool, bool, str]:
    """Return ``(eligible, access_uncertain, reason)`` for one edge.

    The committed Delhi extract contains highway/oneway/surface/lanes but no
    sidewalk, crossing, or mode-access tags.  Highway classes are therefore a
    conservative, explicitly provisional proxy.  If richer OSM tags are
    attached to a future graph edge, explicit ``foot``/``motor_vehicle``/
    ``motorcar``/``access`` values take precedence.
    """

    normalized = validate_transport_mode(mode)
    assert normalized is not None
    highway = str(getattr(edge, "highway", "unclassified") or "unclassified").lower()
    access = _tag(edge, "access")
    foot = _tag(edge, "foot")
    motor_vehicle = _tag(edge, "motor_vehicle")
    motorcar = _tag(edge, "motorcar")

    if access in _CLOSED_ACCESS:
        return False, False, f"explicit access={access} excludes {normalized} travel"

    if normalized == "car":
        if motor_vehicle in _CLOSED_ACCESS or motorcar in _CLOSED_ACCESS:
            tag = motor_vehicle or motorcar
            return False, False, f"explicit {('motor_vehicle' if motor_vehicle else 'motorcar')}={tag} excludes car"
        if highway in _CAR_EXCLUDED_HIGHWAYS:
            return False, False, f"highway={highway} is not vehicle-accessible"
        if foot == "only":
            return False, False, "explicit foot=only excludes cars"
        return True, False, (
            "vehicle access inferred from OSM highway class; no verified closure feed"
        )

    if foot in _CLOSED_ACCESS or foot == "no":
        return False, False, f"explicit foot={foot} excludes walking"
    if highway in _WALK_EXCLUDED_HIGHWAYS:
        return False, False, f"highway={highway} is not pedestrian-accessible"
    if highway in _WALK_REQUIRES_EXPLICIT_ACCESS and foot not in {"yes", "designated", "permissive"}:
        return False, True, (
            f"highway={highway} has no explicit pedestrian access in the committed extract"
        )
    # Ordinary road centerlines are only a pedestrian-network proxy: the
    # extract does not establish sidewalks, crossings, or accessibility.
    return True, not bool(foot), (
        "walking uses the OSM road centerline as a provisional pedestrian proxy; "
        "sidewalks/crossings are not represented"
    )


def normalize_hazard(raw: Any, source_type: str = "UNKNOWN") -> dict:
    """Normalize an edge hazard record without filling missing values."""

    if raw is None:
        raw = {}
    if isinstance(raw, (int, float)):
        raw = {"depth_m": float(raw)}
    if not isinstance(raw, Mapping):
        raw = {}
    depth = raw.get("depth_m")
    velocity = raw.get("velocity_m_s")
    depth_m = (
        float(depth)
        if isinstance(depth, (int, float)) and math.isfinite(depth) and depth >= 0.0
        else None
    )
    velocity_m_s = (
        float(velocity)
        if isinstance(velocity, (int, float)) and math.isfinite(velocity) and velocity >= 0.0
        else None
    )
    return {
        "depth_m": depth_m,
        "velocity_m_s": velocity_m_s,
        "uncertain": bool(raw.get("uncertain", False)),
        "closure_verified": bool(raw.get("closure_verified", False)),
        "rapid_change": bool(raw.get("rapid_change", False)),
        "source_type": str(raw.get("source_type") or source_type),
        "reason": str(raw.get("reason") or ""),
    }


def hazard_for_edge(
    edge_key: str,
    edge_hazards: Optional[Mapping[str, Any]],
) -> Tuple[dict, bool]:
    """Return a hazard and whether the edge has an explicit hazard record."""

    if edge_hazards is None or edge_key not in edge_hazards:
        return normalize_hazard(None), False
    return normalize_hazard(edge_hazards[edge_key]), True


def depth_hazards_from_depths(
    edge_depths: Mapping[str, Optional[float]],
    source_type: str,
    *,
    uncertain: bool = True,
) -> Dict[str, dict]:
    """Adapt the existing modelled road-depth map to the routing contract."""

    return {
        edge_key: {
            "depth_m": depth,
            "velocity_m_s": None,
            "uncertain": uncertain and depth is not None and depth > 0.0,
            "source_type": source_type,
            "reason": (
                "modelled road depth available; water velocity is not available"
                if depth is not None and depth > 0.0
                else "modelled road depth sample"
            ),
        }
        for edge_key, depth in edge_depths.items()
    }


def assess_edge(
    edge: Any,
    mode: TransportMode,
    base_risk: str,
    base_reason: str,
    hazard: dict,
    has_hazard_record: bool,
    config: TransportRiskConfig,
) -> dict:
    """Combine mode access, model risk, and available hazard fields."""

    eligible, access_uncertain, access_reason = edge_access(edge, mode)
    result = {
        "risk_state": base_risk,
        "reason": base_reason,
        "eligible": eligible,
        "access_uncertain": access_uncertain,
        "uncertain": access_uncertain,
        "hazardous": False,
        "blocked": False,
        "depth_m": hazard.get("depth_m"),
        "velocity_m_s": hazard.get("velocity_m_s"),
        "source_type": hazard.get("source_type", "UNKNOWN"),
    }
    if not eligible:
        result["blocked"] = True
        result["reason"] = access_reason
        return result

    if access_uncertain:
        result["reason"] = f"{access_reason}; access evidence is incomplete"

    if base_risk == "ELEVATED_RISK":
        result["hazardous"] = True
        result["risk_state"] = "ELEVATED_RISK"

    if not has_hazard_record:
        result["uncertain"] = True
        result["risk_state"] = "UNKNOWN"
        result["reason"] = (
            f"{result['reason']}; no edge-level depth/velocity record, so hazard is UNKNOWN"
        )
        if base_risk == "BLOCKED":
            result["blocked"] = True
        return result

    if hazard.get("closure_verified"):
        result["blocked"] = True
        result["risk_state"] = "BLOCKED"
        result["reason"] = "verified closure/impassability for the selected transport mode"
        return result

    depth = hazard.get("depth_m")
    velocity = hazard.get("velocity_m_s")
    dv = depth * velocity if depth is not None and velocity is not None else None

    if depth is None:
        result["uncertain"] = True
        result["risk_state"] = "UNKNOWN"
        result["reason"] = f"{result['reason']}; flood depth is UNKNOWN"
    else:
        if depth >= config.depth_exclusion_m:
            result["blocked"] = True
            result["risk_state"] = "BLOCKED"
            result["reason"] = (
                f"modelled depth {depth:.3f} m meets the provisional "
                f"{mode} exclusion threshold {config.depth_exclusion_m:.3f} m"
            )
            return result
        if depth >= config.depth_hazard_m:
            result["hazardous"] = True
            result["risk_state"] = "ELEVATED_RISK"
            result["reason"] = (
                f"modelled depth {depth:.3f} m meets the provisional {mode} "
                f"hazard threshold {config.depth_hazard_m:.3f} m"
            )

    if velocity is not None:
        if config.velocity_exclusion_m_s is not None and velocity >= config.velocity_exclusion_m_s:
            result["blocked"] = True
            result["risk_state"] = "BLOCKED"
            result["reason"] = (
                f"modelled velocity {velocity:.3f} m/s meets the configured "
                f"{mode} exclusion threshold"
            )
            return result
        if config.velocity_hazard_m_s is not None and velocity >= config.velocity_hazard_m_s:
            result["hazardous"] = True
            result["risk_state"] = "ELEVATED_RISK"
            result["reason"] = f"modelled velocity {velocity:.3f} m/s is elevated for {mode}"
    elif depth is not None and depth > 0.0:
        # A positive depth without velocity cannot establish a lower-risk
        # walking or vehicle passage. Keep it visible as uncertainty.
        result["uncertain"] = True
        result["reason"] = f"{result['reason']}; water velocity is UNKNOWN, not zero"

    if dv is not None:
        if (
            config.depth_velocity_exclusion_m2_s is not None
            and dv >= config.depth_velocity_exclusion_m2_s
        ):
            result["blocked"] = True
            result["risk_state"] = "BLOCKED"
            result["reason"] = f"depth-velocity product {dv:.3f} m²/s meets configured exclusion"
            return result
        if config.depth_velocity_hazard_m2_s is not None and dv >= config.depth_velocity_hazard_m2_s:
            result["hazardous"] = True
            result["risk_state"] = "ELEVATED_RISK"
            result["reason"] = f"depth-velocity product {dv:.3f} m²/s is elevated"

    if hazard.get("uncertain") or hazard.get("rapid_change"):
        result["uncertain"] = True
        result["reason"] = f"{result['reason']}; input is flagged uncertain or rapidly changing"

    if base_risk == "BLOCKED":
        result["blocked"] = True
        result["risk_state"] = "BLOCKED"
    elif base_risk == "UNKNOWN" and not result["hazardous"]:
        result["uncertain"] = True
        result["risk_state"] = "UNKNOWN"

    return result
