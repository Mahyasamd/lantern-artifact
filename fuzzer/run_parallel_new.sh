#!/usr/bin/env bash
set -u 

# ======================================
# Configuration
# ======================================
JOBS=7
BASE_PORT=8000
CHROME_BIN="$HOME/chromium/src/out/Default/chrome --no-sandbox --enable-unsafe-webgpu"
FUZZER="python3 fuzz4.py"

# ======================================
# Discover input folders
# ======================================
mapfile -t folders < <(find . -maxdepth 1 -type d -name "cts_mutated*" | sort)
n=${#folders[@]}

if (( n == 0 )); then
  echo "[ERROR]  No cts_mutated* folders found in $(pwd)"
  exit 1
fi

echo "[INFO] Found $n mutated folders in $(pwd)"
echo "[INFO] Running up to $JOBS folders in parallel."
echo "-----------------------------------------------"

# ======================================
# Run fuzzers in parallel
# ======================================
job_pids=()
total_started=0
total_skipped=0
counter=0

for folder in "${folders[@]}"; do
  ((counter++))
  name=$(basename "$folder")
  out_dir="./fuzz_output_${name}"
  port=$((BASE_PORT + counter))

  # skip only if a [DONE] marker exists
  if [[ -f "$out_dir/fuzz_run.log" ]] && grep -q "\[DONE\]" "$out_dir/fuzz_run.log"; then
    echo "[SKIP] ($counter/$n) $name already marked DONE."
    ((total_skipped++))
    continue
  fi

  echo "[JOB] ($counter/$n) Starting $name on port $port"
  mkdir -p "$out_dir"

  (
    PORT=$port $FUZZER -i "$folder" -o "$out_dir" -b "$CHROME_BIN" -p "$port" \
      > "$out_dir/fuzz_run.log" 2>&1
    echo "[DONE] $name finished at $(date)" >> "$out_dir/fuzz_run.log"
    echo "[JOB] $name completed."
  ) &

  job_pids+=($!)
  ((total_started++))

  # Control concurrency
  if (( ${#job_pids[@]} >= JOBS )); then
    wait -n 2>/dev/null || true
    # remove finished jobs from the list
    new_pids=()
    for pid in "${job_pids[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        new_pids+=("$pid")
      fi
    done
    job_pids=("${new_pids[@]}")
  fi
done

# Wait for remaining jobs
if (( ${#job_pids[@]} > 0 )); then
  wait "${job_pids[@]}" 2>/dev/null || true
fi

# ======================================
# Summary
# ======================================
echo "-----------------------------------------------"
echo "[INFO] All fuzzing tasks complete."
echo "[INFO] Total folders: $n"
echo "[INFO] Folders skipped (DONE): $total_skipped"
echo "[INFO] Folders newly started: $total_started"
echo "[INFO] Logs: ./fuzz_output_*/fuzz_run.log"
echo "-----------------------------------------------"
