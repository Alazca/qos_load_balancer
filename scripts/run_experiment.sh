#!/usr/bin/env bash
# =============================================================================
#  run_experiment.sh
# =============================================================================
#  Drives one end-to-end Prometheus experiment:
#
#     1. Launches Ryu (background)
#     2. Launches Mininet topology (background, no-CLI mode)
#     3. Starts the backend echo servers on srv1..srv3 inside Mininet
#     4. Drives the workload from h1..h6
#     5. Collects CSVs and renders a summary plot
#     6. Tears everything down
#
#  Usage:
#     sudo ./scripts/run_experiment.sh [steady|burst|closed] [duration_s]
#
#  Note: must be run from the project root with sudo (Mininet requires root).
# =============================================================================
set -euo pipefail

# ---- args ------------------------------------------------------------------
MODE="${1:-burst}"
DURATION="${2:-30}"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESULTS_DIR="${PROJECT_ROOT}/results/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${RESULTS_DIR}"

echo "[exp] mode=${MODE} duration=${DURATION}s results -> ${RESULTS_DIR}"

# ---- cleanup hook ----------------------------------------------------------
# Mininet leaves OVS bridges and namespaces lying around if it dies messily.
# `mn -c` is the canonical fix; we run it both up-front and on exit.
cleanup() {
    echo "[exp] cleaning up..."
    pkill -f "ryu-manager"            || true
    pkill -f "prometheus_topo"        || true
    sudo mn -c                        >/dev/null 2>&1 || true
}
trap cleanup EXIT
sudo mn -c >/dev/null 2>&1 || true

# ---- 1. Ryu controller -----------------------------------------------------
echo "[exp] starting Ryu..."
cd "${PROJECT_ROOT}"
ryu-manager controller.qos_load_balancer \
    >"${RESULTS_DIR}/ryu.log" 2>&1 &
RYU_PID=$!
sleep 2     # give it a moment to bind 6653 before Mininet dials in

# ---- 2. Mininet topology ---------------------------------------------------
# We run Mininet through a tiny driver script (`drive_topology.py`) instead of
# `prometheus_topo.py --no-cli`, because we need to keep the net object alive
# long enough to issue commands to the hosts.
echo "[exp] starting Mininet..."
sudo -E python3 "${PROJECT_ROOT}/scripts/drive_topology.py" \
    --mode "${MODE}" \
    --duration "${DURATION}" \
    --results "${RESULTS_DIR}" \
    2>&1 | tee "${RESULTS_DIR}/mininet.log"

# ---- 3. Plot ---------------------------------------------------------------
echo "[exp] plotting..."
cd "${PROJECT_ROOT}"
python3 -m analysis.plot_metrics \
    "${RESULTS_DIR}"/h*.csv \
    --out "${RESULTS_DIR}/summary.png" \
    --title "Prometheus -- ${MODE} workload, ${DURATION}s"

echo "[exp] done. See ${RESULTS_DIR}/summary.png"
