#!/usr/bin/env bash
set -euo pipefail

mkdir -p /app/logs \
  /app/static_files \
  /app/media/ai_recording \
  /app/media/requirement_docs \
  /app/media/testcase_imports \
  /app/media/testcase_import_failures \
  /app/media/app-automation/allure-results \
  /app/media/app-automation/allure-reports \
  /app/media/app-automation/screenshots \
  /app/media/app-automation/recordings

python manage.py migrate --noinput
python manage.py collectstatic --noinput || true
if [[ -n "${TESTHUB_BOOTSTRAP_ADMIN_USERNAME:-}" && -n "${TESTHUB_BOOTSTRAP_ADMIN_PASSWORD:-}" ]]; then
  python manage.py shell <<'PY'
import os
from apps.users.models import User

username = os.environ["TESTHUB_BOOTSTRAP_ADMIN_USERNAME"]
password = os.environ["TESTHUB_BOOTSTRAP_ADMIN_PASSWORD"]
email = os.environ.get("TESTHUB_BOOTSTRAP_ADMIN_EMAIL") or f"{username}@localhost"
reset_password = os.environ.get("TESTHUB_BOOTSTRAP_ADMIN_RESET_PASSWORD", "").lower() in {"1", "true", "yes"}

user, created = User.objects.get_or_create(
    username=username,
    defaults={
        "email": email,
        "is_active": True,
        "is_staff": True,
        "is_superuser": True,
    },
)

changed = False
if created or reset_password:
    user.set_password(password)
    changed = True

for field, value in {
    "email": email,
    "is_active": True,
    "is_staff": True,
    "is_superuser": True,
}.items():
    if getattr(user, field) != value:
        setattr(user, field, value)
        changed = True

if changed:
    user.save()

print(f"Bootstrap admin user ready: {username} ({'created' if created else 'existing'})")
PY
fi
python manage.py load_component_pack || true

source /app/docker/start-appium.sh

exec "$@"
