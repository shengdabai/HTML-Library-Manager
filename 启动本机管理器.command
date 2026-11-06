#!/bin/zsh
set -u
cd "${0:A:h}"
python3 - <<'CHECK'
import json, urllib.request, subprocess
try:
    with urllib.request.urlopen('http://127.0.0.1:18455/health', timeout=1) as r:
        data=json.load(r)
    if data.get('app') == 'html-manager':
        subprocess.run(['/usr/bin/open','http://127.0.0.1:18455'], check=True)
        raise SystemExit(17)
except (OSError,ValueError):
    pass
CHECK
result_code=$?
if [[ "$result_code" == "17" ]]; then exit 0; fi
exec python3 manager.py serve --open
