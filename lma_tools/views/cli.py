"""CLI entry point and machine-readable report output."""

import argparse
import json
import sys
from dataclasses import asdict

from .client import ArchiveClient, ArchiveError
from .config import load_config
from .fields import field_name
from .report import render
from .service import SearchService
from .timing import TimingRecorder
from .ui import interactive


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("query", nargs="*", help="replace configured clauses")
    result.add_argument("--config", help="private TOML config path")
    result.add_argument("--sort", type=field_name, help="field or short alias")
    result.add_argument(
        "--reverse", action=argparse.BooleanOptionalAction, default=None
    )
    result.add_argument("--once", action="store_true", help="report and exit")
    result.add_argument(
        "--json", action="store_true", help="JSON; implies once"
    )
    result.add_argument(
        "--version", action="version", version="lma-views (unreleased)"
    )
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    client = None
    try:
        config = load_config(arguments.config)
        clauses = list(arguments.query or config.query)
        sort = arguments.sort or config.sort
        reverse = (
            config.reverse if arguments.reverse is None else arguments.reverse
        )
        timing = TimingRecorder()
        client = ArchiveClient(
            config.timeout,
            view_batch_size=config.view_batch_size,
            timing=timing,
        )
        service = SearchService(client, timing=timing)
        if not (arguments.once or arguments.json):
            return interactive(service, config, clauses, sort, reverse)
        rows, summary = service.report(clauses, config, sort, reverse)
        if arguments.json:
            print(
                json.dumps(
                    {
                        "recordings": [asdict(row) for row in rows],
                        "summary": summary,
                        "warnings": service.warnings,
                    },
                    default=str,
                    indent=2,
                )
            )
        else:
            print(render(rows, summary, sort))
        for warning in service.warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        return 1 if service.warnings else 0
    except (ValueError, OSError, ArchiveError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        if client is not None:
            client.close()
