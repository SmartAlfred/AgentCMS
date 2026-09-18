"""Command-line interface for AgentCMS static export (ticket #23).

Usage::

    python -m scripts.cli export --site=blog --out=dist
    python -m scripts.cli export --site=blog --out=dist --incremental --since=<iso>
    python -m scripts.cli export --out=dist --verify
    python -m scripts.cli export --deploy=github-pages --repo=owner/repo --branch=gh-pages --out=dist

The ``--verify`` flag can also run standalone against an existing export.
``--deploy=github-pages`` builds the publish commands; ``--dry-run`` prints
them instead of running them.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

# Ensure repo root is on sys.path so imports work when run as a module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, rejecting garbage."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"error: '{value}' is not a valid ISO-8601 timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentcms", description="AgentCMS CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Export a site to static files")
    export.add_argument("--site", default=None, help="Site slug (default: configured default_site_slug)")
    export.add_argument("--out", default="dist", help="Output directory (default: dist)")
    export.add_argument("--incremental", action="store_true", help="Reuse unchanged files from the manifest")
    export.add_argument("--since", default=None, help="Process posts updated after this ISO-8601 timestamp")
    export.add_argument("--base-url", default=None, help="Public base URL (default: site.base_url)")
    export.add_argument(
        "--generated-at", default=None, help="Pin the feed lastBuildDate timestamp (ISO-8601)"
    )
    export.add_argument("--media-root", default=None, help="Local directory that hosts exportable media")
    export.add_argument("--verify", action="store_true", help="Verify the export against its manifest")
    export.add_argument("--dry-run", action="store_true", help="Only print deploy commands, do not run them")
    export.add_argument(
        "--deploy",
        choices=["github-pages"],
        default=None,
        help="Publish the export to GitHub Pages",
    )
    export.add_argument("--repo", default=None, help="GitHub repository as owner/repo with --deploy")
    export.add_argument("--branch", default="gh-pages", help="Branch to publish to (default: gh-pages)")
    return parser


def _open_session() -> AbstractContextManager[Session]:
    from app.db.session import session_scope

    return session_scope()


def _print_summary(result: Any, verb: str) -> None:
    print(
        f"{verb} {result.site_slug}: {result.post_count} posts, "
        f"{result.file_count} files ({result.reused_files} reused, "
        f"{result.pruned_files} pruned) in {result.elapsed_seconds:.3f}s"
    )
    if not result.verify_ok:
        suffix = "s" if len(result.verify_issues) != 1 else ""
        print(f"verify FAILED ({len(result.verify_issues)} issue{suffix}):")
        for issue in result.verify_issues:
            print(f"  - {issue}")


def _export_cmd(args: argparse.Namespace) -> None:
    from app.config import get_settings, reset_settings_cache

    reset_settings_cache()
    settings = get_settings()

    from app.services.export import (
        ExportError,
        export_site,
        plan_github_pages_deploy,
        read_manifest,
        verify_export,
    )

    out_dir = Path(args.out).expanduser().resolve()

    # Standalone verify mode: no --site and an existing manifest.
    if args.site is None and not args.deploy and (out_dir / "manifest.json").is_file() and args.verify:
        verify = verify_export(out_dir)
        if not verify.ok:
            print(f"verify FAILED ({len(verify.issues)} issues):")
            for issue in verify.issues:
                print(f"  - {issue}")
            raise SystemExit(1)
        print(f"verify OK: {verify.checked_files} files matched manifest.json for {out_dir}")
        return

    site = args.site or settings.default_site_slug

    if args.deploy == "github-pages":
        if not args.repo:
            raise SystemExit("error: --repo=owner/repo is required with --deploy=github-pages")
        if not args.site and read_manifest(out_dir) is None:
            raise SystemExit(
                "error: no export at --out; run an export first, or pass --site to export then deploy"
            )
        commands = plan_github_pages_deploy(out_dir, args.repo, args.branch)
        if args.dry_run:
            print("dry-run: GitHub Pages deploy plan:")
            for cmd in commands:
                print(f"  {cmd}")
            return
        import subprocess

        for cmd in commands:
            subprocess.run(cmd, shell=True, check=True)
        print(f"deployed {out_dir} to https://github.com/{args.repo}/tree/{args.branch}")
        return

    if args.site is None:
        raise SystemExit(
            "error: nothing to do — pass --site=<slug> to export, --verify to check, or --deploy"
        )

    since = _parse_iso(args.since) if args.since else None
    generated_at = _parse_iso(args.generated_at) if args.generated_at else None
    media_root = Path(args.media_root).expanduser().resolve() if args.media_root else None

    try:
        with _open_session() as session:
            result = export_site(
                session,
                site,
                out_dir,
                incremental=args.incremental,
                since=since,
                base_url=args.base_url,
                generated_at=generated_at,
                media_root=media_root,
            )
    except ExportError as exc:
        raise SystemExit(f"error: {exc.detail}") from exc

    _print_summary(result, verb="exported" if not args.incremental else "re-exported")

    if args.verify:
        verify = verify_export(out_dir)
        if not verify.ok:
            print(f"verify FAILED ({len(verify.issues)} issues):")
            for issue in verify.issues:
                print(f"  - {issue}")
            raise SystemExit(1)
        print(f"verify OK: {verify.checked_files} files matched manifest.json for {out_dir}")


def main(argv: list[str] | None = None) -> None:
    """CLI entry-point."""
    args = _build_parser().parse_args(argv)
    if args.command == "export":
        _export_cmd(args)


if __name__ == "__main__":
    main()
