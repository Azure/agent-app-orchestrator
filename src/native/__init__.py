"""Native MAF applications. No classic strategy or persistence bootstrap."""

if __name__ != "native":
    raise RuntimeError(
        "Use the canonical native package: PYTHONPATH=src python -m native.hosted "
        "or python -m native.durable. The src.native alias is unsupported."
    )
