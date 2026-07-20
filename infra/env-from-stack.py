#!/usr/bin/env python3
"""Write Pulumi stack outputs into an env file.

The three components read their configuration from environment variables
(``shared/config.py``). Those values — topic names, bucket, project — are
produced by the Pulumi stack, so copying them by hand is a standing source of
drift: rename a topic in ``index.ts`` and every stale ``.env`` silently points
at a topic that no longer exists.

This script regenerates a *managed block* inside the target file and leaves
everything outside that block untouched, so hand-added values (API keys, local
overrides) survive. Re-running it is idempotent.

Usage::

    python3 infra/env-from-stack.py                 # -> .env at repo root
    python3 infra/env-from-stack.py --target /etc/gliq/gliq.env --no-secrets
    python3 infra/env-from-stack.py --stack prod --print

On the VMs the same script generates the systemd ``EnvironmentFile``. Pass
``--no-secrets`` there: systemd reads the file as root at unit start, so it
should carry only what the *component* needs, never local tooling credentials
such as the Pulumi passphrase.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

INFRA_DIR = Path(__file__).resolve().parent
REPO_ROOT = INFRA_DIR.parent

BEGIN = "# --- BEGIN pulumi-managed (infra/env-from-stack.py) — do not edit by hand ---"
END = "# --- END pulumi-managed ---"

#: Matches a managed block by its *stable prefix* rather than the full marker.
#:
#: ⚠️ Matching on the exact BEGIN string is fragile: editing the marker text
#: orphans every block written by an earlier version. The script then finds no
#: block, appends a second one, and the file ends up with two — which actually
#: happened, and was survivable only because dotenv keeps the LAST assignment
#: and the stale block happened to sit above the new one. A trailing-comment
#: change must not be able to silently double the file.
BLOCK_RE = re.compile(
    r"^# --- BEGIN pulumi-managed.*?^# --- END pulumi-managed ---[^\n]*\n?",
    re.DOTALL | re.MULTILINE,
)

#: Stack output name -> environment variable name.
#:
#: Keep the right-hand side in sync with ``shared/config.py``. An output that
#: is not listed here is deliberately not exported; adding one is a one-line
#: change here plus the matching field on ``Config``.
OUTPUT_TO_ENV: dict[str, str] = {
    "gcpProject": "GCP_PROJECT_ID",
    "gcpRegion": "GCP_REGION",
    "topicScoringRequested": "PUBSUB_TOPIC_SCORING_REQUESTED",
    "subScoringRequested": "PUBSUB_SUB_SCORING_REQUESTED",
    "topicScoringCompleted": "PUBSUB_TOPIC_SCORING_COMPLETED",
    "subScoringCompleted": "PUBSUB_SUB_SCORING_COMPLETED",
    "deadLetterTopic": "PUBSUB_DEAD_LETTER_TOPIC",
    "artifactsBucket": "GCS_ARTIFACTS_BUCKET",
    "intakeStaticIp": "INTAKE_STATIC_IP",
    # Empty strings when `enableDatabase` is false — see index.ts. The
    # components fall back to the defaults in shared/config.py, which is the
    # right behaviour for a session running against a local corpus instead.
    "dbHost": "DB_HOST",
    "dbName": "DB_NAME",
    "dbUser": "DB_USER",
    "dbPassword": "DB_PASSWORD",
    # Component A's web UI. Empty until `pulumi config set --secret` is run for
    # each; Component A treats an empty ADMIN_PASSWORD_HASH as "no valid
    # password", so an unconfigured stack locks the login gate rather than
    # leaving it open.
    "sessionSecret": "SESSION_SECRET",
    "adminPasswordHash": "ADMIN_PASSWORD_HASH",
}

#: Outputs Pulumi marks secret. They are redacted from `pulumi stack output`
#: unless --show-secrets is passed, and would otherwise be written out as the
#: literal string "[secret]" — which fails at connect time rather than here,
#: where it would be obvious.
SECRET_OUTPUTS = {"dbPassword"}


def read_passphrase_from(target: Path) -> str | None:
    """Recover PULUMI_CONFIG_PASSPHRASE from an existing env file.

    Convenience only: it means `python3 infra/env-from-stack.py` works with no
    exported environment when the passphrase already lives in .env.
    """
    if not target.is_file():
        return None
    for line in target.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "PULUMI_CONFIG_PASSPHRASE":
            return value.strip().strip("\"'")
    return None


def stack_outputs(stack: str | None, env: dict[str, str]) -> dict[str, object]:
    cmd = ["pulumi", "stack", "output", "--json", "--show-secrets"]
    if stack:
        cmd += ["--stack", stack]
    proc = subprocess.run(cmd, cwd=INFRA_DIR, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise SystemExit(f"pulumi stack output failed:\n{detail}")
    return json.loads(proc.stdout)


def build_block(outputs: dict[str, object]) -> tuple[str, list[str]]:
    """Render the managed block. Returns (text, names of missing outputs)."""
    lines = [BEGIN, "# Regenerate after every `pulumi up`."]
    missing: list[str] = []
    for output_name, env_name in OUTPUT_TO_ENV.items():
        if output_name not in outputs:
            missing.append(output_name)
            continue
        lines.append(f"{env_name}={outputs[output_name]}")
    lines.append(END)
    return "\n".join(lines), missing


def splice(existing: str, block: str) -> tuple[str, int]:
    """Replace the managed block, or append it if not yet present.

    Collapses *every* managed block down to one. Returns the new text and the
    number of pre-existing blocks found, so the caller can report a cleanup.
    """
    found = len(BLOCK_RE.findall(existing))
    if found:
        # Keep the first block's position — it is usually above any hand-added
        # values, and dotenv's last-assignment-wins means moving it could
        # change which value applies. Drop the rest.
        replaced = [False]

        def _sub(_match: re.Match[str]) -> str:
            if replaced[0]:
                return ""
            replaced[0] = True
            return block + "\n"

        return BLOCK_RE.sub(_sub, existing), found
    if existing.strip():
        return f"{existing.rstrip()}\n\n{block}\n", 0
    return f"{block}\n", 0


def shadowed_outside_block(existing: str, managed: set[str]) -> list[str]:
    """Managed vars also assigned outside the block.

    dotenv keeps the *last* assignment, so a stray duplicate further down the
    file would silently win over the generated value.
    """
    without_block = BLOCK_RE.sub("", existing)
    found = []
    for line in without_block.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in managed:
            found.append(key)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--stack", help="Pulumi stack (default: currently selected)")
    parser.add_argument(
        "--target",
        type=Path,
        default=REPO_ROOT / ".env",
        help="env file to update (default: <repo>/.env)",
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="write nothing; print the block to stdout",
    )
    parser.add_argument(
        "--no-secrets",
        action="store_true",
        help=(
            "strip local TOOLING credentials (use when generating for systemd). "
            "Component credentials such as DB_PASSWORD are kept — the services "
            "cannot run without them; the file is 0640 root:gliq on the VM"
        ),
    )
    args = parser.parse_args()

    env = dict(os.environ)
    if "PULUMI_CONFIG_PASSPHRASE" not in env:
        recovered = read_passphrase_from(REPO_ROOT / ".env")
        if recovered:
            env["PULUMI_CONFIG_PASSPHRASE"] = recovered

    outputs = stack_outputs(args.stack, env)
    block, missing = build_block(outputs)

    for name in missing:
        print(f"⚠️  stack output '{name}' not found — skipped", file=sys.stderr)

    if args.print_only:
        print(block)
        return 0

    target: Path = args.target
    existing = target.read_text() if target.is_file() else ""

    shadowed = shadowed_outside_block(existing, set(OUTPUT_TO_ENV.values()))
    for key in shadowed:
        print(
            f"⚠️  {key} is also set outside the managed block in {target} — "
            "the later assignment wins; remove the stray one",
            file=sys.stderr,
        )

    updated, blocks_found = splice(existing, block)
    if blocks_found > 1:
        print(
            f"🧹 collapsed {blocks_found} managed blocks into 1 — the extras were "
            "written by an older marker format and were shadowing each other",
            file=sys.stderr,
        )

    if args.no_secrets:
        updated = "\n".join(
            line
            for line in updated.splitlines()
            if not line.strip().startswith("PULUMI_CONFIG_PASSPHRASE")
        ) + "\n"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(updated)
    # These files hold credentials; keep them owner-only.
    target.chmod(0o600)

    count = len(OUTPUT_TO_ENV) - len(missing)
    print(f"✅ wrote {count} variables to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
