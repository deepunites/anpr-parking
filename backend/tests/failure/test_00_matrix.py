"""Каждая строка таблицы §5 покрыта существующим автотестом: ссылки матрицы проверяются по собранному дереву."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from tests.failure.matrix import MATRIX, markdown

BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def collected() -> set[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-m", "", "tests"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )
    return {line.strip() for line in result.stdout.splitlines() if "::" in line}


def test_matrix_has_all_eighteen_rows_and_every_test_exists(collected: set[str]) -> None:
    assert len(MATRIX) == 18
    bare = {re.sub(r"\[.*\]$", "", node) for node in collected}  # параметризованные — по имени функции
    missing: list[str] = []
    for _scenario, _expected, tests in MATRIX:
        for test in tests:
            if test not in collected and re.sub(r"\[.*\]$", "", test) not in bare:
                missing.append(test)
    assert missing == [], f"ссылки матрицы §5 на несуществующие тесты: {missing}"


def test_matrix_markdown_is_in_docs() -> None:
    docs = (
        (BACKEND.parent / "docs" / "TESTING.md").read_text()
        if (BACKEND.parent / "docs" / "TESTING.md").exists()
        else ""
    )
    assert markdown().splitlines()[2] in docs, (
        "таблица §5 в docs/TESTING.md устарела — перегенерируйте (tests/failure/matrix.py)"
    )
