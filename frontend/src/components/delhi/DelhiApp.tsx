import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import DelhiMap, { type MapFlowState, type RoutePickPoint } from './DelhiMap';
import { type MapTheme } from '../../config/mapStyles';
import ReplayControls from './ReplayControls';
import EvidencePanel from './EvidencePanel';
import RoutePanel from './RoutePanel';
import ModelPanel from './ModelPanel';
import ScenarioPanel from './ScenarioPanel';
import LivePanel from './LivePanel';
import {
  delhiApi,
  fetchClientNwpRainfall,
  type DrainageGraphResponse,
  type LiveStateResponse,
  type SafeRouteResponse,
  type ScenarioSafeRouteResponse,
  type StreetsIntel,
  type SurfaceHotspot,
  type TransportMode,
  type WhatIfResponse,
} from '../../api/delhi';

type Mode = 'LIVE' | 'SCENARIO' | 'REPLAY';
type Horizon = 'NOW' | '+1h' | '+2h' | '+3h';

// A computed route is either the LIVE/HISTORICAL route or a WHAT-IF scenario
// route. Both come from the same deterministic routing core (same Dijkstra,
// same risk penalties); they differ only in which hazard state the segments
// were evaluated against, so each arm carries only its own mode's evidence.
type DelhiRouteResult = SafeRouteResponse | ScenarioSafeRouteResponse;

// What a route must be computed against: the live nowcast horizon, the
// replayed event timestep, or the scenario intensity currently displayed.
type RouteContext =
  | { mode: 'live'; stateId: string; rainfallMm?: number[] }
  | {
      mode: 'historical';
      replay: { eventId: string; timestepIndex: number; stateId: string; complete24h: boolean };
    }
  | { mode: 'scenario'; rainfallMmH: number; stateId: string };

const HORIZONS: Horizon[] = ['NOW', '+1h', '+2h', '+3h'];
const SCENARIO_PRESETS = [20, 40, 50, 70];

// V1 hero titles (FloodMap.tsx periodDisplays).
const HORIZON_TITLES: Record<Horizon, string> = {
  NOW: 'Current Hour Baseline',
  '+1h': '+1 Hour Outlook (T+1h)',
  '+2h': '+2 Hours Outlook (T+2h)',
  '+3h': '+3 Hours Outlook (T+3h)',
};

const EMPTY_FLOW: MapFlowState = {
  reachStates: {},
  timestamp: null,
  rainIntensityMmH: null,
  rainKnown: false,
};

// Delhi V1 depth classification (backend/app/domain/delhi/scenarios/config.py
// DEPTH_THRESHOLDS_CM + depth_v1.py V1_DEPTH_COLORS). These are DEMO/UI
// classification thresholds for the modelled depth proxy — not hazard
// standards, not observed thresholds, not a safety claim. The legend, the map
// swatches and the hero badge must all read from this one table.
const DEPTH_LEGEND = [
  { color: '#ffeda0', range: '2 – 10 cm', desc: 'SHALLOW' },
  { color: '#feb24c', range: '10 – 30 cm', desc: 'MODERATE' },
  { color: '#f03b20', range: '30 – 60 cm', desc: 'DEEP' },
  { color: '#bd0026', range: '60 cm +', desc: 'SEVERE' },
];

function riskBanner(maxDepthM: number | null): {
  text: string; bg: string; border: string; color: string; desc: string; legendColor: string;
} {
  if (maxDepthM === null) {
    return { text: 'UNKNOWN', bg: '#f8fafc', border: '#cbd5e1', color: '#475569', desc: 'No defensible model state for this step', legendColor: '#94a3b8' };
  }
  const cm = maxDepthM * 100;
  if (cm >= 60) return { text: 'SEVERE', bg: '#fee2e2', border: '#f87171', color: '#991b1b', desc: '60 cm + band • deepest modelled ponding on the 30 m grid', legendColor: '#bd0026' };
  if (cm >= 30) return { text: 'DEEP', bg: '#fef2f2', border: '#f87171', color: '#b91c1c', desc: '30 – 60 cm band • deep ponding in the lowest modelled cells', legendColor: '#f03b20' };
  if (cm >= 10) return { text: 'MODERATE', bg: '#fff7ed', border: '#fb923c', color: '#c2410c', desc: '10 – 30 cm band • widespread surface water in low-lying cells', legendColor: '#feb24c' };
  if (cm >= 2) return { text: 'SHALLOW', bg: '#fefce8', border: '#facc15', color: '#854d0e', desc: '2 – 10 cm band • shallow ponding in terrain depressions', legendColor: '#ffeda0' };
  return { text: 'NO INUNDATION', bg: '#f8fafc', border: '#cbd5e1', color: '#166534', desc: 'Below the 2 cm band threshold • no meaningful surface ponding', legendColor: '#94a3b8' };
}

const DelhiApp = ({ theme }: { theme?: MapTheme } = {}) => {
  const [mode, setMode] = useState<Mode>('LIVE');
  const [horizon, setHorizon] = useState<Horizon>('+1h');
  const [scenarioMm, setScenarioMm] = useState<number>(40);

  // LIVE: per-horizon states from /api/delhi/live-state (V1 parity).
  const [liveState, setLiveState] = useState<LiveStateResponse | null>(null);
  const [liveLoading, setLiveLoading] = useState(true);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [scenarioState, setScenarioState] = useState<WhatIfResponse | null>(null);
  const [scenarioLoading, setScenarioLoading] = useState(false);

  // Shared map state pushed up by the active panel (model data only).
  const [mapFlow, setMapFlow] = useState<MapFlowState>(EMPTY_FLOW);
  const mapFlowRef = useRef<MapFlowState>(EMPTY_FLOW);
  mapFlowRef.current = mapFlow;

  // Safe-routing shared state (map <-> panel).
  const [routePick, setRoutePick] = useState<'origin' | 'destination' | null>(null);
  const [routePicks, setRoutePicks] = useState<{
    origin: RoutePickPoint | null;
    destination: RoutePickPoint | null;
  }>({ origin: null, destination: null });
  const [routeResult, setRouteResult] = useState<DelhiRouteResult | null>(null);
  const [routeHudOpen, setRouteHudOpen] = useState(false);
  const [routeLoading, setRouteLoading] = useState(false);
  const [routeError, setRouteError] = useState<string | null>(null);
  const [transportMode, setTransportMode] = useState<TransportMode | null>(null);
  // Current replay selection (event + timestep), reported by ReplayControls.
  // Historical safe routes are computed against exactly this state.
  const [replayContext, setReplayContext] = useState<{
    eventId: string;
    timestepIndex: number;
    stateId: string | null;
    complete24h: boolean;
  }>({
    eventId: 'EVT-2024-06-27',
    timestepIndex: 0,
    stateId: null,
    complete24h: false,
  });
  const [drainageGraph, setDrainageGraph] = useState<DrainageGraphResponse | null>(null);
  const [surfaceHotspots, setSurfaceHotspots] = useState<SurfaceHotspot[]>([]);
  const [depthCells, setDepthCells] = useState<
    { lon: number; lat: number; depth_cm: number; flood_state: string; provenance: string }[]
  >([]);
  const [depthPolygons, setDepthPolygons] = useState<{ type: string; features: unknown[] } | null>(null);
  // REPLAY: street/intersection intelligence of the current replay timestep,
  // including each corridor's change vs the previous timestep (V1 replay roads
  // plus an increasing/decreasing indicator).
  const [replayStreets, setReplayStreets] = useState<StreetsIntel | null>(null);
  const [scenarioRoadDepths, setScenarioRoadDepths] = useState<
    Record<
      string,
      { depth_cm: number | null; depth_m: number | null; water_depth_state: string }
    > | null
  >(null);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [mobileDrawerExpanded, setMobileDrawerExpanded] = useState<boolean>(true);

  useEffect(() => {
    delhiApi.getDrainageGraph().then(setDrainageGraph).catch(() => setDrainageGraph(null));
  }, []);

  // LIVE forecast states: fetched once, cached; independent per-horizon
  // switching is instant (no refetch on horizon change).
  // Overlapping loads are deduplicated per refresh-mode: the backend builds
  // its heavy static structures on the first cold request, and two
  // simultaneous cold loads (StrictMode double mount, a click while the
  // first response is still in flight) used to double that peak and take the
  // free-tier instance out of memory. Same request pattern, one flight.
  const liveFlights = useRef<Map<string, Promise<void>>>(new Map());
  const loadLive = useCallback((refresh = false) => {
    const flightKey = String(refresh);
    const running = liveFlights.current.get(flightKey);
    if (running) return running;
    setLiveLoading(true);
    setLiveError(null);
    const flight = delhiApi
      .getLiveState(refresh)
      .then(async (res) => {
        if (res.rainfall_status === 'SYNTHETIC_FALLBACK') {
          try {
            const clientMm = await fetchClientNwpRainfall();
            if (clientMm && clientMm.length === 4) {
              const liveRes = await delhiApi.getLiveState(refresh, 'ALL', clientMm);
              setLiveState(liveRes);
              return;
            }
          } catch {
            // Gracefully keep server fallback response
          }
        }
        setLiveState(res);
      })
      .catch((err) => setLiveError(err instanceof Error ? err.message : 'live state unavailable'))
      .finally(() => {
        liveFlights.current.delete(flightKey);
        setLiveLoading(false);
      });
    liveFlights.current.set(flightKey, flight);
    return flight;
  }, []);

  useEffect(() => {
    loadLive(false);
  }, [loadLive]);

  // MODEL SCENARIO: real backend computation per intensity (cached server-side).
  useEffect(() => {
    if (mode !== 'SCENARIO') return;
    setScenarioState(null);
    setScenarioLoading(true);
    delhiApi
      .getWhatIf(scenarioMm)
      .then(setScenarioState)
      .catch(() => setScenarioState(null))
      .finally(() => setScenarioLoading(false));
  }, [mode, scenarioMm]);

  const activeHorizonState = useMemo(
    () => liveState?.horizons.find((h) => h.horizon === horizon) ?? null,
    [liveState, horizon],
  );

  // Reset map overlays when switching workflows. Route picks are KEPT
  // across modes (V1 behaviour): switching LIVE ↔ REPLAY recomputes the
  // same origin/destination against the new mode's state instead of
  // dumping the user back into the live forecast.
  const switchMode = (next: Mode) => {
    routeRequestSeq.current += 1;
    routeInflightKey.current = null;
    setMode(next);
    setMapFlow(EMPTY_FLOW);
    setRoutePick(null);
    setDepthCells([]);
    setDepthPolygons(null);
    setSurfaceHotspots([]);
    setScenarioRoadDepths(null);
    setReplayStreets(null);
    setRouteResult(null);
    setRouteError(null);
    setRouteLoading(false);
    setReplayContext((prev) => ({ ...prev, stateId: null, complete24h: false }));
    const hasPicks = routePicks.origin !== null && routePicks.destination !== null;
    if (hasPicks) {
      setRouteHudOpen(true);
    }
  };

  // One monotonic request id: the replay clock and the scenario intensity can
  // move faster than the backend answers, and a late response must never
  // replace the route for the state currently displayed. The direct call from
  // a pick and the context effect can also ask for the SAME state back to
  // back — the backend builds heavy structures per request, so an identical
  // request already in flight is never duplicated.
  const routeRequestSeq = useRef(0);
  const routeInflightKey = useRef<string | null>(null);

  const fetchSafeRoute = useCallback(
    async (
      origin: [number, number],
      destination: [number, number],
      context?: RouteContext,
    ) => {
      const contextStateId = context?.mode === 'historical' ? context.replay.stateId : context?.stateId;
      if (!transportMode || !context || !contextStateId) return;
      const key =
        context?.mode === 'scenario'
          ? `scenario:${context.rainfallMmH}:${context.stateId}:${origin.join(',')}:${destination.join(',')}`
          : context?.mode === 'historical'
            ? `historical:${context.replay.eventId}:${context.replay.timestepIndex}:${context.replay.stateId}:${context.replay.complete24h}:${origin.join(',')}:${destination.join(',')}`
            : `live:${horizon}:${context.stateId}:${origin.join(',')}:${destination.join(',')}`;
      if (key === routeInflightKey.current) return;
      const seq = ++routeRequestSeq.current;
      routeInflightKey.current = key;
      setRouteLoading(true);
      setRouteError(null);
      try {
        const res: DelhiRouteResult =
          context?.mode === 'scenario'
            ? await delhiApi.getScenarioSafeRoute({
                origin,
                destination,
                rainfall_mm_h: context.rainfallMmH,
                transport_mode: transportMode,
                model_state_id: context.stateId,
              })
            : context?.mode === 'historical'
              ? await delhiApi.getSafeRoute({
                  origin,
                  destination,
                  mode: 'historical',
                  event_id: context.replay.eventId,
                  timestep_index: context.replay.timestepIndex,
                  complete_24h: context.replay.complete24h,
                  transport_mode: transportMode,
                  model_state_id: context.replay.stateId,
                })
              : await delhiApi.getSafeRoute({
                  origin,
                  destination,
                  mode: 'live',
                  departure_hour: Math.max(0, HORIZONS.indexOf(horizon)),
                  transport_mode: transportMode,
                  model_state_id: context.stateId,
                  client_rainfall_mm: context.rainfallMm,
                });
        if (seq !== routeRequestSeq.current) return;
        setRouteResult(res);
      } catch (err) {
        if (seq !== routeRequestSeq.current) return;
        const msg = err instanceof Error ? err.message : 'Route calculation failed';
        setRouteError(msg);
        setRouteResult(null);
      } finally {
        if (seq === routeRequestSeq.current) {
          setRouteLoading(false);
          routeInflightKey.current = null;
        }
      }
    },
    [horizon, transportMode],
  );

  const routeContext = useCallback(
    (): RouteContext | undefined =>
      mode === 'SCENARIO'
        ? scenarioState?.routing_state_id
          ? { mode: 'scenario', rainfallMmH: scenarioMm, stateId: scenarioState.routing_state_id }
          : undefined
        : mode === 'REPLAY'
          ? replayContext.stateId
            ? {
                mode: 'historical',
                replay: {
                  eventId: replayContext.eventId,
                  timestepIndex: replayContext.timestepIndex,
                  stateId: replayContext.stateId,
                  complete24h: replayContext.complete24h,
                },
              }
            : undefined
          : activeHorizonState?.routing_state_id
            ? {
                mode: 'live',
                stateId: activeHorizonState.routing_state_id,
                rainfallMm:
                  liveState?.horizons.every((state) => typeof state.rainfall_mm === 'number')
                    ? liveState.horizons.map((state) => state.rainfall_mm as number)
                    : undefined,
              }
            : undefined,
    [mode, replayContext, scenarioMm, scenarioState, activeHorizonState, liveState],
  );

  // ReplayControls reports its (event, timestep); identity-stable so the
  // panel's reporting effect never re-runs because of us.
  const handleReplayContext = useCallback(
    (ctx: {
      eventId: string;
      timestepIndex: number;
      stateId?: string | null;
      complete24h?: boolean;
    }) => {
      const complete24h = ctx.complete24h ?? false;
      setReplayContext((prev) =>
        prev.eventId === ctx.eventId &&
        prev.timestepIndex === ctx.timestepIndex &&
        prev.stateId === (ctx.stateId ?? null) &&
        prev.complete24h === complete24h
          ? prev
          : {
              eventId: ctx.eventId,
              timestepIndex: ctx.timestepIndex,
              stateId: ctx.stateId ?? null,
              complete24h,
            },
      );
    },
    [],
  );

  // V1-style: moving the replay clock recomputes an active historical route
  // against the new timestep. Guarded by the route's own event/timestep so
  // a completed fetch can never re-trigger itself.
  useEffect(() => {
    if (mode !== 'REPLAY' || !transportMode || !replayContext.stateId) return;
    if (!routePicks.origin || !routePicks.destination) return;
    if (
      routeResult &&
      routeResult.mode === 'HISTORICAL' &&
      routeResult.event_id === replayContext.eventId &&
      routeResult.timestep_index === replayContext.timestepIndex &&
      routeResult.transport_mode === transportMode &&
      routeResult.model_state?.state_id === replayContext.stateId
    )
      return;
    if (routeResult && (routeResult.mode !== 'HISTORICAL' || routeResult.transport_mode !== transportMode)) return;
    setRouteResult(null);
      void fetchSafeRoute(routePicks.origin.coords, routePicks.destination.coords, {
        mode: 'historical',
        replay: {
          eventId: replayContext.eventId,
          timestepIndex: replayContext.timestepIndex,
          stateId: replayContext.stateId,
          complete24h: replayContext.complete24h,
        },
      });
  }, [mode, replayContext, routeResult, routePicks, fetchSafeRoute, transportMode]);

  // Same V1 pattern for the scenario clock: changing the WHAT-IF intensity
  // re-evaluates an active route against the new modelled street depths, so
  // the drawn route always belongs to the intensity shown in the header.
  // Guarded by the route's own intensity, so a completed fetch never
  // re-triggers itself.
  useEffect(() => {
    if (mode !== 'SCENARIO' || !transportMode || !scenarioState?.routing_state_id) return;
    if (!routePicks.origin || !routePicks.destination) return;
    if (
      routeResult &&
      routeResult.mode === 'SCENARIO' &&
      routeResult.transport_mode === transportMode &&
      routeResult.scenario.rainfall_mm_h === scenarioMm &&
      routeResult.model_state?.state_id === scenarioState.routing_state_id
    )
      return;
    setRouteResult(null);
    void fetchSafeRoute(routePicks.origin.coords, routePicks.destination.coords, {
      mode: 'scenario',
      rainfallMmH: scenarioMm,
      stateId: scenarioState.routing_state_id,
    });
  }, [mode, scenarioMm, scenarioState, routeResult, routePicks, fetchSafeRoute, transportMode]);

  // A refreshed live forecast can retain the same horizon label while its
  // underlying model-state identity changes.  The route must follow the
  // active frame rather than remain attached to the previous forecast.
  useEffect(() => {
    if (mode !== 'LIVE' || !transportMode || !activeHorizonState?.routing_state_id) return;
    if (!routePicks.origin || !routePicks.destination) return;
    if (
      routeResult &&
      routeResult.mode === 'LIVE' &&
      routeResult.transport_mode === transportMode &&
      routeResult.model_state?.state_id === activeHorizonState.routing_state_id
    )
      return;
    setRouteResult(null);
    void fetchSafeRoute(routePicks.origin.coords, routePicks.destination.coords, {
      mode: 'live',
      stateId: activeHorizonState.routing_state_id,
      rainfallMm:
        liveState?.horizons.every((state) => typeof state.rainfall_mm === 'number')
          ? liveState.horizons.map((state) => state.rainfall_mm as number)
          : undefined,
    });
  }, [mode, activeHorizonState, liveState, routeResult, routePicks, fetchSafeRoute, transportMode]);

  // Selecting a transport mode is the final gate before any route request.
  // If points already exist, the new mode is evaluated against the exact
  // active frame; otherwise the selector simply arms the next map pick.
  useEffect(() => {
    if (!routeHudOpen || !transportMode || !routePicks.origin || !routePicks.destination) return;
    const context = routeContext();
    if (!context) return;
    if (
      routeResult?.transport_mode === transportMode &&
      routeResult.model_state?.state_id === (context.mode === 'historical' ? context.replay.stateId : context.stateId)
    )
      return;
    setRouteResult(null);
    void fetchSafeRoute(routePicks.origin.coords, routePicks.destination.coords, context);
  }, [routeHudOpen, transportMode, routePicks, routeContext, routeResult, fetchSafeRoute]);

  const handleExitRouting = useCallback(() => {
    routeRequestSeq.current += 1;
    routeInflightKey.current = null;
    setRouteHudOpen(false);
    setRoutePick(null);
    setRouteResult(null);
    setRouteError(null);
    setRouteLoading(false);
    setRoutePicks({ origin: null, destination: null });
    setTransportMode(null);
  }, []);

  const handleMapClick = (lonlat: [number, number]) => {
    if (!routePick) return;
    if (routePick === 'origin') {
      const newOrigin = { coords: lonlat };
      setRoutePicks((prev) => ({ ...prev, origin: newOrigin }));
      if (routePicks.destination) {
        setRoutePick(null);
        const context = routeContext();
        if (context && transportMode) void fetchSafeRoute(lonlat, routePicks.destination.coords, context);
      } else {
        setRoutePick('destination');
      }
    } else if (routePick === 'destination') {
      const newDest = { coords: lonlat };
      setRoutePicks((prev) => ({ ...prev, destination: newDest }));
      setRoutePick(null);
      if (routePicks.origin) {
        const context = routeContext();
        if (context && transportMode) void fetchSafeRoute(routePicks.origin.coords, lonlat, context);
      } else {
        setRoutePick('origin');
      }
    }
  };

  // The active model state driving the hero card + map overlays.
  const activeState = mode === 'LIVE' ? activeHorizonState : mode === 'SCENARIO' ? scenarioState : null;
  const maxDepthM = activeState?.max_depth_m ?? null;
  const floodedAreaM2 = activeState?.flooded_area_m2 ?? null;
  const streets = activeState?.streets ?? null;
  const banner = riskBanner(maxDepthM);

  const rainfallMetricLabel =
    mode === 'LIVE' ? 'FORECAST RAINFALL' : mode === 'SCENARIO' ? 'SCENARIO INTENSITY' : 'HISTORICAL RAIN RATE';
  const rainfallMetricVal =
    mode === 'LIVE'
      ? activeHorizonState?.rainfall_mm !== null && activeHorizonState?.rainfall_mm !== undefined
        ? activeHorizonState.rainfall_mm.toFixed(1)
        : '--'
      : mode === 'SCENARIO'
        ? scenarioMm.toFixed(1)
        : '--';
  const rainfallMetricSub =
    mode === 'LIVE'
      ? activeHorizonState?.rainfall_provenance === 'SYNTHETIC_FALLBACK'
        ? 'SYNTHETIC FALLBACK (demo series)'
        : 'Open-Meteo NWP hourly (model value)'
      : mode === 'SCENARIO'
        ? 'Hypothetical model input (What-If)'
        : 'Documented event forcing';

  const syntheticFallback = liveState?.rainfall_status === 'SYNTHETIC_FALLBACK';

  return (
    <div className="flood-app">
      {/* 1. TOP BAR: V1 identity hierarchy */}
      <header className="app-topbar">
        <div className="topbar-left">
          <div className="system-identity">
            <span className="system-mark">øy</span>
            <span className="system-title">URBAN FLOOD NOWCAST</span>
          </div>
          <span className="topbar-sep">|</span>
          <div className="system-location">
            <span className="loc-label text-base font-bold">DELHI</span>
          </div>
          <span className="topbar-sep">|</span>
          <div className="system-subtitle text-sm font-normal">
            <span>
              {mode === 'REPLAY'
                ? 'RETROSPECTIVE EVENT REPLAY'
                : mode === 'SCENARIO'
                  ? 'WHAT-IF FLOOD SCENARIO'
                  : '0–3 HOUR FLOOD OUTLOOK'}
            </span>
          </div>
          <div
            className="prototype-status-pill"
            style={
              mode === 'REPLAY'
                ? { background: '#78350f', borderColor: '#d97706' }
                : mode === 'SCENARIO'
                  ? { background: '#0284c7', borderColor: '#38bdf8' }
                  : syntheticFallback
                    ? { background: '#7c2d12', borderColor: '#ea580c' }
                    : undefined
            }
          >
            <span
              className="status-dot"
              style={
                mode === 'REPLAY'
                  ? { background: '#fef3c7' }
                  : mode === 'SCENARIO'
                    ? { background: '#e0f2fe' }
                    : undefined
              }
            />
            <span>
              {mode === 'REPLAY'
                ? 'HISTORICAL REPLAY'
                : mode === 'SCENARIO'
                  ? 'MODEL SCENARIO'
                  : syntheticFallback
                    ? 'LIVE · SYNTHETIC FALLBACK'
                    : 'PROTOTYPE SCENARIO'}
            </span>
          </div>
        </div>
        <div className="topbar-right">
          <button
            className={`evidence-toggle ${evidenceOpen ? 'active' : ''}`}
            onClick={() => setEvidenceOpen((v) => !v)}
          >
            EVIDENCE
          </button>
          <div className="telemetry-info">
            <span className="meta-tag text-xs font-normal tracking-wide">60-MIN TIMESTEP</span>
            <span className="meta-divider">•</span>
            {mode === 'REPLAY' ? (
              <>
                <span
                  className="meta-tag text-xs font-normal tracking-wide"
                  style={{ background: '#78350f', borderColor: '#d97706', color: '#fef3c7' }}
                >
                  24-HR REPLAY
                </span>
                <span className="meta-divider">•</span>
                <span className="meta-label text-xs font-normal">DEPTH:</span>
                <span className="meta-val text-sm font-medium">MODELLED</span>
              </>
            ) : mode === 'SCENARIO' ? (
              <>
                <span className="meta-label text-xs font-normal">INPUT:</span>
                <span className="meta-val text-sm font-medium" style={{ color: '#38bdf8' }}>
                  Hypothetical rainfall input ({scenarioMm} mm/h)
                </span>
                <span className="meta-divider">•</span>
                <span className="meta-label text-xs font-normal">MODE:</span>
                <span className="meta-val text-sm font-medium" style={{ color: '#e2e8f0' }}>
                  MODEL SCENARIO
                </span>
              </>
            ) : (
              <>
                <span className="meta-label text-xs font-normal">SOURCE:</span>
                <span
                  className="meta-val text-sm font-medium"
                  style={syntheticFallback ? { color: '#fb923c' } : undefined}
                >
                  Open-Meteo NWP ({syntheticFallback ? 'SYNTHETIC_FALLBACK' : (liveState?.rainfall_status ?? '…')})
                </span>
                <span className="meta-divider">•</span>
                <span className="meta-label text-xs font-normal">ACQUIRED:</span>
                <span className="meta-val text-sm font-medium">
                  {liveState?.rainfall_acquired_at
                    ? new Date(liveState.rainfall_acquired_at).toLocaleTimeString('en-IN', {
                        hour: '2-digit',
                        minute: '2-digit',
                        timeZone: 'Asia/Kolkata',
                      }) + ' IST'
                    : 'Connecting...'}
                </span>
              </>
            )}
          </div>
        </div>
      </header>

      {/* 2. MAP WORKSPACE */}
      <div className="map-workspace">
        <DelhiMap
          flowState={mapFlow}
          routeResult={routeResult}
          routePicks={routePicks}
          pickTarget={routePick}
          onMapClick={handleMapClick}
          drainageGraph={drainageGraph}
          surfaceHotspots={surfaceHotspots}
          depthCells={mode !== 'LIVE' ? depthCells : []}
          depthPolygons={
            mode === 'LIVE'
              ? activeHorizonState?.depth_polygons ?? null
              : mode === 'SCENARIO'
                ? scenarioState?.depth_polygons ?? null
                : depthPolygons
          }
          scenarioRoadDepths={mode === 'SCENARIO' ? scenarioRoadDepths : null}
          streetRisk={
            mode === 'LIVE'
              ? activeHorizonState?.streets ?? null
              : mode === 'SCENARIO'
                ? scenarioState?.streets ?? null
                : replayStreets
          }
          theme={theme}
        />

        {/* LEFT WORKSPACE OVERLAY: V1 control stack */}
        <div className={`map-left-overlay delhi-overlay ${mobileDrawerExpanded ? 'drawer-expanded' : 'drawer-collapsed'}`}>
          {/* Mobile Drawer Handle / Sticky Header */}
          <div
            className="drawer-handle-bar"
            onClick={() => setMobileDrawerExpanded(prev => !prev)}
            role="button"
            tabIndex={0}
            aria-expanded={mobileDrawerExpanded}
            aria-label={mobileDrawerExpanded ? 'Collapse dashboard insights drawer' : 'Expand dashboard insights drawer'}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                setMobileDrawerExpanded(prev => !prev);
              }
            }}
          >
            <div className="drawer-pill-handle" />
            <div className="drawer-title-row">
              <span className="drawer-summary-text">
                <span className="drawer-risk-dot" style={{ backgroundColor: banner.legendColor }} />
                {banner.text} • {maxDepthM !== null ? `${(maxDepthM * 100).toFixed(0)} cm` : 'no value'} peak
              </span>
              <span className="drawer-toggle-btn">
                {mobileDrawerExpanded ? 'Collapse ▾' : 'Insights ▴'}
              </span>
            </div>
          </div>
          <div className="delhi-panel-scroll">
          {/* CONTROL CARD (V1): mode switcher + outlook controls in ONE card */}
          <div className="flood-timeline-panel">
            <div className="mode-toggle-group">
            <button
              type="button"
              className={`mode-toggle-btn ${mode === 'LIVE' ? 'active' : ''}`}
              onClick={() => switchMode('LIVE')}
            >
              LIVE FORECAST
            </button>
            <button
              type="button"
              className={`mode-toggle-btn ${mode === 'SCENARIO' ? 'scenario-active' : ''}`}
              onClick={() => switchMode('SCENARIO')}
            >
              MODEL SCENARIO
            </button>
            <button
              type="button"
              className={`mode-toggle-btn ${mode === 'REPLAY' ? 'historical-active' : ''}`}
              onClick={() => switchMode('REPLAY')}
            >
              HISTORICAL REPLAY
            </button>
            </div>

            {/* FORECAST / SCENARIO TIMELINE (V1 horizon stepper) */}
            {mode === 'LIVE' && (
              <>
                <div className="timeline-header">
                  <span className="timeline-heading">FLOOD OUTLOOK</span>
                  <span className="timeline-active-tag">
                    Active: {horizon}
                    {syntheticFallback ? ' · SYNTHETIC FALLBACK' : ''}
                  </span>
                </div>
                <div className="timeline-stepper">
                  {HORIZONS.map((h) => {
                    const hs = liveState?.horizons.find((x) => x.horizon === h);
                    const rain = hs?.rainfall_mm;
                    const rainTag =
                      rain !== null && rain !== undefined
                        ? `${rain.toFixed(1)}mm`
                        : hs
                          ? '--mm'
                          : '…';
                    return (
                      <button
                        key={h}
                        type="button"
                        className={`timeline-step-btn ${horizon === h ? 'selected' : ''}`}
                        onClick={() => setHorizon(h)}
                      >
                        <span className="step-time">{h}</span>
                        <span className="step-rain-tag">{rainTag}</span>
                      </button>
                    );
                  })}
                </div>
                {liveLoading && <div className="delhi-note">Acquiring forecast and evaluating horizons…</div>}
                {liveError && (
                  <div className="delhi-panel-error route-error">
                    {liveError}{' '}
                    <button className="delhi-btn" onClick={() => loadLive(true)}>
                      Retry
                    </button>
                  </div>
                )}
                {syntheticFallback && (
                  <div className="scenario-explanatory-note" style={{ color: '#b45309' }}>
                    Live NWP forecast unavailable — running the labeled SYNTHETIC_FALLBACK demo rainfall
                    series. Not observed, not a forecast.
                  </div>
                )}
              </>
            )}

            {mode === 'SCENARIO' && (
              <>
                <div className="timeline-header">
                  <span className="timeline-heading" style={{ color: '#0369a1' }}>
                    MODEL SCENARIO (WHAT-IF)
                  </span>
                  <span className="timeline-active-tag" style={{ background: '#e0f2fe', color: '#0369a1' }}>
                    {scenarioMm} mm/h uniform
                  </span>
                </div>
                <div className="scenario-direct-row">
                  <span className="direct-label">INTENSITY:</span>
                  <div className="direct-buttons">
                    {SCENARIO_PRESETS.map((sc) => (
                      <button
                        key={sc}
                        type="button"
                        className={`scenario-rate-pill ${scenarioMm === sc ? 'active' : ''}`}
                        onClick={() => setScenarioMm(sc)}
                        title={`Evaluate ${sc} mm/h through the modelled pipeline`}
                      >
                        {sc} mm/h
                      </button>
                    ))}
                  </div>
                </div>
                {scenarioLoading && <div className="delhi-note">Evaluating scenario through the modelled pipeline…</div>}
                <div className="scenario-explanatory-note">
                  WHAT-IF: hypothetical uniform rainfall evaluated by real backend computation — not live
                  weather, not a forecast.
                </div>
              </>
            )}

            {/* HISTORICAL REPLAY CONTROLS (V1 block, same slot as V1) */}
            {mode === 'REPLAY' && !evidenceOpen && (
              <ReplayControls
                onMapState={setMapFlow}
                onDepthCells={setDepthCells}
                onDepthPolygons={setDepthPolygons}
                onStreets={setReplayStreets}
                onReplayContext={handleReplayContext}
              />
            )}
            {!evidenceOpen && mode === 'REPLAY' && replayStreets && (
              <StreetIntelCard
                streets={replayStreets}
                modeLabel="REPLAY"
                hourLabel={replayStreets.horizon}
              />
            )}
          </div>

            {/* MAP EXTENT CONTROLS (V1: inside the left stack) */}
            <div className="map-extent-tools">
              <button
                type="button"
                className="extent-btn"
                onClick={() =>
                  (window as any).map?.flyTo({ center: [77.2265, 28.5595], zoom: 11.6, duration: 900 })
                }
                title="Reset to the full Delhi region"
              >
                FULL EXTENT · DELHI
              </button>
              <button
                type="button"
                className="extent-btn highlight"
                onClick={() =>
                  (window as any).map?.flyTo({ center: [77.2265, 28.5595], zoom: 13.6, duration: 900 })
                }
                title="Center on the Kushak-Barapullah corridor window"
              >
                TARGET ZONE · KUSHAK
              </button>
              <button
                type="button"
                className={`extent-btn routing-btn ${routeHudOpen ? 'active' : ''}`}
                onClick={() => {
                  // V1 behaviour: routing works in every workflow without
                  // leaving it — the mode only decides which hazard state
                  // the route is evaluated against (nowcast horizon,
                  // replayed timestep, or scenario intensity).
                  setRouteHudOpen((open) => {
                    const next = !open;
                    if (next) {
                      setMobileDrawerExpanded(false);
                      if (!routePicks.origin) {
                        setRoutePick('origin');
                      } else if (!routePicks.destination) {
                        setRoutePick('destination');
                      }
                    } else {
                      handleExitRouting();
                    }
                    return next;
                  });
                }}
                title="Toggle Flood-Safe Route Finder (click map origin & destination)"
              >
                SAFE ROUTE {routeHudOpen ? '●' : ''}
              </button>
            </div>

            {/* HERO RISK CARD (V1 peak-depth summary) — LIVE & SCENARIO */}
            {(mode === 'LIVE' || mode === 'SCENARIO') && (
              <div className="flood-status-panel">
                <div className="status-panel-top">
                  <div className="status-horizon-kicker text-xs font-medium uppercase tracking-widest text-gray-500">
                    {mode === 'LIVE' ? 'PREDICTED CONDITIONS' : 'SCENARIO CONDITIONS'}
                  </div>
                  <div className="status-horizon-title text-xl font-bold tracking-tight">
                    {mode === 'LIVE'
                      ? HORIZON_TITLES[horizon]
                      : `What-If: ${scenarioMm} mm/h`}
                  </div>
                </div>
                <div
                  className="status-hero-card"
                  style={{ backgroundColor: banner.bg, borderColor: banner.border }}
                >
                  <div className="hero-risk-header">
                    <span className="hero-risk-kicker" style={{ color: banner.color }}>
                      {mode === 'LIVE' ? 'PREDICTED DEPTH BAND' : 'SCENARIO DEPTH BAND'}
                    </span>
                    <span className="hero-risk-badge" style={{ background: banner.color, color: '#fff' }}>
                      {banner.text}
                    </span>
                  </div>
                  <div className="hero-depth-row">
                    <div className="depth-digit-group">
                      <span className="depth-large" style={{ color: banner.color }}>
                        {maxDepthM === null ? '--' : (maxDepthM * 100).toFixed(0)}
                      </span>
                      {maxDepthM !== null && (
                        <span className="depth-metric-unit">cm</span>
                      )}
                    </div>
                    <div className="depth-context-label">
                      <div className="context-primary">Peak modelled depth</div>
                      <div className="context-secondary">
                        {maxDepthM === null ? '30 m grid • no value' : 'deepest 30 m grid cell'}
                      </div>
                    </div>
                  </div>
                  <div className="hero-impact-note" style={{ color: banner.color }}>
                    {banner.desc}
                  </div>
                </div>
                <div className="status-metric-grid grid grid-cols-2 gap-4 mt-4">
                  <div className="grid-metric-box bg-gray-50 p-3 rounded-md">
                    <span className="metric-box-label text-xs font-semibold uppercase tracking-wider text-gray-500">
                      {rainfallMetricLabel}
                    </span>
                    <span className="metric-box-val block text-2xl font-bold tracking-tight">
                      {rainfallMetricVal} <span className="sub-unit text-sm font-medium text-gray-400">mm</span>
                    </span>
                    <span className="metric-box-sub block text-xs font-normal text-gray-600">
                      {rainfallMetricSub}
                    </span>
                  </div>
                  <div className="grid-metric-box bg-gray-50 p-3 rounded-md">
                    <span className="metric-box-label text-xs font-semibold uppercase tracking-wider text-gray-500">
                      INUNDATED AREA
                    </span>
                    <span className="metric-box-val block text-2xl font-bold tracking-tight">
                      {floodedAreaM2 === null ? '--' : Math.round(floodedAreaM2).toLocaleString()}{' '}
                      <span className="sub-unit text-sm font-medium text-gray-400">m²</span>
                    </span>
                    <span className="metric-box-sub block text-xs font-normal text-gray-600">30m DEM grid</span>
                  </div>
                </div>
                <div className="status-panel-footer">
                  <div className="footer-engine-title text-sm font-semibold uppercase tracking-wider text-gray-700">
                    MODEL PIPELINE
                  </div>
                  <div className="footer-engine-desc text-xs font-normal text-gray-500 mb-2">
                    Rainfall–runoff + inlet conveyance + surface routing (V1 reference model)
                  </div>
                  <div className="footer-engine-param text-xs font-medium text-gray-600 bg-gray-100 p-2 rounded">
                    Runoff C = 0.75 • 60-minute horizon step • SIMULATED MODEL OUTPUT
                  </div>
                </div>
              </div>
            )}

            {/* MODE PANELS */}
            {evidenceOpen ? (
              <>
                <EvidencePanel />
                <ModelPanel />
              </>
            ) : mode === 'LIVE' && (
              <>
                <LivePanel
                  liveState={liveState}
                  horizon={horizon}
                  onSurfaceHotspots={setSurfaceHotspots}
                />
                {streets && (
                  <StreetIntelCard streets={streets} modeLabel={mode} />
                )}
                <RoutePanel
                  onMapState={setMapFlow}
                  pickTarget={routePick}
                  setPickTarget={setRoutePick}
                  picks={routePicks}
                  setPicks={setRoutePicks}
                  routeResult={routeResult?.mode === 'SCENARIO' ? null : routeResult}
                  setRouteResult={setRouteResult}
                />
              </>
            )}
            {evidenceOpen && mode !== 'LIVE' && (
              <div className="delhi-note" style={{ padding: '0 4px' }}>
                Evidence surfaces are shown alongside the map. Switch to LIVE FORECAST for the operational
                view, or return to SCENARIO/REPLAY panels.
              </div>
            )}
            {!evidenceOpen && mode === 'SCENARIO' && (
              <>
                {streets && <StreetIntelCard streets={streets} modeLabel="SCENARIO" />}
                <ScenarioPanel
                  onMapState={setMapFlow}
                  onSurfaceHotspots={setSurfaceHotspots}
                  onDepthCells={setDepthCells}
                  onDepthPolygons={setDepthPolygons}
                  onRoadDepths={setScenarioRoadDepths}
                />
              </>
            )}

            {/* PROVENANCE CARD (V1 data status & provenance — inside the left stack) */}
            <ProvenanceCard
              mode={mode}
              syntheticFallback={syntheticFallback}
              rainfallStatus={liveState?.rainfall_status ?? null}
              acquiredAt={liveState?.rainfall_acquired_at ?? null}
            />
          </div>
        </div>

        {/* SAFE-ROUTE HUD (V1 floating panel; all three workflows) */}
        {routeHudOpen && (
          <SafeRouteHud
            picks={routePicks}
            routeResult={routeResult}
            pickTarget={routePick}
            loading={routeLoading}
            error={routeError}
            onExit={handleExitRouting}
            onPick={(t) => setRoutePick(t)}
            transportMode={transportMode}
            onTransportMode={(next) => {
              routeRequestSeq.current += 1;
              routeInflightKey.current = null;
              setTransportMode(next);
              setRouteResult(null);
              setRouteError(null);
            }}
            onPreset={(o, d) => {
              setRoutePicks({ origin: { coords: o }, destination: { coords: d } });
              setRoutePick(null);
              const context = routeContext();
              if (context && transportMode) void fetchSafeRoute(o, d, context);
            }}
            onRetry={() => {
              if (routePicks.origin && routePicks.destination) {
                const context = routeContext();
                if (context && transportMode) void fetchSafeRoute(
                  routePicks.origin.coords,
                  routePicks.destination.coords,
                  context,
                );
              }
            }}
          />
        )}
        {/* FLOOD DEPTH LEGEND (V1, always visible) */}
        <div className="flood-legend-card">
          <div className="legend-heading">Modelled flood depth (cm)</div>
          <div className="legend-scale">
            {DEPTH_LEGEND.map((g) => (
              <div className="legend-grade" key={g.range}>
                <span className="grade-color" style={{ backgroundColor: g.color }} />
                <span className="grade-range">{g.range}</span>
                <span className="grade-desc">{g.desc}</span>
              </div>
            ))}
          </div>
          <div className="legend-note">
            Depth bands of the V1 model output, in centimetres — the same
            thresholds the map uses.
            Classification bands, not hazard standards or a safety claim.
          </div>
        </div>

      </div>
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* STREET & INTERSECTION INTELLIGENCE (V1 panel)                       */
/* ------------------------------------------------------------------ */

type StreetsSummary = NonNullable<
  LiveStateResponse['horizons'][number]['streets']
>['summary'];
type AffectedRoad = NonNullable<
  LiveStateResponse['horizons'][number]['streets']
>['affected_roads'][number];

const TREND_META: Record<string, { icon: string; label: string; color: string }> = {
  RISING: { icon: '▲', label: 'Rising', color: '#b91c1c' },
  FALLING: { icon: '▼', label: 'Falling', color: '#1d4ed8' },
  STEADY: { icon: '＝', label: 'Steady', color: '#6b7280' },
  EMERGING: { icon: '↑', label: 'Newly flooded', color: '#9333ea' },
};

const StreetIntelCard = ({
  streets,
  modeLabel,
  hourLabel = null,
}: {
  streets: {
    summary: StreetsSummary;
    affected_roads: AffectedRoad[];
    provenance: Record<string, string>;
  };
  modeLabel: 'LIVE' | 'SCENARIO' | 'REPLAY';
  hourLabel?: string | null;
}) => {
  const s = streets.summary;
  const trends = 'trend_counts' in s ? (s.trend_counts ?? null) : null;
  return (
    <div className="street-intelligence-panel">
      <div className="street-panel-header">
        <div className="street-header-title">
          <span className="street-title-icon">🛣️</span>{' '}
          {modeLabel === 'LIVE'
            ? 'STREET FLOOD RISK'
            : modeLabel === 'REPLAY'
              ? `REPLAY STREET FLOOD${hourLabel ? ` • ${hourLabel}` : ''}`
              : 'SCENARIO STREET RISK'}
        </div>
        <span className="prov-tag tag-modelled" style={{ fontSize: '9px', padding: '2px 6px', background: '#fef3c7', color: '#92400e' }}>
          Modelled (V1 depth grid)
        </span>
      </div>
      {s.total_affected_roads > 0 || s.total_affected_intersections > 0 ? (
        <>
          <div className="street-impact-summary">
            <div className="impact-count-badge">
              <span className="count-num">{s.total_affected_roads}</span>
              <span className="count-lbl">Streets</span>
            </div>
            <div className="impact-count-badge">
              <span className="count-num">{s.total_affected_intersections}</span>
              <span className="count-lbl">Junctions</span>
            </div>
            <div className="impact-depth-badge">
              <span className="depth-lbl">Peak Street Depth</span>
              <span className="depth-val">{(s.max_street_depth_m * 100).toFixed(0)} cm</span>
            </div>
          </div>
          <div className="street-risk-pills">
            {s.risk_counts.CRITICAL > 0 && <span className="risk-pill critical">{s.risk_counts.CRITICAL} Critical</span>}
            {s.risk_counts.HIGH > 0 && <span className="risk-pill high">{s.risk_counts.HIGH} High</span>}
            {s.risk_counts.MEDIUM > 0 && <span className="risk-pill medium">{s.risk_counts.MEDIUM} Medium</span>}
            {s.risk_counts.LOW > 0 && <span className="risk-pill low">{s.risk_counts.LOW} Low</span>}
          </div>
          {trends && (trends.RISING + trends.FALLING + trends.EMERGING > 0 || trends.STEADY > 0) && (
            <div className="street-risk-pills" title="Change of modelled corridor depth vs the previous replay timestep (not observed)">
              {trends.EMERGING > 0 && <span className="risk-pill" style={{ background: '#f3e8ff', color: '#6b21a8' }}>↑ {trends.EMERGING} newly flooded</span>}
              {trends.RISING > 0 && <span className="risk-pill" style={{ background: '#fee2e2', color: '#b91c1c' }}>▲ {trends.RISING} rising</span>}
              {trends.FALLING > 0 && <span className="risk-pill" style={{ background: '#dbeafe', color: '#1d4ed8' }}>▼ {trends.FALLING} falling</span>}
              {trends.STEADY > 0 && <span className="risk-pill" style={{ background: '#f1f5f9', color: '#475569' }}>＝ {trends.STEADY} steady</span>}
            </div>
          )}
          <div className="street-affected-list">
            <div className="list-title">
              {modeLabel === 'REPLAY' ? 'CORRIDOR DEPTH VS PREVIOUS HOUR' : 'KEY IMPACTED CORRIDORS'}
            </div>
            {streets.affected_roads.slice(0, 3).map((r, i) => (
              <div key={`${r.road_id}-${i}`} className="affected-street-item">
                <div className="street-item-left">
                  <span className="street-item-name">{r.name}</span>
                  <span className="street-item-sub">
                    {r.highway} • {r.flooded_length_m}m flooded
                  </span>
                </div>
                <div className="street-item-right">
                  <span className={`street-risk-tag risk-${r.risk_level.toLowerCase()}`}>{r.risk_level}</span>
                  <span className="street-depth-tag">{Math.round(r.max_depth_m * 100)} cm</span>
                  {r.depth_trend && (
                    <span
                      className="street-depth-tag"
                      style={{ color: TREND_META[r.depth_trend]?.color ?? '#475569' }}
                      title={`Modelled change vs the previous replay timestep: ${
                        r.depth_delta_m !== undefined
                          ? `${(r.depth_delta_m * 100).toFixed(1)} cm`
                          : r.depth_trend
                      }`}
                    >
                      {TREND_META[r.depth_trend]?.icon ?? '•'}{' '}
                      {r.depth_delta_m !== undefined
                        ? `${r.depth_delta_m > 0 ? '+' : ''}${(r.depth_delta_m * 100).toFixed(1)}cm`
                        : (TREND_META[r.depth_trend]?.label ?? '')}
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      ) : (
        <div className="street-safe-state">
          <span className="safe-icon">✅</span>
          <div className="safe-text">
            <div className="safe-title">All Arterial Streets Passable</div>
            <div className="safe-sub">Zero surface ponding modelled on OSM street corridors</div>
          </div>
        </div>
      )}
      <div className="street-panel-provenance">
        OSM roads/junctions matched to the modelled depth grid • Not municipal road sensors • UNKNOWN
        beyond the match distance, never 0 cm
        {trends &&
          ' • ▲/▼ depth change vs the previous replay timestep of the same model, not observed'}
      </div>
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* SAFE-ROUTE HUD (V1 floating panel)                                  */
/* ------------------------------------------------------------------ */

const SafeRouteHud = ({
  picks,
  routeResult,
  pickTarget,
  loading,
  error,
  onExit,
  onPick,
  transportMode,
  onTransportMode,
  onPreset,
  onRetry,
}: {
  picks: { origin: RoutePickPoint | null; destination: RoutePickPoint | null };
  routeResult: DelhiRouteResult | null;
  pickTarget: 'origin' | 'destination' | null;
  loading: boolean;
  error: string | null;
  onExit: () => void;
  onPick: (t: 'origin' | 'destination') => void;
  transportMode: TransportMode | null;
  onTransportMode: (mode: TransportMode) => void;
  onPreset: (o: [number, number], d: [number, number]) => void;
  onRetry: () => void;
}) => {
  const rec = routeResult?.recommended_route;
  const classification = routeResult?.safety_classification ?? rec?.safety_classification ?? null;
  const statusClass =
    classification === 'LOWER_RISK'
      ? 'status-safe'
      : classification === 'CAUTION'
        ? 'status-caution'
        : classification === 'NO_SAFE_ROUTE'
          ? 'status-no_path_found'
          : 'status-unavailable';
  const modelState = routeResult?.model_state;
  const modelFrame = modelState
    ? routeResult?.mode === 'SCENARIO'
      ? `Scenario${modelState.scenario_id ? ` ${modelState.scenario_id}` : ''}`
      : routeResult?.mode === 'HISTORICAL'
        ? `Replay ${modelState.event_id ?? 'event'} · t${modelState.timestep_index ?? '—'}`
        : `Forecast ${modelState.timestep_index === 0 ? 'NOW' : `t+${modelState.timestep_index}h`}`
    : null;
  return (
    <div className="safe-route-hud-panel">
      <div className="route-hud-header">
        <div className="route-hud-title-group">
          <span className="route-hud-icon">R</span>
          <span className="route-hud-title">FLOOD-SAFE ROUTE</span>
        </div>
        <div className="route-hud-actions">
          {rec && (
            <span className={`route-status-badge ${statusClass}`}>
              {classification ?? 'EVALUATED'}
            </span>
          )}
          <button type="button" className="route-close-btn" onClick={onExit} title="Exit safe routing">
            ✕
          </button>
        </div>
      </div>
      <div className="route-transport-selector" role="group" aria-label="Transport mode">
        <div className="presets-label">SELECT TRANSPORT BEFORE CALCULATING</div>
        <div className="presets-buttons">
          <button type="button" className={`preset-btn ${transportMode === 'car' ? 'active' : ''}`} onClick={() => onTransportMode('car')} aria-pressed={transportMode === 'car'}>
            🚗 Car
          </button>
          <button type="button" className={`preset-btn ${transportMode === 'walk' ? 'active' : ''}`} onClick={() => onTransportMode('walk')} aria-pressed={transportMode === 'walk'}>
            🚶 Walk
          </button>
        </div>
        {!transportMode && <div className="delhi-note">Choose Car or Walk before the model calculates a route.</div>}
      </div>
      <div className="route-step-banner">
        {loading ? (
          <div className="step-prompt loading">
            <span className="route-loading-spinner" />
            <span>Computing flood-safe route along corridor…</span>
          </div>
        ) : !transportMode ? (
          <div className="step-prompt">
            <span className="step-pin origin-pin">MODE</span>
            <span>Select Car or Walk above to continue.</span>
          </div>
        ) : !picks.origin ? (
          <div className="step-prompt">
            <span className="step-pin origin-pin">📍</span>
            <span>
              Click map to place <strong>Origin</strong>
            </span>
          </div>
        ) : !picks.destination ? (
          <div className="step-prompt">
            <span className="step-pin dest-pin">END</span>
            <span>
              Origin set. Click map for <strong>Destination</strong>
            </span>
          </div>
        ) : (
          <div className="step-locations">
            <button
              type="button"
              className={`loc-item clickable ${pickTarget === 'origin' ? 'picking' : ''}`}
              onClick={() => onPick('origin')}
              title="Click to re-pick origin on map"
            >
              <span className="loc-dot origin" />
              <span className="loc-text">
                {routeResult?.origin.snapped_road ||
                  `${picks.origin.coords[1].toFixed(4)}, ${picks.origin.coords[0].toFixed(4)}`}
              </span>
              <span className="loc-pick-hint">{pickTarget === 'origin' ? 'PICKING…' : 'CHANGE'}</span>
            </button>
            <div className="loc-arrow">↓</div>
            <button
              type="button"
              className={`loc-item clickable ${pickTarget === 'destination' ? 'picking' : ''}`}
              onClick={() => onPick('destination')}
              title="Click to re-pick destination on map"
            >
              <span className="loc-dot dest" />
              <span className="loc-text">
                {routeResult?.destination.snapped_road ||
                  `${picks.destination.coords[1].toFixed(4)}, ${picks.destination.coords[0].toFixed(4)}`}
              </span>
              <span className="loc-pick-hint">{pickTarget === 'destination' ? 'PICKING…' : 'CHANGE'}</span>
            </button>
          </div>
        )}
      </div>

      {error && !loading && (
        <div className="route-error-banner">
          <span>{error}</span>
          <button type="button" className="delhi-btn route-retry-btn" onClick={onRetry}>
            Retry
          </button>
        </div>
      )}

      {routeResult && !rec && !loading && (
        <div className="route-no-result-banner" role="status">
          <strong>{classification ?? 'NO_SAFE_ROUTE'}</strong>
          {(routeResult.explanation ?? []).slice(0, 2).map((line, index) => (
            <span key={index}>{line}</span>
          ))}
        </div>
      )}

      <div className="route-presets-section">
        <div className="presets-label">
          {pickTarget ? `CLICK MAP TO PLACE ${pickTarget.toUpperCase()}` : 'OR CHOOSE CORRIDOR PRESET:'}
        </div>
        <div className="presets-buttons">
          <button
            type="button"
            className="preset-btn"
            onClick={() => onPreset([77.2055, 28.5685], [77.2435, 28.5715])}
            title="Africa Avenue (UG-01) to Corridor East preset"
          >
            Africa Ave → East
          </button>
          <button
            type="button"
            className="preset-btn"
            onClick={() => onPreset([77.2260, 28.5480], [77.2440, 28.5620])}
            title="Corridor South to Barapullah Junction preset"
          >
            South → Barapullah
          </button>
        </div>
      </div>
      {rec && (
        <>
            <div className="route-metrics-grid">
            <div className="route-metric-card">
              <span className="m-label">TRANSPORT</span>
              <span className="m-val">{routeResult?.transport_mode ?? '—'}</span>
              <span className="m-sub">mode-specific graph rules</span>
            </div>
            <div className="route-metric-card">
              <span className="m-label">SAFETY</span>
              <span className="m-val">{routeResult?.safety_classification ?? '—'}</span>
              <span className="m-sub">not a guarantee</span>
            </div>
            <div className="route-metric-card">
              <span className="m-label">ROUTE DISTANCE</span>
              <span className="m-val">
                {rec.total_distance_km} <span className="m-unit">km</span>
              </span>
                <span className="m-sub">{rec.estimated_travel_time_min == null ? 'ETA unavailable' : `${rec.estimated_travel_time_min} min est.`}</span>
            </div>
            <div className="route-metric-card">
              <span className="m-label">ELEVATED-RISK</span>
              <span className={`m-val ${rec.flood_risk_summary.elevated_risk_segments > 0 ? 'highlight-danger' : ''}`}>
                {rec.flood_risk_summary.elevated_risk_segments}
              </span>
              <span className="m-sub">
                {routeResult?.mode === 'SCENARIO'
                  ? 'segments at/above the modelled hazard depth'
                  : 'segments with modeled loading'}
              </span>
            </div>
            <div className="route-metric-card">
              <span className="m-label">UNKNOWN</span>
              <span className="m-val">{rec.flood_risk_summary.unknown_segments}</span>
              <span className="m-sub">no model coverage (not safe)</span>
            </div>
            <div className="route-metric-card">
              <span className="m-label">UNCERTAIN / HAZARDOUS</span>
              <span className="m-val">
                {rec.flood_risk_summary.uncertain_segments ?? 0} / {rec.flood_risk_summary.hazardous_segments ?? 0}
              </span>
              <span className="m-sub">missing or elevated hazard inputs</span>
            </div>
            <div className="route-metric-card">
              <span className="m-label">MAX DEPTH</span>
              <span className="m-val">{rec.max_depth_m == null ? 'UNKNOWN' : `${(rec.max_depth_m * 100).toFixed(0)} cm`}</span>
              <span className="m-sub">velocity: {rec.max_velocity_m_s == null ? 'UNKNOWN' : `${rec.max_velocity_m_s} m/s`}</span>
            </div>
            <div className="route-metric-card">
              <span className="m-label">EVIDENCE</span>
              <span className="m-val">{routeResult?.evidence_state.route_state}</span>
              <span className="m-sub">weakest-link state</span>
            </div>
          </div>
          {routeResult?.explanation?.length ? (
            <div className="route-comparison-note">{routeResult.explanation[0]}</div>
          ) : null}
          {modelFrame && (
            <div className="route-comparison-note">
              {modelFrame}
              {modelState?.model_timestamp ? ` · model time ${modelState.model_timestamp}` : ''}
              {modelState?.forecast_acquired_at ? ` · acquired ${modelState.forecast_acquired_at}` : ''}
            </div>
          )}
          <div className="route-unavailable-warning">
            {routeResult && routeResult.mode === 'SCENARIO' ? (
              <>
                WHAT-IF {routeResult.scenario.rainfall_mm_h ?? '—'} mm/h • peak modelled depth on this
                route {routeResult.max_depth_on_route_m == null ? 'UNKNOWN' : `${Math.round(routeResult.max_depth_on_route_m * 100)} cm`}. Lowest exposure under a
                hypothetical input — NOT live weather, NOT a guarantee of safety.
              </>
            ) : (
              'LOW MODELED EXPOSURE ≠ SAFE: stage/depth remain UNKNOWN on the Kushak corridor. See the route panel for the full evidence trail.'
            )}
          </div>
        </>
      )}
      <div className="route-hud-footer">
        <button type="button" className="route-clear-btn" onClick={onExit}>
          Exit Routing
        </button>
        <button
          type="button"
          className={`route-pick-btn ${pickTarget === 'origin' ? 'active' : ''}`}
          onClick={() => onPick('origin')}
        >
          {pickTarget === 'origin' ? '● Picking Origin' : 'Pick Origin'}
        </button>
        <button
          type="button"
          className={`route-pick-btn ${pickTarget === 'destination' ? 'active' : ''}`}
          onClick={() => onPick('destination')}
        >
          {pickTarget === 'destination' ? '● Picking Dest' : 'Pick Dest'}
        </button>
      </div>
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* PROVENANCE CARD (V1 data status & provenance)                       */
/* ------------------------------------------------------------------ */

const ProvenanceCard = ({
  mode,
  syntheticFallback,
  rainfallStatus,
  acquiredAt,
}: {
  mode: Mode;
  syntheticFallback: boolean;
  rainfallStatus: string | null;
  acquiredAt: string | null;
}) => (
  <div className="provenance-card">
    <div className="provenance-header">
      {mode === 'REPLAY'
        ? 'HISTORICAL DATA PROVENANCE'
        : mode === 'SCENARIO'
          ? 'SCENARIO PROVENANCE & STATUS'
          : 'DATA PROVENANCE & STATUS'}
    </div>
    <div className="provenance-list">
      <div className="provenance-item">
        <span className="prov-source">Rainfall</span>
        <span
          className="prov-tag tag-modelled"
          style={
            syntheticFallback
              ? { background: '#7c2d12', color: '#fed7aa' }
              : mode === 'SCENARIO'
                ? { background: '#e0f2fe', color: '#0369a1' }
                : undefined
          }
        >
          {mode === 'SCENARIO'
            ? 'MODEL_SCENARIO (hypothetical)'
            : syntheticFallback
              ? 'SYNTHETIC_FALLBACK (demo)'
              : `Open-Meteo NWP (${rainfallStatus ?? '…'})`}
        </span>
      </div>
      <div className="provenance-item">
        <span className="prov-source">Elevation</span>
        <span className="prov-tag tag-modelled">Copernicus GLO-30 DSM (30m, enforced)</span>
      </div>
      <div className="provenance-item">
        <span className="prov-source">Flood depth</span>
        <span className="prov-tag tag-modelled">
          {mode === 'REPLAY' ? 'SIMULATED (event replay)' : 'MODELLED (V1 reference)'}
        </span>
      </div>
      <div className="provenance-item">
        <span className="prov-source">Roads / junctions</span>
        <span className="prov-tag tag-modelled">OpenStreetMap (ODbL) + model matching</span>
      </div>
      <div className="provenance-item">
        <span className="prov-source">Drainage</span>
        <span className="prov-tag tag-modelled">DERIVED / INFERRED_EFFECTIVE (not as-built)</span>
      </div>
      <div className="provenance-item">
        <span className="prov-source">Observations</span>
        <span className="prov-tag tag-prototype">OBSERVED (GSDL events) — validation only</span>
      </div>
    </div>
    <div className="provenance-disclaimer">
      {mode === 'REPLAY'
        ? 'RETROSPECTIVE EVENT REPLAY: model execution against documented events. Never observed depth; observed evidence is labeled separately.'
        : mode === 'SCENARIO'
          ? 'WHAT-IF: hypothetical uniform rainfall through the modelled pipeline. Not live weather, not a forecast.'
          : syntheticFallback
            ? 'SYNTHETIC FALLBACK ACTIVE: the live NWP forecast is unavailable; a deterministic labeled demo series keeps the system usable. These are NOT observed measurements.'
            : 'Weather forecast from Open-Meteo hourly NWP (model value, not observed rainfall). Flood depth is MODELLED on a 30 m DSM — never observed.'}
      {acquiredAt && mode === 'LIVE' && !syntheticFallback
        ? ` Forecast acquired ${new Date(acquiredAt).toLocaleTimeString()}.`
        : ''}
    </div>
  </div>
);

export default DelhiApp;
