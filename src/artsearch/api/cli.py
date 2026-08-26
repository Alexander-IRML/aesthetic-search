from __future__ import annotations

import argparse

from artsearch.api.app import create_app
from artsearch.api.config import load_search_api_config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the ArtSearch public image-search API and frontend."
    )
    parser.add_argument("--app-config", default="config/config.yaml")
    parser.add_argument("--production-config", default="configs/production.default.toml")
    parser.add_argument("--api-config", default="configs/search_api.default.toml")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--frontend-preview",
        action="store_true",
        help="Serve the exact frontend without initializing models or search dependencies.",
    )
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Install API dependencies with: pip install -e '.[api]'") from exc

    config = load_search_api_config(args.api_config)
    application = create_app(
        api_config=config,
        app_config_path=args.app_config,
        production_config_path=args.production_config,
        api_config_path=args.api_config,
        initialize_search_service=not args.frontend_preview,
    )
    uvicorn.run(
        application,
        host=args.host or config.host,
        port=args.port or config.port,
        log_level=args.log_level,
        proxy_headers=config.trust_proxy_headers,
        forwarded_allow_ips="*" if config.trust_proxy_headers else "127.0.0.1",
        server_header=False,
    )


if __name__ == "__main__":
    main()
