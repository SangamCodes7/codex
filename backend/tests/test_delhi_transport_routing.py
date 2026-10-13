"""Focused transport-routing invariants for Delhi/Kushak Phase 8B.

These tests use a small in-memory directed graph.  They do not alter or
rewire the committed Delhi road artifact and keep the mode-specific behavior
independent from the large raster/model fixtures used by integration tests.
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.app.domain.delhi.routing import engine
from backend.app.domain.delhi.routing.network import SegmentEdge, _SegmentGeometry
from backend.app.domain.delhi.routing.transport import (
    assess_edge,
    edge_access,
    hazard_for_edge,
    transport_config,
)


class _Graph:
    def __init__(self, edges: list[SegmentEdge]) -> None:
        self.edge_index = {edge.edge_key: edge for edge in edges}
        self.adj = {}
        for edge in edges:
            self.adj.setdefault(edge.u, []).append(edge)

    @staticmethod
    def travel_time_s(edge: SegmentEdge) -> float:
        speeds = {"primary": 50.0, "residential": 25.0}
        return edge.length_m / (speeds.get(edge.highway, 25.0) / 3.6)


def _edge(key: str, u: int, v: int, highway: str, length_m: float) -> SegmentEdge:
    return SegmentEdge(
        edge_key=key,
        u=u,
        v=v,
        osm_id=int(key.replace("e", "")),
        road_id=key,
        name=key,
        highway=highway,
        length_m=length_m,
        geometry=_SegmentGeometry((float(u), 0.0), (float(v), 0.0)),
        reverse=False,
    )


def _decision(edge: SegmentEdge, mode: str, hazard: dict) -> dict:
    config = transport_config(mode)
    return assess_edge(
        edge=edge,
        mode=mode,
        base_risk="LOW_RISK",
        base_reason="synthetic computed frame",
        hazard=hazard,
        has_hazard_record=True,
        config=config,
    )


def test_safe_route_button_contract_is_mode_specific_at_the_engine_boundary():
    """Car and walk receive different thresholds and cost parameters."""
    car = transport_config("car")
    walk = transport_config("walk")

    assert car.depth_exclusion_m == 0.30
    assert walk.depth_exclusion_m == 0.15
    assert car.hazard_penalty_s_per_m != walk.hazard_penalty_s_per_m
    assert car.uncertainty_penalty_s_per_m != walk.uncertainty_penalty_s_per_m


def test_missing_velocity_is_unknown_not_zero():
    edge = _edge("e1", 0, 1, "residential", 100.0)
    decision = _decision(
        edge,
        "walk",
        {"depth_m": 0.02, "velocity_m_s": None, "source_type": "MODELLED"},
    )

    assert decision["uncertain"] is True
    assert decision["velocity_m_s"] is None
    assert "UNKNOWN" in decision["reason"]


def test_car_and_walk_can_choose_different_eligible_paths():
    direct_a = _edge("e1", 0, 1, "primary", 100.0)
    direct_b = _edge("e2", 1, 3, "primary", 100.0)
    detour_a = _edge("e3", 0, 2, "residential", 500.0)
    detour_b = _edge("e4", 2, 3, "residential", 500.0)
    graph = _Graph([direct_a, direct_b, detour_a, detour_b])

    assessments_car = {
        edge.edge_key: _decision(
            edge,
            "car",
            {"depth_m": 0.20 if edge.edge_key in {"e1", "e2"} else 0.0},
        )
        for edge in graph.edge_index.values()
    }
    assessments_walk = {
        edge.edge_key: _decision(
            edge,
            "walk",
            {"depth_m": 0.20 if edge.edge_key in {"e1", "e2"} else 0.0},
        )
        for edge in graph.edge_index.values()
    }

    car_path = engine._transport_dijkstra(
        graph, 0, 3, assessments_car, transport_config("car")
    )
    walk_path = engine._transport_dijkstra(
        graph, 0, 3, assessments_walk, transport_config("walk")
    )

    assert [edge.edge_key for edge in car_path or []] == ["e1", "e2"]
    assert [edge.edge_key for edge in walk_path or []] == ["e3", "e4"]


def test_same_route_is_allowed_when_mode_constraints_are_equal():
    first = _edge("e1", 0, 1, "residential", 100.0)
    second = _edge("e2", 1, 2, "residential", 100.0)
    graph = _Graph([first, second])
    assessments = {
        edge.edge_key: _decision(edge, "car", {"depth_m": 0.0})
        for edge in graph.edge_index.values()
    }

    path = engine._transport_dijkstra(
        graph, 0, 2, assessments, transport_config("car")
    )
    assert [edge.edge_key for edge in path or []] == ["e1", "e2"]


def test_accessibility_and_exclusion_rules_remove_ineligible_edges():
    footway = SimpleNamespace(highway="footway")
    motorway = SimpleNamespace(highway="motorway")
    trunk_without_access = SimpleNamespace(highway="trunk")

    assert edge_access(footway, "car")[0] is False
    assert edge_access(motorway, "walk")[0] is False
    assert edge_access(trunk_without_access, "walk")[0] is False


def test_exclusion_and_unknown_hazard_prevent_false_safe_routes():
    edge = _edge("e1", 0, 1, "residential", 100.0)
    blocked = _decision(edge, "walk", {"depth_m": 0.15})
    assert blocked["blocked"] is True
    assert blocked["risk_state"] == "BLOCKED"

    _, has_record = hazard_for_edge("missing", {})
    assert has_record is False
    unknown = assess_edge(
        edge=edge,
        mode="car",
        base_risk="LOW_RISK",
        base_reason="synthetic computed frame",
        hazard={},
        has_hazard_record=False,
        config=transport_config("car"),
    )
    assert unknown["risk_state"] == "UNKNOWN"
    assert unknown["uncertain"] is True
