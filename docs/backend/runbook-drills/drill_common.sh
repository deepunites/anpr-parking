# общие функции прогонов runbook на стенде (e2e_stack.py :8010, Redis :6390/2)
export PATH=~/.local/bin:$PATH
B=/home/deep/projects/anpr-parking/backend
STATE=/home/deep/projects/anpr-parking/frontend/e2e/.stack.json
API=$(python3 -c "import json;print(json.load(open('$STATE'))['api'])")
ENTRY_KEY=$(python3 -c "import json;print(json.load(open('$STATE'))['camera_keys']['entry'])")
REDIS="$B/.redis/bin/redis-cli -p 6390 -n 2"
PSQL="$B/.venv/lib/python3.12/site-packages/pgserver/pginstall/bin/psql"
run() { echo; echo "\$ $*"; eval "$@" 2>&1 | sed 's/^/  /' | cut -c1-220; }
ts() { date -u +%H:%M:%S; }
camera_event() {  # $1 plate; печатает HTTP-код и статус
  curl -s -o /tmp/ev.json -w "%{http_code}" -X POST "$API/api/v1/camera/events" -H "X-Camera-Key: $ENTRY_KEY" -H "Content-Type: application/json" \
    -d "{\"camera_code\":\"ENTRY_01\",\"event_type\":\"ENTRY\",\"plate_number\":\"$1\",\"confidence\":96,\"event_time\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"direction\":\"IN\"}"
  echo " $(python3 -c "import json;d=json.load(open('/tmp/ev.json'));print(d.get('status') or d.get('error',{}).get('code'), 'queued=%s'%d.get('queued'))")"
}
token() { curl -s -X POST "$API/api/v1/auth/login" -H "Content-Type: application/json" -d "{\"email\":\"$1\",\"password\":\"$2\"}" | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])"; }
metric() { curl -s "$API/metrics" | grep -E "^$1" | head -${2:-3}; }
