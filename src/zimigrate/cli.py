from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path

from zimigrate import __version__
from zimigrate.archive import MigrationArchive
from zimigrate.config import AppConfig, ArchiveConfig, load_config
from zimigrate.errors import ConfigurationError, Interrupted, ZimigrateError
from zimigrate.exporter import Exporter
from zimigrate.importer import Importer
from zimigrate.interrupt import get_interrupt, handle_sigint
from zimigrate.logging_setup import configure_logging
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
    selected_categories,
    transfer_with_categories,
)
from zimigrate.state import StateStore
from zimigrate.target_verifier import TargetVerifier
from zimigrate.verifier import verify_archive
from zimigrate.zimbra import ZimbraClient

DEFAULT_ARCHIVE = Path("export_data")
LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zimigrate",
        description="Encrypted, resumable Zimbra-to-Zimbra migration",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--verbose", action="store_true", help="enable diagnostic logging")
    parser.add_argument("--json-logs", action="store_true", help="write structured JSON logs")
    commands = parser.add_subparsers(dest="command", required=True)

    command_descriptions = {
        "export": "export the local Zimbra server into a resumable archive",
        "import": "validate an archive and import it into the local Zimbra server",
        "verify": "validate archive structure, authentication, and mailbox artifacts",
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
        "--deep", action="store_true", help="decrypt and scan every mailbox archive"
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
    parser = build_parser()
    arguments = parser.parse_args(argv)
    logging_session = configure_logging(
        verbose=arguments.verbose,
        json_logs=arguments.json_logs,
        operation=arguments.command,
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

        passphrase = _prompt_for_archive_passphrase(
            config.archive,
            arguments.archive,
            creating=arguments.command == "export",
        )
        if arguments.command == "export":
            archive = MigrationArchive(
                arguments.archive,
                config.archive,
                create=True,
                passphrase=passphrase,
            )
            with archive.lock():
                config = _configure_export_categories(config, archive)
                logging_session.start()
                result = Exporter(config, archive).run()
        elif arguments.command == "import":
            archive = MigrationArchive(
                arguments.archive,
                config.archive,
                create=False,
                passphrase=passphrase,
            )
            with archive.lock():
                logging_session.start()
                LOGGER.info("Validating the complete archive before import")
                verification = verify_archive(
                    archive,
                    deep=True,
                    workers=config.transfer.workers,
                )
                logging_session.pause()
                config = _configure_import_categories(config, archive)
                logging_session.start()
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
                config.archive,
                create=False,
                passphrase=passphrase,
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
                config.archive,
                create=False,
                passphrase=passphrase,
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
        ).preflight(require_mailbox=config.transfer.include_mailboxes)
    if side in {"target", "both"}:
        target_version = ZimbraClient(
            config.target,
            retries=config.transfer.retries,
            retry_base_seconds=config.transfer.retry_base_seconds,
        ).preflight(require_mailbox=config.transfer.include_mailboxes)
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


def _prompt_for_archive_passphrase(
    config: ArchiveConfig,
    archive_path: Path,
    *,
    creating: bool,
) -> str | None:
    if not config.encryption_enabled:
        return None
    environment_passphrase = config.passphrase()
    if environment_passphrase is not None:
        os.environ.pop(config.passphrase_env, None)
        return environment_passphrase
    if not _is_interactive():
        raise ConfigurationError(
            f"Archive passphrase is required; set {config.passphrase_env} for non-interactive use"
        )
    passphrase = getpass.getpass("Archive passphrase: ")
    is_new_archive = creating and not (archive_path / ".keycheck").is_file()
    if is_new_archive:
        confirmation = getpass.getpass("Confirm archive passphrase: ")
        if passphrase != confirmation:
            raise ConfigurationError("Archive passphrases do not match")
    return passphrase


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
            try:
                options = json.loads(bound.detail)
            except json.JSONDecodeError:
                options = {}
            if isinstance(options, dict):
                selected = {
                    key
                    for key, field in (
                        ("domains", "include_domains"),
                        ("cos", "include_cos"),
                        ("accounts", "include_accounts"),
                        ("mailboxes", "include_mailboxes"),
                        ("distribution_lists", "include_distribution_lists"),
                        ("global_config", "include_global_config"),
                        ("server_config", "include_server_config"),
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
    disabled_reasons: dict[str, str] = {}
    if "global_config" in available and not config.import_options.apply_global_config:
        disabled_reasons["global_config"] = "enable apply_global_config with an allowlist"
    if "server_config" in available and not config.import_options.apply_server_config:
        disabled_reasons["server_config"] = "enable apply_server_config with an allowlist"
    if cli_scope.active:
        LOGGER.info(
            "Limiting import to selected users and domains",
            extra={**cli_scope.as_options(), "event": "inventory"},
        )
        return config
    defaults = available.intersection(selected_categories(config.transfer))
    if _is_interactive():
        selected = prompt_categories(
            "import",
            available=available,
            defaults=defaults,
            disabled_reasons=disabled_reasons,
        )
    else:
        selected = defaults.difference(disabled_reasons)
    transfer = transfer_with_categories(config.transfer, selected)
    return replace(config, transfer=transfer)


def _is_interactive() -> bool:
    return sys.stdin.isatty()
