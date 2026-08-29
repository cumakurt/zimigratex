from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from dataclasses import replace
from pathlib import Path

from zimigrate import __version__
from zimigrate.archive import MigrationArchive
from zimigrate.backups import discover_backups, prompt_backup_choice
from zimigrate.config import AppConfig, load_config
from zimigrate.errors import ConfigurationError, Interrupted, ZimigrateError
from zimigrate.exporter import Exporter
from zimigrate.importer import Importer
from zimigrate.interrupt import get_interrupt, handle_sigint
from zimigrate.logging_setup import configure_logging
from zimigrate.remote_export import resolve_remote_host, run_remote_export
from zimigrate.scope import (
    apply_scope_to_transfer,
    parse_bound_scope,
    parse_target_scope,
    restore_scope_from_export_options,
    scope_from_transfer,
)
from zimigrate.selection import (
    all_categories,
    exported_categories,
    prompt_categories,
    prompt_domain_selection,
    prompt_import_scope,
    selected_categories,
    transfer_with_categories,
)
from zimigrate.state import StateStore
from zimigrate.target_verifier import TargetVerifier
from zimigrate.verifier import verify_archive
from zimigrate.zimbra import (
    ZimbraClient,
    required_export_commands,
    required_import_commands,
)

DEFAULT_ARCHIVE = Path("export_data")
LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zimigrate",
        description="Resumable Zimbra-to-Zimbra migration",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--verbose", action="store_true", help="enable diagnostic logging")
    parser.add_argument("--json-logs", action="store_true", help="write structured JSON logs")
    commands = parser.add_subparsers(dest="command", required=True)

    command_descriptions = {
        "export": "export Zimbra into a resumable archive; use --target-ip to run it over SSH",
        "import": "validate an archive and import it into the local Zimbra server",
        "verify": "validate archive structure and mailbox artifacts",
        "verify-target": "compare imported destination objects with the archive",
    }
    for name, description in command_descriptions.items():
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument(
            "--config",
            type=Path,
            help="optional local TOML configuration; secure defaults are used when omitted",
        )
        command.add_argument(
            "--archive",
            type=Path,
            default=DEFAULT_ARCHIVE,
            help="archive directory (default: ./export_data)",
        )
        command.add_argument(
            "--user",
            action="append",
            default=[],
            metavar="EMAIL",
            help="limit to this account and its domain (repeatable)",
        )
        command.add_argument(
            "--domain",
            action="append",
            default=[],
            metavar="NAME",
            help="limit to this domain and its accounts (repeatable)",
        )
    commands.choices["verify"].add_argument(
        "--deep", action="store_true", help="scan every mailbox archive"
    )
    commands.choices["export"].add_argument(
        "--target-ip",
        metavar="HOST",
        help="SSH to this Zimbra host, run export there, and store the archive locally",
    )
    commands.choices["export"].add_argument(
        "--ssh-user",
        default="root",
        metavar="NAME",
        help="SSH username (default: root). A password is requested only when key login fails",
    )

    preflight = commands.add_parser(
        "preflight",
        help="check local Zimbra commands and version compatibility",
        description="check local Zimbra commands and version compatibility",
    )
    preflight.add_argument("--config", type=Path)
    preflight.add_argument("--side", choices=("source", "target", "both"), default="source")

    status = commands.add_parser(
        "status",
        help="show checkpoint summaries and failed operations",
        description="show checkpoint summaries and failed operations",
    )
    status.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    arguments = parser.parse_args(argv)
    remote_host = None
    if arguments.command == "export":
        remote_host = resolve_remote_host(arguments.archive, getattr(arguments, "target_ip", None))
    logging_session = configure_logging(
        verbose=arguments.verbose,
        json_logs=arguments.json_logs,
        operation="remote-export" if remote_host else arguments.command,
    )
    interrupt = get_interrupt()
    interrupt.clear()
    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_sigint)
    try:
        if arguments.command == "status":
            state_path = arguments.archive / "state.sqlite3"
            if not state_path.is_file():
                raise ZimigrateError(f"Archive state does not exist: {state_path}")
            state = StateStore(state_path)
            try:
                _print_json(
                    {
                        "operations": state.summary(),
                        "failures": [
                            {
                                "phase": failure.phase,
                                "entity": failure.entity,
                                "attempts": failure.attempts,
                                "detail": failure.detail,
                            }
                            for failure in state.failed()
                        ],
                    }
                )
            finally:
                state.close()
            logging_session.finish("success")
            return 0

        config = load_config(arguments.config)
        if arguments.command in {"export", "import", "verify", "verify-target"}:
            config = _apply_cli_scope(config, arguments)
        if arguments.command == "preflight":
            _print_json(_preflight(config, arguments.side))
            logging_session.finish("success")
            return 0

        if arguments.command == "export":
            archive = MigrationArchive(
                arguments.archive,
                create=True,
            )
            with archive.lock():
                config = _configure_export_categories(config, archive)
                logging_session.start()
                if remote_host:
                    result = run_remote_export(
                        archive,
                        config.transfer,
                        host=remote_host,
                        ssh_user=getattr(arguments, "ssh_user", "root"),
                        verbose=arguments.verbose,
                        json_logs=arguments.json_logs,
                    )
                else:
                    result = Exporter(config, archive).run()
        elif arguments.command == "import":
            archive_path = _resolve_import_archive(arguments.archive, argv)
            archive = MigrationArchive(
                archive_path,
                create=False,
            )
            with archive.lock():
                config = _configure_import_categories(config, archive)
                logging_session.start()
                LOGGER.info("Validating the complete archive before import")
                verification = verify_archive(
                    archive,
                    deep=True,
                    workers=config.transfer.workers,
                )
                LOGGER.info(
                    "Archive validation passed; starting local import",
                    extra={"archive": archive.root},
                )
                import_result = Importer(config, archive).run()
                LOGGER.info("Import completed; validating destination objects")
                result = {
                    "archive_verification": verification,
                    "import": import_result,
                    "target_verification": TargetVerifier(config, archive).run(),
                }
        elif arguments.command == "verify":
            archive = MigrationArchive(
                arguments.archive,
                create=False,
            )
            with archive.lock():
                logging_session.start()
                result = verify_archive(
                    archive,
                    deep=arguments.deep,
                    workers=config.transfer.workers,
                )
        else:
            archive = MigrationArchive(
                arguments.archive,
                create=False,
            )
            with archive.lock():
                logging_session.start()
                result = TargetVerifier(config, archive).run()
        logging_session.finish("success")
        if not logging_session.visual:
            _print_json(result)
        return 0
    except Interrupted:
        interrupt.request()
        logging_session.finish(
            "interrupted",
            detail="Interrupted; rerun the same command to resume",
        )
        print(
            "error: interrupted; rerun the same command to resume",
            file=sys.stderr,
        )
        return 130
    except (ZimigrateError, OSError) as exc:
        logging_session.finish("failed", detail=str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        interrupt.request()
        logging_session.finish(
            "interrupted",
            detail="Interrupted; rerun the same command to resume",
        )
        print(
            "error: interrupted; rerun the same command to resume",
            file=sys.stderr,
        )
        return 130
    finally:
        logging_session.close()
        signal.signal(signal.SIGINT, previous_handler)


def _preflight(config: AppConfig, side: str) -> dict[str, str]:
    result: dict[str, str] = {}
    if side in {"source", "both"}:
        result["source"] = ZimbraClient(
            config.source,
            retries=config.transfer.retries,
            retry_base_seconds=config.transfer.retry_base_seconds,
        ).preflight(
            require_mailbox=config.transfer.include_mailboxes,
            required_provisioning_commands=required_export_commands(config.transfer),
            required_mailbox_commands=(
                {"getRestURL"} if config.transfer.include_mailboxes else set()
            ),
            require_mailbox_output=config.transfer.include_mailboxes,
        )
    if side in {"target", "both"}:
        target_version = ZimbraClient(
            config.target,
            retries=config.transfer.retries,
            retry_base_seconds=config.transfer.retry_base_seconds,
        ).preflight(
            require_mailbox=config.transfer.include_mailboxes,
            required_provisioning_commands=required_import_commands(config.transfer),
            required_mailbox_commands=(
                {"postRestURL"} if config.transfer.include_mailboxes else set()
            ),
        )
        if not config.import_options.allows_version(target_version):
            raise ZimigrateError(
                f"Target version '{target_version}' does not match required pattern "
                f"'{config.import_options.expected_target_version_pattern}'"
            )
        result["target"] = target_version
    return result


def _apply_cli_scope(config: AppConfig, arguments: argparse.Namespace) -> AppConfig:
    users = getattr(arguments, "user", None)
    domains = getattr(arguments, "domain", None)
    if not users and not domains:
        return config
    scope = parse_target_scope(users, domains)
    return replace(config, transfer=apply_scope_to_transfer(config.transfer, scope))


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _configure_export_categories(
    config: AppConfig,
    archive: MigrationArchive,
) -> AppConfig:
    manifest = archive.manifest(optional=True)
    cli_scope = scope_from_transfer(config.transfer)
    if manifest:
        selected = exported_categories(manifest.get("export_options"))
        transfer = transfer_with_categories(config.transfer, selected)
        transfer = restore_scope_from_export_options(transfer, manifest.get("export_options"))
        archived_scope = scope_from_transfer(transfer)
        if cli_scope.active and cli_scope != archived_scope:
            raise ConfigurationError(
                "Resume must use the same --user/--domain values as the existing archive"
            )
        LOGGER.info(
            "Resuming with the archive's locked export categories",
            extra={"categories": sorted(selected)},
        )
        return replace(config, transfer=transfer)
    if cli_scope.active:
        LOGGER.info(
            "Limiting export to selected users and domains",
            extra={**cli_scope.as_options(), "event": "inventory"},
        )
        return config
    if _is_interactive():
        selected = prompt_categories(
            "export",
            available=all_categories(),
            defaults=selected_categories(config.transfer),
        )
    else:
        selected = selected_categories(config.transfer)
    transfer = transfer_with_categories(config.transfer, selected)
    return replace(config, transfer=transfer)


def _configure_import_categories(
    config: AppConfig,
    archive: MigrationArchive,
) -> AppConfig:
    cli_scope = scope_from_transfer(config.transfer)
    bound = archive.state.get("import:configuration", "options")
    if bound and bound.status == "success":
        bound_scope = parse_bound_scope(bound.detail)
        if cli_scope.active and bound_scope.active and cli_scope != bound_scope:
            raise ConfigurationError(
                "Resume must use the same --user/--domain values as the existing import"
            )
        if bound.detail:
            options = json.loads(bound.detail)
            if isinstance(options, dict):
                selected = {
                    key
                    for key, field in (
                        ("domains", "include_domains"),
                        ("cos", "include_cos"),
                        ("accounts", "include_accounts"),
                        ("mailboxes", "include_mailboxes"),
                        ("distribution_lists", "include_distribution_lists"),
                    )
                    if options.get(field, True)
                }
                transfer = transfer_with_categories(config.transfer, selected)
                if bound_scope.active:
                    transfer = apply_scope_to_transfer(transfer, bound_scope)
                LOGGER.info(
                    "Resuming with the import's locked categories",
                    extra={"categories": sorted(selected)},
                )
                return replace(config, transfer=transfer)
    manifest = archive.manifest()
    available = exported_categories(manifest.get("export_options"))
    if cli_scope.active:
        LOGGER.info(
            "Limiting import to selected users and domains",
            extra={**cli_scope.as_options(), "event": "inventory"},
        )
        return config
    domain_records = list(archive.iter_entities("domain")) if "domains" in available else []
    domain_names = [record.name for record in domain_records]
    defaults = available.intersection(selected_categories(config.transfer))
    selected_domains: list[str] = []
    if _is_interactive():
        if prompt_import_scope(has_domains=bool(domain_names)) == "domains":
            selected_domains = prompt_domain_selection(domain_names)
        selected = prompt_categories(
            "import",
            available=available,
            defaults=defaults,
        )
    else:
        selected = set(defaults)
    transfer = transfer_with_categories(config.transfer, selected)
    if selected_domains:
        transfer = apply_scope_to_transfer(
            transfer,
            parse_target_scope([], selected_domains),
        )
        print("Importing selected domain(s): " + ", ".join(selected_domains))
    return replace(config, transfer=transfer)


def _resolve_import_archive(configured: Path, argv: list[str]) -> Path:
    if not _is_interactive() or _has_option(argv, "--archive"):
        return configured
    backups = discover_backups(Path.cwd())
    if not backups:
        return configured
    return prompt_backup_choice(backups, default=configured)


def _has_option(argv: list[str], option: str) -> bool:
    prefix = f"{option}="
    return any(item == option or item.startswith(prefix) for item in argv)


def _is_interactive() -> bool:
    return sys.stdin.isatty()
