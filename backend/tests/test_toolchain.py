import sys
from pathlib import Path


def test_python_version() -> None:
    assert sys.version_info >= (3, 11)


def test_backend_sbom_uses_venv_interpreter() -> None:
    makefile = Path(__file__).resolve().parents[2] / "Makefile"
    contents = makefile.read_text()

    assert (
        "cyclonedx-py environment .venv/bin/python --pyproject pyproject.toml "
        "--output-reproducible -o sbom.cdx.json --of JSON"
    ) in contents
    assert "cyclonedx-py environment .venv --pyproject" not in contents


def test_firestore_image_smoke_closes_pinned_async_client_synchronously() -> None:
    makefile = Path(__file__).resolve().parents[2] / "Makefile"
    contents = makefile.read_text()

    assert contents.count("closed = client.close(); assert closed is None") == 2
    assert "asyncio.run(client.close())" not in contents
    assert (
        'assert importlib.util.find_spec("google.cloud") is None or '
        'importlib.util.find_spec("google.cloud.firestore") is None'
    ) in contents
