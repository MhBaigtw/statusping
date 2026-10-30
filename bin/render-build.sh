#!/usr/bin/env bash
# Render build step for the WEB service (docs/deploy.md). Runs before the new version goes
# live; a failure here leaves the old version serving. Migrations run here only (not in
# the worker's build) so two services never migrate at the same time.
set -o errexit -o nounset -o pipefail

pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate --noinput
python manage.py createcachetable   # throttle counters, status page cache, retention gate

# Idempotent demo data; only when DEMO_PASSWORD is configured on the service.
if [ -n "${DEMO_PASSWORD:-}" ]; then
    python manage.py seed_demo
fi
