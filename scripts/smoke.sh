#!/usr/bin/env bash
# Smoke-test a running deployment end to end:
#   readiness, a 20-buyer race for one seat, and a check that /metrics and the reconcile audit
#   agree with what the buyers were told.
#
# Usage: scripts/smoke.sh <BASE_URL> <ADMIN_KEY>
# Needs: curl, jq.
set -euo pipefail

BASE=${1:?usage: scripts/smoke.sh BASE_URL ADMIN_KEY}
ADMIN_KEY=${2:?usage: scripts/smoke.sh BASE_URL ADMIN_KEY}
BUYERS=20
RUN=$(date +%s)-$$

# Prints a sample's value from /metrics, or 0 if it is absent.
metric() {
  curl -fsS "$BASE/metrics" | awk -v name="$1" '$1 == name { print $2; found = 1 } END { if (!found) print 0 }'
}
delta() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%d", b - a }'; }
fail() { echo "FAIL: $*" >&2; exit 1; }

curl -fsS "$BASE/readyz" > /dev/null || fail "not ready"
confirmed_before=$(metric reservations_confirmed_total)
taken_before=$(metric 'reservations_declined_total{reason="seat_taken"}')

show=$(curl -fsS -X POST "$BASE/shows" -H "X-Admin-Key: $ADMIN_KEY" \
  -H 'content-type: application/json' \
  -d '{"name":"smoke","seats":["A11","A12","A13"],"price_paise":25000}' | jq -r .id)

# All buyers go for A12 at the same moment.
# (-n 1 appends the buyer number as $4; -I would hit BSD xargs' 255-byte limit on macOS.)
statuses=$(seq 1 "$BUYERS" | xargs -P "$BUYERS" -n 1 sh -c '
  token=$(curl -fsS -X POST "$1/auth/token" -H "content-type: application/json" \
    -d "{\"user_id\":\"smoke-$3-$4\"}" | jq -r .access_token)
  curl -s -o /dev/null -w "%{http_code}\n" -X POST "$1/shows/$2/reserve" \
    -H "Authorization: Bearer $token" -H "content-type: application/json" \
    -d "{\"seats\":[\"A12\"]}"
' _ "$BASE" "$show" "$RUN" | sort | uniq -c | awk '{ printf "%s:%s ", $2, $1 }')
echo "outcomes: $statuses"
[ "$statuses" = "201:1 409:$((BUYERS - 1)) " ] || fail "expected exactly one 201 and $((BUYERS - 1)) x 409"

confirmed=$(delta "$confirmed_before" "$(metric reservations_confirmed_total)")
taken=$(delta "$taken_before" "$(metric 'reservations_declined_total{reason="seat_taken"}')")
echo "metrics:  confirmed +$confirmed, seat_taken +$taken"
[ "$confirmed" = 1 ] && [ "$taken" = $((BUYERS - 1)) ] || fail "metrics disagree with outcomes"

report=$(curl -fsS "$BASE/admin/shows/$show/reconcile" -H "X-Admin-Key: $ADMIN_KEY")
echo "reconcile: ok=$(jq -r .ok <<< "$report") counts=$(jq -c .counts <<< "$report")"
[ "$(jq -r .ok <<< "$report")" = true ] || fail "reconcile found problems: $report"

echo "smoke test passed"
