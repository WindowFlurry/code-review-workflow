"""Entry point for ``python -m codereview``.

Kept as a one-line shim: the console setup lives in ``cli.main`` so that the
``codereview`` console script and ``python -m codereview`` behave identically.
"""

from __future__ import annotations


def main() -> None:
    from .cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
