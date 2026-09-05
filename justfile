# Trellmark task runner. Dependencies are managed with uv; everything that
# needs the project environment runs through `uv run`.

app_image := "localhost/trellmark:latest"
deploy_image := "localhost/trellmark:deploy-candidate"
quadlet_dir := env_var("HOME") / ".config/containers/systemd"
local_pod := "trellmark-local"
local_app_container := "trellmark-local-app"
local_db_container := "trellmark-local-db"
local_port := env_var_or_default("TRELLMARK_LOCAL_PORT", "8901")
postgres_image := "docker.io/library/postgres:18-alpine"
db_password_secret := "trellmark-db-password"
db_url_secret := "trellmark-database-url"
public_origin_secret := "trellmark-public-origin"

default:
    @just --list

# Run the complete Trellmark app on 127.0.0.1:8000.
serve:
    uv run python server.py

migrate:
    uv run python server.py migrate

test:
    uv run pytest

typecheck:
    uv run basedpyright trellmark

lint:
    uv run ruff check .

format-check:
    uv run ruff format --check .

format:
    uv run ruff check --fix .
    uv run ruff format .

audit:
    #!/usr/bin/env bash
    set -euo pipefail
    requirements="$(mktemp)"
    cleanup() {
        rm -f "$requirements"
    }
    trap cleanup EXIT
    uv export --locked --all-groups --no-emit-project --quiet --output-file "$requirements"
    uvx pip-audit --disable-pip --no-deps --requirement "$requirements"
    npm audit

check-imports:
    uv run lint-imports

check-frontend-imports:
    npm run check:imports

check: check-api-types check-frontend-artifacts check-imports check-frontend-imports lint format-check typecheck test

check-all: check audit

generate-api-types:
    uv run python scripts/export_openapi.py web/src/generated/openapi.json
    npm run generate:api-types

check-api-types:
    #!/usr/bin/env bash
    set -euo pipefail
    tmpdir="$(mktemp -d)"
    cleanup() {
        rm -rf "$tmpdir"
    }
    trap cleanup EXIT
    uv run python scripts/export_openapi.py "$tmpdir/openapi.json"
    npm run generate:file --workspace trellmark-openapi-types-tool -- "$tmpdir/openapi.json" -o "$tmpdir/openapi.d.ts"
    diff -u web/src/generated/openapi.json "$tmpdir/openapi.json"
    diff -u web/src/generated/openapi.d.ts "$tmpdir/openapi.d.ts"

check-frontend-artifacts:
    node scripts/check_frontend_artifacts.cjs

typecheck-frontend:
    npm run typecheck

build-frontend:
    npm run build

# Local synthetic component overview; no application or database is started.
design-mode: build-frontend
    @echo "http://127.0.0.1:8001/design.html"
    uv run python -m http.server 8001 --bind 127.0.0.1 --directory web

test-api:
    uv run pytest tests/api

playwright:
    uv run playwright install chromium

build: check-api-types build-frontend build-image

image-digests:
    #!/usr/bin/env bash
    set -euo pipefail
    mapfile -t source_images < <(
        bash scripts/container_images.sh sources deploy/Containerfile
    )
    bash scripts/container_images.sh audit \
        "${source_images[@]}" {{postgres_image}} {{app_image}}

deployed-image-digests:
    bash scripts/container_images.sh audit-pod trellmark

build-image:
    #!/usr/bin/env bash
    set -euo pipefail
    mapfile -t source_images < <(
        bash scripts/container_images.sh sources deploy/Containerfile
    )
    podman build --pull=always -t {{app_image}} -f deploy/Containerfile .
    bash scripts/container_images.sh audit "${source_images[@]}" {{app_image}}

# Build and run a disposable local pod. Its application port is loopback-only,
# matching the production contract with the separately deployed edge proxy.
run-container: build
    #!/usr/bin/env bash
    set -euo pipefail
    podman pull --policy=always {{postgres_image}}
    bash scripts/container_images.sh audit {{postgres_image}}
    podman pod rm -f {{local_pod}} >/dev/null 2>&1 || true

    cleanup() {
        podman pod rm -f {{local_pod}} >/dev/null 2>&1 || true
    }
    trap cleanup EXIT

    local_db_password="local-only-$RANDOM"
    local_app_password="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(24))')"
    local_database_url="postgresql+psycopg://trellmark:${local_db_password}@127.0.0.1:5432/trellmark"

    podman pod create --name {{local_pod}} --publish 127.0.0.1:{{local_port}}:8000 >/dev/null
    podman run --detach --pod {{local_pod}} --name {{local_db_container}} \
        --env POSTGRES_USER=trellmark \
        --env POSTGRES_DB=trellmark \
        --env "POSTGRES_PASSWORD=${local_db_password}" \
        --tmpfs /var/lib/postgresql:rw \
        {{postgres_image}} >/dev/null

    echo "Waiting for PostgreSQL..."
    for _ in $(seq 1 60); do
        if podman exec {{local_db_container}} pg_isready --username=trellmark --dbname=trellmark --quiet; then
            break
        fi
        sleep 1
    done
    podman exec {{local_db_container}} pg_isready --username=trellmark --dbname=trellmark

    podman run --rm --pod {{local_pod}} \
        --env "TRELLMARK_DATABASE_URL=${local_database_url}" \
        {{app_image}} python server.py migrate
    printf '%s' "$local_app_password" | podman run --rm --interactive --pod {{local_pod}} \
        --env "TRELLMARK_DATABASE_URL=${local_database_url}" \
        {{app_image}} python server.py set-password --password-stdin
    podman run --detach --pod {{local_pod}} --name {{local_app_container}} \
        --env "TRELLMARK_DATABASE_URL=${local_database_url}" \
        --env "TRELLMARK_PUBLIC_ORIGIN=http://127.0.0.1:{{local_port}}" \
        {{app_image}} >/dev/null

    echo "Trellmark is running at http://127.0.0.1:{{local_port}}"
    echo "Login: admin"
    echo "Password: ${local_app_password}"
    echo "Press Ctrl-C to stop and remove the local test pod."
    podman logs --follow {{local_app_container}}

down-container:
    -podman pod rm -f {{local_pod}}

# Create the database password and encoded DSN as Podman secrets. No secret is
# written to a tracked file or passed as a process argument.
db-secrets:
    #!/usr/bin/env bash
    set -euo pipefail
    urlencode() {
        local LC_ALL=C
        local s="$1" out="" i c
        for (( i=0; i<${#s}; i++ )); do
            c="${s:i:1}"
            case "$c" in
                [a-zA-Z0-9.~_-]) out+="$c" ;;
                *) out+=$(printf '%%%02X' "'$c") ;;
            esac
        done
        printf '%s' "$out"
    }

    IFS= read -rsp "PostgreSQL password for user 'trellmark': " db_password
    echo
    [ -n "$db_password" ] || { echo "Refusing to set an empty password." >&2; exit 1; }
    encoded_password="$(urlencode "$db_password")"
    printf '%s' "$db_password" | podman secret create --replace {{db_password_secret}} -
    printf '%s' "postgresql+psycopg://trellmark:${encoded_password}@127.0.0.1:5432/trellmark" \
        | podman secret create --replace {{db_url_secret}} -
    echo "Created secrets {{db_password_secret}} and {{db_url_secret}}."

# The public origin is not confidential, but keeping it as a Podman secret
# supplies one validated, atomic environment value to the app.
public-origin:
    #!/usr/bin/env bash
    set -euo pipefail
    IFS= read -rp "Public HTTPS origin (https://host[:port]): " public_origin
    case "$public_origin" in
        https://*) ;;
        *) echo "The deployed public origin must start with https://." >&2; exit 1 ;;
    esac
    authority="${public_origin#https://}"
    case "$authority" in
        ""|*/*|*\?*|*\#*|*@*|*\**|*[[:space:]]*)
            echo "Enter one HTTPS origin with no path, credentials, query, fragment, or wildcard." >&2
            exit 1
            ;;
    esac
    printf '%s' "$public_origin" \
        | podman secret create --replace {{public_origin_secret}} -
    echo "Created deployment configuration {{public_origin_secret}}."

set-password:
    #!/usr/bin/env bash
    set -euo pipefail
    podman pod exists trellmark || { echo "The deployed trellmark pod is not running." >&2; exit 1; }
    podman secret exists {{db_url_secret}} || { echo "Missing secret {{db_url_secret}}." >&2; exit 1; }
    podman run --rm --interactive --tty --pod trellmark \
        --secret {{db_url_secret}},type=env,target=TRELLMARK_DATABASE_URL \
        {{app_image}} python server.py set-password

# Install the app/PostgreSQL Quadlets and deploy locally built images. The
# shared Caddy service is intentionally outside this repository and untouched.
deploy:
    #!/usr/bin/env bash
    set -euo pipefail
    podman secret exists {{db_password_secret}} || { echo "Missing secret {{db_password_secret}}; run \`just db-secrets\` first." >&2; exit 1; }
    podman secret exists {{db_url_secret}} || { echo "Missing secret {{db_url_secret}}; run \`just db-secrets\` first." >&2; exit 1; }
    podman secret exists {{public_origin_secret}} || { echo "Missing configuration {{public_origin_secret}}; run \`just public-origin\` first." >&2; exit 1; }

    mapfile -t source_images < <(
        bash scripts/container_images.sh sources deploy/Containerfile
    )
    podman pull --policy=always {{postgres_image}}
    podman build --pull=always -t {{deploy_image}} -f deploy/Containerfile .
    bash scripts/container_images.sh audit \
        "${source_images[@]}" {{postgres_image}} {{deploy_image}}

    mkdir -p {{quadlet_dir}}
    cp deploy/quadlet/* {{quadlet_dir}}/
    systemctl --user daemon-reload
    systemctl --user stop trellmark-pod.service

    unmask_app_best_effort() {
        systemctl --user unmask --runtime trellmark-app.service >/dev/null 2>&1 || true
    }
    trap unmask_app_best_effort EXIT
    systemctl --user mask --runtime trellmark-app.service
    systemctl --user reset-failed trellmark-app.service >/dev/null 2>&1 || true

    systemctl --user start trellmark-postgres.service
    podman healthcheck run trellmark-postgres
    podman run --rm --pod trellmark \
        --secret {{db_url_secret}},type=env,target=TRELLMARK_DATABASE_URL \
        {{deploy_image}} python server.py migrate

    if ! podman run --rm --pod trellmark \
        --secret {{db_url_secret}},type=env,target=TRELLMARK_DATABASE_URL \
        {{deploy_image}} python server.py verify-identity; then
        echo "Initialize the Trellmark administrator password."
        podman run --rm --interactive --tty --pod trellmark \
            --secret {{db_url_secret}},type=env,target=TRELLMARK_DATABASE_URL \
            {{deploy_image}} python server.py set-password
    fi

    podman tag {{deploy_image}} {{app_image}}
    systemctl --user unmask --runtime trellmark-app.service
    systemctl --user reset-failed trellmark-app.service
    state="$(systemctl --user show -p LoadState --value trellmark-app.service)"
    [ "$state" != "masked" ] || { echo "trellmark-app.service is still masked." >&2; exit 1; }
    trap - EXIT

    systemctl --user restart trellmark-pod.service
    systemctl --user start trellmark-app.service
    systemctl --user is-active --quiet trellmark-app.service || { echo "trellmark-app.service did not start after deploy." >&2; exit 1; }

    if ! bash scripts/container_images.sh audit-pod trellmark; then
        echo "Warning: deployment succeeded, but runtime image audit failed." >&2
    fi

backup file="trellmark-backup.dump":
    podman exec trellmark-postgres pg_dump --username=trellmark --format=custom trellmark > {{file}}
    @echo "Wrote {{file}}"

restore file="trellmark-backup.dump":
    podman exec --interactive trellmark-postgres pg_restore --username=trellmark --dbname=trellmark --clean --if-exists < {{file}}

down:
    systemctl --user stop trellmark-pod.service
