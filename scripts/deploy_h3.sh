#!/usr/bin/env bash
# Launch the H3 Ref2VA runtime in the foreground.
set -euo pipefail

usage() {
  cat <<'HELP'
Usage: deploy_h3.sh --runtime-dir DIR --model-dir DIR --output-dir DIR [options]

Required:
  --runtime-dir DIR   H3 Ref2VA runtime containing rv2v/server.py
  --model-dir DIR     H3 model weights
  --output-dir DIR    Durable directory for generated media and task records

Options:
  --python PATH       Python in the runtime's CUDA environment (default: python3)
  --host ADDRESS      Listen address (default: 127.0.0.1)
  --port PORT         Listen port (default: 8653)
  --model-name NAME   Model ID in /v1/models (default: MiniMax-H3-Sol)
  --topology dp|cp    Independent GPU workers or one sharded worker (default: dp)
  --gpus N            Number of visible GPUs to use (default: 1)
  -h, --help          Display this help

Use a runtime build with endpoint-anchor-v1, 73-frame tails and endpoint-only-v1.
Install the runtime, its CUDA dependencies and weights before running the launcher.
CUDA_VISIBLE_DEVICES and runtime-specific RV2V_* options can be set in the environment.
HELP
}

die() { printf '%s\n' "$*" >&2; exit 2; }

runtime_dir=
model_dir=
output_dir=
python_bin=${RV2V_PYTHON:-python3}
host=127.0.0.1
port=8653
model_name=MiniMax-H3-Sol
topology=dp
gpus=1
while (($#)); do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --runtime-dir|--model-dir|--output-dir|--python|--host|--port|--model-name|--topology|--gpus)
      (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] || die "Missing value for $1"
      ;;
    *) die "Unknown argument: $1" ;;
  esac
  case "$1" in
    --runtime-dir) runtime_dir=$2 ;;
    --model-dir) model_dir=$2 ;;
    --output-dir) output_dir=$2 ;;
    --python) python_bin=$2 ;;
    --host) host=$2 ;;
    --port) port=$2 ;;
    --model-name) model_name=$2 ;;
    --topology) topology=$2 ;;
    --gpus) gpus=$2 ;;
  esac
  shift 2
done

[[ -n "$runtime_dir" && -d "$runtime_dir" && -r "$runtime_dir/rv2v/server.py" ]] ||
  die "Specify a compatible H3 runtime with --runtime-dir"
[[ -n "$model_dir" && -d "$model_dir" ]] || die "Specify the H3 weights with --model-dir"
[[ -n "$output_dir" ]] || die "Specify a durable --output-dir"
[[ "$gpus" =~ ^[1-9][0-9]{0,2}$ ]] || die "--gpus must be a positive integer"
[[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] && ((port <= 65535)) || die "Invalid --port"
[[ "$topology" == dp || "$topology" == cp ]] || die "--topology must be dp or cp"
command -v "$python_bin" >/dev/null || die "Cannot find the H3 Python interpreter"

runtime_dir=$(cd -- "$runtime_dir" && pwd -P)
export RV2V_MODEL_ROOT
RV2V_MODEL_ROOT=$(cd -- "$model_dir" && pwd -P)
mkdir -p -- "$output_dir"
export RV2V_SERVE_OUTPUT_DIR
RV2V_SERVE_OUTPUT_DIR=$(cd -- "$output_dir" && pwd -P)
export RV2V_SERVE_OUTPUT_ROOT="$RV2V_SERVE_OUTPUT_DIR"
export RV2V_SERVE_HOST="$host" RV2V_SERVE_PORT="$port" RV2V_SERVE_MODEL_NAME="$model_name"
export RV2V_TOPOLOGY="$topology" RV2V_LATENT_CONTINUATION=1 PYTHONUNBUFFERED=1
export PYTHONPATH="$runtime_dir${PYTHONPATH:+:$PYTHONPATH}"

if [[ "$topology" == dp ]] && { ((gpus > 1)) || [[ "${RV2V_GLOBAL_QUEUE:-0}" == 1 ]]; }; then
  [[ -r "$runtime_dir/rv2v/dp_server.py" ]] || die "This runtime has no DP dispatcher"
  export RV2V_DP_WORKERS="$gpus"
  export RV2V_DP_MAX_INFLIGHT_PER_WORKER="${RV2V_DP_MAX_INFLIGHT_PER_WORKER:-1}"
  export RV2V_SHARED_JOB_DIR="${RV2V_SHARED_JOB_DIR:-$RV2V_SERVE_OUTPUT_DIR/.rv2v_jobs}"
  mkdir -p -- "$RV2V_SHARED_JOB_DIR"
  if [[ "${RV2V_GLOBAL_QUEUE:-0}" == 1 ]]; then
    export RV2V_GLOBAL_QUEUE_DIR="${RV2V_GLOBAL_QUEUE_DIR:-$RV2V_SERVE_OUTPUT_DIR/.rv2v_queue}"
    mkdir -p -- "$RV2V_GLOBAL_QUEUE_DIR"
  fi
  exec "$python_bin" "$runtime_dir/rv2v/dp_server.py"
elif [[ "$topology" == cp ]] && ((gpus > 1)); then
  exec "$python_bin" -m torch.distributed.run --standalone --nproc-per-node="$gpus" \
    "$runtime_dir/rv2v/server.py"
else
  exec "$python_bin" "$runtime_dir/rv2v/server.py"
fi
