#!/bin/bash
# Status line: git branch (dirty marker) | model name | project version (porxpy, brms)
input=$(cat)
# sed is line-oriented; Claude Code normally sends compact single-line JSON,
# but flatten defensively so a pretty-printed payload (key/value split across
# lines) can't silently make extractions come up empty.
flat_input=$(printf '%s' "$input" | tr '\n' ' ')

model=$(printf '%s' "$flat_input" | sed -n 's/.*"display_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)
cwd=$(printf '%s' "$flat_input" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1 | tr '\\' '/')

RESET=$'\033[0m'
DIM=$'\033[2m'
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
RED=$'\033[31m'
CYAN=$'\033[36m'
ARROW=$'\xe2\x86\x92'

git_part=""
root=""
if [ -n "$cwd" ] && git -C "$cwd" --no-optional-locks rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  branch=$(git -C "$cwd" --no-optional-locks rev-parse --abbrev-ref HEAD 2>/dev/null)
  if [ -n "$branch" ]; then
    if [ -n "$(git -C "$cwd" --no-optional-locks status --porcelain 2>/dev/null)" ]; then
      git_part="${YELLOW}${branch}*${RESET}"
    else
      git_part="${GREEN}${branch}${RESET}"
    fi
  fi
  root=$(git -C "$cwd" --no-optional-locks rev-parse --show-toplevel 2>/dev/null)
fi
[ -z "$root" ] && root="$cwd"

version=""
if [ -n "$root" ]; then
  # Projects that expose VERSION = "x.y.z" in their package __init__.py
  for pkg in porxpy brms; do
    if [ -f "$root/$pkg/__init__.py" ]; then
      version=$(sed -n 's/^VERSION[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$root/$pkg/__init__.py" | head -n1)
      [ -n "$version" ] && break
    fi
  done
fi

# Isolate the "five_hour" / "seven_day" sub-objects of rate_limits BEFORE
# reading "used_percentage" or "resets_at" from them, since both keys also
# appear a second time (used_percentage under context_window; resets_at in
# the *other* rate-limit window). Never grep either key against the whole
# payload or against the wrong sub-object.
extract_obj() {
  printf '%s' "$flat_input" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*{\\([^}]*\\)}.*/\\1/p" | head -n1
}
extract_num() {
  printf '%s' "$1" | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\\([0-9][0-9.]*\\).*/\\1/p" | head -n1
}
color_for_pct() {
  local v="$1"
  if [ "$v" -ge 80 ]; then
    printf '%s' "$RED"
  elif [ "$v" -ge 50 ]; then
    printf '%s' "$YELLOW"
  else
    printf '%s' "$GREEN"
  fi
}
# Countdown to $1 (epoch seconds), relative to $2 (now, epoch seconds).
# Prints nothing if the epoch is not strictly in the future (stale payload).
countdown_str() {
  local remaining=$(( $1 - $2 ))
  [ "$remaining" -le 0 ] && return
  local hours=$(( remaining / 3600 ))
  local minutes=$(( (remaining % 3600) / 60 ))
  if [ "$hours" -gt 0 ]; then
    printf '%dh%02dm' "$hours" "$minutes"
  else
    printf '%dm' "$minutes"
  fi
}

now_epoch=$(date +%s)

usage_bits=()
five_hour_obj=$(extract_obj "five_hour")
if [ -n "$five_hour_obj" ]; then
  five_hour_raw=$(extract_num "$five_hour_obj" "used_percentage")
  if [ -n "$five_hour_raw" ]; then
    five_hour_pct=$(printf '%.0f' "$five_hour_raw")
    bit="$(color_for_pct "$five_hour_pct")5h ${five_hour_pct}%${RESET}"
    five_hour_resets=$(extract_num "$five_hour_obj" "resets_at")
    # Truncate any fractional seconds (e.g. "1787050233.0") before doing
    # integer arithmetic on it; whole minutes is all the countdown needs,
    # and a bare "." with nothing before it collapses to empty, which the
    # -n check below correctly treats as "no resets_at".
    five_hour_resets=${five_hour_resets%%.*}
    if [ -n "$five_hour_resets" ] && [ "$((five_hour_resets - now_epoch))" -gt 0 ]; then
      five_hour_countdown=$(countdown_str "$five_hour_resets" "$now_epoch")
      five_hour_clock=$(date -d "@$five_hour_resets" '+%H:%M')
      bit="${bit}${DIM} ${five_hour_countdown}${ARROW}${five_hour_clock}${RESET}"
    fi
    usage_bits+=("$bit")
  fi
fi
seven_day_obj=$(extract_obj "seven_day")
if [ -n "$seven_day_obj" ]; then
  seven_day_raw=$(extract_num "$seven_day_obj" "used_percentage")
  if [ -n "$seven_day_raw" ]; then
    seven_day_pct=$(printf '%.0f' "$seven_day_raw")
    bit="$(color_for_pct "$seven_day_pct")7d ${seven_day_pct}%${RESET}"
    seven_day_resets=$(extract_num "$seven_day_obj" "resets_at")
    seven_day_resets=${seven_day_resets%%.*}
    if [ -n "$seven_day_resets" ] && [ "$((seven_day_resets - now_epoch))" -gt 0 ]; then
      seven_day_clock=$(date -d "@$seven_day_resets" '+%a %H:%M')
      bit="${bit}${DIM} ${ARROW}${seven_day_clock}${RESET}"
    fi
    usage_bits+=("$bit")
  fi
fi

usage_part=""
for b in "${usage_bits[@]}"; do
  if [ -z "$usage_part" ]; then
    usage_part="$b"
  else
    usage_part="${usage_part} ${DIM}·${RESET} ${b}"
  fi
done

parts=()
[ -n "$git_part" ] && parts+=("$git_part")
[ -n "$model" ] && parts+=("${CYAN}${model}${RESET}")
[ -n "$version" ] && parts+=("${DIM}v${version}${RESET}")
[ -n "$usage_part" ] && parts+=("$usage_part")

sep="${DIM} | ${RESET}"
out=""
for p in "${parts[@]}"; do
  if [ -z "$out" ]; then
    out="$p"
  else
    out="${out}${sep}${p}"
  fi
done

printf '%s' "$out"
