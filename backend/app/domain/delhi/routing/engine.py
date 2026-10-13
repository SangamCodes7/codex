"""Deterministic flood-aware route engine (ONE core, all modes).

Cost model (documented; weights are ASSUMED engineering constants, not
observed/calibrated values):

    edge_cost = travel_time_s
              + RISK_PENALTY_S_PER_M[risk_state] * length_m

    RISK_PENALTY_S_PER_M:
      LOW_RISK      0.00   (no modeled loading)
      UNKNOWN       0.09   (~1.5 min/km: uncertainty is penalized, never
                              treated as free, and never treated as SAFE)
      ELEVATED_RISK 0.18   (~3 min/km: modeled conveyance loading)
      BLOCKED       hard-excluded from candidate routes

The optimizer therefore prefers lower modeled flood exposure, not merely
short distance; routes through UNKNOWN segments are only returned when
no better-supported alternative exists, and are explicitly labeled.

Determinism: identical inputs (graph version, risk states, snapping,
constants) produce identical routes and route_run_id.
"""

from __future__ import annotations

import hashlib
import heapq
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .network import DelhiRoadGraph, SegmentEdge, get_road_graph, road_network_provenance
from .risk import (
    CORRIDOR_MAP_DISTANCE_M,
    RiskState,
    aggregate_reach_states,
    build_edge_reach_map,
    freshness_state,
    route_evidence_state,
    segment_evidence,
    translate_reach_to_road_risk,
)
from .state_source import ModeReachStates, now_utc
from .transport import (
    TransportRiskConfig,
    assess_edge,
    hazard_for_edge,
    transport_config,
    validate_transport_mode,
)

RISK_PENALTY_S_PER_M: Dict[RiskState, float] = {
    "LOW_RISK": 0.0,
    "UNKNOWN": 0.09,       # ASSUMED: ~1.5 min/km uncertainty penalty
    "ELEVATED_RISK": 0.18, # ASSUMED: ~3 min/km modeled-loading penalty
    "BLOCKED": float("inf"),  # hard-excluded
}

# Assumed divergence factor for the alternative-route search: edges of the
# recommended route are re-costed with this multiplier so the alternative
# is genuinely distinct rather than a trivial variation.
ALTERNATIVE_DIVERGENCE_FACTOR = 4.0
# Two routes sharing more than this edge fraction are considered the same.
MIN_DISTINCT_EDGE_FRACTION = 0.7


@dataclass
class RouteCandidate:
    edges: List[SegmentEdge]
    risk_by_edge: Dict[str, Tuple[RiskState, str]] = field(default_factory=dict)
    transport_by_edge: Dict[str, dict] = field(default_factory=dict)
    total_distance_m: float = 0.0
    travel_time_s: float = 0.0
    risk_penalty_s: float = 0.0
    counts: Dict[str, int] = field(default_factory=dict)

    @property
    def total_cost_s(self) -> float:
        return self.travel_time_s + self.risk_penalty_s

    def counts_summary(self) -> Dict[str, int]:
        summary = {
            "blocked": self.counts.get("BLOCKED", 0),
            "elevated_risk": self.counts.get("ELEVATED_RISK", 0),
            "unknown": self.counts.get("UNKNOWN", 0),
            "low_risk": self.counts.get("LOW_RISK", 0),
        }
        if self.transport_by_edge:
            summary["uncertain"] = sum(
                1 for record in self.transport_by_edge.values() if record.get("uncertain")
            )
            summary["hazardous"] = sum(
                1 for record in self.transport_by_edge.values() if record.get("hazardous")
            )
        return summary


def _edge_risk_states(
    mode_states: ModeReachStates,
) -> Dict[str, Tuple[RiskState, str]]:
    """Translate aggregated reach states into per-edge road risk."""
    observations = aggregate_reach_states(mode_states.member_states)
    reach_risk = {
        reach_id: translate_reach_to_road_risk(obs)
        for reach_id, obs in observations.items()
    }
    edge_map = build_edge_reach_map()
    out: Dict[str, Tuple[RiskState, str]] = {}
    for edge_key, mapped_reach in edge_map.items():
        if mapped_reach is None:
            out[edge_key] = (
                "UNKNOWN",
                "outside the modeled Kushak corridor; no defensible road-model relationship",
            )
        else:
            out[edge_key] = reach_risk[mapped_reach]
    return out


def _dijkstra(
    graph: DelhiRoadGraph,
    source: int,
    target: int,
    risk_by_edge: Dict[str, Tuple[RiskState, str]],
    cost_multiplier_by_road: Optional[Dict[str, float]] = None,
) -> Optional[List[SegmentEdge]]:
    """Deterministic Dijkstra over the flood-aware cost. BLOCKED edges are
    hard-excluded. Tie-breaking is by (cost, node) for determinism."""
    multiplier = cost_multiplier_by_road or {}
    dist: Dict[int, float] = {source: 0.0}
    prev: Dict[int, Tuple[int, str]] = {}
    visited = set()
    heap: List[Tuple[float, int]] = [(0.0, source)]
    while heap:
        d, u = heapq.heappop(heap)
        if u in visited:
            continue
        visited.add(u)
        if u == target:
            break
        for edge in graph.adj.get(u, ()):  # directed
            if edge.v in visited:
                continue
            risk, _ = risk_by_edge.get(edge.edge_key, ("UNKNOWN", ""))
            if RISK_PENALTY_S_PER_M.get(risk, float("inf")) == float("inf"):
                continue  # BLOCKED: hard-excluded from candidates
            cost = graph.travel_time_s(edge) + RISK_PENALTY_S_PER_M[risk] * edge.length_m
            cost *= multiplier.get(edge.road_id, 1.0)
            nd = d + cost
            if nd < dist.get(edge.v, float("inf")) - 1e-12:
                dist[edge.v] = nd
                prev[edge.v] = (u, edge.edge_key)
                heapq.heappush(heap, (nd, edge.v))
    if target not in dist:
        return None
    edges: List[SegmentEdge] = []
    cur = target
    while cur != source:
        u, key = prev[cur]
        edges.append(graph.edge_index[key])
        cur = u
    edges.reverse()
    return edges


def _transport_assessments(
    graph: DelhiRoadGraph,
    transport_mode: str,
    base_risk_by_edge: Mapping[str, Tuple[RiskState, str]],
    edge_hazards: Optional[Mapping[str, Any]],
    config: TransportRiskConfig,
) -> Dict[str, dict]:
    """Build the immutable per-edge transport decision table.

    The table is deliberately separate from the Dijkstra loop.  This makes
    the access and hazard decisions inspectable in the response and prevents
    a missing hazard record from being accidentally treated as dry pavement.
    """

    assessments: Dict[str, dict] = {}
    for edge_key, edge in graph.edge_index.items():
        base_risk, base_reason = base_risk_by_edge.get(
            edge_key,
            ("UNKNOWN", "no flood-risk translation for this graph edge"),
        )
        hazard, has_record = hazard_for_edge(edge_key, edge_hazards)
        assessments[edge_key] = assess_edge(
            edge=edge,
            mode=transport_mode,
            base_risk=base_risk,
            base_reason=base_reason,
            hazard=hazard,
            has_hazard_record=has_record,
            config=config,
        )
    return assessments


def _transport_dijkstra(
    graph: DelhiRoadGraph,
    source: int,
    target: int,
    assessments: Mapping[str, dict],
    config: TransportRiskConfig,
    cost_multiplier_by_road: Optional[Dict[str, float]] = None,
) -> Optional[List[SegmentEdge]]:
    """Deterministic Dijkstra with access filtering and mode-specific costs."""

    multiplier = cost_multiplier_by_road or {}
    dist: Dict[int, float] = {source: 0.0}
    prev: Dict[int, Tuple[int, str]] = {}
    visited = set()
    heap: List[Tuple[float, int]] = [(0.0, source)]
    while heap:
        current_cost, u = heapq.heappop(heap)
        if u in visited:
            continue
        visited.add(u)
        if u == target:
            break
        for edge in graph.adj.get(u, ()):
            if edge.v in visited:
                continue
            decision = assessments.get(edge.edge_key)
            if not decision or decision["blocked"]:
                continue
            risk = decision["risk_state"]
            base_penalty = RISK_PENALTY_S_PER_M.get(risk, RISK_PENALTY_S_PER_M["UNKNOWN"])
            if decision.get("hazardous"):
                base_penalty = max(base_penalty, config.hazard_penalty_s_per_m)
            if decision.get("uncertain"):
                base_penalty = max(base_penalty, config.uncertainty_penalty_s_per_m)
            cost = graph.travel_time_s(edge) + base_penalty * edge.length_m
            cost *= multiplier.get(edge.road_id, 1.0)
            next_cost = current_cost + cost
            if next_cost < dist.get(edge.v, float("inf")) - 1e-12:
                dist[edge.v] = next_cost
                prev[edge.v] = (u, edge.edge_key)
                heapq.heappush(heap, (next_cost, edge.v))
    if target not in dist:
        return None
    path: List[SegmentEdge] = []
    current = target
    while current != source:
        parent, edge_key = prev[current]
        path.append(graph.edge_index[edge_key])
        current = parent
    path.reverse()
    return path


def _build_transport_candidate(
    edges: List[SegmentEdge],
    assessments: Mapping[str, dict],
    config: TransportRiskConfig,
) -> RouteCandidate:
    candidate = RouteCandidate(edges=edges)
    for edge in edges:
        decision = dict(assessments[edge.edge_key])
        candidate.transport_by_edge[edge.edge_key] = decision
        risk = decision["risk_state"]
        reason = decision["reason"]
        candidate.risk_by_edge[edge.edge_key] = (risk, reason)
        candidate.total_distance_m += edge.length_m
        penalty = RISK_PENALTY_S_PER_M.get(risk, RISK_PENALTY_S_PER_M["UNKNOWN"])
        if decision.get("hazardous"):
            penalty = max(penalty, config.hazard_penalty_s_per_m)
        if decision.get("uncertain"):
            penalty = max(penalty, config.uncertainty_penalty_s_per_m)
        candidate.risk_penalty_s += penalty * edge.length_m
        candidate.counts[risk] = candidate.counts.get(risk, 0) + 1
    candidate.travel_time_s = sum(get_road_graph().travel_time_s(edge) for edge in edges)
    return candidate


def _validate_route_continuity(edges: List[SegmentEdge]) -> None:
    """Reject malformed paths before they reach GeoJSON or the map."""

    for previous, current in zip(edges, edges[1:]):
        if previous.v != current.u:
            raise ValueError("ROUTE_INVALID: returned edges are not topologically continuous")


def _transport_classification(candidate: RouteCandidate) -> Tuple[str, str]:
    counts = candidate.counts_summary()
    if counts.get("uncertain", 0) or counts.get("unknown", 0):
        return "UNKNOWN", (
            "required transport hazard or access evidence is missing on one or more "
            "route segments; UNKNOWN is not treated as safe"
        )
    if counts.get("hazardous", 0) or counts.get("elevated_risk", 0):
        return "CAUTION", "the route remains eligible but includes elevated modeled exposure"
    return "LOWER_RISK", (
        "no configured mode-specific exclusion or elevated hazard criterion was "
        "met on the eligible route; this is not a safety guarantee"
    )


def _transport_explanation(
    mode: str,
    candidate: RouteCandidate,
    classification: str,
    reason: str,
    alternative: Optional[RouteCandidate],
    no_alternative_reason: Optional[str],
) -> List[str]:
    counts = candidate.counts_summary()
    lines = [
        f"{mode.title()} routing used mode-specific access rules and flood penalties.",
        f"Route classification: {classification} — {reason}.",
    ]
    if counts.get("hazardous", 0):
        lines.append(f"{counts['hazardous']} segment(s) carry elevated modeled exposure for {mode}.")
    if counts.get("uncertain", 0):
        lines.append(
            f"{counts['uncertain']} segment(s) have uncertain access or hazard inputs; "
            "missing velocity is UNKNOWN, not zero."
        )
    if alternative is not None:
        delta = (alternative.total_distance_m - candidate.total_distance_m) / 1000.0
        lines.append(
            f"The alternative is {abs(delta):.2f} km "
            f"{'longer' if delta >= 0 else 'shorter'} under the same active model state."
        )
    elif no_alternative_reason:
        lines.append(f"No distinct alternative: {no_alternative_reason}.")
    lines.append(
        "The underlying Delhi network has no surveyed pedestrian facilities, verified "
        "road-closure feed, or road-water velocity field; use this as a modeled screening result."
    )
    return lines


def _build_candidate(
    edges: List[SegmentEdge], risk_by_edge: Dict[str, Tuple[RiskState, str]]
) -> RouteCandidate:
    candidate = RouteCandidate(edges=edges)
    for edge in edges:
        risk, reason = risk_by_edge[edge.edge_key]
        candidate.risk_by_edge[edge.edge_key] = (risk, reason)
        candidate.total_distance_m += edge.length_m
        candidate.risk_penalty_s += RISK_PENALTY_S_PER_M.get(risk, 0.0) * edge.length_m
        candidate.counts[risk] = candidate.counts.get(risk, 0) + 1
    candidate.travel_time_s = sum(
        get_road_graph().travel_time_s(e) for e in edges
    )
    return candidate


def _route_geojson(edges: List[SegmentEdge]) -> dict:
    coords: List[List[float]] = []
    for edge in edges:
        if not coords:
            coords.append(list(edge.coords_4326[0]))
        coords.append(list(edge.coords_4326[1]))
    return {
        "type": "Feature",
        "properties": {},
        "geometry": {"type": "LineString", "coordinates": coords},
    }


def _route_risk_geojson(edges: List[SegmentEdge], risk_by_edge: Dict[str, Tuple[RiskState, str]]) -> dict:
    features = []
    for edge in edges:
        risk, _ = risk_by_edge[edge.edge_key]
        if risk in ("UNKNOWN", "ELEVATED_RISK", "BLOCKED"):
            features.append({
                "type": "Feature",
                "properties": {"segment_id": edge.edge_key, "risk_state": risk, "name": edge.name},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [list(edge.coords_4326[0]), list(edge.coords_4326[1])],
                },
            })
    return {"type": "FeatureCollection", "features": features}


def _explain(
    recommended: RouteCandidate,
    alternative: Optional[RouteCandidate],
    evidence_state: str,
    evidence_reason: str,
    no_alternative_reason: Optional[str],
) -> List[str]:
    """Structured facts -> factual sentences (rule-based, no invention)."""
    lines: List[str] = []
    counts = recommended.counts_summary()
    if counts["elevated_risk"] == 0 and counts["unknown"] == 0:
        lines.append(
            "Recommended route follows road segments with no modeled flood "
            "loading on the mapped corridor."
        )
    else:
        parts = []
        if counts["elevated_risk"]:
            parts.append(
                f"{counts['elevated_risk']} elevated-risk segment(s) adjacent "
                "to the modeled corridor"
            )
        if counts["unknown"]:
            parts.append(
                f"{counts['unknown']} UNKNOWN segment(s) with no model-state "
                "coverage"
            )
        lines.append(
            "Recommended route contains " + " and ".join(parts) + "."
        )
    if alternative is not None:
        d_dist = (alternative.total_distance_m - recommended.total_distance_m) / 1000.0
        d_time = (alternative.travel_time_s - recommended.travel_time_s) / 60.0
        a_counts = alternative.counts_summary()
        delta_elev = a_counts["elevated_risk"] - counts["elevated_risk"]
        delta_unknown = a_counts["unknown"] - counts["unknown"]
        trade = []
        if delta_elev != 0:
            trade.append(
                f"{abs(delta_elev)} {'more' if delta_elev > 0 else 'fewer'} elevated-risk segment(s)"
            )
        if delta_unknown != 0:
            trade.append(
                f"{abs(delta_unknown)} {'more' if delta_unknown > 0 else 'fewer'} UNKNOWN segment(s)"
            )
        lines.append(
            f"Alternative route is {abs(d_dist):.2f} km "
            f"{'longer' if d_dist >= 0 else 'shorter'} and "
            f"{abs(d_time):.1f} min {'slower' if d_time >= 0 else 'faster'} "
            + (", with " + " and ".join(trade) if trade else "with the same risk profile")
            + "."
        )
    elif no_alternative_reason:
        lines.append(f"No supported alternative: {no_alternative_reason}")
    lines.append(f"Route evidence state: {evidence_state} — {evidence_reason}")
    return lines


def _compute_transport_route(
    *,
    graph: DelhiRoadGraph,
    origin_lon: float,
    origin_lat: float,
    dest_lon: float,
    dest_lat: float,
    transport_mode: str,
    mode_states: Optional[ModeReachStates],
    model_timestamp: Optional[str],
    forcing_source: Optional[str],
    mode_freshness_note: str,
    edge_hazards: Optional[Mapping[str, Any]],
    state_provenance: Optional[Mapping[str, Any]],
    config_overrides: Optional[Mapping[str, Any]],
) -> dict:
    """Transport branch of the same Delhi routing engine."""

    normalized = validate_transport_mode(transport_mode)
    assert normalized is not None
    config = transport_config(normalized, config_overrides)
    o_idx, o_dist, o_name = graph.snap(origin_lon, origin_lat)
    d_idx, d_dist, d_name = graph.snap(dest_lon, dest_lat)

    if mode_states is not None and mode_states.status in ("COMPUTED", "EXECUTED") and mode_states.member_states:
        base_risk = _edge_risk_states(mode_states)
        status = "COMPUTED"
    elif mode_states is None and edge_hazards is not None:
        # Scenario surface products can provide a complete edge-level hazard
        # field without passing through the 1-D reach adapter.  Start from a
        # computed/no-loading base and let the transport hazard table apply
        # depth, velocity, and UNKNOWN rules per edge.
        base_risk = {
            edge_key: ("LOW_RISK", "edge-level surface hazard field supplied for this frame")
            for edge_key in graph.edge_index
        }
        status = "COMPUTED"
    else:
        base_risk = {
            edge_key: (
                "UNKNOWN",
                "hydraulic model state unavailable for this route frame",
            )
            for edge_key in graph.edge_index
        }
        status = "UNKNOWN_RISK_STATE"

    assessments = _transport_assessments(
        graph, normalized, base_risk, edge_hazards, config
    )
    edges = _transport_dijkstra(graph, o_idx, d_idx, assessments, config)

    state_meta = dict(state_provenance or {})
    if state_meta.get("state_id") is None and state_meta.get("routing_state_id") is not None:
        state_meta["state_id"] = state_meta["routing_state_id"]
    state_meta.setdefault("mode", mode_states.mode if mode_states else "UNKNOWN")
    state_meta.setdefault("event_id", mode_states.event_id if mode_states else None)
    state_meta.setdefault("timestep_index", mode_states.timestep_index if mode_states else None)
    state_meta.setdefault("model_timestamp", model_timestamp)
    state_meta.setdefault("forcing_source", forcing_source)
    state_meta.setdefault("data_freshness", mode_freshness_note)
    state_meta.setdefault("city", "delhi")
    state_meta.setdefault("model_version", "Delhi/Kushak Phase 8B evidence-constrained routing")
    state_meta.setdefault("transport_mode", normalized)

    def empty_response(reason: str) -> dict:
        run_id = hashlib.sha256(
            repr((normalized, state_meta, round(origin_lon, 6), round(origin_lat, 6),
                 round(dest_lon, 6), round(dest_lat, 6), "NO_SAFE_ROUTE")).encode()
        ).hexdigest()[:16]
        return {
            "status": status,
            "mode": mode_states.mode if mode_states else state_meta.get("mode", "UNKNOWN"),
            "event_id": mode_states.event_id if mode_states else state_meta.get("event_id"),
            "timestep_index": mode_states.timestep_index if mode_states else state_meta.get("timestep_index"),
            "transport_mode": normalized,
            "route_run_id": run_id,
            "generated_at": now_utc().isoformat(),
            "origin": {"coords": [origin_lon, origin_lat], "snapped_road": o_name, "snap_distance_m": round(o_dist, 1)},
            "destination": {"coords": [dest_lon, dest_lat], "snapped_road": d_name, "snap_distance_m": round(d_dist, 1)},
            "recommended_route": None,
            "alternative_route": None,
            "no_alternative_reason": reason,
            "route_comparison": {"recommended": None, "alternative": None},
            "evidence_state": {"route_state": "NO_SAFE_ROUTE", "reason": reason, "derivation": "all ineligible, blocked, or disconnected transport edges were excluded"},
            "safety_classification": "NO_SAFE_ROUTE",
            "explanation": [
                f"No eligible {normalized} route satisfies the configured access and flood constraints.",
                reason,
                "The system did not return an unsafe route relabeled as safe.",
            ],
            "claim_policy": "No safe route was established for this transport mode and active model state.",
            "model_state": state_meta,
            "provenance": {
                "transport_mode": normalized,
                "transport_risk_configuration": config.as_metadata(),
                "road_network_source": road_network_provenance().get("source"),
                "risk_mapping_method": "mode-specific access plus depth/velocity/uncertainty edge assessment",
                "velocity_data": "UNKNOWN when not supplied; never assumed zero",
                "model_state": state_meta,
                "data_freshness": mode_freshness_note,
            },
            "data_freshness": {
                "note": mode_freshness_note,
                "rainfall_updated_at": state_meta.get("forecast_acquired_at"),
                "hydraulic_run_updated_at": model_timestamp,
                "road_network_updated_at": None,
                "observation_updated_at": None,
            },
        }

    if edges is None:
        return empty_response(
            "origin and destination are disconnected after excluding ineligible or confirmed impassable segments"
        )
    _validate_route_continuity(edges)
    recommended = _build_transport_candidate(edges, assessments, config)

    multiplier = {edge.road_id: ALTERNATIVE_DIVERGENCE_FACTOR for edge in recommended.edges}
    alt_edges = _transport_dijkstra(graph, o_idx, d_idx, assessments, config, multiplier)
    alternative: Optional[RouteCandidate] = None
    no_alt_reason: Optional[str] = None
    if alt_edges is None:
        no_alt_reason = "no connected alternative avoiding the recommended corridor"
    else:
        _validate_route_continuity(alt_edges)
        alt_candidate = _build_transport_candidate(alt_edges, assessments, config)
        rec_roads = {edge.road_id for edge in recommended.edges}
        alt_roads = {edge.road_id for edge in alt_candidate.edges}
        shared = len(rec_roads & alt_roads)
        denom = max(len(rec_roads | alt_roads), 1)
        if 1.0 - (shared / denom) >= (1.0 - MIN_DISTINCT_EDGE_FRACTION):
            alternative = alt_candidate
        else:
            no_alt_reason = "the best alternative is not sufficiently distinct from the recommended route"

    classification, class_reason = _transport_classification(recommended)
    counts = recommended.counts_summary()
    covered = sum(1 for edge in recommended.edges if not recommended.transport_by_edge[edge.edge_key].get("uncertain"))
    evidence_state, evidence_reason = route_evidence_state(
        total_edges=len(recommended.edges),
        covered_edges=covered,
        has_unknown_on_route=counts.get("unknown", 0) > 0 or counts.get("uncertain", 0) > 0,
        member_disagreement=False,
    )

    def route_payload(candidate: RouteCandidate) -> dict:
        c = candidate.counts_summary()
        depths = [
            record.get("depth_m")
            for record in candidate.transport_by_edge.values()
            if record.get("depth_m") is not None
        ]
        velocities = [
            record.get("velocity_m_s")
            for record in candidate.transport_by_edge.values()
            if record.get("velocity_m_s") is not None
        ]
        route_class, _ = _transport_classification(candidate)
        risk_features = []
        for edge in candidate.edges:
            decision = candidate.transport_by_edge[edge.edge_key]
            if decision["risk_state"] != "LOW_RISK" or decision.get("uncertain"):
                risk_features.append({
                    "type": "Feature",
                    "properties": {
                        "segment_id": edge.edge_key,
                        "risk_state": decision["risk_state"],
                        "name": edge.name,
                        "reason": decision["reason"],
                        "depth_m": decision.get("depth_m"),
                        "velocity_m_s": decision.get("velocity_m_s"),
                        "uncertain": decision.get("uncertain", False),
                    },
                    "geometry": {"type": "LineString", "coordinates": [list(edge.coords_4326[0]), list(edge.coords_4326[1])]},
                })
        return {
            "edges": [edge.edge_key for edge in candidate.edges],
            "geometry": _route_geojson(candidate.edges),
            "risk_segments": {"type": "FeatureCollection", "features": risk_features},
            "total_distance_km": round(candidate.total_distance_m / 1000.0, 2),
            # Current speeds are class assumptions, not transport-specific ETA
            # evidence.  Keep the existing legacy estimate out of this branch.
            "estimated_travel_time_min": None,
            "flood_risk_summary": {
                "blocked_segments": c.get("blocked", 0),
                "elevated_risk_segments": c.get("elevated_risk", 0),
                "unknown_segments": c.get("unknown", 0),
                "uncertain_segments": c.get("uncertain", 0),
                "hazardous_segments": c.get("hazardous", 0),
                "low_risk_segments": c.get("low_risk", 0),
            },
            "route_state": route_class,
            "safety_classification": route_class,
            "max_depth_m": round(max(depths), 3) if depths else None,
            "max_velocity_m_s": round(max(velocities), 3) if velocities else None,
            "risk_penalty_s": round(candidate.risk_penalty_s, 1),
        }

    route_run_id = hashlib.sha256(
        repr((normalized, config.as_metadata(), state_meta, round(origin_lon, 6), round(origin_lat, 6),
             round(dest_lon, 6), round(dest_lat, 6), [edge.edge_key for edge in recommended.edges])).encode()
    ).hexdigest()[:16]
    rec_payload = route_payload(recommended)
    alt_payload = route_payload(alternative) if alternative else None
    return {
        "status": status,
        "mode": mode_states.mode if mode_states else state_meta.get("mode", "UNKNOWN"),
        "event_id": mode_states.event_id if mode_states else state_meta.get("event_id"),
        "timestep_index": mode_states.timestep_index if mode_states else state_meta.get("timestep_index"),
        "transport_mode": normalized,
        "route_run_id": route_run_id,
        "generated_at": now_utc().isoformat(),
        "origin": {"coords": [origin_lon, origin_lat], "snapped_road": o_name, "snap_distance_m": round(o_dist, 1)},
        "destination": {"coords": [dest_lon, dest_lat], "snapped_road": d_name, "snap_distance_m": round(d_dist, 1)},
        "recommended_route": rec_payload,
        "alternative_route": alt_payload,
        "no_alternative_reason": no_alt_reason,
        "route_comparison": {
            "recommended": {**{k: v for k, v in rec_payload["flood_risk_summary"].items()}, "distance_km": rec_payload["total_distance_km"], "travel_time_min": None, "risk_penalty_s": rec_payload["risk_penalty_s"]},
            "alternative": ({**{k: v for k, v in alt_payload["flood_risk_summary"].items()}, "distance_km": alt_payload["total_distance_km"], "travel_time_min": None, "risk_penalty_s": alt_payload["risk_penalty_s"]} if alt_payload else None),
        },
        "evidence_state": {"route_state": evidence_state, "reason": evidence_reason, "derivation": "transport access and hazard evidence use a weakest-link rule; UNKNOWN is never averaged into safety"},
        "safety_classification": classification,
        "explanation": _transport_explanation(normalized, recommended, classification, class_reason, alternative, no_alt_reason),
        "claim_policy": (
            f"{normalized.title()} route with the lowest supported modeled flood exposure under the active state. "
            "LOWER_RISK is not a guarantee; missing pedestrian facilities, verified closures, and velocity remain explicit limitations."
        ),
        "model_state": state_meta,
        "provenance": {
            "transport_mode": normalized,
            "transport_risk_configuration": config.as_metadata(),
            "road_network_source": road_network_provenance().get("source"),
            "risk_mapping_method": "mode-specific access plus depth/velocity/uncertainty edge assessment",
            "velocity_data": "UNKNOWN when not supplied; never assumed zero",
            "model_state": state_meta,
            "data_freshness": mode_freshness_note,
        },
        "data_freshness": {
            "note": mode_freshness_note,
            "rainfall_updated_at": state_meta.get("forecast_acquired_at"),
            "hydraulic_run_updated_at": model_timestamp,
            "road_network_updated_at": None,
            "observation_updated_at": None,
        },
    }


def compute_safe_route(
    origin_lon: float,
    origin_lat: float,
    dest_lon: float,
    dest_lat: float,
    mode_states: Optional[ModeReachStates] = None,
    model_timestamp: Optional[str] = None,
    forcing_source: Optional[str] = None,
    mode_freshness_note: str = "",
    transport_mode: Optional[str] = None,
    edge_hazards: Optional[Mapping[str, Any]] = None,
    state_provenance: Optional[Mapping[str, Any]] = None,
    transport_config_overrides: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Compute the full flood-aware routing result for ONE mode.

    The same function serves LIVE and HISTORICAL modes; the mode only
    determines the reach-state source (never the engine, never the cost
    model). Raises ValueError on invalid/unroutable inputs with explicit
    messages.
    """
    graph = get_road_graph()
    if transport_mode is not None:
        return _compute_transport_route(
            graph=graph,
            origin_lon=origin_lon,
            origin_lat=origin_lat,
            dest_lon=dest_lon,
            dest_lat=dest_lat,
            transport_mode=transport_mode,
            mode_states=mode_states,
            model_timestamp=model_timestamp,
            forcing_source=forcing_source,
            mode_freshness_note=mode_freshness_note,
            edge_hazards=edge_hazards,
            state_provenance=state_provenance,
            config_overrides=transport_config_overrides,
        )
    if mode_states is None:
        raise ValueError("mode_states is required for legacy flood-safe routing")
    o_idx, o_dist, o_name = graph.snap(origin_lon, origin_lat)
    d_idx, d_dist, d_name = graph.snap(dest_lon, dest_lat)

    if mode_states.status not in ("COMPUTED", "EXECUTED") or not mode_states.member_states:
        risk_by_edge = {
            k: ("UNKNOWN", "model state unavailable for this mode/timestep: " +
                "; ".join(mode_states.diagnostics[:1]))
            for k in build_edge_reach_map()
        }
        status = "UNKNOWN_RISK_STATE"
    else:
        risk_by_edge = _edge_risk_states(mode_states)
        status = "COMPUTED"

    edges = _dijkstra(graph, o_idx, d_idx, risk_by_edge)
    if edges is None:
        raise ValueError("NO_ROUTE: origin and destination are not connected")

    recommended = _build_candidate(edges, risk_by_edge)

    # Alternative: re-run with the recommended roads penalized.
    multiplier = {e.road_id: ALTERNATIVE_DIVERGENCE_FACTOR for e in recommended.edges}
    alt_edges = _dijkstra(graph, o_idx, d_idx, risk_by_edge, multiplier)
    alternative: Optional[RouteCandidate] = None
    no_alt_reason: Optional[str] = None
    if alt_edges is None:
        no_alt_reason = "no connected alternative avoiding the recommended corridor"
    else:
        alt_candidate = _build_candidate(alt_edges, risk_by_edge)
        rec_roads = {e.road_id for e in recommended.edges}
        alt_roads = {e.road_id for e in alt_candidate.edges}
        shared = len(rec_roads & alt_roads)
        denom = max(len(rec_roads | alt_roads), 1)
        if 1.0 - (shared / denom) < (1.0 - MIN_DISTINCT_EDGE_FRACTION):
            no_alt_reason = (
                "the best alternative is not sufficiently distinct from the "
                "recommended route"
            )
            alternative = None
        else:
            alternative = alt_candidate

    # Evidence state (documented weakest-link derivation).
    covered = sum(
        1 for e in recommended.edges
        if recommended.risk_by_edge[e.edge_key][0] != "UNKNOWN"
    )

    def _member_positive(states: Dict[str, Dict[str, Optional[float]]]) -> bool:
        return any(
            ((states.get(rid) or {}).get("incoming_flow_m3_s") or 0.0) > 0.0
            for rid in ("UG-01", "OC-01", "CD-01", "OC-02")
        )

    computed_members = [m for m in mode_states.member_states if m]
    positive_members = sum(1 for m in computed_members if _member_positive(m))
    member_disagreement = (
        len(computed_members) > 0 and 0 < positive_members < len(computed_members)
    )
    evidence_state, evidence_reason = route_evidence_state(
        total_edges=len(recommended.edges),
        covered_edges=covered,
        has_unknown_on_route=any(
            recommended.risk_by_edge[e.edge_key][0] == "UNKNOWN"
            for e in recommended.edges
        ),
        member_disagreement=member_disagreement,
    )

    explanation = _explain(
        recommended, alternative, evidence_state, evidence_reason, no_alt_reason
    )

    now = now_utc()
    network_prov = road_network_provenance()
    route_run_id = hashlib.sha256(
        "|".join(
            str(x)
            for x in (
                mode_states.mode, mode_states.event_id, mode_states.timestep_index,
                round(origin_lon, 6), round(origin_lat, 6),
                round(dest_lon, 6), round(dest_lat, 6),
                o_idx, d_idx, status,
                [e.edge_key for e in recommended.edges],
                recommended.total_cost_s,
            )
        ).encode("utf-8")
    ).hexdigest()[:16]

    def route_payload(c: RouteCandidate) -> dict:
        cs = c.counts_summary()
        route_unknown = cs["unknown"]
        return {
            "edges": [e.edge_key for e in c.edges],
            "geometry": _route_geojson(c.edges),
            "risk_segments": _route_risk_geojson(c.edges, c.risk_by_edge),
            "total_distance_km": round(c.total_distance_m / 1000.0, 2),
            "estimated_travel_time_min": round(c.travel_time_s / 60.0, 1),
            "flood_risk_summary": {
                "blocked_segments": cs["blocked"],
                "elevated_risk_segments": cs["elevated_risk"],
                "unknown_segments": route_unknown,
                "low_risk_segments": cs["low_risk"],
            },
            "route_state": (
                "CONDITIONAL_UNKNOWN_RISK_PORTION" if route_unknown > 0
                else "LOWER_MODELED_RISK"
            ),
            "risk_penalty_s": round(c.risk_penalty_s, 1),
        }

    result: dict = {
        "status": status,
        "mode": mode_states.mode,
        "event_id": mode_states.event_id,
        "timestep_index": mode_states.timestep_index,
        "route_run_id": route_run_id,
        "generated_at": now.isoformat(),
        "origin": {
            "coords": [origin_lon, origin_lat],
            "snapped_road": o_name,
            "snap_distance_m": round(o_dist, 1),
        },
        "destination": {
            "coords": [dest_lon, dest_lat],
            "snapped_road": d_name,
            "snap_distance_m": round(d_dist, 1),
        },
        "recommended_route": route_payload(recommended),
        "alternative_route": route_payload(alternative) if alternative else None,
        "no_alternative_reason": no_alt_reason,
        "route_comparison": {
            "recommended": {
                **recommended.counts_summary(),
                "distance_km": round(recommended.total_distance_m / 1000.0, 2),
                "travel_time_min": round(recommended.travel_time_s / 60.0, 1),
                "risk_penalty_s": round(recommended.risk_penalty_s, 1),
            },
            "alternative": (
                {
                    **alternative.counts_summary(),
                    "distance_km": round(alternative.total_distance_m / 1000.0, 2),
                    "travel_time_min": round(alternative.travel_time_s / 60.0, 1),
                    "risk_penalty_s": round(alternative.risk_penalty_s, 1),
                }
                if alternative else None
            ),
        },
        "evidence_state": {
            "route_state": evidence_state,
            "reason": evidence_reason,
            "derivation": (
                "weakest link: any UNKNOWN segment on the route caps the "
                "route at LOW; MEDIUM requires full coverage with member "
                "disagreement; HIGH requires full coverage with full "
                "ensemble agreement"
            ),
        },
        "explanation": explanation,
        "claim_policy": (
            "Recommended route with lowest supported MODELED flood exposure "
            "under the available evidence — NOT a guarantee of safety. "
            "UNKNOWN segments are not safe segments; stage and depth are "
            "UNKNOWN throughout."
            + (
                " Historical routes are MODEL RECONSTRUCTIONS of a replayed "
                "event, not observations of the real event."
                if mode_states.mode == "HISTORICAL" else ""
            )
        ),
        "provenance": {
            "mode": mode_states.mode,
            "event_id": mode_states.event_id,
            "timestep_index": mode_states.timestep_index,
            "model_timestamp": model_timestamp,
            "forcing_source": forcing_source,
            "road_network_source": network_prov.get("source"),
            "road_network_acquired_at": network_prov.get("acquired_at_utc"),
            "risk_mapping_method": (
                "distance-to-reach-centerline (documented threshold), "
                "loading-based translation (stage-less)"
            ),
            "risk_mapping_threshold_m": CORRIDOR_MAP_DISTANCE_M,
            "cost_model": {
                "type": "travel_time + risk_penalty (documented ASSUMED weights)",
                "weights_s_per_m": {
                    k: (None if v == float("inf") else v)
                    for k, v in RISK_PENALTY_S_PER_M.items()
                },
            },
            "ensemble_members": len(mode_states.member_states),
            "data_freshness": mode_freshness_note,
        },
    }
    return result
