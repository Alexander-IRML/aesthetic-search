from __future__ import annotations

import argparse
from getpass import getpass
import json

from artsearch.deployment.credentials import (
    ensure_session_secret,
    write_neon_connection_string,
    write_qdrant_credentials,
)
from artsearch.deployment.preflight import run_deployment_preflight


DEFAULT_PRODUCTION_CONFIG = "configs/production.alpha-cloud.local.toml"

_ACCOUNTS = (
    ("Cloudflare", "Pages, domain, and four R2 buckets", "https://dash.cloudflare.com/"),
    ("Qdrant Cloud", "one free cluster", "https://cloud.qdrant.io/"),
    ("Hetzner Cloud", "one project; VM creation can wait", "https://console.hetzner.cloud/"),
    ("Modal", "ephemeral GPU inference", "https://modal.com/"),
    ("Neon", "one PostgreSQL 17 canonical metadata project", "https://console.neon.tech/"),
)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if args.command == "accounts":
        payload = [
            {"provider": provider, "resource": resource, "signup_url": url}
            for provider, resource, url in _ACCOUNTS
        ]
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            for provider, resource, url in _ACCOUNTS:
                print(f"{provider}: {resource}\n  {url}")
        return

    if args.command == "configure-qdrant":
        endpoint = args.url.strip() if args.url else input("Qdrant cluster URL: ").strip()
        api_key = getpass("Qdrant database API key (hidden): ")
        try:
            destination = write_qdrant_credentials(
                args.env_file,
                endpoint=endpoint,
                api_key=api_key,
                template=args.template,
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(f"Qdrant credentials stored privately in {destination} (mode 0600).")
        print("The API key was not printed or logged.")
        return

    if args.command == "configure-postgres":
        connection_url = getpass("Neon connection string (hidden): ")
        try:
            destination = write_neon_connection_string(
                args.env_file,
                connection_url=connection_url,
                template=args.template,
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(f"PostgreSQL URLs stored privately in {destination} (mode 0600).")
        print("Pooled and direct URLs were derived without printing either one.")
        return

    if args.command == "generate-session-secret":
        try:
            destination, generated = ensure_session_secret(
                args.env_file,
                template=args.template,
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        status = "generated" if generated else "already present"
        print(f"API session secret {status} in {destination} (mode 0600).")
        print("The secret was not printed or logged.")
        return

    if args.command == "preflight":
        try:
            report = run_deployment_preflight(
                args.production_config,
                phase=args.phase,
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        if args.json:
            print(json.dumps(report.to_dict(), sort_keys=True))
        else:
            for check in report.checks:
                print(f"{check.status.upper():4}  {check.check_id}: {check.message}")
            summary = report.to_dict()["summary"]
            print(
                f"\nphase={report.phase} ready={str(report.ready).lower()} "
                f"pass={summary['pass']} warn={summary['warn']} fail={summary['fail']}"
            )
        if not report.ready:
            raise SystemExit(2)
        return

    raise AssertionError(f"unhandled command: {args.command}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect ArtSearch alpha cloud account and configuration readiness."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    accounts = subparsers.add_parser(
        "accounts",
        help="Print the provider accounts and resources selected by the alpha ADR.",
    )
    accounts.add_argument("--json", action="store_true")

    qdrant = subparsers.add_parser(
        "configure-qdrant",
        help="Prompt for Qdrant credentials and store them in an ignored private env file.",
    )
    qdrant.add_argument("--url", help="Qdrant HTTPS cluster endpoint; prompts when omitted.")
    qdrant.add_argument("--env-file", default="deploy/alpha/.env")
    qdrant.add_argument("--template", default="deploy/alpha/.env.example")

    postgres = subparsers.add_parser(
        "configure-postgres",
        help="Prompt once for a Neon URL and store pooled/direct variants privately.",
    )
    postgres.add_argument("--env-file", default="deploy/alpha/.env")
    postgres.add_argument("--template", default="deploy/alpha/.env.example")

    session = subparsers.add_parser(
        "generate-session-secret",
        help="Generate a private API session secret when one is not already configured.",
    )
    session.add_argument("--env-file", default="deploy/alpha/.env")
    session.add_argument("--template", default="deploy/alpha/.env.example")

    preflight = subparsers.add_parser(
        "preflight",
        help="Validate cloud config and secret presence without contacting providers.",
    )
    preflight.add_argument("--production-config", default=DEFAULT_PRODUCTION_CONFIG)
    preflight.add_argument("--phase", choices=["canary", "api", "full"], default="canary")
    preflight.add_argument("--json", action="store_true")
    return parser


if __name__ == "__main__":
    main()
