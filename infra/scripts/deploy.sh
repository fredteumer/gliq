#!/bin/bash
#
# GreenlightIQ component deploy.
#
# Pushes the working tree to /opt/gliq on one or all component VMs over the
# tailnet, then restarts the systemd unit and shows the first lines of its log.
#
#   ./infra/scripts/deploy.sh all
#   ./infra/scripts/deploy.sh intake --deps
#   ./infra/scripts/deploy.sh scoring --force
#
# This is the project's "CD": there is no pipeline pushing to these hosts. The
# VMs are tailnet-only with public SSH closed, so a hosted runner would need a
# Tailscale auth key of its own — deliberately avoided. See docs/DEPLOYMENT.md.
#
# ⚠️ What lands on the VM is the WORKING TREE, not a pushed commit. The clean
# tree check below is what keeps "what is deployed" answerable; --force skips
# it and marks VERSION accordingly.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

REMOTE_DIR="/opt/gliq"
VENV="${REMOTE_DIR}/.venv"

# One SSH connection per node, reused by every command and by rsync/scp.
#
# A deploy makes roughly eight round trips per node. Without multiplexing each
# one is a separate authentication — which is slow, and matters more than that
# if the tailnet's SSH policy is in check mode: every connection can block on
# an interactive browser check, so a lapsed check period could strand a deploy
# after rsync but before the chown, leaving files the `gliq` user cannot read.
# Multiplexing collapses that to a single auth event per node per deploy.
SSH_CTL_DIR="$(mktemp -d)"
SSH_OPTS=(
    -o ControlMaster=auto
    -o "ControlPath=${SSH_CTL_DIR}/%r@%h:%p"
    -o ControlPersist=60s
    -o ConnectTimeout=10
)
trap 'rm -rf "${SSH_CTL_DIR}"' EXIT

ssh_do()  { ssh "${SSH_OPTS[@]}" "$@"; }
scp_do()  { scp "${SSH_OPTS[@]}" "$@"; }

# Component name -> VM hostname. The names differ on purpose: the package is
# `reporting` (it reports) while the node is `gliq-report`. Keep the mapping
# here rather than papering over it at either end.
declare -A VM_OF=( [intake]=gliq-intake  [scoring]=gliq-scoring  [reporting]=gliq-report )
ALL_COMPONENTS=(intake scoring reporting)

INSTALL_DEPS=0
FORCE=0
SKIP_MIGRATIONS=0
TARGETS=()

#-----------------------------------------------------------------------------
# Arguments
#-----------------------------------------------------------------------------

usage() {
    cat <<EOF
Usage: $(basename "$0") <intake|scoring|reporting|report|all> [--deps] [--force] [--no-migrate]

  --deps    Create the venv if absent and (re)install that component's extras.
            Off by default: dependency installs are slow and rarely change,
            and paying that cost on every deploy would defeat the point.
  --force   Deploy a dirty working tree. VERSION is stamped '<sha>-dirty'.
  --no-migrate
            Skip 'alembic upgrade head'. Migrations otherwise run once,
            before any node is touched.
            (Single quotes, not backticks: this heredoc is unquoted so that
            \$(basename) expands, which means backticks would execute too.)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        all)                TARGETS=("${ALL_COMPONENTS[@]}") ;;
        intake|scoring|reporting) TARGETS+=("$1") ;;
        report)             TARGETS+=(reporting) ;;   # accept the VM's name too
        --deps)             INSTALL_DEPS=1 ;;
        --force)            FORCE=1 ;;
        --no-migrate)       SKIP_MIGRATIONS=1 ;;
        -h|--help)          usage; exit 0 ;;
        *)                  echo "❌ Unknown argument: $1"; usage; exit 1 ;;
    esac
    shift
done

if [ ${#TARGETS[@]} -eq 0 ]; then
    echo "❌ No component specified"
    usage
    exit 1
fi

#-----------------------------------------------------------------------------
# Provenance
#-----------------------------------------------------------------------------

SHA="$(git rev-parse --short HEAD)"

if [ -n "$(git status --porcelain)" ]; then
    if [ "${FORCE}" -eq 0 ]; then
        echo "🛑 Working tree is dirty — refusing to deploy."
        echo "   What runs on the VM would not correspond to any commit."
        git status --short | sed 's/^/     /'
        echo "   Commit, stash, or re-run with --force."
        exit 1
    fi
    echo "⚠️ Deploying a DIRTY tree (--force) — VERSION will be marked accordingly"
    SHA="${SHA}-dirty"
fi

STAMP="$(date -Is)"
echo "🚀 Deploying ${SHA} to: ${TARGETS[*]}"

#-----------------------------------------------------------------------------
# EnvironmentFile
#-----------------------------------------------------------------------------
# Generated once, locally, from Pulumi stack outputs, then shipped to every
# node. It is generated here rather than on the VM because it derives from the
# stack, and infra/ is deliberately excluded from the sync — a node has no
# Pulumi and no business having it.

ENV_FILE="${SSH_CTL_DIR}/gliq.env"

echo "🔒 Generating env file from stack outputs..."
if python3 infra/env-from-stack.py --target "${ENV_FILE}" --no-secrets; then
    echo "✅ Env file generated"
else
    echo "⚠️ Could not generate the env file from the Pulumi stack."
    echo "   Units will fail to start without /etc/gliq/gliq.env."
    rm -f "${ENV_FILE}"
fi

#-----------------------------------------------------------------------------
# Schema migrations
#-----------------------------------------------------------------------------
# `alembic upgrade head` compares the alembic_version table against the
# migration chain and applies only what is missing — idempotent, so running it
# on every deploy is safe.
#
# ⚠️ ONCE, here, before the per-node loop — NOT inside deploy_one(). Three nodes
# each running migrations would race for the same alembic_version lock to do
# identical work. Migrations are a property of the database, not of a node.
#
# Run from the deploy host, which reaches Cloud SQL over the tailnet subnet
# route (`tailscale up --accept-routes`). The nodes reach it directly on the
# VPC and never need Alembic — it is not in any component's extras.

if [ "${SKIP_MIGRATIONS}" -eq 1 ]; then
    echo "⏸️ Skipping migrations (--no-migrate)"
elif ! grep -q '^DB_HOST=.\+' .env 2>/dev/null; then
    echo "⏸️ Skipping migrations — DB_HOST is empty (enableDatabase is off)"
else
    ALEMBIC=""
    for candidate in .venv-etl/bin/alembic .venv/bin/alembic "$(command -v alembic || true)"; do
        [ -x "${candidate}" ] && { ALEMBIC="${candidate}"; break; }
    done

    if [ -z "${ALEMBIC}" ]; then
        echo "🛑 alembic not found — install it with:"
        echo "     python3 -m venv .venv-etl && .venv-etl/bin/pip install -e '.[etl]'"
        echo "   or re-run with --no-migrate to deploy code without touching the schema."
        exit 1
    fi

    echo "🗄️ Applying schema migrations..."
    "${ALEMBIC}" upgrade head || {
        # Deploying code against a schema it does not match is how you get
        # errors far away from their cause.
        echo "❌ Migration failed — refusing to deploy code against a stale schema"
        exit 1
    }
    echo "✅ Schema at head"
fi

#-----------------------------------------------------------------------------
# Deploy
#-----------------------------------------------------------------------------
# rsync as root: /opt/gliq is owned by the unprivileged `gliq` service user, so
# the transfer needs write access it does not have, and ownership is restored
# immediately after. Tailscale SSH authenticates this at the tailnet layer.

deploy_one() {
    local component="$1"
    local vm="${VM_OF[$component]}"

    echo ""
    echo "───────────────────────────────────────────────"
    echo "📦 ${component} → ${vm}"
    echo "───────────────────────────────────────────────"

    if ! ssh_do "root@${vm}" true 2>/dev/null; then
        echo "❌ ${vm} is unreachable over the tailnet"
        echo "   Is the VM up? \`tailscale status\` / \`pulumi up\`"
        return 1
    fi

    # rsync must exist on BOTH ends — it is not in the Debian cloud image.
    # node-startup.sh installs it, but a node bootstrapped before that change
    # will not have it, so repair it here rather than failing the deploy.
    if ! ssh_do "root@${vm}" "command -v rsync >/dev/null"; then
        echo "⚠️ rsync missing on ${vm} — installing"
        ssh_do "root@${vm}" "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rsync" \
            || { echo "❌ Could not install rsync on ${vm}"; return 1; }
    fi

    # --delete so a file removed locally is removed on the VM. Without it a
    # renamed module leaves its old copy behind and can still be imported.
    # infra/ is excluded: Pulumi and node_modules have no business on a node.
    #
    # ⚠️ rsync does NOT read .gitignore, so every gitignored-but-bulky path has
    # to be excluded by hand here. Two that matter: data/raw/ is the ~863MB
    # Kaggle export (ETL input, never needed on a node) and .venv-etl/ is a
    # ~239MB host-architecture virtualenv that would be useless on the VM even
    # if it fitted. Together they were ~1.1GB per node per deploy onto a 20GB
    # boot disk. Anything else added under data/ should be checked against this
    # list before the next deploy.
    echo "📤 Syncing..."
    rsync -a --delete --compress \
        -e "ssh ${SSH_OPTS[*]}" \
        --exclude '.git/' \
        --exclude 'infra/' \
        --exclude '__pycache__/' \
        --exclude '*.pyc' \
        --exclude '.venv/' \
        --exclude '.venv-etl/' \
        --exclude '.env' \
        --exclude 'data/raw/' \
        --exclude 'node_modules/' \
        --exclude '.pytest_cache/' \
        --exclude '*.egg-info/' \
        --exclude 'WIP*.md' \
        ./ "root@${vm}:${REMOTE_DIR}/" \
        || { echo "❌ rsync to ${vm} failed"; return 1; }

    ssh_do "root@${vm}" "printf '%s\n%s\n' '${SHA}' '${STAMP}' > ${REMOTE_DIR}/VERSION" \
        || { echo "❌ Could not stamp VERSION on ${vm}"; return 1; }

    # The systemd EnvironmentFile. Generated locally because it comes from
    # Pulumi stack outputs and infra/ is deliberately not synced to the nodes.
    # Without this the unit cannot start at all — systemd treats a missing
    # EnvironmentFile as a hard failure, which is what "unavailable resources"
    # means in `systemctl status`.
    if [ -f "${ENV_FILE}" ]; then
        echo "🔒 Installing /etc/gliq/gliq.env..."
        scp_do -q "${ENV_FILE}" "root@${vm}:/etc/gliq/gliq.env" \
            || { echo "❌ Could not copy env file to ${vm}"; return 1; }
        # Readable by the service user, nobody else. It holds credentials.
        ssh_do "root@${vm}" "chown root:gliq /etc/gliq/gliq.env && chmod 0640 /etc/gliq/gliq.env" \
            || { echo "❌ Could not set env file permissions on ${vm}"; return 1; }
    else
        echo "⚠️ No local env file — skipping (the unit will fail to start)"
    fi

    # Dependencies, only on request.
    if [ "${INSTALL_DEPS}" -eq 1 ]; then
        echo "📦 Installing ${component} dependencies (slow on an e2-small)..."
        ssh_do "root@${vm}" "
            set -e
            if [ ! -d ${VENV} ]; then
                echo '   creating venv at ${VENV}'
                python3 -m venv ${VENV}
            fi
            ${VENV}/bin/pip install --quiet --upgrade pip
            ${VENV}/bin/pip install --quiet -e '${REMOTE_DIR}[${component}]'
        " || { echo "❌ Dependency install failed on ${vm}"; return 1; }
        echo "✅ Dependencies installed"
    elif ! ssh_do "root@${vm}" "[ -x ${VENV}/bin/python ]"; then
        # Bootstrap does not create the venv, so a first deploy to a fresh node
        # will land here. Fail loudly rather than creating it silently — a
        # missing venv can also mean a half-provisioned VM, and quietly fixing
        # it would hide that.
        echo "🛑 No venv at ${VENV} — re-run with --deps to create it"
        return 1
    fi

    # Ownership last: everything above ran as root and would otherwise leave
    # root-owned files that the `gliq` service user cannot read.
    ssh_do "root@${vm}" "chown -R gliq:gliq ${REMOTE_DIR}" \
        || { echo "❌ Could not chown ${REMOTE_DIR} on ${vm}"; return 1; }

    # Install/refresh the unit file, then restart.
    local unit="gliq-${component}"
    [ "${component}" = "reporting" ] && unit="gliq-report"

    echo "🔧 Installing ${unit}.service..."
    scp_do -q "infra/systemd/${unit}.service" "root@${vm}:/etc/systemd/system/${unit}.service" \
        || { echo "❌ Could not install ${unit}.service on ${vm}"; return 1; }
    ssh_do "root@${vm}" "systemctl daemon-reload && systemctl enable --quiet ${unit}" \
        || { echo "❌ Could not enable ${unit} on ${vm}"; return 1; }

    echo "🔧 Restarting ${unit}..."
    # A failed start is NOT a failed deploy: the code is on the node and the
    # unit is installed. It is reported separately in the summary so the
    # distinction stays visible instead of being rounded off to success.
    ssh_do "root@${vm}" "systemctl restart ${unit}" || true

    sleep 2
    echo ""
    if ssh_do "root@${vm}" "systemctl is-active --quiet ${unit}"; then
        echo "✅ ${unit} is active"
    else
        echo "⚠️ ${unit} is not active — recent log:"
        INACTIVE+=("${unit}")
    fi
    ssh_do "root@${vm}" "journalctl -u ${unit} -n 15 --no-pager -o cat" | sed 's/^/     /'
}

FAILED=()
INACTIVE=()
for component in "${TARGETS[@]}"; do
    deploy_one "${component}" || FAILED+=("${component}")
done

#-----------------------------------------------------------------------------
# Summary
#-----------------------------------------------------------------------------

echo ""
echo "───────────────────────────────────────────────"

SUCCEEDED=$(( ${#TARGETS[@]} - ${#FAILED[@]} ))

if [ ${#FAILED[@]} -gt 0 ]; then
    echo "❌ Deploy FAILED on: ${FAILED[*]}"
    echo "   Succeeded on ${SUCCEEDED} of ${#TARGETS[@]} node(s)"
    exit 1
fi

echo "✅ Deployed ${SHA} to ${SUCCEEDED} node(s) at ${STAMP}"

# Reported, never folded into the exit code: during early development the
# components are stubs and are *expected* to exit immediately. Saying so
# plainly beats both a false ✅ and a misleading ❌.
if [ ${#INACTIVE[@]} -gt 0 ]; then
    echo "⚠️ Not running: ${INACTIVE[*]}"
    echo "   Expected while the components are stubs; check the logs above."
fi
