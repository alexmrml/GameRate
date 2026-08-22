#!/usr/bin/env bash
set -Eeuo pipefail

readonly BASE_URL="${1%/}"
readonly USERNAME="${2:-}"

if [[ ! "${BASE_URL}" =~ ^https://[^/]+$ || -z "${USERNAME}" ]]; then
  echo "usage: $0 https://public-name.share.zrok.io username" >&2
  exit 2
fi

read -r -s -p "Password: " PASSWORD
echo
readonly WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
readonly COOKIES="${WORK_DIR}/cookies"
readonly HEADERS="${WORK_DIR}/headers"
readonly BODY="${WORK_DIR}/body"
readonly ZROK_HEADER='skip_zrok_interstitial: true'

status="$(curl -sS -o /dev/null -D "${HEADERS}" -w '%{http_code}' \
  -H "${ZROK_HEADER}" "${BASE_URL}/games")"
[[ "${status}" == "303" ]]
grep -Eiq '^location:[[:space:]]*/login\?next=/games' "${HEADERS}"

status="$(printf '%s' "${PASSWORD}" | \
  curl -sS -o /dev/null -D "${HEADERS}" -c "${COOKIES}" -w '%{http_code}' \
    -H "${ZROK_HEADER}" \
    --data-urlencode "username=${USERNAME}" \
    --data-urlencode 'password@-' \
    --data-urlencode 'next=/games' \
    "${BASE_URL}/login")"
[[ "${status}" == "303" ]]
grep -Eiq '^location:[[:space:]]*/games' "${HEADERS}"
grep -Eiq '^set-cookie:[[:space:]]*gamerate_session=' "${HEADERS}"
grep -Eiq '^set-cookie:.*HttpOnly' "${HEADERS}"
grep -Eiq '^set-cookie:.*SameSite=lax' "${HEADERS}"
grep -Eiq '^set-cookie:.*Secure' "${HEADERS}"

curl -fsS -H "${ZROK_HEADER}" -b "${COOKIES}" "${BASE_URL}/activity" >"${BODY}"
csrf_token="$(sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p' "${BODY}" | head -n 1)"
[[ -n "${csrf_token}" ]]

status="$(curl -sS -o /dev/null -w '%{http_code}' -H "${ZROK_HEADER}" -b "${COOKIES}" \
  --data-urlencode 'csrf_token=invalid' "${BASE_URL}/logout")"
[[ "${status}" == "403" ]]

set +e
curl -fsS -N --max-time 6 -H "${ZROK_HEADER}" -b "${COOKIES}" \
  "${BASE_URL}/activity/events" >"${BODY}"
sse_status=$?
set -e
[[ "${sse_status}" == "0" || "${sse_status}" == "28" ]]
grep -q '^event: activity' "${BODY}"
grep -q '^data: {' "${BODY}"

status="$(curl -sS -o /dev/null -D "${HEADERS}" -w '%{http_code}' -H "${ZROK_HEADER}" \
  -b "${COOKIES}" --data-urlencode "csrf_token=${csrf_token}" "${BASE_URL}/logout")"
[[ "${status}" == "303" ]]
grep -Eiq '^location:[[:space:]]*/login' "${HEADERS}"

echo "Smoke test passed: redirects, secure cookie, CSRF and SSE"
