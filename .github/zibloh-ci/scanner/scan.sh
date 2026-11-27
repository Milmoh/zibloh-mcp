#!/usr/bin/env bash
# Runs every scanner on a target directory and writes raw JSON results to an output directory.
# Never decides pass/fail: that is gate.py. A scanner that crashes is recorded in status.json so the
# gate fails closed.
#
# Env:
#   ZCI_TARGET          directory to scan (default: .)
#   ZCI_OUT             output directory (default: ./zci-out)
#   ZCI_GITLEAKS_MODE   full | range | dir (default: full if TARGET is a git repo, else dir)
#   ZCI_GITLEAKS_RANGE  git revision range for mode=range (e.g. base..head)
#   ZCI_EXCLUDE         extra comma-separated paths to exclude from SAST/IaC (relative to TARGET)
#   ZCI_TOOLS           tool cache directory (default: ~/.cache/zibloh-ci)
set -uo pipefail

ZCI_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$(cd "${ZCI_TARGET:-.}" && pwd)"
OUT="${ZCI_OUT:-$PWD/zci-out}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
TOOLS="${ZCI_TOOLS:-$HOME/.cache/zibloh-ci}"

# ---- pinned versions (bump here only; checksums from the upstream release checksum files) ----
GITLEAKS_VERSION=8.30.1
GITLEAKS_SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
OSV_VERSION=2.7.1
OSV_SHA256=fb7dab19a8bd9bbe5c3189f09d4b7ee1820c9553a23bac202648a56efd2f6ee8
TRIVY_VERSION=0.76.1
TRIVY_SHA256=c7cbe3137fb8a48a17574bede3dd6beb16742d1e9772929940fc90082c39d9c0

BIN="$TOOLS/bin-g$GITLEAKS_VERSION-o$OSV_VERSION-t$TRIVY_VERSION"
VENV="$TOOLS/venv-$(sha256sum "$ZCI_HOME/scanner/requirements.txt" | cut -c1-12)"
mkdir -p "$BIN"

log() { echo "[zibloh-ci] $*" >&2; }

fetch() { # url sha256 dest
  local url="$1" sum="$2" dest="$3"
  curl -fsSL --retry 3 -o "$dest" "$url" || return 1
  echo "$sum  $dest" | sha256sum -c --quiet - || { log "checksum mismatch for $url"; rm -f "$dest"; return 1; }
}

install_tools() {
  if [ ! -x "$BIN/gitleaks" ]; then
    fetch "https://github.com/gitleaks/gitleaks/releases/download/v$GITLEAKS_VERSION/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
      "$GITLEAKS_SHA256" "$BIN/gitleaks.tgz" && tar -xzf "$BIN/gitleaks.tgz" -C "$BIN" gitleaks && rm -f "$BIN/gitleaks.tgz"
  fi
  if [ ! -x "$BIN/osv-scanner" ]; then
    fetch "https://github.com/google/osv-scanner/releases/download/v$OSV_VERSION/osv-scanner_linux_amd64" \
      "$OSV_SHA256" "$BIN/osv-scanner" && chmod +x "$BIN/osv-scanner"
  fi
  if [ ! -x "$BIN/trivy" ]; then
    fetch "https://github.com/aquasecurity/trivy/releases/download/v$TRIVY_VERSION/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
      "$TRIVY_SHA256" "$BIN/trivy.tgz" && tar -xzf "$BIN/trivy.tgz" -C "$BIN" trivy && rm -f "$BIN/trivy.tgz"
  fi
  if [ ! -x "$VENV/bin/semgrep" ]; then
    python3 -m venv "$VENV" && "$VENV/bin/pip" install -q --disable-pip-version-check -r "$ZCI_HOME/scanner/requirements.txt"
  fi
}

declare -A STATUS
record() { STATUS[$1]="$2"; }

log "installing tools into $TOOLS"
install_tools

# ---- language / manifest detection ----
has_files() { # name patterns...
  local args=() first=1
  for p in "$@"; do
    if [ $first -eq 1 ]; then args+=(-name "$p"); first=0; else args+=(-o -name "$p"); fi
  done
  [ -n "$(find "$TARGET" \( -path '*/node_modules' -o -path '*/.git' -o -path '*/.venv' \) -prune -o -type f \( "${args[@]}" \) -print -quit)" ]
}
HAS_PY=0; has_files '*.py' && HAS_PY=1
HAS_JS=0; has_files '*.js' '*.jsx' '*.ts' '*.tsx' '*.mjs' '*.cjs' && HAS_JS=1

EXCLUDES=(node_modules .venv venv dist build coverage .git tests test __tests__ e2e)
IFS=',' read -r -a EXTRA <<< "${ZCI_EXCLUDE:-}"
for e in "${EXTRA[@]}"; do [ -n "$e" ] && EXCLUDES+=("$e"); done

cd "$TARGET"

# ---- gitleaks: secrets ----
MODE="${ZCI_GITLEAKS_MODE:-}"
if [ -z "$MODE" ]; then
  if git -C "$TARGET" rev-parse --is-inside-work-tree >/dev/null 2>&1; then MODE=full; else MODE=dir; fi
fi
GL_ARGS=(--redact --no-banner --exit-code 0 --report-format json --report-path "$OUT/gitleaks.json")
log "gitleaks ($MODE)"
case "$MODE" in
  dir)   "$BIN/gitleaks" dir "${GL_ARGS[@]}" . ;;
  range) "$BIN/gitleaks" git "${GL_ARGS[@]}" --log-opts="${ZCI_GITLEAKS_RANGE}" . ;;
  *)     "$BIN/gitleaks" git "${GL_ARGS[@]}" . ;;
esac
rc=$?; [ $rc -eq 0 ] && record gitleaks ok || record gitleaks "error rc=$rc"

# ---- semgrep: SAST, all languages ----
SG_CONFIGS=(--config "$ZCI_HOME/rules/semgrep")
[ $HAS_PY -eq 1 ] && SG_CONFIGS+=(--config p/python)
[ $HAS_JS -eq 1 ] && SG_CONFIGS+=(--config p/javascript --config p/typescript --config p/react)
SG_EXCL=(); for e in "${EXCLUDES[@]}"; do SG_EXCL+=(--exclude "$e"); done
log "semgrep (py=$HAS_PY js=$HAS_JS)"
SEMGREP_SEND_METRICS=off "$VENV/bin/semgrep" scan --metrics=off --disable-version-check --quiet \
  "${SG_CONFIGS[@]}" "${SG_EXCL[@]}" --json --output "$OUT/semgrep.json" .
rc=$?; [ $rc -eq 0 ] && record semgrep ok || record semgrep "error rc=$rc"

# ---- bandit: Python SAST ----
if [ $HAS_PY -eq 1 ]; then
  BX=""; for e in "${EXCLUDES[@]}"; do BX="$BX,./$e"; done
  log "bandit"
  "$VENV/bin/bandit" -r . -q -f json -o "$OUT/bandit.json" -x "${BX#,}"
  rc=$?; { [ $rc -eq 0 ] || [ $rc -eq 1 ]; } && record bandit ok || record bandit "error rc=$rc"
else
  record bandit skipped
fi

# ---- osv-scanner: vulnerable dependencies ----
OX=(); for e in "${EXCLUDES[@]}"; do OX+=(--x-exclude "$e"); done
log "osv-scanner"
"$BIN/osv-scanner" scan source -r "${OX[@]}" --format json --output-file "$OUT/osv.json" . >/dev/null
rc=$?
case $rc in
  0|1) record osv ok ;;
  128) record osv "skipped (no manifests)"; echo '{"results":[]}' > "$OUT/osv.json" ;;
  *)   record osv "error rc=$rc" ;;
esac

# ---- trivy config: IaC / Dockerfile misconfiguration ----
TSKIP=$(IFS=,; echo "${EXCLUDES[*]}")
log "trivy config"
"$BIN/trivy" config --quiet --format json --output "$OUT/trivy.json" --skip-dirs "$TSKIP" .
rc=$?; [ $rc -eq 0 ] && record trivy ok || record trivy "error rc=$rc"

# ---- status ----
{
  echo "{"
  echo "  \"target\": \"$TARGET\", \"gitleaks_mode\": \"$MODE\","
  echo "  \"versions\": {\"gitleaks\": \"$GITLEAKS_VERSION\", \"osv-scanner\": \"$OSV_VERSION\", \"trivy\": \"$TRIVY_VERSION\", \"semgrep\": \"$("$VENV/bin/semgrep" --version 2>/dev/null)\", \"bandit\": \"$("$VENV/bin/python" -c 'import bandit;print(bandit.__version__)' 2>/dev/null)\"},"
  echo "  \"tools\": {"
  first=1
  for k in gitleaks semgrep bandit osv trivy; do
    [ $first -eq 1 ] || echo ","
    printf '    "%s": "%s"' "$k" "${STATUS[$k]:-missing}"
    first=0
  done
  echo ""
  echo "  }"
  echo "}"
} > "$OUT/status.json"
log "results in $OUT"
