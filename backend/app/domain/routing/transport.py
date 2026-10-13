"""Mumbai V1 routing screens, independent of Delhi's hydraulic contract."""
from dataclasses import dataclass
from typing import Literal

TransportMode = Literal["car", "walk"]


@dataclass(frozen=True)
class TransportPolicy:
    hazard_depth_m: float
    exclusion_depth_m: float
    low_depth_penalty: float
    medium_depth_penalty: float
    uncertainty_penalty: float
    excluded_highways: frozenset[str]

    def as_metadata(self) -> dict:
        return {
            "hazard_depth_m": self.hazard_depth_m,
            "exclusion_depth_m": self.exclusion_depth_m,
            "low_depth_penalty": self.low_depth_penalty,
            "medium_depth_penalty": self.medium_depth_penalty,
            "uncertainty_penalty": self.uncertainty_penalty,
            "excluded_highways": sorted(self.excluded_highways),
        }


# PROTOTYPE screening weights, not calibrated travel times or safety standards.
# Car retains V1's HIGH (0.30 m) exclusion; walking excludes MEDIUM (0.15 m).
# The walking screen is informed by NWS's six-inch moving-water warning:
# https://www.weather.gov/safety/flood-turn-around-dont-drown
TRANSPORT_POLICIES = {
    "car": TransportPolicy(0.10, 0.30, 2.0, 8.0, 0.5, frozenset({
        "footway", "path", "pedestrian", "steps", "cycleway", "bridleway",
        "corridor", "construction", "proposed",
    })),
    "walk": TransportPolicy(0.05, 0.15, 6.0, 12.0, 1.0, frozenset({
        "motorway", "motorway_link", "construction", "proposed",
    })),
}


def edge_access(edge, mode: TransportMode) -> tuple[bool, bool]:
    """Eligible, uncertain: highway inference is not a pedestrian survey."""
    tags = getattr(edge, "access_tags", {})
    closed = {"no", "private", "emergency", "customers"}
    allowed = {"yes", "designated", "permissive"}
    specific = (tags.get("motorcar") or tags.get("motor_vehicle")) if mode == "car" else tags.get("foot")
    access = specific or tags.get("access")
    if access in closed or edge.highway in TRANSPORT_POLICIES[mode].excluded_highways:
        return False, False
    if mode == "car":
        return tags.get("foot") != "only", edge.highway == "unclassified"
    if edge.highway in {"trunk", "trunk_link"} and specific not in allowed:
        return False, True
    return True, specific not in allowed
