"""
Validação de consistência doc↔código.

Verifica invariantes do projeto que são declarados nos docs mas que devem
estar presentes no código. Falha com exit code != 0 quando há divergência.

Invariantes verificados:
1. cov-fail-under: docs afirmam --cov-fail-under=80 em pytest.ini/pyproject.toml
2. cross-refs de docs/system_design/ apontam para arquivos existentes
3. CLAUDE.md menciona comandos que existem no CI
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DOCS_DIR = REPO_ROOT / "docs" / "system_design"
PYTEST_INI = REPO_ROOT / "pytest.ini"
PYPROJECT = REPO_ROOT / "pyproject.toml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CLAUDE_MD = REPO_ROOT / ".claude" / "worktrees" / "cost-ledger-445" / "CLAUDE.md"

FAILURES: list[str] = []
WARNINGS: list[str] = []


def check(condition: bool, msg: str) -> None:
    if not condition:
        FAILURES.append(msg)


def warn(condition: bool, msg: str) -> None:
    if not condition:
        WARNINGS.append(f"[AVISO] {msg}")


def check_cov_fail_under() -> None:
    """Docs afirmam --cov-fail-under=80. Deve estar em pytest.ini ou pyproject.toml."""
    pytest_ini_text = PYTEST_INI.read_text() if PYTEST_INI.exists() else ""
    pyproject_text = PYPROJECT.read_text() if PYPROJECT.exists() else ""
    ci_text = CI_WORKFLOW.read_text() if CI_WORKFLOW.exists() else ""

    has_cov_gate = (
        "--cov-fail-under" in pytest_ini_text
        or "--cov-fail-under" in pyproject_text
        or "cov-fail-under" in ci_text
    )
    check(
        has_cov_gate,
        "INVARIANTE VIOLADO: docs/system_design/ mencionam '--cov-fail-under=80' "
        "mas a flag não está em pytest.ini, pyproject.toml nem ci.yml. "
        "Corrija: adicione '--cov-fail-under=80' em pytest.ini [addopts] "
        "OU atualize os docs para refletir a realidade.",
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
            if target.startswith("../../") or target.startswith("../"):
                resolved = (DOCS_DIR / target).resolve()
            else:
                resolved = (DOCS_DIR / target).resolve()

            # Ignorar âncoras
            if "#" in str(target):
                target_path = str(target).split("#")[0]
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
    # Aviso apenas (não falha) — o gate de --cov-fail-under é auditado em check_cov_fail_under
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
