"""
Validação de consistência doc↔código.

Verifica invariantes do projeto que são declarados nos docs mas que devem
estar presentes no código/CI. Falha com exit code != 0 quando há divergência.

Invariantes verificados:
1. cov-fail-under: deve estar no comando pytest do CI (ci.yml), NÃO no pytest.ini
   (gate global em pytest.ini quebra runs de subconjunto local — decisão arquitetural).
2. cross-refs de docs/system_design/ apontam para arquivos existentes
3. ci.yml roda pytest (gate real de testes presente)
4. ci.yml usa --cov quando roda pytest
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DOCS_DIR = REPO_ROOT / "docs" / "system_design"
PYTEST_INI = REPO_ROOT / "pytest.ini"
PYPROJECT = REPO_ROOT / "pyproject.toml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

FAILURES: list[str] = []
WARNINGS: list[str] = []


def check(condition: bool, msg: str) -> None:
    if not condition:
        FAILURES.append(msg)


def warn(condition: bool, msg: str) -> None:
    if not condition:
        WARNINGS.append(f"[AVISO] {msg}")


def check_cov_fail_under() -> None:
    """--cov-fail-under deve estar no ci.yml (NÃO em pytest.ini/pyproject.toml).

    Decisão arquitetural: o gate de cobertura fica exclusivamente no comando
    pytest do CI para não bloquear runs de subconjunto locais. Se essa flag
    aparecer em pytest.ini ou pyproject.toml [tool.pytest.ini_options], é um
    erro de configuração.
    """
    pytest_ini_text = PYTEST_INI.read_text() if PYTEST_INI.exists() else ""
    pyproject_text = PYPROJECT.read_text() if PYPROJECT.exists() else ""
    ci_text = CI_WORKFLOW.read_text() if CI_WORKFLOW.exists() else ""

    # Deve estar no CI
    check(
        "--cov-fail-under" in ci_text,
        "INVARIANTE VIOLADO: --cov-fail-under não encontrado em ci.yml. "
        "Adicione '--cov-fail-under=<N>' ao comando pytest no job 'test' do CI.",
    )

    # NÃO deve estar em pytest.ini (quebraria runs locais de subconjunto)
    check(
        "--cov-fail-under" not in pytest_ini_text,
        "INVARIANTE VIOLADO: --cov-fail-under encontrado em pytest.ini. "
        "Remova: o gate de cobertura deve ficar exclusivamente no ci.yml "
        "(gate em pytest.ini bloqueia runs locais de subconjunto).",
    )

    # NÃO deve estar em [tool.pytest.ini_options] do pyproject.toml
    # (equivalente a pytest.ini para propósitos do gate)
    in_pytest_section = False
    for line in pyproject_text.splitlines():
        if "[tool.pytest" in line:
            in_pytest_section = True
        elif line.startswith("[") and "[tool.pytest" not in line:
            in_pytest_section = False
        if in_pytest_section and "--cov-fail-under" in line:
            check(
                False,
                "INVARIANTE VIOLADO: --cov-fail-under encontrado em "
                "[tool.pytest.ini_options] do pyproject.toml. "
                "Remova: equivalente a pytest.ini, quebra runs de subconjunto.",
            )


def check_doc_crossrefs() -> None:
    """Links relativos entre docs/system_design/*.md devem apontar para arquivos existentes."""
    if not DOCS_DIR.exists():
        warn(False, f"docs/system_design/ não encontrado em {DOCS_DIR}")
        return

    md_files = list(DOCS_DIR.glob("*.md"))
    link_pattern = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")

    for md_file in md_files:
        content = md_file.read_text(errors="replace")
        for match in link_pattern.finditer(content):
            target = match.group(2)
            if target.startswith("http") or target.startswith("#"):
                continue

            # Remove âncora para checar só o arquivo
            target_path = target.split("#")[0]
            if not target_path:
                continue

            resolved = (DOCS_DIR / target_path).resolve()
            check(
                resolved.exists(),
                f"LINK QUEBRADO em {md_file.name}: [{match.group(1)}]({target}) "
                f"→ {resolved} não existe",
            )


def check_ci_has_pytest() -> None:
    """CI deve rodar pytest (gate real)."""
    if not CI_WORKFLOW.exists():
        check(False, "ci.yml não encontrado")
        return
    ci_text = CI_WORKFLOW.read_text()
    check(
        "pytest" in ci_text,
        "ci.yml não contém 'pytest' — gate de testes ausente",
    )


def check_ci_cov_consistent() -> None:
    """Se ci.yml roda pytest com --cov, deve ter gate de cobertura."""
    if not CI_WORKFLOW.exists():
        return
    ci_text = CI_WORKFLOW.read_text()
    has_cov = "--cov" in ci_text
    has_gate = "--cov-fail-under" in ci_text
    warn(
        not (has_cov and not has_gate),
        "ci.yml usa --cov mas não tem --cov-fail-under (gate de cobertura ausente no CI)",
    )


def main() -> int:
    print("=== Validação de consistência doc↔código ===\n")

    check_cov_fail_under()
    check_doc_crossrefs()
    check_ci_has_pytest()
    check_ci_cov_consistent()

    if WARNINGS:
        for w in WARNINGS:
            print(w)
        print()

    if FAILURES:
        print(f"FALHOU: {len(FAILURES)} invariante(s) violado(s):\n")
        for i, f in enumerate(FAILURES, 1):
            print(f"  {i}. {f}\n")
        return 1

    print(f"OK: todos os invariantes verificados ({len(WARNINGS)} aviso(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
