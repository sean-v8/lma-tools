"""Original interactive presentation backed by the modular search service."""

import pdb

from .client import ArchiveError
from .fields import ALIASES, SORT_FIELDS, field_name, quoted_clause
from .report import render
from .terminal import FetchDisplay

HELP = """    enter = search
        q = quit

    cache = toggle cache
   reload = reload cache

    trace = set trace
    debug = toggle debugging

        p = print current tags
       pp = print all possible tags

        f = print favorites

        x = clear last tag
       xx = clear all tags + results

        u = uploader
        i = identifier
        a = collection
        v = venue
        c = city
        d = date
        y = year
        n = description

       l_ = list matched tags
       s_ = sort by tag"""


def print_query(clauses, sort, reverse, cache=False):
    print()
    print("sort:" + ("-" if reverse else "") + sort)
    print("cache:" + str(cache))
    for clause in clauses:
        print(clause)


def interactive(service, config, clauses, sort, reverse):
    cache = False
    show_menu = True
    while True:
        try:
            if show_menu:
                print_query(clauses, sort, reverse, cache)
                print("\n" + HELP)
                show_menu = False
            command = input("\n").strip()
            if command == "q":
                return 0
            if command in ("help", "?"):
                print("\n" + HELP)
            elif command == "trace":
                pdb.set_trace()
            elif command == "debug":
                service.timing.enabled = not service.timing.enabled
            elif command == "cache":
                cache = not cache
            elif command == "p":
                print_query(clauses, sort, reverse, cache)
            elif command == "pp":
                print()
                print("\n".join(sorted(SORT_FIELDS)))
            elif command == "x":
                if clauses:
                    clauses.pop()
                print_query(clauses, sort, reverse, cache)
            elif command == "xx":
                clauses.clear()
                service.clear()
                print_query(clauses, sort, reverse, cache)
            elif command in ("", "reload"):
                with FetchDisplay(sort=sort) as display:
                    rows, summary = service.report(
                        clauses,
                        config,
                        sort,
                        reverse,
                        command == "reload",
                        on_progress=display.progress,
                        on_start=display.prepare,
                        reuse_search=cache,
                    )
                if rows or service.timing.enabled:
                    print(render(rows, summary, sort, service.timing.render()))
                for warning in service.warnings:
                    print(f"Warning: {warning}")
                show_menu = True
            elif command == "f":
                if "collection:fav*" not in clauses:
                    clauses.append("collection:fav*")
                favorites = service.favorites(clauses, sort)
                width = max((len(item[0]) for item in favorites), default=0)
                print()
                for identifier, names in favorites:
                    text = ", ".join(names)
                    print(f"{identifier:<{width}}  {text:>4}")
            elif command in ALIASES:
                field = field_name(command)
                clauses.append(quoted_clause(field, input()))
            elif command.startswith("query "):
                clauses.append(command[6:])
            elif command.startswith("l") and command not in SORT_FIELDS:
                field = field_name(command[1:])
                print()
                print(
                    "\n".join(
                        service.list_values(clauses, field, first_only=True)
                    )
                )
            elif command.startswith("s") and command not in SORT_FIELDS:
                value = command[1:] or input()
                try:
                    new_sort = field_name(value.strip("-"))
                except ValueError:
                    print("Not found in tag dictionary.")
                    continue
                sort, reverse = new_sort, value.endswith("-")
            else:
                clauses.append(command + ":" + input("\n"))
        except EOFError:
            print()
            return 0
        except (ValueError, ArchiveError) as exc:
            print(f"Error: {exc}")
