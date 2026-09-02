"""Command entrypoint: python -m bizcard_intake [bot|scan|save|retry-pending|doctor|setup]."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
